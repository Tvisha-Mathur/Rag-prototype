"""Purpose: Provides the evaluate accuracy command-line utility.

Used by: Run manually or via python -m backend.scripts.evaluate_accuracy.
"""

from __future__ import annotations

import argparse
import json

from pymongo import MongoClient

from backend.app.config import settings
from backend.app.services.accuracy_evaluator import AccuracyEvaluator


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate accuracy from verified MongoDB expert reviews."
    )
    parser.add_argument("--model")
    parser.add_argument("--rule-version")
    args = parser.parse_args()

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=20_000)
    try:
        client.admin.command("ping")
        evaluator = AccuracyEvaluator(client[settings.mongodb_database])
        evaluator.ensure_indexes()
        result = evaluator.run(model=args.model, rule_version=args.rule_version)
        print(json.dumps(result, default=str, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
