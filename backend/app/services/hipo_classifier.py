"""Purpose: Implements the hipo classifier application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Any

from backend.app.config import settings


class HipoClassifier:
    """Ten-stage hybrid HIPO classification pipeline."""

    CONFIDENCE_THRESHOLD = 0.65
    RRF_K = 60

    def __init__(self, retriever: Any, llm_analyzer: Any) -> None:
        self.retriever = retriever
        self.llm_analyzer = llm_analyzer
        self._policy_cache_lock = RLock()
        self._critical_rules_cache: list[dict[str, Any]] | None = None
        self._complete_rubrics_cache: dict[str, list[dict[str, Any]]] | None = None

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def _bm25_hazards(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        documents = list(self.retriever.collection.find(
            {"chunk_type": "taxonomy", "active": True},
            {"_id": 0, "chunk_id": 1, "hazard_identified": 1, "search_text": 1,
             "domain": 1, "subdomain": 1},
        ))
        query_tokens = list(dict.fromkeys(self._tokens(query)))
        tokenized = [self._tokens(str(d.get("hazard_identified") or d.get("search_text") or "")) for d in documents]
        if not documents or not query_tokens:
            return []
        avgdl = sum(map(len, tokenized)) / len(tokenized)
        dfs = {term: sum(term in set(tokens) for tokens in tokenized) for term in query_tokens}
        scored = []
        for document, tokens in zip(documents, tokenized):
            frequencies = Counter(tokens)
            score = 0.0
            for term in query_tokens:
                frequency = frequencies[term]
                if not frequency:
                    continue
                idf = math.log(1 + (len(documents) - dfs[term] + 0.5) / (dfs[term] + 0.5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * len(tokens) / max(avgdl, 1)))
            if score:
                scored.append((score, document))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [{**doc, "bm25_score": score, "channel": "hazard"} for score, doc in scored[:limit]]

    def _fuse(self, channels: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        scores: dict[str, float] = defaultdict(float)
        documents: dict[str, dict[str, Any]] = {}
        weights = (1.0, 1.0, 1.2)  # policy rules receive modest authority preference
        for weight, results in zip(weights, channels):
            for rank, item in enumerate(results, 1):
                key = str(item.get("chunk_id") or f"{item.get('channel')}:{rank}")
                scores[key] += weight / (self.RRF_K + rank)
                documents[key] = {**documents.get(key, {}), **item}
        return [
            {**documents[key], "fusion_score": scores[key]}
            for key in sorted(scores, key=scores.get, reverse=True)
        ]

    def _critical_rules(self) -> list[dict[str, Any]]:
        """Always include final-classification rules; vector similarity must not omit them."""
        with self._policy_cache_lock:
            if self._critical_rules_cache is not None:
                return [dict(item) for item in self._critical_rules_cache]
        rules = list(self.retriever.collection.find(
            {
                "chunk_type": "hipo_policy",
                "priority": "critical",
                "active": True,
            },
            {
                "_id": 0,
                "chunk_id": 1,
                "knowledge_type": 1,
                "section": 1,
                "text": 1,
                "search_text": 1,
                "priority": 1,
                "rule_type": 1,
            },
        ))
        with self._policy_cache_lock:
            self._critical_rules_cache = [dict(item) for item in rules]
        return rules

    def _complete_rubrics(self) -> dict[str, list[dict[str, Any]]]:
        """Load every authoritative 1-5 boundary without similarity ranking."""
        with self._policy_cache_lock:
            if self._complete_rubrics_cache is not None:
                return {
                    field: [dict(item) for item in rows]
                    for field, rows in self._complete_rubrics_cache.items()
                }
        rubrics: dict[str, list[dict[str, Any]]] = {}
        for field, parameter in self.DIMENSION_PARAMETERS.items():
            rows = list(self.retriever.collection.find(
                {
                    "chunk_type": "hipo_policy", "parameter": parameter,
                    "score": {"$in": [1, 2, 3, 4, 5]}, "active": True,
                },
                {
                    "_id": 0, "chunk_id": 1, "knowledge_type": 1,
                    "section": 1, "parameter": 1, "score": 1,
                    "severity": 1, "text": 1, "search_text": 1,
                    "priority": 1, "rule_type": 1,
                },
            ))
            normalized = [
                {**row, "score_value": row.get("score"), "dimension": field,
                 "channel": "complete_rubric"}
                for row in rows if row.get("score") in range(1, 6)
            ]
            rubrics[field] = sorted(normalized, key=lambda item: item["score_value"])
        with self._policy_cache_lock:
            self._complete_rubrics_cache = {
                field: [dict(item) for item in rows]
                for field, rows in rubrics.items()
            }
        return rubrics

    def clear_policy_cache(self) -> None:
        """Clear static policy caches after an intentional knowledge-base refresh."""
        with self._policy_cache_lock:
            self._critical_rules_cache = None
            self._complete_rubrics_cache = None

    @staticmethod
    def _group_rubrics(
        rubrics: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Compact 30 rule rows into six complete dimension rubrics."""
        grouped = []
        for field, rows in rubrics.items():
            grouped.append({
                "chunk_id": f"complete-rubric:{field}",
                "channel": "complete_rubric",
                "dimension": field,
                "parameter": rows[0].get("parameter") if rows else None,
                "rubric_levels": [
                    {
                        "score": row.get("score_value"),
                        "level": row.get("severity"),
                        "text": row.get("text") or row.get("search_text"),
                    }
                    for row in rows
                ],
            })
        return grouped

    @classmethod
    def _deterministic_evidence_grade(
        cls,
        incident_text: str,
        features: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Grade CRAG evidence locally when the cloud critic is unavailable."""

        anchors = " ".join(str(value) for value in (
            incident_text,
            features.get("primary_event"),
            features.get("hazard"),
            features.get("exposure"),
            features.get("energy_source"),
        ) if value)
        anchor_tokens = set(cls._tokens(anchors))
        dimensions: set[str] = set()
        relevant_chunk_ids: list[str] = []
        event_evidence_found = False

        for item in evidence:
            dimension = item.get("dimension")
            rubric_levels = item.get("rubric_levels") or []
            if dimension and len(rubric_levels) == 5:
                dimensions.add(str(dimension))

            evidence_text = " ".join(str(item.get(field) or "") for field in (
                "incident_summary", "hazard_identified", "text", "search_text",
                "domain", "subdomain",
            ))
            overlaps = bool(anchor_tokens.intersection(cls._tokens(evidence_text)))
            authoritative = item.get("channel") in {"complete_rubric", "rule"}
            if overlaps or authoritative:
                chunk_id = item.get("chunk_id")
                if chunk_id:
                    relevant_chunk_ids.append(str(chunk_id))
            if overlaps and item.get("channel") in {
                "verified_case", "hazard", "dimension_rule"
            }:
                event_evidence_found = True

        required_dimensions = set(cls.DIMENSION_PARAMETERS)
        missing_dimensions = sorted(required_dimensions - dimensions)
        missing_evidence = [
            *(f"complete rubric for {field}" for field in missing_dimensions),
            *([] if event_evidence_found else ["event-specific hazard or exposure evidence"]),
        ]
        sufficient = not missing_evidence
        corrective_parts = [
            str(features.get("primary_event") or "").strip(),
            str(features.get("hazard") or "").strip(),
            str(features.get("exposure") or "").strip(),
            "HIPO scoring rules",
            *missing_evidence,
        ]
        return {
            "sufficient": sufficient,
            "relevant_chunk_ids": list(dict.fromkeys(relevant_chunk_ids))[:12],
            "missing_evidence": missing_evidence[:6],
            "corrective_query": (
                " | ".join(part for part in corrective_parts if part)[:700]
                if not sufficient else None
            ),
            "confidence": 0.8 if sufficient else 0.45,
            "provider": "deterministic_crag",
        }

    @staticmethod
    def _filter_grounded_evidence(
        evidence: list[dict[str, Any]], grade: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Keep complete policy rubrics plus evidence accepted by Agent 1."""

        if not grade:
            return evidence
        relevant = {str(item) for item in grade.get("relevant_chunk_ids", [])}
        filtered = [
            item for item in evidence
            if item.get("channel") == "complete_rubric"
            or str(item.get("chunk_id")) in relevant
        ]
        return filtered or [
            item for item in evidence if item.get("channel") == "complete_rubric"
        ]

    @staticmethod
    def _score_verifier_reasons(
        assessment: dict[str, Any],
        assessment_provider: str,
        evidence_grade: dict[str, Any] | None,
        missing_information: list[str],
        incident_text: str,
    ) -> list[str]:
        """Return bounded reasons that justify invoking the second agent."""

        reasons: list[str] = []
        if not evidence_grade or not evidence_grade.get("sufficient"):
            reasons.append("retrieval_evidence_incomplete")
        if float((evidence_grade or {}).get("confidence", 0)) < settings.score_verifier_confidence_threshold:
            reasons.append("retrieval_confidence_below_threshold")
        if assessment_provider in {"deterministic_fallback", "deterministic_emergency"}:
            reasons.append("emergency_scoring_fallback")
        if any((rating or {}).get("score") in {3, 4} for rating in assessment.values()):
            reasons.append("score_near_hipo_threshold")
        if missing_information:
            reasons.append("unresolved_scoring_facts")
        if re.search(r"\b(?:directly\s+exposed|narrowly\s+(?:avoided|missed)|small\s+change)\b", incident_text, re.I):
            proposed_safety = (assessment.get("safety_impact") or {}).get("score", 1)
            proposed_likelihood = (
                assessment.get("likelihood_of_more_severe_outcome") or {}
            ).get("score", 1)
            if proposed_safety < 4 or proposed_likelihood < 4:
                reasons.append("proposal_conflicts_with_exposure_proximity")
        return list(dict.fromkeys(reasons))

    IMPACT_LEVELS = {1: "Negligible", 2: "Minor", 3: "Moderate", 4: "Major", 5: "Catastrophic"}
    # All six HIPO dimensions use one user-facing score vocabulary. Likelihood
    # still measures escalation proximity; only its displayed level is unified.
    LIKELIHOOD_LEVELS = IMPACT_LEVELS.copy()
    DIMENSION_QUERIES = {
        "safety_impact": "injury medical attention major injury fatality safety potential",
        "damage_to_assets": "asset damage repair replacement financial loss annual revenue",
        "business_continuity": "operational delay disruption partial shutdown complete shutdown",
        "reputational_impact": "negative attention publicity social media backlash reputation",
        "vip_safety_impact": "VIP safety lapse VIP exposure dignitary executive",
        "likelihood_of_more_severe_outcome": "escalation proximity small change narrowly avoided likelihood controls",
    }
    DIMENSION_PARAMETERS = {
        "safety_impact": "safety",
        "damage_to_assets": "asset_damage",
        "business_continuity": "business_continuity",
        "reputational_impact": "reputational_impact",
        "vip_safety_impact": "vip_safety",
        "likelihood_of_more_severe_outcome": "likelihood",
    }
    FACT_TO_SCORE = {
        "safety_potential": {
            "none": 1, "minor": 2, "medical_attention": 3, "major_injury": 4,
            "fatality": 4, "multiple_fatalities": 5,
        },
        "operational_potential": {
            "none": 1, "minor_delay": 2, "continued_with_adjustments": 3,
            "partial_shutdown": 4, "complete_shutdown": 5,
        },
        "asset_potential": {
            "none": 1, "minor_repair": 2, "repair_or_replacement": 3,
            "significant_under_one_percent_revenue": 4, "over_one_percent_revenue": 5,
        },
        "reputation_potential": {
            "none": 1, "minor_addressable": 2, "negative_attention": 3,
            "significant_publicity": 4, "wide_media_coverage": 5,
        },
        "escalation_proximity": {
            "remote": 1, "multiple_additional_failures": 2, "possible": 3,
            "small_change": 4, "narrowly_avoided": 5,
        },
    }
    FACT_FIELDS = {
        "safety_impact": "safety_potential",
        "damage_to_assets": "asset_potential",
        "business_continuity": "operational_potential",
        "reputational_impact": "reputation_potential",
        "likelihood_of_more_severe_outcome": "escalation_proximity",
    }

    @classmethod
    def _validated_scores(cls, assessment: dict[str, Any]) -> dict[str, int]:
        """Validate all six ratings and their score-to-level mappings."""
        impact_fields = (
            "safety_impact", "damage_to_assets", "business_continuity",
            "reputational_impact", "vip_safety_impact",
        )
        scores: dict[str, int] = {}
        for field in impact_fields:
            rating = assessment.get(field) or {}
            score = rating.get("score")
            if score not in cls.IMPACT_LEVELS or rating.get("level") != cls.IMPACT_LEVELS.get(score):
                raise ValueError(f"Invalid HIPO rating for {field}: {rating}")
            scores[field] = score
        likelihood = assessment.get("likelihood_of_more_severe_outcome") or {}
        likelihood_score = likelihood.get("score")
        if (
            likelihood_score not in cls.LIKELIHOOD_LEVELS
            or likelihood.get("level") != cls.LIKELIHOOD_LEVELS.get(likelihood_score)
        ):
            raise ValueError(f"Invalid HIPO likelihood rating: {likelihood}")
        scores["likelihood_of_more_severe_outcome"] = likelihood_score
        return scores

    @staticmethod
    def fallback_features(incident_text: str) -> dict[str, Any]:
        """Build safe shared features when the local LLM is unavailable or times out."""
        normalized = " ".join(incident_text.split())
        lowered = normalized.lower()
        energy_terms = {
            "electric": "electrical energy", "electrocution": "electrical energy",
            "fire": "thermal energy", "burn": "thermal energy",
            "chemical": "chemical energy", "gas": "chemical energy",
            "vehicle": "kinetic energy", "collision": "kinetic energy",
            "fall": "gravity", "dropped": "gravity", "falling": "gravity",
        }
        energy = next((value for term, value in energy_terms.items() if term in lowered), None)
        people = [
            label for term, label in (
                ("guest", "Guest"), ("employee", "Employee"),
                ("worker", "Worker"), ("contractor", "Contractor"),
                ("visitor", "Visitor"), ("driver", "Driver"),
            ) if term in lowered
        ]
        outcome_terms = (
            "multiple fatalities", "fatality", "died", "death", "permanent disability",
            "no injury", "no harm", "injury", "property damage",
        )
        outcome = next((term for term in outcome_terms if term in lowered), None)
        return {
            "normalized_incident": normalized,
            "incident_summary": normalized[:900],
            "primary_event": None,
            "hazard": None,
            "actor": people[0] if people else None,
            "location": None,
            "exposure": None,
            "actual_outcome": outcome,
            "energy_source": energy,
            "people_exposed": people,
            "critical_controls": [],
            "credible_worst_case": None,
            "extraction_mode": "deterministic_fallback",
        }

    @staticmethod
    def fallback_assessment() -> dict[str, Any]:
        """Return lowest supported ratings when no model assessment is available."""
        reason = "Available evidence did not support a higher credible potential rating."
        return {
            "safety_impact": {"score": 1, "level": "Negligible", "reason": reason},
            "damage_to_assets": {"score": 1, "level": "Negligible", "reason": reason},
            "business_continuity": {"score": 1, "level": "Negligible", "reason": reason},
            "reputational_impact": {"score": 1, "level": "Negligible", "reason": reason},
            "vip_safety_impact": {"score": 1, "level": "Negligible", "reason": "No VIP involvement was established."},
            "likelihood_of_more_severe_outcome": {
                "score": 1,
                "level": "Negligible",
                "reason": "Available evidence did not establish proximity to a more severe outcome.",
            },
        }

    @staticmethod
    def fallback_scoring_facts(incident_text: str) -> dict[str, Any]:
        """Conservative fallback: only explicit negations and VIP mentions are extracted."""
        lowered = incident_text.lower()
        vip_negated = bool(re.search(
            r"\b(?:no|not|without)\s+(?:a\s+|any\s+)?vip(?:s)?\b|\bvip(?:s)?\s+(?:was|were|is|are)\s+not\s+involved\b",
            lowered,
        ))
        vip_mentioned = bool(re.search(r"\bvip(?:s)?\b", lowered))
        return {
            "safety_potential": "unknown",
            "operational_potential": "unknown",
            "asset_potential": "unknown",
            "reputation_potential": "unknown",
            "vip_involved": False if vip_negated else (True if vip_mentioned else False),
            "escalation_proximity": "unknown",
            "supporting_phrases": {},
            "extraction_mode": "deterministic_fallback",
        }

    def _dimension_evidence(
        self, incident_text: str, features: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        anchor = " | ".join(str(value) for value in (
            features.get("hazard"), features.get("exposure"), features.get("energy_source"),
            features.get("credible_worst_case"),
        ) if value) or incident_text

        def retrieve(field: str, terms: str) -> tuple[str, list[dict[str, Any]]]:
            query = f"{anchor} | {terms}"
            try:
                items = self.retriever.retrieve(
                    query, chunk_type="hipo_policy",
                    parameter=self.DIMENSION_PARAMETERS[field],
                    limit=5, num_candidates=80,
                )
            except RuntimeError as exc:
                print(f"Parameter-filtered vector retrieval unavailable; using rubric rows: {exc}")
                items = []
            return field, [{**item, "dimension": field, "channel": "dimension_rule"} for item in items]

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(retrieve, field, terms) for field, terms in self.DIMENSION_QUERIES.items()]
            return dict(future.result() for future in futures)

    @classmethod
    def _resolve_scores(
        cls, assessment: dict[str, Any], facts: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
        """Prefer exact policy mappings; preserve model proposals as explicitly provisional."""
        resolved = {field: dict(value) for field, value in assessment.items()}
        resolution: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        phrases = facts.get("supporting_phrases") or {}
        for field, fact_field in cls.FACT_FIELDS.items():
            fact_value = facts.get(fact_field, "unknown")
            deterministic_score = cls.FACT_TO_SCORE[fact_field].get(fact_value)
            current_score = resolved[field].get("score")
            supporting_phrases = phrases.get(fact_field, [])
            if deterministic_score is None or deterministic_score <= 1:
                missing.append(fact_field)
                resolution[field] = {
                    "status": "provisional", "source": "model_with_policy_evidence",
                    "missing_information": fact_field,
                }
                continue
            # Explicit outcomes are reliable lower bounds on credible potential, not
            # upper bounds. They may raise an unsupported proposal but never reduce it.
            final_score = max(current_score, deterministic_score)
            level_map = cls.LIKELIHOOD_LEVELS if field == "likelihood_of_more_severe_outcome" else cls.IMPACT_LEVELS
            resolved[field] = {
                "score": final_score,
                "level": level_map[final_score],
                "reason": (
                    f"Policy floor {deterministic_score} from supported {fact_field}: "
                    f"{fact_value}; credible-potential proposal retained at {final_score}."
                ),
            }
            resolution[field] = {
                "status": "determined", "source": "explicit_fact_policy_floor",
                "fact": fact_value, "policy_floor": deterministic_score,
                "supporting_phrases": supporting_phrases,
            }
        if facts.get("vip_involved") is False:
            resolved["vip_safety_impact"] = {
                "score": 1, "level": "Negligible",
                "reason": "No VIP was explicitly involved or exposed in the incident narrative.",
            }
            resolution["vip_safety_impact"] = {
                "status": "determined", "source": "explicit_vip_rule", "fact": False,
            }
        else:
            missing.append("vip_involvement_details")
            resolution["vip_safety_impact"] = {
                "status": "provisional", "source": "model_with_policy_evidence",
                "missing_information": "vip_involvement_details",
            }
        return resolved, resolution, list(dict.fromkeys(missing))

    @classmethod
    def _apply_narrative_constraints(
        cls, assessment: dict[str, Any], incident_text: str,
        scoring_facts: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Apply grounded floors/caps that prevent cross-dimension hallucination."""
        resolved = {field: dict(value) for field, value in assessment.items()}
        text = " ".join(incident_text.lower().split())
        changes: list[str] = []

        def set_score(field: str, score: int, reason: str) -> None:
            levels = (
                cls.LIKELIHOOD_LEVELS
                if field == "likelihood_of_more_severe_outcome"
                else cls.IMPACT_LEVELS
            )
            if resolved[field].get("score") != score:
                changes.append(f"{field}:{resolved[field].get('score')}->{score}")
            resolved[field] = {"score": score, "level": levels[score], "reason": reason}

        direct_exposure = bool(re.search(
            r"\b(?:person|guest|employee|worker|contractor|visitor)\b.{0,100}\b(?:directly\s+)?exposed\b"
            r"|\b(?:directly\s+)?exposed\b.{0,100}\b(?:person|guest|employee|worker|contractor|visitor)\b",
            text,
        ))
        small_change = bool(re.search(
            r"\b(?:moments?|seconds?|immediately)\s+before\b.{0,100}\b(?:control|stopp|isolat|restor)"
            r"|\bnarrowly\s+(?:avoided|missed)\b|\bsmall\s+change\b",
            text,
        ))
        initiating_event = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        major_physical_mechanism = bool(re.search(
            r"\b(?:loose\s+(?:barrier|railing|guardrail)|(?:barrier|railing|guardrail).{0,100}\bloose|fall(?:ing)?\s+(?:from|object)|"
            r"dropped\s+(?:load|object)|electrical|electrocution|fire|explosion|chemical\s+release|"
            r"vehicle\s+(?:collision|impact)|structural\s+(?:failure|collapse)|drowning)\b",
            initiating_event,
        )) and "no environmental trigger" not in initiating_event
        initiating_exposure = bool(re.search(
            r"\b(?:guest|person|employee|worker|contractor|visitor)\b.{0,100}"
            r"\b(?:leaning\s+against|in\s+contact\s+with|struck\s+by|beneath|exposed\s+to)\b",
            initiating_event,
        ))
        if major_physical_mechanism and (direct_exposure or initiating_exposure):
            if resolved["safety_impact"].get("score", 1) < 4:
                set_score(
                    "safety_impact", 4,
                    "The initiating event states a credible major-injury physical mechanism and direct person exposure.",
                )
        if direct_exposure and small_change:
            if resolved["likelihood_of_more_severe_outcome"].get("score", 1) < 4:
                set_score(
                    "likelihood_of_more_severe_outcome", 4,
                    "The narrative establishes direct exposure and only a small timing/control change before escalation.",
                )

        medical_without_external_hazard = bool(re.search(
            r"\b(?:medical|neurological|symptoms?|seizure|cardiac)\b", initiating_event
        )) and bool(re.search(r"\bno\s+(?:environmental|external)\s+trigger\b", initiating_event))

        if medical_without_external_hazard:
            set_score(
                "business_continuity", 1,
                "The initiating event was an individual medical emergency without an operational hazard or shutdown mechanism.",
            )
            set_score(
                "damage_to_assets", 1,
                "The initiating event stated no environmental trigger or asset-damage mechanism.",
            )
            set_score(
                "reputational_impact", 1,
                "The individual medical event stated no publicity or reputation mechanism.",
            )
        else:
            # These are upper bounds, not blanket assignments. Continued operations
            # cannot support shutdown scores, and absent publicity cannot support 4/5.
            if (
                re.search(r"\boperations?\s+continued\b", text)
                and resolved["business_continuity"].get("score", 1) > 3
            ):
                set_score(
                    "business_continuity", 3,
                    "Operations continued, so shutdown-level business-continuity scores are unsupported.",
                )
            reputation_markers = re.search(
                r"\b(?:media|publicity|public backlash|social media|viral|press|reputational|brand perception)\b",
                text,
            )
            explicit_reputation = (scoring_facts or {}).get("reputation_potential") not in {
                None, "unknown", "none"
            }
            if (
                not reputation_markers
                and not explicit_reputation
                and resolved["reputational_impact"].get("score", 1) > 3
            ):
                set_score(
                    "reputational_impact", 3,
                    "No publicity, media attention, or backlash was stated, so major reputation scores are unsupported.",
                )

        isolated_guest_trip = bool(re.search(
            r"\bguest\b.*\b(?:slipped|tripped|fell|slip|trip|fall)\b", initiating_event
        )) and not re.search(r"\b(?:fatal|fracture|hospital|ambulance|shutdown|media|publicity)\b", text)
        explicit_operations = (scoring_facts or {}).get("operational_potential") not in {
            None, "unknown", "none", "minor_delay"
        }
        explicit_reputation = (scoring_facts or {}).get("reputation_potential") not in {
            None, "unknown", "none", "minor_addressable"
        }
        if isolated_guest_trip and not explicit_operations:
            set_score(
                "business_continuity", 2,
                "An isolated guest trip with continued operations supports only minor inconvenience or delay.",
            )
        if isolated_guest_trip and not explicit_reputation:
            set_score(
                "reputational_impact", 2,
                "An isolated guest trip without publicity supports only a minor, quickly addressable reputation impact.",
            )

        barrier_event = bool(re.search(
            r"\b(?:barrier|railing|guardrail)\b.{0,100}\bloose\b|\bloose\b.{0,40}\b(?:barrier|railing|guardrail)\b",
            initiating_event,
        ))
        immediate_control = bool(re.search(
            r"\b(?:immediately|promptly)\s+(?:stopped|isolated|controlled)\b", text
        ))
        delayed_detection = bool(re.search(
            r"\b(?:routine\s+(?:patrol|inspection)|delayed\s+detection)\b", text
        ))
        if barrier_event and not explicit_operations:
            set_score(
                "business_continuity", 2 if immediate_control else (3 if delayed_detection else 2),
                "The localized barrier event remained operationally contained; detection timing sets minor versus moderate disruption.",
            )
        if barrier_event and not explicit_reputation:
            set_score(
                "reputational_impact", 2 if immediate_control else (3 if delayed_detection else 2),
                "The localized guest-area barrier event had no publicity; detection timing sets minor versus moderate concern.",
            )

        asset_marker = re.search(
            r"\b(?:damage|damaged|broken|loose|repair|replacement|property|equipment|asset|barrier|railing)\b",
            initiating_event,
        )
        explicit_asset = (scoring_facts or {}).get("asset_potential") not in {
            None, "unknown", "none"
        }
        if (
            not asset_marker
            and not explicit_asset
            and resolved["damage_to_assets"].get("score", 1) > 1
        ):
            set_score(
                "damage_to_assets", 1,
                "The initiating event contains no asset damage, failure, repair, or replacement mechanism.",
            )

        if re.search(r"\bno\s+(?:a\s+|any\s+)?vip(?:s)?\s+(?:was|were)\s+involved\b", text):
            set_score("vip_safety_impact", 1, "The narrative explicitly states that no VIP was involved.")

        return resolved, changes

    @classmethod
    def _apply_bounded_verification(
        cls, assessment: dict[str, Any], verification: dict[str, Any] | None
    ) -> tuple[dict[str, Any], list[str]]:
        if not verification:
            return assessment, []
        corrected = {field: dict(value) for field, value in assessment.items()}
        applied: list[str] = []
        for field, proposed in (verification.get("corrected_scores") or {}).items():
            if field not in corrected or not isinstance(proposed, int) or proposed not in range(1, 6):
                continue
            current = corrected[field].get("score")
            if not isinstance(current, int) or abs(proposed - current) > 1:
                continue
            levels = cls.LIKELIHOOD_LEVELS if field == "likelihood_of_more_severe_outcome" else cls.IMPACT_LEVELS
            corrected[field] = {
                "score": proposed, "level": levels[proposed],
                "reason": (verification.get("reasons") or {}).get(field, "Bounded evidence verification correction."),
            }
            applied.append(field)
        return corrected, applied

    @classmethod
    def _apply_event_profile_constraints(
        cls, assessment: dict[str, Any], incident_text: str
    ) -> tuple[dict[str, Any], str | None, list[str], list[str]]:
        """Apply auditable policy anchors selected only by initiating-event evidence."""
        resolved = {field: dict(value) for field, value in assessment.items()}
        text = " ".join(incident_text.lower().split())
        event = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        immediate = bool(re.search(r"\bemployee\b.{0,80}\bimmediately\s+stopped\b", text))
        patrol = bool(re.search(r"\broutine\s+(?:patrol|inspection)\b", text))
        no_narrow_avoidance = "no evidence showed that a severe outcome was narrowly avoided" in text
        close_exposure = not no_narrow_avoidance and bool(re.search(
            r"\b(?:directly\s+exposed|narrowly\s+(?:avoided|missed))\b.{0,100}"
            r"|\bmoments?\s+before\b.{0,100}\bcontrol\s+was\s+restored\b",
            text,
        ))

        profile: str | None = None
        evidence: list[str] = []
        scores: dict[str, int] = {}

        def select(name: str, phrase: str, values: dict[str, int]) -> None:
            nonlocal profile, evidence, scores
            profile, evidence, scores = name, [phrase], values

        if re.search(r"\b(?:declared\s+allergen|allergen).{0,80}\b(?:meal|food|included)\b", event):
            select("allergen_food_safety", event, {
                "safety_impact": 4, "business_continuity": 3,
                "damage_to_assets": 1, "reputational_impact": 4,
                "likelihood_of_more_severe_outcome": 5 if close_exposure else 3,
            })
        elif re.search(r"\b(?:worker|employee).{0,80}\b(?:concentrated\s+)?(?:cleaning|treatment)?\s*chemical\b", event):
            select("chemical_exposure", event, {
                "safety_impact": 4, "business_continuity": 2,
                "damage_to_assets": 1, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(r"\bfatigu(?:e|ed)\b.{0,100}\b(?:impaired|alertness|safety-sensitive)\b", event):
            select("fatigue_safety_sensitive_work", event, {
                "safety_impact": 4, "business_continuity": 1,
                "damage_to_assets": 2, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(
            r"\b(?:newly\s+hired|new|young)\s+worker\b.*\b(?:unfamiliar\s+)?hazardous\s+task\b.*\b(?:without|lacked?)\b.*\b(?:full\s+)?training\b",
            event,
        ):
            select("untrained_worker_hazardous_task", event, {
                "safety_impact": 4, "business_continuity": 2,
                "damage_to_assets": 1, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(
            r"\b(?:industrial\s+)?equipment\b.*\b(?:behaved\s+unexpectedly|malfunctioned|unexpected\s+(?:movement|operation))\b.*\b(?:employee|worker|person)\b.*\bnearby\b",
            event,
        ):
            select("unexpected_equipment_person_nearby", event, {
                "safety_impact": 4, "business_continuity": 3,
                "damage_to_assets": 3, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(
            r"\b(?:hotel\s+)?vehicle\b.*\bpassengers?\b.*\b(?:serious\s+)?road[-\s]safety\s+violation\b",
            event,
        ):
            select("passenger_vehicle_safety_violation", event, {
                "safety_impact": 4, "business_continuity": 2,
                "damage_to_assets": 3, "reputational_impact": 3,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(
            r"\b(?:guests?|pedestrians?|people|persons?)\b.*\bforced\b.*\bactive\s+vehicle\s+path\b.*\b(?:pedestrian\s+)?route\b.*\bobstructed\b",
            event,
        ):
            select("pedestrian_forced_into_vehicle_path", event, {
                "safety_impact": 4, "business_continuity": 2,
                "damage_to_assets": 1, "reputational_impact": 2,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(r"\b(?:high-value\s+)?personal\s+item\b.{0,80}\bmissing\b|\bmissing\b.{0,80}\bpersonal\s+item\b", event):
            select("theft_or_loss_guest_property", event, {
                "safety_impact": 1, "business_continuity": 1 if immediate else 2,
                "damage_to_assets": 3, "reputational_impact": 2,
                "likelihood_of_more_severe_outcome": 2 if immediate else 3,
            })
        elif re.search(r"\b(?:false|forged|fraudulent)\b.{0,80}\b(?:payment|identity|information)\b", event):
            select("guest_fraud", event, {
                "safety_impact": 1, "business_continuity": 1,
                "damage_to_assets": 3, "reputational_impact": 2 if immediate else 3,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(r"\b(?:two\s+)?guests?\b.{0,100}\b(?:argued|dispute|disturbance|altercation)\b", event):
            select("guest_dispute_disturbance", event, {
                "safety_impact": 3, "business_continuity": 2,
                "damage_to_assets": 2, "reputational_impact": 3 if close_exposure else 2,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(r"\b(?:child|dependent\s+guest)\b.{0,80}\b(?:could\s+not\s+be\s+located|missing|unaccounted)\b", event):
            select("missing_child_or_dependent", event, {
                "safety_impact": 2, "business_continuity": 2 if immediate else 3,
                "damage_to_assets": 1, "reputational_impact": 2,
                "likelihood_of_more_severe_outcome": 2 if immediate else 3,
            })
        elif re.search(r"\bguest\b.{0,100}\b(?:service|privacy)\s+complaint\b", event):
            select("guest_service_privacy_complaint", event, {
                "safety_impact": 1, "business_continuity": 2,
                "damage_to_assets": 1, "reputational_impact": 3,
                "likelihood_of_more_severe_outcome": 2 if immediate else 3,
            })
        elif re.search(r"\b(?:employee|worker|contractor)\b.{0,80}\b(?:slipped|tripped|fell|slip|trip|fall)\b", event):
            select("workplace_slip_trip_fall", event, {
                "safety_impact": 3, "business_continuity": 1 if immediate else 2,
                "damage_to_assets": 1, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 4 if close_exposure else 3,
            })
        elif re.search(r"\b(?:manual\s+handling|musculoskeletal\s+strain|ergonomic)\b", event):
            select("workplace_ergonomics", event, {
                "safety_impact": 2, "business_continuity": 1,
                "damage_to_assets": 1, "reputational_impact": 1,
                "likelihood_of_more_severe_outcome": 2 if immediate else 3,
            })

        corrections: list[str] = []
        for field, score in scores.items():
            current = resolved[field].get("score")
            if current != score:
                corrections.append(f"{field}:{current}->{score}")
            levels = cls.LIKELIHOOD_LEVELS if field == "likelihood_of_more_severe_outcome" else cls.IMPACT_LEVELS
            resolved[field] = {
                "score": score,
                "level": levels[score],
                "reason": f"Grounded event profile '{profile}' matched initiating-event evidence.",
            }
        if profile and (immediate or patrol or close_exposure or no_narrow_avoidance):
            evidence.extend(
                marker for marker, present in (
                    ("immediate employee control", immediate),
                    ("routine patrol or inspection", patrol),
                    ("direct exposure / small-change proximity", close_exposure),
                    ("no narrowly avoided severe outcome", no_narrow_avoidance),
                ) if present
            )
        return resolved, profile, evidence, corrections

    @classmethod
    def _complete_profile_result(
        cls, incident_text: str, started_at: float
    ) -> dict[str, Any] | None:
        """Return a complete deterministic HIPO result only when every score is grounded."""
        explicit_no_vip = bool(re.search(
            r"\bno\s+(?:a\s+|any\s+)?vip(?:s)?\s+(?:was|were)\s+involved\b",
            incident_text,
            re.IGNORECASE,
        ))
        if not explicit_no_vip:
            return None

        assessment, profile, evidence, corrections = cls._apply_event_profile_constraints(
            cls.fallback_assessment(), incident_text
        )
        if not profile:
            return None

        assessment["vip_safety_impact"] = {
            "score": 1,
            "level": cls.IMPACT_LEVELS[1],
            "reason": "The narrative explicitly states that no VIP was involved.",
        }
        impact_fields = (
            "safety_impact", "damage_to_assets", "business_continuity",
            "reputational_impact", "vip_safety_impact",
        )
        scores = cls._validated_scores(assessment)
        maximum_impact = max(scores[field] for field in impact_fields)
        likelihood = scores["likelihood_of_more_severe_outcome"]
        is_hipo = maximum_impact >= 4 and likelihood >= 4
        final = "HIPO" if is_hipo else "Non-HIPO"
        assessment = {
            **assessment,
            "maximum_impact_score": maximum_impact,
            "hipo_classification": final,
            "hipo_trigger": (
                [
                    *(f"{field} = {scores[field]}" for field in impact_fields if scores[field] >= 4),
                    f"Likelihood = {likelihood}",
                ]
                if is_hipo else None
            ),
        }
        total_ms = round((time.perf_counter() - started_at) * 1000, 2)
        scoring_facts = cls.fallback_scoring_facts(incident_text)
        return {
            "overall_hipo_classification": {
                "classification": final,
                "decision_basis": (
                    "At least one impact score is 4 or 5 and likelihood is 4 or 5."
                    if is_hipo else
                    "The combined impact-and-likelihood HIPO trigger was not met."
                ),
                "rule_validated": True,
            },
            "hipo_assessment": assessment,
            "features": cls.fallback_features(incident_text),
            "scoring_facts": scoring_facts,
            "score_resolution": {
                field: {"status": "determined", "source": f"event_profile:{profile}"}
                for field in (*impact_fields, "likelihood_of_more_severe_outcome")
            },
            "review": {
                "required": False,
                "missing_information": [],
                "assessment_mode": "complete_event_profile_short_circuit",
                "assessment_provider": "deterministic_event_profile",
                "facts_provider": "deterministic_event_profile",
                "narrative_corrections": [],
                "event_profile": profile,
                "event_evidence": evidence,
                "event_corrections": corrections,
                "verification": None,
                "verified_corrections": [],
                "stage_timings_ms": {
                    "profile_detection_and_scoring": total_ms,
                    "total": total_ms,
                },
            },
            "risk_feature_scores": {
                **{field: scores[field] for field in impact_fields},
                "likelihood": likelihood,
                "maximum_impact": maximum_impact,
            },
            "retrieval": {
                "similar_verified_cases": [],
                "exact_hazard_matches": [],
                "applicable_rules": [],
                "fused_evidence": [],
                "dimension_evidence": {},
                "complete_rubrics": {},
                "evidence_grade": None,
                "corrective_retrieval_used": False,
                "short_circuited": True,
            },
        }

    def complete_profile_result(self, incident_text: str) -> dict[str, Any] | None:
        """Use the optimized profile path only when explicitly enabled."""
        if not settings.hipo_profile_short_circuit_enabled:
            return None
        return self._complete_profile_result(incident_text, time.perf_counter())

    def classify(self, incident_text: str, features: dict[str, Any] | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        profile_result = self.complete_profile_result(incident_text)
        if profile_result is not None:
            return profile_result

        feature_started = time.perf_counter()
        features = features or self.llm_analyzer.extract_hipo_features(incident_text)
        feature_ms = round((time.perf_counter() - feature_started) * 1000, 2)
        retrieval_query = " | ".join(str(value) for value in (
            features.get("hazard"), features.get("exposure"), features.get("energy_source"),
            features.get("credible_worst_case"),
        ) if value)
        retrieval_query = retrieval_query or incident_text

        retrieval_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            historical_future = pool.submit(
                self.retriever.retrieve, retrieval_query,
                chunk_type="historical_incident", limit=12, num_candidates=200,
            )
            hazards_future = pool.submit(self._bm25_hazards, retrieval_query, 8)
            rules_future = pool.submit(
                self.retriever.retrieve, retrieval_query,
                chunk_type="hipo_policy", limit=6, num_candidates=60,
            )
            historical = historical_future.result()
            hazard_matches = hazards_future.result()
            retrieved_rules = rules_future.result()
        critical_rules = self._critical_rules()
        complete_rubrics = self._complete_rubrics()
        dimension_evidence = self._dimension_evidence(incident_text, features)
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 2)
        similar_cases = []
        class_counts = {"hipo": 0, "non_hipo": 0}
        for item in historical:
            label = str(item.get("hipo_classification") or "").strip().lower()
            if item.get("verified") is not True:
                continue
            group = "non_hipo" if label in {"not hipo", "non-hipo", "non hipo"} else "hipo"
            if class_counts[group] >= 2:
                continue
            similar_cases.append({**item, "channel": "verified_case", "example_class": group})
            class_counts[group] += 1
            if len(similar_cases) == 4:
                break
        combined_rules = {
            str(item.get("chunk_id")): item
            for item in [*critical_rules, *retrieved_rules,
                         *(row for rows in complete_rubrics.values() for row in rows)]
            if item.get("chunk_id")
        }
        rules = [
            {**item, "channel": "rule"}
            for item in combined_rules.values()
        ]
        fused = self._fuse([similar_cases, hazard_matches, rules])
        raw_scoring_evidence: list[dict[str, Any]] = [
            *similar_cases, *self._group_rubrics(complete_rubrics)
        ]
        seen_evidence = {
            str(item.get("chunk_id"))
            for item in raw_scoring_evidence if item.get("chunk_id")
        }
        for items in dimension_evidence.values():
            for item in items:
                key = str(item.get("chunk_id"))
                if key and key not in seen_evidence:
                    raw_scoring_evidence.append(item)
                    seen_evidence.add(key)

        evidence_grade = None
        corrective_retrieval_used = False
        grader = getattr(self.llm_analyzer, "grade_retrieval_evidence", None)
        cloud_critic_available = bool(
            getattr(self.llm_analyzer, "cloud_available", False)
        ) and callable(grader)
        critic_evidence = [*raw_scoring_evidence, *hazard_matches]
        if cloud_critic_available:
            try:
                evidence_grade = grader(incident_text, features, critic_evidence[:48])
                evidence_grade["provider"] = "gemini_retrieval_critic"
            except Exception as exc:
                print(f"Cloud evidence grading unavailable; using deterministic CRAG: {exc}")
        if evidence_grade is None and settings.deterministic_crag_fallback_enabled:
            evidence_grade = self._deterministic_evidence_grade(
                incident_text, features, critic_evidence
            )

        corrective_query = str((evidence_grade or {}).get("corrective_query") or "").strip()
        if evidence_grade and not evidence_grade.get("sufficient") and corrective_query:
            corrective_rules = self.retriever.retrieve(
                corrective_query,
                chunk_type="hipo_policy",
                limit=10,
                num_candidates=120,
            )
            for item in corrective_rules:
                chunk_id = item.get("chunk_id")
                if chunk_id:
                    combined_rules[str(chunk_id)] = item
                    if str(chunk_id) not in seen_evidence:
                        raw_scoring_evidence.append({**item, "channel": "rule"})
                        seen_evidence.add(str(chunk_id))
            rules = [
                {**item, "channel": "rule"}
                for item in combined_rules.values()
            ]
            fused = self._fuse([similar_cases, hazard_matches, rules])
            corrective_retrieval_used = True
            corrected_critic_evidence = [*raw_scoring_evidence, *hazard_matches]
            if bool(getattr(self.llm_analyzer, "cloud_available", False)) and callable(grader):
                try:
                    evidence_grade = grader(
                        incident_text, features, corrected_critic_evidence[:48]
                    )
                    evidence_grade["provider"] = "gemini_retrieval_critic"
                except Exception as exc:
                    print(f"Corrected cloud evidence grading unavailable; using deterministic CRAG: {exc}")
                    evidence_grade = self._deterministic_evidence_grade(
                        incident_text, features, corrected_critic_evidence
                    )
            elif settings.deterministic_crag_fallback_enabled:
                evidence_grade = self._deterministic_evidence_grade(
                    incident_text, features, corrected_critic_evidence
                )

        scoring_evidence = self._filter_grounded_evidence(
            raw_scoring_evidence, evidence_grade
        )
        scoring_started = time.perf_counter()
        try:
            assessment = self.llm_analyzer.classify_hipo(incident_text, features, scoring_evidence[:48])
            assessment_provider = assessment.pop("_provider", "configured_model")
            assessment_mode = "structured_model"
        except Exception as exc:
            print(f"HIPO structured assessment unavailable; using lower-bound ratings: {exc}")
            assessment = self.fallback_assessment()
            assessment_provider = "deterministic_emergency"
            assessment_mode = "emergency_lower_bound_fallback"
        fact_extractor = getattr(self.llm_analyzer, "extract_hipo_scoring_facts", None)
        try:
            scoring_facts = fact_extractor(incident_text) if callable(fact_extractor) else self.fallback_scoring_facts(incident_text)
            facts_provider = scoring_facts.pop("_provider", scoring_facts.get("extraction_mode", "configured_model"))
        except Exception as exc:
            print(f"HIPO scoring fact extraction unavailable; using conservative facts: {exc}")
            scoring_facts = self.fallback_scoring_facts(incident_text)
            facts_provider = "deterministic_fallback"
        assessment, score_resolution, missing_information = self._resolve_scores(assessment, scoring_facts)

        verification = None
        verifier = getattr(self.llm_analyzer, "verify_hipo_scores", None)
        verifier_reasons = self._score_verifier_reasons(
            assessment,
            assessment_provider,
            evidence_grade,
            missing_information,
            incident_text,
        )
        verifier_invoked = False
        verifier_failed = False
        if (
            settings.score_verifier_enabled
            and verifier_reasons
            and bool(getattr(self.llm_analyzer, "cloud_available", False))
            and callable(verifier)
        ):
            try:
                verifier_invoked = True
                verification = verifier(incident_text, scoring_facts, assessment, scoring_evidence[:48])
                assessment, verified_corrections = self._apply_bounded_verification(assessment, verification)
            except Exception as exc:
                print(f"HIPO score verification unavailable; keeping resolved scores: {exc}")
                verifier_failed = True
                verified_corrections = []
        else:
            verified_corrections = []
        # Grounded deterministic constraints are authoritative and are applied
        # after the optional model verifier so hallucinated cross-dimension
        # corrections cannot undo explicit narrative evidence.
        assessment, narrative_corrections = self._apply_narrative_constraints(
            assessment, incident_text, scoring_facts
        )
        assessment, event_profile, event_evidence, event_corrections = (
            self._apply_event_profile_constraints(assessment, incident_text)
        )
        scoring_ms = round((time.perf_counter() - scoring_started) * 1000, 2)

        impact_fields = ("safety_impact", "damage_to_assets", "business_continuity",
                         "reputational_impact", "vip_safety_impact")
        validated_scores = self._validated_scores(assessment)
        max_impact = max(validated_scores[field] for field in impact_fields)
        likelihood = validated_scores["likelihood_of_more_severe_outcome"]
        rule_hipo = max_impact >= 4 and likelihood >= 4
        final = "HIPO" if rule_hipo else "Non-HIPO"
        qualifying = [
            label for field, label in (
                ("safety_impact", "Safety Impact"),
                ("damage_to_assets", "Damage to Assets"),
                ("business_continuity", "Business Continuity"),
                ("reputational_impact", "Reputational Impact"),
                ("vip_safety_impact", "VIP Safety Impact"),
            ) if validated_scores[field] >= 4
        ]
        trigger = (
            [*(f"{label} = {validated_scores[field]}" for field, label in (
                ("safety_impact", "Safety Impact"),
                ("damage_to_assets", "Damage to Assets"),
                ("business_continuity", "Business Continuity"),
                ("reputational_impact", "Reputational Impact"),
                ("vip_safety_impact", "VIP Safety Impact"),
            ) if label in qualifying), f"Likelihood = {likelihood}"]
            if rule_hipo else None
        )
        final_assessment = {
            **assessment,
            "maximum_impact_score": max_impact,
            "hipo_classification": final,
            "hipo_trigger": trigger,
        }

        total_ms = round((time.perf_counter() - started_at) * 1000, 2)
        verifier_unavailable = bool(
            verifier_failed
            or (
                settings.score_verifier_enabled
                and not verifier_invoked
                and set(verifier_reasons).intersection({
                    "retrieval_evidence_incomplete",
                    "retrieval_confidence_below_threshold",
                    "emergency_scoring_fallback",
                    "unresolved_scoring_facts",
                    "proposal_conflicts_with_exposure_proximity",
                })
            )
        )
        evidence_requires_review = bool(
            evidence_grade and not evidence_grade.get("sufficient")
        )
        return {
            "overall_hipo_classification": {
                "classification": final,
                "decision_basis": (
                    "At least one impact score is 4 or 5 and likelihood is 4 or 5."
                    if rule_hipo else
                    "The combined impact-and-likelihood HIPO trigger was not met."
                ),
                "rule_validated": True,
            },
            "hipo_assessment": final_assessment,
            "features": features,
            "scoring_facts": scoring_facts,
            "score_resolution": score_resolution,
            "review": {
                "required": (
                    bool(missing_information)
                    or evidence_requires_review
                    or verifier_unavailable
                    or bool((verification or {}).get("review_required"))
                ),
                "missing_information": missing_information,
                "assessment_mode": assessment_mode,
                "assessment_provider": assessment_provider,
                "facts_provider": facts_provider,
                "narrative_corrections": narrative_corrections,
                "event_profile": event_profile,
                "event_evidence": event_evidence,
                "event_corrections": event_corrections,
                "verification": verification,
                "score_verifier_invoked": verifier_invoked,
                "score_verifier_failed": verifier_failed,
                "score_verifier_trigger_reasons": verifier_reasons,
                "verified_corrections": verified_corrections,
                "stage_timings_ms": {
                    "feature_extraction": feature_ms,
                    "retrieval_and_policy": retrieval_ms,
                    "scoring_and_verification": scoring_ms,
                    "total": total_ms,
                },
            },
            "risk_feature_scores": {**{field: validated_scores[field] for field in impact_fields},
                                    "likelihood": likelihood, "maximum_impact": max_impact},
            "retrieval": {
                "similar_verified_cases": similar_cases,
                "exact_hazard_matches": hazard_matches,
                "applicable_rules": rules,
                "fused_evidence": fused[:8],
                "dimension_evidence": dimension_evidence,
                "complete_rubrics": complete_rubrics,
                "evidence_grade": evidence_grade,
                "corrective_retrieval_used": corrective_retrieval_used,
            },
        }
