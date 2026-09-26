"""Reader translation uses durable jobs without changing the saved source chapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from lncrawl import book_translation, server
from lncrawl.exceptions import LNException
from lncrawl.library import Library
from lncrawl.translation import api
from lncrawl.translation.models import DICTIONARY_VERSION, Translation, TranslatedSegment
from lncrawl.translation.parsing import parse_chapters
from lncrawl.translation.store import Store, digest


HELLO = {
    "source": "你好", "translation": "xin chào", "type": "term",
    "gender": "not_applicable", "status": "locked", "aliases": [],
}
OTHER = {
    "source": "世界", "translation": "thế giới", "type": "term",
    "gender": "not_applicable", "status": "locked", "aliases": [],
}
BODY = "<p>你好。</p><p>第二段。</p>"


def dictionary(*entries):
    return {"version": DICTIONARY_VERSION, "entries": list(entries),
            "unresolved": [], "rejected": []}


class BookReaderTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.library = Library(root=root / "library")
        self.book_id = self.library.save_book_meta({
            "title": "Truyện mẫu", "toc": [
                {"id": 1, "title": "开端", "url": "https://example.test/1"},
                {"id": 2, "title": "继续", "url": "https://example.test/2"},
            ],
        })
        self.library.save_chapter(self.book_id, {"id": 1, "title": "开端", "body": BODY})
        self.library.save_chapter(self.book_id, {"id": 2, "title": "继续", "body": "<p>你好，世界。</p>"})
        self.root = root / "jobs"
        self.created = []
        self.provider_active = True
        for target, name, value in (
            (book_translation, "LIBRARY", self.library),
            (server, "LIBRARY", self.library),
            (api, "ROOT", self.root),
            (api, "create", self.fake_create),
            (api, "start", self.fake_start),
            (Store, "is_active", lambda store: self.provider_active and
             store.read("progress.json", {}).get("status") in ("pending", "running")),
        ):
            patched = patch.object(target, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.client = AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    def fake_start(self, store):
        store.progress("running", stage="Translating", error=None)
        return api.snapshot(store)

    async def fake_create(self, inputs):
        self.created.append(inputs.model_dump())
        store = Store(self.root, inputs=inputs.model_dump())
        store.write("parser-version.json", {"version": api.PARSER_VERSION})
        return self.fake_start(store)

    def url(self, chapter=1, action="translation"):
        return f"/api/books/{self.book_id}/chapters/{chapter}/{action}"

    def store(self, job_id):
        return api.get_store(job_id)

    async def start(self, chapter=1, **payload):
        response = await self.client.post(self.url(chapter, "translate"), json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def finish(self, job_id, chapter, *, text="Xin chào.", updated=None):
        store = self.store(job_id)
        store.write("translated.json", {"chapters": [{
            "number": chapter, "title": "Bắt đầu", "paragraphs": [text],
        }]})
        store.write("dictionary.json", updated if updated is not None else
                    store.read("inputs.json")["dictionary"])
        store.progress("done", stage="Complete")

    async def test_import_normalizes_replaces_and_rejects_conflicting_glossaries(self):
        url = f"/api/books/{self.book_id}/dictionary"
        empty = await self.client.get(url)
        self.assertEqual(empty.json(), {"entry_count": 0, "imported": False,
                                        "active_chapter_id": None})
        imported = await self.client.post(url, json={"dictionary": [HELLO]})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["entry_count"], 1)
        saved = self.library.load_translation_state(self.book_id)
        self.assertEqual(saved["dictionary"], dictionary(HELLO))
        self.assertTrue(saved["imported"])
        self.assertNotIn("dictionary", self.library._read_meta(self.book_id))

        for invalid in ({"version": DICTIONARY_VERSION, "entries": [HELLO, HELLO]},
                        {"version": DICTIONARY_VERSION, "entries": [
                            {**HELLO, "aliases": [{"source": "世界", "translation": "chào"}]}, OTHER,
                        ]},
                        {"entries": "not an array"},
                        {"version": DICTIONARY_VERSION + 1, "entries": []}):
            rejected = await self.client.post(url, json={"dictionary": invalid})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(self.library.load_translation_state(self.book_id)["dictionary"],
                             dictionary(HELLO))
        self.assertEqual((await self.client.post(url, json={"dictionary": {"entries": [OTHER]}})).json(),
                         {"entry_count": 1})
        self.assertEqual(self.library.load_translation_state(self.book_id)["dictionary"],
                         dictionary(OTHER))
        self.assertEqual((await self.client.get(url)).json()["entry_count"], 1)
        missing = await self.client.get("/api/books/not-in-library/dictionary")
        self.assertEqual(missing.status_code, 404)

    async def test_html_becomes_one_numbered_job_and_active_book_is_exclusive(self):
        await self.client.post(f"/api/books/{self.book_id}/dictionary",
                               json={"dictionary": dictionary(HELLO)})
        before = await self.client.get(f"/api/books/{self.book_id}/chapters/1")
        self.assertEqual(before.json()["body"], BODY)
        self.assertEqual((await self.client.get(self.url())).json()["status"], "none")
        started = await self.start()
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["active_chapter_id"], 1)
        self.assertEqual(started["entry_count"], 1)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["raw"], "第1章 开端\n你好。\n第二段。")
        self.assertEqual(len(parse_chapters(self.created[0]["raw"])), 1)
        self.assertEqual(self.created[0]["dictionary"], dictionary(HELLO))
        self.assertEqual(self.created[0]["book_title"], "Truyện mẫu")
        self.assertEqual((await self.start())["job"]["job_id"], started["job"]["job_id"])
        self.assertEqual(len(self.created), 1)
        self.assertEqual((await self.client.post(self.url(2, "translate"), json={})).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/books/{self.book_id}/dictionary",
                                                 json={"dictionary": dictionary(OTHER)})).status_code, 409)
        self.assertEqual((await self.client.get(self.url(2))).json()["active_chapter_id"], 1)
        self.assertEqual((await self.client.get(f"/api/books/{self.book_id}/chapters/1")).json()["body"], BODY)

    async def test_duplicate_saved_heading_is_not_a_second_chapter(self):
        book_id = self.library.save_book_meta({
            "title": "Chương lặp", "toc": [{"id": 56, "title": "开篇"}],
        })
        body = "<h2>第56章 开篇</h2><p>你好。</p><p>第五十六章 标题 他走来了。</p><p>第二段。</p>"
        self.library.save_chapter(book_id, {"id": 56, "title": "开篇", "body": body})
        path = f"/api/books/{book_id}/chapters/56"
        response = await self.client.post(path + "/translate", json={})
        self.assertEqual(response.status_code, 202, response.text)
        raw = self.created[-1]["raw"]
        self.assertEqual(raw, "第56章 开篇\n你好。\n他走来了。\n第二段。")
        self.assertEqual([chapter.number for chapter in parse_chapters(raw)], [56])
        self.assertEqual((await self.client.get(path)).json()["body"], body)
        self.store(response.json()["job"]["job_id"]).progress("cancelled")
        await self.client.get(path + "/translation")

        self.library.save_chapter(book_id, {"id": 56, "title": "开篇",
                                            "body": "<p>你好。</p><p>第57章 错误。</p>"}, overwrite=True)
        rejected = await self.client.post(path + "/translate", json={"refresh": True})
        self.assertEqual(rejected.status_code, 422)

    async def test_translated_toc_title_uses_original_heading_from_saved_body(self):
        book_id = self.library.save_book_meta({
            "title": "Truyện có mục lục dịch", "toc": [{"id": 56, "title": "Chương 56: Gặp gỡ"}],
        })
        body = "<p>第56章 考虑考虑</p><p>你好。</p>"
        self.library.save_chapter(book_id, {"id": 56, "title": "Chương 56: Gặp gỡ", "body": body})
        path = f"/api/books/{book_id}/chapters/56"
        result = await self.client.post(path + "/translate", json={})
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.created[-1]["raw"], "第56章 考虑考虑\n你好。")
        self.assertEqual(parse_chapters(self.created[-1]["raw"])[0].title, "考虑考虑")
        self.assertEqual((await self.client.get(path)).json()["title"], "Chương 56: Gặp gỡ")

        self.store(result.json()["job"]["job_id"]).progress("cancelled")
        await self.client.get(path + "/translation")
        self.library.save_chapter(book_id, {"id": 56, "title": "Chương 56: Gặp gỡ",
                                            "body": "<p>你好。</p>"}, overwrite=True)
        fallback = await self.client.post(path + "/translate", json={"refresh": True})
        self.assertEqual(fallback.status_code, 202, fallback.text)
        self.assertEqual(self.created[-1]["raw"], "第56章 Gặp gỡ\n你好。")



    async def test_done_persists_translation_and_carries_new_terms_to_next_chapter(self):
        await self.client.post(f"/api/books/{self.book_id}/dictionary",
                               json={"dictionary": dictionary(HELLO)})
        first = await self.start()
        job_id = first["job"]["job_id"]
        self.finish(job_id, 1, text="Chào bạn.", updated=dictionary(HELLO, OTHER))
        done = await self.client.get(self.url())
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "done")
        self.assertEqual(done.json()["translated"], {"title": "Bắt đầu",
                                                       "paragraphs": ["Chào bạn."]})
        self.assertFalse(done.json()["stale"])
        self.assertEqual(done.json()["entry_count"], 2)
        self.assertIsNone(done.json()["active_chapter_id"])
        self.assertEqual(self.library.load_translation_state(self.book_id)["dictionary"],
                         dictionary(HELLO, OTHER))
        self.assertEqual((await self.client.get(f"/api/books/{self.book_id}/chapters/1")).json()["body"], BODY)
        self.assertEqual((await self.start())["status"], "done")
        self.assertEqual(len(self.created), 1)
        second = await self.start(2)
        self.assertEqual(second["status"], "running")
        self.assertEqual(self.created[-1]["dictionary"], dictionary(HELLO, OTHER))

    async def test_done_job_reconciles_on_dictionary_route_after_reloading_library(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1, updated=dictionary(HELLO))
        reloaded = Library(root=self.library.root)
        with patch.object(book_translation, "LIBRARY", reloaded):
            result = await self.client.get(f"/api/books/{self.book_id}/dictionary")
            self.assertEqual(result.json()["entry_count"], 1)
            self.assertIsNone(result.json()["active_chapter_id"])
            self.assertEqual(reloaded.load_translated_chapter(self.book_id, 1)["paragraphs"],
                             ["Xin chào."])
            self.assertIsNone(reloaded.load_translation_state(self.book_id)["active"])
        self.assertEqual((await self.client.get(self.url())).json()["status"], "done")

    async def test_retry_after_state_write_failure_finalizes_same_job_once(self):
        first = await self.start()
        job_id = first["job"]["job_id"]
        self.finish(job_id, 1, updated=dictionary(HELLO))
        original = self.library.save_translation_state
        failed_once = False

        def interrupt_commit(book_id, state):
            nonlocal failed_once
            if state.get("active") is None and not failed_once:
                failed_once = True
                raise LNException("Interrupted between translated chapter and dictionary state")
            return original(book_id, state)

        with patch.object(self.library, "save_translation_state", side_effect=interrupt_commit):
            failed = await self.client.get(self.url())
            self.assertEqual(failed.status_code, 500, failed.text)
        self.assertTrue(failed_once)
        self.assertEqual(self.library.load_translated_chapter(self.book_id, 1)["paragraphs"],
                         ["Xin chào."])
        self.assertEqual(self.library.load_translation_state(self.book_id)["active"]["job_id"], job_id)
        recovered = await self.client.get(self.url())
        self.assertEqual(recovered.json()["status"], "done")
        self.assertEqual(recovered.json()["entry_count"], 1)
        self.assertIsNone(self.library.load_translation_state(self.book_id)["active"])
        self.assertEqual((await self.start(2))["status"], "running")
        self.assertEqual(self.created[-1]["dictionary"], dictionary(HELLO))

    async def test_relevant_terms_and_raw_changes_are_stale_but_unrelated_terms_are_not(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1, updated=dictionary(HELLO))
        await self.client.get(self.url())
        url = f"/api/books/{self.book_id}/dictionary"
        await self.client.post(url, json={"dictionary": dictionary(HELLO, OTHER)})
        self.assertFalse((await self.client.get(self.url())).json()["stale"])
        changed = {**HELLO, "translation": "lời chào khác"}
        await self.client.post(url, json={"dictionary": dictionary(changed, OTHER)})
        stale = (await self.client.get(self.url())).json()
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["translated"]["paragraphs"], ["Xin chào."])
        await self.client.post(url, json={"dictionary": dictionary(HELLO)})
        self.assertEqual((await self.client.get(self.url())).json()["status"], "done")
        self.library.save_chapter(self.book_id, {"id": 1, "title": "开端",
                                                      "body": "<p>你好，新的句子。</p>"}, overwrite=True)
        self.assertTrue((await self.client.get(self.url())).json()["stale"])
        self.assertEqual((await self.client.get(f"/api/books/{self.book_id}/chapters/1")).json()["body"],
                         "<p>你好，新的句子。</p>")

    async def test_refresh_uses_new_job_and_keeps_old_translation_until_success(self):
        first = await self.start()
        old_job = first["job"]["job_id"]
        self.finish(old_job, 1, text="Bản trước")
        await self.client.get(self.url())
        refreshing = await self.start(refresh=True)
        self.assertNotEqual(refreshing["job"]["job_id"], old_job)
        self.assertNotEqual(self.created[0]["source_name"], self.created[1]["source_name"])
        self.assertEqual(refreshing["translated"]["paragraphs"], ["Bản trước"])
        self.assertEqual(refreshing["status"], "running")
        self.finish(refreshing["job"]["job_id"], 1, text="Bản mới")
        self.assertEqual((await self.client.get(self.url())).json()["translated"]["paragraphs"],
                         ["Bản mới"])
        self.assertEqual((await self.client.get(f"/api/books/{self.book_id}/chapters/1")).json()["body"], BODY)

    async def test_cancelled_refresh_preserves_saved_translation(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1, text="Bản cũ")
        await self.client.get(self.url())
        refresh = await self.start(refresh=True)
        self.provider_active = False
        cancelled = await self.client.post(
            f"/api/translation/jobs/{refresh['job']['job_id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        old = (await self.client.get(self.url())).json()
        self.assertEqual(old["status"], "done")
        self.assertIsNone(old["active_chapter_id"])
        self.assertEqual(old["translated"]["paragraphs"], ["Bản cũ"])
        self.provider_active = True
        self.assertEqual((await self.start(2))["status"], "running")

    async def test_interrupted_failed_resume_and_cancel_release_only_after_cancel(self):
        first = await self.start()
        job_id = first["job"]["job_id"]
        self.provider_active = False
        interrupted = (await self.client.get(self.url())).json()
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["active_chapter_id"], 1)
        self.provider_active = True
        resumed = await self.client.post(f"/api/translation/jobs/{job_id}/resume")
        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual((await self.client.get(self.url())).json()["status"], "running")
        self.store(job_id).progress("failed", error="Provider refused request")
        with patch.object(book_translation, "LIBRARY", Library(root=self.library.root)):
            failed = (await self.client.get(self.url())).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["job"]["error"], "Provider refused request")
        self.assertEqual((await self.client.post(self.url(2, "translate"), json={})).status_code, 409)
        self.assertEqual((await self.client.get(f"/api/books/{self.book_id}/dictionary")).json()[
            "active_chapter_id"], 1)
        cancelled = await self.client.post(f"/api/translation/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        with patch.object(book_translation, "LIBRARY", Library(root=self.library.root)):
            after = (await self.client.get(self.url())).json()
        self.assertEqual(after["status"], "none")
        self.assertIsNone(after["active_chapter_id"])
        self.assertEqual((await self.start(2))["status"], "running")

    async def test_manual_review_snapshot_and_decision_keep_job_bound_to_book(self):
        first = await self.start()
        store = self.store(first["job"]["job_id"])
        inputs = store.read("inputs.json")
        raw = inputs["raw"]
        chapter = parse_chapters(raw)[0]
        paragraph_id = chapter.paragraph_ids[0]
        canonical = inputs["dictionary"]
        store.write("pre-dictionary.json", {"dictionary": canonical})
        store.write("post-dictionary.json", {"dictionary": canonical})
        store.write("pending-chapters/1.json", {
            "raw_hash": digest((chapter.title, chapter.paragraphs)),
            "pre_dictionary_hash": digest({"version": DICTIONARY_VERSION, "entries": []}),
            "state": "generated_unvalidated",
            "translation": Translation(title="Bắt đầu", segments=[
                TranslatedSegment(id=identifier, text="Bản nháp")
                for identifier in chapter.paragraph_ids
            ]).model_dump(), "request_metadata": [],
        })
        store.progress("failed", error=f"Chapter 1 repair for {paragraph_id} already failed; manual review required")
        with patch.object(book_translation, "LIBRARY", Library(root=self.library.root)):
            status = (await self.client.get(self.url())).json()
        review = status["job"]["manual_review"]
        self.assertEqual(status["status"], "failed")
        self.assertEqual(review["raw"], "你好。")
        self.assertEqual(review["current_text"], "Bản nháp")
        self.assertTrue(review["findings"])
        self.assertEqual(status["active_chapter_id"], 1)
        decision = {"chapter": review["chapter"], "paragraph_id": review["paragraph_id"],
                    "fingerprint": review["fingerprint"], "action": "accept"}
        invalid = await self.client.post(f"/api/translation/jobs/{store.id}/review",
                                         json={**decision, "fingerprint": "0" * 64})
        self.assertEqual(invalid.status_code, 409)
        accepted = await self.client.post(f"/api/translation/jobs/{store.id}/review", json=decision)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["status"], "running")
        self.assertEqual((await self.client.get(self.url())).json()["active_chapter_id"], 1)
        self.assertEqual(len(self.created), 1)

    async def test_invalid_or_missing_outputs_keep_active_and_preserve_prior_translation(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1, text="Bản trước", updated=dictionary(HELLO))
        await self.client.get(self.url())
        valid_output = {"chapters": [{"number": 1, "title": "Đúng", "paragraphs": ["Y"]}]}
        wrong_chapter = {"chapters": [{"number": 2, "title": "Sai", "paragraphs": ["X"]}]}
        for invalid_output, invalid_dictionary in (
            (None, dictionary(OTHER)),
            (wrong_chapter, dictionary(OTHER)),
            (valid_output, None),
            (valid_output, {"version": DICTIONARY_VERSION + 1, "entries": []}),
        ):
            ongoing = await self.start(refresh=True)
            store = self.store(ongoing["job"]["job_id"])
            if invalid_output is not None:
                store.write("translated.json", invalid_output)
            if invalid_dictionary is not None:
                store.write("dictionary.json", invalid_dictionary)
            store.progress("done")
            response = await self.client.get(self.url())
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(self.library.load_translation_state(self.book_id)["active"]["job_id"], store.id)
            self.assertEqual(self.library.load_translated_chapter(self.book_id, 1)["paragraphs"], ["Bản trước"])
            self.assertEqual(self.library.load_translation_state(self.book_id)["dictionary"], dictionary(HELLO))
            retry = await self.start(refresh=True)
            self.assertNotEqual(retry["job"]["job_id"], store.id)
            self.assertEqual(self.created[-1]["dictionary"], dictionary(HELLO))
            self.store(retry["job"]["job_id"]).progress("cancelled")
            await self.client.get(self.url())

    async def test_changed_source_during_job_does_not_commit_dictionary_or_translation(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1, updated=dictionary(HELLO))
        await self.client.get(self.url())
        refresh = await self.start(refresh=True)
        self.library.save_chapter(self.book_id, {"id": 1, "title": "开端", "body": "<p>新内容。</p>"},
                                  overwrite=True)
        self.finish(refresh["job"]["job_id"], 1, text="Bản từ RAW cũ", updated=dictionary(HELLO, OTHER))
        response = (await self.client.get(self.url())).json()
        self.assertEqual(response["status"], "failed")
        self.assertIn("RAW changed", response["error"])
        self.assertIsNone(response["active_chapter_id"])
        self.assertEqual(response["translated"]["paragraphs"], ["Xin chào."])
        self.assertTrue(response["stale"])
        self.assertEqual(self.library.load_translation_state(self.book_id)["dictionary"], dictionary(HELLO))
        self.assertEqual((await self.start(2))["status"], "running")

    async def test_deleted_job_releases_book_and_deleted_chapter_removes_translation(self):
        first = await self.start()
        self.finish(first["job"]["job_id"], 1)
        await self.client.get(self.url())
        self.assertIsNotNone(self.library.load_translated_chapter(self.book_id, 1))
        deleted = await self.client.delete(f"/api/books/{self.book_id}/chapters/1")
        self.assertEqual(deleted.status_code, 204)
        self.assertIsNone(self.library.load_translated_chapter(self.book_id, 1))
        self.assertEqual((await self.client.get(self.url())).status_code, 404)
        running = await self.start(2)
        job_id = running["job"]["job_id"]
        removed = await self.client.delete(f"/api/translation/jobs/{job_id}")
        self.assertEqual(removed.status_code, 200, removed.text)
        status = (await self.client.get(self.url(2))).json()
        self.assertEqual(status["status"], "failed")
        self.assertIn("no longer exists", status["error"])
        self.assertIsNone(status["active_chapter_id"])
        self.assertEqual((await self.start(2))["status"], "running")

    async def test_empty_and_multi_chapter_html_are_rejected_before_job_creation(self):
        for html in ("<p></p>", "<p>你好。</p><p>第2章 错误</p><p>世界。</p>"):
            self.library.save_chapter(self.book_id, {"id": 1, "title": "开端", "body": html},
                                      overwrite=True)
            response = await self.client.post(self.url(1, "translate"), json={})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.created, [])
        self.assertIsNone(self.library.load_translation_state(self.book_id))


if __name__ == "__main__":
    unittest.main()
