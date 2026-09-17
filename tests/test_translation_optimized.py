"""Behavioral proofs for the local-first, globally frozen translation pipeline."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation import prompts
from lncrawl.translation.models import Inputs, Translation
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.preprocessing import build_index, local_resolution
from lncrawl.translation.scheduler import ProviderError, Scheduler
from lncrawl.translation.store import Store, digest, pipeline_identity
from lncrawl.translation.validation import local_findings


class BookProvider:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.corrupt_id = None
        self.fail_chapter = None
        self.report_unresolved = False
        self.malformed_source = None

    async def __call__(self, model, body):
        instruction = body["systemInstruction"]["parts"][0]["text"]
        data = json.loads(body["contents"][0]["parts"][0]["text"])
        self.calls.append((instruction, data))
        if instruction == prompts.BATCH_RESOLVE:
            results = []
            for item in data["candidates"]:
                source = item["source"]
                term = None
                if source in ("老秦", "秦科长"):
                    term = {
                        "source": "秦舒曼",
                        "translation": "Tần Thư Mạn",
                        "type": "character",
                        "status": "locked",
                        "forms": {source: "Lão Tần" if source == "老秦" else "Khoa trưởng Tần"},
                    }
                elif source == "秦舒曼":
                    term = {
                        "source": source,
                        "translation": "Tần Thư Mạn",
                        "type": "character",
                        "status": "provisional",
                    }
                elif source == "祖石":
                    term = {
                        "source": source,
                        "translation": "Tổ Thạch",
                        "type": "artifact",
                        "status": "locked",
                    }
                elif source == "梦境术":
                    term = {
                        "source": source,
                        "translation": "Mộng Cảnh Thuật",
                        "type": "technique",
                        "status": "provisional",
                    }
                result = {
                    "source": source,
                    "decision": "ACCEPT" if term else "REJECT",
                    "term": term,
                    "reason": "Only fixture-attested terminology is accepted",
                    "eligibility": {
                        "complete_semantic_unit": True,
                        "named_or_novel_specific": bool(term),
                        "consistency_matters": True,
                        "evidence_supports": True,
                    },
                }
                if source == self.malformed_source:
                    result["eligibility"] = "invalid independent record"
                    self.malformed_source = None
                results.append(result)
            return {"results": results}
        if instruction == prompts.TRANSLATE:
            frozen = self.store.read("frozen-dictionary.json")
            if frozen is None or frozen["dictionary_hash"] != data["dictionary_hash"]:
                raise AssertionError("Translation started before dictionary freeze")
            if data["CHAPTER CONTEXT"]["number"] == self.fail_chapter:
                raise ProviderError("Simulated interruption")
            vp = {item["id"]: item["text"] for item in data["VIETPHRASE REFERENCE"]}
            result = {
                "title": data["CHAPTER CONTEXT"]["vp_title"],
                "segments": [
                    {
                        "id": item["id"],
                        "text": "未翻译原文" if item["id"] == self.corrupt_id else vp[item["id"]],
                    }
                    for item in data["RAW TO TRANSLATE"]
                ],
            }
            if self.report_unresolved:
                result["unresolved_terms"] = [{"source": "隐秘宫", "evidence": "隐秘宫开放。"}]
            return result
        if instruction == prompts.REPAIR:
            vp = {item["id"]: item["text"] for item in data["vp"]}
            return {
                "segments": [
                    {"id": value, "text": data["vp_title"] if value == -1 else vp[value]}
                    for value in data["affected_ids"]
                ]
            }
        raise AssertionError("Unnecessary AI operation: " + instruction[:50])


class OptimizedPipelineTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, root, raw=None, vp=None, dictionary=None):
        raw = raw or "第1章 开始\n秦舒曼走了。\n秦舒曼点头。\n秦舒曼回来了。"
        vp = (
            vp
            or "Chương 1 Mở đầu\nTần Thư Mạn đi rồi.\nTần Thư Mạn gật đầu.\nTần Thư Mạn quay lại."
        )
        return Store(Path(root), Inputs(raw=raw, vietphrase=vp, dictionary=dictionary).model_dump())

    async def run_book(self, store, provider):
        pipeline = Pipeline(store, Scheduler(transport=provider, spacing=0))
        await pipeline.run()
        return pipeline

    def test_deterministic_logical_lines_blank_runs_and_visual_wrap_irrelevance(self):
        pairs = validate_inputs(
            "第1章\n" + "甲" * 800 + "。\n\n乙。", "Chương 1\n" + "Một " * 600 + ".\n\n\nHai."
        )
        alignment = deterministic_alignment(*pairs[0])
        self.assertEqual([(g.raw, g.vp) for g in alignment.groups], [([0], [0]), ([1], [1])])

    def test_ambiguous_alignment_stops_locally_without_provider(self):
        pairs = validate_inputs("第1章\n甲。\n乙。", "Chương 1\nMột.")
        with self.assertRaisesRegex(ValueError, "Structural alignment failed"):
            deterministic_alignment(*pairs[0])
        pairs = validate_inputs("第1章\n甲。\n\n乙。", "Chương 1\nMột.\nHai.")
        with self.assertRaisesRegex(ValueError, "blank blocks"):
            deterministic_alignment(*pairs[0])

    def test_split_merge_recovery_requires_matching_punctuation_anchors(self):
        pair = validate_inputs("第1章\n甲。乙。", "Chương 1\nMột.\nHai.")[0]
        alignment = deterministic_alignment(*pair)
        self.assertEqual(alignment.groups[0].raw, [0])
        self.assertEqual(alignment.groups[0].vp, [0, 1])
        mixed = validate_inputs("第1章\n甲。乙。\n\n丙。", "Chương 1\nMột.\nHai.\n\nBa.")[0]
        alignment = deterministic_alignment(*mixed)
        self.assertEqual([(g.raw, g.vp) for g in alignment.groups], [([0], [0, 1]), ([1], [2])])

    def test_digit_term_occurrences_and_evidence_never_include_empty_matches(self):
        pairs = validate_inputs(
            "第1章\n《3个愿望》开始。\n他走了。", "Chương 1\n《Ba điều ước》 bắt đầu.\nHắn đi rồi."
        )
        index = build_index(pairs, {r.key: deterministic_alignment(r, v) for r, v in pairs}, [])
        candidate = index["candidates"]["3个愿望"]
        self.assertEqual(candidate["frequency"], 1)
        self.assertEqual(len(candidate["occurrences"]), 1)
        self.assertTrue(candidate["occurrences"][0]["positions"])
        self.assertEqual(len(candidate["representative_evidence"]), 1)

    def test_ordinary_fragments_and_unverified_name_readings_are_not_local_characters(self):
        pairs = validate_inputs(
            "第1章\n方法来了。贾枢从。苏小碗来了。\n方法点头。贾枢从。苏小碗点头。\n方法笑了。贾枢从。苏小碗笑了。",
            "Chương 1\nPhương pháp đến. Giả Trụ Cột. Tô Chén Nhỏ đến.\nPhương pháp gật đầu. Giả Trụ Cột. Tô Chén Nhỏ gật đầu.\nPhương pháp cười. Giả Trụ Cột. Tô Chén Nhỏ cười.",
        )
        index = build_index(pairs, {r.key: deterministic_alignment(r, v) for r, v in pairs}, [])
        self.assertNotIn("方法", index["candidates"])
        self.assertNotIn("贾枢从", index["candidates"])
        self.assertIsNone(local_resolution(index["candidates"]["苏小碗"]))

    async def test_obvious_consistent_full_name_uses_zero_ai_preprocessing(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertEqual([call[0] for call in provider.calls], [prompts.TRANSLATE])
            terms = store.read("dictionary.json")["entries"]
            self.assertEqual(
                [(t["source"], t["translation"], t["status"]) for t in terms],
                [("秦舒曼", "Tần Thư Mạn", "provisional")],
            )
            index = store.read("terminology-index.json")["index"]["candidates"]["秦舒曼"]
            self.assertEqual(index["frequency"], 3)
            self.assertEqual(index["vietphrase_variants"], {"Tần Thư Mạn": 3})
            stats = store.request_statistics()
            self.assertEqual(stats["requests"]["terminology_resolver"], 0)
            self.assertEqual(stats["total_requests"], 1)
            self.assertTrue(all(value == 0 for value in stats["local_ai_requests"].values()))

    async def test_late_chapter_term_is_resolved_before_first_translation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n他走了。\n第2章\n【梦境术】启动。",
                vp="Chương 1\nHắn đi rồi.\nChương 2\n【Mộng Cảnh Thuật】 khởi động.",
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertEqual(provider.calls[0][0], prompts.BATCH_RESOLVE)
            self.assertIn("梦境术", {item["source"] for item in provider.calls[0][1]["candidates"]})
            self.assertEqual(
                store.read("frozen-dictionary.json")["dictionary"]["entries"][0]["source"], "梦境术"
            )
            hashes = {
                data["dictionary_hash"]
                for instruction, data in provider.calls
                if instruction == prompts.TRANSLATE
            }
            self.assertEqual(len(hashes), 1)

    async def test_semantic_address_forms_are_batched_before_freeze(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n秦舒曼走了。\n秦舒曼点头。\n秦舒曼回来了。\n老秦就是秦舒曼。\n秦科长就是秦舒曼。",
                vp="Chương 1\nTần Thư Mạn đi rồi.\nTần Thư Mạn gật đầu.\nTần Thư Mạn quay lại.\nLão Tần là Tần Thư Mạn.\nKhoa trưởng Tần là Tần Thư Mạn.",
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            resolver = [
                data for instruction, data in provider.calls if instruction == prompts.BATCH_RESOLVE
            ]
            self.assertEqual(len(resolver), 1)
            sources = {item["source"] for item in resolver[0]["candidates"]}
            self.assertTrue({"老秦", "秦科长"}.issubset(sources))
            entry = next(
                t for t in store.read("dictionary.json")["entries"] if t["source"] == "秦舒曼"
            )
            self.assertEqual(entry["forms"]["老秦"], "Lão Tần")
            self.assertEqual(entry["forms"]["秦科长"], "Khoa trưởng Tần")
            self.assertEqual([call[0] for call in provider.calls][-1], prompts.TRANSLATE)

    async def test_structural_dictionary_errors_are_audited_locally(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n他走了。",
                vp="Chương 1\nHắn đi rồi.",
                dictionary={
                    "entries": [
                        {
                            "source": "无效",
                            "translation": "Vô hiệu",
                            "type": "character",
                            "gender": "invalid",
                        },
                        {"source": "空名", "translation": "", "type": "other_term"},
                        {"source": "破损", "translation": "Hỏng", "type": "bad_type"},
                        {
                            "source": "破损",
                            "translation": "Hỏng",
                            "type": "other_term",
                            "status": "bad_status",
                        },
                        {"source": "无效\u0000", "translation": "Vô hiệu", "type": "other_term"},
                        {
                            "source": "重复",
                            "translation": "Trùng",
                            "type": "character",
                            "aliases": ["别名", "别名"],
                        },
                        {
                            "source": "表格",
                            "translation": "Bảng",
                            "type": "character",
                            "forms": {"别名": ""},
                        },
                        {
                            "source": "断链",
                            "translation": "Hỏng",
                            "type": "character",
                            "canonical_id": "nonexistent",
                        },
                    ]
                },
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertEqual(len(store.read("legacy-audit.json")), 8)
            self.assertEqual([call[0] for call in provider.calls], [prompts.TRANSLATE])
            self.assertEqual(store.read("dictionary.json")["entries"], [])

    async def test_duplicate_inherited_source_preserves_locked_mapping_locally(self):
        with tempfile.TemporaryDirectory() as root:
            entry = {
                "source": "秦舒曼",
                "translation": "Tần Thư Mạn",
                "type": "character",
                "status": "locked",
            }
            store = self.make_store(
                root, dictionary={"entries": [entry, {**entry, "status": "provisional"}]}
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertEqual(
                [instruction for instruction, _ in provider.calls], [prompts.TRANSLATE]
            )
            self.assertEqual(store.read("dictionary.json")["entries"][0]["status"], "locked")
            self.assertIn("duplicate source", store.read("legacy-audit.json")[0]["reason"])

    async def test_concrete_inherited_canonical_conflict_uses_semantic_batch(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n秦科长就是秦舒曼。",
                vp="Chương 1\nKhoa trưởng Tần là Tần Thư Mạn.",
                dictionary={
                    "entries": [
                        {
                            "source": "秦舒曼",
                            "translation": "Tần Thư Mạn",
                            "type": "character",
                            "status": "locked",
                        },
                        {
                            "source": "秦科长",
                            "translation": "Tần Thư Mạn",
                            "type": "character",
                            "status": "provisional",
                        },
                    ]
                },
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            stats = store.request_statistics()
            self.assertEqual(stats["requests"]["semantic_dictionary_conflict"], 1)
            self.assertEqual(stats["requests"]["terminology_resolver"], 0)
            self.assertEqual(len(store.read("dictionary.json")["entries"]), 1)

    def test_pipeline_cache_identity_changes_with_inputs_prompts_and_models(self):
        inputs = Inputs(raw="第1章\n他走了。", vietphrase="Chương 1\nHắn đi rồi.").model_dump()
        before = pipeline_identity(inputs)
        self.assertNotEqual(before, pipeline_identity({**inputs, "dictionary": {"entries": []}}))
        with patch.object(prompts, "TRANSLATE", prompts.TRANSLATE + " changed contract"):
            self.assertNotEqual(before, pipeline_identity(inputs))
        with patch("lncrawl.translation.store.MODELS", ["changed-model"]):
            self.assertNotEqual(before, pipeline_identity(inputs))

    async def test_valid_locked_canonical_and_forms_reused_without_resolver(self):
        with tempfile.TemporaryDirectory() as root:
            dictionary = {
                "entries": [
                    {
                        "source": "秦舒曼",
                        "translation": "Tần Thư Mạn",
                        "type": "character",
                        "status": "locked",
                        "forms": {"秦科长": "Khoa trưởng Tần"},
                    }
                ]
            }
            store = self.make_store(
                root,
                raw="第1章\n秦科长点头。",
                vp="Chương 1\nKhoa trưởng Tần gật đầu.",
                dictionary=dictionary,
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertEqual([call[0] for call in provider.calls], [prompts.TRANSLATE])
            self.assertEqual(
                store.read("dictionary.json")["entries"],
                [{**dictionary["entries"][0], "gender": "unknown", "aliases": [], "evidence": ""}],
            )

    async def test_dictionary_is_frozen_and_identical_for_all_chunks(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n" + "\n".join(["秦舒曼走了。" * 500] * 3),
                vp="Chương 1\n" + "\n".join(["Tần Thư Mạn đi rồi. " * 500] * 3),
            )
            provider = BookProvider(store)
            pipeline = await self.run_book(store, provider)
            frozen = store.read("frozen-dictionary.json")
            self.assertGreaterEqual(
                sum(instruction == prompts.TRANSLATE for instruction, _ in provider.calls), 2
            )
            for _, data in provider.calls:
                self.assertEqual(data["dictionary_hash"], frozen["dictionary_hash"])
            with self.assertRaises(TypeError):
                pipeline.terms["新词"] = None
            before = digest(store.read("frozen-dictionary.json"))
            with self.assertRaisesRegex(QualityError, "frozen"):
                await pipeline.resolve("新词")
            self.assertEqual(before, digest(store.read("frozen-dictionary.json")))

    async def test_empty_paragraph_is_a_targeted_repairable_finding(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            provider = BookProvider(store)

            async def transport(model, body):
                result = await provider(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.TRANSLATE:
                    result["segments"][1]["text"] = ""
                return result

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            repairs = [
                data for instruction, data in provider.calls if instruction == prompts.REPAIR
            ]
            self.assertEqual([p["id"] for p in repairs[0]["raw"]], [1])
            self.assertEqual(store.request_statistics()["requests"]["translation"], 1)
            self.assertEqual(store.request_statistics()["requests"]["repair"], 1)

    async def test_physical_retry_and_model_fallback_statistics_are_separate(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            provider = BookProvider(store)
            first = True

            async def transport(model, body):
                nonlocal first
                if first:
                    first = False
                    raise ProviderError("Daily quota exhausted", daily_quota=True)
                return await provider(model, body)

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            stats = store.request_statistics()
            self.assertEqual(stats["logical_operations"]["translation"], 1)
            self.assertEqual(stats["requests"]["translation"], 2)
            self.assertEqual(stats["retry"], 1)
            self.assertEqual(stats["model_fallback"], 1)

    def test_local_validator_reports_title_placeholder_residue_order_and_names(self):
        payload = {
            "raw_title": "开始",
            "raw": [{"id": 0, "text": "秦舒曼说 {player}。"}, {"id": 1, "text": "他走了。"}],
            "terminology": [{"source": "秦舒曼", "translation": "Tần Thư Mạn"}],
        }
        output = Translation.model_validate(
            {
                "title": "Mở đầu\nExtra",
                "segments": [
                    {"id": 1, "text": "未翻译原文"},
                    {"id": 0, "text": "Ai đó nói {wrong}."},
                ],
            }
        )
        issues = local_findings(payload, output)
        self.assertTrue(
            {"name", "order", "terminology", "missing"}.issubset({issue.kind for issue in issues})
        )
        with self.assertRaisesRegex(ValueError, "unexpected paragraph IDs"):
            local_findings(
                payload,
                Translation.model_validate(
                    {"title": "Mở đầu", "segments": [{"id": 99, "text": "Added content"}]}
                ),
            )
        output.title = "Chương 2: Mở đầu"
        title_issues = local_findings({**payload, "raw_title": "第1章 开始"}, output)
        self.assertTrue(
            any(issue.kind == "number" and issue.segment_id == -1 for issue in title_issues)
        )

    async def test_success_causes_no_ai_review_or_repair(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertFalse(
                any(
                    instruction
                    in (
                        prompts.VALIDATE,
                        prompts.CONTEXT,
                        prompts.REPAIR,
                        prompts.ALIGN,
                        prompts.EVIDENCE,
                        prompts.DISCOVER,
                    )
                    for instruction, _ in provider.calls
                )
            )

    async def test_failure_repairs_only_failed_paragraph_and_preserves_others(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(root)
            provider = BookProvider(store)
            provider.corrupt_id = 1
            await self.run_book(store, provider)
            repairs = [
                data for instruction, data in provider.calls if instruction == prompts.REPAIR
            ]
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["affected_ids"], [1])
            self.assertEqual([p["id"] for p in repairs[0]["raw"]], [1])
            self.assertEqual(
                store.read("translated.json")["chapters"][0]["text"],
                "Tần Thư Mạn đi rồi.\n\nTần Thư Mạn gật đầu.\n\nTần Thư Mạn quay lại.",
            )
            self.assertEqual(store.request_statistics()["requests"]["repair"], 1)

    async def test_partial_resolver_failure_retries_only_invalid_source(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n【祖石】发光。\n【梦境术】发动。",
                vp="Chương 1\n【Tổ Thạch】 phát sáng.\n【Mộng Cảnh Thuật】 khởi động.",
            )
            provider = BookProvider(store)
            provider.malformed_source = "祖石"
            await self.run_book(store, provider)
            pages = [
                data for instruction, data in provider.calls if instruction == prompts.BATCH_RESOLVE
            ]
            self.assertEqual(len(pages), 2)
            self.assertEqual([item["source"] for item in pages[1]["candidates"]], ["祖石"])
            self.assertEqual(
                {t["source"] for t in store.read("dictionary.json")["entries"]}, {"祖石", "梦境术"}
            )

    async def test_resume_reuses_frozen_dictionary_and_completed_api_work(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n【祖石】发光。\n第2章\n祖石发光。",
                vp="Chương 1\n【Tổ Thạch】 phát sáng.\nChương 2\nTổ Thạch phát sáng.",
            )
            provider = BookProvider(store)
            provider.fail_chapter = 2
            with self.assertRaises(ProviderError):
                await Pipeline(store, Scheduler(transport=provider, concurrency=1, spacing=0)).run()
            first_count = sum(
                instruction == prompts.TRANSLATE and data["CHAPTER CONTEXT"]["number"] == 1
                for instruction, data in provider.calls
            )
            resolver_count = sum(
                instruction == prompts.BATCH_RESOLVE for instruction, _ in provider.calls
            )
            provider.fail_chapter = None
            await self.run_book(store, provider)
            self.assertEqual(
                sum(instruction == prompts.BATCH_RESOLVE for instruction, _ in provider.calls),
                resolver_count,
            )
            self.assertEqual(
                sum(
                    instruction == prompts.TRANSLATE and data["CHAPTER CONTEXT"]["number"] == 1
                    for instruction, data in provider.calls
                ),
                first_count,
            )
            count = len(provider.calls)
            stats = store.request_statistics()["total_requests"]
            await self.run_book(store, provider)
            self.assertEqual(len(provider.calls), count)
            self.assertEqual(store.request_statistics()["total_requests"], stats)

    async def test_new_term_after_freeze_stops_explicitly_without_dictionary_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root, raw="第1章\n隐秘宫开放。", vp="Chương 1\nẨn Bí Cung mở cửa."
            )
            provider = BookProvider(store)
            provider.report_unresolved = True
            with self.assertRaisesRegex(QualityError, "after dictionary freeze"):
                await self.run_book(store, provider)
            self.assertEqual(store.read("frozen-dictionary.json")["dictionary"]["entries"], [])
            self.assertIsNone(store.read("translated.json"))
            self.assertEqual(len(list((store.path / "frozen-conflicts").glob("*.json"))), 1)
