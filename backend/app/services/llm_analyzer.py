"""Purpose: Implements the llm analyzer application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from ollama import Client
from pydantic import BaseModel, Field

from backend.app.config import settings
from backend.app.schemas.llm_analysis import LLMIncidentAnalysis
from backend.app.services.four_impact_scoring import (
    FourImpactScores,
    build_four_impact_messages,
)


class IncidentSummary(BaseModel):
    incident_summary: str = Field(min_length=1, max_length=900)


ImpactOption = Literal[
    "Catastrophic (5 points)",
    "Major (4 points)",
    "Moderate (3 points)",
    "Minor (2 points)",
    "Negligible (1 point)",
]
LikelihoodOption = ImpactOption


class HipoCriteriaAssessment(BaseModel):
    safety_impact: ImpactOption | None
    damage_to_assets: ImpactOption | None
    business_continuity: ImpactOption | None
    reputational_impact: ImpactOption | None
    safety_lapse_for_vip: ImpactOption | None
    likelihood_of_more_severe_outcomes: LikelihoodOption | None


class NormalizedIncident(BaseModel):
    normalized_incident: str = Field(min_length=1, max_length=2000)


class RetrievalQueryPlan(BaseModel):
    """Small, bounded search plan used by the retrieval agent."""

    queries: list[str] = Field(min_length=1, max_length=3)


class HipoFeatures(BaseModel):
    normalized_incident: str
    incident_summary: str
    primary_event: str | None
    hazard: str | None
    actor: str | None
    location: str | None
    exposure: str | None
    actual_outcome: str | None
    energy_source: str | None
    people_exposed: list[str]
    critical_controls: list[str]
    credible_worst_case: str | None


class HipoDecision(HipoCriteriaAssessment):
    pass


ImpactLevel = Literal["Negligible", "Minor", "Moderate", "Major", "Catastrophic"]
LikelihoodLevel = ImpactLevel


class ImpactRating(BaseModel):
    score: int = Field(ge=1, le=5)
    level: ImpactLevel
    reason: str = Field(min_length=1, max_length=500)


class LikelihoodRating(BaseModel):
    score: int = Field(ge=1, le=5)
    level: LikelihoodLevel
    reason: str = Field(min_length=1, max_length=500)


class HipoRuleAssessment(BaseModel):
    safety_impact: ImpactRating
    damage_to_assets: ImpactRating
    business_continuity: ImpactRating
    reputational_impact: ImpactRating
    vip_safety_impact: ImpactRating
    likelihood_of_more_severe_outcome: LikelihoodRating


class HipoScoringFacts(BaseModel):
    """Grounded facts that can be mapped to policy scores without free-form reasoning."""

    safety_potential: Literal[
        "unknown", "none", "minor", "medical_attention", "major_injury",
        "fatality", "multiple_fatalities",
    ] = "unknown"
    operational_potential: Literal[
        "unknown", "none", "minor_delay", "continued_with_adjustments",
        "partial_shutdown", "complete_shutdown",
    ] = "unknown"
    asset_potential: Literal[
        "unknown", "none", "minor_repair", "repair_or_replacement",
        "significant_under_one_percent_revenue", "over_one_percent_revenue",
    ] = "unknown"
    reputation_potential: Literal[
        "unknown", "none", "minor_addressable", "negative_attention",
        "significant_publicity", "wide_media_coverage",
    ] = "unknown"
    vip_involved: bool | None = None
    escalation_proximity: Literal[
        "unknown", "remote", "multiple_additional_failures", "possible",
        "small_change", "narrowly_avoided",
    ] = "unknown"
    supporting_phrases: dict[str, list[str]] = Field(default_factory=dict)


class LLMAnalyzer:
    """Generate grounded incident analysis using Ollama."""

    def __init__(self, model: str | None = None) -> None:
        self.client = Client(
            host=settings.ollama_base_url,
            timeout=settings.ollama_timeout_seconds,
        )
        self.model = model or settings.ollama_model

    def score_four_impacts(
        self,
        incident_text: str,
        policy_rules: list[dict[str, Any]],
        verified_examples: list[dict[str, Any]],
    ) -> FourImpactScores:
        """Score only the four impact dimensions targeted by the first tune."""
        response = self.client.chat(
            model=self.model,
            messages=build_four_impact_messages(
                incident_text,
                policy_rules,
                verified_examples,
            ),
            format=FourImpactScores.model_json_schema(),
            think=False,
            options={"temperature": 0, "num_predict": 200},
        )
        return FourImpactScores.model_validate_json(response["message"]["content"])

    def normalize_incident_for_retrieval(self, incident_text: str) -> str:
        """Normalize an incident without adding facts."""
        try:
            response = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "Normalize incident text for search. Preserve only stated facts, hazards, "
                        "event mechanism, affected party, location, consequence, and action. "
                        "Expand obvious abbreviations. Do not classify or invent information."
                    )},
                    {"role": "user", "content": incident_text},
                ],
                format=NormalizedIncident.model_json_schema(),
                options={"temperature": 0},
            )
            return NormalizedIncident.model_validate_json(
                response["message"]["content"]
            ).normalized_incident.strip()
        except Exception:
            return " ".join(incident_text.split())

    def plan_retrieval_queries(
        self,
        incident_text: str,
        normalized_incident: str,
        *,
        max_queries: int = 3,
    ) -> list[str]:
        """Create complementary, fact-preserving taxonomy search queries."""
        bounded_max = max(1, min(max_queries, 3))
        response = self.client.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a retrieval planner for an incident-risk taxonomy. "
                        "Produce complementary search queries, not answers. Cover: "
                        "(1) event mechanism and hazard, (2) affected party and actual "
                        "consequence, and (3) failed control or operational context when "
                        "explicitly stated. Preserve stated facts only; never infer severity, "
                        "domain, subdomain, injuries, causes, or outcomes. Keep each query "
                        "concise and distinct."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"ORIGINAL INCIDENT:\n{incident_text}\n\n"
                        f"NORMALIZED INCIDENT:\n{normalized_incident}\n\n"
                        f"Return at most {bounded_max} queries."
                    ),
                },
            ],
            format=RetrievalQueryPlan.model_json_schema(),
            think=False,
            options={"temperature": 0, "num_predict": 220},
        )
        plan = RetrievalQueryPlan.model_validate_json(
            response["message"]["content"]
        )
        queries: list[str] = []
        seen: set[str] = set()
        for raw_query in plan.queries:
            query = " ".join(raw_query.split()).strip()
            key = query.casefold()
            if query and key not in seen:
                queries.append(query)
                seen.add(key)
            if len(queries) >= bounded_max:
                break
        return queries

    def extract_hipo_features(self, incident_text: str) -> dict[str, Any]:
        """Create the one shared extraction used by taxonomy and HIPO."""
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "Normalize and summarize the incident, then extract the shared taxonomy "
                    "and HIPO features. Do not invent conditions. "
                    "The credible worst case must be a realistic escalation supported by the "
                    "hazard, energy and exposure described; return null when it cannot be formed."
                )},
                {"role": "user", "content": f"INCIDENT NARRATIVE:\n{incident_text}"},
            ],
            format=HipoFeatures.model_json_schema(),
            think=False,
            options={"temperature": 0, "num_predict": 900},
        )
        return HipoFeatures.model_validate_json(response["message"]["content"]).model_dump()

    def extract_hipo_scoring_facts(self, incident_text: str) -> dict[str, Any]:
        """Extract only explicit facts used by deterministic HIPO score rules."""
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "Extract only actual outcomes and credible potentials explicitly supported by "
                    "the incident. A no-injury actual outcome does not prove negligible safety "
                    "potential. Potential may be supported by the stated mechanism, exposure, "
                    "failed control, and proximity even when the exact injury is not named. Treat "
                    "direct exposure moments before control was restored or equivalent narrowly "
                    "avoided contact as small_change escalation proximity. Use unknown when a "
                    "dimension has no grounded basis. Do not invent shutdown, cost, publicity, "
                    "VIP status, or an unrelated hazard. An explicit 'no VIP' statement means false. "
                    "supporting_phrases must be "
                    "short exact or near-exact phrases from the narrative, grouped by field."
                )},
                {"role": "user", "content": f"INCIDENT NARRATIVE:\n{incident_text}"},
            ],
            format=HipoScoringFacts.model_json_schema(),
            think=False,
            options={"temperature": 0, "num_predict": 500},
        )
        return HipoScoringFacts.model_validate_json(
            response["message"]["content"]
        ).model_dump()

    def classify_hipo(
        self,
        incident_text: str,
        features: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Score six HIPO dimensions; deterministic code makes the final decision."""
        compact_evidence = [
            {
                "channel": item.get("channel"),
                "hazard": item.get("hazard_identified"),
                "classification": item.get("hipo_classification"),
                "verified": item.get("verified"),
                "safety_impact": item.get("safety_impact"),
                "damage_to_assets": item.get("damage_to_assets"),
                "business_continuity": item.get("business_continuity"),
                "reputational_impact": item.get("reputational_impact"),
                "vip_safety_impact": item.get("vip_safety") or item.get("vip_safety_impact"),
                "likelihood": item.get("likelihood_of_more_severe_outcome"),
                "parameter": item.get("parameter"),
                "policy_score": item.get("score_value"),
                "rubric_levels": item.get("rubric_levels"),
                "section": item.get("section") or item.get("source_section"),
                "text": str(
                    item.get("incident_summary")
                    or item.get("search_text")
                    or item.get("hipo_classification_reason")
                    or ""
                )[:1200],
                "fusion_score": item.get("fusion_score"),
            }
            for item in evidence[:48]
        ]
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "Score credible potential consequences, not merely actual treatment or outcome. "
                    "A score of 4 or 5 does not require the severe outcome to have occurred. It "
                    "requires a stated hazard or energy, an exposed person, asset, or operation, a "
                    "failed or ineffective control, and no more than a small change for escalation. "
                    "These elements may be stated across separate sentences and the exact injury "
                    "need not be named. Direct exposure moments before control was restored is "
                    "explicit small-change evidence, not missing evidence. "
                    "Compare against every supplied 1-5 rubric boundary and the complete scores on "
                    "verified examples. Do not invent people, VIPs, media, shutdowns, financial "
                    "values, or hazards. Choose a lower score only when a required supporting element "
                    "is missing. Score each "
                    "dimension independently and distinguish severity from closeness/likelihood. "
                    "Impact scale: 1 negligible, 2 minor, 3 moderate, 4 major, 5 catastrophic. "
                    "Safety 4 means one credible fatality/major permanent injury; 5 means multiple "
                    "fatalities or severe injuries. Assets 5 requires credible loss above 1% annual "
                    "revenue or destruction of major property; never invent revenue. Business 4 is "
                    "partial shutdown/critical-service loss; 5 is complete or major-property shutdown. "
                    "Reputation 4 is significant publicity/backlash; 5 is widespread media or major "
                    "long-term scandal. VIP impact must be 1 unless a VIP is explicitly involved or "
                    "exposed. Likelihood means closeness of this event to the severe outcome: 1 rare, "
                    "2 unlikely, 3 possible, 4 likely with a small change, 5 narrowly avoided. Return "
                    "only the six ratings and concise factual reasons. Do not classify HIPO yourself."
                )},
                {"role": "user", "content": (
                    f"INCIDENT:\n{incident_text}\n\nFEATURES:\n{json.dumps(features)}"
                    f"\n\nFUSED EVIDENCE:\n{json.dumps(compact_evidence, default=str)}"
                )},
            ],
            format=HipoRuleAssessment.model_json_schema(),
            think=False,
            options={"temperature": 0, "num_predict": 900},
        )
        return HipoRuleAssessment.model_validate_json(response["message"]["content"]).model_dump()

    def select_taxonomy_candidate(
        self,
        incident_text: str,
        normalized_incident: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        """Select only one of the supplied master-repository candidates."""
        candidate_ids = [str(item["candidate_id"]) for item in candidates]
        schema = {
            "type": "object",
            "properties": {"candidate_id": {"type": "string", "enum": candidate_ids}},
            "required": ["candidate_id"],
            "additionalProperties": False,
        }
        compact = [
            {
                "candidate_id": item["candidate_id"],
                "domain": item.get("domain"),
                "subdomain": item.get("subdomain"),
                "repository_text": item.get("search_text"),
            }
            for item in candidates
        ]
        try:
            response = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "Choose the single most suitable taxonomy candidate. You may select only "
                        "a supplied candidate_id. Do not create a domain or subdomain."
                    )},
                    {"role": "user", "content": (
                        f"ORIGINAL INCIDENT:\n{incident_text}\n\nNORMALIZED INCIDENT:\n"
                        f"{normalized_incident}\n\nCANDIDATES:\n{json.dumps(compact, default=str)}"
                    )},
                ],
                format=schema,
                options={"temperature": 0},
            )
            candidate_id = json.loads(response["message"]["content"])["candidate_id"]
            return candidate_id if candidate_id in candidate_ids else candidate_ids[0]
        except Exception:
            return candidate_ids[0]

    def generate_incident_summary(self, incident_text: str) -> str:
        """Create a concise, factual summary of one incident narrative."""

        response = self.client.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize incident reports accurately and concisely. "
                        "State what happened, where and when when known, who was "
                        "affected, the consequence, and the immediate response. "
                        "Use 1-2 complete sentences and no more than 55 words. "
                        "Include every available essential fact: what happened, affected party, "
                        "date/time/location, actual consequence, and immediate response. "
                        "Omit background detail that does not change the factual event. "
                        "Use neutral professional language. "
                        "Do not invent, infer, classify, assign blame, or include root causes."
                    ),
                },
                {
                    "role": "user",
                    "content": f"INCIDENT REPORT:\n{incident_text}",
                },
            ],
            format=IncidentSummary.model_json_schema(),
            options={"temperature": 0},
        )

        result = IncidentSummary.model_validate_json(
            response["message"]["content"]
        )
        return result.incident_summary.strip()

    def generate_hipo_criteria(
        self,
        incident_text: str,
        policy_evidence: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Assess the six HIPO criteria using retrieved policy evidence."""

        evidence = [
            {
                "source_file": item.get("source_file"),
                "section": item.get("section") or item.get("source_section"),
                "text": item.get("search_text"),
            }
            for item in policy_evidence
        ]
        response = self.client.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Select ratings for exactly six HIPO/Near Miss criteria. "
                        "Return only an exact option permitted by the supplied policy schema. "
                        "Use only the incident narrative and retrieved policy excerpts. "
                        "Return null when the narrative does not support a rating. "
                        "Do not create labels, explanations, or additional options."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"INCIDENT NARRATIVE:\n{incident_text}\n\n"
                        f"RETRIEVED HIPO/NEAR MISS POLICY:\n{json.dumps(evidence, default=str)}"
                    ),
                },
            ],
            format=HipoCriteriaAssessment.model_json_schema(),
            options={"temperature": 0},
        )
        result = HipoCriteriaAssessment.model_validate_json(
            response["message"]["content"]
        )
        return result.model_dump()

    def generate_analysis(
        self,
        incident_text: str,
        deterministic_result: dict[str, Any],
    ) -> LLMIncidentAnalysis:
        """
        Generate structured incident analysis using only
        the incident narrative and supplied evidence.
        """

        context = self._build_context(
            deterministic_result
        )

        system_prompt = """
You are an incident-analysis assistant.

Use the incident narrative as the primary case to analyze.
Use the controlled system result only as supporting evidence.

Rules:
1. Do not invent facts.
2. Do not change the supplied domain or subdomain.
3. Do not change the supplied severity result.
4. Do not change the supplied HIPO result.
5. Clearly distinguish observed facts from possible causes.
6. Treat unconfirmed root causes as requiring investigation.
7. Do not blame individuals without supporting evidence.
8. Do not copy causes from retrieved example incidents.
9. Use retrieved RCA examples only as methodology guidance.
10. Provide immediate corrective actions where appropriate.
11. List missing information when evidence is insufficient.
12. Return valid JSON matching the supplied schema.
"""

        user_prompt = f"""
The incident narrative below is the primary case to analyze.

INCIDENT NARRATIVE:
{incident_text}

IMPORTANT:
- Analyze the incident narrative above.
- The controlled system result is supporting context only.
- Do not claim that no incident was provided when the
  narrative contains an incident.
- Do not copy example causes from retrieved RCA guidance.
- Treat RCA examples only as methodology references.
- Recommend immediate safety actions even when the root
  cause is not yet confirmed.

CONTROLLED SYSTEM RESULT:
{json.dumps(context, indent=2, default=str)}

Generate a structured incident analysis for the incident
narrative.
"""

        response = self.client.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            format=LLMIncidentAnalysis.model_json_schema(),
            options={
                "temperature": 0,
            },
        )

        content = response["message"]["content"]

        try:
            return LLMIncidentAnalysis.model_validate_json(
                content
            )
        except Exception as exc:
            raise RuntimeError(
                "Ollama returned an invalid structured "
                "analysis."
            ) from exc

    def _build_context(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a compact evidence package for the LLM."""

        policy_evidence = result.get(
            "policy_evidence",
            {},
        )

        def compact_evidence(
            items: list[dict[str, Any]],
            limit: int = 3,
            text_limit: int = 700,
        ) -> list[dict[str, Any]]:
            compact: list[dict[str, Any]] = []

            for item in items[:limit]:
                search_text = str(
                    item.get("search_text") or ""
                )

                compact.append(
                    {
                        "chunk_id": item.get(
                            "chunk_id"
                        ),
                        "section": item.get(
                            "section"
                        ),
                        "score": item.get(
                            "score"
                        ),
                        "search_text": search_text[
                            :text_limit
                        ],
                        "source": item.get(
                            "source"
                        ),
                    }
                )

            return compact

        return {
            "classification": result.get(
                "classification"
            ),
            "mechanism": result.get(
                "mechanism"
            ),
            "severity": result.get(
                "severity"
            ),
            "hipo": result.get(
                "hipo"
            ),
            "taxonomy_evidence": compact_evidence(
                result.get(
                    "taxonomy_evidence",
                    [],
                ),
                limit=3,
                text_limit=500,
            ),
            "hipo_policy_evidence": compact_evidence(
                policy_evidence.get(
                    "hipo",
                    [],
                ),
                limit=2,
                text_limit=500,
            ),
            "severity_policy_evidence": compact_evidence(
                policy_evidence.get(
                    "severity",
                    [],
                ),
                limit=2,
                text_limit=500,
            ),
            "rca_guidance": compact_evidence(
                policy_evidence.get(
                    "rca",
                    [],
                ),
                limit=3,
                text_limit=900,
            ),
        }
