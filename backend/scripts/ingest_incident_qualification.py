"""Purpose: Provides the ingest incident qualification command-line utility.

Used by: Run manually or via python -m backend.scripts.ingest_incident_qualification.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from pymongo import UpdateOne

from backend.app.services.retriever import RetrieverService


SOURCE_FILE = Path(
    "data/raw/Incident qualification.xlsx"
)

CHUNK_TYPE = "historical_incident"
DOCUMENT_TYPE = "incident_reference"


COLUMN_MAP = {
    "Incident No": "incident_no",
    "Incident Summary": "incident_summary",
    "Domain": "domain",
    "Sub-domain": "subdomain",
    "Severity": "severity",
    "Impact": "impact",
    "Safety Impact": "safety_impact",
    "Business Continuity": "business_continuity",
    "Damage to Assets": "damage_to_assets",
    "Reputational Impact": "reputational_impact",
    "Likelihood of More Severe Outcome": (
        "likelihood_of_more_severe_outcome"
    ),
    "VIP Safety": "vip_safety",
    "Environmental Impact": "environmental_impact",
    "Immediate Control Measures Taken": (
        "immediate_control_measures"
    ),
    "HIPO / Not HIPO": "hipo_classification",
    "Reason for HIPO Classification": (
        "hipo_classification_reason"
    ),
}


def clean_value(value: Any) -> str | None:
    """Convert spreadsheet values into clean strings."""

    if pd.isna(value):
        return None

    cleaned = str(value).strip()

    if not cleaned:
        return None

    return cleaned


def create_chunk_id(
    source_file: str,
    incident_no: str,
) -> str:
    """Create a stable ID for repeatable upserts."""

    raw_value = (
        f"{source_file}|{incident_no}|{CHUNK_TYPE}"
    )

    digest = hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()

    return f"historical_incident_{digest[:24]}"


def build_search_text(
    record: dict[str, Any],
) -> str:
    """Build text for vector embedding and retrieval."""

    def value(field_name: str) -> str:
        return str(
            record.get(field_name) or "Not provided"
        )

    parts = [
        (
            "Historical incident reference record. "
            "Use this as supporting evidence only."
        ),
        f"Incident summary: {value('incident_summary')}",
        f"Domain: {value('domain')}",
        f"Subdomain: {value('subdomain')}",
        f"Severity: {value('severity')}",
        f"Impact: {value('impact')}",
        f"Safety impact: {value('safety_impact')}",
        (
            "Business continuity impact: "
            f"{value('business_continuity')}"
        ),
        (
            "Asset damage impact: "
            f"{value('damage_to_assets')}"
        ),
        (
            "Reputational impact: "
            f"{value('reputational_impact')}"
        ),
        (
            "Likelihood of more severe outcome: "
            f"{value('likelihood_of_more_severe_outcome')}"
        ),
        f"VIP safety impact: {value('vip_safety')}",
        (
            "Environmental impact: "
            f"{value('environmental_impact')}"
        ),
        (
            "Immediate control measures: "
            f"{value('immediate_control_measures')}"
        ),
        (
            "HIPO classification: "
            f"{value('hipo_classification')}"
        ),
        (
            "Reason for HIPO classification: "
            f"{value('hipo_classification_reason')}"
        ),
    ]

    return "\n".join(parts)


def load_records() -> list[dict[str, Any]]:
    """Read and normalize the spreadsheet."""

    if not SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"Spreadsheet not found: {SOURCE_FILE.resolve()}"
        )

    dataframe = pd.read_excel(
        SOURCE_FILE,
        sheet_name=0,
        engine="openpyxl",
    )

    missing_columns = [
        column
        for column in COLUMN_MAP
        if column not in dataframe.columns
    ]

    if missing_columns:
        raise ValueError(
            "Spreadsheet is missing required columns: "
            + ", ".join(missing_columns)
        )

    dataframe = dataframe.rename(
        columns=COLUMN_MAP
    )

    records: list[dict[str, Any]] = []

    for _, row in dataframe.iterrows():
        record = {
            field_name: clean_value(
                row.get(field_name)
            )
            for field_name in COLUMN_MAP.values()
        }

        if not record.get("incident_summary"):
            continue

        incident_no = (
            record.get("incident_no")
            or str(len(records) + 1)
        )

        record["incident_no"] = incident_no
        records.append(record)

    return records


def main() -> None:
    records = load_records()

    print(
        f"Loaded {len(records)} historical incidents."
    )

    retriever = RetrieverService()

    try:
        collection = retriever.collection
        operations: list[UpdateOne] = []

        for index, record in enumerate(
            records,
            start=1,
        ):
            search_text = build_search_text(record)

            embedding = retriever.model.encode(
                search_text,
                normalize_embeddings=True,
            ).tolist()

            chunk_id = create_chunk_id(
                source_file=SOURCE_FILE.name,
                incident_no=str(
                    record["incident_no"]
                ),
            )

            document = {
                "chunk_id": chunk_id,
                "chunk_type": CHUNK_TYPE,
                "document_type": DOCUMENT_TYPE,
                "source": SOURCE_FILE.name,
                "source_file": SOURCE_FILE.name,
                "source_section": (
                    f"Incident {record['incident_no']}"
                ),
                "incident_no": record["incident_no"],
                "incident_summary": (
                    record.get("incident_summary")
                ),
                "domain": record.get("domain"),
                "subdomain": record.get("subdomain"),
                "severity": record.get("severity"),
                "impact": record.get("impact"),
                "safety_impact": (
                    record.get("safety_impact")
                ),
                "business_continuity": (
                    record.get("business_continuity")
                ),
                "damage_to_assets": (
                    record.get("damage_to_assets")
                ),
                "reputational_impact": (
                    record.get("reputational_impact")
                ),
                "likelihood_of_more_severe_outcome": (
                    record.get(
                        "likelihood_of_more_severe_outcome"
                    )
                ),
                "vip_safety": (
                    record.get("vip_safety")
                ),
                "environmental_impact": (
                    record.get("environmental_impact")
                ),
                "immediate_control_measures": (
                    record.get(
                        "immediate_control_measures"
                    )
                ),
                "hipo_classification": (
                    record.get("hipo_classification")
                ),
                "hipo_classification_reason": (
                    record.get(
                        "hipo_classification_reason"
                    )
                ),
                "search_text": search_text,
                "embedding": embedding,
                "active": True,
                "reference_only": True,
                "authority_level": "historical_example",
                "updated_at": datetime.now(
                    timezone.utc
                ),
            }

            operations.append(
                UpdateOne(
                    {
                        "chunk_id": chunk_id,
                    },
                    {
                        "$set": document,
                        "$setOnInsert": {
                            "created_at": datetime.now(
                                timezone.utc
                            ),
                        },
                    },
                    upsert=True,
                )
            )

            print(
                f"Prepared {index}/{len(records)}: "
                f"incident {record['incident_no']}"
            )

        if not operations:
            print("No valid records were found.")
            return

        result = collection.bulk_write(
            operations,
            ordered=False,
        )

        print("\nIngestion completed.")
        print(
            "Inserted:",
            result.upserted_count,
        )
        print(
            "Updated:",
            result.modified_count,
        )

        stored_count = collection.count_documents(
            {
                "chunk_type": CHUNK_TYPE,
                "source_file": SOURCE_FILE.name,
                "active": True,
            }
        )

        print(
            "Active historical incident records:",
            stored_count,
        )

    finally:
        retriever.close()


if __name__ == "__main__":
    main()