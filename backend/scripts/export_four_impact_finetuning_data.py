"""Purpose: Provides the export four impact finetuning data command-line utility.

Used by: Run manually or via python -m backend.scripts.export_four_impact_finetuning_data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any

from backend.app.services.four_impact_scoring import (
    FOUR_IMPACT_FIELDS,
    FourImpactScores,
    build_four_impact_messages,
    compact_policy_rules,
    compact_verified_examples,
)

DEFAULT_SOURCE = Path("data/raw/Incident_HIPO_Classification_50_Cases.xlsx")


def parse_score(value: Any, field: str, case_id: str) -> int:
    try:
        score = int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Case {case_id}: invalid {field} score {value!r}") from exc
    if score not in range(1, 6):
        raise ValueError(f"Case {case_id}: {field} must be from 1 to 5")
    return score


def narrative_key(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def split_records(
    records: list[dict[str, Any]], seed: int, validation_size: int, test_size: int
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(narrative_key(record["incident_summary"]), []).append(record)
    grouped = list(groups.values())
    random.Random(seed).shuffle(grouped)

    splits = {"train": [], "validation": [], "test": []}
    for group in grouped:
        if len(splits["test"]) < test_size:
            destination = "test"
        elif len(splits["validation"]) < validation_size:
            destination = "validation"
        else:
            destination = "train"
        splits[destination].extend(group)
    if not all(splits.values()):
        raise ValueError("The requested split produced an empty train, validation, or test set")
    return splits


def case_id(item: dict[str, Any]) -> str:
    return str(item.get("incident_no") or item.get("source_query_id") or "").strip()


def retrieve_snapshot(
    retriever: Any,
    record: dict[str, Any],
    allowed_example_ids: set[str],
    example_limit: int,
    rule_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    narrative = record["incident_summary"]
    own_id = case_id(record)
    candidates = retriever.retrieve(
        narrative,
        chunk_type="historical_incident",
        limit=min(100, max(example_limit * 8, 25)),
        num_candidates=250,
    )
    examples = []
    seen = set()
    for candidate in candidates:
        candidate_id = case_id(candidate)
        if (
            not candidate_id
            or candidate_id == own_id
            or candidate_id not in allowed_example_ids
            or candidate_id in seen
            or candidate.get("verified") is not True
        ):
            continue
        seen.add(candidate_id)
        examples.append(candidate)
        if len(examples) == example_limit:
            break
    rules = retriever.retrieve(
        narrative,
        chunk_type="hipo_policy",
        limit=rule_limit,
        num_candidates=max(60, rule_limit),
    )
    return compact_policy_rules(rules, rule_limit), compact_verified_examples(examples, example_limit)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    from backend.app.services.retriever import RetrieverService
    from backend.scripts.ingest_verified_incidents import load_records

    parser = argparse.ArgumentParser(
        description="Export leakage-safe prompt/completion data for four-impact fine-tuning."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=Path("data/fine_tuning/four_impacts"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-size", type=int, default=5)
    parser.add_argument("--test-size", type=int, default=10)
    parser.add_argument("--example-limit", type=int, default=5)
    parser.add_argument("--rule-limit", type=int, default=6)
    args = parser.parse_args()

    source_records, errors = load_records(args.source)
    if errors:
        raise SystemExit("\n".join(errors))
    splits = split_records(source_records, args.seed, args.validation_size, args.test_size)
    train_ids = {case_id(item) for item in splits["train"]}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    exported: dict[str, list[dict[str, Any]]] = {name: [] for name in splits}
    retriever = RetrieverService()
    try:
        for split_name, items in splits.items():
            for index, item in enumerate(items, 1):
                current_id = case_id(item)
                allowed_ids = train_ids - {current_id}
                rules, examples = retrieve_snapshot(
                    retriever, item, allowed_ids, args.example_limit, args.rule_limit
                )
                labels = FourImpactScores(**{
                    field: parse_score(item.get(field), field, current_id)
                    for field in FOUR_IMPACT_FIELDS
                })
                messages = build_four_impact_messages(item["incident_summary"], rules, examples)
                exported[split_name].append({
                    "prompt": messages,
                    "completion": [{
                        "role": "assistant",
                        "content": labels.model_dump_json(),
                    }],
                    "incident_narrative": item["incident_summary"],
                    "policy_rules": rules,
                    "verified_examples": examples,
                    "expected": labels.model_dump(),
                    "metadata": {
                        "case_id": current_id,
                        "split": split_name,
                        "source_file": args.source.name,
                        "source_row_number": item.get("source_row_number"),
                        "retrieved_rule_ids": [rule.get("rule_id") for rule in rules],
                        "retrieved_case_ids": [example.get("case_id") for example in examples],
                    },
                })
                print(f"Prepared {split_name} case {index}/{len(items)}")
    finally:
        retriever.close()

    for name, items in exported.items():
        write_jsonl(args.output_dir / f"{name}.jsonl", items)
    manifest = {
        "seed": args.seed,
        "source": str(args.source),
        "counts": {name: len(items) for name, items in exported.items()},
        "case_ids": {name: [row["metadata"]["case_id"] for row in items] for name, items in exported.items()},
        "retrieval_policy": "All splits retrieve verified examples from the training split only; self matches are excluded.",
    }
    (args.output_dir / "split_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
