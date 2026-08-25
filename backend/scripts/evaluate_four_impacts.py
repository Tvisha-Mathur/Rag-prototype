"""Purpose: Provides the evaluate four impacts command-line utility.

Used by: Run manually or via python -m backend.scripts.evaluate_four_impacts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from backend.app.services.four_impact_scoring import FOUR_IMPACT_FIELDS, FourImpactScores


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in results if row["predicted"] is not None]
    field_metrics = {}
    for field in FOUR_IMPACT_FIELDS:
        errors = [abs(row["predicted"][field] - row["expected"][field]) for row in valid]
        field_metrics[field] = {
            "exact_accuracy": sum(error == 0 for error in errors) / len(errors) if errors else 0.0,
            "within_one_accuracy": sum(error <= 1 for error in errors) / len(errors) if errors else 0.0,
            "mean_absolute_error": sum(errors) / len(errors) if errors else 0.0,
            "under_scoring_rate": (
                sum(row["predicted"][field] < row["expected"][field] for row in valid) / len(valid)
                if valid else 0.0
            ),
            "serious_under_scoring_count": sum(
                row["expected"][field] >= 4 and row["predicted"][field] <= 2 for row in valid
            ),
        }
    macro_exact = sum(item["exact_accuracy"] for item in field_metrics.values()) / len(FOUR_IMPACT_FIELDS)
    macro_mae = sum(item["mean_absolute_error"] for item in field_metrics.values()) / len(FOUR_IMPACT_FIELDS)
    return {
        "sample_size": len(results),
        "valid_predictions": len(valid),
        "schema_validity_rate": len(valid) / len(results) if results else 0.0,
        "macro_exact_accuracy": macro_exact,
        "macro_mean_absolute_error": macro_mae,
        "fields": field_metrics,
    }


def write_workbook(path: Path, results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    workbook = Workbook()
    details = workbook.active
    details.title = "Case Results"
    headers = ["Case ID", "Incident Narrative"]
    for field in FOUR_IMPACT_FIELDS:
        headers.extend([f"Expected {field}", f"Predicted {field}", f"Absolute error {field}"])
    headers.append("Error")
    details.append(headers)
    for row in results:
        values = [row["case_id"], row["incident_narrative"]]
        for field in FOUR_IMPACT_FIELDS:
            predicted = row["predicted"][field] if row["predicted"] else None
            expected = row["expected"][field]
            values.extend([expected, predicted, abs(predicted - expected) if predicted is not None else None])
        values.append(row["error"])
        details.append(values)
    metrics = workbook.create_sheet("Summary")
    metrics.append(["Metric", "Value"])
    for key in ("sample_size", "valid_predictions", "schema_validity_rate", "macro_exact_accuracy", "macro_mean_absolute_error"):
        metrics.append([key, summary[key]])
    for field, values in summary["fields"].items():
        for metric, value in values.items():
            metrics.append([f"{field}.{metric}", value])
    details.freeze_panes = "A2"
    details.column_dimensions["B"].width = 70
    metrics.column_dimensions["A"].width = 55
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def main() -> None:
    from backend.app.services.llm_analyzer import LLMAnalyzer

    parser = argparse.ArgumentParser(description="Evaluate four-impact scores on a frozen JSONL split.")
    parser.add_argument("--dataset", type=Path, default=Path("data/fine_tuning/four_impacts/test.jsonl"))
    parser.add_argument("--model")
    parser.add_argument("--output-prefix", type=Path, default=Path("outputs/four_impacts_baseline"))
    args = parser.parse_args()

    analyzer = LLMAnalyzer(model=args.model)
    dataset = load_jsonl(args.dataset)
    results = []
    for index, record in enumerate(dataset, 1):
        expected = FourImpactScores.model_validate(record["expected"]).model_dump()
        error = None
        try:
            prediction = analyzer.score_four_impacts(
                record["incident_narrative"],
                record["policy_rules"],
                record["verified_examples"],
            ).model_dump()
        except Exception as exc:
            prediction = None
            error = str(exc)
        results.append({
            "case_id": record["metadata"]["case_id"],
            "incident_narrative": record["incident_narrative"],
            "expected": expected,
            "predicted": prediction,
            "error": error,
        })
        print(f"Evaluated {index}/{len(dataset)}")

    summary = summarize(results)
    report = {"model": args.model or analyzer.model, "dataset": str(args.dataset), "summary": summary, "cases": results}
    json_path = args.output_prefix.with_suffix(".json")
    xlsx_path = args.output_prefix.with_suffix(".xlsx")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_workbook(xlsx_path, results, summary)
    print(json.dumps(summary, indent=2))
    print(f"Saved {json_path} and {xlsx_path}")


if __name__ == "__main__":
    main()
