"""Purpose: Defines validated retrieval data models.

Used by: Imported by API routes and application services for request or response validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ChunkType = Literal[
    "taxonomy",
    "hipo_policy",
    "severity_policy",
    "rca_guidance",
]


class RetrievalRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=10,
        max_length=5_000,
        description="Incident narrative to search.",
    )

    limit: int = Field(
        default=5,
        ge=1,
        le=100,
    )

    num_candidates: int = Field(
        default=100,
        ge=1,
        le=1_000,
    )

    chunk_type: ChunkType | None = None
    domain: str | None = None


class RetrievalResult(BaseModel):
    chunk_id: str
    chunk_type: str
    document_type: str | None = None
    domain: str | None = None
    subdomain: str | None = None
    section: str | None = None
    hazard_identified: str | None = None
    severity: str | None = None
    severity_level: str | None = None
    risk_type: str | None = None
    risk_identified: str | None = None
    risk_explanation: str | None = None
    control_measures: Any | None = None
    search_text: str | None = None
    source: Any | None = None
    score: float


class RetrievalResponse(BaseModel):
    query: str
    result_count: int
    results: list[RetrievalResult]
