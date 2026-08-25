"""Purpose: Implements the hybrid taxonomy classifier application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from sentence_transformers import CrossEncoder

from backend.app.config import settings


class HybridTaxonomyClassifier:
    """Hybrid taxonomy retrieval and constrained final selection."""

    RRF_K = 60
    VECTOR_LIMIT = 10
    BM25_LIMIT = 10
    RERANK_LIMIT = 10
    FINAL_CANDIDATES = 3
    DECISIVE_SCORE_GAP = 0.20
    FUSION_DOMINANCE_RATIO = 1.25

    def __init__(self, retriever: Any, llm_analyzer: Any) -> None:
        self.retriever = retriever
        self.llm_analyzer = llm_analyzer
        self._taxonomy_documents: list[dict[str, Any]] | None = None
        self._cross_encoder: CrossEncoder | None = None

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def _documents(self) -> list[dict[str, Any]]:
        if self._taxonomy_documents is None:
            self._taxonomy_documents = list(
                self.retriever.collection.find(
                    {"chunk_type": "taxonomy", "active": True},
                    {
                        "_id": 0,
                        "chunk_id": 1,
                        "domain": 1,
                        "subdomain": 1,
                        "search_text": 1,
                    },
                )
            )
        return self._taxonomy_documents

    def _bm25(self, query: str) -> list[dict[str, Any]]:
        documents = self._documents()
        query_tokens = list(dict.fromkeys(self._tokens(query)))
        if not documents or not query_tokens:
            return []

        tokenized = [self._tokens(str(doc.get("search_text") or "")) for doc in documents]
        average_length = sum(map(len, tokenized)) / max(len(tokenized), 1)
        document_frequency = {
            token: sum(token in set(tokens) for tokens in tokenized)
            for token in query_tokens
        }
        k1, b = 1.5, 0.75
        scored: list[tuple[float, dict[str, Any]]] = []
        for document, tokens in zip(documents, tokenized):
            frequencies = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies[token]
                if not frequency:
                    continue
                df = document_frequency[token]
                inverse_frequency = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                denominator = frequency + k1 * (1 - b + b * len(tokens) / max(average_length, 1))
                score += inverse_frequency * frequency * (k1 + 1) / denominator
            if score > 0:
                scored.append((score, document))

        scored.sort(key=lambda item: item[0], reverse=True)
        ranked = [{**document, "bm25_score": score} for score, document in scored]
        return self._collapse_by_pair(ranked)[: self.BM25_LIMIT]

    def _collapse_by_pair(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate severity-expanded rows into unique repository pairs."""
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        evidence: dict[tuple[str, str], list[str]] = defaultdict(list)

        for result in results:
            domain = str(result.get("domain") or "").strip()
            subdomain = str(result.get("subdomain") or "").strip()
            if not domain or not subdomain:
                continue
            pair = (domain, subdomain)
            if pair not in grouped:
                grouped[pair] = {
                    **result,
                    "candidate_id": f"{domain}\u241f{subdomain}",
                    "domain": domain,
                    "subdomain": subdomain,
                    "matching_repository_rows": 0,
                }
            grouped[pair]["matching_repository_rows"] += 1
            text = str(result.get("search_text") or "").strip()
            if text and text not in evidence[pair] and len(evidence[pair]) < 5:
                evidence[pair].append(text)

        collapsed: list[dict[str, Any]] = []
        for pair, candidate in grouped.items():
            candidate["search_text"] = "\n".join(evidence[pair])[:5000]
            candidate["repository_evidence"] = evidence[pair]
            collapsed.append(candidate)
        return collapsed

    def _rrf(
        self,
        *ranked_channels: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = defaultdict(float)
        documents: dict[str, dict[str, Any]] = {}
        for ranked_results in ranked_channels:
            for rank, document in enumerate(ranked_results, start=1):
                raw_candidate_id = document.get("candidate_id")
                if not raw_candidate_id:
                    continue
                candidate_id = str(raw_candidate_id)
                scores[candidate_id] += 1 / (self.RRF_K + rank)
                existing = documents.get(candidate_id, {})
                combined_evidence = list(dict.fromkeys(
                    existing.get("repository_evidence", [])
                    + document.get("repository_evidence", [])
                ))[:5]
                documents[candidate_id] = {
                    **existing,
                    **document,
                    "repository_evidence": combined_evidence,
                    "search_text": "\n".join(combined_evidence)[:5000],
                }

        ranked_ids = sorted(scores, key=scores.get, reverse=True)
        return [{**documents[chunk_id], "rrf_score": scores[chunk_id]} for chunk_id in ranked_ids]

    def _rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = candidates[: self.RERANK_LIMIT]
        if not candidates:
            return []
        try:
            if self._cross_encoder is None:
                self._cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            pairs = [(query, str(item.get("search_text") or "")) for item in candidates]
            scores = self._cross_encoder.predict(pairs)
        except Exception as exc:
            print(f"Cross-encoder reranking failed; using RRF order: {exc}")
            return candidates
        reranked = [
            {**candidate, "reranker_score": float(score)}
            for candidate, score in zip(candidates, scores)
        ]
        reranked.sort(key=lambda item: item["reranker_score"], reverse=True)
        return reranked

    def _retrieval_queries(
        self,
        incident_text: str,
        normalized_incident: str,
    ) -> list[str]:
        """Return a bounded agent plan, with the normalized query as a safe anchor."""
        queries = [normalized_incident]
        if not settings.agentic_retrieval_enabled:
            return queries
        try:
            planned = self.llm_analyzer.plan_retrieval_queries(
                incident_text,
                normalized_incident,
                max_queries=settings.agentic_retrieval_max_queries,
            )
        except Exception as exc:
            print(f"Retrieval planning failed; using normalized query: {exc}")
            return queries

        seen = {normalized_incident.strip().casefold()}
        for query in planned:
            key = query.strip().casefold()
            if key and key not in seen:
                queries.append(query.strip())
                seen.add(key)
        return queries[: 1 + max(1, settings.agentic_retrieval_max_queries)]

    def classify(self, incident_text: str, normalized_incident: str | None = None) -> dict[str, Any]:
        normalized = normalized_incident or self.llm_analyzer.normalize_incident_for_retrieval(incident_text)
        retrieval_queries = self._retrieval_queries(incident_text, normalized)
        vector_channels = [
            self._collapse_by_pair(self.retriever.retrieve(
                query,
                chunk_type="taxonomy",
                limit=self.VECTOR_LIMIT,
                num_candidates=200,
            ))
            for query in retrieval_queries
        ]
        keyword_channels = [self._bm25(query) for query in retrieval_queries]
        verified_examples = self._collapse_by_pair([
            item for item in self.retriever.retrieve(
                normalized,
                chunk_type="historical_incident",
                limit=8,
                num_candidates=150,
            )
            if item.get("verified") is True
        ])
        fused = self._rrf(*vector_channels, *keyword_channels, verified_examples)
        fusion_ratio = (
            float(fused[0].get("rrf_score", 0)) / max(float(fused[1].get("rrf_score", 0)), 1e-9)
            if len(fused) > 1 else float("inf")
        )
        reranker_skipped = fusion_ratio >= self.FUSION_DOMINANCE_RATIO
        ranked = fused if reranker_skipped else self._rerank(normalized, fused)
        top_candidates = ranked[: self.FINAL_CANDIDATES]
        if not top_candidates:
            return {
                "domain": None,
                "subdomain": None,
                "retrieval_queries": retrieval_queries,
                "top_candidates": [],
            }

        score_name = "reranker_score" if "reranker_score" in top_candidates[0] else "rrf_score"
        raw_gap = (
            float(top_candidates[0].get(score_name, 0)) - float(top_candidates[1].get(score_name, 0))
            if len(top_candidates) > 1 else 1.0
        )
        score_gap = (
            1 - float(top_candidates[1].get(score_name, 0)) / max(float(top_candidates[0].get(score_name, 0)), 1e-9)
            if score_name == "rrf_score" and len(top_candidates) > 1 else raw_gap
        )
        selected = str(top_candidates[0].get("candidate_id"))
        if score_gap <= self.DECISIVE_SCORE_GAP:
            selected = self.llm_analyzer.select_taxonomy_candidate(
                incident_text=incident_text,
                normalized_incident=normalized,
                candidates=top_candidates,
            )
        candidate = next(
            (item for item in top_candidates if item.get("candidate_id") == selected),
            top_candidates[0],
        )
        return {
            "domain": candidate.get("domain"),
            "subdomain": candidate.get("subdomain"),
            "selected_candidate_id": candidate.get("candidate_id"),
            "selected_chunk_id": candidate.get("chunk_id"),
            "selection_mode": "retrieval" if score_gap > self.DECISIVE_SCORE_GAP else "llm",
            "score_gap": score_gap,
            "reranker_skipped": reranker_skipped,
            "retrieval_queries": retrieval_queries,
            "top_candidates": top_candidates,
        }
