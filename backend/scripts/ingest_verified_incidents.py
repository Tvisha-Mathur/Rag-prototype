"""Purpose: Provides the ingest verified incidents command-line utility.

Used by: Run manually or via python -m backend.scripts.ingest_verified_incidents.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pymongo import UpdateOne

from backend.app.services.retriever import EMBEDDING_DIMENSIONS, MODEL_NAME, RetrieverService


DEFAULT_SOURCE = Path("data/raw/Incident_HIPO_Classification_50_Cases.xlsx")
CHUNK_TYPE = "historical_incident"

ALIASES = {
    "incident_no": ("incident no", "incident number", "test case id", "case id", "id"),
    "incident_summary": ("incident narrative", "incident summary", "incident title", "narrative", "incident"),
    "domain": ("domain",),
    "subdomain": ("subdomain", "sub domain", "sub-domain"),
    "hipo_classification": ("hipo classification", "hipo / not hipo", "hipo", "classification"),
    "hipo_classification_reason": ("hipo reason", "reason for hipo classification", "classification reason", "reason"),
    "hazard": ("hazard", "primary hazard"),
    "exposure": ("exposure",),
    "actual_outcome": ("actual outcome", "outcome"),
    "energy_source": ("energy source",),
    "people_exposed": ("people exposed", "persons exposed"),
    "critical_controls": ("critical controls", "controls"),
    "credible_worst_case": ("credible worst case", "worst case"),
    "safety_impact": ("safety impact", "safety impact 1 5"),
    "damage_to_assets": ("damage to assets", "damage to assets 1 5", "asset impact"),
    "business_continuity": ("business continuity", "business continuity 1 5"),
    "reputational_impact": ("reputational impact", "reputational impact 1 5"),
    "vip_safety": ("vip safety", "safety lapse for vip", "safety lapse for vip 1 5"),
    "likelihood_of_more_severe_outcome": (
        "likelihood of more severe outcome", "likelihood of more severe outcomes 1 5", "likelihood"
    ),
}


def normalized_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()


def clean(value: Any) -> str | None:
    if pd.isna(value):
        return None
    result = str(value).strip()
    return result or None


def column_mapping(columns: list[Any]) -> dict[str, Any]:
    available = {normalized_header(column): column for column in columns}
    mapping = {}
    for field, aliases in ALIASES.items():
        match = next((available[alias] for alias in aliases if alias in available), None)
        if match is not None:
            mapping[field] = match
    missing = [field for field in ("incident_summary", "domain", "subdomain", "hipo_classification") if field not in mapping]
    if missing:
        raise ValueError(f"Missing required workbook fields: {', '.join(missing)}. Found: {list(columns)}")
    return mapping


def normalize_hipo(value: str | None) -> str:
    label = normalized_header(value or "")
    if label in {"hipo", "yes", "high potential", "high potential incident"}:
        return "HIPO"
    if label in {"non hipo", "not hipo", "no"}:
        return "NON-HIPO"
    raise ValueError(f"Unsupported HIPO classification: {value!r}")


def build_search_text(record: dict[str, Any]) -> str:
    ordered = (
        ("Verified incident", record["incident_summary"]),
        ("Domain", record["domain"]), ("Subdomain", record["subdomain"]),
        ("Hazard", record.get("hazard")), ("Exposure", record.get("exposure")),
        ("Actual outcome", record.get("actual_outcome")),
        ("Energy source", record.get("energy_source")),
        ("People exposed", record.get("people_exposed")),
        ("Critical controls", record.get("critical_controls")),
        ("Credible worst case", record.get("credible_worst_case")),
        ("Safety impact", record.get("safety_impact")),
        ("Asset impact", record.get("damage_to_assets")),
        ("Business continuity", record.get("business_continuity")),
        ("Reputational impact", record.get("reputational_impact")),
        ("VIP safety", record.get("vip_safety")),
        ("Likelihood", record.get("likelihood_of_more_severe_outcome")),
        ("HIPO classification", record["hipo_classification"]),
        ("Classification reason", record.get("hipo_classification_reason")),
    )
    return "\n".join(f"{label}: {value}" for label, value in ordered if value)


def load_records(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    dataframe = pd.read_excel(source, sheet_name=0, engine="openpyxl")
    mapping = column_mapping(list(dataframe.columns))
    records, errors = [], []
    for row_number, (_, row) in enumerate(dataframe.iterrows(), start=2):
        record = {field: clean(row[column]) for field, column in mapping.items()}
        if not record.get("incident_summary"):
            continue
        try:
            record["hipo_classification"] = normalize_hipo(record.get("hipo_classification"))
            if not record.get("domain") or not record.get("subdomain"):
                raise ValueError("domain and subdomain are required")
        except ValueError as exc:
            errors.append(f"Row {row_number}: {exc}")
            continue
        record["incident_no"] = record.get("incident_no") or str(row_number - 1)
        record["source_row_number"] = row_number
        records.append(record)
    return records, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed verified classified incidents into MongoDB.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    records, errors = load_records(args.source)
    print(f"Valid records: {len(records)}")
    for error in errors:
        print(error)
    if errors:
        raise SystemExit("Workbook validation failed; MongoDB was not changed.")
    if args.validate_only:
        return

    retriever = RetrieverService()
    try:
        texts = [build_search_text(record) for record in records]
        embeddings = retriever.model.encode(texts, batch_size=32, normalize_embeddings=True)
        operations = []
        now = datetime.now(UTC)
        for record, text, vector in zip(records, texts, embeddings, strict=True):
            embedding = vector.tolist()
            if len(embedding) != EMBEDDING_DIMENSIONS:
                raise ValueError(f"Embedding dimension mismatch: {len(embedding)}")
            identity = f"{args.source.name}|{record['incident_no']}|verified_incident"
            chunk_id = "verified_incident_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            document = {
                **record,
                "chunk_id": chunk_id,
                "chunk_type": CHUNK_TYPE,
                "document_type": "verified_incident_reference",
                "source": args.source.name,
                "source_file": args.source.name,
                "source_section": f"Row {record['source_row_number']}",
                "search_text": text,
                "embedding": embedding,
                "embedding_metadata": {"model": MODEL_NAME, "dimensions": EMBEDDING_DIMENSIONS, "normalized": True, "generated_at": now},
                "verified": True,
                "reference_only": True,
                "authority_level": "verified_example",
                "active": True,
                "updated_at": now,
            }
            operations.append(UpdateOne({"chunk_id": chunk_id}, {"$set": document, "$setOnInsert": {"created_at": now}}, upsert=True))
        result = retriever.collection.bulk_write(operations, ordered=False)
        count = retriever.collection.count_documents({"source_file": args.source.name, "verified": True, "active": True})
        print(f"Inserted: {result.upserted_count}; updated: {result.modified_count}; active verified records: {count}")
    finally:
        retriever.close()


if __name__ == "__main__":
    main()
