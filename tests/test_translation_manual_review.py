"""Offline tests for explicit, paragraph-scoped translation review."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from lncrawl.translation import api
from lncrawl.translation.models import PIPELINE_VERSION, Translation, TranslatedSegment
from lncrawl.translation.parsing import parse_chapters
from lncrawl.translation.pipeline import Pipeline
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store, digest


RAW = "第145章 测试\n会试结束。"
DICTIONARY = {"version": 10, "entries": [{
    "source": "会试", "translation": "hội thí", "type": "term",
    "gender": "not_applicable", "status": "locked", "aliases": [],
}]}
ORIGINAL = "Thi hội đã kết thúc."
REPLACEMENT = "Hội thí đã kết thúc."


def failed_store(root):
    inputs = {"raw": RAW, "dictionary": DICTIONARY}
    store = Store(root, inputs=inputs)
    chapter = parse_chapters(RAW)[0]
    pre_hash = digest(DICTIONARY)
    input_hash = digest(inputs)
    store.write("pre-dictionary.json", {
        "input_hash": input_hash, "dictionary": DICTIONARY, "unresolved": [],
    })
    store.write("post-dictionary.json", {
        "input_hash": input_hash, "pre_hash": pre_hash,
        "dictionary": DICTIONARY, "unresolved": [],
    })
    result = Translation(title="Kiểm tra", segments=[
        TranslatedSegment(id=chapter.paragraph_ids[0], text=ORIGINAL),
    ])
    store.write("pending-chapters/145.json", {
        "raw_hash": digest((chapter.title, chapter.paragraphs)),
        "pre_dictionary_hash": pre_hash, "state": "generated_unvalidated",
        "translation": result.model_dump(), "request_metadata": [],
    })
    store.progress("failed", error=(
        "Chapter 145 repair for P0145_0001 already failed; manual review required"
    ))
    return store


def failed_committed_store(root):
    raw = "第142章 测试\n会试结束。"
    inputs = {"raw": raw, "dictionary": None}
    store = Store(root, inputs=inputs)
    chapter = parse_chapters(raw)[0]
    empty_dictionary = {"version": 10, "entries": []}
    pre_hash = digest(empty_dictionary)
    input_hash = digest(inputs)
    store.write("pre-dictionary.json", {
        "input_hash": input_hash, "dictionary": empty_dictionary, "unresolved": [],
    })
    store.write("post-dictionary.json", {
        "input_hash": input_hash, "pre_hash": pre_hash,
        "dictionary": DICTIONARY, "unresolved": [],
    })
    result = Translation(title="Kiểm tra", segments=[
        TranslatedSegment(id=chapter.paragraph_ids[0], text=ORIGINAL),
    ])
    store.write("chapters/142.json", {
        "raw_hash": digest((chapter.title, chapter.paragraphs)),
        "pre_dictionary_hash": pre_hash, "translation": result.model_dump(),
        "request_metadata": [], "qa_status": "pass", "state": "committed",
        "pipeline_version": PIPELINE_VERSION, "audited_ids": [],
    })
    store.write("validation-failures/142.json", {
        "chapter": 142, "paragraph_id": "P0142_0001",
        "final_status": "manual_review_required", "original_text": ORIGINAL,
        "findings": [{"kind": "locked_term_missing", "id": "P0142_0001",
                      "source": "会试", "required": "hội thí"}],
    })
    store.progress("failed", error=(
        "Chapter 142 repair for P0142_0001 already failed; manual review required"
    ), error_detail={"chapter": 149, "paragraph_id": "P0149_0009"})
    return store


class ManualReviewTests(unittest.IsolatedAsyncioTestCase):
    async def _decide(self, store, action, text=None, fingerprint=None):
        context = api._manual_review_context(store)
        self.assertIsNotNone(context)
        decision = api.ManualReviewDecision(
            chapter=context["chapter"], paragraph_id=context["paragraph_id"],
            fingerprint=fingerprint or context["fingerprint"],
            action=action, text=text,
        )
        with patch.object(api, "ROOT", store.path.parent), patch.object(
            api, "start", side_effect=lambda current: api.snapshot(current)
        ):
            return await api.review(store.id, decision)

    async def _run_without_provider(self, store):
        async def unexpected(*args, **kwargs):
            self.fail("A manual review resume should not ask the model again")
        scheduler = Scheduler(concurrency=1, spacing=0, transport=unexpected, keys=[None])
        await Pipeline(store, scheduler).run()

    async def test_accept_is_audited_override_and_resumes_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_store(Path(directory))
            visible = api.snapshot(store)["manual_review"]
            self.assertEqual(visible["current_text"], ORIGINAL)
            self.assertEqual(visible["findings"][0]["kind"], "locked_term_missing")
            await self._decide(store, "accept")
            await self._run_without_provider(store)
            self.assertEqual(store.read("progress.json")["status"], "done")
            self.assertEqual(store.read("chapters/145.json")["qa_status"], "manual_override")
            self.assertEqual(store.read("translated.json")["chapters"][0]["segments"][0]["text"], ORIGINAL)
            self.assertEqual(store.read("manual-reviews/145/P0145_0001.json")["decision"], "accept")
            self.assertFalse((store.path / "pending-chapters/145.json").exists())
            await self._run_without_provider(store)

    async def test_replacement_is_validated_and_normal_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_store(Path(directory))
            await self._decide(store, "replace", REPLACEMENT)
            self.assertEqual(store.read("revisions/145/P0145_0001.json")[-1]["kind"],
                             "manual_replacement")
            await self._run_without_provider(store)
            self.assertEqual(store.read("chapters/145.json")["qa_status"], "pass")
            self.assertEqual(store.read("translated.json")["chapters"][0]["segments"][0]["text"],
                             REPLACEMENT)

    async def test_stale_or_invalid_decision_cannot_change_pending_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_store(Path(directory))
            before = store.read("pending-chapters/145.json")
            with self.assertRaises(HTTPException) as stale:
                await self._decide(store, "accept", fingerprint="0" * 64)
            self.assertEqual(stale.exception.status_code, 409)
            with self.assertRaises(HTTPException) as invalid:
                await self._decide(store, "replace", "Thi hội đã kết thúc")
            self.assertEqual(invalid.exception.status_code, 422)
            with self.assertRaises(HTTPException) as missing_term:
                await self._decide(store, "replace", "Thi hội đã xong.")
            self.assertEqual(missing_term.exception.status_code, 422)
            self.assertEqual(store.read("pending-chapters/145.json"), before)
            self.assertIsNone(store.read("manual-reviews/145/P0145_0001.json"))

    async def test_changed_text_invalidates_an_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_store(Path(directory))
            await self._decide(store, "accept")
            pending = store.read("pending-chapters/145.json")
            pending["translation"]["segments"][0]["text"] = "Thi hội chấm dứt."
            store.write("pending-chapters/145.json", pending)
            chapter = parse_chapters(RAW)[0]
            result = Translation.model_validate(pending["translation"])
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0, keys=[None]))
            pipeline.dictionary = DICTIONARY
            self.assertTrue(pipeline._local_findings(chapter, result))

    async def test_semantic_repair_failure_without_local_findings_is_reviewable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_store(Path(directory))
            pending = store.read("pending-chapters/145.json")
            pending["translation"]["segments"][0]["text"] = REPLACEMENT
            store.write("pending-chapters/145.json", pending)
            context = api._manual_review_context(store)
            self.assertEqual(context["findings"][0]["kind"], "manual_repair_failed")
            await self._decide(store, "accept")
            await self._run_without_provider(store)
            self.assertEqual(store.read("chapters/145.json")["qa_status"], "manual_override")

    async def test_post_dictionary_committed_chapter_accept_is_reviewable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_committed_store(Path(directory))
            visible = api.snapshot(store)
            self.assertEqual(visible["manual_review"]["paragraph_id"], "P0142_0001")
            self.assertEqual(visible["error_detail"]["paragraph_id"], "P0142_0001")
            self.assertFalse((store.path / "pending-chapters/142.json").exists())
            await self._decide(store, "accept")
            await self._run_without_provider(store)
            self.assertEqual(store.read("chapters/142.json")["qa_status"], "manual_override")
            self.assertEqual(store.read("translated.json")["chapters"][0]["segments"][0]["text"],
                             ORIGINAL)

    async def test_post_dictionary_committed_chapter_replace_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = failed_committed_store(Path(directory))
            await self._decide(store, "replace", REPLACEMENT)
            self.assertEqual(store.read("chapters/142.json")["translation"]["segments"][0]["text"],
                             REPLACEMENT)
            self.assertFalse((store.path / "pending-chapters/142.json").exists())
            await self._run_without_provider(store)
            self.assertEqual(store.read("chapters/142.json")["qa_status"], "pass")
