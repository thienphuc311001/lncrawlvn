import json
import tempfile
import unittest
from pathlib import Path

from lncrawl.translation.dictionary import (
    evaluate_reference_evidence,
    export_dictionary,
    load_legacy,
    terminology_findings,
    terminology_occurrences,
)
from lncrawl.translation.models import Inputs, Resolution, Segment, Term, Translation
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline
from lncrawl.translation.preprocessing import build_index, local_resolution
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store, digest
from lncrawl.translation.validation import local_findings


class TerminologyPipelineRegressionTests(unittest.TestCase):
    def test_reference_confirmation_requires_raw_evidence_and_rejects_structural_noise(self):
        confirmed = evaluate_reference_evidence(
            "张侍班",
            "张四维",
            [
                "张四维任东宫侍班。",
                "张侍班随后说道。",
                "张侍班再次出现。",
            ],
        )
        self.assertTrue(confirmed["confirmed"])
        self.assertIn("INDIRECT_ROLE_CONTINUITY", confirmed["evidence_types"])
        self.assertIn("INDIRECT_REPEATED_CONTEXT", confirmed["evidence_types"])

        weak = evaluate_reference_evidence("张侍班", "张四维", ["张侍班说道。"])
        self.assertFalse(weak["confirmed"])
        self.assertEqual(weak["reason"], "insufficient_identity_evidence")

        competing = evaluate_reference_evidence(
            "张侍班",
            "张四维",
            ["张四维任东宫侍班。", "张侍班说道。", "张宏也在场。"],
            competing_identities=["张宏"],
        )
        self.assertFalse(competing["confirmed"])
        self.assertEqual(competing["reason"], "competing_identity")

        truncated = evaluate_reference_evidence(
            "大司徒王国",
            "王国光",
            ["大司徒王国光入殿。"],
        )
        self.assertFalse(truncated["confirmed"])
        self.assertEqual(truncated["reason"], "truncated_identity")

        residue = evaluate_reference_evidence("邱途来", "邱途", ["邱途来了。"])
        self.assertFalse(residue["confirmed"])
        self.assertEqual(residue["reason"], "contextual_residue")

    def test_historical_forms_are_owned_by_canonical_identities_once(self):
        entries = [
            ("冯保", "Phùng Bảo", "冯大伴", "Phùng Đại bạn"),
            ("张宏", "Trương Hoành", "张大伴", "Trương Đại bạn"),
            ("张翰", "Trương Hãn", "张尚书", "Trương Thượng thư"),
            ("李成梁", "Lý Thành Lương", "李帅", "Lý soái"),
            ("赵梦祐", "Triệu Mộng Hựu", "赵缇帅", "Triệu Đề soái"),
        ]
        records = []
        for canonical, translation, form, form_translation in entries:
            records.append(
                {
                    "source": canonical,
                    "translation": translation,
                    "type": "character",
                    "status": "locked",
                    "forms": {form: form_translation},
                }
            )
        # Reproduce the resolver duplication that previously caused 李帅 to be
        # reported twice: the same key arrives as both alias and form.
        records[-2]["aliases"] = ["李帅"]
        terms, problems = load_legacy({"entries": records})
        by_source = {term.source: term for term in terms}
        self.assertEqual(by_source["李成梁"].aliases, [])
        self.assertEqual(by_source["李成梁"].forms, {"李帅": "Lý soái"})
        occurrences = terminology_occurrences(
            [by_source["李成梁"].model_dump()], "李帅。"
        )
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(
            [item for item in problems if item["classification"] == "form_cleanup"], []
        )

    def test_title_prefix_migrates_full_identity_and_quarantines_partial_identity(self):
        migrated, problems = load_legacy(
            {
                "entries": [
                    {
                        "source": "大司徒王国",
                        "translation": "Vương Quốc",
                        "type": "character",
                        "status": "locked",
                        "forms": {"大司徒王国光": "Đại Tư đồ Vương Quốc Quang"},
                    },
                    {
                        "source": "王国光",
                        "translation": "Vương Quốc Quang",
                        "type": "character",
                        "status": "locked",
                    },
                ]
            }
        )
        self.assertEqual([term.source for term in migrated], ["王国光"])
        self.assertEqual(
            migrated[0].forms, {"大司徒王国光": "Đại Tư đồ Vương Quốc Quang"}
        )
        self.assertTrue(
            any(item["classification"] == "canonical_cleanup" for item in problems)
        )

        partial, _ = load_legacy(
            {
                "entries": [
                    {
                        "source": "大司徒王国",
                        "translation": "Vương Quốc",
                        "type": "character",
                        "status": "locked",
                    }
                ]
            }
        )
        self.assertEqual(partial[0].source, "大司徒王国")
        self.assertFalse(partial[0].enforceable)

    def test侍班_requires_raw_identity_evidence(self):
        without_identity = validate_inputs(
            "第1章\n张侍班来了。",
            "Chương 1\nTrương Thị ban đến.",
        )
        index = build_index(
            without_identity,
            {raw.key: deterministic_alignment(raw, vp) for raw, vp in without_identity},
            [],
        )
        self.assertNotIn("张侍班", index["candidates"])
        self.assertEqual(index["report_only"]["张侍班"]["status"], "report_only")

        with_identity = validate_inputs(
            "第1章\n张侍班就是张四维。",
            "Chương 1\nTrương Thị ban là Trương Tứ Duy.",
        )
        proven = build_index(
            with_identity,
            {raw.key: deterministic_alignment(raw, vp) for raw, vp in with_identity},
            [],
        )
        self.assertIn("张侍班", proven["candidates"])

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
        self.assertEqual(by_source["黄哥"]["reason"], "confirmed_mapping_mismatch")
        # Presence of the confirmed target is sufficient; a shorter source key
        # is not counted inside the longer source key.
        self.assertNotIn("龙山", by_source)
        self.assertNotIn("龙山道", by_source)

    def test_canonical_span_never_absorbs_adjacent_raw_text(self):
        term = Term(
            source="邱途",
            translation="Khâu Đồ",
            type="character",
            status="locked",
        )
        raw = "邱途接了电话"
        occurrences = terminology_occurrences([term.model_dump()], raw)
        self.assertEqual(len(occurrences), 1)
        occurrence = occurrences[0]
        self.assertEqual(occurrence["matched_source"], "邱途")
        self.assertEqual(raw[occurrence["start"] : occurrence["end"]], "邱途")
        self.assertEqual(occurrence["right_context"], "接了电话")
        self.assertNotIn("接", occurrence["matched_source"])

        findings = terminology_findings(
            [term.model_dump()], raw, "Đợi Khâu nhận điện thoại。"
        )
        self.assertEqual(findings[0]["source"], "邱途")
        self.assertEqual(findings[0]["canonical_source"], "邱途")
        self.assertEqual(findings[0]["required_translation"], "Khâu Đồ")
        self.assertEqual(findings[0]["start"], 0)
        self.assertEqual(findings[0]["end"], 2)
        self.assertEqual(
            [raw[item["start"] : item["end"]] for item in findings[0]["source_spans"]],
            ["邱途"],
        )
        self.assertNotIn("接", findings[0]["source"])
        self.assertNotIn("接", findings[0]["required_translation"])

    def test_alias_punctuation_ascii_and_overlapping_keys_keep_exact_offsets(self):
        alias = Term(
            source="丁小七",
            translation="Đinh Tiểu Thất",
            aliases=["小七"],
            type="character",
            status="locked",
        )
        occurrences = terminology_occurrences(
            [alias.model_dump()], "“小七说道42A"
        )
        self.assertEqual(occurrences[0]["source"], "小七")
        self.assertEqual(occurrences[0]["canonical_source"], "丁小七")
        self.assertEqual(occurrences[0]["matched_source"], "小七")
        self.assertEqual(occurrences[0]["right_context"], "说道42A")
        self.assertEqual(
            "“小七说道42A"[occurrences[0]["start"] : occurrences[0]["end"]],
            "小七",
        )

        short = Term(
            source="龙山",
            translation="Long Sơn",
            type="location",
            status="locked",
        )
        long = Term(
            source="龙山道",
            translation="Long Sơn Đạo",
            type="location",
            status="locked",
        )
        overlap = terminology_occurrences(
            [short.model_dump(), long.model_dump()], "龙山道人来了"
        )
        self.assertEqual([item["matched_source"] for item in overlap], ["龙山道"])
        self.assertEqual(overlap[0]["right_context"], "人来了")
        self.assertEqual("龙山道人来了"[overlap[0]["start"] : overlap[0]["end"]], "龙山道")

    def test_fake_serialized_span_metadata_cannot_change_the_source(self):
        record = Term(
            source="邱途",
            translation="Khâu Đồ",
            type="character",
            status="locked",
        ).model_dump()
        record.update(start=14, end=28, context_span="邱途接了电话")
        occurrence = terminology_occurrences([record], "邱途接了电话")
        self.assertEqual(occurrence[0]["matched_source"], "邱途")
        self.assertEqual(occurrence[0]["start"], 0)
        self.assertEqual(occurrence[0]["end"], 2)

    def test_timestamp_like_raw_text_is_not_a_fatal_terminology_candidate(self):
        pairs = validate_inputs(
            "第1章\n时间是23：59：59。",
            "Chương 1\nThời gian là 23：59：59.",
        )
        index = build_index(
            pairs, {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs}, []
        )
        self.assertNotIn("23：59：59", index["candidates"])
        self.assertNotIn("23：59：59", index["report_only"])

    def test_legacy_cleanup_removes_contextual_forms_but_preserves_titles(self):
        terms, problems = load_legacy(
            {
                "entries": [
                    {
                        "source": "邱途",
                        "translation": "Khâu Đồ",
                        "type": "character",
                        "status": "locked",
                        "aliases": ["邱副科长", "邱探员", "邱科长"],
                        "forms": {
                            "邱途来": "Khâu Đồ đến",
                            "邱途接": "Khâu Đồ tiếp",
                            "邱途笑": "Khâu Đồ cười",
                            "邱副科长": "Khâu phó khoa trưởng",
                            "邱探员": "Khâu thám viên",
                            "邱科长": "Khâu khoa trưởng",
                        },
                    }
                ]
            }
        )
        self.assertEqual(len(terms), 1)
        term = terms[0]
        self.assertEqual(set(term.forms), {"邱副科长", "邱探员", "邱科长"})
        # A rendered form owns the key; the duplicate bare aliases are removed.
        self.assertEqual(set(term.aliases), set())
        cleanup = [item for item in problems if item["classification"] == "form_cleanup"]
        self.assertEqual(
            {item["source"] for item in cleanup[0]["removed_forms"]},
            {"邱途来", "邱途接", "邱途笑"},
        )
        self.assertEqual(
            set(cleanup[0]["preserved_forms"]),
            {"邱副科长", "邱探员", "邱科长"},
        )
        self.assertNotIn("邱途接", json.dumps(export_dictionary({"邱途": term}), ensure_ascii=False))

    def test_resolver_forms_require_identity_shape_or_raw_identity_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n邱途来了。\n邱科长就是邱途。",
                    vietphrase="Chương 1\nKhâu Đồ đến.\nKhoa trưởng Khâu là Khâu Đồ.",
                ).model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            raw, vp = pipeline.pairs[0]
            pipeline.alignments[raw.key] = deterministic_alignment(raw, vp)
            result = Resolution.model_validate(
                {
                    "decision": "ACCEPT",
                    "eligibility": {
                        "complete_semantic_unit": True,
                        "named_or_novel_specific": True,
                        "consistency_matters": True,
                        "evidence_supports": True,
                    },
                    "term": {
                        "source": "邱途",
                        "translation": "Khâu Đồ",
                        "type": "character",
                        "aliases": ["邱途来", "邱科长"],
                        "forms": {
                            "邱途接": "Khâu Đồ tiếp",
                            "邱科长": "Khâu khoa trưởng",
                        },
                    },
                    "reason": "fixture",
                }
            )
            term = pipeline.apply_resolution("邱途", None, result)
            self.assertEqual(term.aliases, [])
            self.assertEqual(term.forms, {"邱科长": "Khâu khoa trưởng"})
            self.assertEqual(
                {item["source"] for item in pipeline.form_cleanup[0]["removed_forms"]},
                {"邱途来", "邱途接"},
            )

    def test_candidate_discovery_does_not_turn_fixed_length_name_extensions_into_terms(self):
        pairs = validate_inputs(
            "第1章\n邱途来走了。",
            "Chương 1\nKhâu Đồ đi rồi.",
        )
        index = build_index(
            pairs, {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs}, []
        )
        self.assertNotIn("邱途来", index["report_only"])
        self.assertNotIn("邱途来", index["candidates"])

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
    async def test_contaminated_frozen_dictionary_is_cleaned_and_completed_chapter_reused(self):
        with tempfile.TemporaryDirectory() as root:
            old_dictionary = {
                "version": 3,
                "entries": [
                    {
                        "source": "邱途",
                        "translation": "Khâu Đồ",
                        "type": "character",
                        "status": "locked",
                        "aliases": ["邱副科长", "邱探员", "邱科长"],
                        "forms": {
                            "邱途来": "Khâu Đồ đến",
                            "邱途接": "Khâu Đồ tiếp",
                            "邱途笑": "Khâu Đồ cười",
                            "邱科长": "Khâu khoa trưởng",
                        },
                    }
                ],
            }
            inputs = Inputs(
                raw="第1章\n邱途来了。",
                vietphrase="Chương 1\nKhâu Đồ đến.",
                dictionary=old_dictionary,
            ).model_dump()
            store = Store(Path(root), inputs)
            old_hash = digest(old_dictionary)
            store.write(
                "frozen-dictionary.json",
                {
                    "input_hash": "legacy-input-hash",
                    "dictionary_hash": old_hash,
                    "dictionary": old_dictionary,
                },
            )
            store.write(
                "chapters/1.json",
                {
                    "dictionary_hash": old_hash,
                    "pipeline_input_hash": "legacy-input-hash",
                    "number": 1,
                    "translation": {
                        "title": "Chương 1",
                        "segments": [{"id": 0, "text": "Khâu Đồ đến."}],
                    },
                },
            )

            async def must_not_translate(model, body):
                raise AssertionError("a valid completed chapter should be reused")

            await Pipeline(
                store,
                Scheduler(transport=must_not_translate, concurrency=1, spacing=0),
            ).run()
            cleaned = store.read("dictionary.json")
            entry = cleaned["entries"][0]
            self.assertNotIn("邱途来", json.dumps(cleaned["entries"], ensure_ascii=False))
            self.assertEqual(set(entry["forms"]), {"邱科长"})
            self.assertNotEqual(cleaned["dictionary_hash"], old_hash)
            self.assertEqual(
                store.read("chapters/1.json")["dictionary_hash"],
                cleaned["dictionary_hash"],
            )
            summary = store.read("dictionary-resolution-report.json")["summary"]
            self.assertEqual(summary["removed_contextual_forms"], 3)
            self.assertEqual(summary["preserved_identity_forms"], 3)

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
