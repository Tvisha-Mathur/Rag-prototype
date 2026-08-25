"""Purpose: Defines validated controlled classification data models.

Used by: Imported by API routes and application services for request or response validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImpactResult(BaseModel):
    impact_type: str | None = None
    matched_evidence: str | None = None
    is_validated: bool = False


class ControlledSeverityResult(BaseModel):
    level: str | None = None
    level_number: int | None = Field(
        default=None,
        ge=1,
        le=5,
    )
    status: str
    source_document: str | None = None
    source_section: str | None = None


class ControlledClassificationResult(BaseModel):
    domain: str | None = None
    subdomain: str | None = None

    impact: ImpactResult
    severity: ControlledSeverityResult

    status: str
    domain_subdomain_valid: bool = False
    impact_supported: bool = False
    severity_supported: bool = False
    requires_manual_review: bool = False

    validation_errors: list[str] = Field(
        default_factory=list
    )

    taxonomy_evidence: list[dict[str, Any]] = Field(
        default_factory=list
    )