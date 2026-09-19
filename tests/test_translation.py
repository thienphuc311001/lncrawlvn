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
from lncrawl.translation.dictionary import (
    export_dictionary,
    load_legacy,
    quantity_source_problem,
    sanity,
    source_name,
    terminology_gaps,
)
from lncrawl.translation.models import MODELS, Alignment, Context, Inputs, Term
from lncrawl.translation.parsing import (
    make_chunks,
    pair_chapters,
    parse_chapters,
    validate_alignment,
    validate_inputs,
)
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.scheduler import (
    ErrorCategory,
    ProviderError,
    Scheduler,
    api_keys,
    response_daily_quota,
    response_error_category,
    response_retry_delay,
    retry_delay,
)
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
    def test_quantity_phrase_requires_explicit_fixed_name_evidence(self):
        name = "2-3队特勤部小队"
        self.assertIsNotNone(quantity_source_problem(name, ["任意调动" + name + "的特权"]))
        self.assertIsNone(quantity_source_problem(name, ["番号为" + name]))
        self.assertIsNone(quantity_source_problem("3个愿望", ["《3个愿望》是一本书。"]))
        self.assertIsNone(quantity_source_problem("108种增进感情的方式"))

    def test_outer_source_markers_do_not_change_named_identity(self):
        self.assertEqual(source_name("【怨念纠缠】"), "怨念纠缠")
        self.assertEqual(source_name("《美丽新世界》"), "美丽新世界")
        self.assertEqual(source_name("【不完整"), "【不完整")
        self.assertEqual(source_name("祖石（残）"), "祖石（残）")

    def test_source_aware_mapping_checks_longest_names_and_address_forms(self):
        terms = [
            {"source": "探查署", "translation": "Cục Thám tra"},
            {"source": "新界市探查署", "translation": "Thám tra thự thành phố Tân Giới"},
            {"source": "邱途", "translation": "Khâu Đồ", "forms": {"邱长官": "Trưởng quan Khâu"}},
        ]
        self.assertEqual(
            terminology_gaps(
                terms,
                "新界市探查署。邱长官。",
                "Thám tra thự thành phố Tân Giới. trưởng quan  Khâu.",
            ),
            [],
        )
        self.assertEqual(
            terminology_gaps(terms, "新界市探查署。", "Cục Thám tra thành phố Tân Giới."),
            [("新界市探查署", "Thám tra thự thành phố Tân Giới")],
        )
        self.assertEqual(terminology_gaps(terms, "他走了。", "Anh đi rồi."), [])

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
        self.assertEqual(dictionary["entries"], [])
        self.assertEqual(dictionary["statistics"]["ignored_terms"], 1)

    def test_legacy_conflict_is_reconsidered(self):
        terms, problems = load_legacy(
            {"characters": {"邱途": "Khâu Đồ"}, "glossary": {"邱途": "Sai tên"}}
        )
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].status, "provisional")
        self.assertIn("Legacy conflict", terms[0].evidence)
        self.assertEqual(problems[0]["classification"], "suspicious")

    def test_collision_and_fragment(self):
        one = Term(
            source="邱途", translation="Khâu Đồ", type="character", status="locked"
        )
        two = Term(
            source="阎嗔", translation="Khâu Đồ", type="character", status="locked"
        )
        # Shared Vietnamese surfaces are legal; source identity collisions are
        # checked separately through canonical/alias ownership.
        sanity({one.source: one, two.source: two})


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_server_keys_order_and_deduplication(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_AI_API_KEY": " primary ",
                "GOOGLE_AI_API_KEY_BACKUP": "backup",
                "GOOGLE_AI_API_KEY_THIRD": "third",
                "GOOGLE_AI_API_KEYS": "backup, fourth, primary, ,fifth",
            },
            clear=True,
        ):
            self.assertEqual(api_keys(), ["primary", "backup", "third", "fourth", "fifth"])

    def test_provider_error_categories_distinguish_daily_rpm_tpm_and_429_unknown(self):
        def response(quota_id):
            return httpx.Response(
                429, json={"error": {"details": [{"violations": [{"quotaId": quota_id}]}]}}
            )

        self.assertEqual(
            response_error_category(response("GenerateRequestsPerDayPerProject")),
            ErrorCategory.DAILY_QUOTA_EXHAUSTED,
        )
        self.assertEqual(
            response_error_category(response("GenerateRequestsPerMinutePerProject")),
            ErrorCategory.RPM_LIMIT,
        )
        self.assertEqual(
            response_error_category(response("GenerateTokensPerMinutePerProject")),
            ErrorCategory.TPM_LIMIT,
        )
        self.assertEqual(
            response_error_category(httpx.Response(429, json={"error": {}})),
            ErrorCategory.UNKNOWN_ERROR,
        )
        self.assertEqual(
            response_error_category(httpx.Response(503)), ErrorCategory.TEMPORARY_PROVIDER_ERROR
        )
        self.assertEqual(
            response_error_category(httpx.Response(403)), ErrorCategory.AUTHENTICATION_ERROR
        )

    async def test_daily_pair_is_never_retried_during_current_scheduler_run(self):
        records, calls = [], []

        async def transport(model, body):
            calls.append(model)
            if len(calls) == 1:
                raise ProviderError("daily", daily_quota=True)
            return {"context": "ok"}

        scheduler = Scheduler(transport=transport, keys=["one", "two", "three"], spacing=0)
        await scheduler.request("one", {}, Context, records.append)
        scheduler.cursor = 0
        await scheduler.request("two", {}, Context, records.append)
        successes = [event["key_slot"] for event in records if event["status"] == "success"]
        self.assertEqual(successes, [2, 2])
        self.assertIn((MODELS[0], 0), scheduler.daily_exhausted)

    async def test_rpm_cooldown_rotates_immediately_and_reenables_after_60_seconds(self):
        records, clock, calls = [], [0.0], []

        async def transport(model, body):
            calls.append(model)
            if len(calls) == 1:
                raise ProviderError("rpm", True, category=ErrorCategory.RPM_LIMIT)
            return {"context": "ok"}

        async def sleep(delay):
            clock[0] += delay

        with (
            patch("lncrawl.translation.scheduler.time.monotonic", side_effect=lambda: clock[0]),
            patch("lncrawl.translation.scheduler.asyncio.sleep", side_effect=sleep),
        ):
            scheduler = Scheduler(transport=transport, keys=["one", "two", "three"], spacing=0)
            await scheduler.request("one", {}, Context, records.append)
            scheduler.cursor = 0
            await scheduler.request("two", {}, Context, records.append)
            clock[0] += 60
            scheduler.cursor = 0
            await scheduler.request("three", {}, Context, records.append)
        self.assertEqual(
            [event["key_slot"] for event in records if event["status"] == "success"], [2, 2, 1]
        )
        self.assertTrue(
            any(
                event["status"] == "rate_limit_cooldown" and event["retry_after"] == 60
                for event in records
            )
        )

    async def test_quota_rotates_key_before_model_and_never_logs_credentials(self):
        original_client = httpx.AsyncClient
        for daily, fallback in ((False, False), (True, False), (True, True)):
            seen, records = [], []

            def handle(request):
                model = request.url.path.split("/")[-1].split(":")[0]
                key = request.headers["x-goog-api-key"]
                seen.append((model, key))
                if key == "private-primary" or (fallback and model == MODELS[0]):
                    return httpx.Response(
                        429,
                        json={
                            "error": {
                                "details": [
                                    {
                                        "violations": [
                                            {
                                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel"
                                                if daily
                                                else "GenerateRequestsPerMinutePerProjectPerModel"
                                            }
                                        ]
                                    }
                                ]
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {"parts": [{"text": '{"context":"ok"}'}]},
                            }
                        ]
                    },
                )

            def client_factory(**kwargs):
                return original_client(transport=httpx.MockTransport(handle), **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {
                        "GOOGLE_AI_API_KEY": "private-primary",
                        "GOOGLE_AI_API_KEY_BACKUP": "private-backup",
                    },
                    clear=True,
                ),
                patch(
                    "lncrawl.translation.scheduler.httpx.AsyncClient", side_effect=client_factory
                ),
                patch("lncrawl.translation.scheduler.asyncio.sleep", new=AsyncMock()),
            ):
                result = await Scheduler(spacing=0).request(
                    "test", {"chapter": 1}, Context, records.append
                )
            expected = [(MODELS[0], "private-primary"), (MODELS[0], "private-backup")]
            if fallback:
                expected.extend([(MODELS[1], "private-primary"), (MODELS[1], "private-backup")])
            self.assertEqual(seen, expected)
            self.assertEqual(result.context, "ok")
            self.assertTrue(
                any(
                    record["status"] in ("daily_quota_disabled", "rate_limit_cooldown")
                    for record in records
                )
            )
            self.assertEqual(records[-1]["key_slot"], 2)
            self.assertNotIn("private-primary", json.dumps(records))
            self.assertNotIn("private-backup", json.dumps(records))

    async def test_auth_and_outage_do_not_rotate_keys(self):
        original_client = httpx.AsyncClient
        for status in (403, 503):
            seen = []

            def handle(request):
                seen.append(request.headers["x-goog-api-key"])
                return httpx.Response(status, json={})

            def client_factory(**kwargs):
                return original_client(transport=httpx.MockTransport(handle), **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {"GOOGLE_AI_API_KEY": "primary", "GOOGLE_AI_API_KEY_BACKUP": "backup"},
                    clear=True,
                ),
                patch(
                    "lncrawl.translation.scheduler.httpx.AsyncClient", side_effect=client_factory
                ),
                patch("lncrawl.translation.scheduler.asyncio.sleep", new=AsyncMock()),
            ):
                with self.assertRaises(ProviderError):
                    await Scheduler(spacing=0).request("test", {}, Context, lambda record: None)
            self.assertEqual(
                seen,
                ["primary", "backup"]
                if status == 403
                else [
                    "primary",
                    "primary",
                    "backup",
                    "backup",
                    "primary",
                    "primary",
                    "backup",
                    "backup",
                    "primary",
                    "primary",
                    "backup",
                    "backup",
                ],
            )

    async def test_retry_waits_past_rounded_provider_quota_window(self):
        clock, starts = [0.0], []

        async def sleep(delay):
            clock[0] += delay

        async def transport(model, body):
            starts.append((model, clock[0]))
            if len(starts) == 1:
                raise ProviderError("minute quota", True, retry_after=7)
            return {"context": "ok"}

        with (
            patch("lncrawl.translation.scheduler.asyncio.sleep", side_effect=sleep),
            patch("lncrawl.translation.scheduler.time.monotonic", side_effect=lambda: clock[0]),
        ):
            result = await Scheduler(transport=transport).request(
                "test", {}, Context, lambda metadata: None
            )
        self.assertEqual(result.context, "ok")
        self.assertEqual([model for model, _ in starts], [MODELS[0], MODELS[0]])
        self.assertGreaterEqual(starts[1][1] - starts[0][1], 8)

    async def test_daily_model_allowance_moves_to_configured_fallback_without_short_retry(self):
        seen, records = [], []

        async def transport(model, body):
            seen.append((model, body))
            if model == MODELS[0]:
                raise ProviderError("daily allowance", True, retry_after=59, daily_quota=True)
            return {"context": "ok"}

        result = await Scheduler(transport=transport, spacing=0).request(
            "test", {"chapter": 46}, Context, records.append
        )
        self.assertEqual(result.context, "ok")
        self.assertEqual([model for model, _ in seen], [MODELS[0], MODELS[1]])
        self.assertEqual(seen[0][1], seen[1][1])
        self.assertTrue(
            next(record for record in records if record["status"] == "failed")["daily_quota"]
        )

    async def test_exhausted_chain_reports_all_models_and_logs_live_attempts(self):
        seen, records = [], []

        async def transport(model, body):
            seen.append(model)
            self.assertEqual(records[-1]["status"], "running")
            if model != MODELS[2]:
                raise ProviderError("HTTP 429 (daily quota exhausted)", True, daily_quota=True)
            raise ProviderError("HTTP 503", True)

        with patch("lncrawl.translation.scheduler.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ProviderError) as caught:
                await Scheduler(transport=transport, spacing=0).request(
                    "test", {}, Context, records.append
                )
        self.assertEqual(seen, [MODELS[0], MODELS[1], MODELS[2], MODELS[2]])
        message = str(caught.exception)
        self.assertIn("No usable Gemini model/API-key combination remains", message)
        self.assertLess(message.index(MODELS[0]), message.index(MODELS[1]))
        self.assertLess(message.index(MODELS[1]), message.index(MODELS[2]))
        self.assertIn("daily quota exhausted", message)
        self.assertIn("HTTP 503", message)
        self.assertEqual([r["model"] for r in records if r["status"] == "retrying"], [MODELS[2]])

    async def test_primary_success_never_calls_fallback(self):
        seen, records = [], []

        async def transport(model, body):
            seen.append(model)
            return {"context": "ok"}

        await Scheduler(transport=transport, spacing=0).request("test", {}, Context, records.append)
        self.assertEqual(seen, [MODELS[0]])
        self.assertEqual([r["status"] for r in records], ["queued", "running", "success"])

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
        response = httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "42.125s",
                        }
                    ]
                }
            },
        )
        self.assertEqual(response_retry_delay(response), 42.125)
        self.assertEqual(
            response_retry_delay(
                httpx.Response(429, json={"error": None}, headers={"Retry-After": "2"})
            ),
            2,
        )
        daily = httpx.Response(
            429,
            json={
                "error": {
                    "details": [
                        {
                            "violations": [
                                {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                            ]
                        }
                    ]
                }
            },
        )
        self.assertTrue(response_daily_quota(daily))
        self.assertFalse(response_daily_quota(response))


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
        if instruction == prompts.BATCH_RESOLVE:
            return {
                "results": [
                    {
                        "source": item["source"],
                        "decision": "ACCEPT" if item["source"] == "祖石" else "REJECT",
                        "eligibility": {
                            "complete_semantic_unit": True,
                            "named_or_novel_specific": item["source"] == "祖石",
                            "consistency_matters": True,
                            "evidence_supports": True,
                        },
                        "term": {
                            "source": item["source"],
                            "translation": "Tổ Thạch",
                            "type": "artifact",
                            "status": "locked",
                        }
                        if item["source"] == "祖石"
                        else None,
                        "reason": "Fixture-specific canonical artifact or rejected fragment",
                    }
                    for item in data["candidates"]
                ]
            }
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
            return {
                "decision": "ACCEPT",
                "eligibility": {
                    "complete_semantic_unit": True,
                    "named_or_novel_specific": True,
                    "consistency_matters": True,
                    "evidence_supports": True,
                },
                "term": term,
                "reason": "Explicit evidence",
            }
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
                        "text": "未翻译原文"
                        if self.bad_translation
                        else f"Đoạn {p['id']} Tổ Thạch." * max(1, len(p["text"]) // 20),
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
    async def test_absent_inherited_numeric_book_title_is_retained(self):
        with tempfile.TemporaryDirectory() as root:
            dictionary = {
                "entries": [
                    {
                        "source": "3个愿望",
                        "translation": "Ba Điều Ước",
                        "type": "artifact",
                        "status": "locked",
                        "evidence": "Previously identified book title.",
                    }
                ]
            }
            store = self.store(root, dictionary=dictionary)
            await Pipeline(store, Scheduler(transport=FakeGemini(), spacing=0)).run()
            self.assertEqual(store.read("dictionary.json")["entries"][0]["source"], "3个愿望")

    async def test_generic_locked_role_is_not_folded_into_one_person(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n署长阎嗔是男人。",
                    vietphrase="Chương 1\nThự trưởng Diêm Chân là đàn ông.",
                ).model_dump(),
            )
            fake = FakeGemini()
            count = 0
            feedback = []

            async def transport(model, body):
                nonlocal count
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.RESOLVE:
                    count += 1
                    data = json.loads(body["contents"][0]["parts"][0]["text"])
                    result["term"].update(
                        source="阎嗔", translation="Diêm Chân", type="character", gender="male"
                    )
                    if count == 1:
                        result["term"].update(aliases=["署长"], forms={"署长": "Thự trưởng"})
                    else:
                        feedback.append(data)
                return result

            pipeline = Pipeline(store, Scheduler(transport=transport, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            await pipeline.align(pipeline.pairs[0])
            role = Term(source="署长", translation="Thự trưởng", type="title", status="locked")
            pipeline.terms = {role.source: role}
            await pipeline.resolve("阎嗔")
            self.assertEqual(count, 1)
            self.assertEqual(feedback, [])
            self.assertEqual(pipeline.terms["署长"].model_dump(), role.model_dump())
            self.assertEqual(set(pipeline.terms), {"署长"})
            self.assertIn("阎嗔", pipeline.ignored)

    async def test_person_alias_metadata_receives_targeted_canonical_actor_correction(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n唐副署长就是唐菲菲。",
                    vietphrase="Chương 1\nPhó thự trưởng Đường là Đường Phỉ Phỉ.",
                ).model_dump(),
            )
            fake = FakeGemini()
            count = 0
            feedback = []

            async def transport(model, body):
                nonlocal count
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.RESOLVE:
                    count += 1
                    data = json.loads(body["contents"][0]["parts"][0]["text"])
                    if count == 1:
                        result["term"].update(
                            source="唐副署长",
                            translation="Phó thự trưởng Đường",
                            type="character_alias",
                            gender="female",
                            aliases=["唐菲菲"],
                        )
                    else:
                        feedback.append(data)
                        result["term"].update(
                            source="唐菲菲",
                            translation="Đường Phỉ Phỉ",
                            type="character",
                            gender="female",
                            aliases=["唐副署长"],
                            forms={"唐副署长": "Phó thự trưởng Đường"},
                        )
                        if count == 2:
                            result["term"]["forms"] = {"title": "Phó thự trưởng Đường"}
                return result

            pipeline = Pipeline(store, Scheduler(transport=transport, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            await pipeline.align(pipeline.pairs[0])
            await pipeline.resolve("唐副署长")
            self.assertEqual(count, 1)
            self.assertEqual(feedback, [])
            self.assertEqual(list(pipeline.terms), [])
            self.assertIn("唐副署长", pipeline.ignored)
            self.assertTrue(all(model == MODELS[0] for _, _, model in fake.calls))

    async def test_established_address_form_is_preserved_and_frozen(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()
            pipeline = Pipeline(store, Scheduler(transport=fake, spacing=0))
            pipeline.freeze(
                {
                    "entries": [
                        Term(
                            source="叶将军",
                            translation="Tướng quân Diệp",
                            type="character",
                            status="locked",
                            forms={"叶上校": "Đại tá Diệp"},
                        ).model_dump()
                    ]
                }
            )
            self.assertTrue(pipeline.known_source("叶上校"))
            with self.assertRaisesRegex(QualityError, "frozen"):
                await pipeline.resolve("叶上校")
            self.assertEqual(fake.calls, [])

    async def test_title_alias_merge_preserves_locked_historical_rank_form(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n叶上校升为叶将军。",
                    vietphrase="Chương 1\nĐại tá Diệp thăng thành Tướng quân Diệp.",
                ).model_dump(),
            )
            fake = FakeGemini()
            pipeline = Pipeline(store, Scheduler(transport=fake, spacing=0))
            pipeline.pairs = [
                (
                    parse_chapters(store.read("inputs.json")["raw"])[0],
                    parse_chapters(store.read("inputs.json")["vietphrase"], "VIETPHRASE")[0],
                )
            ]
            await pipeline.align(pipeline.pairs[0])
            pipeline.terms = {
                "叶上校": Term(
                    source="叶上校",
                    translation="Đại tá Diệp",
                    type="character",
                    status="locked",
                    gender="male",
                )
            }

            async def transport(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.RESOLVE:
                    result["term"].update(
                        source="叶将军",
                        translation="Tướng quân Diệp",
                        type="character",
                        gender="male",
                        aliases=["叶上校"],
                        forms={"叶上校": "Diệp thượng tá"},
                    )
                return result

            pipeline.scheduler = Scheduler(transport=transport, spacing=0)
            await pipeline.resolve("叶将军")
            self.assertEqual(list(pipeline.terms), ["叶上校"])
            self.assertIn("叶将军", pipeline.ignored)
            sanity(pipeline.terms)

    async def test_ordinary_prop_is_rejected_when_provider_accepts_failed_eligibility(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n【停尸柜】开了。", vietphrase="Chương 1\n【Tủ thi thể】 mở."
                ).model_dump(),
            )
            fake = FakeGemini()

            async def transport(model, body):
                instruction = body["systemInstruction"]["parts"][0]["text"]
                result = await fake(model, body)
                if instruction == prompts.BATCH_RESOLVE:
                    result["results"][0].update(
                        decision="ACCEPT",
                        term={"source": "停尸柜", "translation": "Tủ thi thể", "type": "artifact"},
                    )
                    result["results"][0]["eligibility"]["named_or_novel_specific"] = False
                return result

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            self.assertEqual(store.read("dictionary.json")["entries"], [])
            self.assertEqual(store.read("working-dictionary.json")["statistics"]["total_terms"], 0)

    async def test_discovery_normalizes_system_markers_without_modifying_raw(self):
        with tempfile.TemporaryDirectory() as root:
            inputs = Inputs(
                raw="第1章\n【祖石】发光。", vietphrase="Chương 1\n【Tổ Thạch】 phát sáng."
            ).model_dump()
            store = Store(Path(root), inputs)
            fake = FakeGemini()

            async def transport(model, body):
                if body["systemInstruction"]["parts"][0]["text"] == prompts.DISCOVER:
                    return {"candidates": [{"source": "【祖石】", "evidence": "【祖石】发光。"}]}
                return await fake(model, body)

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            self.assertEqual(store.read("dictionary.json")["entries"][0]["source"], "祖石")
            self.assertEqual(store.read("inputs.json"), inputs)
            self.assertIn("祖石", store.read("terminology-index.json")["index"]["candidates"])

    async def test_dictionary_synonym_is_repaired_even_when_ai_validator_accepts_it(self):
        with tempfile.TemporaryDirectory() as root:
            dictionary = {
                "entries": [
                    {
                        "source": "祖石",
                        "translation": "Tổ Thạch",
                        "type": "artifact",
                        "status": "locked",
                    }
                ]
            }
            store = self.store(root, dictionary=dictionary)
            fake = FakeGemini()

            async def transport(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.TRANSLATE:
                    result["segments"][0]["text"] = "Viên đá tổ tiên phát sáng."
                return result

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            repairs = [data for instruction, data, _ in fake.calls if instruction == prompts.REPAIR]
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["affected_ids"], [0])
            self.assertEqual(repairs[0]["issues"][0]["kind"], "terminology")
            self.assertIn("Tổ Thạch", store.read("translated.json")["chapters"][0]["text"])
            self.assertTrue(all(model == MODELS[0] for _, _, model in fake.calls))
            self.assertTrue(
                all(
                    "vp" not in data
                    for instruction, data, _ in fake.calls
                    if instruction == prompts.VALIDATE
                )
            )

    async def test_persistent_targeted_repair_fails_after_one_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()
            fake.bad_translation = True
            repairs = []

            async def transport(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.REPAIR:
                    data = json.loads(body["contents"][0]["parts"][0]["text"])
                    repairs.append(data)
                    if len(repairs) == 1:
                        result = {
                            "segments": [
                                {"id": i, "text": "未翻译原文"} for i in data["affected_ids"]
                            ]
                        }
                return result

            with self.assertRaisesRegex(QualityError, "after 1 targeted repair"):
                await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            self.assertEqual([data["repair_attempt"] for data in repairs], [1])

    async def test_alignment_is_local_and_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            self.assertFalse(any(call[0] == prompts.ALIGN for call in fake.calls))
            self.assertIsNotNone(store.read("alignment/1.json"))
            self.assertTrue(all(call[2] == MODELS[0] for call in fake.calls))

    async def test_vietnamese_source_alias_metadata_is_quarantined(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()
            fake.missed = True

            async def transport(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.BATCH_RESOLVE:
                    for item in result["results"]:
                        if item["source"] == "祖石":
                            item["term"]["aliases"] = ["Tổ Thạch", "无证据"]
                            item["term"]["forms"] = {"Tổ Thạch": "Tổ Thạch"}
                return result

            await Pipeline(store, Scheduler(transport=transport, spacing=0)).run()
            term = store.read("dictionary.json")["entries"][0]
            self.assertEqual(term["aliases"], [])
            self.assertEqual(term["forms"], {})
            self.assertEqual(term["source"], "祖石")

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

    async def test_missing_segment_coverage_repairs_only_missing_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            fake = FakeGemini()

            async def corrupt(model, body):
                result = await fake(model, body)
                if body["systemInstruction"]["parts"][0]["text"] == prompts.TRANSLATE:
                    result["segments"].pop()
                return result

            await Pipeline(store, Scheduler(transport=corrupt, spacing=0)).run()
            repairs = [data for instruction, data, _ in fake.calls if instruction == prompts.REPAIR]
            self.assertEqual([item["affected_ids"] for item in repairs], [[1]])
            self.assertEqual([p["id"] for p in repairs[0]["raw"]], [1])
            self.assertEqual(repairs[0]["issues"][0]["type"], "content_missing")
            self.assertEqual(repairs[0]["issues"][0]["source_segment_id"], "c1-k0-s1")
            self.assertEqual(repairs[0]["source_context"][0]["affected_id"], 1)
            self.assertEqual(repairs[0]["translated_context"][0]["affected_id"], 1)
            count = len(fake.calls)
            await Pipeline(store, Scheduler(transport=fake, spacing=0)).run()
            self.assertEqual(len(fake.calls), count)


if __name__ == "__main__":
    unittest.main()
