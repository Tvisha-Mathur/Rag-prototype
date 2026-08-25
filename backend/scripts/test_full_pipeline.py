"""Purpose: Provides the test full pipeline command-line utility.

Used by: Run manually or via python -m backend.scripts.test_full_pipeline.
"""

from __future__ import annotations

import json

from backend.app.services.incident_analyzer import IncidentAnalyzer
from backend.app.services.retriever import RetrieverService


def main() -> None:
    print("\nEnter the incident narrative:")
    incident_text = input("> ").strip()

    if not incident_text:
        print("Incident narrative cannot be empty.")
        return

    retriever = RetrieverService()

    try:
        analyzer = IncidentAnalyzer(retriever)

        print("\nAnalyzing incident...\n")

        result = analyzer.analyze(incident_text)

        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )

    except Exception as exc:
        print(f"\nAnalysis failed: {exc}")

    finally:
        retriever.close()


if __name__ == "__main__":
    main()