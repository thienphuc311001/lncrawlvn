"""Regression tests for stable identity, completeness, repair, and resume."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lncrawl.translation.dictionary import DictionaryConflict, load_dictionary, locked_matches, merge_patch, relevant_entries, scan
from lncrawl.translation.models import Entry, PIPELINE_VERSION, Translation, TranslatedSegment
from lncrawl.translation import prompts
from lncrawl.translation.notes import author_note_policy, is_author_note
from lncrawl.translation.parsing import ChapterValidationError, parse_chapters
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.scheduler import Scheduler, provider_json_schema
from lncrawl.translation.store import Store
from lncrawl.translation.validation import (
    local_findings, numeric_mismatch, repair_integrity_findings, structural_findings,
    source_quality_findings,
)


def entry(source, translation, kind="character", gender="unknown"):
    return {
        "source": source, "translation": translation, "type": kind,
        "gender": gender, "status": "locked", "aliases": [],
    }


def payload_from(body):
    return json.loads(body["contents"][0]["parts"][0]["text"])


class ParsingAndDictionarySafetyTests(unittest.TestCase):
    def test_ids_stable_and_heading_diagnostics_include_source(self):
        raw = "第141章 起始\n甲。\n\n乙。\n第143章 继续\n丙。"
        first = parse_chapters(raw)
        second = parse_chapters(raw)
        self.assertEqual(first, second)
        self.assertEqual(first[0].paragraph_ids, ("P0141_0001", "P0141_0002"))
        self.assertEqual(first[1].paragraph_ids, ("P0143_0001",))
        with self.assertRaises(ChapterValidationError) as caught:
            parse_chapters("第1章 开始\n甲。\n第十章 错误\n乙。")
        self.assertEqual(caught.exception.detail["error_type"], "malformed_heading")
        self.assertEqual(caught.exception.detail["input"], "RAW")
        self.assertEqual(caught.exception.detail["line"], 3)
        self.assertTrue(caught.exception.detail["reason"])

    def test_legacy_types_migrate_to_one_versioned_schema(self):
        old = {"version": 9, "entries": [
            entry("官署", "quan thự", "organization"),
            entry("神术", "thần thuật", "ability"),
            entry("法器", "pháp khí", "item"),
            entry("道心", "đạo tâm", "concept"),
        ]}
        migrations = []
        loaded = load_dictionary(old, migrations)
        self.assertEqual(loaded["version"], 10)
        self.assertEqual([item["type"] for item in loaded["entries"]],
                         ["institution", "term", "term", "term"])
        self.assertTrue(all(item["gender"] == "not_applicable" for item in loaded["entries"]))
        self.assertEqual(migrations[0], {"field": "version", "from": 9, "to": 10})
        with self.assertRaises(DictionaryConflict):
            load_dictionary({"version": 10, "entries": old["entries"]})

    def test_locked_conflict_is_rejected_and_full_alias_owner_is_selected(self):
        dictionary = load_dictionary({"version": 10, "entries": [
            {**entry("邱途", "Khâu Đồ"), "aliases": [
                {"source": "邱探员", "translation": "Thám viên Khâu"}]},
        ]})
        self.assertEqual(relevant_entries(dictionary, "邱探员来了。"), dictionary["entries"])
        with self.assertRaises(DictionaryConflict):
            merge_patch(dictionary, {"confirmed": [{"operation": "add_entry",
                "entry": entry("邱途", "Khâu Tô")}], "unresolved": []}, "邱途")

    def test_scanner_does_not_promote_action_fragments_or_generic_roles(self):
        raw = ("第141章 一亩三分地\n张居正把奏疏放下。\n"
               "王崇古笑了，低声问使者。\n他只是看了看门外。\n"
               "第142章 继续\n秦舒曼说：好个屁！")
        sources = {item["source"] for item in scan(parse_chapters(raw), load_dictionary(None))}
        self.assertEqual(sources, {"张居正", "王崇古", "秦舒曼"})
        with self.assertRaises(DictionaryConflict):
            merge_patch(load_dictionary(None), {"confirmed": [{"operation": "add_entry",
                "entry": entry("使者", "Sứ giả", "term", "not_applicable")}],
                "unresolved": []}, raw)

    def test_repeated_title_idiom_is_not_automatically_locked(self):
        chapters = parse_chapters("第141章 一亩三分地\n朝廷不能只守着自己的一亩三分地。")
        candidates = scan(chapters, load_dictionary(None))
        self.assertNotIn("一亩三分地", {item["source"] for item in candidates})

    def test_short_locked_term_does_not_cross_chinese_variable_boundary(self):
        dictionary = load_dictionary({"version": 10, "entries": [
            entry("里甲", "lý giáp", "term", "not_applicable")
        ]})
        self.assertEqual(locked_matches(dictionary, "公式里甲乙丙三商"), [])
        self.assertEqual([source for _, source, _ in locked_matches(dictionary, "里甲制度")],
                         ["里甲"])

    def test_structural_validator_names_exact_failure_modes(self):
        expected = ["P0142_0001", "P0142_0002"]
        make = lambda ids: [SimpleNamespace(id=value, text="Đã dịch.") for value in ids]
        self.assertEqual(structural_findings(expected, make(expected)), [])
        self.assertEqual(structural_findings(expected, make(expected[:1]))[0]["kind"], "missing_id")
        self.assertEqual(structural_findings(expected, make([expected[0]] * 2))[0]["kind"], "duplicate_id")
        self.assertEqual(structural_findings(expected, make(expected + ["P0142_0003"]))[0]["kind"],
                         "unexpected_id")
        self.assertEqual(structural_findings(expected, make(expected[::-1]))[0]["kind"],
                         "out_of_order_id")
        self.assertEqual(structural_findings(expected, [SimpleNamespace(id=expected[0], text=""),
                                                        SimpleNamespace(id=expected[1], text="x")])[0]["kind"],
                         "empty_segment")

    def test_truncation_and_numeric_helpers(self):
        raw = "第142章 大礼包\n王崇古这才知道这不是大礼包，而是朝廷给他的考验。"
        chapter = parse_chapters(raw)[0]
        fragment = Translation(title="Gói quà lớn", segments=[{
            "id": chapter.paragraph_ids[0],
            "text": "Vương Sùng Cổ lúc này mới biết đây không phải gói quà",
        }])
        findings = local_findings(chapter, fragment, load_dictionary(None))
        self.assertIn("obvious_truncation", {item["kind"] for item in findings})
        self.assertEqual(numeric_mismatch("热量2.5714", "Nhiệt lượng 2,5714"), None)
        self.assertEqual(numeric_mismatch("有两个人。", "Có 2 người."), None)
        self.assertEqual(numeric_mismatch("有2个人。", "Có 3 người.")["missing"], {"2": 1})
        self.assertTrue(numeric_mismatch("来了 一万二人。", "Có 12.000 người.")["ambiguous"])
        self.assertEqual(numeric_mismatch("国帑入库162.5万两。",
                                          "Quốc khố nhập 1,625 triệu lượng."), None)
        self.assertEqual(numeric_mismatch("国帑入库162.5万两。",
                                          "Quốc khố nhập 1.625.000 lượng."), None)
        self.assertEqual(numeric_mismatch("国帑入库162.5万两。",
                                          "Quốc khố nhập 1,625 tỷ lượng.")["missing"],
                         {"1625000": 1})
        self.assertEqual(numeric_mismatch("一匹利十二银五钱五分。",
                                          "Mỗi xấp lãi 12 lượng 5 tiền 5 phân."), None)
        self.assertEqual(numeric_mismatch("明穆宗实录五十三卷。",
                                          "Minh Mục Tông thực lục, quyển 53."), None)
        self.assertEqual(numeric_mismatch("先开2的2次方。",
                                          "Trước hết khai căn bậc hai của 2."), None)
        self.assertEqual(numeric_mismatch("√2从1.5到1.414。",
                                          "√2 từ 1.5 đến 1.414."), None)

    def test_locked_terms_ignore_all_casing_but_not_spelling_changes(self):
        raw = "第142章 造船\n松江造船厂给新船起了名字。"
        chapter = parse_chapters(raw)[0]
        dictionary = load_dictionary({"version": 10, "entries": [
            entry("松江造船厂", "Xưởng đóng tàu Tùng Giang", "institution", "not_applicable")
        ]})
        identifier = chapter.paragraph_ids[0]
        def findings(text):
            return local_findings(chapter, Translation(title="Đóng tàu", segments=[
                {"id": identifier, "text": text}]), dictionary)

        self.assertNotIn("locked_term_missing", {item["kind"] for item in findings(
            "Người ở xưởng đóng tàu Tùng Giang đặt tên cho con tàu mới.")})
        self.assertNotIn("locked_term_missing", {item["kind"] for item in findings(
            "Người ở Xưởng Đóng Tàu Tùng Giang đặt tên cho con tàu mới.")})
        self.assertNotIn("locked_term_missing", {item["kind"] for item in findings(
            "Người ở XƯỞNG ĐÓNG TÀU TÙNG GIANG đặt tên cho con tàu mới.")})
        self.assertIn("locked_term_missing", {item["kind"] for item in findings(
            "Người ở xưởng đóng tàu Tùng gian đặt tên cho con tàu mới.")})
        self.assertIn("locked_term_missing", {item["kind"] for item in findings(
            "Người ở xưởng đóng tàu Tung Giang đặt tên cho con tàu mới.")})

    def test_corpus_relative_short_paragraph_is_only_a_qa_suspicion(self):
        raw = "第1章 开始\n他望着窗外的雨，想起了多年前在同一座城市里经历的那些漫长而难忘的夜晚，以及那时许下的诺言。"
        chapter = parse_chapters(raw)[0]
        result = Translation(title="Khởi đầu", segments=[{
            "id": chapter.paragraph_ids[0], "text": "Ông nhớ chuyện cũ.",
        }])
        findings = local_findings(chapter, result, load_dictionary(None),
                                  paragraph_ratio_baseline=[1.5] * 20)
        self.assertIn("suspicious_length_ratio", {item["kind"] for item in findings})

    def test_repair_integrity_rejects_fragments_and_unterminated_quotes(self):
        raw = "王崇古来到大殿后，先向皇帝行礼，然后说明自己为何没有接受这份任命。"
        original = "Vương Sùng Cổ vào điện, hành lễ với hoàng đế rồi giải thích vì sao ông không nhận chức."
        dictionary = load_dictionary(None)
        cases = (
            "vì sao ông không nhận chức.",
            "Vương Sùng Cổ vào điện, hành lễ với hoàng đế rồi giải thích vì",
            "Vương Sùng Cổ vào điện, hành lễ với hoàng đế rồi nói: “Tôi không nhận chức.",
            "Vương Sùng Cổ vào điện, hành lễ với hoàng đế rồi",
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertTrue(repair_integrity_findings("P0001_0001", raw, original,
                                                          candidate, dictionary))

    def test_author_note_classifier_and_policy_validation(self):
        self.assertTrue(is_author_note("作者的话：求月票！"))
        self.assertTrue(is_author_note("平台提示：请支持正版。"))
        self.assertFalse(is_author_note("他向作者问了一句话。"))
        with patch.dict("os.environ", {"TRANSLATION_AUTHOR_NOTE_POLICY": "invalid"}):
            with self.assertRaises(ValueError):
                author_note_policy()

    def test_source_damage_is_recorded_without_changing_raw(self):
        raw = "第1章 开始\n他看见了被删掉的*字。\n“这句话没有收尾。"
        chapter = parse_chapters(raw)[0]
        findings = source_quality_findings([chapter])
        self.assertEqual({item["kind"] for item in findings},
                         {"asterisk_censorship", "unbalanced_source_punctuation"})
        self.assertEqual(chapter.paragraphs[0], "他看见了被删掉的*字。")

    def test_legacy_job_lookup_reuses_exact_raw_and_dictionary(self):
        raw = "第1章 开始\n甲。"
        old_inputs = {"raw": raw, "dictionary": None, "vietphrase": "obsolete"}
        new_inputs = {"raw": raw, "dictionary": None}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TRANSLATION_AUTHOR_NOTE_POLICY": "preserve"}
        ):
            root = Path(directory)
            legacy_id = "a" * 64
            old = Store(root, inputs=old_inputs, job_id=legacy_id)
            current = Store(root, inputs=new_inputs)
            self.assertEqual(current.id, legacy_id)
            self.assertEqual(old.read("inputs.json"), old_inputs)
            self.assertEqual(current.read("inputs.json"), old_inputs)

    def test_versioned_raw_only_job_is_not_reused_after_policy_change(self):
        raw = "第1章 开始\n甲。"
        inputs = {"raw": raw, "dictionary": None}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = Store(root, inputs=inputs, job_id="b" * 64)
            old.write("pipeline-version.json", {"version": PIPELINE_VERSION - 1})
            current = Store(root, inputs=inputs)
            self.assertNotEqual(current.id, old.id)
            self.assertEqual(current.id, Store(root, inputs=inputs).id)

    def test_cache_fingerprint_changes_with_relevant_inputs(self):
        raw = "第1章 开始\n甲。"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0,
                                                 transport=lambda *_: None, keys=[None]))
            payload = {"chapter": 1, "raw": [{"id": "P0001_0001", "text": "甲。"}],
                       "dictionary": [], "previous_translation": [], "next_source": ""}
            base = pipeline._fingerprint("translation", prompts.TRANSLATE, payload,
                                         Translation, "gemini-3.1-flash-lite")
            changed = {**payload, "dictionary": [entry("甲", "Giáp")]}
            self.assertNotEqual(base, pipeline._fingerprint(
                "translation", prompts.TRANSLATE, changed, Translation, "gemini-3.1-flash-lite"))
            changed_context = {**payload, "next_source": "乙。"}
            self.assertNotEqual(base, pipeline._fingerprint(
                "translation", prompts.TRANSLATE, changed_context, Translation,
                "gemini-3.1-flash-lite"))
            store.cache_put(base, {"title": "Khởi đầu", "segments": [
                {"id": "P0001_0001", "text": "Giáp."}]}, {"actual_model": "gemini-3.1-flash-lite"})
            self.assertIsNotNone(store.cache_get(base))
            self.assertIsNone(store.cache_get(pipeline._fingerprint(
                "translation", prompts.TRANSLATE, changed, Translation,
                "gemini-3.1-flash-lite")))

    def test_provider_schema_uses_supported_subset_while_local_model_stays_strict(self):
        entry_schema = provider_json_schema(Entry)
        segment_schema = provider_json_schema(TranslatedSegment)
        encoded = json.dumps({"entry": entry_schema, "segment": segment_schema})
        self.assertNotIn('"const"', encoded)
        self.assertNotIn('"pattern"', encoded)
        self.assertNotIn('"minLength"', encoded)
        self.assertEqual(entry_schema["properties"]["status"]["enum"], ["locked"])
        with self.assertRaises(ValueError):
            TranslatedSegment.model_validate({"id": "wrong", "text": "x"})


class PipelineSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_raw_is_logged_before_failure(self):
        raw = "第1章 开始\n甲。\n第1章 重复\n乙。"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            with self.assertRaises(ChapterValidationError):
                await Pipeline(store, Scheduler(concurrency=1, spacing=0,
                                                transport=lambda *_: None, keys=[None])).run()
            self.assertIn("RAW_VALIDATION_FAILED", (store.path / "events.jsonl").read_text())

    async def test_unresolved_keeps_raw_evidence_and_skips_unchanged_post_scan(self):
        raw = "第1章 初见\n邱途说：“我来了。”"
        calls = []

        async def transport(model, body):
            payload = payload_from(body)
            calls.append(payload)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": [{
                    "source": "邱途", "possible_type": "character",
                    "reason": "identity unclear", "evidence": [],
                }]}
            return {"title": "Lần đầu gặp", "segments": [
                {"id": item["id"], "text": "Khâu Đồ nói: “Tôi đến rồi.”"}
                for item in payload["raw"]]}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport,
                                            keys=[None])).run()
            self.assertEqual(sum("candidates" in payload for payload in calls), 1)
            self.assertTrue(store.read("unresolved.json")[0]["evidence"])
            self.assertIsNotNone(store.read("post-dictionary.json"))

    async def test_request_budget_warning_and_hard_stop_are_visible(self):
        raw = "第1章 开始\n甲。\n第2章 继续\n乙。"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TRANSLATION_WARNING_LOGICAL_CALLS": "2",
                           "TRANSLATION_HARD_LOGICAL_CALLS": "3"}
        ):
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0,
                                                 transport=lambda *_: None, keys=[None]))
            pipeline.chapters = parse_chapters(raw)
            store.account("translation", "logical")
            store.account("translation", "logical")
            pipeline._guard_requests()
            self.assertEqual(store.read("request-warning.json")["severity"], "warning")
            store.account("repair", "logical")
            with self.assertRaises(QualityError):
                pipeline._guard_requests()
            self.assertEqual(store.read("request-warning.json")["severity"], "fatal")

    async def test_targeted_qa_receives_only_suspect_and_neighbor_context(self):
        raw = "第1章 开始\n第一段。\n第二段写了更多事情。\n第三段。"
        chapter = parse_chapters(raw)[0]
        captured = []

        async def transport(model, body):
            captured.append((model, payload_from(body)))
            return {"findings": []}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0,
                                                 transport=transport, keys=[None]))
            pipeline.chapters = [chapter]
            pipeline.dictionary = load_dictionary(None)
            result = Translation(title="Khởi đầu", segments=[
                {"id": identifier, "text": text} for identifier, text in zip(
                    chapter.paragraph_ids, ("Đoạn một.", "Đoạn hai nhiều việc hơn.", "Đoạn ba."))])
            confirmed, audited = await pipeline._audit_suspicions(chapter, result, [
                {"id": chapter.paragraph_ids[1], "kind": "suspicious_length_ratio"}])
            self.assertEqual(confirmed, [])
            self.assertEqual(audited, [chapter.paragraph_ids[1]])
            self.assertEqual(captured[0][0], "gemini-3.5-flash-lite")
            self.assertEqual([item["id"] for item in captured[0][1]["raw"]],
                             [chapter.paragraph_ids[1]])
            self.assertEqual(captured[0][1]["context"][0]["previous_raw"], "第一段。")
            self.assertEqual(captured[0][1]["context"][0]["next_raw"], "第三段。")

    async def test_separate_author_notes_keep_ids_and_reader_order(self):
        raw = "第1章 开始\n他点点头。\n作者的话：求月票！"
        translations = {"他点点头。": "Ông gật đầu.",
                        "作者的话：求月票！": "Lời tác giả: Xin phiếu tháng!"}

        async def transport(model, body):
            payload = payload_from(body)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            return {"title": "Khởi đầu", "segments": [
                {"id": item["id"], "text": translations[item["text"]]}
                for item in payload["raw"]]}

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TRANSLATION_AUTHOR_NOTE_POLICY": "separate"}
        ):
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport,
                                            keys=[None])).run()
            artifact = store.read("translated.json")["chapters"][0]
            self.assertEqual(artifact["author_note_ids"], ["P0001_0002"])
            self.assertEqual(len(artifact["segments"]), 2)
            self.assertEqual(artifact["paragraphs"], ["Ông gật đầu."])
            self.assertNotIn("phiếu tháng", store.read("translated.txt"))
            self.assertIn("phiếu tháng", store.read("author-notes.txt"))

    async def test_fragment_repair_is_rejected_preserving_original_and_resume_budget(self):
        raw = ("第1章 再会\n"
               "秦舒曼带着2个人走进房间，随后笑着说：“我们都到了。”")
        dictionary = [entry("秦舒曼", "Tần Thư Mạn")]
        original = "Tần Thư Man dẫn 2 người vào phòng, rồi mỉm cười nói: “Chúng ta đều đã đến.”"
        calls = []

        async def transport(model, body):
            payload = payload_from(body)
            calls.append((model, payload))
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            if "current_vietnamese" in payload:
                return {"id": payload["id"], "text": "Tần Thư Mạn"}
            return {"title": "Gặp lại", "segments": [
                {"id": item["id"], "text": original} for item in payload["raw"]]}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": dictionary})
            scheduler = Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])
            with self.assertRaises(QualityError):
                await Pipeline(store, scheduler).run()
            self.assertIsNone(store.read("chapters/1.json"))
            self.assertEqual(store.read("pending-chapters/1.json")["translation"]["segments"][0]["text"],
                             original)
            self.assertEqual(store.read("repair-attempts/1/P0001_0001.json")["status"], "rejected")
            self.assertEqual(store.request_statistics()["by_operation"]["repair"]["logical_calls"], 1)
            before = len(calls)
            with self.assertRaises(QualityError):
                await Pipeline(store, scheduler).run()
            self.assertEqual(len(calls), before)
            self.assertEqual(store.read("pending-chapters/1.json")["translation"]["segments"][0]["text"],
                             original)

    async def test_complete_repair_changes_only_target_and_keeps_revision(self):
        raw = "第1章 再会\n秦舒曼笑了。\n第二个人也笑了。"
        dictionary = [entry("秦舒曼", "Tần Thư Mạn")]

        async def transport(model, body):
            payload = payload_from(body)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            if "current_vietnamese" in payload:
                return {"id": payload["id"], "text": "Tần Thư Mạn mỉm cười."}
            return {"title": "Gặp lại", "segments": [
                {"id": item["id"], "text": "Tần Thư Man mỉm cười." if index == 0
                 else "Người thứ hai cũng cười."}
                for index, item in enumerate(payload["raw"])]}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": dictionary})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport,
                                            keys=[None])).run()
            segments = store.read("chapters/1.json")["translation"]["segments"]
            self.assertEqual([item["id"] for item in segments], ["P0001_0001", "P0001_0002"])
            self.assertEqual(segments[0]["text"], "Tần Thư Mạn mỉm cười.")
            self.assertEqual(segments[1]["text"], "Người thứ hai cũng cười.")
            revisions = store.read("revisions/1/P0001_0001.json")
            self.assertEqual([item["revision"] for item in revisions], [0, 1])
            self.assertIsNone(store.read("revisions/1/P0001_0002.json"))

    async def test_two_chapter_historical_fixture_uses_two_translation_calls(self):
        raw = ("第141章 一亩三分地\n"
               "张居正说，朝廷不能只守着自己的一亩三分地。\n"
               "费利佩二世派来的使者问：“真有这份大礼包？”\n"
               "第142章 好个屁\n"
               "王崇古听完冷笑一声：好个屁！\n"
               "他没有答应，也没有拒绝。")
        dictionary = [entry("张居正", "Trương Cư Chính"),
                      entry("费利佩二世", "Felipe II"),
                      entry("王崇古", "Vương Sùng Cổ")]
        expected = {
            "张居正说，朝廷不能只守着自己的一亩三分地。":
                "Trương Cư Chính nói triều đình không thể chỉ bo bo giữ lấy phần lợi ích nhỏ của mình.",
            "费利佩二世派来的使者问：“真有这份大礼包？”":
                "Sứ giả do Felipe II phái tới hỏi: “Thật có món quà lớn ấy sao?”",
            "王崇古听完冷笑一声：好个屁！":
                "Nghe xong, Vương Sùng Cổ cười khẩy: Hay ho cái quái gì!",
            "他没有答应，也没有拒绝。":
                "Ông không nhận lời, cũng không từ chối.",
        }
        calls = []

        async def transport(model, body):
            payload = payload_from(body)
            calls.append(payload)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            return {"title": "Phần lợi ích nhỏ" if payload["chapter"] == 141 else "Hay ho cái quái gì",
                    "segments": [{"id": item["id"], "text": expected[item["text"]]}
                                 for item in payload["raw"]]}

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": dictionary})
            await Pipeline(store, Scheduler(concurrency=2, spacing=0, transport=transport,
                                            keys=[None])).run()
            chapters = store.read("translated.json")["chapters"]
            self.assertEqual([item["number"] for item in chapters], [141, 142])
            self.assertEqual([item["id"] for item in chapters[0]["segments"]],
                             ["P0141_0001", "P0141_0002"])
            self.assertEqual(sum("chapter" in item and "raw" in item for item in calls), 2)
            self.assertEqual(sum("vietnamese" in item for item in calls), 0)
            self.assertEqual(sum("current_vietnamese" in item for item in calls), 0)
            self.assertIn("Felipe II", store.read("translated.txt"))
            self.assertNotIn("张居正", store.read("translated.txt"))
            self.assertEqual(store.read("dictionary.json")["version"], 10)
            self.assertEqual(store.request_statistics()["by_operation"]["translation"]["logical_calls"], 2)

    async def test_audited_checkpoint_is_reused_without_retranslation(self):
        raw = "第1章 开始\n远处的钟声响起，所有人都停下脚步等待消息。"
        calls = []

        async def transport(model, body):
            payload = payload_from(body)
            calls.append(payload)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            if "vietnamese" in payload:
                return {"findings": []}
            return {"title": "Khởi đầu", "segments": [
                {"id": item["id"],
                 "text": "Tiếng chuông vang lên nơi xa, mọi người đều dừng bước chờ tin."}
                for item in payload["raw"]]}

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TRANSLATION_MIN_LENGTH_RATIO": "10"}
        ):
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            scheduler = Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])
            await Pipeline(store, scheduler).run()
            accepted = store.read("chapters/1.json")
            self.assertEqual(accepted["audited_ids"], ["P0001_0001"])
            before = len(calls)
            await Pipeline(store, scheduler).run()
            self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()
