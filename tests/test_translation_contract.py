import json
import tempfile
import unittest
from pathlib import Path

from lncrawl.translation import prompts
from lncrawl.translation.dictionary import export_dictionary, terminology_findings
from lncrawl.translation.models import Inputs, Segment, Term, Translation
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.preprocessing import build_index
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store, digest


class TranslationContractTests(unittest.TestCase):
    def test_only_confirmed_entries_are_exported_and_shared_targets_are_valid(self):
        terms = {
            "甲": Term(source="甲", translation="X", type="concept", status="locked"),
            "乙": Term(source="乙", translation="X", type="concept", status="locked"),
            "会派": Term(
                source="会派",
                translation="Sẽ phái",
                type="verb_phrase",
                status="report_only",
            ),
        }
        dictionary = export_dictionary(terms)
        self.assertEqual({item["source"] for item in dictionary["entries"]}, {"甲", "乙"})
        self.assertEqual(
            terminology_findings(
                [term.model_dump() for term in terms.values()],
                "甲乙会派",
                "X X Sẽ phái",
            ),
            [],
        )

    def test_vietphrase_is_evidence_for_raw_candidates_not_an_independent_source(self):
        pairs = validate_inputs(
            "第1章\n丁小七来了。\n丁小七点头。\n丁小七走了。",
            "Chương 1\nĐinh Tiểu Thất đến.\nĐinh Tiểu Thất gật đầu.\nĐinh Tiểu Thất đi rồi.",
        )
        index = build_index(
            pairs,
            {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs},
            [],
        )
        self.assertIn("丁小七", index["candidates"])
        self.assertNotIn("Đinh Tiểu Thất", index["candidates"])

    def test_freeze_discards_ignore_and_detects_post_freeze_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(raw="第1章\n甲。", vietphrase="Chương 1\nX.").model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.freeze(
                {
                    "entries": [
                        {"source": "甲", "translation": "X", "type": "concept", "status": "locked"},
                        {
                            "source": "会派",
                            "translation": "Sẽ phái",
                            "type": "verb_phrase",
                            "status": "report_only",
                        },
                    ]
                }
            )
            self.assertEqual(list(pipeline.terms), ["甲"])
            pipeline.terms["甲"].translation = "Y"
            with self.assertRaisesRegex(QualityError, "Frozen dictionary changed"):
                pipeline.assert_frozen()


class TargetedRepairContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_recomputes_stale_expanded_terminology_finding(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n邱途接了电话。",
                    vietphrase="Chương 1\nKhâu Đồ nhận điện thoại.",
                ).model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.freeze(
                {
                    "entries": [
                        {
                            "source": "邱途",
                            "translation": "Khâu Đồ",
                            "type": "character",
                            "status": "locked",
                        }
                    ]
                }
            )
            task = "chapter:1:chunk:0"
            store.write(
                f"validation-rejections/{digest(task)}.json",
                {
                    "task": task,
                    "findings": [
                        {
                            "source": "邱途接",
                            "required_translation": "Khâu Đồ tiếp",
                        }
                    ],
                },
            )
            payload = {
                "raw_title": "第1章",
                "raw": [{"id": 0, "text": "邱途接了电话。"}],
                "terminology": [
                    {
                        "source": "邱途",
                        "translation": "Khâu Đồ",
                        "type": "character",
                        "status": "locked",
                    }
                ],
                "location": {"chapter": 1, "chunk": 0},
            }
            translation = Translation(
                title="Chương 1",
                segments=[Segment(id=0, text="Khâu Đồ nhận điện thoại.")],
            )
            await pipeline.validate_and_repair(task, payload, translation)
            recomputed = store.read(f"validation-rejections/{digest(task)}.json")
            self.assertEqual(recomputed["findings"], [])
            self.assertNotIn("邱途接", json.dumps(recomputed, ensure_ascii=False))

    async def test_persistent_failure_stops_after_one_repair(self):
        calls = []

        async def transport(model, body):
            instruction = body["systemInstruction"]["parts"][0]["text"]
            calls.append(instruction)
            self.assertEqual(instruction, prompts.REPAIR)
            request = json.loads(body["contents"][0]["parts"][0]["text"])
            self.assertEqual(request["affected_ids"], [0])
            self.assertEqual(request["issues"][0]["source"], "黄哥")
            self.assertEqual(request["issues"][0]["required_translation"], "Hoàng ca")
            self.assertNotIn("走", request["issues"][0]["source"])
            return {"segments": [{"id": 0, "text": "anh Hoàng đi rồi."}]}

        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(raw="第1章\n黄哥走了。", vietphrase="Chương 1\nanh Hoàng đi rồi.").model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(transport=transport, spacing=0))
            pairs = validate_inputs(store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"])
            pipeline.pairs = pairs
            await pipeline.align(pairs[0])
            pipeline.freeze(
                {
                    "entries": [
                        {
                            "source": "黄哥",
                            "translation": "Hoàng ca",
                            "type": "character",
                            "status": "locked",
                        }
                    ]
                }
            )
            payload = {
                "raw_title": "第1章",
                "vp_title": "Chương 1",
                "raw": [{"id": 0, "text": "黄哥走了。"}],
                "vp": [{"id": 0, "text": "anh Hoàng đi rồi."}],
                "terminology": [
                    {
                        "source": "黄哥",
                        "translation": "Hoàng ca",
                        "type": "character",
                        "status": "locked",
                    }
                ],
                "location": {"chapter": 1, "chunk": 0},
            }
            translation = Translation(
                title="Chương 1", segments=[Segment(id=0, text="anh Hoàng đi rồi.")]
            )
            with self.assertRaisesRegex(QualityError, "after 1 targeted repair"):
                await pipeline.validate_and_repair("chapter:1:chunk:0", payload, translation)
            self.assertEqual(calls.count(prompts.REPAIR), 1)


if __name__ == "__main__":
    unittest.main()
