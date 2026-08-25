"""Purpose: Provides the test vector search command-line utility.

Used by: Run manually or via python -m backend.scripts.test_vector_search.
"""

from __future__ import annotations

from pprint import pprint

from pymongo import MongoClient
from sentence_transformers import SentenceTransformer

from backend.app.config import settings


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
INDEX_NAME = "knowledge_vector_index"
EMBEDDING_DIMENSIONS = 384


def main() -> None:
    query_text = (
        "A guest slipped on a wet floor near the hotel lobby "
        "and suffered an injury."
    )

    print(f"Loading model: {MODEL_NAME}")

    model = SentenceTransformer(MODEL_NAME)

    query_vector = model.encode(
        query_text,
        normalize_embeddings=True,
    ).tolist()

    if len(query_vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Expected {EMBEDDING_DIMENSIONS} dimensions, "
            f"received {len(query_vector)}"
        )

    client = MongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=20_000,
        connectTimeoutMS=20_000,
    )

    try:
        client.admin.command("ping")

        database = client[settings.mongodb_database]
        collection = database["knowledge_chunks"]

        pipeline = [
            {
                "$vectorSearch": {
                    "index": INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": 100,
                    "limit": 5,
                    "filter": {
                        "active": True,
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "chunk_id": 1,
                    "chunk_type": 1,
                    "document_type": 1,
                    "domain": 1,
                    "subdomain": 1,
                    "section": 1,
                    "search_text": 1,
                    "score": {
                        "$meta": "vectorSearchScore",
                    },
                }
            },
        ]

        results = list(
            collection.aggregate(pipeline)
        )

        print("\nQUERY")
        print("=" * 60)
        print(query_text)

        print("\nVECTOR SEARCH RESULTS")
        print("=" * 60)

        if not results:
            print("No results returned.")
            return

        for position, result in enumerate(
            results,
            start=1,
        ):
            print(f"\nResult {position}")
            print("-" * 60)
            pprint(result)

    finally:
        client.close()


if __name__ == "__main__":
    main()