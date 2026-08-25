"""Purpose: Provides the test mongodb connection command-line utility.

Used by: Run manually or via python -m backend.scripts.test_mongodb_connection.
"""

from __future__ import annotations

from pymongo import MongoClient
from pymongo.errors import (
    ConfigurationError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from backend.app.config import settings


def main() -> None:
    client: MongoClient | None = None

    try:
        client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
        )

        result = client.admin.command("ping")

        if result.get("ok") != 1.0:
            raise RuntimeError("MongoDB ping was unsuccessful.")

        database = client[settings.mongodb_database]

        print("MongoDB connection test: PASS")
        print(f"Database: {database.name}")

    except ServerSelectionTimeoutError as exc:
        print("MongoDB connection test: FAIL")
        print(
            "Atlas could not be reached. Check your internet connection, "
            "Atlas Network Access IP entry, and cluster address."
        )
        raise SystemExit(1) from exc

    except OperationFailure as exc:
        print("MongoDB connection test: FAIL")
        print(
            "Authentication failed. Check your MongoDB database username "
            "and password."
        )
        raise SystemExit(1) from exc

    except ConfigurationError as exc:
        print("MongoDB connection test: FAIL")
        print("The MongoDB connection string is invalid.")
        raise SystemExit(1) from exc

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
