import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from lncrawl.translation import prompts
from lncrawl.translation.dictionary import export_dictionary, load_legacy, sanity
from lncrawl.translation.models import MODELS, Alignment, Context, Inputs, Term
from lncrawl.translation.parsing import (
    make_chunks,
    pair_chapters,
    parse_chapters,
    validate_alignment,
)
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.scheduler import ProviderError, Scheduler, retry_delay
from lncrawl.translation.store import Store


class ParsingTests(unittest.TestCase):
    def test_chinese_numbers_and_mismatch(self):
        raw = parse_chapters("第十一章 开始\n他走了。\n第十二章 结束\n她来了。")
        vp = parse_chapters("Chương 11 Bắt đầu\nHắn đi rồi.\nChương 12 Kết thúc\nNàng đến.")
        self.assertEqual([r.number for r, _ in pair_chapters(raw, vp)], [11, 12])
        with self.assertRaises(ValueError):
            pair_chapters(raw, vp[:1])
        with self.assertRaises(ValueError):
            parse_chapters("lost content\n第1章\n他走了。")

    def test_semantic_groups_and_complete_coverage(self):
        raw = parse_chapters("第1章\n" + "甲。" * 2100 + "\n" + "乙。" * 2100)[0]
        vp = parse_chapters("Chương 1\nMột\nHai\nBa")[0]
        alignment = Alignment.model_validate(
            {
                "confirmed": True,
                "groups": [
                    {"raw": [0], "vp": [0, 1], "safe_break": True},
                    {"raw": [1], "vp": [2], "safe_break": True},
                ],
            }
        )
        validate_alignment(alignment, raw, vp)
        chunks = make_chunks(alignment, raw, vp)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([p["text"] for p in chunks[0]["vp"]], ["Một", "Hai"])
        alignment.groups[1].vp = [1]
        with self.assertRaises(ValueError):
            validate_alignment(alignment, raw, vp)

    def test_exact_input_contract(self):
        with self.assertRaises(ValueError):
            Inputs(raw="中", vietphrase="Trung", model="arbitrary")


class DictionaryTests(unittest.TestCase):
    def test_legacy_cleanup_and_status(self):
        terms, problems = load_legacy(
            {
                "glossary": {
                    "不会": "ạnh được",
                    "Khâu Đồ": "Khâu Đồ",
                    "邱途": {
                        "translation": "Khâu Đồ",
                        "type": "character",
                        "status": "provisional",
                    },
                }
            }
        )
        self.assertEqual(len(problems), 2)
        self.assertEqual(terms[0].status, "provisional")
        self.assertEqual(terms[0].gender, "unknown")
        dictionary = export_dictionary({t.source: t for t in terms})
        self.assertEqual(dictionary["statistics"]["total_characters"], 1)

    def test_legacy_conflict_is_reconsidered(self):
        terms, problems = load_legacy(
            {"characters": {"邱途": "Khâu Đồ"}, "glossary": {"邱途": "Sai tên"}}
        )
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].status, "provisional")
        self.assertIn("Legacy conflict", terms[0].evidence)
        self.assertEqual(problems[0]["classification"], "suspicious")

    def test_collision_and_fragment(self):
        one = Term(source="邱途", translation="Khâu Đồ", type="character")
        two = Term(source="阎嗔", translation="Khâu Đồ", type="character")
        with self.assertRaises(ValueError):
            sanity({one.source: one, two.source: two})
        two.source = "邱途也"
        two.translation = "Khâu Đồ cũng"
        with self.assertRaises(ValueError):
            sanity({one.source: one, two.source: two})


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_order_same_task(self):
        seen, records = [], []

        async def transport(model, body):
            seen.append((model, body))
            if len(seen) < 5:
                raise ProviderError("quota", True)
            return {"context": "ok"}

        scheduler = Scheduler(transport=transport, spacing=0)
        with patch("lncrawl.translation.scheduler.asyncio.sleep", new=AsyncMock()):
            result = await scheduler.request(
                "operation", {"chapter": 31, "chunk": 2}, Context, records.append
            )
        self.assertEqual(result.context, "ok")
        self.assertEqual(
            [m for m, _ in seen], [MODELS[0], MODELS[0], MODELS[1], MODELS[1], MODELS[2]]
        )
        self.assertTrue(all(body == seen[0][1] for _, body in seen))
        self.assertEqual(records[-1]["retry_count"], 4)

    async def test_deterministic_failure_no_fallback(self):
        calls = []

        async def transport(model, body):
            calls.append(model)
            raise ProviderError("invalid API key", False)

        with self.assertRaises(ProviderError):
            await Scheduler(transport=transport).request("test", {}, Context, lambda x: None)
        self.assertEqual(calls, [MODELS[0]])

    async def test_global_stagger_concurrency_and_cancel(self):
        starts, running, maximum = [], 0, 0

        async def transport(model, body):
            nonlocal running, maximum
            starts.append(time.monotonic())
            running += 1
            maximum = max(maximum, running)
            try:
                await asyncio.sleep(0.04)
                return {"context": "ok"}
            finally:
                running -= 1

        scheduler = Scheduler(transport=transport, spacing=0.02)
        await asyncio.gather(
            *(scheduler.request("test", {"i": i}, Context, lambda x: None) for i in range(4))
        )
        self.assertLessEqual(maximum, 2)
        self.assertTrue(all(b - a >= 0.018 for a, b in zip(starts, starts[1:])))
        task = asyncio.create_task(scheduler.request("test", {}, Context, lambda x: None))
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(running, 0)

    async def test_gemini_transport_response_and_error_normalization(self):
        original_client = httpx.AsyncClient
        fixtures = [
            (
                200,
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": '{"context":"ok"}'}]},
                        }
                    ]
                },
                None,
            ),
            (200, {"candidates": [{"finishReason": "MAX_TOKENS"}]}, True),
            (200, {"candidates": [{"finishReason": "SAFETY"}]}, False),
            (200, {"candidates": []}, True),
            (429, {}, True),
            (403, {}, False),
            (404, {}, False),
        ]
        for status, response, retryable in fixtures:

            def handle(request):
                self.assertEqual(request.headers["x-goog-api-key"], "test-key")
                self.assertEqual(request.url.path, f"/v1beta/models/{MODELS[0]}:generateContent")
                return httpx.Response(status, json=response, headers={"Retry-After": "7"})

            def client_factory(**kwargs):
                return original_client(transport=httpx.MockTransport(handle), **kwargs)

            with (
                patch.dict(os.environ, {"GOOGLE_AI_API_KEY": "test-key"}),
                patch(
                    "lncrawl.translation.scheduler.httpx.AsyncClient", side_effect=client_factory
                ),
            ):
                if retryable is None:
                    self.assertEqual(await Scheduler()._send(MODELS[0], {}), {"context": "ok"})
                else:
                    with self.assertRaises(ProviderError) as caught:
                        await Scheduler()._send(MODELS[0], {})
                    self.assertEqual(caught.exception.retryable, retryable)
                    if status == 429:
                        self.assertEqual(caught.exception.retry_after, 7)

    def test_retry_after(self):
        self.assertEqual(retry_delay("12"), 12)
        self.assertEqual(retry_delay("garbage"), 0)


class FakeGemini:
    def __init__(self):
        self.calls = []
        self.fail_chunk = False
        self.bad_translation = False
        self.missed = False

    async def __call__(self, model, body):
        instruction = body["systemInstruction"]["parts"][0]["text"]
        data = json.loads(body["contents"][0]["parts"][0]["text"])
        self.calls.append((instruction, data, model))
        if instruction == prompts.ALIGN:
            return {
                "confirmed": True,
                "groups": [
                    {"raw": [i], "vp": [i], "safe_break": True}
                    for i in range(len(data["raw"]["paragraphs"]))
                ],
            }
        if instruction == prompts.DISCOVER:
            return {"candidates": []}
        if instruction == prompts.EVIDENCE:
            return {"findings": "祖石 is an artifact called Tổ Thạch."}
        if instruction == prompts.RESOLVE:
            inherited = data.get("inherited")
            term = inherited or {
                "source": data["source"],
                "translation": "Tổ Thạch",
                "type": "artifact",
                "status": "locked",
                "gender": "unknown",
                "aliases": [],
                "forms": {},
                "evidence": "祖石",
            }
            return {"decision": "ACCEPT", "term": term, "reason": "Explicit evidence"}
        if instruction == prompts.CONTEXT:
            return {"context": "No unsupported facts."}
        if instruction == prompts.TRANSLATE:
            paragraphs = data["RAW TO TRANSLATE"]
            if self.fail_chunk and paragraphs[0]["id"] == 1:
                raise ProviderError("Simulated outage", False)
            return {
                "title": "Chương 1",
                "segments": [
                    {
                        "id": p["id"],
                        "text": "bad" if self.bad_translation else f"Đoạn {p['id']} Tổ Thạch.",
                    }
                    for p in paragraphs
                ],
            }
        if instruction == prompts.VALIDATE:
            return {
                "issues": [
                    {"segment_id": s["id"], "kind": "missing", "explanation": "Missing source"}
                    for s in data["translation"]["segments"]
                    if s["text"] == "bad"
                ],
                "missed_terms": [{"source": "祖石", "evidence": "祖石"}] if self.missed else [],
            }
        if instruction == prompts.REPAIR:
            return {
                "segments": [{"id": i, "text": f"Đoạn {i} Tổ Thạch."} for i in data["affected_ids"]]
            }
        raise AssertionError("Unknown operation")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root, long=False, dictionary=None):
        paragraph = "祖石发光。" * (850 if long else 1)
        return Store(
            Path(root),
            Inputs(
                raw=f"第1章 开始\n{paragraph}\n{paragraph}",
                vietphrase="Chương 1 Bắt đầu\nTổ thạch sáng.\nTổ thạch sáng.",
                dictionary=dictionary,
            ).model_dump(),
        )

    async def test_end_to_end_targeted_repair_and_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()
            fake.bad_translation = True
            scheduler = Scheduler(transport=fake, spacing=0)
            await Pipeline(store, scheduler).run()
            self.assertEqual(store.read("progress.json")["status"], "done")
            self.assertEqual(len(store.read("translated.json")["chapters"]), 1)
            self.assertEqual(len([c for c in fake.calls if c[0] == prompts.REPAIR]), 1)
            self.assertTrue(all(c[2] == MODELS[0] for c in fake.calls))
            count = len(fake.calls)
            await Pipeline(store, scheduler).run()
            self.assertEqual(count, len(fake.calls))

    async def test_hundred_chapter_batch_order_and_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            inputs = Inputs(
                raw="\n".join(f"第{i}章\n他走了。" for i in range(1, 101)),
                vietphrase="\n".join(f"Chương {i}\nHắn đi rồi." for i in range(1, 101)),
            )
            store = Store(Path(root), inputs.model_dump())
            fake = FakeGemini()
            scheduler = Scheduler(transport=fake, spacing=0)
            await Pipeline(store, scheduler).run()
            self.assertEqual(
                [c["number"] for c in store.read("translated.json")["chapters"]],
                list(range(1, 101)),
            )
            count = len(fake.calls)
            await Pipeline(store, scheduler).run()
            self.assertEqual(len(fake.calls), count)

    async def test_resume_preserves_finalized_chunk(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root, long=True)
            fake = FakeGemini()
            fake.fail_chunk = True
            scheduler = Scheduler(transport=fake, spacing=0)
            with self.assertRaises(ProviderError):
                await Pipeline(store, scheduler).run()
            self.assertIsNotNone(store.read("chunks/1-0.json"))
            fake.fail_chunk = False
            await Pipeline(store, scheduler).run()
            first = [
                c
                for c in fake.calls
                if c[0] == prompts.TRANSLATE and c[1]["RAW TO TRANSLATE"][0]["id"] == 0
            ]
            self.assertEqual(len(first), 1)

    async def test_missed_term_resolved_and_locked_legacy_retained(self):
        with tempfile.TemporaryDirectory() as root:
            absent = Term(source="邱途", translation="Khâu Đồ", type="character", status="locked")
            store = self.store(root, dictionary={"entries": [absent.model_dump()]})
            fake = FakeGemini()
            fake.missed = True
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            entries = {t["source"]: t for t in store.read("dictionary.json")["entries"]}
            self.assertEqual(entries["邱途"]["translation"], "Khâu Đồ")
            self.assertEqual(entries["邱途"]["status"], "locked")
            self.assertEqual(entries["祖石"]["type"], "artifact")
            self.assertEqual(len([c for c in fake.calls if c[0] == prompts.TRANSLATE]), 1)

    async def test_invalid_segment_coverage_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()

            async def corrupt(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.TRANSLATE:
                    result["segments"].pop()
                return result

            with self.assertRaises(QualityError):
                await Pipeline(store, Scheduler(transport=corrupt, spacing=0)).run()
            self.assertIsNone(store.read("translated.json"))
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            self.assertEqual(store.read("progress.json")["status"], "done")


if __name__ == "__main__":
    unittest.main()
