from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.learned_ranker import DEFAULT_MODEL_PATH, FEATURE_NAMES, feature_vector
from app.store import ListeningStore

POSITIVE_ACTIONS = {"played", "saved", "liked"}


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, value))))


def split_by_session(
    examples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session_times: dict[str, str] = {}
    for example in examples:
        session_times[example["session_id"]] = example["created_at"]
    sessions = sorted(session_times.items(), key=lambda item: item[1])
    if len(sessions) < 5:
        raise ValueError("Need feedback from at least five recommendation sessions for a temporal holdout.")
    split_at = max(1, min(len(sessions) - 1, int(len(sessions) * 0.8)))
    training_ids = {session_id for session_id, _ in sessions[:split_at]}
    train = [row for row in examples if row["session_id"] in training_ids]
    test = [row for row in examples if row["session_id"] not in training_ids]
    return train, test


def train_weights(
    rows: list[dict[str, Any]],
) -> tuple[list[float], float]:
    weights = [0.0] * len(FEATURE_NAMES)
    bias = 0.0
    learning_rate = 0.08
    regularization = 0.002
    for _ in range(600):
        weight_gradient = [0.0] * len(weights)
        bias_gradient = 0.0
        for row in rows:
            probability = sigmoid(bias + sum(
                weight * feature
                for weight, feature in zip(weights, row["vector"], strict=True)
            ))
            error = probability - row["label"]
            bias_gradient += error
            for index, feature in enumerate(row["vector"]):
                weight_gradient[index] += error * feature
        count = len(rows)
        bias -= learning_rate * bias_gradient / count
        weights = [
            weight - learning_rate * (gradient / count + regularization * weight)
            for weight, gradient in zip(weights, weight_gradient, strict=True)
        ]
    return weights, bias


def evaluate(
    rows: list[dict[str, Any]],
    weights: list[float],
    bias: float,
) -> dict[str, float]:
    predictions = [
        sigmoid(bias + sum(
            weight * feature
            for weight, feature in zip(weights, row["vector"], strict=True)
        ))
        for row in rows
    ]
    accuracy = sum(
        (prediction >= 0.5) == bool(row["label"])
        for prediction, row in zip(predictions, rows, strict=True)
    ) / len(rows)
    log_loss = -sum(
        row["label"] * math.log(max(probability, 1e-12))
        + (1 - row["label"]) * math.log(max(1 - probability, 1e-12))
        for probability, row in zip(predictions, rows, strict=True)
    ) / len(rows)

    grouped: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for probability, row in zip(predictions, rows, strict=True):
        grouped[row["session_id"]].append((probability, row["label"]))
    ndcg_values = []
    for group in grouped.values():
        ideal = sorted((label for _, label in group), reverse=True)
        if not any(ideal):
            continue
        ranked = sorted(group, reverse=True)
        dcg = sum(label / math.log2(index + 2) for index, (_, label) in enumerate(ranked[:5]))
        idcg = sum(label / math.log2(index + 2) for index, label in enumerate(ideal[:5]))
        ndcg_values.append(dcg / idcg)
    return {
        "accuracy": round(accuracy, 4),
        "log_loss": round(log_loss, 4),
        "ndcg_at_5": round(sum(ndcg_values) / len(ndcg_values), 4) if ndcg_values else 0.0,
    }


def ndcg_at_5(rows: list[dict[str, Any]], scores: list[float]) -> float:
    grouped: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for score, row in zip(scores, rows, strict=True):
        grouped[row["session_id"]].append((score, row["label"]))
    values = []
    for group in grouped.values():
        ideal = sorted((label for _, label in group), reverse=True)
        if not any(ideal):
            continue
        ranked = sorted(group, reverse=True)
        dcg = sum(label / math.log2(index + 2) for index, (_, label) in enumerate(ranked[:5]))
        idcg = sum(label / math.log2(index + 2) for index, label in enumerate(ideal[:5]))
        values.append(dcg / idcg)
    return round(sum(values) / len(values), 4) if values else 0.0


def train(db_path: Path, output_path: Path) -> dict[str, Any]:
    store = ListeningStore(db_path)
    examples = store.training_examples()
    expanded = []
    for row in examples:
        if row["action"] not in POSITIVE_ACTIONS | {"not_for_me"}:
            continue
        expanded.append(
            {
                **row,
                "label": int(row["action"] in POSITIVE_ACTIONS),
                "vector": feature_vector(row["features"], row["context"]),
            }
        )
    if len(expanded) < 20:
        raise ValueError(f"Need at least 20 explicit feedback examples; found {len(expanded)}.")
    training, test = split_by_session(expanded)
    for label, name in ((0, "negative"), (1, "positive")):
        if not any(row["label"] == label for row in training):
            raise ValueError(f"Training split has no {name} examples.")
        if not any(row["label"] == label for row in test):
            raise ValueError(f"Temporal test split has no {name} examples; collect more feedback.")

    weights, bias = train_weights(training)
    metrics = evaluate(test, weights, bias)
    metrics["baseline_ndcg_at_5"] = ndcg_at_5(test, [row["score"] for row in test])
    model = {
        "version": 1,
        "algorithm": "logistic_regression_sgd",
        "feature_names": list(FEATURE_NAMES),
        "weights": [round(weight, 8) for weight in weights],
        "bias": round(bias, 8),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "examples": {"total": len(expanded), "train": len(training), "test": len(test)},
        "metrics": metrics,
        "temporal_split": "oldest 80% of sessions train; newest 20% test",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train/evaluate Aftertone's recommendation ranker.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "aftertone.db",
        help="SQLite listening database",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL_PATH, help="Model artifact JSON path")
    args = parser.parse_args()
    try:
        model = train(args.db, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"model": str(args.output), "examples": model["examples"], "metrics": model["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
