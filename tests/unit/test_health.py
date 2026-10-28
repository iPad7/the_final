"""앱이 기동하고 liveness 응답을 주는지 확인한다(24-0 관문)."""

from fastapi.testclient import TestClient

from fulfill.main import app


def test_health_reports_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
