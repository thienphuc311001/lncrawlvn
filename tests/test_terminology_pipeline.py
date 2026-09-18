import json
import tempfile
import unittest
from pathlib import Path

from lncrawl.translation.dictionary import load_legacy, terminology_findings
from lncrawl.translation.models import Inputs, Segment, Term, Translation
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline
from lncrawl.translation.preprocessing import build_index, local_resolution
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store, digest
from lncrawl.translation.validation import local_findings


class TerminologyPipelineRegressionTests(unittest.TestCase):
    def test_generic_candidates_are_report_only_and_not_resolver_candidates(self):
        pairs = validate_inputs(
            "第1章\n会派。\n108种增进感情的方式。\n世界基石。\n任队长。\n丁小七来了。\n丁小七走了。\n丁小七回来了。",
            "Chương 1\nSẽ phái.\n108 cách tăng tình cảm.\nNền tảng thế giới.\nĐội trưởng Nhậm.\nĐinh Tiểu Thất đến.\nĐinh Tiểu Thất đi.\nĐinh Tiểu Thất về.",
        )
        index = build_index(
            pairs, {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs}, []
        )

        self.assertNotIn("会派", index["candidates"])
        self.assertNotIn("108种增进感情的方式", index["candidates"])
        self.assertNotIn("世界基石", index["candidates"])
        self.assertNotIn("任队长", index["candidates"])
        self.assertIn("丁小七", index["candidates"])
        self.assertEqual(index["report_only"]["会派"]["status"], "report_only")
        self.assertFalse(index["report_only"]["会派"]["enforceable"])

    def test_local_consensus_can_be_explicitly_enforceable(self):
        pairs = validate_inputs(
            "第1章\n丁小七来了。\n丁小七走了。\n丁小七回来了。",
            "Chương 1\nĐinh Tiểu Thất đến.\nĐinh Tiểu Thất đi.\nĐinh Tiểu Thất về.",
        )
        index = build_index(
            pairs, {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs}, []
        )
        term = local_resolution(index["candidates"]["丁小七"])
        self.assertIsNotNone(term)
        self.assertTrue(term.enforceable)

    def test_report_only_and_review_entries_are_not_strict_validator_constraints(self):
        report_only = Term(
            source="世界基石",
            translation="Nền tảng thế giới",
            type="common_noun",
            status="report_only",
            resolution_reason="generic_compositional_phrase",
        )
        review = Term(
            source="不朽",
            translation="Bất Hủ",
            type="concept",
            status="provisional",
            needs_review=True,
            resolution_reason="semantic_gender_ambiguous",
        )
        payload = {
            "raw_title": "第1章",
            "raw": [{"id": 0, "text": "世界基石与不朽。"}],
            "terminology": [report_only.model_dump(), review.model_dump()],
        }
        translation = Translation(
            title="Chương 1",
            segments=[Segment(id=0, text="Nền móng của thế giới và Bất Hủ.")],
        )
        self.assertEqual(local_findings(payload, translation), [])
        self.assertFalse(
            Term(
                source="不朽",
                translation="Bất Hủ",
                type="concept",
                status="provisional",
                semantic_resolution="resolved",
                confidence=0.0,
                enforceable=True,
            ).enforceable
        )

    def test_structured_finding_and_longest_overlap(self):
        terms = [
            Term(source="黄哥", translation="Hoàng ca", type="character", status="locked"),
            Term(source="龙山", translation="Long Sơn", type="location", status="locked"),
            Term(source="龙山道", translation="Long Sơn Đạo", type="location", status="locked"),
        ]
        findings = terminology_findings(
            [term.model_dump() for term in terms],
            "黄哥在龙山道，龙山。",
            "anh Hoàng ở Long Sơn đạo, Long Sơn.",
            {"chapter": 3, "chunk": 0},
        )
        by_source = {finding["source"]: finding for finding in findings}
        self.assertEqual(by_source["黄哥"]["required_translation"], "Hoàng ca")
        self.assertEqual(by_source["黄哥"]["actual_text"], "anh Hoàng")
        self.assertEqual(by_source["黄哥"]["reason"], "frozen_mapping_mismatch")
        # Presence of the confirmed target is sufficient; a shorter source key
        # is not counted inside the longer source key.
        self.assertNotIn("龙山", by_source)
        self.assertNotIn("龙山道", by_source)

    def test_shared_vietnamese_surface_is_not_identity_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(raw="第1章\n甲乙。", vietphrase="Chương 1\nX.").model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.terms = {
                "甲": Term(source="甲", translation="X", type="concept", status="locked"),
                "乙": Term(source="乙", translation="X", type="concept", status="locked"),
            }
            pipeline.index = {"units": [{"raw": "甲乙。"}]}
            self.assertEqual(pipeline.dictionary_conflicts(), set())

    def test_legacy_report_only_fallback_is_migrated_to_non_enforceable(self):
        terms, problems = load_legacy(
            {
                "entries": [
                    {
                        "source": "会派",
                        "translation": "Sẽ phái",
                        "type": "verb_phrase",
                        "status": "provisional",
                        "fallback": "report_only",
                    }
                ]
            }
        )
        self.assertEqual(problems, [])
        self.assertEqual(terms[0].status, "report_only")
        self.assertFalse(terms[0].enforceable)


class TerminologyResumeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_report_only_frozen_entry_is_migrated_without_retranslation_contract(self):
        with tempfile.TemporaryDirectory() as root:
            inputs = Inputs(
                raw="第1章\n会派。",
                vietphrase="Chương 1\nSẽ phái.",
            ).model_dump()
            store = Store(Path(root), inputs)
            old_dictionary = {
                "version": 2,
                "entries": [
                    {
                        "source": "会派",
                        "translation": "Sẽ phái",
                        "type": "verb_phrase",
                        "status": "provisional",
                        "fallback": "report_only",
                    }
                ],
            }
            store.write(
                "frozen-dictionary.json",
                {
                    "input_hash": "legacy-input-hash",
                    "dictionary_hash": digest(old_dictionary),
                    "dictionary": old_dictionary,
                },
            )

            async def transport(model, body):
                data = json.loads(body["contents"][0]["parts"][0]["text"])
                return {
                    "title": data["CHAPTER CONTEXT"]["vp_title"],
                    "segments": [
                        {
                            "id": item["id"],
                            "text": data["VIETPHRASE REFERENCE"][item["id"]]["text"],
                        }
                        for item in data["RAW TO TRANSLATE"]
                    ],
                }

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            self.assertEqual(store.read("dictionary.json")["entries"], [])


if __name__ == "__main__":
    unittest.main()
