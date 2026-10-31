import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.main import SESSION_COOKIE, _account_stores, app, sessions
from app.store import ListeningStore


class SpotifyOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = ListeningStore(Path(self.tmpdir.name) / "aftertone.db")
        self.store_patch = patch("app.main.store", self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        _account_stores.clear()
        self.addCleanup(_account_stores.clear)
        self.account_db_dir = Path(self.tmpdir.name) / "accounts"
        self.account_db_env = patch.dict(
            "os.environ",
            {"AFTERTONE_ACCOUNT_DB_DIR": str(self.account_db_dir)},
        )
        self.account_db_env.start()
        self.addCleanup(self.account_db_env.stop)
        self.client = TestClient(app, follow_redirects=False)
        self.config = patch(
            "app.main.spotify_config",
            return_value=("client-id", "client-secret", "http://127.0.0.1:8888/callback"),
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.client.close)

    def test_callback_exchanges_code_and_recently_played_returns_tracks(self) -> None:
        login = self.client.get("/login")
        self.assertEqual(login.status_code, 307)
        authorize_params = parse_qs(urlparse(login.headers["location"]).query)
        state = authorize_params["state"][0]
        self.assertEqual(
            set(authorize_params["scope"][0].split()),
            {"user-read-private", "user-read-recently-played"},
        )

        token_response = Mock()
        token_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        token_response.raise_for_status.return_value = None
        account_response = Mock()
        account_response.json.return_value = {"id": "test-spotify-account"}
        account_response.status_code = 200
        account_response.raise_for_status.return_value = None
        with patch("app.main.httpx.post", return_value=token_response) as post:
            with patch("app.main.httpx.get", return_value=account_response):
                callback = self.client.get(
                    "/callback",
                    params={"code": "one-time-code", "state": state},
                )

        self.assertEqual(callback.status_code, 307)
        self.assertIn("spotify_connected=1", callback.headers["location"])
        self.assertEqual(post.call_args.kwargs["data"]["code"], "one-time-code")
        self.assertTrue(callback.cookies.get("aftertone_session"))

        spotify_response = Mock()
        spotify_response.json.return_value = {
            "items": [
                {
                    "played_at": "2026-10-15T20:00:00Z",
                    "track": {
                        "id": "track-id",
                        "name": "Track title",
                        "artists": [{"name": "Artist"}],
                        "album": {
                            "name": "Album",
                            "images": [{"url": "https://example.test/cover.jpg"}],
                        },
                        "external_urls": {"spotify": "https://open.spotify.com/track/track-id"},
                    },
                }
            ]
        }
        spotify_response.status_code = 200
        spotify_response.raise_for_status.return_value = None
        with patch("app.main.httpx.get", return_value=spotify_response):
            recent = self.client.get("/api/recently-played")

        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.json()["tracks"][0]["name"], "Track title")
        self.assertEqual(recent.json()["tracks"][0]["artists"], ["Artist"])
        from app.main import account_store

        self.assertEqual(account_store("test-spotify-account").list_plays()[0]["name"], "Track title")

        recs = self.client.post(
            "/api/recommendations",
            json={"hour": 16, "seed_track_ids": ["track-id"], "limit": 5},
        )
        self.assertEqual(recs.status_code, 200)
        self.assertIn("recommendations", recs.json())
        self.assertEqual(recs.json()["ranking_model"], "rule-based baseline")
        if recs.json()["recommendations"]:
            feedback = self.client.post(
                "/api/feedback",
                json={
                    "session_id": recs.json()["session_id"],
                    "track_id": recs.json()["recommendations"][0]["track"]["id"],
                    "action": "liked",
                },
            )
            self.assertEqual(feedback.status_code, 200)
            learning_status = self.client.get("/api/learning-status").json()
            self.assertEqual(learning_status["liked"], 1)
            self.assertEqual(learning_status["training_examples"], 1)
            self.assertEqual(learning_status["training_sessions"], 1)

        settings = self.client.put(
            "/api/recommender/settings",
            json={"sequence_weight": 1.5, "location_weight": 0.2},
        )
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["settings"]["sequence_weight"], 1.5)

    def test_listening_profiles_are_scoped_to_spotify_account(self) -> None:
        from app.main import _account_stores, account_store

        self.addCleanup(lambda: _account_stores.clear())
        self.client.cookies.set(SESSION_COOKIE, "profile-account-one")
        sessions["profile-account-one"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
            "spotify_user_id": "spotify-account-one",
        }
        sessions["profile-account-two"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
            "spotify_user_id": "spotify-account-two",
        }
        self.addCleanup(lambda: sessions.pop("profile-account-one", None))
        self.addCleanup(lambda: sessions.pop("profile-account-two", None))

        saved = self.client.put(
            "/api/listening-profiles",
            json={
                "office": "Breadcrumbs — Isabella Contadini",
                "gym": "",
                "car": "",
            },
        )
        self.assertEqual(saved.status_code, 200)

        self.client.cookies.set(SESSION_COOKIE, "profile-account-two")
        other_account = self.client.get("/api/listening-profiles")
        self.assertEqual(other_account.status_code, 200)
        self.assertEqual(other_account.json()["profiles"]["office"], "")

        self.client.cookies.set(SESSION_COOKIE, "profile-account-one")
        same_account = self.client.get("/api/listening-profiles")
        self.assertEqual(
            same_account.json()["profiles"]["office"],
            "Breadcrumbs — Isabella Contadini",
        )

    def test_invite_allowlist_rejects_uninvited_spotify_accounts(self) -> None:
        from app.main import authorize_spotify_user

        with patch.dict(
            "os.environ",
            {
                "AFTERTONE_ALLOWED_SPOTIFY_USERS": "invited-account",
                "AFTERTONE_REQUIRE_ALLOWLIST": "true",
            },
        ):
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as raised:
                authorize_spotify_user("uninvited-account")
        self.assertEqual(raised.exception.status_code, 403)

    def test_callback_rejects_mismatched_state(self) -> None:
        self.client.get("/login")
        callback = self.client.get(
            "/callback",
            params={"code": "one-time-code", "state": "incorrect-state"},
        )
        self.assertEqual(callback.status_code, 400)

    def test_recently_played_requires_a_session(self) -> None:
        response = self.client.get("/api/recently-played")
        self.assertEqual(response.status_code, 401)

    def test_recommendations_use_local_history_and_programmable_weights(self) -> None:
        sessions["test-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.client.cookies.set(SESSION_COOKIE, "test-session")
        self.store.upsert_plays(
            [
                {
                    "id": "opener",
                    "name": "Opener",
                    "artists": ["A"],
                    "album": "LP",
                    "image": None,
                    "url": None,
                    "played_at": "2026-10-08T08:00:00Z",
                },
                {
                    "id": "morning-hit",
                    "name": "Morning Hit",
                    "artists": ["A"],
                    "album": "LP",
                    "image": None,
                    "url": None,
                    "played_at": "2026-10-08T08:03:00Z",
                },
            ]
        )
        response = self.client.post(
            "/api/recommendations",
            json={
                "hour": 8,
                "weekday": 3,
                "listening_mode": "gym",
                "preferred_tracks": [{"name": "Morning Hit", "artist": "A"}],
                "seed_track_ids": ["opener"],
                "limit": 5,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendations"][0]["track"]["id"], "morning-hit")
        self.assertEqual(response.json()["context"]["listening_mode"], "gym")
        self.assertEqual(response.json()["recommendations"][0]["parts"]["profile_artist"], 1.0)
        self.assertIn("listening profile", response.json()["ranking_model"])
        self.addCleanup(lambda: sessions.pop("test-session", None))

    def test_live_location_sync_tags_new_plays_at_saved_place(self) -> None:
        sessions["test-location-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-location-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-location-session")
        place = self.store.create_place("Home", 40.7, -74.0, 0.5)
        spotify_response = Mock()
        spotify_response.json.return_value = {
            "items": [
                {
                    "played_at": "2026-10-15T20:00:00Z",
                    "track": {
                        "id": "live-track",
                        "name": "Live Track",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Album", "images": []},
                        "external_urls": {},
                    },
                }
            ]
        }
        spotify_response.status_code = 200
        spotify_response.raise_for_status.return_value = None
        with patch("app.main.httpx.get", return_value=spotify_response) as spotify_get:
            response = self.client.post(
                "/api/location/sync",
                json={
                    "after_ms": 1_792_000_000_000,
                    "latitude": 40.701,
                    "longitude": -74.0,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["captured"], 1)
        self.assertEqual(response.json()["tagged"], 1)
        self.assertEqual(response.json()["place"]["id"], place["id"])
        self.assertEqual(spotify_get.call_args.kwargs["params"]["after"], 1_792_000_000_000)
        saved_play = self.store.list_plays()[0]
        self.assertEqual(saved_play["place_id"], place["id"])
        self.assertIsNone(saved_play["latitude"])
        self.assertIsNone(saved_play["longitude"])

    def test_spotify_search_does_not_log_until_opted_in(self) -> None:
        sessions["test-search-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-search-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-search-session")
        spotify_result = {
            "tracks": {
                "items": [
                    {
                        "id": "search-track",
                        "name": "Search Track",
                        "artists": [{"name": "Artist"}],
                        "album": {"name": "Album", "images": []},
                        "external_urls": {},
                    }
                ]
            }
        }
        with patch("app.main.spotify_get", return_value=spotify_result):
            private_search = self.client.post(
                "/api/spotify-search",
                json={"query": "private query"},
            )
            self.assertEqual(private_search.status_code, 200)
            self.assertIsNone(private_search.json()["search_id"])

            self.client.put(
                "/api/recommender/settings",
                json={"search_logging_enabled": True},
            )
            logged_search = self.client.post(
                "/api/spotify-search",
                json={"query": "work playlist"},
            )

        self.assertEqual(logged_search.status_code, 200)
        self.assertIsInstance(logged_search.json()["search_id"], int)
        search_id = logged_search.json()["search_id"]
        selected = self.client.post(
            "/api/search-selection",
            json={"search_id": search_id, "track_id": "search-track"},
        )
        self.assertEqual(selected.status_code, 200)
        with self.store.session() as connection:
            row = connection.execute(
                "SELECT query, selected_track_id FROM search_events WHERE id = ?",
                (search_id,),
            ).fetchone()
        self.assertEqual(row["query"], "work playlist")
        self.assertEqual(row["selected_track_id"], "search-track")
        deleted = self.client.delete("/api/search-history")
        self.assertEqual(deleted.status_code, 200)
        with self.store.session() as connection:
            count = connection.execute("SELECT COUNT(*) FROM search_events").fetchone()[0]
        self.assertEqual(count, 0)

    def test_favorites_rank_tracks_by_plays_in_the_last_week(self) -> None:
        sessions["test-favorites-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-favorites-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-favorites-session")
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=2)
        old = now - timedelta(days=9)
        self.store.upsert_plays(
            [
                {
                    "id": track_id,
                    "name": track_name,
                    "artists": ["Artist"],
                    "artist_ids": ["artist-id"],
                    "duration_ms": 180000,
                    "album": "Album",
                    "image": None,
                    "url": None,
                    "played_at": played_at.isoformat().replace("+00:00", "Z"),
                }
                for track_id, track_name, played_at in [
                    ("frequent", "Frequent Track", recent),
                    ("frequent", "Frequent Track", recent + timedelta(hours=1)),
                    ("frequent", "Frequent Track", recent + timedelta(hours=2)),
                    ("once", "Once Track", recent + timedelta(hours=3)),
                    ("old", "Old Track", old),
                ]
            ]
        )
        response = self.client.get("/api/favorites?limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["period_days"], 7)
        self.assertEqual(response.json()["total_plays"], 4)
        self.assertEqual(response.json()["tracks"][0]["id"], "frequent")
        self.assertEqual(response.json()["tracks"][0]["play_count"], 3)
        self.assertEqual(response.json()["tracks"][1]["id"], "once")
        self.assertEqual([track["id"] for track in response.json()["tracks"]], ["frequent", "once"])

    def test_stats_group_by_local_work_hours_and_saved_place(self) -> None:
        sessions["test-stats-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-stats-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-stats-session")
        plays = [
            ("before-work", "2026-10-16T11:59:00Z"),
            ("at-work", "2026-10-16T12:00:00Z"),
            ("after-work", "2026-10-16T20:00:00Z"),
            ("at-gym", "2026-10-17T14:00:00Z"),
        ]
        self.store.upsert_plays(
            [
                {
                    "id": track_id,
                    "name": track_id,
                    "artists": ["Artist"],
                    "artist_ids": ["artist-id"],
                    "duration_ms": 180000,
                    "album": "Album",
                    "image": None,
                    "url": None,
                    "played_at": played_at,
                }
                for track_id, played_at in plays
            ]
        )
        gym = self.store.create_place("Gym", 40.0, -74.0)
        gym_play = next(play for play in self.store.list_plays() if play["track_id"] == "at-gym")
        self.store.assign_place(gym_play["id"], gym["id"])

        artist_response = Mock()
        artist_response.json.return_value = {
            "artists": [{"id": "artist-id", "name": "Artist", "genres": ["indie rock"]}]
        }
        artist_response.status_code = 200
        artist_response.raise_for_status.return_value = None
        with patch("app.main.httpx.get", return_value=artist_response):
            response = self.client.get("/api/stats?timezone=America%2FNew_York")

        self.assertEqual(response.status_code, 200)
        stats = response.json()
        self.assertEqual(stats["play_count"], 4)
        self.assertEqual(stats["timezone"], "America/New_York")
        self.assertEqual(
            {context["name"]: context["play_count"] for context in stats["contexts"]},
            {"Outside work hours": 2, "Work hours": 1, "Gym": 1},
        )
        self.assertEqual(stats["hours"][7]["play_count"], 1)
        self.assertEqual(stats["hours"][8]["play_count"], 1)
        self.assertEqual(stats["hours"][16]["play_count"], 1)
        self.assertEqual(stats["top_artists"][0], {"name": "Artist", "play_count": 4})
        self.assertEqual(stats["top_genres"], [{"name": "indie rock", "play_count": 4}])

    def test_stats_require_authentication_and_reject_unknown_time_zone(self) -> None:
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        sessions["test-stats-session"] = {"access_token": "x", "expires_at": time.time() + 3600}
        self.addCleanup(lambda: sessions.pop("test-stats-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-stats-session")
        self.assertEqual(self.client.get("/api/stats?timezone=not-a-zone").status_code, 400)

    def test_stats_preferences_and_manual_genre_labels(self) -> None:
        sessions["test-stats-settings-session"] = {
            "access_token": "access-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-stats-settings-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-stats-settings-session")

        response = self.client.put(
            "/api/stats/settings",
            json={"work_start_hour": 9, "work_end_hour": 17, "work_weekdays": [0, 1, 2, 3, 4]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settings"]["work_start_hour"], 9)
        invalid_schedule = self.client.put(
            "/api/stats/settings",
            json={"work_start_hour": 18, "work_end_hour": 17, "work_weekdays": [0]},
        )
        self.assertEqual(invalid_schedule.status_code, 422)

        genre_response = self.client.put(
            "/api/artist-genres",
            json={
                "artist_id": "artist-1",
                "artist_name": "Artist",
                "genres": [" Indie Rock ", "dream pop", "indie rock"],
            },
        )
        self.assertEqual(genre_response.status_code, 200)
        self.assertEqual(
            self.store.artist_genres(),
            {"artist-1": ["dream pop", "indie rock"]},
        )

    def test_recently_played_import_paginates_thirty_days_and_stores_metadata(self) -> None:
        sessions["test-import-session"] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,
        }
        self.addCleanup(lambda: sessions.pop("test-import-session", None))
        self.client.cookies.set(SESSION_COOKIE, "test-import-session")
        now = datetime.now(timezone.utc)

        def response_for(items):
            response = Mock()
            response.json.return_value = {"items": items}
            response.status_code = 200
            response.raise_for_status.return_value = None
            return response

        def item(track_id: str, played_at: datetime):
            return {
                "played_at": played_at.isoformat().replace("+00:00", "Z"),
                "track": {
                    "id": track_id,
                    "name": track_id,
                    "artists": [{"id": f"artist-{track_id}", "name": "Artist"}],
                    "duration_ms": 180000,
                    "album": {"name": "Album", "images": []},
                    "external_urls": {},
                },
            }

        pages = [
            response_for([item("recent", now - timedelta(days=1))]),
            response_for([item("older", now - timedelta(days=20))]),
            response_for([]),
        ]
        with patch("app.main.httpx.get", side_effect=pages) as spotify_get:
            response = self.client.get("/api/recently-played?days=30")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["days_requested"], 30)
        self.assertEqual(response.json()["pages_fetched"], 3)
        self.assertFalse(response.json()["page_limit_reached"])
        self.assertEqual([track["id"] for track in response.json()["tracks"]], ["recent", "older"])
        self.assertEqual(spotify_get.call_count, 3)
        saved = {play["track_id"]: play for play in self.store.list_plays()}
        self.assertEqual(saved["older"]["duration_ms"], 180000)
        self.assertEqual(saved["older"]["artist_ids"], ["artist-older"])


if __name__ == "__main__":
    unittest.main()
