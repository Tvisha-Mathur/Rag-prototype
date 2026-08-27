"""Purpose: Implements the incident workflow application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4


class IncidentWorkflow:
    """
    Controls the step-by-step incident review flow.

    The next step is only shown after the user confirms
    the current step.
    """

    STEPS = ["final_review"]

    STEP_TITLES = {
        "facts": "Incident Facts",
        "taxonomy": "Domain & Subdomain",
        "severity_impact": "Impact & Severity",
        "actual_near_miss": "Actual / Near Miss",
        "affected_parties": "Affected Parties",
        "hipo": "HIPO",
        "final_review": "Final Review",
    }

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    def create_session(
        self,
        incident_text: str,
    ) -> dict[str, Any]:
        session_id = str(uuid4())

        self.sessions[session_id] = {
            "session_id": session_id,
            "incident_text": incident_text,
            "current_step_index": 0,
            "confirmed": {},
            "pending": None,
            "corrections": {},
            "completed": False,
        }

        return self.sessions[session_id]

    def get_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        session = self.sessions.get(session_id)

        if session is None:
            raise ValueError(
                "Incident workflow session not found."
            )

        return session

    def get_current_step(
        self,
        session_id: str,
    ) -> str:
        session = self.get_session(session_id)

        index = session["current_step_index"]

        if index >= len(self.STEPS):
            return "completed"

        return self.STEPS[index]

    def step_number(self, step: str) -> int:
        if step in self.STEPS:
            return self.STEPS.index(step) + 1
        return 0

    def step_title(self, step: str) -> str:
        return self.STEP_TITLES.get(step, step.replace("_", " ").title())

    def set_pending_result(
        self,
        session_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.get_session(session_id)

        step = self.get_current_step(session_id)

        session["pending"] = {
            "step": step,
            "result": result,
        }

        return {
            "session_id": session_id,
            "step": step,
            "step_number": self.step_number(step),
            "step_title": self.step_title(step),
            "result": result,
            "awaiting_confirmation": True,
            "question": self._confirmation_question(step),
            "completed": False,
        }

    def process_current_step(
        self,
        session_id: str,
        analyzer: Any | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        step = self.get_current_step(session_id)

        if step == "completed":
            session["completed"] = True
            return {
                "session_id": session_id,
                "completed": True,
                "confirmed": session["confirmed"],
            }

        result = self._generate_step_result(
            session=session,
            step=step,
            analyzer=analyzer,
        )

        return self.set_pending_result(
            session_id=session_id,
            result=result,
        )

    def confirm_current_step(
        self,
        session_id: str,
        approved: bool,
        correction: dict[str, Any] | None = None,
        analyzer: Any | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)

        pending = session.get("pending")

        if pending is None:
            raise ValueError(
                "There is no pending step to confirm."
            )

        step = pending["step"]

        if approved:
            session["confirmed"][step] = pending["result"]
        else:
            if not correction:
                return {
                    "session_id": session_id,
                    "step": step,
                    "step_number": self.step_number(step),
                    "step_title": self.step_title(step),
                    "awaiting_correction": True,
                    "message": (
                        "Please provide the correct value "
                        "for this step."
                    ),
                }

            session["corrections"][step] = correction
            session["confirmed"][step] = correction

        session["pending"] = None
        session["current_step_index"] += 1

        if (
            session["current_step_index"]
            >= len(self.STEPS)
        ):
            session["completed"] = True
            return {
                "session_id": session_id,
                "completed": True,
                "confirmed": session["confirmed"],
            }

        return self.process_current_step(
            session_id=session_id,
            analyzer=analyzer,
        )

    def _generate_step_result(
        self,
        *,
        session: dict[str, Any],
        step: str,
        analyzer: Any | None,
    ) -> dict[str, Any]:
        incident_text = session.get("incident_text", "")

        if step == "facts":
            return self._build_facts_result(incident_text, analyzer)

        if step == "taxonomy":
            return self._build_taxonomy_result(incident_text, analyzer)

        if step == "severity_impact":
            return self._build_severity_result(incident_text, session, analyzer)

        if step == "actual_near_miss":
            return self._build_actual_near_miss_result(incident_text)

        if step == "affected_parties":
            return self._build_affected_parties_result(incident_text)

        if step == "hipo":
            return self._build_hipo_result(incident_text, analyzer)

        if step == "final_review":
            return self._build_complete_analysis(session, analyzer)

        return {"summary": incident_text, "status": "pending"}

    def _build_facts_result(
        self,
        incident_text: str,
        analyzer: Any | None = None,
        shared_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = incident_text.strip()
        date_match = re.search(
            r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]+ \d{1,2}, \d{4})\b",
            text,
        )
        time_match = re.search(
            r"\b(\d{1,2}:\d{2}(?:\s?[AP]M)?)\b",
            text,
            re.IGNORECASE,
        )
        location_matches = re.finditer(
            r"\b(?:at|in|inside|outside|near)\s+(?:the\s+)?([A-Za-z0-9 #&'/-]+?)"
            r"(?=\s+(?:at|on|during|while|when|where|informed|reported|stated|said|"
            r"noticed|observed|found|complained|developed|requested|asked|called|"
            r"entered|exited|slipped|fell|was|were)\b|[,.]|$)",
            text,
            re.IGNORECASE,
        )
        room_match = re.search(
            r"\b(?:room|rm)\s*(?:no\.?|number|#)?\s*([A-Za-z0-9-]+)\b",
            text,
            re.IGNORECASE,
        )
        location_value = None
        for match in location_matches:
            candidate = match.group(1).strip()
            if re.fullmatch(
                r"(?:approximately\s+)?\d{1,2}:\d{2}(?:\s?[AP]M|\s*hrs?)?",
                candidate,
                re.IGNORECASE,
            ):
                continue
            if re.fullmatch(r"(?:room|rm)\s*(?:no\.?|number|#)?\s*[A-Za-z0-9-]+", candidate, re.IGNORECASE):
                continue
            location_value = candidate
            break
        if location_value:
            location_value = re.sub(
                r"\s+(?:near|outside|inside)?\s*(?:room|rm)\s*(?:no\.?|number|#)?\s*[A-Za-z0-9-]+.*$",
                "",
                location_value,
                flags=re.IGNORECASE,
            ).strip() or None
        action_match = re.search(
            r"(?:immediate action|immediate actions|actions taken|responded by|was taken|taken immediately)[:\s]+(.+)",
            text,
            re.IGNORECASE,
        )
        persons = []
        for token in ["guest", "employee", "contractor", "visitor", "driver", "child"]:
            if token in text.lower():
                persons.append(token.title())

        summary = (shared_features or {}).get("incident_summary") or self._fallback_incident_summary(text)
        llm_analyzer = getattr(analyzer, "llm_analyzer", None)
        if llm_analyzer is not None and shared_features is None:
            try:
                summary = llm_analyzer.generate_incident_summary(text)
            except Exception as exc:
                print(f"Incident summarization failed; using fallback: {exc}")

        return {
            "date": date_match.group(0) if date_match else None,
            "time": time_match.group(0) if time_match else None,
            "location": location_value,
            "room_number": room_match.group(1).upper() if room_match else None,
            "incident_summary": summary,
            "affected_persons": persons or ["Not explicitly named"],
            "immediate_action_taken": action_match.group(1).strip() if action_match else None,
        }

    def _fallback_incident_summary(self, text: str) -> str | None:
        """Return readable complete sentences if Ollama is unavailable."""

        if not text:
            return None

        normalized = re.sub(r"\s+", " ", text).strip()
        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        selected: list[str] = []

        for sentence in sentences:
            if sentence and sentence not in selected:
                selected.append(sentence)
            if len(selected) == 3 or sum(map(len, selected)) >= 600:
                break

        summary = " ".join(selected).strip()
        if summary and summary[-1] not in ".!?":
            summary += "."
        return summary

    def _build_taxonomy_result(
        self,
        incident_text: str,
        analyzer: Any | None,
        shared_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        retriever = getattr(analyzer, "retriever", None)
        taxonomy_text = self._initiating_event_text(incident_text)

        def is_approved_pair(domain: Any, subdomain: Any) -> bool:
            if not domain or not subdomain or retriever is None:
                return False
            validator = getattr(analyzer, "classification_validator", None)
            validator_check = getattr(validator, "is_approved_taxonomy_pair", None)
            if callable(validator_check):
                return bool(validator_check(str(domain), str(subdomain)))
            return retriever.collection.find_one(
                {
                    "chunk_type": "taxonomy",
                    "domain": domain,
                    "subdomain": subdomain,
                    "active": True,
                },
                {"_id": 1},
            ) is not None

        def calibrate_pair(domain: Any, subdomain: Any) -> tuple[Any, Any]:
            """Apply approved event-mechanism routes before generic semantic ranking."""
            text = taxonomy_text.lower()
            guest_domain = "Guest-Related Incidents"
            ohs_domain = "Occupational Health and Safety"
            ohs_routes = (
                (
                    bool(re.search(r"\bfatigu(?:e|ed)\b.*\b(?:impaired|alertness|safety-sensitive)\b", text)),
                    "Fatigue Management",
                ),
                (
                    bool(re.search(r"\b(?:worker|employee)\b.*\b(?:chemical|fume|vapou?r|biological)\b", text)),
                    "Chemical and Biological Safety",
                ),
                (
                    bool(re.search(r"\b(?:manual handling|musculoskeletal strain|ergonomic)\b", text)),
                    "Ergonomics",
                ),
                (
                    bool(re.search(r"\b(?:employee|worker|contractor)\b.*\b(?:slipped|tripped|fell|slip|trip|fall)\b", text)),
                    "Slips, Trips & Falls",
                ),
            )
            for matched, routed_subdomain in ohs_routes:
                if matched and is_approved_pair(ohs_domain, routed_subdomain):
                    return ohs_domain, routed_subdomain
            canonical_routes = (
                (
                    bool(re.search(
                        r"\bhotel\s+property\b.*\b(?:damaged|removed)\b.*\b(?:unidentified|unknown)\s+person\b",
                        text,
                    )),
                    "Physical Security",
                    "Theft and Vandalism",
                ),
                (
                    bool(re.search(
                        r"\b(?:security\s+)?cameras?\b.*\b(?:stopped|failed|ceased)\b.*\b(?:recording|functioning)\b",
                        text,
                    )),
                    "Physical Security",
                    "Surveillance Systems",
                ),
                (
                    bool(re.search(
                        r"\b(?:hotel\s+)?vehicle\b.*\bpassengers?\b.*\b(?:serious\s+)?road[-\s]safety\s+violation\b",
                        text,
                    )),
                    "Road Safety",
                    "Speeding & Safety Equipment Violations",
                ),
            )
            for matched, routed_domain, routed_subdomain in canonical_routes:
                if matched and is_approved_pair(routed_domain, routed_subdomain):
                    return routed_domain, routed_subdomain
            routes = (
                (
                    bool(re.search(r"\bguest\b.*\b(?:personal item|property)\b.*\b(?:missing|stolen|theft|lost)\b", text)),
                    "Theft or Loss of Guest Property",
                ),
                (
                    bool(re.search(r"\bguest\b.*\b(?:service|privacy)\s+complaint\b", text)),
                    "Guest Complaints",
                ),
                (
                    bool(re.search(r"\bguest\b.*\b(?:false|forged|fraudulent)\b.*\b(?:payment|identity|information)\b", text)),
                    "Fraudulent Activities by Guests",
                ),
                (
                    bool(re.search(r"\b(?:child|dependent guest)\b.*\b(?:could not be located|missing|unaccounted)\b", text)),
                    "Missing Person",
                ),
                (
                    bool(re.search(r"\bguest\b.*\b(?:medical|neurological|symptoms?|seizure|cardiac)\b", text)),
                    "Guest Medical Emergency",
                ),
                (
                    bool(re.search(r"\bguest\b.*\b(?:slipped|tripped|fell|fallen|slip|trip|fall)\b", text)),
                    "Guest Slip, Trip & Fall",
                ),
                (
                    "guest" in text and bool(re.search(
                        r"\b(?:hazard|unsafe|loose|barrier|railing|exposed|safety)\b", text
                    )),
                    "Safety Incidents Involving Guests",
                ),
            )
            for matched, routed_subdomain in routes:
                if matched and is_approved_pair(guest_domain, routed_subdomain):
                    return guest_domain, routed_subdomain
            return domain, subdomain

        hybrid_classifier = getattr(analyzer, "hybrid_taxonomy_classifier", None)
        if hybrid_classifier is not None:
            try:
                result = hybrid_classifier.classify(
                    taxonomy_text,
                    normalized_incident=taxonomy_text,
                )
                domain, subdomain = calibrate_pair(
                    result.get("domain"), result.get("subdomain")
                )
                if is_approved_pair(domain, subdomain):
                    return {
                        "domain": domain,
                        "subdomain": subdomain,
                    }
            except Exception as exc:
                print(f"Hybrid taxonomy classification failed: {exc}")

        # Safe repository-only fallback if a model dependency is unavailable.
        if retriever is not None:
            try:
                candidates = retriever.retrieve(
                    taxonomy_text,
                    chunk_type="taxonomy",
                    limit=10,
                    num_candidates=200,
                )
                best_match = next(
                    (
                        candidate
                        for candidate in candidates
                        if is_approved_pair(
                            candidate.get("domain"),
                            candidate.get("subdomain"),
                        )
                    ),
                    None,
                )
                if best_match is not None:
                    domain, subdomain = calibrate_pair(
                        best_match["domain"], best_match["subdomain"]
                    )
                    return {
                        "domain": domain,
                        "subdomain": subdomain,
                    }
            except Exception as exc:
                print(f"Repository taxonomy autofill failed: {exc}")

        return {
            "domain": None,
            "subdomain": None,
        }

    @staticmethod
    def _initiating_event_text(incident_text: str) -> str:
        """Return the initiating-event sentence and exclude generic response boilerplate."""
        normalized = " ".join(incident_text.split()).strip()
        if not normalized:
            return normalized
        boundary = re.search(
            r"(?<=[.!?])\s+(?=(?:The event occurred|The affected area|Operations continued|No VIP|The case is intended)\b)",
            normalized,
            re.IGNORECASE,
        )
        if boundary:
            return normalized[:boundary.start()].strip()
        first_sentence = re.match(r"^.*?[.!?](?:\s|$)", normalized)
        return first_sentence.group(0).strip() if first_sentence else normalized

    def _build_severity_result(
        self,
        incident_text: str,
        session: dict[str, Any],
        analyzer: Any | None,
    ) -> dict[str, Any]:
        taxonomy = session.get("confirmed", {}).get("taxonomy") or {}
        domain = taxonomy.get("domain")
        subdomain = taxonomy.get("subdomain")

        if analyzer is not None and analyzer.classification_validator is not None:
            try:
                impact_result = analyzer.classification_validator.identify_impact(incident_text)
                validation = analyzer.classification_validator.validate(
                    domain=domain,
                    subdomain=subdomain,
                    impact_type=impact_result.get("impact_type"),
                    matched_evidence=impact_result.get("matched_evidence"),
                )
                if validation.get("impact"):
                    return {
                        "impact_classification": validation["impact"].get("impact_type"),
                        "matched_impact_evidence": validation["impact"].get("matched_evidence"),
                        "severity_level": validation["severity"].get("level_number"),
                        "severity_name": validation["severity"].get("level"),
                    }
            except Exception:
                pass

        return {
            "impact_classification": None,
            "matched_impact_evidence": None,
            "severity_level": None,
            "severity_name": None,
        }

    def _build_actual_near_miss_result(self, incident_text: str) -> dict[str, Any]:
        text = incident_text.lower()
        if any(term in text for term in (
            "near miss", "almost", "nearly", "narrowly missed", "narrow miss"
        )):
            return {
                "classification": "Near Miss",
                "reason": "The narrative describes an event that could have become more severe but did not result in the final outcome.",
            }

        return {
            "classification": "Actual Incident",
            "reason": "The narrative describes an event that occurred and had a measurable impact.",
        }

    def _build_affected_parties_result(
        self,
        incident_text: str,
        shared_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = incident_text.lower()
        party_type = "Unknown"
        if "employee" in text:
            party_type = "Employee"
        elif "guest" in text:
            party_type = "Guest"
        elif "contractor" in text:
            party_type = "Contractor"

        injury_terms = ["injured", "injury", "cut", "laceration", "fracture", "burn", "bruise"]
        consequence = next((term for term in injury_terms if term in text), None)

        extracted_people = (shared_features or {}).get("people_exposed") or []
        extracted_actor = (shared_features or {}).get("actor")
        extracted_outcome = (shared_features or {}).get("actual_outcome")
        if extracted_people:
            party_type = ", ".join(map(str, extracted_people))
        elif extracted_actor:
            party_type = str(extracted_actor)

        return {
            "affected_party_type": party_type,
            "affected_person_details": "Details were not explicitly named in the narrative.",
            "injury_or_consequence": extracted_outcome or (consequence.title() if consequence else None),
        }

    def _build_hipo_result(
        self,
        incident_text: str,
        analyzer: Any | None,
        shared_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hipo_classifier = getattr(analyzer, "hipo_classifier", None) if analyzer else None
        if hipo_classifier is not None:
            return hipo_classifier.classify(incident_text, features=shared_features)

        impact_definitions = {
            "safety_impact": {
                "Catastrophic (5 points)": "Could have resulted in multiple fatalities or severe injuries (e.g., loss of limb).",
                "Major (4 points)": "Could have resulted in a fatality or major injury.",
                "Moderate (3 points)": "Could have caused serious injuries that require medical attention.",
                "Minor (2 points)": "Could have caused minor injuries or health concerns.",
                "Negligible (1 point)": "No significant injury potential was established.",
            },
            "damage_to_assets": {
                "Catastrophic (5 points)": "Could have caused damage exceeding 1% of the annual revenue.",
                "Major (4 points)": "Could have caused significant damage but less than 1% of the annual revenue.",
                "Moderate (3 points)": "Could have caused moderate damage, requiring repair or replacement.",
                "Minor (2 points)": "Could have caused minor damage, easily repairable.",
                "Negligible (1 point)": "No significant damage to assets.",
            },
            "business_continuity": {
                "Catastrophic (5 points)": "Could have completely shut down operations or rendered the property/part of the property non-functional.",
                "Major (4 points)": "Could have caused significant disruption, leading to partial operational shutdown.",
                "Moderate (3 points)": "Could have caused a noticeable disruption, but operations could continue with adjustments.",
                "Minor (2 points)": "Could have caused minor operational delays or inconveniences.",
                "Negligible (1 point)": "No significant impact on business continuity.",
            },
            "reputational_impact": {
                "Catastrophic (5 points)": "Could have led to major reputational damage with wide media coverage or public backlash.",
                "Major (4 points)": "Could have resulted in significant negative publicity or social media backlash.",
                "Moderate (3 points)": "Could have caused some negative attention, affecting brand perception.",
                "Minor (2 points)": "Could have caused minor reputational issues, quickly addressable.",
                "Negligible (1 point)": "No significant reputational impact.",
            },
            "safety_lapse_for_vip": {
                "Catastrophic (5 points)": "A safety lapse that could have caused severe harm or embarrassment to a VIP, potentially escalating to a legal case or public scandal.",
                "Major (4 points)": "A safety lapse that could have caused significant inconvenience or minor harm to a VIP, with potential public backlash.",
                "Moderate (3 points)": "A safety lapse that could have caused minor discomfort or concern to a VIP, likely manageable without public attention.",
                "Minor (2 points)": "A safety lapse that could have caused slight inconvenience to a VIP, easily mitigated.",
                "Negligible (1 point)": "No significant impact on VIP safety.",
            },
            "likelihood_of_more_severe_outcomes": {
                "Catastrophic (5 points)": "The severe outcome was narrowly avoided and could have easily occurred under slightly different conditions.",
                "Major (4 points)": "The severe outcome would have occurred with a minor change in circumstances.",
                "Moderate (3 points)": "The severe outcome was possible but less proximate.",
                "Minor (2 points)": "Several additional control failures would have been required.",
                "Negligible (1 point)": "The available evidence showed remote proximity to a more severe outcome.",
            },
        }

        def add_policy_text(ratings: dict[str, str | None]) -> dict[str, Any]:
            score_by_option = {
                "Catastrophic (5 points)": 5,
                "Major (4 points)": 4,
                "Moderate (3 points)": 3,
                "Minor (2 points)": 2,
                "Negligible (1 point)": 1,
            }
            impact_ratings = [
                rating
                for category, rating in ratings.items()
                if category != "likelihood_of_more_severe_outcomes"
            ]

            major_impact = any(score_by_option.get(rating, 0) >= 4 for rating in impact_ratings)
            if major_impact:
                overall = "HIPO"
                basis = "At least one impact parameter is rated Major (4) or Catastrophic (5), so the incident is automatically HIPO."
            elif any(rating is None for rating in ratings.values()):
                overall = "Not Assessable"
                basis = "One or more required HIPO parameters could not be rated from the incident information."
            else:
                overall = "Not HIPO"
                basis = "No impact parameter is rated Major (4) or Catastrophic (5)."

            parameters = {
                category: {
                    "classification": rating,
                    "description": impact_definitions[category].get(rating) if rating else None,
                }
                for category, rating in ratings.items()
            }
            return {
                "overall_hipo_classification": {
                    "classification": overall,
                    "decision_basis": basis,
                },
                **parameters,
            }

        empty_assessment = {
            "safety_impact": None,
            "damage_to_assets": None,
            "business_continuity": None,
            "reputational_impact": None,
            "safety_lapse_for_vip": None,
            "likelihood_of_more_severe_outcomes": None,
        }

        if analyzer is not None and getattr(analyzer, "retriever", None):
            try:
                evidence = analyzer.retriever.retrieve(
                    incident_text,
                    chunk_type="hipo_policy",
                    limit=10,
                    num_candidates=100,
                )
                llm_analyzer = getattr(analyzer, "llm_analyzer", None)
                if llm_analyzer is not None and evidence:
                    return add_policy_text(
                        llm_analyzer.generate_hipo_criteria(
                            incident_text,
                            evidence,
                        )
                    )
            except Exception as exc:
                print(f"HIPO criteria assessment failed; using fallback: {exc}")

        return add_policy_text(empty_assessment)

    def _build_complete_analysis(
        self,
        session: dict[str, Any],
        analyzer: Any | None,
    ) -> dict[str, Any]:
        """Generate the entire incident record before requesting one review."""

        incident_text = session.get("incident_text", "")
        llm_analyzer = getattr(analyzer, "llm_analyzer", None) if analyzer else None
        hipo_classifier = getattr(analyzer, "hipo_classifier", None) if analyzer else None
        precomputed_hipo = None
        profile_builder = getattr(hipo_classifier, "complete_profile_result", None)
        if callable(profile_builder):
            precomputed_hipo = profile_builder(incident_text)
        shared_features = None
        if precomputed_hipo is not None:
            shared_features = precomputed_hipo.get("features")
        elif llm_analyzer is not None:
            try:
                shared_features = llm_analyzer.extract_hipo_features(incident_text)
                shared_features["extraction_mode"] = "llm"
            except Exception as exc:
                print(f"Shared incident extraction unavailable; using fallback: {exc}")
                hipo_classifier = getattr(analyzer, "hipo_classifier", None)
                if hipo_classifier is not None:
                    shared_features = hipo_classifier.fallback_features(incident_text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            taxonomy_future = pool.submit(
                self._build_taxonomy_result,
                incident_text,
                analyzer,
                shared_features,
            )
            hipo_future = pool.submit(
                lambda: precomputed_hipo or self._build_hipo_result(
                    incident_text, analyzer, shared_features
                )
            )
            taxonomy = taxonomy_future.result()
            hipo = hipo_future.result()
        facts = self._build_facts_result(
            incident_text,
            analyzer=None,
            shared_features=shared_features,
        )
        affected = self._build_affected_parties_result(
            incident_text, shared_features=shared_features
        )
        affected_value = affected.get("affected_party_type") or "Unknown"
        consequence = affected.get("injury_or_consequence")
        if consequence:
            affected_value = f"{affected_value} \u2014 {consequence}"
        event_type = self._build_actual_near_miss_result(incident_text)["classification"]
        hipo_value = hipo.get("hipo_assessment")
        scores = hipo.get("risk_feature_scores", {})
        hipo_review = hipo.get("review") or {}
        def policy_score(name: str) -> int | None:
            value = scores.get(name)
            return value if isinstance(value, int) and 1 <= value <= 5 else None

        return {
            "factual_summary": (
                (shared_features or {}).get("incident_summary")
                or self._fallback_incident_summary(incident_text)
            ),
            "date": facts.get("date"),
            "time": facts.get("time"),
            "domain": taxonomy.get("domain"),
            "subdomain": taxonomy.get("subdomain"),
            "affected_party_details": affected_value,
            "actual_or_near_miss": event_type,
            "safety_impact": policy_score("safety_impact"),
            "damage_to_assets": policy_score("damage_to_assets"),
            "business_continuity": policy_score("business_continuity"),
            "reputational_impact": policy_score("reputational_impact"),
            "vip_safety_impact": policy_score("vip_safety_impact"),
            "likelihood_of_more_severe_outcomes": policy_score("likelihood"),
            "hipo_classification": hipo_value,
            "diagnostics": {
                "hipo_assessment_mode": hipo_review.get("assessment_mode"),
                "hipo_assessment_provider": hipo_review.get("assessment_provider"),
                "hipo_facts_provider": hipo_review.get("facts_provider"),
                "hipo_narrative_corrections": hipo_review.get("narrative_corrections") or [],
                "hipo_event_profile": hipo_review.get("event_profile"),
                "hipo_event_evidence": hipo_review.get("event_evidence") or [],
                "hipo_event_corrections": hipo_review.get("event_corrections") or [],
                "hipo_stage_timings_ms": hipo_review.get("stage_timings_ms") or {},
                "hipo_review_required": bool(hipo_review.get("required")),
                "hipo_missing_information": hipo_review.get("missing_information") or [],
            },
        }

    def _build_final_review_result(self, session: dict[str, Any]) -> dict[str, Any]:
        confirmed = session.get("confirmed", {})
        return {
            "summary": {
                "facts": confirmed.get("facts"),
                "taxonomy": confirmed.get("taxonomy"),
                "severity_impact": confirmed.get("severity_impact"),
                "actual_near_miss": confirmed.get("actual_near_miss"),
                "affected_parties": confirmed.get("affected_parties"),
                "hipo": confirmed.get("hipo"),
            },
            "ready_for_submission": True,
        }

    def _confirmation_question(
        self,
        step: str,
    ) -> str:
        questions = {
            "facts": "Are the extracted incident facts correct?",
            "taxonomy": "Are the Domain and Subdomain correct?",
            "severity_impact": "Are the Impact and Severity correct?",
            "actual_near_miss": "Is the Actual / Near Miss classification correct?",
            "affected_parties": "Are the affected-party details correct?",
            "hipo": "Is the HIPO classification correct?",
            "final_review": "Review the complete incident record before saving.",
        }

        return questions.get(step, "Is this result correct?")
