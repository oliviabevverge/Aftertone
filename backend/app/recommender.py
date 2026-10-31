from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.store import parse_played_at, utc_now

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def circular_hour_kernel(hour_a: float, hour_b: float, bandwidth: float) -> float:
    delta = abs(hour_a - hour_b) % 24
    distance = min(delta, 24 - delta)
    return math.exp(-(distance**2) / (2 * bandwidth**2))


def event_recency(played_at: datetime, now: datetime) -> float:
    age_days = max((now - played_at).total_seconds() / 86400, 0.0)
    return math.exp(-age_days / 30.0)


def min_max(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def rerank_with_listening_profile(
    recommendations: list[dict[str, Any]],
    preferred_tracks: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not recommendations or not preferred_tracks:
        return recommendations

    def normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    preferred_artists = {
        normalize(track["artist"])
        for track in preferred_tracks
        if track["artist"].strip()
    }
    preferred_songs = {
        (normalize(track["name"]), normalize(track["artist"]))
        for track in preferred_tracks
        if track["name"].strip() and track["artist"].strip()
    }
    scores = [item["score"] for item in recommendations]
    low, high = min(scores), max(scores)

    for item in recommendations:
        track = item["track"]
        artists = {normalize(artist) for artist in track.get("artists", [])}
        artist_match = len(artists & preferred_artists) / len(preferred_artists)
        exact_match = any(
            (normalize(track["name"]), artist) in preferred_songs
            for artist in artists
        )
        baseline = (item["score"] - low) / (high - low) if high > low else 0.5
        profile_match = min(1.0, 0.6 * artist_match + 0.4 * exact_match)
        item["parts"]["profile_artist"] = round(profile_match, 4)
        item["score"] = round(0.45 * baseline + 0.55 * profile_match, 4)

    recommendations.sort(key=lambda item: item["score"], reverse=True)
    return recommendations


def sessions_from_plays(
    plays_oldest_first: list[dict[str, Any]],
    gap_minutes: float,
) -> list[list[dict[str, Any]]]:
    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    gap = timedelta(minutes=gap_minutes)
    for play in plays_oldest_first:
        played_at = parse_played_at(play["played_at"])
        if previous_time is not None and played_at - previous_time > gap:
            if current:
                sessions.append(current)
            current = []
        current.append(play)
        previous_time = played_at
    if current:
        sessions.append(current)
    return sessions


def recommend(
    plays: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    hour: float,
    weekday: int,
    place_id: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone_offset_minutes: int = 0,
    seed_track_ids: list[str] | None = None,
    limit: int = 8,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not plays:
        return []

    now = now or utc_now()
    catalog: dict[str, dict[str, Any]] = {}
    for play in plays:
        catalog[play["track_id"]] = {
            "id": play["track_id"],
            "name": play["name"],
            "artists": play["artists"],
            "album": play["album"],
            "image": play["image"],
            "url": play["url"],
        }

    chronological = sorted(plays, key=lambda item: item["played_at"])
    recent_ids = [play["track_id"] for play in reversed(chronological)]
    seeds = [track_id for track_id in (seed_track_ids or []) if track_id]
    if not seeds:
        seeds = recent_ids[:2][::-1]

    exclude_last_n = int(settings.get("exclude_last_n", 3))
    blocked = set(recent_ids[:exclude_last_n]) | set(seeds)
    session_gap = float(settings.get("session_gap_minutes", 30))
    bandwidth = max(float(settings.get("hour_bandwidth", 2.5)), 0.25)
    sessions = sessions_from_plays(chronological, session_gap)

    bigram: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    trigram: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    skip: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    context: dict[str, list[tuple[float, float, float, int | None, float | None, float | None, int]]] = defaultdict(list)

    for play in chronological:
        played_at = parse_played_at(play["played_at"])
        recency = event_recency(played_at, now)
        local_played_at = played_at + timedelta(minutes=-timezone_offset_minutes)
        local_hour = local_played_at.hour + local_played_at.minute / 60
        context[play["track_id"]].append(
            (
                recency,
                local_hour,
                local_played_at.weekday(),
                play.get("place_id"),
                play.get("latitude"),
                play.get("longitude"),
                1 if local_played_at.weekday() == weekday else 0,
            )
        )

    for session in sessions:
        ids = [play["track_id"] for play in session]
        times = [parse_played_at(play["played_at"]) for play in session]
        for index, track_id in enumerate(ids[:-1]):
            weight = event_recency(times[index + 1], now)
            nxt = ids[index + 1]
            bigram[track_id][nxt] += weight
            if index + 2 < len(ids):
                skip[track_id][ids[index + 2]] += weight * 0.5
            if index > 0:
                trigram[(ids[index - 1], track_id)][nxt] += weight

    last_seed = seeds[-1] if seeds else None
    prior_seed = seeds[-2] if len(seeds) > 1 else None
    candidates = [track_id for track_id in catalog if track_id not in blocked]
    if not candidates:
        candidates = [track_id for track_id in catalog if track_id != last_seed]
    if not candidates:
        return []

    sequence_raw: list[float] = []
    artist_raw: list[float] = []
    time_raw: list[float] = []
    location_raw: list[float] = []
    recency_raw: list[float] = []
    novelty_raw: list[float] = []
    explanations: list[dict[str, Any]] = []
    artist_frequency: dict[str, float] = defaultdict(float)
    for play in chronological:
        weight = event_recency(parse_played_at(play["played_at"]), now)
        for artist in play.get("artists", []):
            artist_frequency[artist] += weight

    for track_id in candidates:
        sequence = 0.0
        sequence_notes: list[str] = []
        if last_seed:
            follow = bigram[last_seed].get(track_id, 0.0)
            denom = sum(bigram[last_seed].values()) or 1.0
            sequence += follow / denom
            if follow:
                sequence_notes.append(f"often follows {catalog[last_seed]['name']}")
            if prior_seed:
                tri = trigram[(prior_seed, last_seed)].get(track_id, 0.0)
                tri_denom = sum(trigram[(prior_seed, last_seed)].values()) or 1.0
                sequence += 0.7 * (tri / tri_denom)
                if tri:
                    sequence_notes.append("matches this exact two-song run")
            skip_follow = skip[last_seed].get(track_id, 0.0)
            skip_denom = sum(skip[last_seed].values()) or 1.0
            sequence += 0.25 * (skip_follow / skip_denom)

        time_score = 0.0
        artist_score = sum(
            artist_frequency.get(artist, 0.0)
            for artist in catalog[track_id].get("artists", [])
        )
        weekday_hits = 0.0
        recency_score = 0.0
        location_score = 0.0
        observations = context[track_id]
        recency_total = sum(item[0] for item in observations) or 1.0
        for recency, play_hour, play_weekday, play_place, play_lat, play_lng, weekday_match in observations:
            time_score += recency * circular_hour_kernel(play_hour, hour, bandwidth)
            weekday_hits += recency * weekday_match
            recency_score = max(recency_score, recency)
            location_score = max(
                location_score,
                _location_affinity(place_id, latitude, longitude, play_place, play_lat, play_lng),
            )
        time_score = (time_score / recency_total) + 0.2 * (weekday_hits / recency_total)

        repeats = recent_ids[:12].count(track_id)
        novelty_penalty = min(repeats / 4.0, 1.0)

        sequence_raw.append(sequence)
        artist_raw.append(artist_score)
        time_raw.append(time_score)
        location_raw.append(location_score)
        recency_raw.append(recency_score)
        novelty_raw.append(novelty_penalty)
        explanations.append(
            {
                "sequence_notes": sequence_notes,
                "has_time_match": time_score > 0.35,
                "has_location_match": location_score > 0.5,
                "weekday_match": weekday_hits > 0,
            }
        )

    sequence_n = min_max(sequence_raw)
    artist_n = min_max(artist_raw)
    time_n = min_max(time_raw)
    location_n = min_max(location_raw)
    recency_n = min_max(recency_raw)
    novelty_n = min_max(novelty_raw)

    w_seq = float(settings.get("sequence_weight", 1.0))
    w_time = float(settings.get("time_weight", 0.85))
    w_loc = float(settings.get("location_weight", 0.8))
    w_rec = float(settings.get("recency_weight", 0.45))
    w_nov = float(settings.get("novelty_weight", 0.35))
    query_has_location = place_id is not None or (latitude is not None and longitude is not None)

    ranked: list[dict[str, Any]] = []
    for index, track_id in enumerate(candidates):
        parts = {
            "sequence": round(sequence_n[index], 4),
            "artist": round(artist_n[index], 4),
            "time": round(time_n[index], 4),
            "location": round(location_n[index], 4) if query_has_location else 0.0,
            "recency": round(recency_n[index], 4),
            "novelty_penalty": round(novelty_n[index], 4),
        }
        score = (
            w_seq * parts["sequence"]
            + 0.35 * parts["artist"]
            + w_time * parts["time"]
            + (w_loc * parts["location"] if query_has_location else 0.0)
            + w_rec * parts["recency"]
            - w_nov * parts["novelty_penalty"]
        )
        why = _why(catalog, last_seed, explanations[index], parts, query_has_location)
        ranked.append(
            {
                "track": catalog[track_id],
                "score": round(score, 4),
                "parts": parts,
                "why": why,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[: max(1, min(limit, 25))]


def _location_affinity(
    query_place: int | None,
    query_lat: float | None,
    query_lng: float | None,
    play_place: int | None,
    play_lat: float | None,
    play_lng: float | None,
) -> float:
    if query_place is not None and play_place is not None and query_place == play_place:
        return 1.0
    if (
        query_lat is not None
        and query_lng is not None
        and play_lat is not None
        and play_lng is not None
    ):
        distance = haversine_km(query_lat, query_lng, play_lat, play_lng)
        return math.exp(-distance / 1.5)
    return 0.0


def _why(
    catalog: dict[str, dict[str, Any]],
    last_seed: str | None,
    explanation: dict[str, Any],
    parts: dict[str, float],
    query_has_location: bool,
) -> str:
    notes = list(explanation["sequence_notes"])
    if explanation["has_time_match"]:
        notes.append("fits this time of day")
    if explanation["weekday_match"]:
        notes.append("shows up on this weekday")
    if parts["artist"] > 0.5:
        notes.append("fits artists you listen to often")
    if query_has_location and explanation["has_location_match"]:
        notes.append("matches this place")
    if not notes:
        strongest = max(
            ("sequence", parts["sequence"]),
            ("time", parts["time"]),
            ("location", parts["location"]),
            key=lambda item: item[1],
        )[0]
        notes.append(f"ranked by {strongest}")
    if last_seed and last_seed in catalog and parts["sequence"] >= 0.6:
        return "; ".join(notes)
    return "; ".join(notes)
