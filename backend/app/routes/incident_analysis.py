"""Purpose: Defines incident analysis API routing behavior.

Used by: Mounted or imported by the FastAPI application.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.schemas.incident_analysis import (
    IncidentAnalysisRequest,
    IncidentAnalysisResponse,
)
from backend.app.services.incident_analyzer import IncidentAnalyzer


router = APIRouter(
    prefix="/analyze-incident",
    tags=["Incident Analysis"],
)


@router.post(
    "",
    response_model=IncidentAnalysisResponse,
    status_code=status.HTTP_200_OK,
)
def analyze_incident(
    payload: IncidentAnalysisRequest,
    request: Request,
) -> IncidentAnalysisResponse:
    """Analyze an incident using taxonomy and policy evidence."""

    retriever = getattr(
        request.app.state,
        "retriever",
        None,
    )

    if retriever is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retriever service is not initialized.",
        )

    analyzer = IncidentAnalyzer(retriever)

    try:
        result = analyzer.analyze(
            payload.incident_text
        )

        return IncidentAnalysisResponse(
            **result
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Incident analysis failed: "
                f"{exc}"
            ),
        ) from exc