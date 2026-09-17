import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lncrawl.translation import api
from lncrawl.translation.store import Store


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(api, "ROOT", Path(self.temp.name))
        self.root.start()
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        api._scheduler = None

    async def asyncTearDown(self):
        await api.shutdown()
        api._tasks.clear()
        api._scheduler = None
        await self.client.aclose()
        self.root.stop()
        self.temp.cleanup()

    async def test_config_exposes_only_fixed_models_and_no_key(self):
        with patch.dict(os.environ, {"GOOGLE_AI_API_KEY": "secret-test-key"}):
            response = await self.client.get("/api/translation/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tuple(response.json()["models"].values()), api.MODELS)
        self.assertNotIn("secret-test-key", response.text)

    async def test_inputs_fail_before_provider_and_unknown_fields_rejected(self):
        response = await self.client.post(
            "/api/translation/jobs",
            json={"raw": "第1章\n他走了。", "vietphrase": "Chương 2\nĐi rồi."},
        )
        self.assertEqual(response.status_code, 422)
        response = await self.client.post(
            "/api/translation/jobs",
            json={"raw": "第1章\n他走了。", "vietphrase": "Chương 1\nĐi rồi.", "model": "custom"},
        )
        self.assertEqual(response.status_code, 422)

    async def test_immediate_cancel_and_restart_visibility(self):
        store = Store(
            api.ROOT,
            {"raw": "第1章\n他走了。", "vietphrase": "Chương 1\nĐi rồi.", "dictionary": None},
        )
        store.progress("running", stage="Translation")
        response = await self.client.get(f"/api/translation/jobs/{store.id}")
        self.assertEqual(response.json()["status"], "interrupted")
        # A task cancelled before its coroutine starts still needs explicit cleanup.
        api._tasks[store.id] = asyncio.create_task(asyncio.sleep(60))
        response = await self.client.post(f"/api/translation/jobs/{store.id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertNotIn(store.id, api._tasks)
        response = await self.client.get(
            f"/api/translation/jobs/{store.id}/outputs/translated.json"
        )
        self.assertEqual(response.status_code, 409)
        response = await self.client.get(f"/api/translation/jobs/{store.id}/outputs/inputs.json")
        self.assertEqual(response.status_code, 404)

    async def test_completed_job_is_idempotent_without_api_key(self):
        inputs = {"raw": "第1章\n他走了。", "vietphrase": "Chương 1\nĐi rồi.", "dictionary": None}
        store = Store(api.ROOT, inputs)
        store.progress("done")
        with patch.dict(os.environ, {"GOOGLE_AI_API_KEY": ""}):
            response = await self.client.post("/api/translation/jobs", json=inputs)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "done")
        self.assertNotIn(store.id, api._tasks)
