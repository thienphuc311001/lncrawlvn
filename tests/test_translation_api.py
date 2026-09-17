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

    async def test_structured_errors_and_duplicate_artifact_acceptance(self):
        from lncrawl.translation.models import PARSER_VERSION

        valid = {
            "raw": "Chapter 1: 第1章 开始\n第1章 开始\n正文。",
            "vietphrase": "Chương 1\nNội dung.",
        }
        with patch.object(api, "start", return_value={"status": "pending"}) as start:
            response = await self.client.post("/api/translation/jobs", json=valid)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(start.call_count, 1)
            store = start.call_args.args[0]
            self.assertEqual(store.read("parser-version.json"), {"version": PARSER_VERSION})
            for field, label in (("raw", "RAW"), ("vietphrase", "VIETPHRASE")):
                invalid = {**valid, field: "Chapter 3\n正文。\nChapter 2\n正文。"}
                response = await self.client.post("/api/translation/jobs", json=invalid)
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"]
                self.assertEqual(detail["input"], label)
                self.assertEqual(detail["line"], 3)
                self.assertEqual(detail["previous_chapter"], 3)
                self.assertEqual(detail["reason"], "backward_numbering_without_volume_boundary")
            self.assertEqual(start.call_count, 1)
            self.assertEqual(len(list(api.ROOT.iterdir())), 1)

    async def test_actual_truncated_exporter_heading_accepted(self):
        raw = (
            "Chapter 62: 第62章 秦舒曼恢复身体(感谢“Kawabunga”\n"
            + "-" * 60
            + "\n\n第62章 秦舒曼恢复身体（感谢“KAWABUNGA”的10万赏）\n正文。"
        )
        inputs = {"raw": raw, "vietphrase": "Chương 62\nNội dung."}
        with patch.object(api, "start", return_value={"status": "pending"}) as start:
            response = await self.client.post("/api/translation/jobs", json=inputs)
            self.assertEqual(response.status_code, 202, response.text)
            start.assert_called_once()
            inputs["raw"] = raw.replace("\n\n第62章", "\n真实正文\n第62章")
            response = await self.client.post("/api/translation/jobs", json=inputs)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["reason"], "duplicate_chapter")
            start.assert_called_once()

    async def test_legacy_resume_refused_but_completed_output_preserved(self):
        store = Store(api.ROOT, {"raw": "第1章\n正文。", "vietphrase": "Chương 1\nVăn."})
        store.progress("cancelled")
        with patch.object(api, "start") as start:
            response = await self.client.post(f"/api/translation/jobs/{store.id}/resume")
            self.assertEqual(response.status_code, 409)
            self.assertIn("Resubmit", response.json()["detail"])
            start.assert_not_called()
        store.write("translated.json", {"chapters": []})
        store.progress("done")
        response = await self.client.post(f"/api/translation/jobs/{store.id}/resume")
        self.assertEqual(response.status_code, 202)
        response = await self.client.get(
            f"/api/translation/jobs/{store.id}/outputs/translated.json"
        )
        self.assertEqual(response.status_code, 200)

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
