from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FEATURE_NAMES = (
    "sequence",
    "artist",
    "time",
    "location",
    "recency",
    "novelty_penalty",
    "search_affinity",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "has_place",
    "seed_count",
)
LEGACY_FEATURE_NAMES = tuple(name for name in FEATURE_NAMES if name != "artist")
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "recommender_model.json"


def feature_vector(
    parts: dict[str, float],
    context: dict[str, Any],
) -> list[float]:
    hour = float(context.get("hour") or 0.0) % 24
    weekday = int(context.get("weekday") or 0) % 7
    return [
        float(parts.get("sequence", 0.0)),
        float(parts.get("artist", 0.0)),
        float(parts.get("time", 0.0)),
        float(parts.get("location", 0.0)),
        float(parts.get("recency", 0.0)),
        float(parts.get("novelty_penalty", 0.0)),
        float(parts.get("search_affinity", 0.0)),
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
        math.sin(2 * math.pi * weekday / 7),
        math.cos(2 * math.pi * weekday / 7),
        float(context.get("place_id") is not None),
        min(len(context.get("seed_track_ids", [])), 8) / 8.0,
    ]


def load_model(path: str | Path | None = None) -> dict[str, Any] | None:
    model_path = Path(path or os.getenv("AFTERTONE_MODEL", DEFAULT_MODEL_PATH))
    if not model_path.exists():
        return None
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read trained recommendation model at {model_path}") from exc
    if model.get("feature_names") not in (list(FEATURE_NAMES), list(LEGACY_FEATURE_NAMES)):
        raise RuntimeError(f"Trained recommendation model has incompatible features: {model_path}")
    if len(model.get("weights", [])) != len(model["feature_names"]):
        raise RuntimeError(f"Trained recommendation model has invalid weights: {model_path}")
    return model


def rank_with_model(
    recommendations: list[dict[str, Any]],
    context: dict[str, Any],
    model: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if model is None:
        return recommendations
    weights = model["weights"]
    bias = float(model["bias"])
    ranked = []
    for item in recommendations:
        all_values = dict(zip(FEATURE_NAMES, feature_vector(item["parts"], context), strict=True))
        vector = [all_values[name] for name in model["feature_names"]]
        raw = bias + sum(weight * value for weight, value in zip(weights, vector, strict=True))
        probability = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, raw))))
        ranked.append({**item, "learned_probability": round(probability, 4)})
    return sorted(ranked, key=lambda item: item["learned_probability"], reverse=True)


def add_search_affinity(
    recommendations: list[dict[str, Any]],
    search_events: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not search_events:
        return recommendations
    now = datetime.now(timezone.utc)
    hour = float(context.get("hour") or 0.0)
    place_id = context.get("place_id")
    enriched = []
    for item in recommendations:
        track = item["track"]
        candidate_tokens = set(
            re.findall(r"[a-z0-9]+", " ".join([track["name"], *track.get("artists", [])]).lower())
        )
        affinity = 0.0
        for event in search_events:
            query_tokens = set(re.findall(r"[a-z0-9]+", event["query"].lower()))
            lexical_match = len(query_tokens & candidate_tokens) / max(len(query_tokens), 1)
            if event["selected_track_id"] == track["id"]:
                lexical_match = 1.0
            if lexical_match == 0:
                continue
            searched_at = datetime.fromisoformat(event["searched_at"].replace("Z", "+00:00"))
            if searched_at.tzinfo is None:
                searched_at = searched_at.replace(tzinfo=timezone.utc)
            age_days = max((now - searched_at.astimezone(timezone.utc)).total_seconds() / 86400, 0.0)
            recency = math.exp(-age_days / 30.0)
            hour_delta = abs(float(event["local_hour"]) - hour) % 24
            hour_factor = math.exp(-(min(hour_delta, 24 - hour_delta) ** 2) / (2 * 4.0**2))
            place_factor = 1.0 if place_id is None or event["place_id"] == place_id else 0.5
            affinity = max(affinity, lexical_match * recency * (0.5 + 0.5 * hour_factor) * place_factor)
        enriched.append(
            {
                **item,
                "parts": {**item["parts"], "search_affinity": round(affinity, 4)},
            }
        )
    return enriched
