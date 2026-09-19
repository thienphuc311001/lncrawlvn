"""Behavioral proofs for the local-first, globally frozen translation pipeline."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation import prompts
from lncrawl.translation.models import Inputs, Issue, Translation
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.preprocessing import build_index, local_resolution
from lncrawl.translation.scheduler import ProviderError, Scheduler
from lncrawl.translation.store import Store, digest, pipeline_identity
from lncrawl.translation.validation import local_findings, validate_findings


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

    def test_quantified_measure_word_fragment_is_rejected(self):
        pairs = validate_inputs(
            "第1章\n那块极光石很亮。\n1块极光石很贵。",
            "Chương 1\nKhối Cực Quang Thạch rất sáng.\n1 khối Cực Quang Thạch rất quý.",
        )
        index = build_index(pairs, {r.key: deterministic_alignment(r, v) for r, v in pairs}, [])
        self.assertNotIn("块极光石", index["candidates"])
        self.assertEqual(index["rejected"]["块极光石"], "quantity modifier is not a canonical entity name")

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
                [("秦舒曼", "Tần Thư Mạn", "locked")],
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
            with self.assertRaisesRegex(QualityError, "broken canonical reference"):
                await self.run_book(store, provider)
            self.assertEqual(len(store.read("legacy-audit.json")), 8)
            self.assertEqual(store.read("legacy-audit.json")[-1]["classification"], "fatal")
            self.assertEqual(provider.calls, [])

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

    async def test_two_locked_mappings_for_one_source_are_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                dictionary={"entries": [
                    {"source": "祖石", "translation": "Tổ Thạch", "type": "artifact", "status": "locked"},
                    {"source": "祖石", "translation": "Tổ Đá", "type": "artifact", "status": "locked"},
                ]},
            )
            provider = BookProvider(store)
            with self.assertRaisesRegex(QualityError, "Fatal inherited dictionary"):
                await self.run_book(store, provider)
            self.assertEqual(provider.calls, [])

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
            self.assertEqual(stats["requests"]["semantic_dictionary_conflict"], 0)
            self.assertEqual(stats["requests"]["terminology_resolver"], 1)
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
                [{
                    **dictionary["entries"][0],
                    "gender": "unknown",
                    "aliases": [],
                    "evidence": "",
                    # The confirmed address form keeps its class and the batch
                    # register that was enforced for it.
                    "form_kinds": {"秦科长": "official_title"},
                    "address_register": "sino-vietnamese",
                    "runtime_state": "CONFIRMED",
                }],
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

    def test_local_validator_uses_typed_findings_with_source_evidence(self):
        payload = {
            "location": {"chapter": 175, "chunk": 2},
            "raw": [
                {
                    "id": 0,
                    "text": "第一段。",
                    "source_segment_id": "c175-k2-s0",
                    "source_line": 18,
                },
                {
                    "id": 1,
                    "text": "第二段。",
                    "source_segment_id": "c175-k2-s1",
                    "source_line": 20,
                },
            ],
            "vp": [],
            "terminology": [],
        }
        issues = local_findings(
            payload,
            Translation.model_validate(
                {"title": "Chương 175", "segments": [{"id": 0, "text": "Đoạn một."}]}
            ),
        )
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.type, "content_missing")
        self.assertEqual(issue.validator, "content_coverage")
        self.assertEqual(issue.source_segment_id, "c175-k2-s1")
        self.assertEqual(issue.source_excerpt, "第二段。")
        self.assertEqual(issue.source_line, 20)
        report = Pipeline.validation_failure_text("chapter:175:chunk:2", issues, 1)
        self.assertIn("Type: content_missing", report)
        self.assertIn("RAW excerpt: 第二段。", report)
        self.assertNotIn("n/a", report)

    def test_merged_output_is_not_reported_as_missing_when_complete_vp_is_visible(self):
        payload = {
            "raw": [
                {"id": 0, "text": "第一段。"},
                {"id": 1, "text": "第二段。"},
            ],
            "vp": [
                {"id": 0, "text": "Alpha"},
                {"id": 1, "text": "Beta Gamma"},
            ],
            "terminology": [],
        }
        merged = local_findings(
            payload,
            Translation.model_validate(
                {"title": "Chương 1", "segments": [{"id": 0, "text": "Alpha Beta Gamma"}]}
            ),
        )
        self.assertEqual(merged, [])

    def test_fatal_findings_cannot_be_anonymous(self):
        with self.assertRaisesRegex(ValueError, "no actionable evidence"):
            validate_findings(
                [Issue(segment_id=4, kind="missing", explanation="missing")]
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
            self.assertEqual(len(pages), 1)
            self.assertEqual(
                {item["source"] for item in pages[0]["candidates"]}, {"祖石", "梦境术"}
            )
            self.assertEqual(
                {t["source"] for t in store.read("dictionary.json")["entries"]}, {"梦境术"}
            )
            self.assertIn("祖石", {item["source"] for item in store.read("dictionary-audit.json")["ignored"]})

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

    async def test_uncertain_item_translation_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n极光石发光。\n极光石很贵。\n极光石被收起。",
                vp="Chương 1\nCực Quang Thạch phát sáng.\nCực Quang Thạch rất quý.\nCực Quang Thạch được cất đi.",
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            self.assertNotIn("极光石", {t["source"] for t in store.read("dictionary.json")["entries"]})
            self.assertGreater(store.read("dictionary-audit.json")["ignored_candidates"], 0)
            self.assertEqual(
                [call[0] for call in provider.calls],
                [prompts.BATCH_RESOLVE, prompts.TRANSLATE],
            )
            self.assertEqual(store.read("dictionary-resolution-report.json")["summary"]["fatal_conflicts"], 0)

    async def test_stable_person_name_is_resolved_locally_without_alias_inference(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n秦政光来了。\n秦政光点头。\n秦政光离开。",
                vp="Chương 1\nTần Chính Quang đến.\nTần Chính Quang gật đầu.\nTần Chính Quang rời đi.",
            )
            provider = BookProvider(store)
            await self.run_book(store, provider)
            entry = next(t for t in store.read("dictionary.json")["entries"] if t["source"] == "秦政光")
            self.assertEqual((entry["translation"], entry["type"], entry["aliases"]), ("Tần Chính Quang", "character", []))
            self.assertEqual(store.request_statistics()["requests"]["terminology_resolver"], 0)

    async def test_uncertain_alias_is_ignored_without_identity_inference(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n秦舒曼来了。\n老秦点头。\n老秦离开。\n老秦回来了。",
                vp="Chương 1\nTần Thư Mạn đến.\nLão Tần gật đầu.\nLão Tần rời đi.\nLão Tần quay lại.",
            )

            async def provider(model, body):
                data = json.loads(body["contents"][0]["parts"][0]["text"])
                if body["systemInstruction"]["parts"][0]["text"] == prompts.BATCH_RESOLVE:
                    results = []
                    for item in data["candidates"]:
                        source = item["source"]
                        results.append({
                            "source": source,
                            "decision": "REVIEW",
                            "eligibility": {
                                "complete_semantic_unit": True,
                                "named_or_novel_specific": True,
                                "consistency_matters": True,
                                "evidence_supports": False,
                            },
                            "term": {
                                "source": "秦舒曼",
                                "translation": "Tần Thư Mạn",
                                "type": "character",
                                "forms": {source: "Lão Tần"},
                            },
                            "reason": "Alias identity is uncertain",
                        })
                    return {"results": results}
                if body["systemInstruction"]["parts"][0]["text"] == prompts.TRANSLATE:
                    data = json.loads(body["contents"][0]["parts"][0]["text"])
                    return {
                        "title": data["CHAPTER CONTEXT"]["vp_title"],
                        "segments": [{"id": item["id"], "text": data["VIETPHRASE REFERENCE"][item["id"]]["text"]}
                                     for item in data["RAW TO TRANSLATE"]],
                    }
                raise AssertionError("Unexpected provider operation")

            await self.run_book(store, provider)
            entries = {entry["source"]: entry for entry in store.read("dictionary.json")["entries"]}
            self.assertNotIn("老秦", entries)
            self.assertNotIn("老秦", entries.get("秦舒曼", {}).get("aliases", []))
            self.assertGreater(store.read("dictionary-audit.json")["ignored_candidates"], 0)

    async def test_invalid_metadata_falls_back_without_repeating_corrections(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n【祖石】发光。",
                vp="Chương 1\n【Tổ Thạch】 phát sáng.",
            )
            calls = []

            async def provider(model, body):
                instruction = body["systemInstruction"]["parts"][0]["text"]
                calls.append(instruction)
                data = json.loads(body["contents"][0]["parts"][0]["text"])
                if instruction == prompts.BATCH_RESOLVE:
                    source = data["candidates"][0]["source"]
                    return {"results": [{
                        "source": source,
                        "decision": "ACCEPT",
                        "eligibility": {
                            "complete_semantic_unit": True,
                            "named_or_novel_specific": True,
                            "consistency_matters": True,
                            "evidence_supports": True,
                        },
                        "term": {
                            "source": source,
                            "translation": "Tổ Thạch",
                            "type": "character_alias",
                        },
                        "reason": "Metadata is not canonical",
                    }]}
                if instruction == prompts.TRANSLATE:
                    return {"title": data["CHAPTER CONTEXT"]["vp_title"], "segments": [
                        {"id": item["id"], "text": data["VIETPHRASE REFERENCE"][item["id"]]["text"]}
                        for item in data["RAW TO TRANSLATE"]
                    ]}
                raise AssertionError("Metadata-only fallback should not issue a repair request")

            await self.run_book(store, provider)
            self.assertEqual(calls.count(prompts.BATCH_RESOLVE), 1)
            self.assertEqual(store.read("dictionary.json")["entries"], [])
            self.assertIn("祖石", {item["source"] for item in store.read("dictionary-audit.json")["ignored"]})

    async def test_malformed_resolver_translation_is_ignored_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root,
                raw="第1章\n【祖石】发光。",
                vp="Chương 1\n【Tổ Thạch】 phát sáng.",
            )
            calls = []

            async def provider(model, body):
                instruction = body["systemInstruction"]["parts"][0]["text"]
                calls.append(instruction)
                data = json.loads(body["contents"][0]["parts"][0]["text"])
                if instruction == prompts.BATCH_RESOLVE:
                    source = data["candidates"][0]["source"]
                    return {"results": [{
                        "source": source,
                        "decision": "ACCEPT",
                        "eligibility": {
                            "complete_semantic_unit": True,
                            "named_or_novel_specific": True,
                            "consistency_matters": True,
                            "evidence_supports": True,
                        },
                        "term": {"source": source, "translation": "中文", "type": "artifact"},
                        "reason": "Bad translation",
                    }]}
                if instruction == prompts.TRANSLATE:
                    return {"title": data["CHAPTER CONTEXT"]["vp_title"], "segments": [
                        {"id": item["id"], "text": data["VIETPHRASE REFERENCE"][item["id"]]["text"]}
                        for item in data["RAW TO TRANSLATE"]
                    ]}
                raise AssertionError("Unexpected provider operation")

            await self.run_book(store, provider)
            self.assertEqual(calls.count(prompts.BATCH_RESOLVE), 1)
            self.assertEqual(store.read("dictionary.json")["entries"], [])

    async def test_legacy_dictionary_stage_resume_reuses_exhausted_wording(self):
        with tempfile.TemporaryDirectory() as root:
            inputs = Inputs(
                raw="第1章\n【祖石】发光。",
                vietphrase="Chương 1\n【Tổ Thạch】 phát sáng.",
            ).model_dump()
            current = Store(Path(root), inputs)
            legacy_id = "a" * 64
            current.path.rename(Path(root) / legacy_id)
            store = Store(Path(root), job_id=legacy_id)
            store.write("resolved-terms.json", {
                "input_hash": legacy_id,
                "terms": [],
                "decisions": {},
            })
            response = {
                "source": "祖石",
                "decision": "ACCEPT",
                "eligibility": {
                    "complete_semantic_unit": True,
                    "named_or_novel_specific": True,
                    "consistency_matters": True,
                    "evidence_supports": True,
                },
                "term": {"source": "祖石", "translation": "Tổ Thạch", "type": "artifact"},
                "reason": "Stable inherited resolver wording",
            }
            for attempt in range(3):
                store.write(
                    f"resolution-rejections/{digest('祖石')}-{attempt}.json",
                    {"error": "legacy metadata rejection", "response": response},
                )
            calls = []

            async def provider(model, body):
                instruction = body["systemInstruction"]["parts"][0]["text"]
                calls.append(instruction)
                if instruction == prompts.BATCH_RESOLVE:
                    raise AssertionError("Legacy exhausted resolver result was not migrated")
                data = json.loads(body["contents"][0]["parts"][0]["text"])
                return {
                    "title": data["CHAPTER CONTEXT"]["vp_title"],
                    "segments": [{"id": item["id"], "text": data["VIETPHRASE REFERENCE"][item["id"]]["text"]}
                                 for item in data["RAW TO TRANSLATE"]],
                }

            await Pipeline(store, Scheduler(transport=provider, spacing=0)).run()
            self.assertNotIn(prompts.BATCH_RESOLVE, calls)
            self.assertEqual(store.read("dictionary.json")["entries"][0]["translation"], "Tổ Thạch")
            self.assertEqual(store.read("resolved-terms.json")["input_hash"], pipeline_identity(inputs))

    async def test_unresolved_term_after_freeze_is_audit_only(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.make_store(
                root, raw="第1章\n隐秘宫开放。", vp="Chương 1\nẨn Bí Cung mở cửa."
            )
            provider = BookProvider(store)
            provider.report_unresolved = True
            await self.run_book(store, provider)
            self.assertEqual(store.read("frozen-dictionary.json")["dictionary"]["entries"], [])
            self.assertIsNotNone(store.read("translated.json"))
            self.assertIsNotNone(next((store.path / "ignored-terms").glob("*.json"), None))
