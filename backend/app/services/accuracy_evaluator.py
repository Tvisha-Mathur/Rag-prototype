"""Purpose: Implements the accuracy evaluator application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


SCORE_FIELDS = (
    "safety_impact",
    "damage_to_assets",
    "business_continuity",
    "reputational_impact",
    "vip_safety_impact",
    "likelihood_of_more_severe_outcomes",
)


def normalize_hipo(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hipo_classification") or value.get("classification")
    label = str(value or "").strip().lower().replace("_", "-")
    if label == "hipo":
        return "HIPO"
    if label in {"non-hipo", "not hipo", "non hipo"}:
        return "Non-HIPO"
    raise ValueError(f"Unsupported HIPO label: {value!r}")


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "domain": str(result.get("domain") or "").strip() or None,
        "subdomain": str(result.get("subdomain") or "").strip() or None,
    }
    for field in SCORE_FIELDS:
        value = result.get(field)
        if isinstance(value, dict):
            value = value.get("score")
        if not isinstance(value, int) or isinstance(value, bool) or value not in range(1, 6):
            raise ValueError(f"{field} must be an integer from 1 to 5")
        normalized[field] = value
    normalized["hipo_classification"] = normalize_hipo(result.get("hipo_classification"))
    return normalized


def calculate_case_metrics(predicted: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    predicted = normalize_result(predicted)
    expected = normalize_result(expected)
    errors = {
        field: {
            "predicted": predicted[field],
            "expected": expected[field],
            "absolute_error": abs(predicted[field] - expected[field]),
            "exact": predicted[field] == expected[field],
            "within_one": abs(predicted[field] - expected[field]) <= 1,
        }
        for field in SCORE_FIELDS
    }
    domain_correct = predicted["domain"] == expected["domain"]
    subdomain_correct = predicted["subdomain"] == expected["subdomain"]
    return {
        "predicted": predicted,
        "expected": expected,
        "correctness": {
            "domain": domain_correct,
            "subdomain": subdomain_correct,
            "domain_subdomain_pair": domain_correct and subdomain_correct,
            "hipo": predicted["hipo_classification"] == expected["hipo_classification"],
        },
        "score_errors": errors,
    }


def safe_divide(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


class AccuracyEvaluator:
    def __init__(self, database: Any) -> None:
        self.database = database

    def ensure_indexes(self) -> None:
        self.database["incident_responses"].create_index([
            ("review_status", 1),
            ("prediction_metadata.model", 1),
            ("prediction_metadata.rule_version", 1),
        ])
        self.database["evaluation_case_results"].create_index(
            [("evaluation_run_id", 1), ("query_id", 1)], unique=True
        )
        self.database["evaluation_runs"].create_index([("created_at", -1)])

    def save_expert_review(
        self,
        query_id: str,
        review: dict[str, Any],
        reviewer: str,
        reviewer_role: str,
    ) -> dict[str, Any]:
        responses = self.database["incident_responses"]
        record = responses.find_one({"query_id": query_id}, {"model_prediction": 1})
        if not record:
            raise ValueError("Incident response was not found.")
        if not record.get("model_prediction"):
            raise ValueError("This legacy record has no immutable model prediction.")

        expected = normalize_result(review)
        hierarchy = self.database["taxonomy_hierarchy"]
        if hierarchy.find_one({
            "domain": expected["domain"],
            "subdomains": expected["subdomain"],
            "active": True,
        }, {"_id": 1}) is None:
            raise ValueError("Expert Domain/Subdomain pair is not in the active master list.")

        calculated = "HIPO" if max(expected[field] for field in SCORE_FIELDS) >= 4 else "Non-HIPO"
        if expected["hipo_classification"] != calculated:
            raise ValueError(f"Expert HIPO label conflicts with the score rule; expected {calculated}.")
        now = datetime.now(UTC)
        responses.update_one(
            {"query_id": query_id},
            {"$set": {
                "expert_review": expected,
                "review_status": "verified",
                "reviewer": reviewer,
                "reviewer_role": reviewer_role,
                "reviewed_at": now,
                "updated_at": now,
            }},
        )
        return expected

    def run(self, *, model: str | None = None, rule_version: str | None = None) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "review_status": "verified",
            "model_prediction": {"$exists": True},
            "expert_review": {"$exists": True},
        }
        if model:
            filters["prediction_metadata.model"] = model
        if rule_version:
            filters["prediction_metadata.rule_version"] = rule_version
        records = list(self.database["incident_responses"].find(filters))
        if not records:
            raise ValueError("No verified expert-reviewed predictions are available.")

        run_id = str(uuid4())
        now = datetime.now(UTC)
        cases = []
        for record in records:
            metrics = calculate_case_metrics(record["model_prediction"], record["expert_review"])
            cases.append({
                "evaluation_run_id": run_id,
                "query_id": record["query_id"],
                "prediction_metadata": record.get("prediction_metadata", {}),
                **metrics,
                "evaluated_at": now,
            })

        total = len(cases)
        expected_hipo = [case["expected"]["hipo_classification"] for case in cases]
        predicted_hipo = [case["predicted"]["hipo_classification"] for case in cases]
        tp = sum(e == "HIPO" and p == "HIPO" for e, p in zip(expected_hipo, predicted_hipo))
        fp = sum(e != "HIPO" and p == "HIPO" for e, p in zip(expected_hipo, predicted_hipo))
        fn = sum(e == "HIPO" and p != "HIPO" for e, p in zip(expected_hipo, predicted_hipo))
        tn = total - tp - fp - fn
        precision, recall = safe_divide(tp, tp + fp), safe_divide(tp, tp + fn)
        score_metrics = {}
        for field in SCORE_FIELDS:
            field_errors = [case["score_errors"][field] for case in cases]
            score_metrics[field] = {
                "exact_accuracy": safe_divide(sum(item["exact"] for item in field_errors), total),
                "mean_absolute_error": safe_divide(sum(item["absolute_error"] for item in field_errors), total),
                "within_one_accuracy": safe_divide(sum(item["within_one"] for item in field_errors), total),
            }
        summary = {
            "evaluation_run_id": run_id,
            "filters": {"model": model, "rule_version": rule_version},
            "sample_size": total,
            "domain_accuracy": safe_divide(sum(c["correctness"]["domain"] for c in cases), total),
            "subdomain_accuracy": safe_divide(sum(c["correctness"]["subdomain"] for c in cases), total),
            "pair_accuracy": safe_divide(sum(c["correctness"]["domain_subdomain_pair"] for c in cases), total),
            "hipo": {
                "true_positive": tp, "false_positive": fp,
                "false_negative": fn, "true_negative": tn,
                "accuracy": safe_divide(tp + tn, total),
                "precision": precision, "recall": recall,
                "specificity": safe_divide(tn, tn + fp),
                "f1": safe_divide(2 * precision * recall, precision + recall),
            },
            "score_metrics": score_metrics,
            "created_at": now,
        }
        self.database["evaluation_case_results"].insert_many(cases)
        self.database["evaluation_runs"].insert_one(summary)
        return summary
