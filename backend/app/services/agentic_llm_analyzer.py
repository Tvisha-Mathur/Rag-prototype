"""Purpose: Implements the agentic llm analyzer application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from backend.app.config import settings
from backend.app.services.llm_analyzer import (
    HipoFeatures,
    HipoRuleAssessment,
    HipoScoringFacts,
    LLMAnalyzer,
    RetrievalQueryPlan,
)


class TaxonomySelection(BaseModel):
    candidate_id: str


class EvidenceGrade(BaseModel):
    sufficient: bool
    relevant_chunk_ids: list[str] = Field(default_factory=list, max_length=12)
    missing_evidence: list[str] = Field(default_factory=list, max_length=6)
    corrective_query: str | None = Field(default=None, max_length=700)
    confidence: float = Field(ge=0, le=1)


class ScoreVerification(BaseModel):
    accepted: bool
    review_required: bool = False
    corrected_scores: dict[str, int] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)


class ParameterScoreDecision(BaseModel):
    score: int | None = Field(default=None, ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    lower_boundary: int | None = Field(default=None, ge=1, le=5)
    upper_boundary: int | None = Field(default=None, ge=1, le=5)
    supporting_phrases: list[str] = Field(default_factory=list, max_length=4)
    why_selected: str = Field(min_length=1, max_length=500)
    why_not_adjacent: str = Field(min_length=1, max_length=500)
    missing_information: list[str] = Field(default_factory=list, max_length=4)


class ParameterScoreBatch(BaseModel):
    safety_impact: ParameterScoreDecision
    damage_to_assets: ParameterScoreDecision
    business_continuity: ParameterScoreDecision
    reputational_impact: ParameterScoreDecision
    vip_safety_impact: ParameterScoreDecision
    likelihood_of_more_severe_outcome: ParameterScoreDecision


class CloudCircuitOpenError(RuntimeError):
    pass


def redact_incident_text(text: str) -> str:
    """Remove common direct identifiers before a narrative leaves the host."""
    redacted = text
    patterns = (
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]"),
        (r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)", "[REDACTED_PHONE]"),
        (r"\b(?:employee|guest|staff|incident)\s*(?:id|no\.?|number|#)\s*[:=-]?\s*[A-Z0-9-]+", "[REDACTED_ID]"),
        (r"\b(?:name|guest name|employee name)\s*[:=-]\s*[^,;\n]+", "[REDACTED_NAME]"),
    )
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


class GeminiAgenticAnalyzer:
    """Gemini/LlamaIndex agent with transparent local Ollama fallback."""

    def __init__(self, fallback: LLMAnalyzer | None = None) -> None:
        self.fallback = fallback or LLMAnalyzer()
        self.model = settings.gemini_agent_model
        self._client: Any | None = None
        self._genai_types: Any | None = None
        self._prompt_type: Any | None = None
        self._circuit_open_until = 0.0
        self._circuit_lock = threading.Lock()
        if not settings.gemini_agent_enabled or not settings.gemini_api_key:
            return
        try:
            from google import genai
            from google.genai import types
            from llama_index.core import PromptTemplate

            self._prompt_type = PromptTemplate
            self._genai_types = types
            self._client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=max(5, settings.gemini_agent_timeout_seconds) * 1000,
                    retry_options=types.HttpRetryOptions(
                        # Fail over once and open the circuit. Repeating several long
                        # provider retries at every pipeline stage caused 20+ minute cases.
                        attempts=1,
                        initial_delay=1.0,
                        max_delay=8.0,
                        exp_base=2.0,
                        jitter=1.0,
                        http_status_codes=[408, 429, 500, 502, 503, 504],
                    )
                ),
            )
        except Exception as exc:
            print(f"Gemini agent unavailable; using Ollama fallback: {exc}")

    @property
    def cloud_available(self) -> bool:
        return self._client is not None and time.monotonic() >= self._circuit_open_until

    def __getattr__(self, name: str) -> Any:
        return getattr(self.fallback, name)

    def _compact_evidence(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": item.get("chunk_id"),
                "channel": item.get("channel"),
                "section": item.get("section") or item.get("source_section"),
                "parameter": item.get("parameter"),
                "score": item.get("score_value"),
                "verified": item.get("verified"),
                "incident_summary": self._safe_incident(
                    str(item.get("incident_summary") or "")[:1000]
                ),
                "domain": item.get("domain"),
                "subdomain": item.get("subdomain"),
                "safety_impact": item.get("safety_impact"),
                "damage_to_assets": item.get("damage_to_assets"),
                "business_continuity": item.get("business_continuity"),
                "reputational_impact": item.get("reputational_impact"),
                "vip_safety_impact": item.get("vip_safety") or item.get("vip_safety_impact"),
                "likelihood": item.get("likelihood_of_more_severe_outcome"),
                "hipo_classification": item.get("hipo_classification"),
                "rubric_levels": item.get("rubric_levels"),
                "text": self._safe_incident(
                    str(item.get("text") or item.get("search_text") or "")[:1500]
                ),
            }
            for item in evidence[:48]
        ]

    def _safe_incident(self, text: str) -> str:
        return redact_incident_text(text) if settings.gemini_agent_redact_pii else text

    def _predict(self, schema: type[BaseModel], template: str, **values: Any) -> BaseModel:
        if self._client is not None and time.monotonic() < self._circuit_open_until:
            raise CloudCircuitOpenError("Gemini is cooling down after a transient failure")
        if self._client is None or self._prompt_type is None or self._genai_types is None:
            raise RuntimeError("Gemini cloud agent is not configured")
        prompt = self._prompt_type(template).format(**values)
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=self._genai_types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=schema,
                    automatic_function_calling=self._genai_types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
        except Exception as exc:
            if self._is_transient_error(exc):
                with self._circuit_lock:
                    self._circuit_open_until = time.monotonic() + max(
                        1, settings.gemini_agent_cooldown_seconds
                    )
            raise
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        if parsed is not None:
            return schema.model_validate(parsed)
        return schema.model_validate_json(response.text)

    def _report_failure(self, stage: str, exc: Exception) -> None:
        if not isinstance(exc, CloudCircuitOpenError):
            fallback_name = (
                "Ollama" if settings.ollama_fallback_enabled
                else "deterministic rules"
            )
            print(f"Gemini {stage} failed; using {fallback_name}: {exc}")

    @staticmethod
    def _deterministic_features(incident_text: str) -> dict[str, Any]:
        from backend.app.services.hipo_classifier import HipoClassifier

        return HipoClassifier.fallback_features(incident_text)

    @staticmethod
    def _deterministic_scoring_facts(incident_text: str) -> dict[str, Any]:
        from backend.app.services.hipo_classifier import HipoClassifier

        return HipoClassifier.fallback_scoring_facts(incident_text)

    @staticmethod
    def _deterministic_assessment() -> dict[str, Any]:
        from backend.app.services.hipo_classifier import HipoClassifier

        return HipoClassifier.fallback_assessment()

    def normalize_incident_for_retrieval(self, incident_text: str) -> str:
        if settings.ollama_fallback_enabled:
            return self.fallback.normalize_incident_for_retrieval(incident_text)
        return " ".join(incident_text.split())

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in {408, 429, 500, 502, 503, 504}:
            return True
        message = str(exc).upper()
        return any(marker in message for marker in (
            "503 UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED",
            "SERVICE UNAVAILABLE", "TIMED OUT", "TIMEOUT",
        ))

    def plan_retrieval_queries(self, incident_text: str, normalized_incident: str, *, max_queries: int = 3) -> list[str]:
        try:
            result = self._predict(
                RetrievalQueryPlan,
                """You are the query-planning stage of a corrective incident RAG system.
Create at most {max_queries} distinct MongoDB vector-search queries covering the initiating
event/hazard, exposure and consequence, and failed control or operational context. Preserve
only stated facts. Do not invent severity, injury, cause, domain, or outcome.
Incident: {incident}\nNormalized incident: {normalized}""",
                max_queries=max(1, min(max_queries, 3)), incident=self._safe_incident(incident_text),
                normalized=self._safe_incident(normalized_incident),
            )
            return list(result.queries)[:max_queries]  # type: ignore[attr-defined]
        except Exception as exc:
            self._report_failure("query planning", exc)
            if settings.ollama_fallback_enabled:
                return self.fallback.plan_retrieval_queries(
                    incident_text, normalized_incident, max_queries=max_queries
                )
            return [normalized_incident]

    def extract_hipo_features(self, incident_text: str) -> dict[str, Any]:
        try:
            result = self._predict(
                HipoFeatures,
                """Extract grounded incident features for taxonomy and HIPO retrieval. Do not
invent facts. A credible worst case must follow from the stated hazard, energy, and exposure
under only a slight change; otherwise return null. Keep incident_summary crisp: one or two
sentences, no more than 55 words. Include every available essential fact: what happened,
affected party, date/time/location, actual consequence, and immediate response. Omit only
background detail that does not change those facts. Incident: {incident}""",
                incident=self._safe_incident(incident_text),
            )
            return result.model_dump()
        except Exception as exc:
            self._report_failure("feature extraction", exc)
            if settings.ollama_fallback_enabled:
                return self.fallback.extract_hipo_features(incident_text)
            return self._deterministic_features(incident_text)

    def extract_hipo_scoring_facts(self, incident_text: str) -> dict[str, Any]:
        try:
            result = self._predict(
                HipoScoringFacts,
                """Extract actual outcomes and credible potentials supported by the incident for
deterministic HIPO scoring. Potential may be established by the stated physical mechanism,
exposure, failed control, and proximity even when the narrative does not name the injury that
could have occurred. A no-injury outcome does not establish negligible safety potential. Treat
direct exposure moments before control was restored, narrowly avoided contact, or an equivalent
single-small-change description as small_change escalation proximity. Never invent an unrelated
hazard, downtime, asset cost, publicity, or VIP involvement. Explicit statements such as 'no VIP
was involved' mean vip_involved=false. Use unknown only when the narrative supplies no grounded
basis for that dimension. supporting_phrases must contain short phrases grounded in the narrative.
Incident: {incident}""",
                incident=self._safe_incident(incident_text),
            )
            output = result.model_dump()
            output["_provider"] = "gemini"
            return output
        except Exception as exc:
            self._report_failure("HIPO fact extraction", exc)
            if settings.ollama_fallback_enabled:
                output = self.fallback.extract_hipo_scoring_facts(incident_text)
                output["_provider"] = "ollama"
            else:
                output = self._deterministic_scoring_facts(incident_text)
                output["_provider"] = "deterministic_fallback"
            return output

    def grade_retrieval_evidence(self, incident_text: str, features: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        result = self._predict(
            EvidenceGrade,
            """Act as a corrective-RAG evidence grader. Determine whether the supplied evidence
is relevant and sufficient to score all six HIPO dimensions without guessing. Critical decision
rules plus dimension boundaries should be present. If insufficient, provide one concise corrective
search query targeting the missing evidence. Never classify the incident.
Incident: {incident}\nFeatures: {features}\nEvidence: {evidence}""",
            incident=self._safe_incident(incident_text), features=json.dumps(features, default=str),
            evidence=json.dumps(self._compact_evidence(evidence), default=str),
        )
        return result.model_dump()

    def classify_hipo(self, incident_text: str, features: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            result = self._predict(
                HipoRuleAssessment,
                """Score the five HIPO impact dimensions and likelihood independently using only
the incident facts and supplied evidence. Score credible potential, not only the actual outcome.
A score of 4 or 5 does not require the severe outcome to have occurred. It requires a stated
hazard or energy, an exposed person/asset/operation, a failed or ineffective control, and no more
than a small change for escalation. Evidence for these elements may appear across separate
sentences; the narrative need not explicitly name the credible injury. Direct exposure moments
before control was restored is explicit small-change evidence, not missing evidence. Compare the
case against all supplied 1-5 rubric boundaries and verified scored examples. Choose a lower score
only when the incident lacks a grounded mechanism, exposure, or escalation path. VIP is Negligible
unless a VIP is explicitly involved. Return matching labels and cite
short factual support in reasons. Do not make the final HIPO classification; Python applies it.
Incident: {incident}\nFeatures: {features}\nEvidence: {evidence}""",
                incident=self._safe_incident(incident_text), features=json.dumps(features, default=str),
                evidence=json.dumps(self._compact_evidence(evidence), default=str),
            )
            output = result.model_dump()
            output["_provider"] = "gemini"
            return output
        except Exception as exc:
            self._report_failure("HIPO scoring", exc)
            if settings.ollama_fallback_enabled:
                output = self.fallback.classify_hipo(incident_text, features, evidence)
                output["_provider"] = "ollama"
            else:
                output = self._deterministic_assessment()
                output["_provider"] = "deterministic_fallback"
            return output

    def verify_hipo_scores(
        self,
        incident_text: str,
        facts: dict[str, Any],
        assessment: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Perform one bounded verification; corrections may move only one level."""
        result = self._predict(
            ScoreVerification,
            """Verify the six proposed HIPO scores against the explicit facts and policy evidence.
Accept supported scores. A corrected score may differ by at most one point from its proposal.
Do not fill missing facts by assumption; set review_required instead. Return only corrections
that are directly supported by evidence. Incident: {incident}\nFacts: {facts}\nAssessment:
{assessment}\nEvidence: {evidence}""",
            incident=self._safe_incident(incident_text),
            facts=json.dumps(facts, default=str),
            assessment=json.dumps(assessment, default=str),
            evidence=json.dumps(self._compact_evidence(evidence), default=str),
        )
        return result.model_dump()

    def rescore_hipo_scores(
        self,
        incident_text: str,
        facts: dict[str, Any],
        assessment: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create fresh dimension scores when the original scorer used fallback values."""
        result = self._predict(
            ScoreVerification,
            """The original structured HIPO scorer was unavailable, so its proposed values are
fallback placeholders rather than trustworthy scores. Independently rescore all six dimensions
from 1 to 5 using only the incident, extracted facts, complete policy rubrics, and verified
examples. Return every supported score in corrected_scores. Do not preserve a placeholder merely
because changing it requires more than one level. Do not invent missing consequences. VIP must be
1 unless VIP involvement is explicit. Set review_required when a dimension cannot be grounded.
Incident: {incident}\nFacts: {facts}\nFallback assessment: {assessment}\nEvidence: {evidence}""",
            incident=self._safe_incident(incident_text),
            facts=json.dumps(facts, default=str),
            assessment=json.dumps(assessment, default=str),
            evidence=json.dumps(self._compact_evidence(evidence), default=str),
        )
        return result.model_dump()

    def score_hipo_parameter(
        self,
        incident_text: str,
        parameter: str,
        provisional_score: int,
        rubric: dict[str, Any],
        verified_examples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Independently score one dimension with adjacent-boundary comparison."""
        result = self._predict(
            ParameterScoreDecision,
            """Score exactly one HIPO parameter independently from 1 to 5. Use only the incident,
the complete parameter rubric, and reviewer-verified examples. Compare the best score with its
adjacent boundary or boundaries and state why the selected boundary is met and the adjacent one
is not. Do not transfer consequences from another parameter. Return score=null and list the
missing information when the evidence cannot distinguish a defensible boundary. The provisional
score is context, not authority. For VIP, score above 1 only when VIP involvement is explicit.
Parameter: {parameter}\nIncident: {incident}\nProvisional score: {provisional}\nRubric:
{rubric}\nVerified examples: {examples}""",
            parameter=parameter,
            incident=self._safe_incident(incident_text),
            provisional=provisional_score,
            rubric=json.dumps(rubric, default=str),
            examples=json.dumps(self._compact_evidence(verified_examples[:5]), default=str),
        )
        return result.model_dump()

    def score_hipo_parameters(
        self,
        incident_text: str,
        provisional_scores: dict[str, int],
        rubrics: list[dict[str, Any]],
        verified_examples: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """Score all dimensions independently in one provider request."""
        compact_examples = {
            field: self._compact_evidence(items[:5])
            for field, items in verified_examples.items()
        }
        result = self._predict(
            ParameterScoreBatch,
            """Score all six HIPO parameters, but decide each parameter independently. For each
parameter use only its matching complete 1-5 rubric, its matching reviewer-verified examples,
and incident facts relevant to that parameter. Compare the selected score with adjacent
boundaries and explain why the selected boundary is met and the adjacent one is not. Do not
transfer consequences across parameters. Return score=null for a parameter when its evidence
cannot distinguish a defensible boundary. VIP must be 1 unless VIP involvement is explicit.
The provisional scores are context, not authority.
Incident: {incident}\nProvisional scores: {provisional}\nRubrics: {rubrics}\nVerified examples by parameter: {examples}""",
            incident=self._safe_incident(incident_text),
            provisional=json.dumps(provisional_scores, default=str),
            rubrics=json.dumps(rubrics, default=str),
            examples=json.dumps(compact_examples, default=str),
        )
        return result.model_dump()

    def select_taxonomy_candidate(self, incident_text: str, normalized_incident: str, candidates: list[dict[str, Any]]) -> str:
        allowed = {str(candidate["candidate_id"]) for candidate in candidates}
        try:
            result = self._predict(
                TaxonomySelection,
                """Select exactly one candidate_id from the supplied approved taxonomy candidates.
Use event mechanism, hazard, exposed party, and actual outcome. Never create a new ID.
Incident: {incident}\nNormalized: {normalized}\nCandidates: {candidates}""",
                incident=self._safe_incident(incident_text), normalized=self._safe_incident(normalized_incident),
                candidates=json.dumps(candidates, default=str),
            )
            if result.candidate_id not in allowed:  # type: ignore[attr-defined]
                raise ValueError("Gemini returned an unapproved taxonomy candidate")
            return result.candidate_id  # type: ignore[attr-defined]
        except Exception as exc:
            self._report_failure("taxonomy selection", exc)
            if settings.ollama_fallback_enabled:
                return self.fallback.select_taxonomy_candidate(
                    incident_text, normalized_incident, candidates
                )
            return str(candidates[0]["candidate_id"])


def build_agentic_analyzer() -> GeminiAgenticAnalyzer:
    return GeminiAgenticAnalyzer(LLMAnalyzer())
