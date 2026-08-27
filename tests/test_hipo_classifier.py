"""Purpose: Tests test hipo classifier behavior and expected regressions.

Used by: Executed by pytest as part of the automated regression suite.
"""

import pytest

from backend.app.services.hipo_classifier import HipoClassifier


class Collection:
    def find(self, query, *_args, **_kwargs):
        if query.get("chunk_type") == "hipo_policy" and query.get("parameter"):
            parameter = query["parameter"]
            return [
                {
                    "chunk_id": f"{parameter}-{score}", "parameter": parameter,
                    "score": score, "text": f"{parameter} score {score}",
                }
                for score in range(1, 6)
            ]
        if query.get("chunk_type") == "hipo_policy":
            return []
        return [{"chunk_id": "hazard-1", "hazard_identified": "falling object"}]


class Retriever:
    collection = Collection()

    def __init__(self):
        self.parameters = []

    def retrieve(self, _query, *, chunk_type, **_kwargs):
        if _kwargs.get("parameter"):
            self.parameters.append(_kwargs["parameter"])
        if chunk_type == "historical_incident":
            return [
                {
                    "chunk_id": "case-1", "hipo_classification": "HIPO", "verified": True,
                    "safety_impact": 4, "business_continuity": 2,
                    "damage_to_assets": 1, "reputational_impact": 2,
                    "vip_safety": 1, "likelihood_of_more_severe_outcome": 4,
                },
                {
                    "chunk_id": "case-2", "hipo_classification": "Non-HIPO", "verified": True,
                    "safety_impact": 2, "business_continuity": 1,
                    "damage_to_assets": 1, "reputational_impact": 1,
                    "vip_safety": 1, "likelihood_of_more_severe_outcome": 2,
                },
            ]
        return [{"chunk_id": "rule-1", "search_text": "impact 4/5 and likelihood 4/5"}]


class Analyzer:
    def extract_hipo_features(self, _text):
        return {
            "hazard": "falling object",
            "exposure": "person below suspended load",
            "actual_outcome": "no injury",
            "energy_source": "gravity",
            "people_exposed": ["worker"],
            "critical_controls": ["exclusion zone"],
            "credible_worst_case": "fatal struck-by injury",
        }

    def classify_hipo(self, _text, _features, _evidence):
        return {
            "safety_impact": {"score": 4, "level": "Major", "reason": "Fatal struck-by outcome was credible."},
            "damage_to_assets": {"score": 1, "level": "Negligible", "reason": "No meaningful asset exposure."},
            "business_continuity": {"score": 2, "level": "Minor", "reason": "Only local interruption was credible."},
            "reputational_impact": {"score": 2, "level": "Minor", "reason": "Limited complaint potential."},
            "vip_safety_impact": {"score": 3, "level": "Moderate", "reason": "This deliberately tests the no-VIP override."},
            "likelihood_of_more_severe_outcome": {"score": 4, "level": "Major", "reason": "A small positional change could cause impact."},
        }


def test_full_pipeline_returns_rule_validated_hipo():
    result = HipoClassifier(Retriever(), Analyzer()).classify("A load fell near a worker")
    overall = result["overall_hipo_classification"]
    assert overall["classification"] == "HIPO"
    assert overall["rule_validated"] is True
    assert result["risk_feature_scores"]["maximum_impact"] == 4
    assert result["hipo_assessment"]["vip_safety_impact"]["score"] == 1
    assert result["hipo_assessment"]["hipo_trigger"] == ["Safety Impact = 4", "Likelihood = 4"]
    assert [item["chunk_id"] for item in result["retrieval"]["similar_verified_cases"]] == ["case-1", "case-2"]
    assert sum(len(rows) for rows in result["retrieval"]["complete_rubrics"].values()) == 30


def test_dimension_vector_search_does_not_require_parameter_filter_index():
    retriever = Retriever()
    HipoClassifier(retriever, Analyzer()).classify("A load fell near a worker")

    assert retriever.parameters == []


def test_dimension_specific_verified_examples_are_retrieved_for_all_parameters():
    classifier = HipoClassifier(Retriever(), Analyzer())
    candidates = classifier.retriever.retrieve(
        "A load fell near a worker", chunk_type="historical_incident"
    )

    examples = classifier._dimension_verified_examples(
        "A load fell near a worker", Analyzer().extract_hipo_features(""), candidates
    )

    assert set(examples) == set(HipoClassifier.DIMENSION_PARAMETERS)
    assert all(items for items in examples.values())
    assert all(
        item["dimension"] == field
        for field, items in examples.items()
        for item in items
    )


def test_weighted_example_vote_reports_confidence_and_distribution():
    examples = [
        {
            "chunk_id": "a", "verified": True, "score": 0.95,
            "incident_summary": "falling load above worker", "hazard": "falling object",
            "safety_impact": 4,
        },
        {
            "chunk_id": "b", "verified": True, "score": 0.80,
            "incident_summary": "falling load near worker", "hazard": "falling object",
            "safety_impact": 4,
        },
        {
            "chunk_id": "c", "verified": True, "score": 0.20,
            "incident_summary": "minor office event", "safety_impact": 2,
        },
    ]
    features = {"hazard": "falling object", "exposure": "worker beneath load"}

    vote = HipoClassifier._weighted_example_vote(
        "safety_impact", examples, "A falling load passed above a worker", features
    )

    assert vote["score"] == 4
    assert vote["confidence"] >= 0.62
    assert set(vote["distribution"]) == {"2", "4"}


def test_complete_rubrics_are_compacted_to_six_model_evidence_items():
    rubrics = {
        field: [
            {"parameter": parameter, "score_value": score, "severity": "Level", "text": f"score {score}"}
            for score in range(1, 6)
        ]
        for field, parameter in HipoClassifier.DIMENSION_PARAMETERS.items()
    }

    grouped = HipoClassifier._group_rubrics(rubrics)

    assert len(grouped) == 6
    assert all(len(item["rubric_levels"]) == 5 for item in grouped)


def test_severe_but_unlikely_does_not_meet_combined_trigger():
    analyzer = Analyzer()
    original = analyzer.classify_hipo

    def unlikely(*args):
        result = original(*args)
        result["safety_impact"] = {"score": 5, "level": "Catastrophic", "reason": "Multiple fatalities were credible."}
        result["likelihood_of_more_severe_outcome"] = {"score": 2, "level": "Minor", "reason": "Several additional failures were required."}
        return result

    analyzer.classify_hipo = unlikely
    result = HipoClassifier(Retriever(), analyzer).classify("A load fell near a worker")
    assert result["hipo_assessment"]["hipo_classification"] == "Non-HIPO"
    assert result["hipo_assessment"]["hipo_trigger"] is None


def test_actual_fatality_does_not_bypass_likelihood_rule():
    analyzer = Analyzer()
    features = analyzer.extract_hipo_features("incident")
    features["actual_outcome"] = "One employee died"
    result = HipoClassifier(Retriever(), analyzer).classify("incident", features=features)
    assert result["hipo_assessment"]["maximum_impact_score"] == 4
    assert result["hipo_assessment"]["hipo_classification"] == "HIPO"


def test_timeout_fallback_features_are_safe_and_structured():
    features = HipoClassifier.fallback_features(
        "A worker had no injury after a chemical container fell."
    )
    assert features["normalized_incident"]
    assert features["actual_outcome"] == "no injury"
    assert features["energy_source"] in {"chemical energy", "gravity"}
    assert features["extraction_mode"] == "deterministic_fallback"


def test_all_six_hipo_scores_are_required():
    decision = Analyzer().classify_hipo(None, None, None)
    decision["damage_to_assets"] = None
    try:
        HipoClassifier._validated_scores(decision)
    except ValueError as exc:
        assert "damage_to_assets" in str(exc)
    else:
        raise AssertionError("Missing HIPO score should be rejected")


def test_corrective_retrieval_runs_once_when_cloud_grader_requests_it():
    class CorrectiveAnalyzer(Analyzer):
        cloud_available = True
        grade_calls = 0

        def grade_retrieval_evidence(self, *_args):
            self.grade_calls += 1
            return {
                "sufficient": self.grade_calls > 1,
                "corrective_query": "safety score boundary" if self.grade_calls == 1 else None,
                "relevant_chunk_ids": [],
                "missing_evidence": [],
                "confidence": 0.8,
            }

    analyzer = CorrectiveAnalyzer()
    result = HipoClassifier(Retriever(), analyzer).classify("A load fell near a worker")

    assert result["retrieval"]["corrective_retrieval_used"] is True
    assert analyzer.grade_calls == 2


def test_deterministic_crag_grades_evidence_when_cloud_is_unavailable():
    result = HipoClassifier(Retriever(), Analyzer()).classify("A load fell near a worker")

    grade = result["retrieval"]["evidence_grade"]
    assert grade["provider"] == "deterministic_crag"
    assert grade["sufficient"] is True
    assert grade["confidence"] >= 0.75


def test_retrieval_critic_filters_unapproved_scoring_evidence():
    evidence = [
        {"chunk_id": "rubric", "channel": "complete_rubric"},
        {"chunk_id": "accepted", "channel": "verified_case"},
        {"chunk_id": "rejected", "channel": "verified_case"},
    ]

    filtered = HipoClassifier._filter_grounded_evidence(
        evidence,
        {"relevant_chunk_ids": ["accepted"]},
    )

    assert [item["chunk_id"] for item in filtered] == ["rubric", "accepted"]


def test_second_agent_runs_for_near_threshold_scores_when_cloud_is_available():
    class TwoAgentAnalyzer(Analyzer):
        cloud_available = True
        verifier_calls = 0

        def grade_retrieval_evidence(self, _incident, _features, evidence):
            return {
                "sufficient": True,
                "corrective_query": None,
                "relevant_chunk_ids": [
                    item["chunk_id"] for item in evidence if item.get("chunk_id")
                ],
                "missing_evidence": [],
                "confidence": 0.9,
            }

        def verify_hipo_scores(self, *_args):
            self.verifier_calls += 1
            return {
                "accepted": True,
                "review_required": False,
                "corrected_scores": {},
                "reasons": {},
            }

    analyzer = TwoAgentAnalyzer()
    result = HipoClassifier(Retriever(), analyzer).classify("A load fell near a worker")

    assert analyzer.verifier_calls == 1
    assert result["review"]["score_verifier_invoked"] is True
    assert "score_near_hipo_threshold" in result["review"]["score_verifier_trigger_reasons"]


def test_second_agent_is_skipped_for_fully_supported_low_risk_scores():
    class LowRiskAnalyzer(Analyzer):
        cloud_available = True
        verifier_calls = 0

        def grade_retrieval_evidence(self, _incident, _features, evidence):
            return {
                "sufficient": True,
                "corrective_query": None,
                "relevant_chunk_ids": [
                    item["chunk_id"] for item in evidence if item.get("chunk_id")
                ],
                "missing_evidence": [],
                "confidence": 0.95,
            }

        def classify_hipo(self, *_args):
            return {
                "safety_impact": {"score": 2, "level": "Minor", "reason": "Minor exposure."},
                "damage_to_assets": {"score": 2, "level": "Minor", "reason": "Minor repair."},
                "business_continuity": {"score": 2, "level": "Minor", "reason": "Minor delay."},
                "reputational_impact": {"score": 2, "level": "Minor", "reason": "Minor concern."},
                "vip_safety_impact": {"score": 1, "level": "Negligible", "reason": "No VIP."},
                "likelihood_of_more_severe_outcome": {"score": 2, "level": "Minor", "reason": "Additional failures required."},
                "_provider": "gemini",
            }

        def extract_hipo_scoring_facts(self, _text):
            return {
                "safety_potential": "minor",
                "operational_potential": "minor_delay",
                "asset_potential": "minor_repair",
                "reputation_potential": "minor_addressable",
                "vip_involved": False,
                "escalation_proximity": "multiple_additional_failures",
                "supporting_phrases": {},
                "_provider": "gemini",
            }

        def verify_hipo_scores(self, *_args):
            self.verifier_calls += 1
            raise AssertionError("Low-risk supported scores should not invoke Agent 2")

    analyzer = LowRiskAnalyzer()
    result = HipoClassifier(Retriever(), analyzer).classify("A minor contained incident")

    assert analyzer.verifier_calls == 0
    assert result["review"]["score_verifier_invoked"] is False
    assert result["review"]["score_verifier_trigger_reasons"] == []


def test_dimension_retrieval_and_missing_facts_are_exposed_for_review():
    result = HipoClassifier(Retriever(), Analyzer()).classify("A load fell near a worker")

    assert set(result["retrieval"]["dimension_evidence"]) == set(HipoClassifier.DIMENSION_QUERIES)
    assert result["review"]["required"] is True
    assert "operational_potential" in result["review"]["missing_information"]
    assert result["score_resolution"]["business_continuity"]["status"] == "provisional"
    assert result["score_resolution"]["vip_safety_impact"]["status"] == "determined"


def test_explicit_scoring_facts_override_model_proposals_deterministically():
    class FactAnalyzer(Analyzer):
        def extract_hipo_scoring_facts(self, _text):
            return {
                "safety_potential": "medical_attention",
                "operational_potential": "partial_shutdown",
                "asset_potential": "minor_repair",
                "reputation_potential": "significant_publicity",
                "vip_involved": False,
                "escalation_proximity": "small_change",
                "supporting_phrases": {"operational_potential": ["partial shutdown"]},
            }

    result = HipoClassifier(Retriever(), FactAnalyzer()).classify("Partial shutdown; no VIP involved")

    scores = result["risk_feature_scores"]
    assert scores["safety_impact"] == 4
    assert scores["business_continuity"] == 4
    assert scores["damage_to_assets"] == 2
    assert scores["reputational_impact"] == 4
    assert scores["likelihood"] == 4
    assert result["review"]["required"] is False
    assert result["score_resolution"]["business_continuity"]["supporting_phrases"] == ["partial shutdown"]


def test_low_actual_fact_cannot_reduce_credible_potential_score():
    assessment = Analyzer().classify_hipo(None, None, None)
    facts = {
        "safety_potential": "none",
        "operational_potential": "none",
        "asset_potential": "none",
        "reputation_potential": "none",
        "vip_involved": False,
        "escalation_proximity": "remote",
        "supporting_phrases": {},
    }

    resolved, resolution, _missing = HipoClassifier._resolve_scores(assessment, facts)

    assert resolved["safety_impact"]["score"] == 4
    assert resolved["business_continuity"]["score"] == 2
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == 4
    assert resolution["safety_impact"]["status"] == "provisional"


def test_verifier_cannot_change_a_score_by_more_than_one_level():
    assessment = Analyzer().classify_hipo(None, None, None)
    verification = {
        "corrected_scores": {"safety_impact": 2, "business_continuity": 3},
        "reasons": {"business_continuity": "Policy evidence supports moderate disruption."},
    }

    corrected, applied = HipoClassifier._apply_bounded_verification(assessment, verification)

    assert corrected["safety_impact"]["score"] == 4
    assert corrected["business_continuity"]["score"] == 3
    assert applied == ["business_continuity"]


def test_fallback_scoring_facts_respects_explicit_no_vip_statement():
    facts = HipoClassifier.fallback_scoring_facts(
        "A person was exposed to the hazard. No VIP was involved."
    )

    assert facts["vip_involved"] is False


def test_fallback_scoring_facts_defaults_vip_to_not_involved_when_unmentioned():
    facts = HipoClassifier.fallback_scoring_facts(
        "A person was exposed to the hazard."
    )

    assert facts["vip_involved"] is False


def test_fallback_scoring_facts_extracts_independent_explicit_rubric_anchors():
    facts = HipoClassifier.fallback_scoring_facts(
        "A worker required medical attention. Operations continued with a temporary "
        "workaround. Equipment replacement was required and the event received negative "
        "publicity. The worker was directly exposed moments before control was restored."
    )

    assert facts["safety_potential"] == "medical_attention"
    assert facts["operational_potential"] == "continued_with_adjustments"
    assert facts["asset_potential"] == "repair_or_replacement"
    assert facts["reputation_potential"] == "negative_attention"
    assert facts["escalation_proximity"] == "small_change"
    assert set(facts["supporting_phrases"]) == {
        "safety_potential", "operational_potential", "asset_potential",
        "reputation_potential", "escalation_proximity",
    }


def test_evidence_fallback_uses_facts_then_verified_example_median():
    facts = HipoClassifier.fallback_scoring_facts(
        "A worker required medical attention. No VIP was involved."
    )
    examples = [
        {
            "verified": True, "safety_impact": 5, "business_continuity": 2,
            "damage_to_assets": 3, "reputational_impact": 2,
            "likelihood_of_more_severe_outcome": 3,
        },
        {
            "verified": True, "safety_impact": 4, "business_continuity": 3,
            "damage_to_assets": 2, "reputational_impact": 3,
            "likelihood_of_more_severe_outcome": 4,
        },
    ]

    assessment = HipoClassifier._evidence_fallback_assessment(facts, examples)

    assert assessment["safety_impact"]["score"] == 3
    assert assessment["business_continuity"]["score"] == 3
    assert assessment["damage_to_assets"]["score"] == 3
    assert assessment["reputational_impact"]["score"] == 3
    assert assessment["likelihood_of_more_severe_outcome"]["score"] == 4
    assert assessment["vip_safety_impact"]["score"] == 1


def test_fresh_rescore_can_replace_placeholder_by_more_than_one_level():
    assessment = HipoClassifier.fallback_assessment()
    verification = {
        "corrected_scores": {"safety_impact": 4, "likelihood_of_more_severe_outcome": 4},
        "reasons": {"safety_impact": "Major-injury mechanism was directly supported."},
    }

    corrected, applied = HipoClassifier._apply_bounded_verification(
        assessment, verification, allow_fresh_scores=True
    )

    assert corrected["safety_impact"]["score"] == 4
    assert corrected["likelihood_of_more_severe_outcome"]["score"] == 4
    assert applied == ["safety_impact", "likelihood_of_more_severe_outcome"]


def test_independent_parameter_scorer_applies_confident_scores_and_abstains():
    class ParameterAnalyzer(Analyzer):
        cloud_available = True

        def score_hipo_parameter(
            self, _incident, parameter, _provisional, _rubric, _examples
        ):
            if parameter == "damage_to_assets":
                return {
                    "score": None, "confidence": 0.4,
                    "lower_boundary": 1, "upper_boundary": 2,
                    "why_selected": "Evidence is incomplete.",
                    "why_not_adjacent": "Damage extent was not stated.",
                    "missing_information": ["repair extent"],
                }
            return {
                "score": 3, "confidence": 0.9,
                "lower_boundary": 2, "upper_boundary": 4,
                "why_selected": "The score-3 boundary is supported.",
                "why_not_adjacent": "The score-4 boundary is not supported.",
                "missing_information": [],
            }

    classifier = HipoClassifier(Retriever(), ParameterAnalyzer())
    assessment = Analyzer().classify_hipo(None, None, None)
    facts = HipoClassifier.fallback_scoring_facts("A worker was injured. A VIP was involved.")
    rubrics = classifier._complete_rubrics()
    candidates = classifier.retriever.retrieve(
        "A worker was injured", chunk_type="historical_incident"
    )
    examples = classifier._dimension_verified_examples(
        "A worker was injured", Analyzer().extract_hipo_features(""), candidates
    )

    scored, decisions, abstained = classifier._apply_independent_parameter_scoring(
        "A worker was injured", Analyzer().extract_hipo_features(""), facts,
        assessment, "gemini", rubrics, examples,
    )

    assert scored["safety_impact"]["score"] == 3
    assert decisions["safety_impact"]["provider"] == "gemini_parameter_scorer"
    assert decisions["safety_impact"]["adjacent_boundary"]["upper"] == 4
    assert "damage_to_assets" in abstained


def test_batched_parameter_scorer_is_called_once_for_all_dimensions():
    class BatchAnalyzer(Analyzer):
        cloud_available = True

        def __init__(self):
            self.batch_calls = 0

        def score_hipo_parameters(
            self, _incident, _provisional_scores, _rubrics, _examples
        ):
            self.batch_calls += 1
            return {
                field: {
                    "score": 3,
                    "confidence": 0.9,
                    "lower_boundary": 2,
                    "upper_boundary": 4,
                    "why_selected": "The score-3 boundary is supported.",
                    "why_not_adjacent": "Adjacent boundaries are unsupported.",
                    "missing_information": [],
                }
                for field in HipoClassifier.DIMENSION_PARAMETERS
            }

        def score_hipo_parameter(self, *_args, **_kwargs):
            raise AssertionError("The legacy per-parameter scorer must not run")

    analyzer = BatchAnalyzer()
    classifier = HipoClassifier(Retriever(), analyzer)
    features = analyzer.extract_hipo_features("")
    facts = HipoClassifier.fallback_scoring_facts("A worker was injured.")
    candidates = classifier.retriever.retrieve(
        "A worker was injured", chunk_type="historical_incident"
    )

    scored, decisions, _ = classifier._apply_independent_parameter_scoring(
        "A worker was injured", features, facts,
        analyzer.classify_hipo(None, None, None), "gemini",
        classifier._complete_rubrics(),
        classifier._dimension_verified_examples(
            "A worker was injured", features, candidates
        ),
    )

    assert analyzer.batch_calls == 1
    assert scored["safety_impact"]["score"] == 3
    assert decisions["safety_impact"]["provider"] == "gemini_parameter_scorer"


def test_narrative_constraints_calibrate_direct_exposure_without_cross_dimension_invention():
    assessment = Analyzer().classify_hipo(None, None, None)
    assessment["safety_impact"]["score"] = 3
    assessment["safety_impact"]["level"] = "Moderate"
    assessment["business_continuity"] = {
        "score": 5, "level": "Catastrophic", "reason": "unsupported"
    }
    assessment["reputational_impact"] = {
        "score": 4, "level": "Major", "reason": "unsupported"
    }
    assessment["likelihood_of_more_severe_outcome"] = {
        "score": 3, "level": "Moderate", "reason": "under-scored"
    }
    narrative = (
        "A guest was directly exposed to a loose barrier moments before control was restored. "
        "Operations continued with a temporary workaround. No VIP was involved."
    )

    resolved, changes = HipoClassifier._apply_narrative_constraints(assessment, narrative)

    assert resolved["safety_impact"]["score"] == 4
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == 4
    assert resolved["business_continuity"]["score"] == 2
    assert resolved["reputational_impact"]["score"] == 2
    assert resolved["vip_safety_impact"]["score"] == 1
    assert changes


def test_medical_event_keeps_safety_moderate_while_proximity_raises_likelihood():
    assessment = Analyzer().classify_hipo(None, None, None)
    assessment["safety_impact"] = {
        "score": 3, "level": "Moderate", "reason": "medical symptoms"
    }
    assessment["business_continuity"] = {
        "score": 5, "level": "Catastrophic", "reason": "unsupported"
    }
    assessment["damage_to_assets"] = {
        "score": 2, "level": "Minor", "reason": "unsupported"
    }
    assessment["reputational_impact"] = {
        "score": 4, "level": "Major", "reason": "unsupported"
    }
    assessment["likelihood_of_more_severe_outcome"] = {
        "score": 3, "level": "Moderate", "reason": "under-scored"
    }
    narrative = (
        "A guest suddenly developed neurological symptoms with no environmental trigger. "
        "A person had been directly exposed to the hazard moments before control was restored. "
        "Operations continued with a temporary workaround. No VIP was involved."
    )

    resolved, _changes = HipoClassifier._apply_narrative_constraints(assessment, narrative)

    assert resolved["safety_impact"]["score"] == 3
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == 4
    assert resolved["business_continuity"]["score"] == 1
    assert resolved["damage_to_assets"]["score"] == 1
    assert resolved["reputational_impact"]["score"] == 1


def test_structural_exposure_raises_safety_without_raising_likelihood():
    assessment = Analyzer().classify_hipo(None, None, None)
    assessment["safety_impact"] = {
        "score": 3, "level": "Moderate", "reason": "under-scored"
    }
    assessment["likelihood_of_more_severe_outcome"] = {
        "score": 3, "level": "Moderate", "reason": "not narrowly avoided"
    }
    narrative = (
        "A decorative barrier became loose while a guest was leaning against it. "
        "No evidence showed that a severe outcome was narrowly avoided."
    )

    resolved, _changes = HipoClassifier._apply_narrative_constraints(assessment, narrative)

    assert resolved["safety_impact"]["score"] == 4
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == 3


def test_decorative_barrier_wording_and_detection_timing_are_calibrated():
    base = Analyzer().classify_hipo(None, None, None)
    base["safety_impact"] = {"score": 3, "level": "Moderate", "reason": "under-scored"}
    base["business_continuity"] = {"score": 5, "level": "Catastrophic", "reason": "unsupported"}
    base["reputational_impact"] = {"score": 4, "level": "Major", "reason": "unsupported"}
    event = "A decorative barrier beside a public seating area became loose while a guest was leaning against it. "

    immediate, _ = HipoClassifier._apply_narrative_constraints(
        base,
        event + "An employee immediately stopped the activity. Operations continued.",
    )
    patrol, _ = HipoClassifier._apply_narrative_constraints(
        base,
        event + "It was identified by a routine patrol or inspection. Operations continued.",
    )

    assert immediate["safety_impact"]["score"] == 4
    assert immediate["business_continuity"]["score"] == 2
    assert immediate["reputational_impact"]["score"] == 2
    assert patrol["safety_impact"]["score"] == 4
    assert patrol["business_continuity"]["score"] == 3
    assert patrol["reputational_impact"]["score"] == 3


@pytest.mark.parametrize(
    ("narrative", "profile", "expected"),
    [
        (
            "A declared allergen was mistakenly included in a guest meal. A person was directly exposed moments before control was restored.",
            "allergen_food_safety", {"safety_impact": 4, "reputational_impact": 4, "likelihood_of_more_severe_outcome": 5},
        ),
        (
            "A worker was nearly exposed to a concentrated cleaning chemical. A person was directly exposed moments before control was restored.",
            "chemical_exposure", {"safety_impact": 4, "business_continuity": 2, "likelihood_of_more_severe_outcome": 4},
        ),
        (
            "A fatigued employee showed impaired alertness during safety-sensitive work. A person was directly exposed moments before control was restored.",
            "fatigue_safety_sensitive_work", {"safety_impact": 4, "damage_to_assets": 2, "likelihood_of_more_severe_outcome": 4},
        ),
        (
            "A guest reported a high-value personal item missing from an unsecured location. An employee immediately stopped the activity.",
            "theft_or_loss_guest_property", {"safety_impact": 1, "damage_to_assets": 3, "likelihood_of_more_severe_outcome": 2},
        ),
        (
            "A guest attempted to use false payment or identity information. It was identified by a routine patrol or inspection.",
            "guest_fraud", {"safety_impact": 1, "damage_to_assets": 3, "reputational_impact": 3},
        ),
        (
            "Two guests argued loudly and security intervened before serious violence occurred. A person was directly exposed moments before control was restored.",
            "guest_dispute_disturbance", {"safety_impact": 3, "business_continuity": 2, "damage_to_assets": 2, "reputational_impact": 3, "likelihood_of_more_severe_outcome": 4},
        ),
        (
            "A child or dependent guest could not be located for a short period. An employee immediately stopped the activity.",
            "missing_child_or_dependent", {"safety_impact": 2, "business_continuity": 2, "likelihood_of_more_severe_outcome": 2},
        ),
        (
            "A guest raised a serious service/privacy complaint and threatened public escalation. An employee immediately stopped the activity.",
            "guest_service_privacy_complaint", {"safety_impact": 1, "reputational_impact": 3, "likelihood_of_more_severe_outcome": 2},
        ),
        (
            "An employee slipped on a back-of-house walking surface. It was identified by a routine patrol or inspection.",
            "workplace_slip_trip_fall", {"safety_impact": 3, "business_continuity": 2, "reputational_impact": 1},
        ),
        (
            "Repeated manual handling caused musculoskeletal strain. An employee immediately stopped the activity.",
            "workplace_ergonomics", {"safety_impact": 2, "business_continuity": 1, "likelihood_of_more_severe_outcome": 2},
        ),
    ],
)
def test_grounded_event_profiles(narrative, profile, expected):
    assessment = Analyzer().classify_hipo(None, None, None)
    resolved, detected, evidence, corrections = HipoClassifier._apply_event_profile_constraints(
        assessment, narrative
    )

    assert detected == profile
    assert evidence
    assert corrections
    for field, score in expected.items():
        assert resolved[field]["score"] == score


@pytest.mark.parametrize(
    "narrative",
    [
        "The restaurant reviewed its allergen policy during a meeting.",
        "Cleaning chemicals remained sealed in the storage room.",
        "An employee mentioned feeling tired after completing ordinary office work.",
        "A guest asked where lost-property reports should be submitted.",
        "The payment terminal received a scheduled software update.",
        "Two guests discussed a previous argument calmly with management.",
        "A child waited with their parent in the lobby.",
        "A guest praised the privacy service.",
        "An employee walked across the back-of-house surface without incident.",
        "Manual-handling training was scheduled for next month.",
    ],
)
def test_event_profiles_do_not_trigger_without_incident_evidence(narrative):
    assessment = Analyzer().classify_hipo(None, None, None)
    resolved, profile, evidence, corrections = HipoClassifier._apply_event_profile_constraints(
        assessment, narrative
    )

    assert profile is None
    assert evidence == []
    assert corrections == []
    assert resolved == assessment


def test_explicit_no_narrow_avoidance_overrides_phrase_keyword():
    assessment = Analyzer().classify_hipo(None, None, None)
    narrative = (
        "A declared allergen was mistakenly included in a guest meal. "
        "No evidence showed that a severe outcome was narrowly avoided."
    )

    resolved, profile, _evidence, _corrections = (
        HipoClassifier._apply_event_profile_constraints(assessment, narrative)
    )

    assert profile == "allergen_food_safety"
    assert resolved["safety_impact"]["score"] == 4
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == 3


@pytest.mark.parametrize(
    ("narrative", "profile", "expected_scores"),
    [
        (
            "A newly hired worker attempted an unfamiliar hazardous task without full training. "
            "A person had been directly exposed to the hazard moments before control was restored.",
            "untrained_worker_hazardous_task",
            (4, 1, 2, 1, 4),
        ),
        (
            "Industrial equipment behaved unexpectedly while an employee was nearby. "
            "A person had been directly exposed to the hazard moments before control was restored.",
            "unexpected_equipment_person_nearby",
            (4, 3, 3, 1, 4),
        ),
        (
            "A hotel vehicle carried passengers while the driver committed a serious road-safety violation. "
            "A person had been directly exposed to the hazard moments before control was restored.",
            "passenger_vehicle_safety_violation",
            (4, 3, 2, 3, 4),
        ),
        (
            "Guests were forced into an active vehicle path because the designated pedestrian route was obstructed. "
            "A person had been directly exposed to the hazard moments before control was restored.",
            "pedestrian_forced_into_vehicle_path",
            (4, 1, 2, 2, 4),
        ),
    ],
)
def test_grounded_major_safety_profiles(narrative, profile, expected_scores):
    assessment = Analyzer().classify_hipo(None, None, None)

    resolved, selected_profile, evidence, _corrections = (
        HipoClassifier._apply_event_profile_constraints(assessment, narrative)
    )

    assert selected_profile == profile
    assert evidence
    safety, assets, continuity, reputation, likelihood = expected_scores
    assert resolved["safety_impact"]["score"] == safety
    assert resolved["damage_to_assets"]["score"] == assets
    assert resolved["business_continuity"]["score"] == continuity
    assert resolved["reputational_impact"]["score"] == reputation
    assert resolved["likelihood_of_more_severe_outcome"]["score"] == likelihood


@pytest.mark.parametrize(
    "narrative",
    [
        "A newly hired worker attended a fully supervised training demonstration.",
        "Industrial equipment was inspected while isolated and no person was nearby.",
        "An empty hotel vehicle was parked after a routine inspection.",
        "Guests used the designated pedestrian route without obstruction.",
    ],
)
def test_major_safety_profiles_require_incident_mechanism(narrative):
    assessment = Analyzer().classify_hipo(None, None, None)

    resolved, profile, evidence, corrections = HipoClassifier._apply_event_profile_constraints(
        assessment, narrative
    )

    assert profile is None
    assert evidence == []
    assert corrections == []
    assert resolved == assessment


def test_complete_profile_short_circuit_needs_no_retrieval_or_model_calls(monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.hipo_classifier.settings.hipo_profile_short_circuit_enabled",
        True,
    )
    class UnusedDependency:
        def __getattr__(self, name):
            raise AssertionError(f"Short-circuit unexpectedly accessed {name}")

    classifier = HipoClassifier(UnusedDependency(), UnusedDependency())
    narrative = (
        "A declared allergen was mistakenly included in a guest meal. "
        "A person had been directly exposed to the hazard moments before control was restored. "
        "No VIP was involved."
    )

    result = classifier.classify(narrative)

    assert result["overall_hipo_classification"]["classification"] == "HIPO"
    assert result["risk_feature_scores"]["safety_impact"] == 4
    assert result["risk_feature_scores"]["likelihood"] == 5
    assert result["review"]["assessment_mode"] == "complete_event_profile_short_circuit"
    assert result["retrieval"]["short_circuited"] is True
    assert result["review"]["stage_timings_ms"]["total"] >= 0


def test_profile_without_explicit_vip_status_does_not_short_circuit():
    narrative = "A declared allergen was mistakenly included in a guest meal."

    assert HipoClassifier._complete_profile_result(narrative, 0.0) is None


def test_isolated_guest_trip_has_minor_continuity_and_reputation_caps():
    assessment = Analyzer().classify_hipo(None, None, None)
    assessment["business_continuity"] = {
        "score": 5, "level": "Catastrophic", "reason": "unsupported"
    }
    assessment["reputational_impact"] = {
        "score": 4, "level": "Major", "reason": "unsupported"
    }

    resolved, _changes = HipoClassifier._apply_narrative_constraints(
        assessment,
        "A guest tripped on a raised flooring edge. Operations continued. No VIP was involved.",
    )

    assert resolved["business_continuity"]["score"] == 2
    assert resolved["reputational_impact"]["score"] == 2
