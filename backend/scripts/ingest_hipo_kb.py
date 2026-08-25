"""Purpose: Provides the ingest hipo kb command-line utility.

Used by: Run manually or via python -m backend.scripts.ingest_hipo_kb.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from pymongo import MongoClient, UpdateOne
from sentence_transformers import SentenceTransformer

from backend.app.config import settings
from backend.app.services.retriever import EMBEDDING_DIMENSIONS, MODEL_NAME
from backend.scripts.load_knowledge import transform_policy_chunk
from ingestion.policy_chunking import (
    RAW_DIRECTORY,
    _build_hipo_chunks,
    extract_pdf_pages,
    resolve_source_path,
)


SOURCE_DOCUMENT = "hipo_classification.pdf"


def build_documents() -> list[dict[str, Any]]:
    """Extract the PDF and return embedding-ready semantic HIPO documents."""
    source_path = resolve_source_path(SOURCE_DOCUMENT, RAW_DIRECTORY)
    if source_path is None:
        raise FileNotFoundError("HIPO and Near Miss Classification PDF was not found.")
    pages = extract_pdf_pages(source_path)
    chunks = _build_hipo_chunks(pages)
    if len(chunks) < 35:
        raise ValueError(f"Expected at least 35 semantic HIPO chunks, received {len(chunks)}.")
    return [transform_policy_chunk(chunk) for chunk in chunks]


def embed_documents(
    documents: list[dict[str, Any]],
    model: SentenceTransformer,
) -> None:
    vectors = model.encode(
        [str(document["search_text"]) for document in documents],
        batch_size=32,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    generated_at = datetime.now(UTC)
    for document, vector in zip(documents, vectors, strict=True):
        embedding = vector.tolist()
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"{document['chunk_id']} has {len(embedding)} dimensions; "
                f"expected {EMBEDDING_DIMENSIONS}."
            )
        document["embedding"] = embedding
        document["embedding_metadata"] = {
            "model": MODEL_NAME,
            "dimensions": EMBEDDING_DIMENSIONS,
            "normalized": True,
            "generated_at": generated_at,
        }


def upsert_documents(collection: Any, documents: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert semantic chunks and deactivate superseded coarse HIPO chunks."""
    now = datetime.now(UTC)
    chunk_ids = [document["chunk_id"] for document in documents]
    operations = [
        UpdateOne(
            {"chunk_id": document["chunk_id"]},
            {
                "$set": {**document, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        for document in documents
    ]
    result = collection.bulk_write(operations, ordered=False)
    retired = collection.update_many(
        {
            "chunk_type": "hipo_policy",
            "document_type": SOURCE_DOCUMENT,
            "chunk_id": {"$nin": chunk_ids},
            "active": True,
        },
        {"$set": {"active": False, "superseded_at": now}},
    )
    return {
        "prepared": len(documents),
        "matched": result.matched_count,
        "modified": result.modified_count,
        "upserted": len(result.upserted_ids),
        "retired_legacy_chunks": retired.modified_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest rule-level HIPO PDF chunks into the existing vector collection."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write embedded chunks to MongoDB. Without this flag, only validate extraction.",
    )
    args = parser.parse_args()

    documents = build_documents()
    print(f"Validated {len(documents)} semantic HIPO chunks.")
    if not args.apply:
        print("Dry run only. Re-run with --apply to embed and upsert them.")
        return

    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    embed_documents(documents, model)
    client = MongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=20_000,
        connectTimeoutMS=20_000,
    )
    try:
        client.admin.command("ping")
        collection = client[settings.mongodb_database]["knowledge_chunks"]
        result = upsert_documents(collection, documents)
        print(f"HIPO ingestion complete: {result}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
