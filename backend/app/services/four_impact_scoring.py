"""Purpose: Implements the four impact scoring application service.

Used by: Imported by the incident-analysis runtime, workflow, or supporting evaluation tools.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


FOUR_IMPACT_FIELDS = (
    "safety_impact",
    "damage_to_assets",
    "business_continuity",
    "reputational_impact",
)


class FourImpactScores(BaseModel):
    """The deliberately small output contract used for impact fine-tuning."""

    model_config = ConfigDict(extra="forbid")

    safety_impact: int = Field(strict=True, ge=1, le=5)
    damage_to_assets: int = Field(strict=True, ge=1, le=5)
    business_continuity: int = Field(strict=True, ge=1, le=5)
    reputational_impact: int = Field(strict=True, ge=1, le=5)

    @model_validator(mode="after")
    def reject_boolean_scores(self) -> "FourImpactScores":
        for field in FOUR_IMPACT_FIELDS:
            if isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be an integer from 1 to 5")
        return self


FOUR_IMPACT_SYSTEM_PROMPT = """You are an incident impact scoring assistant.

Score exactly four credible potential-impact dimensions from 1 to 5:
1. Safety impact
2. Damage to assets
3. Business continuity
4. Reputational impact

Use the incident narrative as the primary case evidence.
Use retrieved HIPO scoring rules as the scoring authority.
Use retrieved verified incidents only as examples of applying those rules.

Requirements:
- Assess credible potential impact under one or two slightly different circumstances, not only the actual outcome.
- Do not invent people, injuries, equipment, shutdowns, financial values, media coverage, or hazards.
- Choose the lower score when a higher score requires unsupported assumptions.
- Score each dimension independently.
- Do not copy a retrieved example's score merely because its wording is similar.
- Return only valid JSON containing the four required integer scores.
""".strip()


def compact_policy_rules(items: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    return [
        {
            "rule_id": item.get("rule_id") or item.get("chunk_id"),
            "section": item.get("section") or item.get("source_section"),
            "text": str(item.get("text") or item.get("search_text") or "")[:1600],
        }
        for item in items[:limit]
    ]


def compact_verified_examples(
    items: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    examples = []
    for item in items[:limit]:
        examples.append({
            "case_id": str(
                item.get("case_id")
                or item.get("incident_no")
                or item.get("source_query_id")
                or item.get("chunk_id")
            ),
            "incident_summary": str(item.get("incident_summary") or "")[:1200],
            **{field: item.get(field) for field in FOUR_IMPACT_FIELDS},
        })
    return examples


def build_four_impact_messages(
    incident_text: str,
    policy_rules: list[dict[str, Any]],
    verified_examples: list[dict[str, Any]],
) -> list[dict[str, str]]:
    narrative = " ".join(incident_text.split())
    if not narrative:
        raise ValueError("incident_text cannot be empty")
    user_prompt = (
        f"INCIDENT NARRATIVE:\n{narrative}\n\n"
        "RETRIEVED HIPO SCORING RULES:\n"
        f"{json.dumps(compact_policy_rules(policy_rules), ensure_ascii=False)}\n\n"
        "RETRIEVED VERIFIED EXAMPLES:\n"
        f"{json.dumps(compact_verified_examples(verified_examples), ensure_ascii=False)}\n\n"
        "Return the four impact scores."
    )
    return [
        {"role": "system", "content": FOUR_IMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
