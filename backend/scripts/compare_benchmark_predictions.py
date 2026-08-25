"""Purpose: Compares benchmark checkpoints for exact prediction equivalence.

Used by: Speed-optimization regression checks before holdout or final evaluation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backend.scripts.benchmark_excel_accuracy import FIELDS


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Checkpoint must contain a JSON list: {path}")
    return {str(item["case_id"]): item for item in data}


def compare_predictions(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for case_id in sorted(set(baseline) | set(candidate)):
        before = baseline.get(case_id)
        after = candidate.get(case_id)
        if before is None or after is None:
            differences.append({
                "case_id": case_id,
                "field": "case_presence",
                "baseline": before is not None,
                "candidate": after is not None,
            })
            continue
        for field in FIELDS:
            before_value = (before.get("predicted") or {}).get(field)
            after_value = (after.get("predicted") or {}).get(field)
            if before_value != after_value:
                differences.append({
                    "case_id": case_id,
                    "field": field,
                    "baseline": before_value,
                    "candidate": after_value,
                })
    return differences


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Require exact prediction equivalence between two benchmark checkpoints."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    differences = compare_predictions(
        load_results(args.baseline), load_results(args.candidate)
    )
    report = {
        "identical": not differences,
        "difference_count": len(differences),
        "differences": differences,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Saved comparison report: {args.output.resolve()}")
    print(rendered)
    raise SystemExit(0 if not differences else 1)


if __name__ == "__main__":
    main()
