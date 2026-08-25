"""Purpose: Provides the create database command-line utility.

Used by: Run manually or via python -m backend.scripts.create_database.
"""

from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

from backend.app.config import settings


COLLECTIONS = (
    "knowledge_chunks",
    "taxonomy_hierarchy",
    "incident_queries",
    "incident_responses",
)


def create_collections(database) -> None:
    existing_collections = set(database.list_collection_names())

    for collection_name in COLLECTIONS:
        if collection_name in existing_collections:
            print(f"Collection already exists: {collection_name}")
        else:
            database.create_collection(collection_name)
            print(f"Created collection: {collection_name}")


def create_indexes(database) -> None:
    knowledge = database["knowledge_chunks"]
    hierarchy = database["taxonomy_hierarchy"]
    queries = database["incident_queries"]
    responses = database["incident_responses"]

    knowledge.create_index(
        [("chunk_id", ASCENDING)],
        unique=True,
        name="uq_chunk_id",
    )

    knowledge.create_index(
        [
            ("chunk_type", ASCENDING),
            ("active", ASCENDING),
        ],
        name="idx_chunk_type_active",
    )

    knowledge.create_index(
        [
            ("domain", ASCENDING),
            ("subdomain", ASCENDING),
        ],
        name="idx_domain_subdomain",
    )

    hierarchy.create_index(
        [("domain", ASCENDING)],
        unique=True,
        name="uq_domain",
    )

    queries.create_index(
        [("query_id", ASCENDING)],
        unique=True,
        name="uq_query_id",
    )

    queries.create_index(
        [("processing_status", ASCENDING)],
        name="idx_processing_status",
    )

    queries.create_index(
        [("created_at", DESCENDING)],
        name="idx_query_created_at",
    )

    responses.create_index(
        [("response_id", ASCENDING)],
        unique=True,
        name="uq_response_id",
    )

    responses.create_index(
        [("query_id", ASCENDING)],
        unique=True,
        name="uq_response_query_id",
    )

    responses.create_index(
        [("created_at", DESCENDING)],
        name="idx_response_created_at",
    )

    print("Indexes created or verified.")


def print_database_summary(database) -> None:
    print("\nDATABASE SUMMARY")
    print("=" * 60)
    print(f"Database: {settings.mongodb_database}")

    for collection_name in sorted(database.list_collection_names()):
        index_names = [
            index["name"]
            for index in database[collection_name].list_indexes()
        ]

        print(f"\nCollection: {collection_name}")
        print(f"Indexes: {index_names}")


def main() -> None:
    client: MongoClient | None = None

    try:
        client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
        )

        client.admin.command("ping")

        database = client[settings.mongodb_database]

        create_collections(database)
        create_indexes(database)
        print_database_summary(database)

        print("\nDatabase setup completed successfully.")

    except PyMongoError as exc:
        print(f"Database setup failed: {exc}")
        raise SystemExit(1) from exc

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()