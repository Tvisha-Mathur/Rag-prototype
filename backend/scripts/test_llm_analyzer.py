"""Purpose: Provides the test llm analyzer command-line utility.

Used by: Run manually or via python -m backend.scripts.test_llm_analyzer.
"""

from __future__ import annotations

from backend.app.services.llm_analyzer import LLMAnalyzer


def main() -> None:
    analyzer = LLMAnalyzer()

    incident_text = (
        "A falling object narrowly missed a guest. "
        "No injury occurred."
    )

    deterministic_result = {
        "classification": {
            "domain": "Guest-Related Incidents",
            "subdomain": "Other Guest Safety Incident",
            "confidence": 0.45,
            "status": "fallback_classification",
            "is_fallback": True,
        },
        "mechanism": {
            "primary_mechanism": "falling_object",
            "matched_term": "falling object",
        },
        "severity": {
            "level": "negligible",
            "status": "assessed",
            "matched_evidence": "no injury",
            "reason": (
                "The narrative explicitly states that "
                "no actual injury occurred."
            ),
        },
        "hipo": {
            "status": "potential_hipo",
            "assessment_status": "assessed",
            "matched_evidence": "falling object",
            "reason": (
                "A falling object could potentially "
                "cause severe or fatal harm."
            ),
        },
        "taxonomy_evidence": [],
        "policy_evidence": {
            "hipo": [
                {
                    "section": "Falling-object example",
                    "search_text": (
                        "A falling object could have caused "
                        "a fatality or major injury."
                    ),
                    "score": 0.80,
                }
            ],
            "severity": [],
            "rca": [
                {
                    "section": "Investigation principles",
                    "search_text": (
                        "Consider equipment failure, procedures, "
                        "human factors, environmental factors, "
                        "and historical context."
                    ),
                    "score": 0.65,
                }
            ],
        },
    }

    result = analyzer.generate_analysis(
        incident_text=incident_text,
        deterministic_result=deterministic_result,
    )

    print(
        result.model_dump_json(
            indent=2
        )
    )


if __name__ == "__main__":
    main()