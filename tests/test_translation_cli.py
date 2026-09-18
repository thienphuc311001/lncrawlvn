import argparse
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation.__main__ import first_chapters, run_owned, translate
from lncrawl.translation.api import snapshot
from lncrawl.translation.parsing import parse_chapters
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import AlreadyRunning, Store
from tests.test_translation import FakeGemini


class CLITests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_ownership_and_external_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory), {"raw": "第1章\n祖石。", "vietphrase": "Chương 1\nTổ thạch."}
            )
            store.progress("running", stage="Translating")
            started = asyncio.Event()

            class WaitingPipeline:
                async def run(self):
                    started.set()
                    await asyncio.Event().wait()

            async def runner():
                with store.execution():
                    await run_owned(store, 1)

            with patch("lncrawl.translation.__main__.Pipeline", return_value=WaitingPipeline()):
                task = asyncio.create_task(runner())
                await started.wait()
                self.assertTrue(store.is_active())
                self.assertEqual(snapshot(store)["status"], "running")
                with self.assertRaises(AlreadyRunning):
                    with store.execution():
                        pass
                store.write("cancel-request.json", {"requested": True})
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                self.assertEqual(store.read("progress.json")["status"], "cancelled")
                self.assertFalse(store.is_active())

    async def test_export_exactly_two_files_and_cached_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, vp = root / "raw.txt", root / "vp.txt"
            raw.write_text("第1章\n祖石发光。\n第2章\n他来了。", encoding="utf-8")
            vp.write_text("Chương 1\nTổ thạch sáng.\nChương 2\nHắn đến.", encoding="utf-8")
            arguments = argparse.Namespace(
                raw=raw,
                vietphrase=vp,
                dictionary=None,
                output=root / "output",
                workers=1,
                first=1,
                check_inputs=False,
            )
            fake = FakeGemini()
            with (
                patch.dict(os.environ, {"GOOGLE_AI_API_KEY": "test-key"}),
                patch("lncrawl.translation.__main__.APP_DIR", root / "data"),
                patch(
                    "lncrawl.translation.__main__.Scheduler",
                    return_value=Scheduler(transport=fake, spacing=0),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await translate(arguments), 0)
                count = len(fake.calls)
                self.assertEqual(await translate(arguments), 0)
                self.assertEqual(count, len(fake.calls))
                self.assertEqual(
                    sorted(p.name for p in arguments.output.iterdir()),
                    ["dictionary.json", "translated.json", "translated.txt"],
                )
                result = json.loads((arguments.output / "translated.json").read_text())
                self.assertEqual([ch["number"] for ch in result["chapters"]], [1])
                (arguments.output / "dictionary.json").write_text("my existing data")
                with self.assertRaises(ValueError):
                    await translate(arguments)
                self.assertEqual(
                    (arguments.output / "dictionary.json").read_text(), "my existing data"
                )

    def test_subset_does_not_include_following_volume(self):
        text = "第一卷 起\n第1章\n正文。\n第二卷 后\n第1章\n新正文。"
        selected = first_chapters(text, 1, "RAW")
        self.assertNotIn("第二卷", selected)
        self.assertEqual([ch.key for ch in parse_chapters(selected)], ["v1-c1"])
