"""Purpose: Provides the load knowledge command-line utility.

Used by: Run manually or via python -m backend.scripts.load_knowledge.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import BulkWriteError, PyMongoError

from backend.app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]

TAXONOMY_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "taxonomy_cleaned.jsonl"
)

POLICY_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "policy_chunks.jsonl"
)

REPORT_FILE = (
    PROJECT_ROOT
    / "data"
    / "reports"
    / "knowledge_loading_report.json"
)

BATCH_SIZE = 500


def read_jsonl(file_path: Path) -> Iterator[dict[str, Any]]:
    """Read one JSON document at a time from a JSONL file."""

    if not file_path.exists():
        raise FileNotFoundError(
            f"Required JSONL file does not exist: {file_path}"
        )

    with file_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()

            if not stripped:
                continue

            try:
                document = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {file_path.name}, "
                    f"line {line_number}: {exc}"
                ) from exc

            if not isinstance(document, dict):
                raise ValueError(
                    f"Expected an object in {file_path.name}, "
                    f"line {line_number}"
                )

            yield document


def transform_taxonomy_record(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Convert a cleaned taxonomy record into a knowledge document."""

    record_id = record.get("record_id")

    if not record_id:
        raise ValueError(
            "Taxonomy record is missing record_id."
        )

    search_text = record.get("search_text")

    if not search_text:
        raise ValueError(
            f"Taxonomy record {record_id} is missing search_text."
        )

    return {
        "chunk_id": record_id,
        "chunk_type": "taxonomy",
        "document_type": "domain_subdomain_repository",
        "domain": record.get("domain"),
        "subdomain": record.get("subdomain"),
        "code": record.get("code"),
        "hazard_identified": record.get(
            "hazard_identified"
        ),
        "severity": record.get("severity"),
        "severity_level": record.get(
            "severity_level"
        ),
        "risk_type": record.get("risk_type"),
        "risk_identified": record.get(
            "risk_identified"
        ),
        "risk_explanation": record.get(
            "risk_explanation"
        ),
        "control_measures": record.get(
            "control_measures"
        ),
        "text": search_text,
        "search_text": search_text,
        "source": record.get("source"),
        "version": record.get(
            "taxonomy_version",
            "prototype-v1",
        ),
        "active": bool(record.get("active", True)),
        "embedding": None,
        "embedding_metadata": None,
        "updated_at": datetime.now(UTC),
    }


def transform_policy_chunk(
    chunk: dict[str, Any],
) -> dict[str, Any]:
    """Convert a policy chunk into a knowledge document."""

    chunk_id = chunk.get("chunk_id")

    if not chunk_id:
        raise ValueError(
            "Policy chunk is missing chunk_id."
        )

    search_text = chunk.get("search_text")

    if not search_text:
        raise ValueError(
            f"Policy chunk {chunk_id} is missing search_text."
        )

    document_name = chunk.get("document_name")

    type_mapping = {
        "hipo_classification.pdf": "hipo_policy",
        "escalation_matrix.pdf": "severity_policy",
        "root_cause_analysis.pdf": "rca_guidance",
    }

    mapped_chunk_type = type_mapping.get(document_name)

    if not mapped_chunk_type:
        raise ValueError(
            f"Unknown policy document: {document_name}"
        )

    return {
        "chunk_id": chunk_id,
        "chunk_type": mapped_chunk_type,
        "document_type": document_name,
        "domain": None,
        "subdomain": None,
        "section": chunk.get("section"),
        "page_number": chunk.get("page_number"),
        "knowledge_type": chunk.get("knowledge_type"),
        "document": chunk.get("document"),
        "parameter": chunk.get("parameter"),
        "score": chunk.get("score"),
        "severity": chunk.get("severity"),
        "tags": chunk.get("tags", []),
        "priority": chunk.get("priority"),
        "rule_type": chunk.get("rule_type"),
        "text": chunk.get("text"),
        "search_text": search_text,
        "source": chunk.get("source_metadata"),
        "version": chunk.get(
            "version",
            "prototype-v1",
        ),
        "active": bool(chunk.get("active", True)),
        "embedding": None,
        "embedding_metadata": None,
        "updated_at": datetime.now(UTC),
    }


def execute_batch(
    collection: Collection,
    operations: list[UpdateOne],
) -> dict[str, int]:
    """Execute a batch of idempotent upserts."""

    if not operations:
        return {
            "matched": 0,
            "modified": 0,
            "upserted": 0,
        }

    result = collection.bulk_write(
        operations,
        ordered=False,
    )

    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
        "upserted": len(result.upserted_ids),
    }


def load_documents(
    collection: Collection,
    documents: Iterator[dict[str, Any]],
) -> dict[str, int]:
    """Load documents into MongoDB using batched upserts."""

    totals = {
        "read": 0,
        "matched": 0,
        "modified": 0,
        "upserted": 0,
    }

    operations: list[UpdateOne] = []

    for document in documents:
        totals["read"] += 1

        operations.append(
            UpdateOne(
                {
                    "chunk_id": document["chunk_id"],
                },
                {
                    "$set": document,
                    "$setOnInsert": {
                        "created_at": datetime.now(UTC),
                    },
                },
                upsert=True,
            )
        )

        if len(operations) >= BATCH_SIZE:
            result = execute_batch(
                collection,
                operations,
            )

            for key in (
                "matched",
                "modified",
                "upserted",
            ):
                totals[key] += result[key]

            print(
                f"Processed {totals['read']:,} documents..."
            )

            operations.clear()

    if operations:
        result = execute_batch(
            collection,
            operations,
        )

        for key in (
            "matched",
            "modified",
            "upserted",
        ):
            totals[key] += result[key]

    return totals


def build_taxonomy_hierarchy(
    taxonomy_records: Iterator[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one hierarchy document per domain."""

    hierarchy: dict[str, set[str]] = defaultdict(set)

    for record in taxonomy_records:
        domain = record.get("domain")
        subdomain = record.get("subdomain")

        if not domain:
            continue

        if subdomain:
            hierarchy[domain].add(subdomain)

    hierarchy_documents: list[dict[str, Any]] = []

    for domain in sorted(hierarchy):
        hierarchy_documents.append(
            {
                "domain": domain,
                "subdomains": sorted(hierarchy[domain]),
                "version": "prototype-v1",
                "active": True,
                "updated_at": datetime.now(UTC),
            }
        )

    return hierarchy_documents


def load_hierarchy(
    collection: Collection,
    documents: list[dict[str, Any]],
) -> dict[str, int]:
    """Load taxonomy hierarchy documents using upserts."""

    operations = [
        UpdateOne(
            {
                "domain": document["domain"],
            },
            {
                "$set": document,
                "$setOnInsert": {
                    "created_at": datetime.now(UTC),
                },
            },
            upsert=True,
        )
        for document in documents
    ]

    return execute_batch(
        collection,
        operations,
    )


def validate_database_counts(
    knowledge_collection: Collection,
    hierarchy_collection: Collection,
) -> dict[str, Any]:
    """Validate loaded knowledge and hierarchy records."""

    taxonomy_count = knowledge_collection.count_documents(
        {
            "chunk_type": "taxonomy",
        }
    )

    hipo_count = knowledge_collection.count_documents(
        {
            "chunk_type": "hipo_policy",
        }
    )

    severity_count = knowledge_collection.count_documents(
        {
            "chunk_type": "severity_policy",
        }
    )

    rca_count = knowledge_collection.count_documents(
        {
            "chunk_type": "rca_guidance",
        }
    )

    legacy_policy_count = knowledge_collection.count_documents(
        {
            "chunk_type": "policy",
        }
    )

    total_knowledge = knowledge_collection.count_documents({})

    hierarchy_count = hierarchy_collection.count_documents({})

    missing_search_text = knowledge_collection.count_documents(
        {
            "$or": [
                {
                    "search_text": {
                        "$exists": False,
                    }
                },
                {
                    "search_text": None,
                },
                {
                    "search_text": "",
                },
            ]
        }
    )

    missing_chunk_id = knowledge_collection.count_documents(
        {
            "$or": [
                {
                    "chunk_id": {
                        "$exists": False,
                    }
                },
                {
                    "chunk_id": None,
                },
                {
                    "chunk_id": "",
                },
            ]
        }
    )

    return {
        "taxonomy_count": taxonomy_count,
        "hipo_policy_count": hipo_count,
        "severity_policy_count": severity_count,
        "rca_guidance_count": rca_count,
        "legacy_policy_count": legacy_policy_count,
        "total_knowledge_count": total_knowledge,
        "taxonomy_hierarchy_count": hierarchy_count,
        "missing_search_text_count": missing_search_text,
        "missing_chunk_id_count": missing_chunk_id,
    }


def main() -> None:
    """Load all Stage 6 knowledge into MongoDB."""

    client: MongoClient | None = None

    try:
        client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=20_000,
            connectTimeoutMS=20_000,
        )

        client.admin.command("ping")

        database = client[settings.mongodb_database]

        knowledge_collection = database[
            "knowledge_chunks"
        ]

        hierarchy_collection = database[
            "taxonomy_hierarchy"
        ]

        print("\nLoading taxonomy records...")

        taxonomy_result = load_documents(
            knowledge_collection,
            (
                transform_taxonomy_record(record)
                for record in read_jsonl(TAXONOMY_FILE)
            ),
        )

        print("\nLoading policy chunks...")

        policy_result = load_documents(
            knowledge_collection,
            (
                transform_policy_chunk(chunk)
                for chunk in read_jsonl(POLICY_FILE)
            ),
        )

        print("\nBuilding taxonomy hierarchy...")

        hierarchy_documents = build_taxonomy_hierarchy(
            read_jsonl(TAXONOMY_FILE)
        )

        hierarchy_result = load_hierarchy(
            hierarchy_collection,
            hierarchy_documents,
        )

        validation = validate_database_counts(
            knowledge_collection,
            hierarchy_collection,
        )

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "database": settings.mongodb_database,
            "source_files": {
                "taxonomy": str(TAXONOMY_FILE),
                "policies": str(POLICY_FILE),
            },
            "taxonomy_load": taxonomy_result,
            "policy_load": policy_result,
            "hierarchy_load": {
                **hierarchy_result,
                "domains_prepared": len(
                    hierarchy_documents
                ),
            },
            "validation": validation,
        }

        REPORT_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        REPORT_FILE.write_text(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

        print("\nKNOWLEDGE LOADING SUMMARY")
        print("=" * 60)

        print(
            f"Taxonomy records read: "
            f"{taxonomy_result['read']:,}"
        )

        print(
            f"Policy chunks read: "
            f"{policy_result['read']:,}"
        )

        print(
            f"Domains prepared: "
            f"{len(hierarchy_documents):,}"
        )

        print(
            f"Taxonomy documents: "
            f"{validation['taxonomy_count']:,}"
        )

        print(
            f"HIPO policy documents: "
            f"{validation['hipo_policy_count']:,}"
        )

        print(
            f"Severity policy documents: "
            f"{validation['severity_policy_count']:,}"
        )

        print(
            f"RCA guidance documents: "
            f"{validation['rca_guidance_count']:,}"
        )

        print(
            f"Legacy policy documents: "
            f"{validation['legacy_policy_count']:,}"
        )

        print(
            f"Knowledge documents in MongoDB: "
            f"{validation['total_knowledge_count']:,}"
        )

        print(
            f"Hierarchy documents in MongoDB: "
            f"{validation['taxonomy_hierarchy_count']:,}"
        )

        print(
            f"Missing chunk IDs: "
            f"{validation['missing_chunk_id_count']}"
        )

        print(
            f"Missing search text: "
            f"{validation['missing_search_text_count']}"
        )

        print(
            f"Report: {REPORT_FILE}"
        )

    except BulkWriteError as exc:
        print("MongoDB bulk write failed.")
        print(exc.details)
        raise SystemExit(1) from exc

    except PyMongoError as exc:
        print(
            f"MongoDB loading failed: {exc}"
        )
        raise SystemExit(1) from exc

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
