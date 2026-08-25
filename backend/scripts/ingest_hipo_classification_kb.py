"""Purpose: Provides the ingest hipo classification kb command-line utility.

Used by: Run manually or via python -m backend.scripts.ingest_hipo_classification_kb.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pymongo import MongoClient, UpdateOne
from sentence_transformers import SentenceTransformer

from backend.app.config import settings
from backend.app.services.retriever import EMBEDDING_DIMENSIONS, MODEL_NAME
from ingestion.hipo_classification_kb import SOURCE_FILE_NAME, build_hipo_kb_chunks


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = PROJECT_ROOT / "data" / "raw" / SOURCE_FILE_NAME


def main() -> None:
    chunks = build_hipo_kb_chunks(PDF_PATH)
    model = SentenceTransformer(
        MODEL_NAME,
        local_files_only=settings.embedding_local_files_only,
    )
    vectors = model.encode(
        [chunk["search_text"] for chunk in chunks],
        batch_size=32,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    now = datetime.now(UTC)
    operations = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        embedding = vector.tolist()
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"Invalid embedding dimensions for {chunk['chunk_id']}")
        document = {
            **chunk,
            "embedding": embedding,
            "embedding_metadata": {
                "model": MODEL_NAME,
                "dimensions": EMBEDDING_DIMENSIONS,
                "normalized": True,
                "generated_at": now,
            },
            "updated_at": now,
        }
        operations.append(UpdateOne(
            {"chunk_id": chunk["chunk_id"]},
            {"$set": document, "$setOnInsert": {"created_at": now}},
            upsert=True,
        ))

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000)
    try:
        client.admin.command("ping")
        collection = client[settings.mongodb_database]["knowledge_chunks"]
        result = collection.bulk_write(operations, ordered=False)
        active_ids = [chunk["chunk_id"] for chunk in chunks]
        retired = collection.update_many(
            {"document_name": SOURCE_FILE_NAME, "chunk_id": {"$nin": active_ids}, "active": True},
            {"$set": {"active": False, "superseded_at": now}},
        )
        print({
            "prepared": len(chunks), "matched": result.matched_count,
            "modified": result.modified_count, "upserted": len(result.upserted_ids),
            "retired": retired.modified_count,
        })
    finally:
        client.close()


if __name__ == "__main__":
    main()
