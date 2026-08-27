"""Purpose: Tests test incident workflow fields behavior and expected regressions.

Used by: Executed by pytest as part of the automated regression suite.
"""

from backend.app.services.incident_workflow import IncidentWorkflow


def test_extracts_date_time_area_and_room_from_narrative():
    workflow = IncidentWorkflow()
    result = workflow._build_facts_result(
        "On 2026-08-12 at 10:30 PM, a guest slipped in the east corridor near room 204."
    )
    assert result["date"] == "2026-08-12"
    assert result["time"] == "10:30 PM"
    assert result["location"] == "east corridor"
    assert result["room_number"] == "204"


def test_missing_explicit_fields_remain_null():
    result = IncidentWorkflow()._build_facts_result("A guest slipped and recovered.")
    assert result["date"] is None
    assert result["time"] is None
    assert result["location"] is None
    assert result["room_number"] is None


def test_factual_summary_is_limited_to_two_sentences_and_55_words():
    narrative = (
        "A guest slipped beside the restaurant entrance and injured an ankle. "
        "An employee provided first aid and isolated the wet floor. "
        "The manager later reviewed staffing records and several unrelated background details."
    )

    summary = IncidentWorkflow()._fallback_incident_summary(narrative)

    assert summary == (
        "A guest slipped beside the restaurant entrance and injured an ankle. "
        "An employee provided first aid and isolated the wet floor."
    )
    assert len(summary.split()) <= 55


def test_fallback_summary_prioritizes_immediate_response_sentence():
    narrative = (
        "A guest slipped beside the restaurant entrance and injured an ankle. "
        "Several employees had started their evening shift. "
        "An employee provided first aid and isolated the wet floor."
    )

    summary = IncidentWorkflow()._fallback_incident_summary(narrative)

    assert "injured an ankle" in summary
    assert "provided first aid and isolated the wet floor" in summary
    assert "evening shift" not in summary


def test_cloud_feature_summary_is_concisely_capped_before_display():
    workflow = IncidentWorkflow()
    long_summary = " ".join(f"detail{index}" for index in range(60))

    result = workflow._build_facts_result(
        "A guest slipped.", shared_features={"incident_summary": long_summary}
    )

    assert len(result["incident_summary"].split()) == 55
    assert result["incident_summary"].endswith(".")


def test_intake_gate_reports_missing_mandatory_fields_before_taxonomy():
    class Analyzer:
        llm_analyzer = None

        @staticmethod
        def detect_incident_mechanism(_text):
            return {"primary_mechanism": "unknown", "matched_term": None}

    result = IncidentWorkflow().validate_intake("A brief incident occurred.", Analyzer())

    assert result["mandatory_complete"] is False
    assert set(result["missing_mandatory_information"]) == {
        "date", "time", "location", "primary_event", "primary_hazard",
    }
    assert result["domain"] is None
    assert result["subdomain"] is None
    assert result["event_type"] is None


def test_final_location_prefers_room_number_over_area():
    facts = IncidentWorkflow()._build_facts_result(
        "A guest slipped in the east corridor near room 204."
    )
    location = f"Room {facts['room_number']}" if facts.get("room_number") else facts.get("location")
    assert location == "Room 204"


def test_final_location_uses_area_when_room_is_absent():
    facts = IncidentWorkflow()._build_facts_result("A guest slipped in the east corridor.")
    location = f"Room {facts['room_number']}" if facts.get("room_number") else facts.get("location")
    assert location == "east corridor"


def test_location_stops_before_reporting_action():
    narrative = (
        "On 07/08/2026 at approximately 20:45 hrs, a guest dining at the "
        "speciality restaurant informed the service associate that she had an allergy."
    )
    facts = IncidentWorkflow()._build_facts_result(narrative)
    assert facts["location"] == "speciality restaurant"


def test_taxonomy_text_uses_only_the_initiating_event():
    narrative = (
        "A guest tripped on a raised flooring edge near a function room. "
        "The event occurred during a busy period and was identified by an employee. "
        "The affected area was isolated and operations continued."
    )

    assert IncidentWorkflow._initiating_event_text(narrative) == (
        "A guest tripped on a raised flooring edge near a function room."
    )


def test_event_routes_override_unrelated_semantic_taxonomy_candidate():
    approved = {
        ("Guest-Related Incidents", "Guest Medical Emergency"),
        ("Guest-Related Incidents", "Guest Slip, Trip & Fall"),
        ("Guest-Related Incidents", "Safety Incidents Involving Guests"),
        ("Guest-Related Incidents", "Theft or Loss of Guest Property"),
        ("Guest-Related Incidents", "Guest Complaints"),
        ("Guest-Related Incidents", "Fraudulent Activities by Guests"),
        ("Guest-Related Incidents", "Missing Person"),
        ("Occupational Health and Safety", "Fatigue Management"),
        ("Physical Security", "Theft and Vandalism"),
        ("Physical Security", "Surveillance Systems"),
        ("Road Safety", "Speeding & Safety Equipment Violations"),
    }

    class TaxonomyCollection:
        def find_one(self, query, _projection):
            if (
                query.get("chunk_type") == "taxonomy"
                and (query.get("domain"), query.get("subdomain")) in approved
                and query.get("active") is True
            ):
                return {"_id": 1}
            return None

    class Collection:
        def find_one(self, query, projection):
            return TaxonomyCollection().find_one(query, projection)

    class Retriever:
        collection = Collection()

    class Classifier:
        def classify(self, *_args, **_kwargs):
            return {"domain": "Physical Security", "subdomain": "Risk Management"}

    class Analyzer:
        retriever = Retriever()
        hybrid_taxonomy_classifier = Classifier()

    workflow = IncidentWorkflow()
    cases = (
        ("A guest developed neurological symptoms with no environmental trigger.", "Guest Medical Emergency"),
        ("A guest tripped on a raised flooring edge.", "Guest Slip, Trip & Fall"),
        ("A loose barrier moved while a guest leaned against it.", "Safety Incidents Involving Guests"),
        ("A guest reported a personal item missing from an unsecured location.", "Theft or Loss of Guest Property"),
        ("A guest raised a serious privacy complaint.", "Guest Complaints"),
        ("A guest used false payment information.", "Fraudulent Activities by Guests"),
        ("A child could not be located for a short period.", "Missing Person"),
    )
    for narrative, expected_subdomain in cases:
        result = workflow._build_taxonomy_result(narrative, Analyzer())
        assert result == {
            "domain": "Guest-Related Incidents",
            "subdomain": expected_subdomain,
        }

    fatigue = workflow._build_taxonomy_result(
        "A fatigued employee showed impaired alertness during safety-sensitive work.", Analyzer()
    )
    assert fatigue == {
        "domain": "Occupational Health and Safety",
        "subdomain": "Fatigue Management",
    }

    canonical_cases = (
        (
            "Hotel property was damaged or removed by an unidentified person.",
            "Physical Security",
            "Theft and Vandalism",
        ),
        (
            "A group of security cameras stopped recording for a limited period.",
            "Physical Security",
            "Surveillance Systems",
        ),
        (
            "A hotel vehicle carried passengers while the driver committed a serious road-safety violation.",
            "Road Safety",
            "Speeding & Safety Equipment Violations",
        ),
    )
    for narrative, expected_domain, expected_subdomain in canonical_cases:
        assert workflow._build_taxonomy_result(narrative, Analyzer()) == {
            "domain": expected_domain,
            "subdomain": expected_subdomain,
        }


def test_unapproved_taxonomy_candidate_never_reaches_workflow_result():
    class Collection:
        def find_one(self, _query, _projection):
            return None

        def aggregate(self, _pipeline):
            return []

    class Retriever:
        collection = Collection()

        def retrieve(self, *_args, **_kwargs):
            return []

    class Classifier:
        def classify(self, *_args, **_kwargs):
            return {"domain": "Invented Domain", "subdomain": "Invented Subdomain"}

    class Analyzer:
        retriever = Retriever()
        hybrid_taxonomy_classifier = Classifier()

    result = IncidentWorkflow()._build_taxonomy_result(
        "An incident occurred.", Analyzer()
    )

    assert result == {"domain": None, "subdomain": None}
