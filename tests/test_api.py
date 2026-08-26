from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_accepts_local_host_and_rejects_dns_rebinding() -> None:
    client = TestClient(app, base_url="http://localhost")
    try:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert (
            client.get("/api/health", headers={"host": "attacker.example"}).status_code
            == 400
        )
    finally:
        client.close()
