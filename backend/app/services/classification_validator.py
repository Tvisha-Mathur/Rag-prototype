"""Purpose: Implements the classification validator application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database


class ClassificationValidator:
    """
    Validate domain, subdomain, impact, and severity using
    approved MongoDB collections only.
    """

    VALID_SEVERITY_LEVELS = {
        1: "Negligible",
        2: "Minor",
        3: "Moderate",
        4: "Major",
        5: "Catastrophic",
    }

    def __init__(self, database: Database) -> None:
        # Active taxonomy knowledge chunks are the canonical source of valid
        # domain/subdomain pairs.  The hierarchy collection is a derived view
        # and must not authorize stale or generated labels.
        self.taxonomy_collection = database["knowledge_chunks"]

        self.severity_collection = database[
            "severity_impact_rules"
        ]

    def is_approved_taxonomy_pair(
        self,
        domain: str | None,
        subdomain: str | None,
    ) -> bool:
        """Return whether an exact pair exists in the active taxonomy source."""

        if not domain or not subdomain:
            return False
        return self.taxonomy_collection.find_one(
            {
                "chunk_type": "taxonomy",
                "domain": domain,
                "subdomain": subdomain,
                "active": True,
            },
            {"_id": 1},
        ) is not None

    def identify_impact(
        self,
        incident_text: str,
    ) -> dict[str, Any]:
        """
        Identify impact only from approved database keywords.
        """

        text = incident_text.lower()

        rules = self.severity_collection.find(
            {
                "active": True,
            },
            {
                "_id": 0,
            },
        )

        matches: list[dict[str, Any]] = []

        for rule in rules:
            keywords = rule.get(
                "impact_keywords",
                [],
            )

            for keyword in keywords:
                normalized_keyword = str(
                    keyword
                ).lower()

                if normalized_keyword in text:
                    matches.append(
                        {
                            "impact_type": rule.get(
                                "impact_type"
                            ),
                            "matched_evidence": keyword,
                            "severity": rule.get(
                                "severity"
                            ),
                            "severity_level": rule.get(
                                "severity_level"
                            ),
                        }
                    )
                    break

        if not matches:
            return {
                "impact_type": None,
                "matched_evidence": None,
                "status": "insufficient_information",
            }

        matches.sort(
            key=lambda item: int(
                item.get("severity_level") or 0
            ),
            reverse=True,
        )

        selected = matches[0]

        return {
            "impact_type": selected.get(
                "impact_type"
            ),
            "matched_evidence": selected.get(
                "matched_evidence"
            ),
            "status": "identified",
        }

    def validate(
        self,
        domain: str | None,
        subdomain: str | None,
        impact_type: str | None,
        matched_evidence: str | None,
    ) -> dict[str, Any]:
        errors: list[str] = []

        if not domain:
            errors.append(
                "No domain was retrieved from the approved taxonomy."
            )

        if not subdomain:
            errors.append(
                "No subdomain was retrieved from the approved taxonomy."
            )

        if not impact_type:
            errors.append(
                "No explicit impact was identified in the narrative."
            )

        if errors:
            return self._manual_review(
                errors=errors,
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        if not self.is_approved_taxonomy_pair(domain, subdomain):
            return self._manual_review(
                errors=[
                    "The domain and subdomain pair does not "
                    "exist in the approved taxonomy."
                ],
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        severity_rule = (
            self.severity_collection.find_one(
                {
                    "impact_type": impact_type,
                    "active": True,
                },
                {
                    "_id": 0,
                },
            )
        )

        if severity_rule is None:
            return self._manual_review(
                errors=[
                    "The impact has no approved severity mapping."
                ],
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        severity = str(
            severity_rule.get("severity") or ""
        )

        severity_level = severity_rule.get(
            "severity_level"
        )

        try:
            severity_level = int(severity_level)
        except (TypeError, ValueError):
            return self._manual_review(
                errors=[
                    "The severity level is invalid."
                ],
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        expected_name = self.VALID_SEVERITY_LEVELS.get(
            severity_level
        )

        if expected_name is None:
            return self._manual_review(
                errors=[
                    "The severity level must be between 1 and 5."
                ],
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        if severity.lower() != expected_name.lower():
            return self._manual_review(
                errors=[
                    "The severity name does not match the "
                    "approved severity level."
                ],
                impact_type=impact_type,
                matched_evidence=matched_evidence,
            )

        return {
            "domain": domain,
            "subdomain": subdomain,
            "impact": {
                "impact_type": impact_type,
                "matched_evidence": matched_evidence,
                "is_validated": True,
            },
            "severity": {
                "level": severity,
                "level_number": severity_level,
                "status": "validated",
                "source_document": severity_rule.get(
                    "source_document"
                ),
                "source_section": severity_rule.get(
                    "source_section"
                ),
            },
            "status": "validated",
            "domain_subdomain_valid": True,
            "impact_supported": True,
            "severity_supported": True,
            "requires_manual_review": False,
            "validation_errors": [],
        }

    def _manual_review(
        self,
        errors: list[str],
        impact_type: str | None,
        matched_evidence: str | None,
    ) -> dict[str, Any]:
        return {
            "domain": None,
            "subdomain": None,
            "impact": {
                "impact_type": impact_type,
                "matched_evidence": matched_evidence,
                "is_validated": False,
            },
            "severity": {
                "level": None,
                "level_number": None,
                "status": "not_assessed",
                "source_document": None,
                "source_section": None,
            },
            "status": "requires_manual_review",
            "domain_subdomain_valid": False,
            "impact_supported": False,
            "severity_supported": False,
            "requires_manual_review": True,
            "validation_errors": errors,
        }
