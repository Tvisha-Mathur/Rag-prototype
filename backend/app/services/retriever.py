"""Purpose: Implements the retriever application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from sentence_transformers import SentenceTransformer

from backend.app.config import settings


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
INDEX_NAME = "knowledge_vector_index"
EMBEDDING_DIMENSIONS = 384

ALLOWED_CHUNK_TYPES = {
    "taxonomy",
    "severity_policy",
    "hipo_policy",
    "rca_guidance",
    "historical_incident",
}
ALLOWED_HIPO_PARAMETERS = {
    "safety", "asset_damage", "business_continuity",
    "reputational_impact", "vip_safety", "likelihood",
}


class RetrieverService:
    """Retrieve semantically relevant knowledge from MongoDB Atlas."""

    def __init__(self) -> None:
        print(f"Loading retriever model: {MODEL_NAME}")

        self.model = SentenceTransformer(
            MODEL_NAME,
            local_files_only=settings.embedding_local_files_only,
        )
        self._cache_lock = RLock()
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._retrieval_cache: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()

        self.client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=20_000,
            connectTimeoutMS=20_000,
        )

        self.client.admin.command("ping")

        database = self.client[
            settings.mongodb_database
        ]

        self.collection: Collection = database[
            "knowledge_chunks"
        ]

        print("Retriever service initialized.")

    def close(self) -> None:
        """Close the MongoDB client."""

        self.client.close()

    def clear_retrieval_cache(self) -> None:
        """Make newly added or changed knowledge visible immediately."""
        with self._cache_lock:
            self._retrieval_cache.clear()

    def create_query_embedding(
        self,
        query: str,
    ) -> list[float]:
        """Convert a user query into a normalized embedding."""

        cleaned_query = query.strip()

        if not cleaned_query:
            raise ValueError(
                "The retrieval query cannot be empty."
            )

        cache_key = cleaned_query.casefold()
        with self._cache_lock:
            cached = self._embedding_cache.get(cache_key)
            if cached is not None:
                self._embedding_cache.move_to_end(cache_key)
                return list(cached)

        embedding = self.model.encode(
            cleaned_query,
            normalize_embeddings=True,
        ).tolist()

        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Expected {EMBEDDING_DIMENSIONS} dimensions, "
                f"received {len(embedding)}."
            )

        with self._cache_lock:
            self._embedding_cache[cache_key] = embedding
            self._embedding_cache.move_to_end(cache_key)
            while len(self._embedding_cache) > 256:
                self._embedding_cache.popitem(last=False)
        return list(embedding)

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 5,
        num_candidates: int = 100,
        chunk_type: str | None = None,
        domain: str | None = None,
        parameter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve semantically similar knowledge documents."""

        result_cache_key = (
            query.strip().casefold(), limit, num_candidates, chunk_type, domain,
            parameter,
        )
        with self._cache_lock:
            cached_results = self._retrieval_cache.get(result_cache_key)
            if cached_results is not None:
                self._retrieval_cache.move_to_end(result_cache_key)
                return [dict(item) for item in cached_results]

        if limit < 1 or limit > 100:
            raise ValueError(
                "limit must be between 1 and 100."
            )

        if num_candidates < limit:
            raise ValueError(
                "num_candidates must be greater than or "
                "equal to limit."
            )

        if (
            chunk_type
            and chunk_type not in ALLOWED_CHUNK_TYPES
        ):
            raise ValueError(
                f"Unsupported chunk_type: {chunk_type}"
            )

        if parameter and parameter not in ALLOWED_HIPO_PARAMETERS:
            raise ValueError(f"Unsupported HIPO parameter: {parameter}")

        query_vector = self.create_query_embedding(
            query
        )

        vector_filter: dict[str, Any] = {
            "active": True,
        }

        if chunk_type:
            vector_filter["chunk_type"] = chunk_type

        if domain:
            vector_filter["domain"] = domain

        if parameter:
            vector_filter["parameter"] = parameter

        pipeline = [
            {
                "$vectorSearch": {
                    "index": INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": num_candidates,
                    "limit": limit,
                    "filter": vector_filter,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "chunk_id": 1,
                    "chunk_type": 1,
                    "document_type": 1,
                    "source": 1,
                    "source_file": 1,
                    "source_section": 1,
                    "search_text": 1,

                    "domain": 1,
                    "subdomain": 1,

                    "incident_no": 1,
                    "incident_summary": 1,
                    "severity": 1,
                    "impact": 1,
                    "safety_impact": 1,
                    "business_continuity": 1,
                    "damage_to_assets": 1,
                    "reputational_impact": 1,
                    "likelihood_of_more_severe_outcome": 1,
                    "vip_safety": 1,
                    "environmental_impact": 1,
                    "immediate_control_measures": 1,
                    "hipo_classification": 1,
                    "hipo_classification_reason": 1,
                    "verified": 1,
                    "hazard": 1,
                    "exposure": 1,
                    "actual_outcome": 1,
                    "energy_source": 1,
                    "people_exposed": 1,
                    "critical_controls": 1,
                    "credible_worst_case": 1,

                    "hazard_identified": 1,
                    "risk_identified": 1,
                    "risk_explanation": 1,
                    "control_measures": 1,
                    "section": 1,
                    "active": 1,
                    "reference_only": 1,
                    "authority_level": 1,
                    "knowledge_type": 1,
                    "document": 1,
                    "parameter": 1,
                    "score_value": "$score",
                    "tags": 1,
                    "priority": 1,
                    "rule_type": 1,

                    "score": {
                        "$meta": "vectorSearchScore"
                    },
                }
            },
        ]

        try:
            results = list(
                self.collection.aggregate(pipeline)
            )
            with self._cache_lock:
                self._retrieval_cache[result_cache_key] = results
                self._retrieval_cache.move_to_end(result_cache_key)
                while len(self._retrieval_cache) > 128:
                    self._retrieval_cache.popitem(last=False)
            return [dict(item) for item in results]

        except PyMongoError as exc:
            raise RuntimeError(
                f"MongoDB vector retrieval failed: {exc}"
            ) from exc

    def retrieve_historical_incidents(
        self,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Retrieve similar historical incident references."""

        return self.retrieve(
            query=query,
            chunk_type="historical_incident",
            limit=limit,
            num_candidates=200,
        )

    def retrieve_incident_context(
        self,
        incident_text: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve separate context groups for incident analysis."""

        return {
            "taxonomy": self.retrieve(
                incident_text,
                chunk_type="taxonomy",
                limit=5,
                num_candidates=150,
            ),
            "hipo_policy": self.retrieve(
                incident_text,
                chunk_type="hipo_policy",
                limit=3,
                num_candidates=50,
            ),
            "severity_policy": self.retrieve(
                incident_text,
                chunk_type="severity_policy",
                limit=3,
                num_candidates=50,
            ),
            "rca_guidance": self.retrieve(
                incident_text,
                chunk_type="rca_guidance",
                limit=3,
                num_candidates=50,
            ),
            "historical_incidents": (
                self.retrieve_historical_incidents(
                    incident_text,
                    limit=5,
                )
            ),
        }
