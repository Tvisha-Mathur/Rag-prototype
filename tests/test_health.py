"""Purpose: Tests test health behavior and expected regressions.

Used by: Executed by pytest as part of the automated regression suite.
"""

from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"healthy", "degraded"}
    assert payload["application"] == "flexicore-incident-rag-prototype"
    assert isinstance(payload["retriever_ready"], bool)
    assert payload["rag_ready"] == payload["retriever_ready"]
    assert isinstance(payload["cloud_retrieval_critic_ready"], bool)
    assert payload["deterministic_crag_fallback_enabled"] is True
    assert payload["score_verifier_enabled"] is True
