"""Multi-volume pipeline and API integration tests (volumed chapters, parser marker)."""

import tempfile
import unittest
from pathlib import Path

from lncrawl.translation.models import Inputs
from lncrawl.translation.pipeline import Pipeline
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store
from tests.test_translation import FakeGemini


class MultiVolumePipelineTests(unittest.IsolatedAsyncioTestCase):
    """Reuses the proven FakeGemini transport and helpers from PipelineTests."""

    def volume_store(self, root):
        raw = (
            "第一卷 起\n"
            "第1章 开始\n他走了。\n"
            "第2章 继续\n她来了。\n"
            "第二卷 起\n"
            "第1章 又一\n内容如下。"
        )
        vp = (
            "Quyển 1\n"
            "Chương 1 Bắt đầu\nHắn đi rồi.\n"
            "Chương 2 Tiếp\nNàng đến.\n"
            "Quyển 2\n"
            "Chương 1 Lại một lần\nNội dung mới."
        )
        return Store(Path(root), Inputs(raw=raw, vietphrase=vp).model_dump())

    async def test_volume_keys_isolate_checkpoints_and_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.volume_store(root)
            fake = FakeGemini()
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            self.assertEqual(store.read("progress.json")["status"], "done")
            files = sorted(str(p.relative_to(store.path)) for p in store.path.rglob("*.json"))
            self.assertIn("alignment/v1-c1.json", files)
            self.assertIn("alignment/v2-c1.json", files)
            self.assertIn("chapters/v1-c1.json", files)
            self.assertIn("chapters/v2-c1.json", files)
            self.assertNotIn("chapters/1.json", files)
            translated = store.read("translated.json")
            self.assertEqual([c["number"] for c in translated["chapters"]], [1, 2, 1])
            self.assertEqual([c.get("volume") for c in translated["chapters"]], [1, 1, 2])
            # Local units scope repeated chapter numbers by volume.
            units = store.read("terminology-index.json")["index"]["units"]
            self.assertEqual({unit["chapter_key"] for unit in units}, {"v1-c1", "v1-c2", "v2-c1"})
            # Checkpoints are content-addressed per volume-scoped key, so a second
            # run performs zero provider calls.
            count = len(fake.calls)
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            self.assertEqual(count, len(fake.calls))
