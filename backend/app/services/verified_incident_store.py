"""Purpose: Implements the verified incident store application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from backend.app.services.accuracy_evaluator import SCORE_FIELDS, normalize_result
from backend.app.services.retriever import EMBEDDING_DIMENSIONS, MODEL_NAME, RetrieverService


def build_verified_search_text(incident_text: str, review: dict[str, Any]) -> str:
    """Build the text embedded for a reviewed incident."""
    expected = normalize_result(review)
    labels = {
        "safety_impact": "Safety impact",
        "damage_to_assets": "Asset impact",
        "business_continuity": "Business continuity",
        "reputational_impact": "Reputational impact",
        "vip_safety_impact": "VIP safety",
        "likelihood_of_more_severe_outcomes": "Likelihood",
    }
    lines = [
        f"Verified incident: {incident_text.strip()}",
        f"Domain: {expected['domain']}",
        f"Subdomain: {expected['subdomain']}",
    ]
    lines.extend(f"{labels[field]}: {expected[field]}" for field in SCORE_FIELDS)
    lines.append(f"HIPO classification: {expected['hipo_classification']}")
    return "\n".join(lines)


class VerifiedIncidentStore:
    """Promote expert-reviewed responses into retrievable RAG examples."""

    def __init__(self, retriever: RetrieverService) -> None:
        self.retriever = retriever
        self.database = retriever.collection.database

    def promote(self, query_id: str) -> dict[str, Any]:
        response = self.database["incident_responses"].find_one(
            {"query_id": query_id, "review_status": "verified"},
            {"expert_review": 1, "reviewer": 1, "reviewed_at": 1},
        )
        query = self.database["incident_queries"].find_one(
            {"query_id": query_id}, {"incident_text": 1}
        )
        if response is None or not response.get("expert_review"):
            raise ValueError("A verified expert review is required before promotion.")
        incident_text = str((query or {}).get("incident_text") or "").strip()
        if not incident_text:
            raise ValueError("The original incident narrative was not found.")

        expected = normalize_result(response["expert_review"])
        search_text = build_verified_search_text(incident_text, expected)
        embedding = self.retriever.create_query_embedding(search_text)
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"Expected {EMBEDDING_DIMENSIONS} embedding dimensions.")

        now = datetime.now(UTC)
        chunk_id = "verified_incident_" + hashlib.sha256(
            f"expert_review|{query_id}".encode()
        ).hexdigest()[:24]
        document = {
            "chunk_id": chunk_id,
            "chunk_type": "historical_incident",
            "document_type": "verified_incident_reference",
            "incident_no": query_id,
            "incident_summary": incident_text,
            "domain": expected["domain"],
            "subdomain": expected["subdomain"],
            "safety_impact": expected["safety_impact"],
            "damage_to_assets": expected["damage_to_assets"],
            "business_continuity": expected["business_continuity"],
            "reputational_impact": expected["reputational_impact"],
            "vip_safety": expected["vip_safety_impact"],
            "likelihood_of_more_severe_outcome": expected[
                "likelihood_of_more_severe_outcomes"
            ],
            "hipo_classification": expected["hipo_classification"],
            "source": "expert_review",
            "source_file": "incident_responses",
            "source_section": query_id,
            "source_query_id": query_id,
            "search_text": search_text,
            "embedding": embedding,
            "embedding_metadata": {
                "model": MODEL_NAME,
                "dimensions": EMBEDDING_DIMENSIONS,
                "normalized": True,
                "generated_at": now,
            },
            "verified": True,
            "reference_only": True,
            "authority_level": "verified_example",
            "active": True,
            "reviewer": response.get("reviewer"),
            "reviewed_at": response.get("reviewed_at"),
            "updated_at": now,
        }
        self.retriever.collection.update_one(
            {"chunk_id": chunk_id},
            {"$set": document, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        self.retriever.clear_retrieval_cache()
        return {"chunk_id": chunk_id, "promoted": True, "updated_at": now}
