"""24-0 gate: the app starts and answers the liveness probe."""

from fastapi.testclient import TestClient

from fulfill.main import app


def test_health_reports_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
