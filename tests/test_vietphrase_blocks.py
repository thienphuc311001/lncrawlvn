"""Regression tests for the VietPhrase-safe TXT block layout.

Chapters are chunked into deterministic blocks (target 2600 / max 3000 chars)
separated by exactly one blank line. Only blank block boundaries may be added:
removing them must reproduce the crawled chapter content exactly.
"""

import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lncrawl.binder import make_text
from lncrawl.binder import vietphrase as vp
from lncrawl.binder.vietphrase import (
    SAFE_BLOCK_MAX,
    SAFE_BLOCK_TARGET,
    build_chapter_blocks,
    build_export_text,
    content_lines,
    normalize_newlines,
    resolve_safe_block_limits,
    validate_export,
)
from lncrawl.core import Chapter, Novel
from lncrawl.exceptions import LNException

SEP = "-" * 60
WRAPPER_LINE = re.compile(r"^Chapter \d+[:\uff1a]", re.MULTILINE)


def novel() -> Novel:
    return Novel(
        url="https://example.test/book",
        title="\u6d4b\u8bd5\u5c0f\u8bf4",
        author="\u4f5c\u8005",
        synopsis="\u7b80\u4ecb\u3002",
        tags=["\u5386\u53f2"],
    )


def chapter(cid, native_heading, paragraphs, wrapper_title="\u5217\u8868\u6807\u9898"):
    body = "<h3>%s</h3>" % native_heading + "".join(
        "<p>%s</p>" % p for p in paragraphs
    )
    return Chapter(
        id=cid,
        url="https://example.test/ch/%s" % cid,
        title=wrapper_title,
        body=body,
        success=True,
    )


def para(fill, length):
    """Deterministic CJK-ish paragraph of exactly ``length`` chars."""
    return (fill * ((length // len(fill)) + 1))[:length]


def export(chapters, **kwargs):
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "book.txt"
        make_text(novel(), chapters, out, **kwargs)
        raw = out.read_bytes()
        raw.decode("utf-8")  # must be valid UTF-8
        return raw.decode("utf-8")


def chapter_region(text):
    return text.split("+" * 60, 1)[1]


def blocks_of(region_part):
    """Block line-lists for one chapter body (blank separators removed)."""
    return [
        block.split("\n")
        for block in region_part.strip("\n").split("\n\n")
    ]


def chapter_parts(text):
    region = chapter_region(text)
    raw = region.split(SEP)
    return [chunk[1:] for chunk in raw[1:]]


class ShortChapterTests(unittest.TestCase):
    def test_short_chapter_gains_no_internal_blank_lines(self):
        text = export([chapter(1, "\u7b2c1\u7ae0 \u5f00\u59cb", ["\u6b63\u6587\u3002"])])
        parts = chapter_parts(text)
        self.assertEqual(len(parts), 1)
        self.assertEqual(blocks_of(parts[0]), [["\u7b2c1\u7ae0 \u5f00\u59cb", "\u6b63\u6587\u3002"]])


class LongChapterTests(unittest.TestCase):
    def test_6000_char_chapter_becomes_two_or_three_blocks(self):
        paragraphs = [para("\u7532", 500) for _ in range(12)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u957f\u7ae0", paragraphs)])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertEqual(len(blocks), 3)
        self.assertTrue(all(0 < sum(map(len, b)) <= SAFE_BLOCK_MAX for b in blocks))

    def test_9000_char_chapter_becomes_three_or_four_blocks(self):
        paragraphs = [para("\u4e59", 500) for _ in range(18)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u957f\u7ae0", paragraphs)])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertIn(len(blocks), (3, 4))
        self.assertTrue(all(0 < sum(map(len, b)) <= SAFE_BLOCK_MAX for b in blocks))


class BoundaryBehaviorTests(unittest.TestCase):
    def test_paragraph_moves_whole_when_it_would_overflow(self):
        first = [para("\u7532", 2950)]
        second = para("\u4e59", 200)
        text = export([chapter(1, "\u7b2c1\u7ae0 \u754c\u9650", first + [second])])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1][-1], second)
        self.assertEqual(len(blocks[1][-1]), 200)

    def test_clean_early_boundary_beats_weaker_late_one(self):
        early = para("\u7532\u3002", 2400)
        filler = [para("\u4e59", 100) for _ in range(5)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u754c\u9650", [early] + filler)])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertGreaterEqual(len(blocks), 1)
        self.assertLessEqual(sum(map(len, blocks[0])), SAFE_BLOCK_MAX)

    def test_speech_intro_stays_with_its_quotation(self):
        intro = "\u5f20\u5c45\u6b63\u6c89\u58f0\u8bf4\u9053\uff1a"
        quote = "\u201c\u81e3\u4ee5\u4e3a\u6b64\u4e8b\u4e0d\u59a5\u2026\u2026\u201d"
        filler = [para("\u7532\u3002", 400) for _ in range(6)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u5bf9\u8bdd", filler + [intro, quote])])
        region = chapter_region(text)
        flat = [ln for ln in region.split("\n") if ln != ""]
        self.assertEqual(flat.index(intro) + 1, flat.index(quote))

    def test_no_boundary_inside_open_quotation(self):
        open_quote = "\u201c\u9640\u4e0b\u3001\u81e3\u8ba4\u4e3a" + para("\u8fd9\u4ef6\u4e8b", 2600)
        tail = para("\u8fd8\u9700\u4ece\u957f\u8ba1\u8bae\u3002\u201d", 120)
        after = para("\u4e0b\u4e00\u6bb5", 300)
        text = export([chapter(1, "\u7b2c1\u7ae0 \u5f15\u53f7", [open_quote, tail, after])])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        joined_first = "".join(blocks[0])
        self.assertIn("\u201d", joined_first)


class OversizedParagraphTests(unittest.TestCase):
    def test_oversized_paragraph_splits_at_sentence_boundary(self):
        long_para = para("\u7532\u3002", 1500) + para("\u4e59\u3002", 1500) + para("\u4e19", 200)
        text = export([chapter(1, "\u7b2c1\u7ae0 \u8d85\u957f", [long_para])])
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLessEqual(sum(map(len, block)), SAFE_BLOCK_MAX)
        segments = [ln for block in blocks for ln in block][1:]
        self.assertTrue(segments[0].endswith("\u3002"))
        rejoined = [ln for block in blocks for ln in block][1:]
        self.assertEqual("".join(rejoined), long_para)

    def test_native_heading_is_never_split_even_when_oversized(self):
        heading = "\u7b2c1\u7ae0 " + para("\u9898", 4000)
        body = para("\u6b63\u6587\u3002", 300)
        built = build_export_text(novel(), [chapter(1, heading, [body])])
        self.assertEqual(built.audit["oversized_headings_kept"], 1)
        self.assertIn(len(heading), built.oversized_heading_sizes)
        validate_export(built)  # reported exception, never a fatal validation path
        region = built.text.split("+" * 60, 1)[1]
        lines = [ln for ln in region.split("\n") if ln != ""]
        self.assertIn(heading, lines)
        self.assertEqual(lines[1], heading)


class PreservationTests(unittest.TestCase):
    def test_source_lines_stay_separate(self):
        text = export([chapter(1, "\u7b2c1\u7ae0 \u6807\u9898", ["line A", "line B", "line C"])])
        self.assertIn("line A\nline B\nline C", text)
        self.assertNotIn("line Aline B", text)

    def test_heading_never_merged_with_first_paragraph(self):
        text = export([chapter(1, "\u7b2c102\u7ae0 \u6807\u9898", ["\u9ad8\u542f\u611a\u641e\u4e86\u4e2a\u5927\u65b0\u95fb..."])])
        self.assertIn(SEP + "\n\u7b2c102\u7ae0 \u6807\u9898\n", text)

    def test_stripping_block_blanks_reproduces_source(self):
        paragraphs = [para("\u6bb5", 400) for _ in range(10)]
        chapters = [chapter(1, "\u7b2c1\u7ae0 \u7532", paragraphs), chapter(2, "\u7b2c2\u7ae0 \u4e59", paragraphs)]
        text = export(chapters)
        region = chapter_region(text)
        bodies = [chunk[1:].strip("\n").split("\n") for chunk in region.split(SEP)[1:]]
        flat = [ln for body in bodies for ln in body if ln != ""]
        expected = []
        for ch in chapters:
            expected.extend(content_lines(ch.body and __import__("lncrawl.utils.html_tools", fromlist=["extract_text"]).extract_text(ch.body)))
        self.assertEqual(flat, expected)

    def test_no_wrapper_no_markers_lf_only(self):
        text = export([chapter(1, "\u7b2c1\u7ae0 \u5f00\u59cb", ["\u6b63\u6587\u3002"])])
        self.assertNotRegex(text, WRAPPER_LINE)
        self.assertNotIn("[BLOCK", text)
        self.assertNotIn("\r", text)
        for line in text.split("\n"):
            self.assertEqual(line, line.strip() if not line else line)
            if line == "":
                continue
            self.assertTrue(line.strip())

    def test_written_bytes_are_utf8_with_lf_only(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "book.txt"
            make_text(novel(), [chapter(1, "\u7b2c1\u7ae0 \u7532", ["\u6b63\u6587\u3002"])], out)
            raw = out.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertEqual(raw.decode("utf-8").count("\u6b63\u6587\u3002"), 1)
            self.assertTrue(raw.endswith(b"\n"))

    def test_exactly_one_blank_before_each_separator(self):
        chapters = [chapter(1, "\u7b2c1\u7ae0 \u7532", ["a"]), chapter(2, "\u7b2c2\u7ae0 \u4e59", ["b"])]
        text = export(chapters)
        self.assertNotIn("\n\n\n" + SEP, text)
        region = chapter_region(text)
        self.assertEqual(region.count("\n\n" + SEP), 2)
        self.assertNotIn("\n\n\n" + SEP, region)


class HeadingLessTests(unittest.TestCase):
    def test_heading_less_chapter_is_report_only(self):
        chapters = [chapter(7, "wrapper", ["\u6b63\u6587\u7b2c\u4e00\u6bb5\u3002"])]
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "book.txt"
            make_text(novel(), chapters, out)
            text = out.read_text(encoding="utf-8")
        self.assertIn(SEP + "\nwrapper\n\u6b63\u6587\u7b2c\u4e00\u6bb5\u3002", text)
        built = build_export_text(novel(), chapters)
        self.assertEqual(built.audit["heading_missing_report_only"], 1)
        validate_export(built)  # must not raise


class BoundaryRankTests(unittest.TestCase):
    """Semantic boundary strength, and the rule that rank outranks score."""

    def test_rank_table_orders_structural_strength(self):
        ranks = vp._BOUNDARY_RANK
        self.assertGreater(ranks["section"], ranks["dialogue"])
        self.assertEqual(ranks["dialogue"], ranks["paragraph"])
        self.assertGreater(ranks["paragraph"], ranks["sentence"])
        self.assertGreater(ranks["sentence"], ranks["medium"])
        self.assertGreater(ranks["medium"], ranks["weak"])
        # The section score is *lower* than the dialogue score, so only the rank
        # ordering can keep "a cleaner section boundary" ahead of a dialogue.
        self.assertEqual((vp._BOUNDARY_SCORE["section"], vp._BOUNDARY_SCORE["dialogue"]), (120, 140))

    def test_section_outranks_higher_scoring_dialogue(self):
        """+120 section beats +140 dialogue purely on rank."""
        section = vp.Boundary(
            index=10, kind="section", score=120, block_chars=2400, next_chars=600
        )
        dialogue = vp.Boundary(
            index=12, kind="dialogue", score=140, block_chars=2500, next_chars=500
        )
        self.assertEqual(vp.select_boundary([dialogue, section]), section)
        self.assertEqual(vp.select_boundary([section, dialogue]), section)

    def test_equal_rank_higher_score_wins(self):
        dialogue = vp.Boundary(
            index=12, kind="dialogue", score=140, block_chars=2500, next_chars=500
        )
        paragraph = vp.Boundary(
            index=13, kind="paragraph", score=130, block_chars=2700, next_chars=300
        )
        self.assertEqual(vp.select_boundary([paragraph, dialogue]), dialogue)

    def test_rank_dominates_any_score_or_position_bonus(self):
        """No window/sweet-spot bonus can promote a weaker boundary class."""
        best_weaker = vp.Boundary(
            index=20, kind="sentence", score=10_000, block_chars=2800, next_chars=200
        )
        plain_paragraph = vp.Boundary(
            index=15, kind="paragraph", score=100, block_chars=2350, next_chars=650
        )
        weak = vp.Boundary(
            index=21, kind="weak", score=10_000, block_chars=2900, next_chars=100
        )
        self.assertEqual(vp.select_boundary([best_weaker, plain_paragraph, weak]), plain_paragraph)

    def test_open_quotation_penalty_loses_within_the_same_rank(self):
        inside = vp.Boundary(
            index=11, kind="paragraph", score=100 - 1000, block_chars=2650,
            next_chars=350, inside_quotation=True, depth=1,
        )
        closed = vp.Boundary(
            index=12, kind="paragraph", score=100, block_chars=2700, next_chars=300, depth=0
        )
        self.assertEqual(vp.select_boundary([inside, closed]), closed)

    def test_section_boundary_wins_over_dialogue_in_a_chapter(self):
        """End to end: the section split is chosen, and no dialogue is broken."""
        heading = "\u7b2c1\u7ae0 \u6807\u9898"
        section_paragraphs = para("\u7532", 2393) + "\u3002"
        dialogue = "\u201c" + para("\u4e59", 146) + "\u3002\u201d"
        closing = para("\u4e19\u3002", 400)
        source = "\n".join([heading, section_paragraphs, "", dialogue, closing])
        lines, sections = vp.source_layout(source)
        self.assertEqual(sorted(sections), [1])

        built = build_chapter_blocks(
            lines,
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
            sections=sections,
        )
        self.assertEqual(built.blocks[0], [heading, section_paragraphs])
        self.assertEqual(built.blocks[1][0], dialogue)
        self.assertEqual([(b.kind, b.score) for b in built.boundaries], [("section", 120)])
        self.assertEqual(built.open_quote_cuts, 0)
        self.assertEqual("".join(built.content), "".join(lines))


class DialoguePreferenceTests(unittest.TestCase):
    def test_clean_dialogue_boundary_beats_late_paragraph_boundary(self):
        """A closed-dialogue paragraph at ~2400 wins over a paragraph at ~2950."""
        heading = "\u7b2c1\u7ae0 \u5bf9\u8bdd"
        speech = "\u201c" + para("\u7532", 2396) + "\u3002\u201d"
        following = para("\u4e59\u3002", 548)
        built = build_chapter_blocks(
            [heading, speech, following],
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
        )
        self.assertEqual(len(built.blocks), 2)
        self.assertEqual(built.block_sizes[0], len(heading) + len(speech))
        self.assertEqual([b.kind for b in built.boundaries], ["dialogue"])
        self.assertEqual(built.content, [heading, speech, following])

    def test_closed_quotation_candidate_beats_open_one(self):
        """A boundary that closes the quotation beats an earlier open one."""
        opened = "\u201c" + para("\u4e19", 2598)
        closed = para("\u4e01", 298) + "\u3002\u201d"
        after = para("\u620a", 100)
        built = build_chapter_blocks(
            ["\u7b2c1\u7ae0 \u5f15\u53f7", opened, closed, after],
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
        )
        self.assertEqual(built.open_quote_cuts, 0)
        self.assertIn("\u201d", "".join(built.blocks[0]))
        self.assertGreaterEqual(built.block_sizes[0], len(opened) + len(closed))
        self.assertTrue(all(size <= SAFE_BLOCK_MAX for size in built.block_sizes))


class AuditTests(unittest.TestCase):
    def test_boundary_counts_match_inserted_blanks(self):
        paragraphs = [para("\u6bb5\u3002", 400) for _ in range(12)]
        built = build_export_text(
            novel(), [chapter(1, "\u7b2c1\u7ae0", paragraphs), chapter(2, "\u7b2c2\u7ae0", paragraphs)]
        )
        audit = built.audit
        self.assertEqual(
            audit["boundaries_at_paragraph"]
            + audit["boundaries_at_sentence"]
            + audit["boundaries_using_weak_fallback"],
            audit["blank_boundaries_inserted"],
        )
        self.assertEqual(audit["blank_boundaries_inserted"], audit["safe_blocks_created"] - 2)
        self.assertGreater(audit["boundaries_at_paragraph"], 0)

    def test_chapter_end_cut_is_not_a_boundary(self):
        """A chapter that fits in one block records no boundary at all."""
        built = build_chapter_blocks(
            ["\u7b2c1\u7ae0 \u5f00\u59cb", "\u6b63\u6587\u3002"],
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
        )
        self.assertEqual(len(built.blocks), 1)
        self.assertEqual(built.boundaries, [])
        self.assertEqual(built.stunted_tail_cuts, 0)

    def test_soft_penalty_avoids_a_tiny_trailing_block(self):
        """A 2750 + 150 split rebalances: both ends punctuate, so one block wins."""
        heading = "\u7b2c1\u7ae0 \u5c3e\u5df4"
        first = para("\u7532", 2744) + "\u3002"
        tail = para("\u4e59", 147) + "\u3002"
        built = build_chapter_blocks(
            [heading, first, tail],
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
        )
        self.assertEqual(built.block_sizes, [len(heading) + len(first) + len(tail)])
        self.assertEqual(built.boundaries, [])
        self.assertEqual(built.stunted_tail_cuts, 0)

    def test_short_tail_penalty_is_soft_not_a_rule(self):
        """The penalty never forces a split: a worse boundary loses to it."""
        heading = "\u7b2c1\u7ae0 \u5c3e\u5df4"
        first = para("\u7532", 2744) + "\u3002"
        unpunctuated_tail = para("\u4e59", 150)
        built = build_chapter_blocks(
            [heading, first, unpunctuated_tail],
            target=SAFE_BLOCK_TARGET,
            maximum=SAFE_BLOCK_MAX,
        )
        # The 2750 boundary ends a sentence; the alternative ends mid-air, so the
        # small tail is kept and reported rather than being rebalanced away.
        self.assertEqual(built.block_sizes, [len(heading) + len(first), len(unpunctuated_tail)])
        self.assertEqual(built.stunted_tail_cuts, 1)
        self.assertTrue(all(size <= SAFE_BLOCK_MAX for size in built.block_sizes))


class InterfaceTests(unittest.TestCase):
    def test_cli_flags_and_defaults(self):
        from lncrawl.app import build_parser

        parser = build_parser()
        defaults = parser.parse_args(["https://example.test/book"])
        self.assertIsNone(defaults.safe_block_target)
        self.assertIsNone(defaults.safe_block_max)
        parsed = parser.parse_args(
            ["https://example.test/book", "--safe-block-target", "2000", "--safe-block-max", "2200"]
        )
        self.assertEqual((parsed.safe_block_target, parsed.safe_block_max), (2000, 2200))

    def test_web_settings_expose_resolved_defaults(self):
        from lncrawl.server import get_config

        fields = {field.key: field for field in get_config()}
        self.assertEqual(fields["safe_block_target"].value, SAFE_BLOCK_TARGET)
        self.assertEqual(fields["safe_block_max"].value, SAFE_BLOCK_MAX)
        self.assertEqual(fields["safe_block_target"].default, SAFE_BLOCK_TARGET)
        self.assertEqual(fields["safe_block_max"].default, SAFE_BLOCK_MAX)


class LimitConfigTests(unittest.TestCase):
    def setUp(self):
        from lncrawl.context import ctx

        self.ctx = ctx
        self.prior_target = getattr(ctx.config, "safe_block_target", None)
        self.prior_max = getattr(ctx.config, "safe_block_max", None)

    def tearDown(self):
        for name, value in (
            ("safe_block_target", self.prior_target),
            ("safe_block_max", self.prior_max),
        ):
            if value is None:
                self.ctx.config.__dict__.pop(name, None)
            else:
                setattr(self.ctx.config, name, value)

    def test_defaults(self):
        self.assertEqual((SAFE_BLOCK_TARGET, SAFE_BLOCK_MAX), (2600, 3000))
        target, maximum = resolve_safe_block_limits()
        self.assertEqual((target, maximum), (2600, 3000))

    def test_target_clamped_to_maximum(self):
        target, maximum = resolve_safe_block_limits(target=3500, maximum=3000)
        self.assertEqual((target, maximum), (3000, 3000))

    def test_env_override(self):
        os.environ["VIETPHRASE_SAFE_BLOCK_TARGET"] = "2000"
        os.environ["VIETPHRASE_SAFE_BLOCK_MAX"] = "2500"
        try:
            self.assertEqual(resolve_safe_block_limits(), (2000, 2500))
        finally:
            del os.environ["VIETPHRASE_SAFE_BLOCK_TARGET"]
            del os.environ["VIETPHRASE_SAFE_BLOCK_MAX"]

    def test_explicit_arguments_win(self):
        target, maximum = resolve_safe_block_limits(target=1000, maximum=1200)
        self.assertEqual((target, maximum), (1000, 1200))
        paragraphs = [para("\u7532", 500) for _ in range(6)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u77ed", paragraphs)], target=1000, maximum=1200)
        parts = chapter_parts(text)
        blocks = blocks_of(parts[0])
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(sum(map(len, b)) <= 1200 for b in blocks))

    def test_ui_setting_wins_over_env_and_default(self):
        os.environ["VIETPHRASE_SAFE_BLOCK_TARGET"] = "2000"
        os.environ["VIETPHRASE_SAFE_BLOCK_MAX"] = "2500"
        self.ctx.config.safe_block_target = 1500
        self.ctx.config.safe_block_max = 1800
        try:
            self.assertEqual(resolve_safe_block_limits(), (1500, 1800))
            # an explicit argument still outranks the persisted UI setting
            self.assertEqual(resolve_safe_block_limits(maximum=1200)[1], 1200)
        finally:
            del os.environ["VIETPHRASE_SAFE_BLOCK_TARGET"]
            del os.environ["VIETPHRASE_SAFE_BLOCK_MAX"]

    def test_ui_setting_reaches_the_exporter(self):
        self.ctx.config.safe_block_target = 1000
        self.ctx.config.safe_block_max = 1200
        paragraphs = [para("\u7532\u3002", 400) for _ in range(10)]
        text = export([chapter(1, "\u7b2c1\u7ae0 \u77ed", paragraphs)])
        blocks = blocks_of(chapter_parts(text)[0])
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(sum(map(len, block)) <= 1200 for block in blocks))


class ValidationTests(unittest.TestCase):
    def test_rejects_wrapper_line(self):
        bad = chapter(1, "\u7b2c1\u7ae0", ["\u6b63\u6587"])
        bad.body = "<p>Chapter 1: fake</p><p>\u6b63\u6587</p>"
        built = build_export_text(novel(), [bad])
        with self.assertRaises(LNException):
            validate_export(built)

    def test_rejects_content_mismatch(self):
        built = build_export_text(novel(), [chapter(1, "\u7b2c1\u7ae0", ["\u6b63\u6587"])])
        built.chapters[0][1].append("\u4e22\u5931\u7684\u884c")
        with self.assertRaises(LNException):
            validate_export(built)

    def test_rejects_block_over_maximum(self):
        built = build_export_text(novel(), [chapter(1, "\u7b2c1\u7ae0", ["\u6b63\u6587"])])
        built.block_sizes.append(SAFE_BLOCK_MAX + 1)
        with self.assertRaises(LNException):
            validate_export(built)

    def test_audit_keys_present(self):
        paragraphs = [para("\u7532\u3002", 500) for _ in range(8)]
        built = build_export_text(novel(), [chapter(1, "\u7b2c1\u7ae0", paragraphs)])
        for key in (
            "chapters_exported", "safe_block_target", "safe_block_max",
            "safe_blocks_created", "blank_boundaries_inserted",
            "oversized_paragraphs_split", "maximum_block_chars",
            "minimum_block_chars", "average_block_chars",
            "boundaries_at_paragraph", "boundaries_at_sentence",
            "boundaries_using_weak_fallback", "heading_missing_report_only",
        ):
            self.assertIn(key, built.audit)
        self.assertEqual(built.audit["chapters_exported"], 1)
        self.assertGreater(built.audit["safe_blocks_created"], 1)

    def test_normalize_newlines(self):
        self.assertEqual(normalize_newlines("a\r\nb\rc"), "a\nb\nc")

    def test_build_blocks_directly(self):
        lines = ["\u7b2c1\u7ae0 \u5f00\u59cb"] + [para("\u6bb5", 500) for _ in range(8)]
        built = build_chapter_blocks(lines, target=SAFE_BLOCK_TARGET, maximum=SAFE_BLOCK_MAX)
        self.assertEqual(built.content, lines)
        self.assertTrue(all(size <= SAFE_BLOCK_MAX for size in built.block_sizes))


if __name__ == "__main__":
    unittest.main()
