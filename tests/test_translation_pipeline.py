"""Offline contract tests for the RAW-only translation pipeline."""
from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation.dictionary import DictionaryConflict, load_dictionary, load_unresolved, locked_matches, merge_patch, scan
from lncrawl.translation.models import DictionaryPatch
from lncrawl.translation.parsing import ChapterValidationError, parse_chapters
from lncrawl.translation.pipeline import Pipeline
from lncrawl.translation.scheduler import ErrorCategory, ProviderError, Scheduler
from lncrawl.translation.store import Store

RAW = "第1章 初见\n邱途说：我们有2个人。\n秦舒曼笑了。\n第2章 继续\n邱途走了。"

def entry(source, translation, gender="unknown"):
    return {
        "source": source, "translation": translation, "type": "character",
        "gender": gender, "status": "locked", "aliases": [],
    }

class FakeGemini:
    def __init__(self, fail_chapter=None):
        self.calls = []
        self.fail_chapter = fail_chapter

    async def __call__(self, model, body):
        import json
        payload = json.loads(body["contents"][0]["parts"][0]["text"])
        self.calls.append((model, payload))
        if "candidates" in payload:
            names = {"邱途": "Khâu Đồ", "秦舒曼": "Tần Thư Mạn"}
            return {"confirmed": [
                {"operation": "add_entry", "entry": entry(item["source"], names[item["source"]])}
                for item in payload["candidates"] if item["source"] in names
            ], "unresolved": []}
        if "current_vietnamese" in payload:
            return {"id": payload["id"],
                    "text": payload["current_vietnamese"].replace("Tần Thư Man", "Tần Thư Mạn")}
        if "vietnamese" in payload:
            return {"findings": []}
        chapter = payload["chapter"]
        if self.fail_chapter == chapter:
            raise ProviderError("temporary test failure", category=ErrorCategory.MODEL_FAILURE)
        lookup = {
            "邱途说：我们有2个人。": "Khâu Đồ nói: Chúng ta có 2 người.",
            "秦舒曼笑了。": "Tần Thư Mạn mỉm cười.",
            "邱途走了。": "Khâu Đồ rời đi.",
        }
        return {
            "title": "Lần đầu gặp" if chapter == 1 else "Tiếp tục",
            "segments": [{"id": item["id"], "text": lookup[item["text"]]} for item in payload["raw"]],
        }

class ParserTests(unittest.TestCase):
    def test_export_separators_do_not_become_chapter_content(self):
        rule = "-" * 60
        chapters = parse_chapters(f"\n{rule}\n第141章 开始\n甲。\n{rule}\n第142章 继续\n----\n乙。")
        self.assertEqual([chapter.number for chapter in chapters], [141, 142])
        self.assertEqual(chapters[0].paragraphs, ("甲。",))
        self.assertEqual(chapters[1].paragraphs, ("----", "乙。"))
        self.assertEqual([chapter.source_line for chapter in chapters], [3, 6])

    def test_gap_and_body_reference(self):
        chapters = parse_chapters("第21章 开始\n第21章中提到旧事。\n第102章 终章\n结束。")
        self.assertEqual([chapter.number for chapter in chapters], [21, 102])
        self.assertEqual(chapters[0].paragraphs, ("第21章中提到旧事。",))

    def test_duplicate_and_backwards_diagnostics(self):
        for raw, kind in [
            ("第1章\n甲。\n第1章\n乙。", "duplicate_chapter"),
            ("第2章\n甲。\n第1章\n乙。", "backwards_chapter"),
        ]:
            with self.subTest(kind=kind), self.assertRaises(ChapterValidationError) as caught:
                parse_chapters(raw)
            self.assertEqual(caught.exception.detail["error_type"], kind)
            self.assertEqual(caught.exception.detail["line"], 3)
            self.assertEqual(caught.exception.detail["previous_line"], 1)

class DictionaryTests(unittest.TestCase):
    def test_patch_is_atomic_and_preserves_locked_identity(self):
        dictionary = load_dictionary({"version": 9, "entries": [entry("邱途", "Khâu Đồ")]})
        patch_data = {"confirmed": [
            {"operation": "add_alias", "canonical_source": "邱途",
             "alias": {"source": "邱探员", "translation": "Thám viên Khâu"}},
        ], "unresolved": []}
        merged = merge_patch(dictionary, patch_data, "邱探员见到邱途。")
        self.assertEqual(merged["entries"][0]["aliases"][0]["source"], "邱探员")
        self.assertEqual(dictionary["entries"][0]["aliases"], [])
        with self.assertRaises(DictionaryConflict):
            merge_patch(merged, {"confirmed": [
                {"operation": "add_alias", "canonical_source": "邱途",
                 "alias": {"source": "邱探员", "translation": "Sai"}},
            ]}, "邱探员")
        with self.assertRaises(DictionaryConflict):
            merge_patch(dictionary, {"confirmed": [
                {"operation": "add_entry", "entry": entry("邱途笑", "Sai")},
            ]}, "邱途笑")
        with self.assertRaises(DictionaryConflict):
            merge_patch(dictionary, {"confirmed": [
                {"operation": "add_entry", "entry": entry("邱探员", "Sai")},
            ]}, "邱探员")

    def test_same_vietnamese_wording_is_allowed(self):
        merged = merge_patch(load_dictionary(None), {"confirmed": [
            {"operation": "add_entry", "entry": entry("邱途", "Tên Chung")},
            {"operation": "add_entry", "entry": entry("秦舒曼", "Tên Chung")},
        ]}, "邱途和秦舒曼")
        self.assertEqual(len(merged["entries"]), 2)

    def test_longest_alias_wins_without_requiring_embedded_name(self):
        dictionary = load_dictionary({"version": 9, "entries": [{
            **entry("邱途", "Khâu Đồ"),
            "aliases": [{"source": "邱途哥", "translation": "Đồ ca"}],
        }]})
        matches = locked_matches(dictionary, "邱途哥来了")
        self.assertEqual([(source, target) for _, source, target in matches], [("邱途哥", "Đồ ca")])

    def test_local_scan_prefers_name_not_action_fragment(self):
        candidates = scan(parse_chapters(RAW), load_dictionary(None))
        sources = {item["source"] for item in candidates}
        self.assertIn("邱途", sources)
        self.assertIn("秦舒曼", sources)
        self.assertNotIn("邱途说", sources)
        self.assertNotIn("邱途走", sources)

    def test_unresolved_metadata_carries_separately_from_locked_entries(self):
        data = {"version": 9, "entries": [entry("邱途", "Khâu Đồ")],
                "unresolved": [{"source": "邱探员", "possible_type": "character",
                                "reason": "owner unclear", "evidence": ["邱探员说。"],
                                "first_seen_chapter": 1, "last_seen_chapter": 2}]}
        self.assertEqual(len(load_dictionary(data)["entries"]), 1)
        self.assertEqual(load_unresolved(data)[0]["source"], "邱探员")

    def test_book_dictionary_flat_array_and_types_are_preserved(self):
        from lncrawl.translation.models import Inputs
        records = [
            {**entry("王守仁", "Vương Thủ Nhân"), "aliases": [
                {"source": "王阳明", "translation": "Vương Dương Minh"}]},
            *({**entry(source, target), "type": kind} for source, target, kind in (
                ("钦天监", "Khâm Thiên Giám", "institution"),
                ("四书直解", "Tứ Thư Trực Giải", "book_title"),
                ("陈五事疏", "Trần Ngũ Sự Sớ", "memorial"),
                ("孟子·离娄上", "Mạnh Tử · Ly Lâu thượng", "book_section"),
                ("税票", "thuế phiếu", "term"),
                ("南澳岛", "đảo Nam Áo", "location"),
            )),
        ]
        inputs = Inputs(raw=RAW, dictionary=records)
        canonical = load_dictionary(inputs.dictionary)["entries"]
        self.assertEqual([item["source"] for item in canonical], [item["source"] for item in records])
        self.assertTrue(all(item["gender"] == "not_applicable" for item in canonical[1:]))

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_batch_and_clean_zero_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": RAW, "dictionary": None})
            fake = FakeGemini()
            scheduler = Scheduler(concurrency=2, spacing=0, transport=fake, keys=[None])
            await Pipeline(store, scheduler).run()
            self.assertEqual(store.read("progress.json")["status"], "done")
            self.assertEqual(len(store.read("translated.json")["chapters"]), 2)
            self.assertEqual(len(store.read("dictionary.json")["entries"]), 2)
            self.assertIn("Khâu Đồ", store.read("translated.txt"))
            self.assertEqual(store.read("unresolved.json"), [])
            calls = [call for _, call in fake.calls]
            self.assertEqual(sum("candidates" in call for call in calls), 1)
            self.assertEqual(sum("chapter" in call and "raw" in call for call in calls), 2)
            self.assertEqual(sum("vietnamese" in call for call in calls), 0)

    async def test_resume_reuses_completed_chapter_and_pre_dictionary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": RAW, "dictionary": None})
            fake = FakeGemini(fail_chapter=2)
            scheduler = Scheduler(concurrency=1, spacing=0, transport=fake, keys=[None])
            with self.assertRaises(ProviderError):
                await Pipeline(store, scheduler).run()
            self.assertIsNotNone(store.read("chapters/1.json"))
            calls_before = len(fake.calls)
            fake.fail_chapter = None
            await Pipeline(store, scheduler).run()
            new_calls = [call for _, call in fake.calls[calls_before:]]
            self.assertEqual(sum("candidates" in call for call in new_calls), 0)
            self.assertEqual(sum(call.get("chapter") == 1 for call in new_calls), 0)
            self.assertEqual(store.read("progress.json")["status"], "done")

    async def test_model_fallback_keeps_same_payload(self):
        calls = []
        async def transport(model, body):
            calls.append((model, body))
            if model == "gemini-3.1-flash-lite":
                raise ProviderError("model unavailable", category=ErrorCategory.MODEL_FAILURE)
            return {"id": "P0001_0001", "text": "x"}
        scheduler = Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])
        from lncrawl.translation.models import Repair
        result = await scheduler.request("same policy", {"raw": "原文"}, Repair, lambda _: None)
        self.assertEqual(result._request_meta["actual_model"], "gemini-3.5-flash-lite")
        self.assertEqual(calls[0][1], calls[1][1])

if __name__ == "__main__":
    unittest.main()

class IntegrityAndApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_provider_response_is_rejected(self):
        from unittest.mock import patch
        from lncrawl.translation import scheduler as module
        class Client:
            def __init__(self, data):
                self.data = data
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, *args, **kwargs):
                class Response:
                    status_code = 200
                    headers = {}
                    def json(self_nonlocal):
                        return self.data
                return Response()
        for reason in ("MAX_TOKENS", "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
            data = {"candidates": [{"finishReason": reason, "content": {"parts": [{"text": '{"text":"partial"}'}]}}]}
            with self.subTest(reason=reason), patch.object(module.httpx, "AsyncClient", return_value=Client(data)):
                with self.assertRaises(ProviderError):
                    await Scheduler(keys=["test"])._send("gemini-3.1-flash-lite", {}, "test")
        data = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"text":"complete"}'}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}}
        with patch.object(module.httpx, "AsyncClient", return_value=Client(data)):
            result = await Scheduler(keys=["test"])._send("gemini-3.1-flash-lite", {}, "test")
        self.assertEqual(result["text"], "complete")
        self.assertEqual(result["__response_meta__"]["input_token_count"], 10)

    async def test_api_accepts_raw_only_and_rejects_duplicate(self):
        from lncrawl.translation import api
        from lncrawl.translation.models import Inputs
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory, patch.object(api, "ROOT", Path(directory)), \
             patch.object(api, "start", side_effect=lambda store: {"job_id": store.id}):
            result = await api.create(Inputs(raw=RAW))
            self.assertEqual(len(result["job_id"]), 64)
            flat_dictionary = [{**entry("钦天监", "Khâm Thiên Giám"), "type": "institution"}]
            result = await api.create(Inputs(raw=RAW, dictionary=flat_dictionary))
            self.assertEqual(api.get_store(result["job_id"]).read("inputs.json")["dictionary"],
                             flat_dictionary)
            with self.assertRaises(HTTPException) as caught:
                await api.create(Inputs(raw="第1章\n甲。\n第1章\n乙。"))
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(caught.exception.detail["error_type"], "duplicate_chapter")
        with self.assertRaises(ValueError):
            Inputs.model_validate({"raw": RAW, "vietphrase": "unused"})

class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_legacy_chapter_checkpoint_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": RAW, "dictionary": None, "vietphrase": "old input"})
            chapters = parse_chapters(RAW)
            legacy_entries = [entry("邱途", "Khâu Đồ"), entry("秦舒曼", "Tần Thư Mạn")]
            store.write("frozen-dictionary.json", {
                "dictionary": {"version": 8, "entries": legacy_entries},
            })
            store.write("parsed-chapters.json", {
                "pairs": [[{"number": ch.number, "title": ch.title,
                            "paragraphs": list(ch.paragraphs)}, {}] for ch in chapters],
            })
            translations = [
                ["Khâu Đồ nói: Chúng ta có 2 người.", "Tần Thư Mạn mỉm cười."],
                ["Khâu Đồ rời đi."],
            ]
            for ch, texts in zip(chapters, translations):
                store.write(f"chapters/{ch.key}.json", {
                    "translation": {
                        "title": f"Chương {ch.number}: " + ("Lần đầu gặp" if ch.number == 1 else "Tiếp tục"),
                        "segments": [{"id": i, "text": text} for i, text in enumerate(texts)],
                        "unresolved_terms": [],
                    },
                })
            fake = FakeGemini()
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=fake, keys=[None])).run()
            self.assertEqual(fake.calls, [])
            self.assertTrue(store.read("chapters/1.json")["migrated"])
            self.assertEqual(len(store.read("dictionary.json")["entries"]), 2)

class LongAndPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_paragraph_splits_at_sentence_boundary_and_merges(self):
        import json
        raw = "第1章 雨\n" + "今天下雨了。" * 120
        calls = []
        async def transport(model, body):
            payload = json.loads(body["contents"][0]["parts"][0]["text"])
            calls.append(payload)
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            return {"title": "Mưa", "segments": [
                {"id": item["id"], "text": "Hôm nay trời mưa. " * item["text"].count("今天下雨了。")}
                for item in payload["raw"]
            ]}
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TRANSLATION_MAX_RAW_CHARS": "500"}):
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])).run()
            translated = store.read("translated.json")["chapters"][0]["paragraphs"]
            self.assertEqual(len(translated), 1)
            self.assertEqual(translated[0].count("Hôm nay trời mưa."), 120)
            translation_calls = [item for item in calls if "chapter" in item and "raw" in item]
            self.assertEqual(len(translation_calls), 2)
            self.assertTrue(all("。" == part["text"][-1] for item in translation_calls for part in item["raw"]))

    async def test_post_dictionary_merge_repairs_only_affected_paragraph(self):
        import json
        raw = "第1章 再会\n邱途说。\n他提到了《明月策》。"
        requests = []
        async def transport(model, body):
            payload = json.loads(body["contents"][0]["parts"][0]["text"])
            requests.append(payload)
            if "candidates" in payload:
                return {"confirmed": [{
                    "operation": "add_entry", "entry": {
                        **entry("明月策", "Minh Nguyệt Sách"),
                        "type": "book_title", "gender": "not_applicable",
                    },
                }], "unresolved": []}
            if "current_vietnamese" in payload:
                return {"id": payload["id"], "text": "Ông nhắc tới Minh Nguyệt Sách."}
            return {"title": "Gặp lại", "segments": [
                {"id": item["id"], "text": "Khâu Đồ nói." if item["text"] == "邱途说。" else "Ông nhắc tới cuốn sách ấy."}
                for item in payload["raw"]
            ]}
        with tempfile.TemporaryDirectory() as directory:
            dictionary = {"version": 10, "entries": [entry("邱途", "Khâu Đồ")]}
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": dictionary})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])).run()
            self.assertEqual(next(item for item in store.read("dictionary.json")["entries"]
                                  if item["source"] == "明月策")["translation"], "Minh Nguyệt Sách")
            self.assertEqual(store.read("translated.json")["chapters"][0]["paragraphs"][1],
                             "Ông nhắc tới Minh Nguyệt Sách.")
            self.assertEqual(sum("current_vietnamese" in item for item in requests), 1)

class NumericTests(unittest.TestCase):
    def test_chinese_number_may_be_rendered_as_digit(self):
        from lncrawl.translation.models import Translation
        from lncrawl.translation.validation import local_findings
        chapter = parse_chapters("第1章 数目\n这里有两个人。")[0]
        result = Translation.model_validate({"title": "Số người", "segments": [
            {"id": chapter.paragraph_ids[0], "text": "Ở đây có 2 người."}
        ]})
        self.assertEqual(local_findings(chapter, result, load_dictionary(None)), [])

    def test_vietnamese_decimal_comma_matches_source_decimal_period(self):
        from lncrawl.translation.models import Translation
        from lncrawl.translation.validation import local_findings
        chapter = parse_chapters("第1章 数目\n热量350和900，一斤油等于2.5714斤淀粉。")[0]
        result = Translation.model_validate({"title": "Số liệu", "segments": [
            {"id": chapter.paragraph_ids[0], "text": "Nhiệt lượng 350 và 900, một cân dầu bằng 2,5714 cân tinh bột."}
        ]})
        self.assertEqual(local_findings(chapter, result, load_dictionary(None)), [])
        result.segments[0].text = "Nhiệt lượng 350 và 900, một cân dầu bằng 2,5715 cân tinh bột."
        self.assertEqual(local_findings(chapter, result, load_dictionary(None))[0]["kind"],
                         "numeric_mismatch")

    def test_locked_term_ignores_capitalization_but_not_spelling(self):
        from lncrawl.translation.models import Translation
        from lncrawl.translation.validation import local_findings
        chapter = parse_chapters("第1章 廷议\n今日廷议免了。")[0]
        dictionary = load_dictionary([{**entry("廷议", "đình nghị"), "type": "term"}])
        result = Translation.model_validate({"title": "Đình nghị", "segments": [
            {"id": chapter.paragraph_ids[0], "text": "Đình nghị hôm nay được miễn."}
        ]})
        self.assertEqual(local_findings(chapter, result, dictionary), [])
        result.segments[0].text = "ĐÌNH NGHỊ hôm nay được miễn."
        self.assertEqual(local_findings(chapter, result, dictionary), [])
        result.segments[0].text = "ĐÌNH NGHĨ hôm nay được miễn."
        self.assertEqual(local_findings(chapter, result, dictionary)[0]["kind"],
                         "locked_term_missing")

class RejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_title_entity_stays_unresolved_and_translation_continues(self):
        import json
        raw = "第1章 Tin tức\n邱探员说。"
        async def transport(model, body):
            payload = json.loads(body["contents"][0]["parts"][0]["text"])
            if "candidates" in payload:
                return {"confirmed": [{"operation": "add_entry",
                                       "entry": entry("邱探员", "Thám viên Khâu")}], "unresolved": []}
            return {"title": "Tin tức", "segments": [
                {"id": item["id"], "text": "Một thám viên họ Khâu nói."} for item in payload["raw"]]}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])).run()
            self.assertEqual(store.read("dictionary.json")["entries"], [])
            self.assertEqual(store.read("unresolved.json")[0]["source"], "邱探员")
            self.assertEqual(load_unresolved(store.read("dictionary.json"))[0]["source"], "邱探员")
            self.assertEqual(store.read("progress.json")["status"], "done")

class TruncationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_tokens_causes_semantic_resplit_before_commit(self):
        import json
        raw = "第1章 雨\n" + "今天下雨了。" * 120
        lengths = []
        async def transport(model, body):
            payload = json.loads(body["contents"][0]["parts"][0]["text"])
            if "candidates" in payload:
                return {"confirmed": [], "unresolved": []}
            size = sum(len(part["text"]) for part in payload["raw"])
            lengths.append(size)
            if size > 500:
                raise ProviderError("Gemini incomplete response: MAX_TOKENS",
                                    category=ErrorCategory.INCOMPLETE_GENERATION)
            return {"title": "Mưa", "segments": [
                {"id": item["id"], "text": "Hôm nay trời mưa. " * item["text"].count("今天下雨了。")}
                for item in payload["raw"]
            ]}
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TRANSLATION_MAX_RAW_CHARS": "1000"}):
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])).run()
            self.assertGreater(max(lengths), 500)
            self.assertTrue(any(length <= 500 for length in lengths))
            self.assertEqual(store.read("progress.json")["status"], "done")
            self.assertEqual(store.read("translated.json")["chapters"][0]["paragraphs"][0].count("Hôm nay trời mưa."), 120)

class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_quota_disables_only_one_key_model_pair(self):
        from lncrawl.translation.models import Repair
        scheduler = Scheduler(concurrency=1, spacing=0, keys=["first", "second"])
        attempted, records = [], []
        async def send(model, body, key):
            attempted.append((model, key))
            if model == "gemini-3.1-flash-lite" and key == "first":
                raise ProviderError("daily quota", category=ErrorCategory.DAILY_QUOTA_EXHAUSTED)
            return {"id": "P0001_0001", "text": "Đã dịch"}
        scheduler._send = send
        result = await scheduler.request("policy", {"raw": "原文"}, Repair, records.append)
        self.assertEqual(attempted, [
            ("gemini-3.1-flash-lite", "first"),
            ("gemini-3.1-flash-lite", "second"),
        ])
        self.assertIn(("gemini-3.1-flash-lite", 0), scheduler.daily_exhausted)
        self.assertNotIn(("gemini-3.5-flash-lite", 0), scheduler.daily_exhausted)
        self.assertEqual(result._request_meta["key_slot"], 2)
        running = [record for record in records if record["status"] == "running"]
        self.assertEqual([record["key_rotated"] for record in running], [False, True])
        self.assertNotIn("queued", {record["status"] for record in records})

    async def test_temporary_error_gets_two_retries_on_same_pair(self):
        from lncrawl.translation.models import Repair
        scheduler = Scheduler(concurrency=1, spacing=0, keys=["first"])
        attempts, records = [], []
        async def send(model, body, key):
            attempts.append((model, key))
            if len(attempts) < 3:
                raise ProviderError("temporary", retryable=True,
                                    category=ErrorCategory.TEMPORARY_PROVIDER_ERROR)
            return {"id": "P0001_0001", "text": "Đã dịch"}
        scheduler._send = send
        result = await scheduler.request("policy", {"raw": "原文"}, Repair, records.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result._request_meta["retry_count"], 2)
        running = [record for record in records if record["status"] == "running"]
        self.assertEqual([record["attempt"] for record in running], [1, 2, 3])
        self.assertFalse(any(record["key_rotated"] for record in running))
        self.assertNotIn("queued", {record["status"] for record in records})

    async def test_quota_classification_uses_dimension_even_on_403(self):
        from lncrawl.translation.scheduler import response_error_category
        class Response:
            status_code = 403
            headers = {}
            def json(self):
                return {"error": {"message": "Quota exceeded: requests per day"}}
        self.assertEqual(response_error_category(Response()), ErrorCategory.DAILY_QUOTA_EXHAUSTED)

class UnresolvedScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_carried_unresolved_outside_active_raw_does_not_request_review(self):
        dictionary = {"version": 9, "entries": [
            entry("邱途", "Khâu Đồ"), entry("秦舒曼", "Tần Thư Mạn"),
        ], "unresolved": [{
            "source": "旧地名", "possible_type": "location", "reason": "unknown",
            "evidence": ["旧地名"], "first_seen_chapter": 1, "last_seen_chapter": 1,
        }]}
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeGemini()
            store = Store(Path(directory), inputs={"raw": RAW, "dictionary": dictionary})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=fake, keys=[None])).run()
            self.assertEqual(sum("candidates" in call for _, call in fake.calls), 0)
            self.assertEqual(store.read("unresolved.json")[0]["source"], "旧地名")

class ProviderShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_noncanonical_provider_fields_become_unresolved(self):
        import json
        raw = "第1章 开始\n邱途说。"
        async def transport(model, body):
            payload = json.loads(body["contents"][0]["parts"][0]["text"])
            if "candidates" in payload:
                return {"confirmed": [{"operation": "add", "entry": {
                    "source": "邱途", "translation": "Qiu Tu", "type": "character",
                    "gender": "male", "status": "active",
                }}], "unresolved": []}
            return {"title": "Bắt đầu", "segments": [
                {"id": item["id"], "text": "Một người lên tiếng."} for item in payload["raw"]]}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), inputs={"raw": raw, "dictionary": None})
            await Pipeline(store, Scheduler(concurrency=1, spacing=0, transport=transport, keys=[None])).run()
            self.assertEqual(store.read("dictionary.json")["entries"], [])
            self.assertEqual(store.read("unresolved.json")[0]["source"], "邱途")
            self.assertEqual(store.read("progress.json")["status"], "done")
