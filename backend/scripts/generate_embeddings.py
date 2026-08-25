"""Purpose: Provides the generate embeddings command-line utility.

Used by: Run manually or via python -m backend.scripts.generate_embeddings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import BulkWriteError, PyMongoError
from sentence_transformers import SentenceTransformer

from backend.app.config import settings


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384

READ_BATCH_SIZE = 100
WRITE_BATCH_SIZE = 100

# Keep this at 10 for the first test run.
# Change it to None only after the test succeeds.
TEST_LIMIT: int | None = None


def pending_embedding_filter() -> dict[str, Any]:
    """Return the MongoDB filter for documents needing embeddings."""

    return {
        "active": True,
        "$or": [
            {"embedding": {"$exists": False}},
            {"embedding": None},
            {"embedding": []},
        ],
        "search_text": {
            "$exists": True,
            "$nin": [None, ""],
        },
    }


def get_documents_without_embeddings(
    collection: Collection,
) -> Any:
    """Return active knowledge documents without embeddings."""

    cursor = collection.find(
        pending_embedding_filter(),
        {
            "_id": 1,
            "chunk_id": 1,
            "search_text": 1,
        },
        batch_size=READ_BATCH_SIZE,
    )

    if TEST_LIMIT is not None:
        cursor = cursor.limit(TEST_LIMIT)

    return cursor


def write_embedding_batch(
    collection: Collection,
    documents: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> dict[str, int]:
    """Write a batch of generated embeddings to MongoDB."""

    operations: list[UpdateOne] = []

    for document, embedding in zip(
        documents,
        embeddings,
        strict=True,
    ):
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding dimension mismatch for "
                f"{document.get('chunk_id')}: "
                f"expected {EMBEDDING_DIMENSIONS}, "
                f"received {len(embedding)}"
            )

        now = datetime.now(UTC)

        operations.append(
            UpdateOne(
                {
                    "_id": document["_id"],
                },
                {
                    "$set": {
                        "embedding": embedding,
                        "embedding_metadata": {
                            "model": MODEL_NAME,
                            "dimensions": EMBEDDING_DIMENSIONS,
                            "normalized": True,
                            "generated_at": now,
                        },
                        "updated_at": now,
                    }
                },
            )
        )

    if not operations:
        return {
            "matched": 0,
            "modified": 0,
        }

    result = collection.bulk_write(
        operations,
        ordered=False,
    )

    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
    }


def encode_and_write_batch(
    model: SentenceTransformer,
    collection: Collection,
    document_batch: list[dict[str, Any]],
) -> dict[str, int]:
    """Generate and store embeddings for one document batch."""

    texts = [
        str(document["search_text"])
        for document in document_batch
    ]

    vectors = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        normalize_embeddings=True,
    )

    vector_lists = [
        vector.tolist()
        for vector in vectors
    ]

    return write_embedding_batch(
        collection,
        document_batch,
        vector_lists,
    )


def validate_embedding_counts(
    collection: Collection,
) -> dict[str, int]:
    """Return embedding-related MongoDB counts."""

    total_documents = collection.count_documents({})

    embedded_documents = collection.count_documents(
        {
            "embedding": {
                "$type": "array",
                "$ne": [],
            }
        }
    )

    missing_embeddings = collection.count_documents(
        {
            "$or": [
                {"embedding": {"$exists": False}},
                {"embedding": None},
                {"embedding": []},
            ]
        }
    )

    wrong_dimensions = collection.count_documents(
        {
            "embedding": {
                "$type": "array",
            },
            "$expr": {
                "$ne": [
                    {"$size": "$embedding"},
                    EMBEDDING_DIMENSIONS,
                ]
            },
        }
    )

    return {
        "total_documents": total_documents,
        "embedded_documents": embedded_documents,
        "missing_embeddings": missing_embeddings,
        "wrong_dimension_embeddings": wrong_dimensions,
    }


def main() -> None:
    """Generate and store embeddings for knowledge documents."""

    client: MongoClient | None = None

    try:
        print(f"Loading embedding model: {MODEL_NAME}")

        model = SentenceTransformer(MODEL_NAME)

        print("Embedding model loaded.")

        client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=20_000,
            connectTimeoutMS=20_000,
        )

        client.admin.command("ping")

        database = client[settings.mongodb_database]
        collection = database["knowledge_chunks"]

        pending_count = collection.count_documents(
            pending_embedding_filter()
        )

        print(
            f"Documents requiring embeddings: "
            f"{pending_count:,}"
        )

        if TEST_LIMIT is None:
            run_target = pending_count
            print("Run mode: full embedding generation")
        else:
            run_target = min(
                TEST_LIMIT,
                pending_count,
            )
            print(
                f"Run mode: test"
            )
            print(
                f"Test limit: {TEST_LIMIT}"
            )

        if pending_count == 0:
            print("No embeddings need to be generated.")

            validation = validate_embedding_counts(
                collection
            )

            print("\nEMBEDDING SUMMARY")
            print("=" * 60)
            print(
                f"Total documents: "
                f"{validation['total_documents']:,}"
            )
            print(
                f"Embedded documents: "
                f"{validation['embedded_documents']:,}"
            )
            print(
                f"Missing embeddings: "
                f"{validation['missing_embeddings']:,}"
            )
            print(
                f"Wrong dimensions: "
                f"{validation['wrong_dimension_embeddings']:,}"
            )
            return

        cursor = get_documents_without_embeddings(
            collection
        )

        document_batch: list[dict[str, Any]] = []

        processed = 0
        matched = 0
        modified = 0

        for document in cursor:
            document_batch.append(document)

            if len(document_batch) < WRITE_BATCH_SIZE:
                continue

            result = encode_and_write_batch(
                model,
                collection,
                document_batch,
            )

            processed += len(document_batch)
            matched += result["matched"]
            modified += result["modified"]

            print(
                f"Embedded {processed:,} / "
                f"{run_target:,} documents"
            )

            document_batch.clear()

        if document_batch:
            result = encode_and_write_batch(
                model,
                collection,
                document_batch,
            )

            processed += len(document_batch)
            matched += result["matched"]
            modified += result["modified"]

            print(
                f"Embedded {processed:,} / "
                f"{run_target:,} documents"
            )

        validation = validate_embedding_counts(
            collection
        )

        print("\nEMBEDDING SUMMARY")
        print("=" * 60)
        print(f"Processed: {processed:,}")
        print(f"Matched: {matched:,}")
        print(f"Modified: {modified:,}")
        print(
            f"Total documents: "
            f"{validation['total_documents']:,}"
        )
        print(
            f"Embedded documents: "
            f"{validation['embedded_documents']:,}"
        )
        print(
            f"Missing embeddings: "
            f"{validation['missing_embeddings']:,}"
        )
        print(
            f"Wrong dimensions: "
            f"{validation['wrong_dimension_embeddings']:,}"
        )

    except BulkWriteError as exc:
        print("MongoDB embedding update failed.")
        print(exc.details)
        raise SystemExit(1) from exc

    except PyMongoError as exc:
        print(
            f"MongoDB connection or update failed: {exc}"
        )
        raise SystemExit(1) from exc

    except ValueError as exc:
        print(
            f"Embedding validation failed: {exc}"
        )
        raise SystemExit(1) from exc

    except KeyboardInterrupt:
        print(
            "\nEmbedding generation was interrupted. "
            "Run the command again to continue."
        )
        raise SystemExit(1)

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()