from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

DEFAULT_SETTINGS = {
    "sequence_weight": 1.0,
    "time_weight": 0.85,
    "location_weight": 0.8,
    "recency_weight": 0.45,
    "novelty_weight": 0.35,
    "hour_bandwidth": 2.5,
    "session_gap_minutes": 30,
    "exclude_last_n": 3,
    "search_logging_enabled": False,
}

DEFAULT_STATS_SETTINGS = {
    "work_start_hour": 8,
    "work_end_hour": 16,
    "work_weekdays": [0, 1, 2, 3, 4],
}

_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_played_at(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    played = datetime.fromisoformat(normalized)
    if played.tzinfo is None:
        return played.replace(tzinfo=timezone.utc)
    return played.astimezone(timezone.utc)


class ListeningStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with _lock, self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    image TEXT,
                    url TEXT,
                    played_at TEXT NOT NULL,
                    duration_ms INTEGER,
                    artist_ids_json TEXT NOT NULL DEFAULT '[]',
                    place_id INTEGER,
                    latitude REAL,
                    longitude REAL,
                    UNIQUE(track_id, played_at),
                    FOREIGN KEY(place_id) REFERENCES places(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS places (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    radius_km REAL NOT NULL DEFAULT 0.4
                );

                CREATE TABLE IF NOT EXISTS artist_genres (
                    artist_id TEXT PRIMARY KEY,
                    artist_name TEXT NOT NULL,
                    genres_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listening_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recommendation_sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    context_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recommendation_items (
                    session_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    track_json TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    PRIMARY KEY(session_id, track_id),
                    FOREIGN KEY(session_id) REFERENCES recommendation_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS recommendation_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('played', 'saved', 'liked', 'not_for_me')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id, track_id)
                        REFERENCES recommendation_items(session_id, track_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS search_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    searched_at TEXT NOT NULL,
                    local_hour REAL NOT NULL,
                    weekday INTEGER NOT NULL,
                    place_id INTEGER,
                    selected_track_id TEXT,
                    FOREIGN KEY(place_id) REFERENCES places(id) ON DELETE SET NULL
                );
                """
            )
            play_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plays)")
            }
            if "duration_ms" not in play_columns:
                connection.execute("ALTER TABLE plays ADD COLUMN duration_ms INTEGER")
            if "artist_ids_json" not in play_columns:
                connection.execute(
                    "ALTER TABLE plays ADD COLUMN artist_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            for key, value in DEFAULT_SETTINGS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
            for key, value in DEFAULT_STATS_SETTINGS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO listening_preferences(key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )

    def upsert_plays(self, tracks: list[dict[str, Any]]) -> int:
        inserted = 0
        with _lock, self.session() as connection:
            inserted = self._insert_plays(connection, tracks)
        return inserted

    def sync_plays_with_location(
        self,
        tracks: list[dict[str, Any]],
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        with _lock, self.session() as connection:
            inserted = self._insert_plays(connection, tracks)
            place_id = self._match_place_id(connection, latitude, longitude)
            place = None
            tagged = 0
            if place_id is not None:
                place_row = connection.execute(
                    "SELECT id, name FROM places WHERE id = ?", (place_id,)
                ).fetchone()
                place = dict(place_row) if place_row else None
                for track in tracks:
                    cursor = connection.execute(
                        """
                        UPDATE plays SET place_id = ?
                        WHERE track_id = ? AND played_at = ? AND place_id IS NULL
                        """,
                        (place_id, track["id"], track["played_at"]),
                    )
                    tagged += cursor.rowcount
        return {"inserted": inserted, "tagged": tagged, "place": place}

    @staticmethod
    def _insert_plays(
        connection: sqlite3.Connection,
        tracks: list[dict[str, Any]],
    ) -> int:
        inserted = 0
        for track in tracks:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO plays (
                    track_id, name, artists_json, album, image, url, played_at,
                    duration_ms, artist_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track["id"],
                    track["name"],
                    json.dumps(track.get("artists") or []),
                    track.get("album") or "",
                    track.get("image"),
                    track.get("url"),
                    track["played_at"],
                    track.get("duration_ms"),
                    json.dumps(track.get("artist_ids") or []),
                ),
            )
            if cursor.rowcount:
                inserted += cursor.rowcount
            else:
                connection.execute(
                    """
                    UPDATE plays
                    SET duration_ms = COALESCE(duration_ms, ?),
                        artist_ids_json = CASE
                            WHEN artist_ids_json = '[]' THEN ? ELSE artist_ids_json
                        END
                    WHERE track_id = ? AND played_at = ?
                    """,
                    (
                        track.get("duration_ms"),
                        json.dumps(track.get("artist_ids") or []),
                        track["id"],
                        track["played_at"],
                    ),
                )
        return inserted

    def list_plays(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT plays.*, places.name AS place_name
                FROM plays
                LEFT JOIN places ON places.id = plays.place_id
                ORDER BY plays.played_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._play_from_row(row) for row in rows]

    def most_listened_since(
        self,
        since: datetime,
        limit: int = 50,
    ) -> dict[str, Any]:
        cutoff = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.session() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM plays WHERE played_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT
                    track_id AS id,
                    name,
                    artists_json,
                    album,
                    image,
                    url,
                    COUNT(*) AS play_count,
                    MAX(played_at) AS played_at
                FROM plays
                WHERE played_at >= ?
                GROUP BY track_id
                ORDER BY play_count DESC, played_at DESC, name COLLATE NOCASE
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return {
            "total_plays": total,
            "tracks": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "artists": json.loads(row["artists_json"]),
                    "album": row["album"],
                    "image": row["image"],
                    "url": row["url"],
                    "play_count": row["play_count"],
                    "played_at": row["played_at"],
                }
                for row in rows
            ],
        }

    def history_stats(self) -> dict[str, Any]:
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS play_count, MAX(played_at) AS latest_play FROM plays"
            ).fetchone()
            sync_row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'last_history_sync'"
            ).fetchone()
        return {
            "play_count": row["play_count"],
            "latest_play": row["latest_play"],
            "last_sync_at": sync_row["value"] if sync_row else None,
        }

    def listening_stats(
        self,
        time_zone: ZoneInfo,
        context_key: str = "all",
        genres_by_artist_id: dict[str, list[str]] | None = None,
        *,
        days: int = 0,
        work_start_hour: int = 8,
        work_end_hour: int = 16,
        work_weekdays: list[int] | None = None,
    ) -> dict[str, Any]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT plays.*, places.name AS place_name
                FROM plays
                LEFT JOIN places ON places.id = plays.place_id
                ORDER BY plays.played_at
                """
            ).fetchall()

        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        context_counts: dict[str, int] = {}
        hour_counts = [0] * 24
        weekday_counts = [0] * 7
        artist_counts: dict[str, int] = {}
        artist_details: dict[str, dict[str, Any]] = {}
        track_counts: dict[str, dict[str, Any]] = {}
        genre_counts: dict[str, int] = {}
        genres_by_artist_id = genres_by_artist_id or {}
        estimated_duration_ms = 0
        selected_play_count = 0
        tagged_play_count = 0
        untagged_play_count = 0
        duration_metadata_play_count = 0
        artists_without_genres: set[str] = set()
        first_selected_play: str | None = None
        last_selected_play: str | None = None
        cutoff = utc_now() - timedelta(days=days) if days else None
        work_days = set(work_weekdays if work_weekdays is not None else DEFAULT_STATS_SETTINGS["work_weekdays"])
        selected_label = {
            "all": "All listening",
            "work-hours": "Work hours",
            "outside-work-hours": "Outside work hours",
        }.get(context_key, "")
        for row in rows:
            played_at = parse_played_at(row["played_at"])
            if cutoff is not None and played_at < cutoff:
                continue
            local_time = played_at.astimezone(time_zone)

            place_name = row["place_name"]
            if place_name:
                context = place_name
            elif (
                local_time.weekday() in work_days
                and work_start_hour <= local_time.hour < work_end_hour
            ):
                context = "Work hours"
            else:
                context = "Outside work hours"
            if row["place_id"] is not None:
                tagged_play_count += 1
            else:
                untagged_play_count += 1
            if context_key.startswith("place:"):
                if row["place_id"] is None or str(row["place_id"]) != context_key[6:]:
                    continue
                selected_label = place_name or "Saved place"
            elif context_key == "work-hours" and context != "Work hours":
                continue
            elif context_key == "outside-work-hours" and context != "Outside work hours":
                continue
            elif context_key not in {"all", "work-hours", "outside-work-hours"}:
                raise ValueError("Unknown listening context.")
            context_counts[context] = context_counts.get(context, 0) + 1

            weekday_counts[local_time.weekday()] += 1
            hour_counts[local_time.hour] += 1
            selected_play_count += 1
            if first_selected_play is None:
                first_selected_play = row["played_at"]
            last_selected_play = row["played_at"]

            artists = json.loads(row["artists_json"])
            artist_ids = json.loads(row["artist_ids_json"])
            for artist in artists:
                artist_counts[artist] = artist_counts.get(artist, 0) + 1
            for artist_index, artist_id in enumerate(artist_ids):
                if artist_index >= len(artists):
                    continue
                artist = artist_details.setdefault(
                    artist_id,
                    {
                        "id": artist_id,
                        "name": artists[artist_index],
                        "play_count": 0,
                        "genres": genres_by_artist_id.get(artist_id, []),
                    },
                )
                artist["play_count"] += 1
            genres = {
                genre
                for artist_id in artist_ids
                for genre in genres_by_artist_id.get(artist_id, [])
            }
            for genre in genres:
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
            artists_without_genres.update(
                artist_id
                for artist_id in artist_ids
                if not genres_by_artist_id.get(artist_id)
            )
            estimated_duration_ms += row["duration_ms"] or 0
            duration_metadata_play_count += row["duration_ms"] is not None

            track = track_counts.setdefault(
                row["track_id"],
                {
                    "id": row["track_id"],
                    "name": row["name"],
                    "artists": artists,
                    "album": row["album"],
                    "image": row["image"],
                    "url": row["url"],
                    "duration_ms": row["duration_ms"],
                    "play_count": 0,
                },
            )
            track["play_count"] += 1

        return {
            "play_count": selected_play_count,
            "context": {"key": context_key, "name": selected_label or context_key},
            "estimated_full_track_minutes": round(estimated_duration_ms / 60000, 1),
            "duration_metadata_play_count": duration_metadata_play_count,
            "location_coverage": {
                "tagged_plays": tagged_play_count,
                "untagged_plays": untagged_play_count,
                "tagged_percent": round(
                    100 * tagged_play_count / max(tagged_play_count + untagged_play_count, 1), 1
                ),
            },
            "artists_without_genres": len(artists_without_genres),
            "days": days,
            "contexts": [
                {"name": name, "play_count": count}
                for name, count in sorted(
                    context_counts.items(), key=lambda item: (-item[1], item[0].casefold())
                )
            ],
            "hours": [{"hour": hour, "play_count": count} for hour, count in enumerate(hour_counts)],
            "weekdays": [
                {"day": day, "play_count": weekday_counts[index]}
                for index, day in enumerate(weekdays)
            ],
            "top_artists": [
                {"name": name, "play_count": count}
                for name, count in sorted(
                    artist_counts.items(), key=lambda item: (-item[1], item[0].casefold())
                )[:10]
            ],
            "artists": sorted(
                artist_details.values(),
                key=lambda artist: (-artist["play_count"], artist["name"].casefold()),
            )[:20],
            "top_genres": [
                {"name": name, "play_count": count}
                for name, count in sorted(
                    genre_counts.items(), key=lambda item: (-item[1], item[0].casefold())
                )[:15]
            ],
            "top_tracks": sorted(
                track_counts.values(),
                key=lambda track: (-track["play_count"], track["name"].casefold()),
            )[:10],
            "first_played_at": first_selected_play,
            "last_played_at": last_selected_play,
        }

    def stats_settings(self) -> dict[str, Any]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT key, value FROM listening_preferences"
            ).fetchall()
        result = dict(DEFAULT_STATS_SETTINGS)
        result.update({row["key"]: json.loads(row["value"]) for row in rows})
        return result

    def update_stats_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.stats_settings()
        for key in DEFAULT_STATS_SETTINGS:
            if key in patch:
                current[key] = patch[key]
        with _lock, self.session() as connection:
            for key, value in current.items():
                connection.execute(
                    "INSERT OR REPLACE INTO listening_preferences(key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
        return current

    def uncached_artist_ids(self) -> list[str]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT artist_id
                FROM (
                    SELECT json_each.value AS artist_id
                    FROM plays, json_each(plays.artist_ids_json)
                    WHERE json_each.value != ''
                ) AS play_artists
                WHERE artist_id NOT IN (SELECT artist_id FROM artist_genres)
                """
            ).fetchall()
        return [row["artist_id"] for row in rows]

    def save_artist_genres(self, artists: list[dict[str, Any]]) -> None:
        with _lock, self.session() as connection:
            for artist in artists:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO artist_genres(artist_id, artist_name, genres_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        artist["id"],
                        artist.get("name") or "",
                        json.dumps(artist.get("genres") or []),
                    ),
                )

    def set_artist_genres(
        self,
        artist_id: str,
        artist_name: str,
        genres: list[str],
    ) -> None:
        with _lock, self.session() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO artist_genres(artist_id, artist_name, genres_json)
                VALUES (?, ?, ?)
                """,
                (artist_id, artist_name, json.dumps(genres)),
            )

    def artist_genres(self) -> dict[str, list[str]]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT artist_id, genres_json FROM artist_genres"
            ).fetchall()
        return {row["artist_id"]: json.loads(row["genres_json"]) for row in rows}

    def mark_history_sync(self) -> dict[str, Any]:
        synced_at = utc_now().isoformat()
        with _lock, self.session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('last_history_sync', ?)",
                (synced_at,),
            )
            count = connection.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        return {"last_sync_at": synced_at, "play_count": count}

    def record_recommendations(
        self,
        session_id: str,
        context: dict[str, Any],
        recommendations: list[dict[str, Any]],
    ) -> None:
        created_at = utc_now().isoformat()
        with _lock, self.session() as connection:
            connection.execute(
                "INSERT INTO recommendation_sessions(id, created_at, context_json) VALUES (?, ?, ?)",
                (session_id, created_at, json.dumps(context)),
            )
            for position, item in enumerate(recommendations, start=1):
                connection.execute(
                    """
                    INSERT INTO recommendation_items(
                        session_id, track_id, position, track_json, features_json, score
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        item["track"]["id"],
                        position,
                        json.dumps(item["track"]),
                        json.dumps(item["parts"]),
                        item["score"],
                    ),
                )

    def record_recommendation_feedback(
        self,
        session_id: str,
        track_id: str,
        action: str,
    ) -> bool:
        with _lock, self.session() as connection:
            exists = connection.execute(
                "SELECT 1 FROM recommendation_items WHERE session_id = ? AND track_id = ?",
                (session_id, track_id),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                """
                INSERT INTO recommendation_feedback(session_id, track_id, action, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, track_id, action, utc_now().isoformat()),
            )
        return True

    def feedback_stats(self) -> dict[str, int]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT action, COUNT(*) AS total
                FROM recommendation_feedback
                GROUP BY action
                """
            ).fetchall()
            sessions = connection.execute(
                "SELECT COUNT(*) FROM recommendation_sessions"
            ).fetchone()[0]
        counts = {row["action"]: row["total"] for row in rows}
        return {
            "impression_sessions": sessions,
            "played": counts.get("played", 0),
            "saved": counts.get("saved", 0),
            "liked": counts.get("liked", 0),
            "not_for_me": counts.get("not_for_me", 0),
        }

    def record_search(
        self,
        query: str,
        local_hour: float,
        weekday: int,
        place_id: int | None,
    ) -> int:
        with _lock, self.session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO search_events(query, searched_at, local_hour, weekday, place_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (query, utc_now().isoformat(), local_hour, weekday, place_id),
            )
            return int(cursor.lastrowid)

    def select_search_result(self, search_id: int, track_id: str) -> bool:
        with _lock, self.session() as connection:
            cursor = connection.execute(
                "UPDATE search_events SET selected_track_id = ? WHERE id = ?",
                (track_id, search_id),
            )
        return cursor.rowcount > 0

    def delete_search_events(self) -> None:
        with _lock, self.session() as connection:
            connection.execute("DELETE FROM search_events")

    def list_search_events(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT query, searched_at, local_hour, weekday, place_id, selected_track_id
                FROM search_events
                ORDER BY searched_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def training_examples(self) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS session_id, s.created_at, s.context_json,
                       i.track_id, i.position, i.score,
                       i.features_json, f.action, f.created_at AS feedback_at
                FROM recommendation_feedback AS f
                JOIN recommendation_sessions AS s ON s.id = f.session_id
                JOIN recommendation_items AS i
                    ON i.session_id = f.session_id AND i.track_id = f.track_id
                WHERE f.id = (
                    SELECT MAX(f2.id)
                    FROM recommendation_feedback AS f2
                    WHERE f2.session_id = f.session_id AND f2.track_id = f.track_id
                )
                ORDER BY s.created_at, f.id
                """
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "feedback_at": row["feedback_at"],
                "context": json.loads(row["context_json"]),
                "track_id": row["track_id"],
                "position": row["position"],
                "score": row["score"],
                "features": json.loads(row["features_json"]),
                "action": row["action"],
            }
            for row in rows
        ]

    def delete_recommendation_data(self) -> None:
        with _lock, self.session() as connection:
            connection.execute("DELETE FROM recommendation_feedback")
            connection.execute("DELETE FROM recommendation_sessions")

    def unique_tracks(self) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT track_id, name, artists_json, album, image, url,
                       MAX(played_at) AS last_played
                FROM plays
                GROUP BY track_id
                ORDER BY last_played DESC
                """
            ).fetchall()
        tracks = []
        for row in rows:
            tracks.append(
                {
                    "id": row["track_id"],
                    "name": row["name"],
                    "artists": json.loads(row["artists_json"]),
                    "album": row["album"],
                    "image": row["image"],
                    "url": row["url"],
                    "last_played": row["last_played"],
                }
            )
        return tracks

    def list_places(self) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT * FROM places ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_place(
        self,
        name: str,
        latitude: float,
        longitude: float,
        radius_km: float = 0.4,
    ) -> dict[str, Any]:
        with _lock, self.session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO places(name, latitude, longitude, radius_km)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), latitude, longitude, radius_km),
            )
            place_id = cursor.lastrowid
            row = connection.execute(
                "SELECT * FROM places WHERE id = ?", (place_id,)
            ).fetchone()
        return dict(row)

    def delete_place(self, place_id: int) -> None:
        with _lock, self.session() as connection:
            connection.execute("DELETE FROM places WHERE id = ?", (place_id,))

    def assign_place(
        self,
        play_id: int,
        place_id: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any] | None:
        with _lock, self.session() as connection:
            play = connection.execute(
                "SELECT * FROM plays WHERE id = ?", (play_id,)
            ).fetchone()
            if play is None:
                return None
            resolved_place_id = place_id
            if resolved_place_id is None and latitude is not None and longitude is not None:
                resolved_place_id = self._match_place_id(connection, latitude, longitude)
            connection.execute(
                """
                UPDATE plays SET place_id = ? WHERE id = ?
                """,
                (resolved_place_id, play_id),
            )
            row = connection.execute(
                """
                SELECT plays.*, places.name AS place_name
                FROM plays
                LEFT JOIN places ON places.id = plays.place_id
                WHERE plays.id = ?
                """,
                (play_id,),
            ).fetchone()
        return self._play_from_row(row) if row else None

    def tag_recent_plays(
        self,
        latitude: float,
        longitude: float,
        within_hours: float = 2.0,
    ) -> int:
        cutoff = utc_now().timestamp() - within_hours * 3600
        updated = 0
        with _lock, self.session() as connection:
            place_id = self._match_place_id(connection, latitude, longitude)
            if place_id is None:
                return 0
            rows = connection.execute("SELECT id, played_at FROM plays").fetchall()
            for row in rows:
                played = parse_played_at(row["played_at"]).timestamp()
                if played >= cutoff:
                    connection.execute(
                        """
                        UPDATE plays SET place_id = COALESCE(place_id, ?) WHERE id = ?
                        """,
                        (place_id, row["id"]),
                    )
                    updated += 1
        return updated

    def settings(self) -> dict[str, float | int | bool]:
        with self.session() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        values: dict[str, float | int | bool] = dict(DEFAULT_SETTINGS)
        for row in rows:
            values[row["key"]] = json.loads(row["value"])
        return values

    def update_settings(self, patch: dict[str, Any]) -> dict[str, float | int | bool]:
        current = self.settings()
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS:
                continue
            current[key] = value
        with _lock, self.session() as connection:
            for key, value in current.items():
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
        return current

    def _match_place_id(
        self,
        connection: sqlite3.Connection,
        latitude: float,
        longitude: float,
    ) -> int | None:
        from app.recommender import haversine_km

        places = connection.execute("SELECT * FROM places").fetchall()
        nearest: tuple[float, int] | None = None
        for place in places:
            distance = haversine_km(
                latitude, longitude, place["latitude"], place["longitude"]
            )
            if distance <= place["radius_km"] and (nearest is None or distance < nearest[0]):
                nearest = (distance, place["id"])
        return nearest[1] if nearest else None

    def _play_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return         {
            "session_id": row["id"],
            "id": row["id"],
            "track_id": row["track_id"],
            "name": row["name"],
            "artists": json.loads(row["artists_json"]),
            "album": row["album"],
            "image": row["image"],
            "url": row["url"],
            "played_at": row["played_at"],
            "duration_ms": row["duration_ms"] if "duration_ms" in row.keys() else None,
            "artist_ids": json.loads(row["artist_ids_json"]) if "artist_ids_json" in row.keys() else [],
            "place_id": row["place_id"],
            "place_name": row["place_name"] if "place_name" in row.keys() else None,
            "latitude": row["latitude"],
            "longitude": row["longitude"],
        }
