"""Offline regression fixtures for real crawler/export chapter structure."""

import unittest

from lncrawl.translation.parsing import (
    ChapterValidationError,
    chapter_number,
    has_meaningful_body_content,
    pair_chapters,
    parse_chapter_heading,
    parse_chapters,
    validate_inputs,
)


def book(numbers, heading="Chapter {}", body="他走了。"):
    return "\n\n".join(f"{heading.format(n)}\n{body}" for n in numbers)


class RobustParsingTests(unittest.TestCase):
    def assert_failure(self, text, reason, label="RAW"):
        with self.assertRaises(ChapterValidationError) as caught:
            parse_chapters(text, label)
        self.assertEqual(caught.exception.detail["reason"], reason)
        self.assertEqual(caught.exception.detail["input"], label)
        for field in ("line", "heading", "chapter", "previous_chapter", "message", "severity"):
            self.assertIn(field, caught.exception.detail)
        return caught.exception.detail

    def test_order_gaps_and_short_bodies(self):
        for numbers in ([1, 2, 3], [1, 3, 5], [15, 17], list(range(1, 101))):
            for separator in ("\n", "\n\n"):
                text = separator.join(f"Chapter {n}\n“嗯。”" for n in numbers)
                self.assertEqual([c.number for c in parse_chapters(text)], list(numbers))

    def test_formats_and_numerals(self):
        for heading in (
            "第1章",
            "第 1 章",
            "第一章",
            "Chapter 1",
            "CHAPTER 1",
            "Chương 1",
            "CHƯƠNG 1",
            "第1章 开始",
            "Chapter 1: Beginning",
            "Chương 1: Khởi đầu",
        ):
            with self.subTest(heading=heading):
                self.assertEqual(parse_chapters(heading + "\n正文。")[0].number, 1)
        for text, number in (
            ("一〇一", 101),
            ("一百零一", 101),
            ("十一", 11),
            ("两千三百四十五", 2345),
            ("一万零一", 10001),
        ):
            self.assertEqual(chapter_number(text), number)

    def test_export_duplicate_preserved(self):
        diagnostics = []
        text = "Chapter 1: 第1章 开始\n第1章 开始\n正文……"
        chapters = parse_chapters(text, diagnostics=diagnostics)
        self.assertEqual(len(chapters), 1)
        # The confirmed exporter duplicate is excluded from translation prose.
        self.assertEqual(chapters[0].paragraphs, ["正文……"])
        duplicate = next(d for d in diagnostics if d["reason"] == "duplicate_embedded_heading")
        self.assertEqual(duplicate["severity"], "WARNING")
        self.assertEqual(duplicate["line"], 2)
        self.assertEqual(duplicate["heading"], "第1章 开始")
        self.assertEqual(duplicate["previous_line"], 1)
        self.assertEqual(duplicate["nested_heading"], "第1章 开始")
        self.assertEqual(duplicate["wrapper_line"], 1)
        self.assertEqual(duplicate["meaningful_characters"], 0)

    def test_vietphrase_export_duplicate_is_excluded_before_alignment(self):
        raw = "Chapter 1: 第1章 危机嗅觉\n" + "-" * 60 + "\n第1章 危机嗅觉\n正文。"
        vp = (
            "Chapter 1: thứ 1 chương nguy cơ khứu giác\n"
            + "-" * 60
            + ("\nThứ 1 chương nguy cơ khứu giác\nBản tham chiếu.")
        )
        diagnostics = []
        chapters = parse_chapters(vp, "VIETPHRASE", diagnostics)
        self.assertEqual(chapters[0].paragraphs, ["Bản tham chiếu."])
        duplicate = next(
            diagnostic
            for diagnostic in diagnostics
            if diagnostic["reason"] == "duplicate_embedded_vietphrase_title"
        )
        self.assertEqual(duplicate["line"], 3)
        self.assertEqual(duplicate["wrapper_line"], 1)
        self.assertEqual(duplicate["original_text"], "Thứ 1 chương nguy cơ khứu giác")
        pair_chapters(parse_chapters(raw), chapters)

    def test_vietphrase_title_mismatch_is_a_reported_conflict(self):
        # ``Thứ N chương`` is itself a recognized heading form, so an embedded
        # title that disagrees with its wrapper is a conflicting duplicate of the
        # same chapter, exactly like the RAW ``Chapter N: 第N章 ...`` mismatch.
        # It must not be silently merged into the body of either chapter.
        text = "Chapter 1: thứ 1 chương tên cũ\n" + "-" * 60 + ("\nThứ 1 chương tên mới\nNội dung.")
        self.assert_failure(text, "duplicate_chapter", "VIETPHRASE")

    def test_uploaded_vietphrase_chapter_62_truncated_title(self):
        wrapper = "Chapter 62: thứ 62 chương Tần Schumann khôi phục thân thể (cảm tạ “Kawabunga”"
        embedded = (
            "Thứ 62 chương Tần Schumann khôi phục thân thể (cảm tạ “KAWABUNGA” 10 vạn thưởng)"
        )
        diagnostics = []
        chapter = parse_chapters(
            wrapper + "\n" + "-" * 60 + "\n" + embedded + "\nNội dung.", "VIETPHRASE", diagnostics
        )[0]
        self.assertEqual(chapter.paragraphs, ["Nội dung."])
        self.assertEqual(diagnostics[-1]["match_kind"], "truncated_parenthetical")

    def test_exact_chapter_sixty_two_exporter_format(self):
        heading = "第62章 秦舒曼恢复身体（感谢“KAWABUNGA”的10万赏）"
        for eol in ("\n", "\r\n"):
            for separator in ("-" * 60, "=" * 40, "*" * 20, "_" * 30, "＊＊＊＊"):
                diagnostics = []
                text = eol.join(
                    [
                        f"Chapter 62: {heading}",
                        separator,
                        "",
                        heading,
                        "秦舒曼睁开眼。",
                        "“我回来了。”",
                    ]
                )
                chapters = parse_chapters(text, diagnostics=diagnostics)
                self.assertEqual([c.number for c in chapters], [62])
                self.assertEqual(chapters[0].title, f"Chapter 62: {heading}")
                self.assertEqual(chapters[0].paragraphs, ["秦舒曼睁开眼。", "“我回来了。”"])
                duplicate = next(
                    d for d in diagnostics if d["reason"] == "duplicate_embedded_heading"
                )
                self.assertEqual(duplicate["line"], 4)
                self.assertEqual(duplicate["heading"], heading)
                self.assertEqual(duplicate["nested_heading"], heading)
                self.assertNotIn("第62章", "".join(chapters[0].paragraphs))

    def test_actual_uploaded_truncated_chapter_62(self):
        wrapper = "Chapter 62: 第62章 秦舒曼恢复身体(感谢“Kawabunga”"
        heading = "第62章 秦舒曼恢复身体（感谢“KAWABUNGA”的10万赏）"
        diagnostics = []
        text = "\n" * 5359 + wrapper + "\n" + "-" * 60 + "\n\n" + heading + "\n正文。"
        chapters = parse_chapters(text, diagnostics=diagnostics)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].source_line, 5360)
        self.assertEqual(chapters[0].title, wrapper)
        self.assertEqual(chapters[0].paragraphs, ["正文。"])
        duplicate = next(d for d in diagnostics if d["reason"] == "duplicate_embedded_heading")
        self.assertEqual(duplicate["line"], 5363)
        self.assertEqual(duplicate["original_text"], heading)
        self.assertEqual(duplicate["match_kind"], "truncated_parenthetical")

    def test_exporter_unicode_and_truncation_guards(self):
        heading = "第62章 秦舒曼恢复身体（感谢“KAWABUNGA”的10万赏）"
        wrapper = "Chapter 62: " + heading.replace("KAWABUNGA", "ＫＡＷＡＢＵＮＧＡ")
        chapters = parse_chapters(wrapper + "\n---\n" + heading + "\n正文。")
        self.assertEqual(chapters[0].paragraphs, ["正文。"])
        # The uploaded exporter also removes punctuation from chapter-list titles.
        for number, short, full in (
            (57, "金手指刷新1(7K求追读!)", "金手指刷新+1（7k求追读！）"),
            (66, "收获记忆碎片(4K字)", "收获-记忆碎片（4k字）"),
        ):
            text = f"Chapter {number}: 第{number}章 {short}\n---\n第{number}章 {full}\n正文。"
            self.assertEqual(parse_chapters(text)[0].paragraphs, ["正文。"])
        for nested in (
            "第61章 秦舒曼恢复身体(感谢“Kawabunga”",
            "第62章 秦舒曼失去身体(感谢“Kawabunga”",
            "第62章 秦舒曼恢复身体(感谢“Other”",
            "第62章 秦舒曼恢复身体(感谢“Kawabunga”) ",
            "第62章 秦舒曼恢复",
        ):
            with self.subTest(nested=nested):
                self.assert_failure(
                    "Chapter 62: " + nested + "\n---\n" + heading + "\n正文。", "duplicate_chapter"
                )
        self.assert_failure(
            "Chapter 62: 第62章 秦舒曼恢复身体(感谢“Kawabunga”\n真实正文\n" + heading + "\n正文。",
            "duplicate_chapter",
        )

    def test_multiple_exporter_chapters_with_volumes(self):
        text = ""
        for n, title in ((61, "前情"), (62, "秦舒曼恢复身体")):
            text += (
                f"第一卷 起\nChapter {n}: 第{n}章 {title}\n"
                + "-" * 60
                + f"\n\n第{n}章 {title}\n正文{n}。\n"
            )
        diagnostics = []
        chapters = parse_chapters(text, diagnostics=diagnostics)
        self.assertEqual([(c.number, c.volume) for c in chapters], [(61, 1), (62, 1)])
        # Volume marker lines remain documented body/structural paragraphs;
        # only the embedded duplicate heading is removed.
        self.assertEqual(
            [c.paragraphs for c in chapters], [["第一卷 起", "正文61。"], ["第一卷 起", "正文62。"]]
        )
        self.assertEqual([c.volume for c in chapters], [1, 1])
        self.assertTrue(sum(d["reason"] == "duplicate_embedded_heading" for d in diagnostics) == 2)

    def test_exporter_mismatches_stay_fatal(self):
        heading = "第62章 秦舒曼恢复身体"
        for text in (
            # Conflicting nested number.
            "Chapter 62: 第61章 标题\n" + "-" * 60 + f"\n{heading}\n正文。",
            # Wrapper and embedded titles disagree.
            "Chapter 62: 第62章 另一个标题\n" + "-" * 60 + f"\n{heading}\n正文。",
            # Vietnamese-family boundary never matches the Chinese wrapper rule.
            "Chương 62: 标题\n" + "-" * 60 + f"\n{heading}\n正文。",
        ):
            detail = self.assert_failure(text, "duplicate_chapter")
            self.assertEqual(detail["chapter"], 62)
        # Nested number disagrees with the wrapper itself: the embedded heading
        # becomes chapter 63, leaving 62 bodyless — still fatal, never silent.
        self.assert_failure(
            "Chapter 62: 第63章 标题\n" + "-" * 60 + "\n第63章 标题\n正文。", "empty_chapter_body"
        )

    def test_meaningful_fragment_before_repeat_is_fatal(self):
        for between in ("更新提示", "他走了。", "真正的正文。" * 50):
            self.assert_failure(
                f"Chapter 1: 第1章 开始\n{between}\n第1章 开始\n正文。", "duplicate_chapter"
            )

    def test_generic_structural_duplicate_still_kept_as_prose(self):
        diagnostics = []
        text = "第1章 开始\n第1章 开始\n正文。"
        chapters = parse_chapters(text, diagnostics=diagnostics)
        self.assertEqual(chapters[0].paragraphs, ["第1章 开始", "正文。"])
        self.assertTrue(any(d["reason"] == "duplicate_embedded_heading" for d in diagnostics))

    def test_many_decorative_lines_not_a_tiny_line_window(self):
        text = "Chapter 1: 第1章 开始\n" + "\n\n＊＊＊\n———\n" * 20 + "第1章 开始\n正文。"
        chapters = parse_chapters(text)
        self.assertEqual(len(chapters), 1)
        self.assertNotIn("第1章 开始", chapters[0].paragraphs)

    def test_matching_small_fragment_requires_wrapper_evidence(self):
        self.assert_failure("第1章 开始\n更新提示\n第1章 开始\n正文。", "duplicate_chapter")
        self.assert_failure(
            "Chapter 1: 第1章 开始\n他走了。\n第1章 开始\n正文。", "duplicate_chapter"
        )

    def test_real_duplicates_conflicting_titles_and_backward_order(self):
        self.assert_failure(book([1, 1], body="真正的正文。" * 50), "duplicate_chapter")
        self.assert_failure("Chapter 1: AAA\nChapter 1: BBB\n正文。", "duplicate_chapter")
        for numbers in ([1, 3, 2], [1, 2, 7, 3, 8]):
            self.assert_failure(book(numbers), "backward_numbering_without_volume_boundary")

    def test_bodyless_and_decorations_not_prose(self):
        for text in (
            "Chapter 1\nChapter 3\nChapter 5",
            "Chapter 1\n―――\n。。。",
            "Chapter 1: 第1章 开始\n第1章 开始",
        ):
            self.assert_failure(text, "empty_chapter_body")
        self.assertTrue(has_meaningful_body_content("“嗯。”"))
        self.assertFalse(has_meaningful_body_content("＊＊＊ —— …"))

    def test_volume_restarts_and_languages(self):
        for first, second in (
            ("卷一", "卷二"),
            ("第一卷", "第二卷"),
            ("Volume 1", "Volume 2"),
            ("Quyển 1", "Quyển 2"),
        ):
            diagnostics = []
            text = f"{first}\n第1章\n正文。\n第3章\n正文。\n{second}\n第1章\n新正文。"
            chapters = parse_chapters(text, diagnostics=diagnostics)
            self.assertEqual([c.key for c in chapters], ["v1-c1", "v1-c3", "v2-c1"])
            self.assertEqual([c.number for c in chapters], [1, 3, 1])
            self.assertIn(second, chapters[-1].paragraphs)
            self.assertIn("volume_numbering_restart", [d["reason"] for d in diagnostics])

    def test_volume_labels_do_not_blindly_reset(self):
        text = "Volume 1\nChapter 1\n正文。\nVolume 1\nChapter 2\n正文。"
        self.assertEqual([c.key for c in parse_chapters(text)], ["v1-c1", "v1-c2"])
        self.assert_failure(text.replace("Chapter 2", "Chapter 1"), "duplicate_chapter")
        self.assert_failure(
            "Volume 2\nChapter 3\n正文。\nVolume 1\nChapter 4\n正文。", "volume_order_invalid"
        )
        self.assert_failure(
            "Chapter 3\n正文。\nVolume 2\n这一卷说的是往事。\nChapter 1\n正文。",
            "backward_numbering_without_volume_boundary",
        )

    def test_toc_labeled_and_unlabeled(self):
        for label in ("目录\n", "目錄\n", "Contents\n", "Mục lục\n", ""):
            text = label + "第1章 AAA\n第2章 BBB\n第3章 CCC\n\n" + book([1, 2, 3], "第{}章")
            diagnostics = []
            chapters = parse_chapters(text, diagnostics=diagnostics)
            self.assertEqual([c.number for c in chapters], [1, 2, 3])
            self.assertEqual(sum(d["reason"] == "toc_heading_skipped" for d in diagnostics), 3)
            self.assertTrue(all(c.paragraphs == ["他走了。"] for c in chapters))

    def test_toc_plus_export_duplicates_preserve_real_wrapper(self):
        text = "目录\n第1章 AAA\n第2章 BBB\n第3章 CCC\n"
        text += "\n".join(
            f"Chapter {n}: 第{n}章 {title}\n第{n}章 {title}\n正文。"
            for n, title in ((1, "AAA"), (2, "BBB"), (3, "CCC"))
        )
        chapters = parse_chapters(text)
        self.assertEqual([c.number for c in chapters], [1, 2, 3])
        self.assertEqual(chapters[0].title, "Chapter 1: 第1章 AAA")
        self.assertEqual(chapters[0].source_line, 5)
        self.assertEqual(chapters[0].paragraphs, ["正文。"])

    def test_toc_only_is_not_a_novel_and_prose_not_discarded(self):
        self.assert_failure("目录\n第1章 AAA\n第2章 BBB\n第3章 CCC", "empty_chapter_body")
        self.assert_failure(
            "目录\n不应消失的前言。\n第1章 A\n第2章 B\n第3章 C\n" + book([1, 2, 3], "第{}章"),
            "unheaded_content",
        )
        self.assert_failure("lost prose\n" + book([1]), "unheaded_content")

    def test_export_metadata_and_inline_references(self):
        text = "Book title\nSource: https://example.test\nChapters: 2\n+++++\n" + book([1, 2])
        self.assertEqual(len(parse_chapters(text)), 2)
        text = "Chapter 7\n正文。\nChapter 3 was mentioned in the letter.\n第2章中记载了他的名字。\nChapter 8\n正文。"
        chapters = parse_chapters(text)
        self.assertEqual([c.number for c in chapters], [7, 8])
        self.assertIn("Chapter 3 was mentioned in the letter.", chapters[0].paragraphs)
        self.assertIn("第2章中记载了他的名字。", chapters[0].paragraphs)

    def test_precise_lines_and_encoding(self):
        detail = self.assert_failure(
            "\ufeffChapter 3\r\n正文。\r\n\r\nChapter 2\r\n正文。",
            "backward_numbering_without_volume_boundary",
            "VIETPHRASE",
        )
        self.assertEqual(
            (
                detail["line"],
                detail["heading"],
                detail["chapter"],
                detail["previous_chapter"],
                detail["previous_line"],
            ),
            (4, "Chapter 2", 2, 3, 1),
        )
        self.assertEqual(self.assert_failure("Chapter 1\n坏\ufffd", "invalid_encoding")["line"], 2)

    def test_independent_titles_gaps_and_volume_pairing(self):
        pairs = validate_inputs(
            book([15, 17], "第{}章 夜色"), book([15, 17], "Chương {}: Dạ sắc", "Hắn đi rồi.")
        )
        self.assertEqual([r.number for r, _ in pairs], [15, 17])
        pairs = validate_inputs(
            "第一卷\n第1章\n正文。\n第二卷\n第1章\n正文。",
            "Quyển 1\nChương 1\nVăn.\nQuyển 2\nChương 1\nVăn.",
        )
        self.assertEqual([r.key for r, _ in pairs], ["v1-c1", "v2-c1"])
        pairs = validate_inputs("Volume 2\n" + book([3, 5]), book([3, 5], body="Văn."))
        self.assertEqual([(r.volume, v.volume) for r, v in pairs], [(2, 2), (2, 2)])

    def test_alignment_mismatch_never_shifts(self):
        with self.assertRaises(ChapterValidationError) as caught:
            validate_inputs(book([1, 3, 5]), book([1, 5], body="Văn."))
        detail = caught.exception.detail
        self.assertEqual(detail["reason"], "chapter_identity_mismatch")
        self.assertEqual(detail["chapter"], 5)
        self.assertEqual(detail["counterpart"]["chapter"], 3)
        self.assertIsNotNone(detail["line"])
        with self.assertRaises(ChapterValidationError) as caught:
            validate_inputs(book([1, 3]), book([1], body="Văn."))
        self.assertEqual(caught.exception.detail["reason"], "chapter_count_mismatch")
        with self.assertRaises(ChapterValidationError):
            validate_inputs("Volume 1\n" + book([1]), "Volume 2\n" + book([1], body="Văn."))
        with self.assertRaises(ChapterValidationError):
            validate_inputs(
                "Volume 1\n" + book([1]) + "\nVolume 2\n" + book([1]), book([1, 1], body="Văn.")
            )

    def test_duplicate_han_heading_does_not_satisfy_raw_source(self):
        raw = parse_chapters("Chapter 1: 第1章 开始\n第1章 开始\nOnly English prose.")
        vp = parse_chapters("Chương 1\nVăn.", "VIETPHRASE")
        with self.assertRaises(ChapterValidationError) as caught:
            pair_chapters(raw, vp)
        self.assertEqual(caught.exception.detail["reason"], "missing_chinese_source")
        self.assertNotIn("meaningful_text", raw[0].model_dump())
        self.assertNotIn("source_line", raw[0].model_dump())


class PartialRangeHeadingTests(unittest.TestCase):
    """A file may begin at any positive chapter; chapter 1 is not special."""

    def assert_failure(self, text, reason, label="RAW"):
        with self.assertRaises(ChapterValidationError) as caught:
            parse_chapters(text, label)
        self.assertEqual(caught.exception.detail["reason"], reason)
        self.assertEqual(caught.exception.detail["input"], label)
        return caught.exception.detail

    def test_chinese_range_beginning_at_101(self):
        text = "-" * 60 + "\n第101章 标题\n正文\n" + "-" * 60 + "\n第102章 标题\n正文"
        chapters = parse_chapters(text)
        self.assertEqual([c.number for c in chapters], [101, 102])
        self.assertEqual(chapters[0].title, "第101章 标题")

    def test_chinese_range_beginning_at_501(self):
        self.assertEqual([c.number for c in parse_chapters("第501章 标题\n正文")], [501])

    def test_english_partial_range(self):
        text = "Chapter 101: Title\nbody\n\nChapter 102: Title\nbody"
        self.assertEqual([c.number for c in parse_chapters(text)], [101, 102])

    def test_vietnamese_partial_range(self):
        text = "Chương 101: Tiêu đề\nnội dung"
        self.assertEqual([c.number for c in parse_chapters(text, "VIETPHRASE")], [101])

    def test_front_matter_before_chapter_101(self):
        text = (
            "book title\nauthor\n\nSource: https://example.test\nTags: 历史\n"
            "Chapters: 100\n\n" + "+" * 60 + "\n\n"
            + "-" * 60 + "\n第101章 标题\n正文"
        )
        chapters = parse_chapters(text)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].number, 101)

    def test_no_headings_reports_file_level_failure(self):
        detail = self.assert_failure(
            "book title\nauthor\nnormal body text", "no_chapter_headings", "VIETPHRASE"
        )
        self.assertEqual(detail["severity"], "FATAL")
        self.assertIsNone(detail["line"])
        self.assertIsNone(detail["heading"])
        self.assertIn("No valid chapter heading found", detail["message"])
        self.assertIn("Thứ 101 chương", detail["message"])
        self.assertNotIn("required: 第1章", detail["message"])

    def test_chapter_one_still_works(self):
        self.assertEqual([c.number for c in parse_chapters("第1章 标题\n正文")], [1])

    def test_large_chapter_numbers(self):
        self.assertEqual([c.number for c in parse_chapters("第1234章 标题\n正文")], [1234])

    def test_vietphrase_accepts_the_same_chinese_heading_as_raw(self):
        text = "第101章 标题\n正文"
        self.assertEqual([c.number for c in parse_chapters(text, "RAW")], [101])
        self.assertEqual([c.number for c in parse_chapters(text, "VIETPHRASE")], [101])

    def test_duplicate_detection_remains_intact(self):
        self.assert_failure("第101章 A\n正文\n第101章 B\n正文", "duplicate_chapter")

    def test_partial_range_alignment_compares_identities(self):
        raw = "第101章 开始\n他走了。\n\n第102章 结束\n她来了。"
        vp = "第101章 ...\nHắn đi rồi.\n\n第102章 ...\nNàng tới rồi."
        pairs = validate_inputs(raw, vp)
        self.assertEqual([r.number for r, _ in pairs], [101, 102])
        self.assertEqual([v.number for _, v in pairs], [101, 102])

    def test_heading_forms_do_not_regress(self):
        for heading in ("第 101 章", "第１０１章", "第一百零一章"):
            with self.subTest(heading=heading):
                self.assertEqual([c.number for c in parse_chapters(heading + " 标题\n正文")], [101])

    def test_canonical_parser_normalizes_positive_numbers(self):
        for text, number, family in (
            ("第101章 明摄宗张居正", 101, "han"),
            ("第 1201 章 标题", 1201, "han"),
            ("Chapter 101: Title", 101, "english"),
            ("Chapter 501 - Title", 501, "english"),
            ("Chương 101: Tiêu đề", 101, "vietnamese"),
        ):
            with self.subTest(text=text):
                heading = parse_chapter_heading(text)
                self.assertIsNotNone(heading)
                self.assertEqual(heading.number, number)
                self.assertEqual(heading.family, family)
                self.assertEqual(heading.raw, text)
        self.assertIsNone(parse_chapter_heading("第0章 序"))
        self.assertIsNone(parse_chapter_heading("normal body text"))


class VietPhraseHeadingTests(unittest.TestCase):
    """VietPhrase.app renders ``第101章 <title>`` as ``Thứ 101 chương <title>``."""

    def assert_failure(self, text, reason, label="RAW"):
        with self.assertRaises(ChapterValidationError) as caught:
            parse_chapters(text, label)
        self.assertEqual(caught.exception.detail["reason"], reason)
        return caught.exception.detail

    def test_thu_chuong_heading_forms(self):
        for text, number in (
            ("Thứ 101 chương minh nhiếp tông Trương Cư Chính", 101),
            ("thứ 501 chương tiêu đề", 501),
            ("Thứ 1234 chương abc", 1234),
            ("THỨ 101 CHƯƠNG abc", 101),
            ("Thứ 1 chương mở đầu", 1),
        ):
            with self.subTest(text=text):
                self.assertEqual([c.number for c in parse_chapters(text + "\nNội dung.")], [number])

    def test_canonical_parser_normalizes_vietphrase_headings(self):
        heading = parse_chapter_heading("Thứ 101 chương minh nhiếp tông Trương Cư Chính")
        self.assertEqual(heading.number, 101)
        self.assertEqual(heading.family, "vietnamese")
        self.assertEqual(heading.raw, "Thứ 101 chương minh nhiếp tông Trương Cư Chính")
        # The rendered text is never rewritten into another heading form.
        self.assertEqual(heading.title, " minh nhiếp tông Trương Cư Chính")

    def test_vietphrase_number_is_never_renumbered(self):
        text = "Thứ 101 chương a\nNội dung.\n\nThứ 102 chương b\nNội dung."
        self.assertEqual([c.number for c in parse_chapters(text, "VIETPHRASE")], [101, 102])

    def test_real_vietphrase_file_after_front_matter(self):
        text = (
            "朕真的不务正业\nby 吾谁与归\n\nSource: https://example.test\nTags: 历史\n"
            "Volumes: 0\nChapters: 100\n\n" + "+" * 60 + "\n\n"
            + "-" * 60 + "\nThứ 101 chương minh nhiếp tông Trương Cư Chính\nHắn đi rồi."
        )
        self.assertEqual([c.number for c in parse_chapters(text, "VIETPHRASE")], [101])

    def test_raw_and_vietphrase_share_one_parser_for_alignment(self):
        raw = "第101章 明摄宗张居正\n他走了。"
        vp = "Thứ 101 chương minh nhiếp tông Trương Cư Chính\nHắn đi rồi."
        self.assertEqual([c.number for c in parse_chapters(raw, "RAW")], [101])
        self.assertEqual([c.number for c in parse_chapters(vp, "VIETPHRASE")], [101])
        pairs = validate_inputs(raw, vp)
        self.assertEqual([(r.number, v.number) for r, v in pairs], [(101, 101)])

    def test_duplicate_vietphrase_headings_stay_fatal(self):
        self.assert_failure(
            "Thứ 101 chương A\nNội dung.\nThứ 101 chương B\nNội dung khác.",
            "duplicate_chapter",
            "VIETPHRASE",
        )

    def test_decreasing_vietphrase_headings_stay_fatal(self):
        detail = self.assert_failure(
            "Thứ 102 chương A\nNội dung.\n\nThứ 101 chương B\nNội dung.",
            "backward_numbering_without_volume_boundary",
            "VIETPHRASE",
        )
        self.assertEqual((detail["chapter"], detail["previous_chapter"]), (101, 102))


if __name__ == "__main__":
    unittest.main()
