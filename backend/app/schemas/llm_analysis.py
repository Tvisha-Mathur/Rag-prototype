"""Purpose: Defines validated llm analysis data models.

Used by: Imported by API routes and application services for request or response validation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RootCauseItem(BaseModel):
    root_cause: str
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    supporting_evidence: list[str] = Field(
        default_factory=list
    )
    is_hypothesis: bool = True


class LLMIncidentAnalysis(BaseModel):
    incident_summary: str

    immediate_cause: str | None = None

    contributing_factors: list[str] = Field(
        default_factory=list
    )

    possible_root_causes: list[RootCauseItem] = Field(
        default_factory=list
    )

    corrective_actions: list[str] = Field(
        default_factory=list
    )

    preventive_actions: list[str] = Field(
        default_factory=list
    )

    missing_information: list[str] = Field(
        default_factory=list
    )

    analysis_explanation: str