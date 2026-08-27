"""Purpose: Provides the main application module.

Used by: Imported during FastAPI startup and backend runtime.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.app.routes.incident_analysis import (
    router as incident_analysis_router,
)
from backend.app.routes.retrieval import (
    router as retrieval_router,
)
from backend.app.services.incident_analyzer import IncidentAnalyzer
from backend.app.services.incident_workflow import IncidentWorkflow
from backend.app.services.retriever import RetrieverService
from backend.app.services.accuracy_evaluator import AccuracyEvaluator
from backend.app.services.verified_incident_store import VerifiedIncidentStore
from backend.app.config import settings
from backend.app.services.excel_exporter import build_response_workbook, load_export_records


class StartIncidentRequest(BaseModel):
    incident_text: str


class ConfirmStepRequest(BaseModel):
    approved: bool
    correction: dict[str, Any] | None = None


class ExpertResult(BaseModel):
    domain: str
    subdomain: str
    safety_impact: int = Field(ge=1, le=5)
    damage_to_assets: int = Field(ge=1, le=5)
    business_continuity: int = Field(ge=1, le=5)
    reputational_impact: int = Field(ge=1, le=5)
    vip_safety_impact: int = Field(ge=1, le=5)
    likelihood_of_more_severe_outcomes: int = Field(ge=1, le=5)
    hipo_classification: str


class ExpertReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1)
    reviewer_role: str = Field(min_length=1)
    expected: ExpertResult


class EvaluationRunRequest(BaseModel):
    model: str | None = None
    rule_version: str | None = None


@asynccontextmanager
async def lifespan(
    app: FastAPI,
) -> AsyncIterator[None]:
    """
    Initialize shared application services when FastAPI starts
    and close them cleanly when the application shuts down.
    """

    retriever: RetrieverService | None = None

    try:
        retriever = RetrieverService()
    except Exception as exc:
        print(f"Retriever initialization failed: {exc}")

    app.state.retriever = retriever
    app.state.workflow = IncidentWorkflow()
    app.state.analyzer = IncidentAnalyzer(retriever)
    app.state.accuracy_evaluator = (
        AccuracyEvaluator(retriever.collection.database) if retriever is not None else None
    )
    app.state.verified_incident_store = (
        VerifiedIncidentStore(retriever) if retriever is not None else None
    )
    if app.state.accuracy_evaluator is not None:
        app.state.accuracy_evaluator.ensure_indexes()

    try:
        yield

    finally:
        if retriever is not None:
            retriever.close()


app = FastAPI(
    title="FlexiCore Incident RAG Prototype",
    version="0.1.0",
    lifespan=lifespan,
)

# One Render worker owns these short-lived states. Completed workflow data is
# persisted to MongoDB by the background task.
analysis_jobs: dict[str, dict[str, Any]] = {}


def save_analysis_job(session_id: str, job: dict[str, Any]) -> None:
    """Persist background-job state so Render restarts do not lose it."""

    analysis_jobs[session_id] = dict(job)
    retriever = getattr(app.state, "retriever", None)
    if retriever is None:
        return
    retriever.collection.database["analysis_jobs"].update_one(
        {"session_id": session_id},
        {
            "$set": {**job, "updated_at": datetime.now(timezone.utc)},
            "$setOnInsert": {
                "session_id": session_id,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


def load_analysis_job(session_id: str) -> dict[str, Any] | None:
    """Load job state from memory, falling back to MongoDB."""

    job = analysis_jobs.get(session_id)
    if job is not None:
        return dict(job)
    retriever = getattr(app.state, "retriever", None)
    if retriever is None:
        return None
    stored = retriever.collection.database["analysis_jobs"].find_one(
        {"session_id": session_id},
        {"_id": 0, "session_id": 0, "created_at": 0, "updated_at": 0},
    )
    if stored is not None:
        analysis_jobs[session_id] = dict(stored)
    return stored

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health() -> dict[str, Any]:
    """
    Basic API health check.
    """

    retriever = getattr(app.state, "retriever", None)

    analyzer = getattr(app.state, "analyzer", None)
    agent = getattr(analyzer, "llm_analyzer", None)
    return {
        "status": "healthy" if retriever is not None else "degraded",
        "application": "flexicore-incident-rag-prototype",
        "retriever_ready": retriever is not None,
        "embedding_provider": settings.embedding_provider,
        "agent_framework": "llamaindex",
        "agentic_model": getattr(agent, "model", settings.ollama_model),
        "cloud_agent_ready": bool(getattr(agent, "cloud_available", False)),
        "active_agent_model": (
            getattr(agent, "model", settings.gemini_agent_model)
            if bool(getattr(agent, "cloud_available", False))
            else settings.ollama_model
        ),
        "local_fallback_model": settings.ollama_model,
        "rag_ready": retriever is not None,
        "cloud_retrieval_critic_ready": bool(getattr(agent, "cloud_available", False)),
        "deterministic_crag_fallback_enabled": settings.deterministic_crag_fallback_enabled,
        "score_verifier_enabled": settings.score_verifier_enabled,
        "parameter_scorer_enabled": settings.parameter_scorer_enabled,
        "parameter_scorer_confidence_threshold": settings.parameter_scorer_confidence_threshold,
        "verified_example_confidence_threshold": settings.verified_example_confidence_threshold,
    }


# ============================================================
# DATABASE HEALTH CHECK
# ============================================================

@app.get("/database-health")
def database_health() -> dict[str, str]:
    """
    Check the MongoDB connection.
    """

    retriever = getattr(app.state, "retriever", None)

    if retriever is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retriever service is not initialized.",
        )

    retriever.client.admin.command("ping")

    database = retriever.collection.database

    return {
        "status": "connected",
        "database": database.name,
        "collection": retriever.collection.name,
    }


# ============================================================
# START INCIDENT WORKFLOW
# ============================================================

@app.post("/incident/workflow/start")
def start_incident_workflow(
    request: StartIncidentRequest,
) -> dict[str, Any]:
    """
    Start a new step-by-step incident workflow.

    The workflow begins at the first review step.
    """

    incident_text = request.incident_text.strip()

    if not incident_text:
        raise HTTPException(
            status_code=400,
            detail="Incident narrative cannot be empty.",
        )

    workflow: IncidentWorkflow = app.state.workflow
    analyzer = getattr(app.state, "analyzer", None)

    session = workflow.create_session(incident_text)
    session_id = session["session_id"]

    response = workflow.process_current_step(
        session_id=session_id,
        analyzer=analyzer,
    )
    persist_workflow_session(workflow, session_id)
    return response


@app.post("/incident/intake/validate")
def validate_incident_intake(request: StartIncidentRequest) -> dict[str, Any]:
    """Return missing mandatory facts and route taxonomy only after they are complete."""
    incident_text = request.incident_text.strip()
    if not incident_text:
        raise HTTPException(status_code=400, detail="Incident narrative cannot be empty.")
    workflow: IncidentWorkflow = app.state.workflow
    analyzer = getattr(app.state, "analyzer", None)
    return workflow.validate_intake(incident_text, analyzer)


def process_incident_workflow_job(session_id: str) -> None:
    """Run long incident analysis after the start response is returned."""

    workflow: IncidentWorkflow = app.state.workflow
    analyzer = getattr(app.state, "analyzer", None)
    try:
        response = workflow.process_current_step(
            session_id=session_id,
            analyzer=analyzer,
        )
        persist_workflow_session(workflow, session_id)
        save_analysis_job(session_id, {"status": "completed", "result": response})
    except Exception as exc:
        print(f"Background incident analysis failed for {session_id}: {exc}")
        save_analysis_job(session_id, {"status": "failed", "error": str(exc)})


@app.post("/incident/workflow/start-async", status_code=status.HTTP_202_ACCEPTED)
def start_incident_workflow_async(
    request: StartIncidentRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Create a workflow immediately and process it in the background."""

    incident_text = request.incident_text.strip()
    if not incident_text:
        raise HTTPException(status_code=400, detail="Incident narrative cannot be empty.")

    workflow: IncidentWorkflow = app.state.workflow
    session = workflow.create_session(incident_text)
    session_id = session["session_id"]
    persist_workflow_session(workflow, session_id)
    save_analysis_job(session_id, {"status": "processing"})
    background_tasks.add_task(process_incident_workflow_job, session_id)
    return {"session_id": session_id, "status": "processing"}


@app.get("/incident/workflow/{session_id}/status")
def get_incident_workflow_job_status(session_id: str) -> dict[str, Any]:
    """Return background-analysis progress without a long HTTP connection."""

    job = load_analysis_job(session_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis job was not found.")
    return {"session_id": session_id, **job}


# ============================================================
# SET CURRENT STEP RESULT
# ============================================================

@app.post("/incident/workflow/{session_id}/process")
def process_current_workflow_step(
    session_id: str,
) -> dict[str, Any]:
    """Process the current workflow step and return the pending result."""

    workflow: IncidentWorkflow = app.state.workflow
    analyzer = getattr(app.state, "analyzer", None)

    try:
        response = workflow.process_current_step(
            session_id=session_id,
            analyzer=analyzer,
        )
        persist_workflow_session(workflow, session_id)
        return response
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc


@app.post("/incident/workflow/{session_id}/result")
def set_workflow_result(
    session_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """
    Store the generated result for the current step.

    The frontend can then ask the user whether
    the result is correct.
    """

    workflow: IncidentWorkflow = app.state.workflow

    try:
        response = workflow.set_pending_result(
            session_id=session_id,
            result=result,
        )
        persist_workflow_session(workflow, session_id)
        return response

    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc


# ============================================================
# CONFIRM CURRENT STEP
# ============================================================

@app.post("/incident/workflow/{session_id}/confirm")
def confirm_workflow_step(
    session_id: str,
    request: ConfirmStepRequest,
) -> dict[str, Any]:
    """
    Confirm or reject the current workflow step.

    approved=True
        -> save result
        -> move to next step

    approved=False
        -> correction is required
    """

    workflow: IncidentWorkflow = app.state.workflow
    analyzer = getattr(app.state, "analyzer", None)

    try:
        response = workflow.confirm_current_step(
            session_id=session_id,
            approved=request.approved,
            correction=request.correction,
            analyzer=analyzer,
        )
        persist_workflow_session(workflow, session_id)
        return response

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


# ============================================================
# GET WORKFLOW SESSION
# ============================================================

@app.get(
    "/incident/workflow/"
    "{session_id}"
)
def get_workflow_session(
    session_id: str,
) -> dict[str, Any]:
    """
    Return the complete current workflow state.
    """

    workflow: IncidentWorkflow = (
        app.state.workflow
    )

    try:
        return (
            workflow.get_session(
                session_id
            )
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc


# ============================================================
# GET CURRENT WORKFLOW STEP
# ============================================================

@app.get(
    "/incident/workflow/"
    "{session_id}/current-step"
)
def get_current_workflow_step(
    session_id: str,
) -> dict[str, Any]:
    """
    Return the current review step for a session.
    """

    workflow: IncidentWorkflow = (
        app.state.workflow
    )

    try:
        step = (
            workflow.get_current_step(
                session_id
            )
        )

        session = (
            workflow.get_session(
                session_id
            )
        )

        return {
            "session_id": session_id,
            "current_step": step,
            "completed": session.get(
                "completed",
                False,
            ),
            "pending": session.get(
                "pending"
            ),
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc


def get_accuracy_evaluator() -> AccuracyEvaluator:
    evaluator = getattr(app.state, "accuracy_evaluator", None)
    if evaluator is None:
        raise HTTPException(status_code=503, detail="Accuracy evaluator is unavailable.")
    return evaluator


@app.post("/incident/{query_id}/expert-review")
def submit_expert_review(query_id: str, request: ExpertReviewRequest) -> dict[str, Any]:
    try:
        expected = get_accuracy_evaluator().save_expert_review(
            query_id=query_id,
            review=request.expected.model_dump(),
            reviewer=request.reviewer,
            reviewer_role=request.reviewer_role,
        )
        store = getattr(app.state, "verified_incident_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="Verified incident store is unavailable.")
        retrieval_example = store.promote(query_id)
        return {
            "query_id": query_id,
            "review_status": "verified",
            "expert_review": expected,
            "retrieval_example": retrieval_example,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/evaluation/run")
def run_evaluation(request: EvaluationRunRequest) -> dict[str, Any]:
    try:
        return get_accuracy_evaluator().run(
            model=request.model,
            rule_version=request.rule_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/evaluation/latest")
def latest_evaluation() -> dict[str, Any]:
    evaluator = get_accuracy_evaluator()
    result = evaluator.database["evaluation_runs"].find_one(
        {}, {"_id": 0}, sort=[("created_at", -1)]
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No evaluation run is available.")
    return result


@app.get("/evaluation/{run_id}/errors")
def evaluation_errors(run_id: str) -> dict[str, Any]:
    evaluator = get_accuracy_evaluator()
    score_fields = (
        "safety_impact", "damage_to_assets", "business_continuity",
        "reputational_impact", "vip_safety_impact",
        "likelihood_of_more_severe_outcomes",
    )
    query = {
        "evaluation_run_id": run_id,
        "$or": [
            {"correctness.domain_subdomain_pair": False},
            {"correctness.hipo": False},
            *({f"score_errors.{field}.exact": False} for field in score_fields),
        ]
    }
    errors = list(evaluator.database["evaluation_case_results"].find(query, {"_id": 0}))
    return {"evaluation_run_id": run_id, "error_count": len(errors), "errors": errors}


@app.get("/exports/incident-responses.xlsx")
def export_incident_responses(verified_only: bool = False) -> StreamingResponse:
    evaluator = get_accuracy_evaluator()
    records = load_export_records(evaluator.database, verified_only=verified_only)
    workbook = build_response_workbook(records)
    filename = f"incident_responses_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
# EXISTING ROUTERS
# ============================================================

app.include_router(
    retrieval_router
)

app.include_router(
    incident_analysis_router
)


def persist_workflow_session(workflow: IncidentWorkflow, session_id: str) -> None:
    """Store the latest workflow state in MongoDB."""
    retriever = getattr(app.state, "retriever", None)
    if retriever is None:
        raise RuntimeError("MongoDB is unavailable; the workflow was not saved.")

    session = workflow.get_session(session_id)
    database = retriever.collection.database
    now = datetime.now(timezone.utc)
    database["incident_queries"].update_one(
        {"query_id": session_id},
        {"$set": {
            "incident_text": session["incident_text"],
            "processing_status": "completed" if session["completed"] else "in_progress",
            "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    database["incident_responses"].update_one(
        {"query_id": session_id},
        {"$set": {
            "response_id": session_id,
            "confirmed_responses": session["confirmed"],
            "corrections": session["corrections"],
            "pending_response": session["pending"],
            "current_step": workflow.get_current_step(session_id),
            "completed": session["completed"],
            "updated_at": now,
        }, "$setOnInsert": {
            "created_at": now,
            "model_prediction": (session.get("pending") or {}).get("result"),
            "prediction_metadata": {
                "model": settings.ollama_model,
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "agentic_model": settings.gemini_agent_model,
                "local_fallback_model": settings.ollama_model,
                "rule_version": "coupled-impact-likelihood-v2",
                "prompt_version": "hipo-scoring-v3",
                "created_at": now,
            },
            "review_status": "unreviewed",
        }},
        upsert=True,
    )
