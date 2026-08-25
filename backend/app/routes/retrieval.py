"""Purpose: Defines retrieval API routing behavior.

Used by: Mounted or imported by the FastAPI application.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.app.schemas.retrieval import (
    RetrievalRequest,
    RetrievalResponse,
)
from backend.app.services.retriever import RetrieverService


router = APIRouter(
    prefix="/retrieve",
    tags=["Retrieval"],
)


@router.post(
    "",
    response_model=RetrievalResponse,
)
def retrieve_knowledge(
    payload: RetrievalRequest,
    request: Request,
) -> RetrievalResponse:
    retriever: RetrieverService = (
        request.app.state.retriever
    )

    if payload.num_candidates < payload.limit:
        raise HTTPException(
            status_code=422,
            detail=(
                "num_candidates must be greater than "
                "or equal to limit."
            ),
        )

    try:
        results = retriever.retrieve(
            payload.query,
            limit=payload.limit,
            num_candidates=payload.num_candidates,
            chunk_type=payload.chunk_type,
            domain=payload.domain,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    return RetrievalResponse(
        query=payload.query,
        result_count=len(results),
        results=results,
    )