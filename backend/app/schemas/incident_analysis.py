"""Purpose: Defines validated incident analysis data models.

Used by: Imported by API routes and application services for request or response validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IncidentAnalysisRequest(BaseModel):
    incident_text: str = Field(
        ...,
        min_length=10,
        max_length=5000,
        description="Narrative describing the incident.",
    )


class AgreementDetails(BaseModel):
    matching_results: int
    total_results: int
    simple_ratio: float
    weighted_ratio: float


class ClassificationResult(BaseModel):
    domain: str | None
    subdomain: str | None
    confidence: float
    status: str
    top_score: float
    selected_total_score: float
    agreement: AgreementDetails | None = None


class CandidateScore(BaseModel):
    domain: str
    subdomain: str
    matching_results: int
    combined_score: float
    weighted_share: float


class TaxonomyEvidence(BaseModel):
    chunk_id: str | None = None
    domain: str | None = None
    subdomain: str | None = None
    score: float
    hazard_identified: Any = None
    risk_identified: Any = None
    risk_explanation: Any = None
    control_measures: Any = None


class PolicyEvidence(BaseModel):
    chunk_id: str | None = None
    chunk_type: str | None = None
    document_type: str | None = None
    section: str | None = None
    search_text: str | None = None
    score: float
    source: Any = None


class PolicyEvidenceGroups(BaseModel):
    hipo: list[PolicyEvidence]
    severity: list[PolicyEvidence]
    rca: list[PolicyEvidence]

class SeverityAssessment(BaseModel):
    level: str
    status: str
    matched_evidence: str | None = None
    reason: str

class HipoAssessment(BaseModel):
    status: str
    assessment_status: str
    matched_evidence: str | None = None
    reason: str

class IncidentAnalysisResponse(BaseModel):
    incident_text: str
    classification: ClassificationResult
    severity: SeverityAssessment
    hipo: HipoAssessment
    candidate_scores: list[CandidateScore]
    taxonomy_evidence: list[TaxonomyEvidence]
    policy_evidence: PolicyEvidenceGroups