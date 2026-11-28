from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.deps import get_current_user
from app.main import app


class PublicHealthTest(unittest.TestCase):
    def test_health_reports_availability_only(self) -> None:
        client = TestClient(app)
        for status, ok in (("ok", True), ("failed", False)):
            with self.subTest(status=status), patch(
                "app.services.diagnostics.check_postgres",
                new=AsyncMock(return_value={
                    "name": "postgres", "status": status, "required": True,
                    "details": {"host": "private-db", "database": "private-db-name"},
                }),
            ):
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"ok": ok})

    def test_operational_routes_are_removed(self) -> None:
        client = TestClient(app)
        paths = ("/api/diagnostics", "/api/metrics")
        for path in paths:
            self.assertEqual(client.get(path).status_code, 404)
        with patch.dict(app.dependency_overrides, {get_current_user: lambda: object()}):
            for path in paths:
                self.assertEqual(client.get(path).status_code, 404)
        for path in paths:
            self.assertNotIn(path, app.openapi()["paths"])
