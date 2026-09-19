"""Address/title register policy: Sino-Vietnamese by default, locally enforced."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation import style
from lncrawl.translation.dictionary import (
    export_dictionary,
    load_legacy,
    sanity,
    terminology_findings,
)
from lncrawl.translation.models import Inputs, Resolution, Term
from lncrawl.translation.parsing import deterministic_alignment, validate_inputs
from lncrawl.translation.pipeline import Pipeline, QualityError
from lncrawl.translation.preprocessing import build_index
from lncrawl.translation.scheduler import Scheduler
from lncrawl.translation.store import Store, digest


def resolver_result(source, translation, forms, kinds=None):
    return Resolution.model_validate(
        {
            "decision": "ACCEPT",
            "eligibility": {
                "complete_semantic_unit": True,
                "named_or_novel_specific": True,
                "consistency_matters": True,
                "evidence_supports": True,
            },
            "term": {
                "source": source,
                "translation": translation,
                "type": "character",
                "forms": forms,
                **({"form_kinds": kinds} if kinds else {}),
            },
            "reason": "fixture",
        }
    )


class RegisterClassificationTests(unittest.TestCase):
    def test_established_and_modern_renderings_are_distinguished(self):
        for value in ("Đường tỷ", "Khâu khoa trưởng", "Lão Tần", "Tần Tứ gia", "Thự trưởng"):
            self.assertEqual(style.register_of(value), style.SINO_VIETNAMESE, value)
        for value in ("Chị Đường", "Sếp Khâu", "Ông Đường", "Trưởng khoa Khâu"):
            self.assertEqual(style.register_of(value), style.MODERN, value)
        # Unclassified renderings are never rewritten by the policy.
        for value in ("Khâu Đồ", "Tổ Thạch", ""):
            self.assertIsNone(style.register_of(value), value)

    def test_address_shapes_are_classified_by_semantic_function(self):
        self.assertEqual(style.address_spec("唐姐")["kind"], "social_honorific")
        self.assertEqual(style.address_spec("邱科长")["kind"], "official_title")
        self.assertEqual(style.address_spec("叶将军")["kind"], "official_title")
        self.assertEqual(style.address_spec("叶上校")["kind"], "rank")
        self.assertEqual(style.address_spec("邱探员")["kind"], "role_reference")
        self.assertEqual(style.address_spec("老秦")["kind"], "nickname")
        # A plain canonical name or an ordinary name extension has no shape.
        self.assertIsNone(style.address_spec("唐菲菲"))
        self.assertIsNone(style.address_spec("邱途来"))
        # Literal kinship is a RAW question, reported as a hint only.
        self.assertTrue(
            style.address_spec("唐姐", "", ["唐姐是她的亲姐。"])["literal_kinship_hint"]
        )
        self.assertFalse(style.address_spec("唐姐", "", ["唐姐来了。"])["literal_kinship_hint"])

    def test_historical_reference_shapes_and_renderings(self):
        cases = (
            ("冯大伴", "冯保", "Phùng Bảo", "social_honorific", "Phùng Đại bạn"),
            ("张大伴", "张宏", "Trương Hoành", "social_honorific", "Trương Đại bạn"),
            ("张尚书", "张翰", "Trương Hãn", "official_title", "Trương Thượng thư"),
            ("李帅", "李成梁", "Lý Thành Lương", "rank", "Lý soái"),
            ("赵缇帅", "赵梦祐", "Triệu Mộng Hựu", "role_reference", "Triệu Đề soái"),
            ("张侍班", "张四维", "Trương Tứ Duy", "role_reference", "Trương Thị ban"),
            (
                "大司徒王国光",
                "王国光",
                "Vương Quốc Quang",
                "official_title",
                "Đại Tư đồ Vương Quốc Quang",
            ),
        )
        for form, canonical, translation, kind, expected in cases:
            spec = style.address_spec(form, canonical)
            self.assertEqual(spec["kind"], kind, form)
            self.assertEqual(style.preferred_form(form, canonical, translation, spec), expected)

        proven = Term(
            source="张四维",
            translation="Trương Tứ Duy",
            type="character",
            status="locked",
            forms={"张侍班": "Trương Thị ban"},
            evidence="张侍班就是张四维",
        )
        sanity({proven.source: proven})
class RegisterProfileTests(unittest.TestCase):
    def terms(self, *forms):
        return [
            Term(
                source="唐菲菲",
                translation="Đường Phỉ Phỉ",
                type="character",
                status="locked",
                forms=dict(forms),
            )
        ]

    def test_profile_is_derived_from_confirmed_forms_per_class(self):
        terms = self.terms(
            ("唐姐", "Đường tỷ"),
            ("唐妹", "Đường muội"),
            ("邱科长", "Khâu khoa trưởng"),
            ("邱副科长", "Trưởng khoa Khâu"),
            ("邱长官", "Sếp Khâu"),
        )
        derived = style.profile(terms)
        self.assertEqual(derived["dominant"], style.SINO_VIETNAMESE)
        self.assertEqual(derived["evidence"], 5)
        # Comparison is class-scoped: official titles drift modern while the
        # kinship-style honorifics stay Sino-Vietnamese.
        self.assertEqual(derived["classes"]["official_title"]["dominant"], style.MODERN)
        self.assertEqual(
            derived["classes"]["social_honorific"]["dominant"], style.SINO_VIETNAMESE
        )

    def test_auto_mode_needs_evidence_and_falls_back_to_the_baseline(self):
        self.assertEqual(
            style.effective_register(style.AUTO, style.profile([])),
            (style.SINO_VIETNAMESE, "baseline"),
        )
        modern = self.terms(("唐姐", "Chị Đường"), ("邱科长", "Trưởng khoa Khâu"))
        self.assertEqual(
            style.effective_register(style.AUTO, style.profile(modern)),
            (style.MODERN, "confirmed-forms"),
        )

    def test_configured_register_wins_over_derived_evidence(self):
        modern = self.terms(("唐姐", "Chị Đường"))
        self.assertEqual(
            style.effective_register(style.SINO_VIETNAMESE, style.profile(modern)),
            (style.SINO_VIETNAMESE, "configured"),
        )
        with patch.dict(os.environ, {"TRANSLATION_DICTIONARY_REGISTER": "auto"}):
            self.assertEqual(style.configured_register(), style.AUTO)
        with patch.dict(os.environ, {"TRANSLATION_DICTIONARY_REGISTER": "modern"}):
            self.assertEqual(style.configured_register(), style.MODERN)
        with patch.dict(os.environ, {}, clear=True):
            # Sino-Vietnamese is the project default, not "most natural modern".
            self.assertEqual(style.configured_register(), style.SINO_VIETNAMESE)


class AddressFormPolicyTests(unittest.TestCase):
    def test_kinship_address_is_normalized_to_the_established_register(self):
        terms, problems = load_legacy(
            {
                "entries": [
                    {
                        "source": "唐菲菲",
                        "translation": "Đường Phỉ Phỉ",
                        "type": "character",
                        "status": "locked",
                        "forms": {"唐姐": "Chị Đường", "老秦": "Lão Tần"},
                    }
                ]
            }
        )
        term = terms[0]
        # One preferred rendering per Chinese key; no second alias, no new identity.
        self.assertEqual(term.source, "唐菲菲")
        self.assertEqual(term.forms, {"唐姐": "Đường tỷ", "老秦": "Lão Tần"})
        self.assertEqual(term.form_kinds["唐姐"], "social_honorific")
        self.assertEqual(term.address_register, style.SINO_VIETNAMESE)
        cleanup = [
            item for item in problems if item["classification"] == "register_form_cleanup"
        ]
        self.assertEqual(cleanup[0]["normalizations"][0]["to"], "Đường tỷ")
        # The rewrite is a fixed point and stays enforced.
        normalized, again = load_legacy(export_dictionary({"唐菲菲": term}))
        self.assertEqual(normalized[0].forms, term.forms)
        self.assertEqual(
            [item for item in again if item["classification"] == "register_form_cleanup"],
            [],
        )
        findings = terminology_findings(
            [normalized[0].model_dump()], "唐姐来了。", "Chị Đường đến."
        )
        self.assertEqual(findings[0]["required_translation"], "Đường tỷ")

    def test_unrenderable_modern_form_is_dropped_instead_of_guessed(self):
        terms, problems = load_legacy(
            {
                "entries": [
                    {
                        "source": "唐菲菲",
                        "translation": "Đường Phỉ Phỉ",
                        "type": "character",
                        "status": "locked",
                        "forms": {"长官": "Sếp", "菲菲姐": "Chị Phỉ Phỉ"},
                    }
                ]
            }
        )
        term = terms[0]
        # A bare official title has an established wording, so it is normalized.
        # A personal reference that cannot be mapped onto the canonical name is
        # dropped instead of being guessed.
        self.assertEqual(term.forms, {"长官": "Trưởng quan"})
        exported = json.dumps(export_dictionary({"唐菲菲": term}), ensure_ascii=False)
        self.assertNotIn("菲菲姐", exported)
        cleanup = [
            item for item in problems if item["classification"] == "register_form_cleanup"
        ]
        self.assertEqual(cleanup[0]["normalizations"][0]["to"], "Trưởng quan")
        self.assertEqual(
            [item["source"] for item in cleanup[0]["removed_forms"]], ["菲菲姐"]
        )
        self.assertIn("modern wording", cleanup[0]["removed_forms"][0]["reason"])

    def test_modern_override_keeps_the_batch_modern(self):
        with patch.dict(os.environ, {"TRANSLATION_DICTIONARY_REGISTER": "modern"}):
            terms, problems = load_legacy(
                {
                    "entries": [
                        {
                            "source": "唐菲菲",
                            "translation": "Đường Phỉ Phỉ",
                            "type": "character",
                            "status": "locked",
                            "forms": {"唐姐": "Chị Đường"},
                        }
                    ]
                }
            )
        self.assertEqual(terms[0].forms, {"唐姐": "Chị Đường"})
        self.assertEqual(terms[0].address_register, style.MODERN)
        self.assertEqual(
            [item for item in problems if item["classification"] == "register_form_cleanup"],
            [],
        )

    def test_frozen_dictionary_records_its_register_and_sanity_enforces_it(self):
        terms, _ = load_legacy(
            {
                "entries": [
                    {
                        "source": "唐菲菲",
                        "translation": "Đường Phỉ Phỉ",
                        "type": "character",
                        "status": "locked",
                        "forms": {"唐姐": "Đường tỷ"},
                    }
                ]
            }
        )
        dictionary = export_dictionary({"唐菲菲": terms[0]})
        self.assertEqual(dictionary["register"], style.SINO_VIETNAMESE)
        self.assertEqual(
            dictionary["entries"][0]["form_kinds"], {"唐姐": "social_honorific"}
        )
        sanity({"唐菲菲": terms[0]})
        inconsistent = terms[0].model_copy(deep=True)
        inconsistent.forms["唐姐"] = "Chị Đường"
        with self.assertRaisesRegex(ValueError, "modern wording"):
            sanity({"唐菲菲": inconsistent})


class ResolverStyleContextTests(unittest.TestCase):
    def test_resolver_payload_carries_the_address_shape_and_register(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(
                Path(root),
                Inputs(
                    raw="第1章\n唐姐来了。\n唐姐就是唐菲菲。",
                    vietphrase="Chương 1\nChị Đường đến.\nChị Đường là Đường Phỉ Phỉ.",
                ).model_dump(),
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            payload = pipeline.resolver_payload("唐姐")
            self.assertEqual(payload["address_form"]["kind"], "social_honorific")
            self.assertEqual(payload["address_form"]["honorific"], "tỷ")
            self.assertEqual(
                payload["address_form"]["expected_register"], style.SINO_VIETNAMESE
            )
            context = style.resolver_context(
                pipeline.address_register,
                pipeline.address_register_source,
                pipeline.style_profile,
            )
            self.assertEqual(context["register"], style.SINO_VIETNAMESE)
            self.assertEqual(context["priority"][0], "canonical character identity")
            self.assertEqual(context["priority"][-1], "modern Vietnamese naturalness")
            # A plain canonical name is not an address form.
            self.assertIsNone(pipeline.resolver_payload("唐菲菲").get("address_form"))

    def test_unproven_kinship_address_never_becomes_its_own_character(self):
        pairs = validate_inputs(
            "第1章\n唐姐来了。\n唐姐走了。",
            "Chương 1\nChị Đường đến.\nChị Đường đi rồi.",
        )
        index = build_index(
            pairs, {raw.key: deterministic_alignment(raw, vp) for raw, vp in pairs}, []
        )
        self.assertNotIn("唐姐", index["candidates"])
        self.assertEqual(index["report_only"]["唐姐"]["classification"], "character_form")
        # RAW proving the identity keeps the reference available as a form.
        proven = validate_inputs(
            "第1章\n唐姐就是唐菲菲。\n唐姐走了。",
            "Chương 1\nChị Đường là Đường Phỉ Phỉ.\nChị Đường đi rồi.",
        )
        proven_index = build_index(
            proven,
            {raw.key: deterministic_alignment(raw, vp) for raw, vp in proven},
            [],
        )
        self.assertIn("唐姐", proven_index["candidates"])


class PipelineRegisterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_store(root, raw, vp, dictionary=None):
        return Store(
            Path(root),
            Inputs(raw=raw, vietphrase=vp, dictionary=dictionary).model_dump(),
        )

    async def test_frozen_dictionary_form_is_normalized_and_chapter_reused(self):
        with tempfile.TemporaryDirectory() as root:
            old_dictionary = {
                "version": 3,
                "entries": [
                    {
                        "source": "唐菲菲",
                        "translation": "Đường Phỉ Phỉ",
                        "type": "character",
                        "status": "locked",
                        "forms": {"唐姐": "Chị Đường"},
                    }
                ],
            }
            store = PipelineRegisterTests.make_store(
                root,
                raw="第1章\n唐姐来了。",
                vp="Chương 1\nChị Đường đến.",
                dictionary=old_dictionary,
            )
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
                        "segments": [{"id": 0, "text": "Đường tỷ đến."}],
                    },
                },
            )

            async def must_not_translate(model, body):
                raise AssertionError("a valid completed chapter should be reused")

            await Pipeline(
                store,
                Scheduler(transport=must_not_translate, concurrency=1, spacing=0),
            ).run()

            dictionary = store.read("dictionary.json")
            entry = dictionary["entries"][0]
            self.assertEqual(entry["forms"], {"唐姐": "Đường tỷ"})
            self.assertEqual(entry["form_kinds"], {"唐姐": "social_honorific"})
            self.assertEqual(entry["address_register"], style.SINO_VIETNAMESE)
            self.assertNotEqual(dictionary["dictionary_hash"], old_hash)
            summary = store.read("dictionary-resolution-report.json")["summary"]
            self.assertEqual(summary["register"], style.SINO_VIETNAMESE)
            self.assertEqual(summary["register_normalizations"], 1)
            self.assertEqual(summary["register_conflicts"], 0)
            profile = store.read("dictionary-style-profile.json")
            self.assertEqual(profile["register"], style.SINO_VIETNAMESE)
            # One classified form is evidence, not yet a derived majority; the
            # enforced register still comes from the frozen dictionary/override.
            self.assertEqual(profile["evidence"], 1)
            self.assertEqual(profile["counts"], {style.SINO_VIETNAMESE: 1})
            self.assertEqual(profile["dominant"], "unestablished")
            self.assertEqual(summary["register_source"], "stored")
            self.assertEqual(
                store.read("chapters/1.json")["dictionary_hash"],
                dictionary["dictionary_hash"],
            )

    async def test_resolver_address_form_is_normalized_before_freeze(self):
        with tempfile.TemporaryDirectory() as root:
            store = PipelineRegisterTests.make_store(
                root,
                raw="第1章\n唐姐来了。\n唐姐就是唐菲菲。",
                vp="Chương 1\nChị Đường đến.\nChị Đường là Đường Phỉ Phỉ.",
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            await pipeline.align(pipeline.pairs[0])
            pipeline.refresh_style()
            term = pipeline.apply_resolution(
                "唐姐",
                None,
                resolver_result("唐菲菲", "Đường Phỉ Phỉ", {"唐姐": "Chị Đường"}),
            )
            # The address form is attached to the canonical identity, never a
            # separate character, and it keeps the established rendering.
            self.assertEqual(term.source, "唐菲菲")
            self.assertEqual(term.forms, {"唐姐": "Đường tỷ"})
            self.assertEqual(term.form_kinds, {"唐姐": "social_honorific"})
            self.assertEqual(list(pipeline.terms), ["唐菲菲"])
            # Freezing the same dictionary again is a fixed point.
            pipeline.freeze(export_dictionary(pipeline.terms))
            first = pipeline.frozen_hash
            pipeline.freeze(export_dictionary(pipeline.terms))
            self.assertEqual(pipeline.frozen_hash, first)
            summary = pipeline.dictionary_report()["summary"]
            self.assertEqual(summary["register_normalizations"], 1)
            self.assertEqual(summary["register_conflicts"], 0)
            self.assertEqual(summary["register"], style.SINO_VIETNAMESE)

    async def test_address_shaped_source_never_becomes_a_canonical_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = PipelineRegisterTests.make_store(
                root,
                raw="第1章\n唐姐来了。\n唐姐就是唐菲菲。\n老秦来了。",
                vp="Chương 1\nChị Đường đến.\nChị Đường là Đường Phỉ Phỉ.\nLão Tần đến.",
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            await pipeline.align(pipeline.pairs[0])
            pipeline.refresh_style()
            # Returning the address wording as its own canonical identity is
            # rejected; a bare nickname stays eligible as a real identity.
            with self.assertRaisesRegex(QualityError, "cannot be a canonical identity"):
                pipeline.apply_resolution(
                    "唐姐",
                    None,
                    resolver_result("唐姐", "Đường tỷ", {}),
                )
            self.assertEqual(pipeline.terms, {})
            nickname = pipeline.apply_resolution(
                "老秦",
                None,
                resolver_result("老秦", "Lão Tần", {}),
            )
            self.assertEqual(nickname.source, "老秦")

    async def test_unrenderable_resolver_form_is_dropped_with_a_reason(self):
        with tempfile.TemporaryDirectory() as root:
            store = PipelineRegisterTests.make_store(
                root,
                raw="第1章\n菲菲姐来了。\n菲菲姐就是唐菲菲。",
                vp="Chương 1\nChị Phỉ Phỉ đến.\nChị Phỉ Phỉ là Đường Phỉ Phỉ.",
            )
            pipeline = Pipeline(store, Scheduler(concurrency=1, spacing=0))
            pipeline.pairs = validate_inputs(
                store.read("inputs.json")["raw"], store.read("inputs.json")["vietphrase"]
            )
            await pipeline.align(pipeline.pairs[0])
            pipeline.refresh_style()
            term = pipeline.apply_resolution(
                "菲菲姐",
                None,
                resolver_result("唐菲菲", "Đường Phỉ Phỉ", {"菲菲姐": "Chị Phỉ Phỉ"}),
            )
            self.assertEqual(term.forms, {})
            record = pipeline.register_cleanup[0]
            self.assertEqual(record["register"], style.SINO_VIETNAMESE)
            self.assertEqual(record["removed_forms"][0]["source"], "菲菲姐")
            self.assertEqual(
                store.read("dictionary-register-cleanup.json")["entries"],
                pipeline.register_cleanup,
            )
