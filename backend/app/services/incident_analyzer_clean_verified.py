"""Purpose: Implements the incident analyzer clean verified application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from backend.app.services.classification_validator import ClassificationValidator
from backend.app.services.llm_analyzer import LLMAnalyzer
from backend.app.services.retriever import RetrieverService


class IncidentAnalyzer:
    """Analyze incidents using approved taxonomy, severity rules, and policy evidence."""

    def __init__(self, retriever: RetrieverService) -> None:
        self.retriever = retriever
        self.llm_analyzer = LLMAnalyzer()
        self.classification_validator = ClassificationValidator(
            self.retriever.collection.database
        )

    def detect_incident_mechanism(self, incident_text: str) -> dict[str, Any]:
        """Detect incident mechanisms from explicit phrases only."""
        text = incident_text.lower()

        mechanism_rules: dict[str, list[str]] = {
            "falling_object": [
                "falling object",
                "object fell",
                "object dropped",
                "struck by object",
                "hit by object",
                "overhead object",
                "falling luggage",
                "falling rack",
                "rack fell",
            ],
            "slip_trip_fall": [
                "slipped",
                "slip",
                "tripped",
                "trip",
                "wet floor",
                "slippery floor",
                "slippery surface",
                "fell while walking",
                "lost balance",
            ],
            "fire_explosion": [
                "fire",
                "smoke",
                "explosion",
                "burning",
                "flames",
            ],
            "electrical": [
                "electric shock",
                "electrocution",
                "electrical fault",
                "short circuit",
                "live wire",
            ],
            "vehicle_collision": [
                "vehicle collision",
                "car accident",
                "vehicle struck",
                "hit by vehicle",
                "road accident",
            ],
            "chemical_exposure": [
                "chemical exposure",
                "chemical spill",
                "toxic gas",
                "gas leak",
                "hazardous substance",
            ],
            "equipment_failure": [
                "equipment failed",
                "equipment failure",
                "machine failure",
                "component broke",
                "rack broke",
                "equipment broke",
            ],
        }

        detected: list[dict[str, str]] = []
        for mechanism, terms in mechanism_rules.items():
            for term in terms:
                if term in text:
                    detected.append(
                        {
                            "mechanism": mechanism,
                            "matched_term": term,
                        }
                    )
                    break

        if not detected:
            return {
                "primary_mechanism": "unknown",
                "matched_term": None,
                "all_detected": [],
            }

        return {
            "primary_mechanism": detected[0]["mechanism"],
            "matched_term": detected[0]["matched_term"],
            "all_detected": detected,
        }

    def get_mechanism_fallback(self, mechanism: str) -> dict[str, str] | None:
        """Return a candidate fallback; it is used only if it exists in approved taxonomy."""
        fallbacks: dict[str, dict[str, str]] = {
            "falling_object": {
                "domain": "Guest-Related Incidents",
                "subdomain": "Other Guest Safety Incident",
            },
            "fire_explosion": {
                "domain": "Fire and Life Safety",
                "subdomain": "Other Fire or Explosion Incident",
            },
            "electrical": {
                "domain": "Engineering and Maintenance",
                "subdomain": "Other Electrical Safety Incident",
            },
            "vehicle_collision": {
                "domain": "Transport and Road Safety",
                "subdomain": "Other Vehicle-Related Incident",
            },
            "chemical_exposure": {
                "domain": "Environmental Health and Safety",
                "subdomain": "Other Chemical Exposure Incident",
            },
            "equipment_failure": {
                "domain": "Engineering and Maintenance",
                "subdomain": "Other Equipment Failure Incident",
            },
        }
        return fallbacks.get(mechanism)

    def is_taxonomy_compatible(
        self,
        mechanism: str,
        domain: str,
        subdomain: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Check whether a retrieved taxonomy result matches the detected mechanism."""
        result = result or {}
        searchable_fields = [
            domain,
            subdomain,
            str(result.get("hazard_identified") or ""),
            str(result.get("risk_identified") or ""),
            str(result.get("risk_explanation") or ""),
            str(result.get("control_measures") or ""),
            str(result.get("search_text") or ""),
        ]
        taxonomy_text = " ".join(searchable_fields).lower()

        compatibility_rules: dict[str, list[str]] = {
            "falling_object": [
                "falling object",
                "struck by object",
                "object impact",
                "overhead object",
                "falling luggage",
                "falling rack",
                "luggage rack",
                "equipment failure",
                "structural failure",
                "fixture failure",
                "rack broke",
                "object fell",
            ],
            "slip_trip_fall": [
                "slip",
                "trip",
                "wet floor",
                "slippery surface",
                "slippery floor",
                "fall while walking",
                "lost balance",
            ],
            "fire_explosion": ["fire", "explosion", "smoke", "flame", "burn"],
            "electrical": [
                "electrical",
                "electrocution",
                "electric shock",
                "short circuit",
                "live wire",
            ],
            "vehicle_collision": [
                "vehicle",
                "road accident",
                "collision",
                "transport",
                "traffic",
            ],
            "chemical_exposure": [
                "chemical",
                "gas leak",
                "hazardous substance",
                "toxic",
                "chemical spill",
            ],
            "equipment_failure": [
                "equipment",
                "machinery",
                "structural",
                "asset failure",
                "component failure",
                "fixture failure",
            ],
        }

        allowed_terms = compatibility_rules.get(mechanism)
        if not allowed_terms:
            return True
        return any(term in taxonomy_text for term in allowed_terms)

    def _empty_taxonomy_result(
        self,
        status: str,
        mechanism_result: dict[str, Any],
        retrieved_count: int = 0,
        rejected_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "domain": None,
            "subdomain": None,
            "confidence": 0.0,
            "status": status,
            "top_score": 0.0,
            "selected_total_score": 0.0,
            "mechanism": mechanism_result,
            "agreement": {
                "matching_results": 0,
                "total_results": 0,
                "simple_ratio": 0.0,
                "weighted_ratio": 0.0,
            },
            "candidate_scores": [],
            "retrieved_result_count": retrieved_count,
            "compatible_result_count": 0,
            "rejected_results": rejected_results or [],
            "evidence": [],
            "is_fallback": False,
        }

    def _fallback_taxonomy_result(
        self,
        fallback: dict[str, str],
        mechanism_result: dict[str, Any],
        retrieved_count: int,
        rejected_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "domain": fallback["domain"],
            "subdomain": fallback["subdomain"],
            "confidence": 0.45,
            "status": "fallback_classification",
            "top_score": 0.0,
            "selected_total_score": 0.0,
            "mechanism": mechanism_result,
            "agreement": {
                "matching_results": 0,
                "total_results": 0,
                "simple_ratio": 0.0,
                "weighted_ratio": 0.0,
            },
            "candidate_scores": [],
            "retrieved_result_count": retrieved_count,
            "compatible_result_count": 0,
            "rejected_results": rejected_results,
            "evidence": [],
            "is_fallback": True,
        }

    def analyze_taxonomy(self, incident_text: str) -> dict[str, Any]:
        """Retrieve and classify an incident into an approved domain/subdomain pair."""
        cleaned_text = incident_text.strip()
        if not cleaned_text:
            raise ValueError("Incident text cannot be empty.")

        mechanism_result = self.detect_incident_mechanism(cleaned_text)
        primary_mechanism = mechanism_result["primary_mechanism"]

        results = self.retriever.retrieve(
            cleaned_text,
            chunk_type="taxonomy",
            limit=20,
            num_candidates=300,
        )

        if not results:
            return self._empty_taxonomy_result(
                status="no_match",
                mechanism_result=mechanism_result,
            )

        candidate_scores: dict[tuple[str, str], float] = defaultdict(float)
        candidate_counts: dict[tuple[str, str], int] = defaultdict(int)
        valid_results: list[dict[str, Any]] = []
        rejected_results: list[dict[str, Any]] = []

        for result in results:
            domain = result.get("domain")
            subdomain = result.get("subdomain")
            if not domain or not subdomain:
                continue

            score = float(result.get("score", 0.0))
            domain_text = str(domain)
            subdomain_text = str(subdomain)

            compatible = primary_mechanism == "unknown" or self.is_taxonomy_compatible(
                mechanism=primary_mechanism,
                domain=domain_text,
                subdomain=subdomain_text,
                result=result,
            )

            if not compatible:
                rejected_results.append(
                    {
                        "chunk_id": result.get("chunk_id"),
                        "domain": domain_text,
                        "subdomain": subdomain_text,
                        "score": round(score, 4),
                        "rejection_reason": (
                            "Taxonomy result is incompatible with detected mechanism: "
                            f"{primary_mechanism}."
                        ),
                    }
                )
                continue

            candidate = (domain_text, subdomain_text)
            candidate_scores[candidate] += score
            candidate_counts[candidate] += 1
            valid_results.append(result)

        if not candidate_scores:
            fallback = self.get_mechanism_fallback(primary_mechanism)
            if fallback is not None:
                fallback_exists = self.classification_validator.taxonomy_collection.find_one(
                    {
                        "domain": fallback["domain"],
                        "subdomain": fallback["subdomain"],
                        "active": True,
                    },
                    {"_id": 1},
                )
                if fallback_exists is not None:
                    return self._fallback_taxonomy_result(
                        fallback=fallback,
                        mechanism_result=mechanism_result,
                        retrieved_count=len(results),
                        rejected_results=rejected_results,
                    )

            return self._empty_taxonomy_result(
                status="no_compatible_taxonomy_match",
                mechanism_result=mechanism_result,
                retrieved_count=len(results),
                rejected_results=rejected_results,
            )

        ranked_candidates = sorted(
            candidate_scores.items(), key=lambda item: item[1], reverse=True
        )
        selected_candidate, selected_total_score = ranked_candidates[0]
        selected_domain, selected_subdomain = selected_candidate

        matching_results = [
            result
            for result in valid_results
            if (
                str(result.get("domain")),
                str(result.get("subdomain")),
            )
            == selected_candidate
        ]

        top_score = max(float(result.get("score", 0.0)) for result in matching_results)
        agreement_count = candidate_counts[selected_candidate]
        simple_agreement_ratio = (
            agreement_count / len(valid_results) if valid_results else 0.0
        )
        total_retrieval_score = sum(
            float(result.get("score", 0.0)) for result in valid_results
        )
        selected_weighted_score = sum(
            float(result.get("score", 0.0)) for result in matching_results
        )
        weighted_agreement_ratio = (
            selected_weighted_score / total_retrieval_score
            if total_retrieval_score > 0
            else 0.0
        )

        confidence = round(top_score * 0.7 + weighted_agreement_ratio * 0.3, 4)
        if confidence >= 0.80:
            status = "high_confidence"
        elif confidence >= 0.65:
            status = "medium_confidence"
        else:
            status = "low_confidence"

        evidence = [
            {
                "chunk_id": result.get("chunk_id"),
                "domain": result.get("domain"),
                "subdomain": result.get("subdomain"),
                "score": round(float(result.get("score", 0.0)), 4),
                "hazard_identified": result.get("hazard_identified"),
                "risk_identified": result.get("risk_identified"),
                "risk_explanation": result.get("risk_explanation"),
                "control_measures": result.get("control_measures"),
            }
            for result in valid_results
        ]

        candidate_score_details: list[dict[str, Any]] = []
        for (domain, subdomain), total_score in ranked_candidates:
            candidate = (domain, subdomain)
            count = candidate_counts[candidate]
            weighted_share = (
                total_score / total_retrieval_score
                if total_retrieval_score > 0
                else 0.0
            )
            candidate_score_details.append(
                {
                    "domain": domain,
                    "subdomain": subdomain,
                    "matching_results": count,
                    "combined_score": round(total_score, 4),
                    "weighted_share": round(weighted_share, 4),
                }
            )

        return {
            "domain": selected_domain,
            "subdomain": selected_subdomain,
            "confidence": confidence,
            "status": status,
            "top_score": round(top_score, 4),
            "selected_total_score": round(selected_total_score, 4),
            "mechanism": mechanism_result,
            "agreement": {
                "matching_results": agreement_count,
                "total_results": len(valid_results),
                "simple_ratio": round(simple_agreement_ratio, 4),
                "weighted_ratio": round(weighted_agreement_ratio, 4),
            },
            "candidate_scores": candidate_score_details,
            "retrieved_result_count": len(results),
            "compatible_result_count": len(valid_results),
            "rejected_results": rejected_results,
            "evidence": evidence,
            "is_fallback": False,
        }

    def assess_hipo(self, incident_text: str) -> dict[str, Any]:
        """Perform a conservative rule-based HIPO assessment."""
        text = incident_text.lower()

        confirmed_terms = [
            "multiple fatalities",
            "fatality",
            "death",
            "died",
            "permanent disability",
            "critical condition",
        ]
        potential_terms = [
            "near miss",
            "could have caused death",
            "could have been fatal",
            "potential fatality",
            "high potential incident",
            "high potential event",
            "fell from height",
            "fall from height",
            "electrocution",
            "electric shock",
            "fire",
            "explosion",
            "structural collapse",
            "vehicle collision",
            "struck by vehicle",
            "falling object",
            "confined space",
            "chemical exposure",
            "gas leak",
        ]
        non_hipo_terms = [
            "minor injury",
            "first aid",
            "small cut",
            "bruise",
            "no injury",
            "no harm",
            "no damage",
        ]

        def find_match(terms: list[str]) -> str | None:
            for term in terms:
                if term in text:
                    return term
            return None

        matched_term = find_match(confirmed_terms)
        if matched_term:
            return {
                "status": "confirmed_hipo",
                "assessment_status": "assessed",
                "matched_evidence": matched_term,
                "reason": (
                    "The narrative explicitly describes a fatal or permanently "
                    "disabling consequence."
                ),
            }

        matched_term = find_match(potential_terms)
        if matched_term:
            return {
                "status": "potential_hipo",
                "assessment_status": "assessed",
                "matched_evidence": matched_term,
                "reason": (
                    "The narrative describes a condition with potential for a "
                    "severe or fatal consequence."
                ),
            }

        matched_term = find_match(non_hipo_terms)
        if matched_term:
            return {
                "status": "not_hipo",
                "assessment_status": "assessed",
                "matched_evidence": matched_term,
                "reason": (
                    "The narrative describes a limited consequence and contains "
                    "no stated high-potential condition."
                ),
            }

        return {
            "status": "insufficient_information",
            "assessment_status": "not_assessed",
            "matched_evidence": None,
            "reason": (
                "The narrative does not contain enough information to determine "
                "whether the incident had high potential."
            ),
        }

    def analyze(self, incident_text: str) -> dict[str, Any]:
        """Run controlled classification, policy retrieval, and grounded LLM analysis."""
        cleaned_text = incident_text.strip()
        if not cleaned_text:
            raise ValueError("Incident text cannot be empty.")

        taxonomy_analysis = self.analyze_taxonomy(cleaned_text)

        impact_result = self.classification_validator.identify_impact(cleaned_text)
        controlled_classification = self.classification_validator.validate(
            domain=taxonomy_analysis.get("domain"),
            subdomain=taxonomy_analysis.get("subdomain"),
            impact_type=impact_result.get("impact_type"),
            matched_evidence=impact_result.get("matched_evidence"),
        )

        hipo_assessment = self.assess_hipo(cleaned_text)
        context = self.retriever.retrieve_incident_context(cleaned_text)

        def format_policy_evidence(
            results: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            return [
                {
                    "chunk_id": result.get("chunk_id"),
                    "chunk_type": result.get("chunk_type"),
                    "document_type": result.get("document_type"),
                    "section": result.get("section"),
                    "search_text": result.get("search_text"),
                    "score": round(float(result.get("score", 0.0)), 4),
                    "source": result.get("source"),
                }
                for result in results
            ]

        result: dict[str, Any] = {
            "incident_text": cleaned_text,
            "classification": {
                "domain": controlled_classification.get("domain"),
                "subdomain": controlled_classification.get("subdomain"),
                "status": controlled_classification.get("status"),
                "confidence": taxonomy_analysis.get("confidence", 0.0),
                "top_score": taxonomy_analysis.get("top_score", 0.0),
                "selected_total_score": taxonomy_analysis.get(
                    "selected_total_score", 0.0
                ),
                "agreement": taxonomy_analysis.get("agreement"),
                "is_fallback": taxonomy_analysis.get("is_fallback", False),
                "requires_manual_review": controlled_classification.get(
                    "requires_manual_review", False
                ),
                "validation_errors": controlled_classification.get(
                    "validation_errors", []
                ),
            },
            "impact": controlled_classification.get("impact"),
            "severity": controlled_classification.get("severity"),
            "mechanism": taxonomy_analysis.get("mechanism"),
            "hipo": hipo_assessment,
            "candidate_scores": taxonomy_analysis.get("candidate_scores", []),
            "retrieval_summary": {
                "retrieved_result_count": taxonomy_analysis.get(
                    "retrieved_result_count", 0
                ),
                "compatible_result_count": taxonomy_analysis.get(
                    "compatible_result_count", 0
                ),
                "rejected_result_count": len(
                    taxonomy_analysis.get("rejected_results", [])
                ),
            },
            "rejected_taxonomy_results": taxonomy_analysis.get(
                "rejected_results", []
            ),
            "taxonomy_evidence": taxonomy_analysis.get("evidence", []),
            "policy_evidence": {
                "hipo": format_policy_evidence(context.get("hipo_policy", [])),
                "severity": format_policy_evidence(
                    context.get("severity_policy", [])
                ),
                "rca": format_policy_evidence(context.get("rca_guidance", [])),
            },
        }

        if controlled_classification.get("requires_manual_review"):
            result["llm_analysis"] = None
            result["llm_status"] = "not_run"
            result["llm_error"] = (
                "LLM analysis was not generated because the controlled "
                "classification requires manual review."
            )
            return result

        try:
            llm_result = self.llm_analyzer.generate_analysis(
                incident_text=cleaned_text,
                deterministic_result=result,
            )
            result["llm_analysis"] = llm_result.model_dump()
            result["llm_status"] = "completed"
            result["llm_error"] = None
        except Exception as exc:
            result["llm_analysis"] = None
            result["llm_status"] = "failed"
            result["llm_error"] = str(exc)

        return result
