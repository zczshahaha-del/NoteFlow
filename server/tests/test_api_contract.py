from __future__ import annotations

import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.schemas.common import API_ERROR_RESPONSES
from app.services.observability import install_observability, metrics_snapshot


class ApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI(responses=API_ERROR_RESPONSES)
        install_observability(app)

        @app.get("/conflict")
        async def conflict():
            raise HTTPException(status_code=409, detail="version changed")

        @app.get("/ok")
        async def ok():
            return {"ok": True}

        @app.get("/limited")
        async def limited():
            raise HTTPException(429, "请稍候", headers={"retry-after": "17", "X-Internal": "private"})

        @app.get("/note-conflict")
        async def note_conflict():
            raise HTTPException(409, {"code": "NOTE_VERSION_CONFLICT", "message": "已更新",
                                      "currentContentHash": "f" * 64, "currentUpdatedAt": "2026-11-19T01:23:45",
                                      "internal": "private"})

        self.client = TestClient(app)
        self.app = app

    def test_http_errors_use_stable_error_envelope(self) -> None:
        response = self.client.get("/conflict", headers={"X-Request-Id": "test-request-1"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], {"code": "CONFLICT", "message": "version changed"})
        self.assertEqual(response.json()["requestId"], "test-request-1")
        self.assertEqual(response.headers["x-request-id"], "test-request-1")

    def test_rate_limit_and_note_conflict_keep_only_public_recovery_fields(self) -> None:
        limited = self.client.get("/limited")
        self.assertEqual(limited.headers["Retry-After"], "17")
        self.assertNotIn("X-Internal", limited.headers)
        conflict = self.client.get("/note-conflict")
        self.assertEqual(conflict.json()["error"]["details"], {
            "currentContentHash": "f" * 64, "currentUpdatedAt": "2026-11-19T01:23:45",
        })

    def test_security_headers_and_metrics_are_recorded(self) -> None:
        response = self.client.get("/ok")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        snapshot = metrics_snapshot()
        self.assertGreaterEqual(snapshot["routes"]["GET /ok"]["requests"], 1)

    def test_openapi_documents_shared_error_envelope(self) -> None:
        schema = self.app.openapi()
        conflict_response = schema["paths"]["/conflict"]["get"]["responses"]["409"]

        self.assertEqual(
            conflict_response["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ApiErrorEnvelope",
        )
        self.assertIn("ApiErrorDetail", schema["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
