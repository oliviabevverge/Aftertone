import os
import secrets
import time
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.learned_ranker import add_search_affinity, load_model, rank_with_model
from app.recommender import recommend
from app.store import ListeningStore, parse_played_at

load_dotenv()

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_RECENTLY_PLAYED_URL = "https://api.spotify.com/v1/me/player/recently-played"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
SPOTIFY_ARTISTS_URL = "https://api.spotify.com/v1/artists"
STATE_COOKIE = "aftertone_oauth_state"
SESSION_COOKIE = "aftertone_session"
sessions: dict[str, dict[str, Any]] = {}
store = ListeningStore(
    os.getenv(
        "AFTERTONE_DB",
        str(Path(__file__).resolve().parent.parent / "data" / "aftertone.db"),
    )
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "FRONTEND_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def spotify_config() -> tuple[str, str, str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="Spotify OAuth is not configured. Set the Spotify client ID, "
            "client secret, and redirect URI in the backend environment.",
        )
    return client_id, client_secret, redirect_uri


def frontend_url() -> str:
    return os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")


def exchange_code(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = spotify_config()
    try:
        response = httpx.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail="Spotify rejected the authorization code. Start a fresh login "
            "and confirm the configured redirect URI matches Spotify exactly.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not reach Spotify to exchange the authorization code.",
        ) from exc
    return response.json()


def refresh_access_token(session: dict[str, Any]) -> None:
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail="Your Spotify session expired. Please connect Spotify again.",
        )
    client_id, client_secret, _ = spotify_config()
    try:
        response = httpx.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(client_id, client_secret),
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail="Spotify could not refresh your access token."
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail="Could not reach Spotify to refresh your session."
        ) from exc

    token = response.json()
    session["access_token"] = token["access_token"]
    session["expires_at"] = time.time() + token.get("expires_in", 3600)
    session["refresh_token"] = token.get("refresh_token", refresh_token)


def spotify_get(
    url: str,
    session: dict[str, Any],
    params: dict[str, Any],
    resource_name: str = "your Spotify data",
) -> Any:
    if session["expires_at"] <= time.time():
        refresh_access_token(session)
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {session['access_token']}"},
            params=params,
            timeout=15,
        )
        if response.status_code == 401:
            refresh_access_token(session)
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {session['access_token']}"},
                params=params,
                timeout=15,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Spotify denied access to {resource_name}. "
                "Disconnect and reconnect Spotify to approve the requested permissions."
                if exc.response.status_code == 403
                else f"Spotify could not return {resource_name}."
            ),
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail="Could not reach Spotify to load your music."
        ) from exc
    return response.json()


def current_session(request: Request) -> dict[str, Any]:
    session_id = request.cookies.get(SESSION_COOKIE)
    session = sessions.get(session_id or "")
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Spotify is not connected. Connect your account to continue.",
        )
    return session


class PlaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=0.4, gt=0, le=50)


class PlayLocationUpdate(BaseModel):
    place_id: int | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class ContextTag(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    within_hours: float = Field(default=2.0, gt=0, le=48)


class LiveLocationSync(BaseModel):
    after_ms: int = Field(ge=0)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RecommenderSettings(BaseModel):
    sequence_weight: float | None = Field(default=None, ge=0, le=3)
    time_weight: float | None = Field(default=None, ge=0, le=3)
    location_weight: float | None = Field(default=None, ge=0, le=3)
    recency_weight: float | None = Field(default=None, ge=0, le=3)
    novelty_weight: float | None = Field(default=None, ge=0, le=3)
    hour_bandwidth: float | None = Field(default=None, gt=0, le=12)
    session_gap_minutes: float | None = Field(default=None, gt=1, le=240)
    exclude_last_n: int | None = Field(default=None, ge=0, le=20)
    search_logging_enabled: bool | None = None


class RecommendRequest(BaseModel):
    hour: float | None = Field(default=None, ge=0, lt=24)
    weekday: int | None = Field(default=None, ge=0, le=6)
    timezone_offset_minutes: int = Field(default=0, ge=-840, le=840)
    place_id: int | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    seed_track_ids: list[str] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=8, ge=1, le=25)


class RecommendationFeedback(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    track_id: str = Field(min_length=1, max_length=200)
    action: Literal["played", "saved", "liked", "not_for_me"]


class SpotifySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    place_id: int | None = None


class SearchSelection(BaseModel):
    search_id: int = Field(gt=0)
    track_id: str = Field(min_length=1, max_length=200)


class ListeningStatsSettings(BaseModel):
    work_start_hour: int = Field(ge=0, le=23)
    work_end_hour: int = Field(ge=1, le=24)
    work_weekdays: list[int] = Field(min_length=1, max_length=7)


class ArtistGenresUpdate(BaseModel):
    artist_id: str = Field(min_length=1, max_length=100)
    artist_name: str = Field(min_length=1, max_length=200)
    genres: list[str] = Field(max_length=20)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/env-check")
def env_check() -> dict[str, bool]:
    return {
        "client_id_loaded": bool(os.getenv("SPOTIFY_CLIENT_ID")),
        "client_secret_loaded": bool(os.getenv("SPOTIFY_CLIENT_SECRET")),
        "redirect_uri_loaded": bool(os.getenv("SPOTIFY_REDIRECT_URI")),
    }


@app.get("/login")
def login() -> RedirectResponse:
    client_id, _, redirect_uri = spotify_config()
    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "user-read-recently-played",
        "state": state,
    }
    response = RedirectResponse(f"{SPOTIFY_AUTHORIZE_URL}?{urlencode(params)}")
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=redirect_uri.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    response = RedirectResponse(frontend_url())
    response.delete_cookie(STATE_COOKIE)

    if error:
        response.headers["Location"] = f"{frontend_url()}/?spotify_error=authorization_denied"
        return response
    expected_state = request.cookies.get(STATE_COOKIE)
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Spotify authorization state did not match.")
    if not code:
        raise HTTPException(status_code=400, detail="Spotify did not return an authorization code.")

    token = exchange_code(code)
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": time.time() + token.get("expires_in", 3600),
    }
    response = RedirectResponse(f"{frontend_url()}/?spotify_connected=1")
    response.delete_cookie(STATE_COOKIE)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=os.getenv("SPOTIFY_REDIRECT_URI", "").startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/api/recently-played")
def recently_played(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    days: int = Query(default=0, ge=0, le=30),
) -> dict[str, Any]:
    session = current_session(request)
    tracks: list[dict[str, Any]] = []
    pages = 0
    has_more = False
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        before_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 1
        while pages < 200:
            result = spotify_get(
                SPOTIFY_RECENTLY_PLAYED_URL,
                session,
                {"limit": 50, "before": before_ms},
                "your recently played history",
            )
            items = result.get("items", [])
            pages += 1
            if not items:
                break
            oldest_play = min(parse_played_at(item["played_at"]) for item in items)
            for item in items:
                if parse_played_at(item["played_at"]) < cutoff:
                    continue
                track = item["track"]
                images = track.get("album", {}).get("images", [])
                artists = track.get("artists", [])
                tracks.append(
                    {
                        "id": track["id"],
                        "name": track["name"],
                        "artists": [artist["name"] for artist in artists],
                        "artist_ids": [artist["id"] for artist in artists if artist.get("id")],
                        "duration_ms": track.get("duration_ms"),
                        "album": track.get("album", {}).get("name", ""),
                        "image": images[0]["url"] if images else None,
                        "url": track.get("external_urls", {}).get("spotify"),
                        "played_at": item["played_at"],
                    }
                )
            if oldest_play <= cutoff:
                break
            next_before_ms = int(oldest_play.timestamp() * 1000)
            if next_before_ms >= before_ms:
                break
            before_ms = next_before_ms
        else:
            has_more = True
    else:
        result = spotify_get(
            SPOTIFY_RECENTLY_PLAYED_URL,
            session,
            {"limit": limit},
        )
        for item in result.get("items", []):
            track = item["track"]
            images = track.get("album", {}).get("images", [])
            artists = track.get("artists", [])
            tracks.append(
                {
                    "id": track["id"],
                    "name": track["name"],
                    "artists": [artist["name"] for artist in artists],
                    "artist_ids": [artist["id"] for artist in artists if artist.get("id")],
                    "duration_ms": track.get("duration_ms"),
                    "album": track.get("album", {}).get("name", ""),
                    "image": images[0]["url"] if images else None,
                    "url": track.get("external_urls", {}).get("spotify"),
                    "played_at": item["played_at"],
                }
            )
    newly_saved = store.upsert_plays(tracks)
    sync = store.mark_history_sync()
    return {
        "tracks": tracks,
        "newly_saved": newly_saved,
        "days_requested": days or None,
        "pages_fetched": pages or 1,
        "page_limit_reached": has_more,
        **sync,
    }


@app.get("/api/history")
def history(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    current_session(request)
    return {
        "plays": store.list_plays(limit=limit, offset=offset),
        "tracks": store.unique_tracks(),
        "stats": store.history_stats(),
    }


@app.get("/api/stats")
def listening_stats(
    request: Request,
    time_zone: str = Query(default="UTC", alias="timezone", min_length=1, max_length=100),
    context: str = Query(default="all", min_length=1, max_length=100),
    days: int = Query(default=0, ge=0, le=365),
) -> dict[str, Any]:
    session = current_session(request)
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Use a valid IANA time zone.") from exc
    if context.startswith("place:"):
        place_id = context.removeprefix("place:")
        places = {str(place["id"]): place for place in store.list_places()}
        if place_id not in places:
            raise HTTPException(status_code=400, detail="Select a saved place.")
    elif context not in {"all", "work-hours", "outside-work-hours"}:
        raise HTTPException(status_code=400, detail="Select a valid listening context.")

    genre_error = None
    missing_artist_ids = store.uncached_artist_ids()
    try:
        for offset in range(0, len(missing_artist_ids), 50):
            artist_result = spotify_get(
                SPOTIFY_ARTISTS_URL,
                session,
                {"ids": ",".join(missing_artist_ids[offset : offset + 50])},
                "artist genre data",
            )
            store.save_artist_genres(
                [artist for artist in artist_result.get("artists", []) if artist and artist.get("id")]
            )
    except HTTPException as exc:
        genre_error = exc.detail
    preferences = store.stats_settings()
    stats = store.listening_stats(
        zone,
        context,
        store.artist_genres(),
        days=days,
        work_start_hour=preferences["work_start_hour"],
        work_end_hour=preferences["work_end_hour"],
        work_weekdays=preferences["work_weekdays"],
    )
    if genre_error is None and stats["artists_without_genres"]:
        genre_error = "Some artists have no genre metadata. Add or correct genres below."
    return {
        "timezone": time_zone,
        "genres_available": stats["artists_without_genres"] == 0,
        "genres_message": genre_error,
        **stats,
    }


@app.get("/api/stats/settings")
def get_listening_stats_settings(request: Request) -> dict[str, Any]:
    current_session(request)
    return {"settings": store.stats_settings()}


@app.put("/api/stats/settings")
def put_listening_stats_settings(
    request: Request,
    body: ListeningStatsSettings,
) -> dict[str, Any]:
    current_session(request)
    if body.work_start_hour >= body.work_end_hour:
        raise HTTPException(status_code=422, detail="Work end hour must be later than start hour.")
    if len(set(body.work_weekdays)) != len(body.work_weekdays) or any(
        weekday < 0 or weekday > 6 for weekday in body.work_weekdays
    ):
        raise HTTPException(status_code=422, detail="Choose each workday at most once.")
    return {"settings": store.update_stats_settings(body.model_dump())}


@app.put("/api/artist-genres")
def update_artist_genres(
    request: Request,
    body: ArtistGenresUpdate,
) -> dict[str, str]:
    current_session(request)
    genres = sorted({genre.strip().casefold() for genre in body.genres if genre.strip()})
    if any(len(genre) > 60 for genre in genres):
        raise HTTPException(status_code=422, detail="Each genre must be 60 characters or fewer.")
    if len(genres) > 20:
        raise HTTPException(status_code=422, detail="Add no more than 20 genres per artist.")
    store.set_artist_genres(body.artist_id, body.artist_name.strip(), genres)
    return {"status": "saved"}


@app.get("/api/favorites")
def favorites(
    request: Request,
    limit: int = Query(default=50, ge=1, le=50),
) -> dict[str, Any]:
    current_session(request)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    return {"period_days": 7, **store.most_listened_since(since, limit)}


@app.get("/api/learning-status")
def learning_status(request: Request) -> dict[str, Any]:
    current_session(request)
    stats = store.feedback_stats()
    examples = store.training_examples()
    return {
        **stats,
        "training_examples": len(examples),
        "training_sessions": len({example["session_id"] for example in examples}),
        "minimum_training_examples": 20,
        "minimum_training_sessions": 5,
    }


@app.post("/api/feedback")
def recommendation_feedback(
    request: Request,
    body: RecommendationFeedback,
) -> dict[str, str]:
    current_session(request)
    if not store.record_recommendation_feedback(body.session_id, body.track_id, body.action):
        raise HTTPException(status_code=404, detail="That recommendation was not found.")
    return {"status": "recorded"}


@app.post("/api/spotify-search")
def spotify_search(request: Request, body: SpotifySearchRequest) -> dict[str, Any]:
    session = current_session(request)
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Enter a search query.")
    result = spotify_get(
        SPOTIFY_SEARCH_URL,
        session,
        {"q": query, "type": "track", "limit": 10},
    )
    tracks = []
    for item in result.get("tracks", {}).get("items", []):
        images = item.get("album", {}).get("images", [])
        tracks.append(
            {
                "id": item["id"],
                "name": item["name"],
                "artists": [artist["name"] for artist in item.get("artists", [])],
                "album": item.get("album", {}).get("name", ""),
                "image": images[0]["url"] if images else None,
                "url": item.get("external_urls", {}).get("spotify"),
            }
        )

    search_id = None
    if store.settings().get("search_logging_enabled", False):
        local_now = datetime.now().astimezone()
        try:
            search_id = store.record_search(
                query,
                local_now.hour + local_now.minute / 60,
                local_now.weekday(),
                body.place_id,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="The selected place was not found.") from exc
    return {"tracks": tracks, "search_id": search_id}


@app.post("/api/search-selection")
def search_selection(request: Request, body: SearchSelection) -> dict[str, str]:
    current_session(request)
    if not store.select_search_result(body.search_id, body.track_id):
        raise HTTPException(status_code=404, detail="That search record was not found.")
    return {"status": "recorded"}


@app.delete("/api/search-history")
def delete_search_history(request: Request) -> dict[str, str]:
    current_session(request)
    store.delete_search_events()
    return {"status": "deleted"}


@app.get("/api/places")
def places(request: Request) -> dict[str, Any]:
    current_session(request)
    return {"places": store.list_places()}


@app.post("/api/places")
def create_place(request: Request, body: PlaceCreate) -> dict[str, Any]:
    current_session(request)
    try:
        place = store.create_place(body.name, body.latitude, body.longitude, body.radius_km)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="A place with that name already exists.") from exc
    return {"place": place}


@app.delete("/api/places/{place_id}")
def remove_place(request: Request, place_id: int) -> dict[str, str]:
    current_session(request)
    store.delete_place(place_id)
    return {"status": "ok"}


@app.post("/api/plays/{play_id}/location")
def update_play_location(request: Request, play_id: int, body: PlayLocationUpdate) -> dict[str, Any]:
    current_session(request)
    play = store.assign_place(play_id, body.place_id, body.latitude, body.longitude)
    if play is None:
        raise HTTPException(status_code=404, detail="That play was not found.")
    return {"play": play}


@app.post("/api/history/tag-location")
def tag_recent_location(request: Request, body: ContextTag) -> dict[str, int]:
    current_session(request)
    updated = store.tag_recent_plays(body.latitude, body.longitude, body.within_hours)
    return {"updated": updated}


@app.post("/api/location/sync")
def sync_live_location(request: Request, body: LiveLocationSync) -> dict[str, Any]:
    session = current_session(request)
    result = spotify_get(
        SPOTIFY_RECENTLY_PLAYED_URL,
        session,
        {"after": body.after_ms, "limit": 50},
    )
    tracks = []
    for item in result.get("items", []):
        track = item["track"]
        tracks.append(
            {
                "id": track["id"],
                "name": track["name"],
                "artists": [artist["name"] for artist in track.get("artists", [])],
                "artist_ids": [
                    artist["id"] for artist in track.get("artists", []) if artist.get("id")
                ],
                "duration_ms": track.get("duration_ms"),
                "album": track.get("album", {}).get("name", ""),
                "image": next(
                    iter(track.get("album", {}).get("images", [])), {}
                ).get("url"),
                "url": track.get("external_urls", {}).get("spotify"),
                "played_at": item["played_at"],
            }
        )

    sync_result = store.sync_plays_with_location(
        tracks,
        body.latitude,
        body.longitude,
    )
    return {
        "captured": sync_result["inserted"],
        "tagged": sync_result["tagged"],
        "place": sync_result["place"],
    }


@app.get("/api/recommender/settings")
def get_settings(request: Request) -> dict[str, Any]:
    current_session(request)
    return {"settings": store.settings()}


@app.put("/api/recommender/settings")
def put_settings(request: Request, body: RecommenderSettings) -> dict[str, Any]:
    current_session(request)
    patch = body.model_dump(exclude_none=True)
    return {"settings": store.update_settings(patch)}


@app.post("/api/recommendations")
def recommendations(request: Request, body: RecommendRequest) -> dict[str, Any]:
    current_session(request)
    now = datetime.now().astimezone()
    hour = body.hour if body.hour is not None else now.hour + now.minute / 60
    weekday = body.weekday if body.weekday is not None else now.weekday()
    context = {
        "hour": hour,
        "weekday": weekday,
        "place_id": body.place_id,
        "seed_track_ids": body.seed_track_ids,
    }
    results = recommend(
        store.list_plays(limit=500),
        store.settings(),
        hour=hour,
        weekday=weekday,
        timezone_offset_minutes=body.timezone_offset_minutes,
        place_id=body.place_id,
        latitude=body.latitude,
        longitude=body.longitude,
        seed_track_ids=body.seed_track_ids,
        limit=body.limit,
    )
    if store.settings().get("search_logging_enabled", False):
        results = add_search_affinity(results, store.list_search_events(), context)
    else:
        results = [
            {**item, "parts": {**item["parts"], "search_affinity": 0.0}}
            for item in results
        ]
    trained_model = load_model()
    results = rank_with_model(results, context, trained_model)
    session_id = str(uuid.uuid4())
    store.record_recommendations(session_id, context, results)
    return {
        "session_id": session_id,
        "context": context,
        "ranking_model": "trained logistic regression" if trained_model else "rule-based baseline",
        "recommendations": results,
    }


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        sessions.pop(session_id, None)
    response = RedirectResponse(frontend_url())
    response.delete_cookie(SESSION_COOKIE)
    return response


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
