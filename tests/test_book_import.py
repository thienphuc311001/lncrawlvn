import io
import stat
import struct
import tempfile
import unittest
import zipfile
from html import escape
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from lxml import etree

from lncrawl.book_import import import_books
from lncrawl.exceptions import LNException
from lncrawl.library import Library


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def epub_bytes(title="Imported Novel", author="Author", description="A description.", ncx=False):
    navigation = (
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        if not ncx else '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    )
    package = f'''<?xml version="1.0" encoding="utf-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>
        <dc:language>vi</dc:language><dc:subject>Fantasy</dc:subject>
        <dc:description>{escape(description)}</dc:description>
      </metadata>
      <manifest>
        <item id="second" href="text/a.xhtml" media-type="application/xhtml+xml"/>
        <item id="first" href="text/z.xhtml" media-type="application/xhtml+xml"/>
        {navigation}
      </manifest>
      <spine {'toc="ncx"' if ncx else ''}><itemref idref="first"/><itemref idref="second"/></spine>
    </package>'''
    first = '''<html><head><link rel="stylesheet" href="https://evil.test/style.css"/>
    <style>p {background:url(https://evil.test/image)}</style></head>
    <body onload="bad()"><h2>Original heading</h2><p onclick="bad()" style="background:url(x)">
    Nội dung đầu tiên. <strong>Readable emphasis.</strong> <a href="javascript:bad()">Link prose.</a></p>
    <img src="https://evil.test/image" onerror="bad()" alt="Illustration"/>
    <script>bad_script_marker()</script><iframe src="https://evil.test">hidden_frame_marker</iframe>
    <object data="https://evil.test">hidden_object_marker</object>
    <svg><script>bad_svg_marker()</script></svg><form><input name="password"/></form>
    <p data-value="x">Literal &lt;script&gt; prose remains text.</p></body></html>'''
    entries = [
        ("mimetype", "application/epub+zip"),
        ("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>'),
        ("OPS/package.opf", package),
        ("OPS/text/a.xhtml", '<html><body><h3>Second heading</h3><p>Nội dung cuối cùng. 中文正文。</p></body></html>'),
        ("OPS/text/z.xhtml", first),
    ]
    if ncx:
        entries.append(("OPS/toc.ncx", '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="one"><navLabel><text>First from NCX</text></navLabel><content src="text/z.xhtml#top"/></navPoint></navMap></ncx>'))
    else:
        entries.append(("OPS/nav.xhtml", '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="text/z.xhtml#top">First from TOC</a></li></ol></nav></body></html>'))
    return archive_bytes(entries)


def encrypted_zip():
    content = bytearray(archive_bytes([("encrypted.txt", "Secret prose.")]))
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    for offset in (local + 6, central + 8):
        flags = struct.unpack_from("<H", content, offset)[0]
        struct.pack_into("<H", content, offset, flags | 1)
    return bytes(content)


class BookImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library(root=Path(self.temporary.name) / "library")

    def import_files(self, *files):
        return import_books(self.library, [(name, io.BytesIO(content)) for name, content in files])

    def test_nested_mixed_zip_stores_each_novel_and_reports_partial_outcomes(self):
        bundle = archive_bytes([
            ("novels/", b""),
            ("novels/first.txt", "Chương 1: Mở đầu\nTruyện thứ nhất.".encode("utf-8-sig")),
            ("novels/deep/second.txt", "第十二章 开始\n第二本正文。".encode("utf-16")),
            ("novels/third.epub", epub_bytes()),
            ("novels/broken.epub", b"not an EPUB"),
            ("notes/readme.pdf", b"unsupported"),
            ("nested.zip", archive_bytes([("not-imported.txt", "Nested archive")])),
            ("__MACOSX/._first.txt", b"metadata"),
            ("novels/.hidden.txt", b"metadata"),
        ])
        result = self.import_files(("collection.zip", bundle), ("fourth.txt", b"Fourth story."))
        self.assertEqual(len(result["books"]), 4)
        self.assertEqual([item["filename"] for item in result["errors"]], ["collection.zip/novels/broken.epub"])
        self.assertEqual(result["skipped"], ["collection.zip/notes/readme.pdf", "collection.zip/nested.zip"])
        self.assertEqual(len(self.library.list_books()), 4)
        for summary in result["books"]:
            book = self.library.load_book(summary["book_id"])
            self.assertEqual(book["url"], "")
            self.assertEqual(book["saved_count"], book["total_chapters"])
            for fmt in ("txt", "epub"):
                output = self.library.export_file(summary["book_id"], fmt)
                self.assertTrue(output.is_file())
                self.assertEqual(output.suffix, "." + fmt)
                output.unlink()
        self.assertFalse(any(path.name.startswith(".import-") for path in self.library.root.iterdir()))

    def test_duplicate_titles_and_filenames_do_not_overwrite_crawled_or_imported_books(self):
        original_id = self.library.save_book_meta({"title": "Same", "url": "https://source.test", "toc": [{"id": 1, "title": "Original"}]})
        self.library.save_chapter(original_id, {"id": 1, "title": "Original", "body": "Original prose."})
        result = self.import_files(("Same.txt", b"First imported prose."), ("Same.txt", b"Second imported prose."))
        ids = [book["book_id"] for book in result["books"]]
        self.assertEqual(len(set([original_id] + ids)), 3)
        self.assertEqual(self.library.load_chapter(original_id, 1)["body"], "Original prose.")
        self.assertIn("First imported prose.", self.library.load_chapter(ids[0], 1)["body"])
        self.assertIn("Second imported prose.", self.library.load_chapter(ids[1], 1)["body"])
        self.assertEqual(len(self.library.list_books()), 3)

    def test_epub_spine_metadata_sanitization_and_both_complete_downloads(self):
        title = '<script>title attack</script> & Novel'
        author = '<img src="https://evil.test/author" onerror="bad()">Author'
        description = '<p onclick="bad()">Safe synopsis.</p><script>bad_description_marker()</script><img src="https://evil.test/cover"/>'
        result = self.import_files(("upload.epub", epub_bytes(title, author, description)))
        self.assertEqual(result["errors"], [])
        book_id = result["books"][0]["book_id"]
        book = self.library.load_book(book_id)
        self.assertEqual((book["title"], book["author"], book["language"], book["tags"]), (title, author, "vi", ["Fantasy"]))
        self.assertEqual([chapter["title"] for chapter in book["chapters"]], ["First from TOC", "Second heading"])
        first = self.library.load_chapter(book_id, 1)["body"]
        second = self.library.load_chapter(book_id, 2)["body"]
        self.assertIn("Nội dung đầu tiên.", first)
        self.assertIn("Nội dung cuối cùng.", second)
        self.assertIn("<strong>Readable emphasis.</strong>", first)
        for markup in (first, book["synopsis"]):
            soup = BeautifulSoup(markup, "html.parser")
            self.assertIsNone(soup.find(["script", "style", "iframe", "img", "object", "svg", "form", "input", "link"]))
            self.assertTrue(all(not tag.attrs for tag in soup.find_all(True)))
            self.assertNotIn("bad_script_marker", markup)
            self.assertNotIn("bad_description_marker", markup)
        txt = self.library.export_file(book_id, "txt")
        exported_text = txt.read_text(encoding="utf-8")
        self.assertLess(exported_text.index("Nội dung đầu tiên."), exported_text.index("Nội dung cuối cùng."))
        self.assertIn("中文正文。", exported_text)
        self.assertNotIn("Chương 1:", exported_text)
        epub = self.library.export_file(book_id, "epub")
        with zipfile.ZipFile(epub) as output:
            first_output = output.read("EPUB/chapter_00001.xhtml").decode()
            second_output = output.read("EPUB/chapter_00002.xhtml").decode()
            intro = output.read("EPUB/intro.xhtml").decode()
            self.assertIn("Nội dung đầu tiên.", first_output)
            self.assertIn("Nội dung cuối cùng.", second_output)
            for document in (first_output, second_output, intro):
                soup = BeautifulSoup(document, "html.parser")
                self.assertIsNone(soup.find(["script", "img", "iframe", "object", "svg"]))
                self.assertFalse(any(attr.startswith("on") for node in soup.find_all(True) for attr in node.attrs))
            self.assertNotIn("Chương 1:", first_output)
            package = etree.fromstring(output.read("EPUB/content.opf"))
            self.assertEqual(package.xpath("string(//*[local-name()='title'])"), title)
        txt.unlink()
        epub.unlink()

    def test_ncx_chapter_title_and_heading_fallback_follow_spine(self):
        result = self.import_files(("old.epub", epub_bytes(ncx=True)))
        self.assertEqual(result["errors"], [])
        book = self.library.load_book(result["books"][0]["book_id"])
        self.assertEqual([entry["title"] for entry in book["chapters"]], ["First from NCX", "Second heading"])

    def test_txt_unicode_headings_and_no_heading_prose_survive_both_formats(self):
        texts = [
            ("vietnamese.txt", "Lời mở đầu.\n\nChương 1: Bắt đầu\nĐêm trăng sáng.\n\nChương 2 - Tiếp tục\nNgày mới đến.", "utf-8-sig", 3),
            ("chinese.txt", "第十二章 初见\n中文故事。\n\n第十三章 重逢\n结尾。", "utf-16", 2),
            ("english.txt", "Chapter I: Start\nFirst prose.\n\nChapter 2: End\nLast prose.", "utf-8", 2),
            ("no-headings.txt", "Một dòng có <markup> & ký tự.\nDòng tiếp theo.\n\n最后一段。", "utf-8", 1),
            ("Chapter 99: offline.txt", "Chapter 1: Start\nFirst prose.\n" + "-" * 60 + "\n" + "+" * 60 + "\n\nChapter 2: End\nLast prose.", "utf-8", 2),
        ]
        for filename, text, encoding, count in texts:
            with self.subTest(filename=filename):
                result = self.import_files((filename, text.encode(encoding)))
                self.assertEqual(result["errors"], [])
                summary = result["books"][0]
                self.assertEqual(summary["total_chapters"], count)
                txt = self.library.export_file(summary["book_id"], "txt")
                exported = txt.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.strip():
                        self.assertIn(line, exported)
                epub = self.library.export_file(summary["book_id"], "epub")
                with zipfile.ZipFile(epub) as output:
                    chapters = [name for name in output.namelist() if "/chapter_" in name]
                    self.assertEqual(len(chapters), count)
                    prose = "\n".join(BeautifulSoup(output.read(name), "html.parser").get_text("\n") for name in chapters)
                    for line in text.splitlines():
                        if line.strip():
                            self.assertIn(line, prose)
                txt.unlink()
                epub.unlink()
        invalid = self.import_files(("binary.txt", b"\xff\x00\x81"), ("empty.txt", b" \n"))
        self.assertEqual(len(invalid["errors"]), 2)
        self.assertEqual(invalid["books"], [])

    def test_unsafe_paths_links_encryption_and_corruption_keep_valid_siblings(self):
        link = zipfile.ZipInfo("linked.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle = archive_bytes([
            ("../escape.txt", "Unsafe"), ("/absolute.txt", "Unsafe"),
            ("C:/drive.txt", "Unsafe"), ("back\\slash.txt", "Unsafe"),
            (link, "../../outside"), ("valid.txt", "Valid sibling prose."),
        ])
        result = self.import_files(("unsafe.zip", bundle), ("encrypted.zip", encrypted_zip()), ("broken.zip", b"broken ZIP"), ("unsupported.pdf", b"PDF"))
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(len(result["errors"]), 8)
        self.assertEqual(len(self.library.list_books()), 1)
        self.assertFalse((Path(self.temporary.name) / "escape.txt").exists())

    def test_expansion_limits_apply_before_decompression_and_include_inner_epub(self):
        bundle = archive_bytes([("oversized.txt", "Text " * 100)])
        with patch("lncrawl.book_import.MAX_EXPANDED_BYTES", 100):
            result = self.import_files(("large.zip", bundle), ("small.txt", b"Readable sibling."))
        self.assertEqual(len(result["books"]), 1)
        self.assertIn("Expanded content", result["errors"][0]["error"])
        inner = epub_bytes()
        with patch("lncrawl.book_import.MAX_EXPANDED_BYTES", len(inner) + 10):
            result = self.import_files(("inner.epub", inner))
        self.assertEqual(result["books"], [])
        self.assertIn("Expanded content", result["errors"][0]["error"])
        with patch("lncrawl.book_import.MAX_EXPANDED_BYTES", 25):
            result = self.import_files(("first.txt", b"A readable first story."), ("second.txt", b"Another readable story."))
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(result["errors"][0]["filename"], "second.txt")

    def test_entry_count_and_compression_ratio_limits(self):
        bundle = archive_bytes([("one.txt", "One."), ("two.txt", "Two.")])
        with patch("lncrawl.book_import.MAX_ARCHIVE_ENTRIES", 1):
            result = self.import_files(("too-many.zip", bundle), ("outside.txt", b"Outside story."))
        self.assertEqual(len(result["books"]), 1)
        self.assertIn("entry count", result["errors"][0]["error"])
        compressed = archive_bytes([("bomb.txt", b"a" * (2 * 1024 * 1024)), ("safe.txt", "Safe prose.")])
        result = self.import_files(("ratio.zip", compressed))
        self.assertEqual(len(result["books"]), 1)
        self.assertIn("compression ratio", result["errors"][0]["error"])
        inner = epub_bytes()
        with patch("lncrawl.book_import.MAX_ARCHIVE_ENTRIES", 2):
            result = self.import_files(("wrapper.zip", archive_bytes([("inner.epub", inner)])))
        self.assertEqual(result["books"], [])
        self.assertIn("entry count", result["errors"][0]["error"])

    def test_epub_unsafe_archive_and_external_xml_entities_are_rejected(self):
        malicious = archive_bytes([
            ("META-INF/container.xml", '<!DOCTYPE container [<!ENTITY attack SYSTEM "file:///etc/passwd">]><container><rootfile full-path="&attack;"/></container>'),
        ])
        unsafe = archive_bytes([("../outside.xhtml", "Unsafe")])
        result = self.import_files(("entity.epub", malicious), ("unsafe.epub", unsafe), ("valid.txt", b"Valid story."))
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(len(self.library.list_books()), 1)

    def test_utf16_big_endian_bom_and_traditional_chinese_headings(self):
        text = "第十二節 相遇\n繁體中文正文。\n\n第十三節 結束\n故事結尾。"
        result = self.import_files(("traditional.txt", b"\xfe\xff" + text.encode("utf-16-be")))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["books"][0]["total_chapters"], 2)
        output = self.library.export_file(result["books"][0]["book_id"], "txt")
        exported = output.read_text(encoding="utf-8")
        self.assertIn("繁體中文正文。", exported)
        self.assertIn("故事結尾。", exported)
        output.unlink()

    def test_failed_persistence_never_publishes_a_partial_book(self):
        original = Library.save_chapter

        def fail_second(library, book_id, chapter, overwrite=False):
            if chapter["id"] == 2:
                raise OSError("Storage unavailable")
            return original(library, book_id, chapter, overwrite)

        with patch.object(Library, "save_chapter", fail_second):
            result = self.import_files(("story.txt", b"Chapter 1\nFirst.\n\nChapter 2\nSecond."))
        self.assertEqual(result["books"], [])
        self.assertIn("Storage unavailable", result["errors"][0]["error"])
        self.assertEqual(self.library.list_books(), [])
        self.assertEqual(list(self.library.root.iterdir()), [])

    def test_failed_conversion_removes_its_unique_output_file(self):
        result = self.import_files(("story.txt", b"Readable story."))
        book_id = result["books"][0]["book_id"]
        with patch("lncrawl.library.make_text", side_effect=OSError("Conversion failed")):
            with self.assertRaises(OSError):
                self.library.export_file(book_id, "txt")
        directory = self.library.root / book_id / "exports" / "direct"
        self.assertEqual(list(directory.iterdir()), [])

    def test_download_paths_are_independent_and_chunk_cleanup_does_not_remove_them(self):
        result = self.import_files(("story.txt", b"Chapter 1\nFirst prose.\n\nChapter 2\nSecond prose."))
        book_id = result["books"][0]["book_id"]
        first = self.library.export_file(book_id, "txt")
        second = self.library.export_file(book_id, "txt")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        chunked = self.library.export_zip(book_id, "txt", per_file=1)
        self.assertTrue(chunked.is_file())
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        first.unlink()
        second.unlink()
        with self.assertRaises(LNException):
            self.library.export_file(book_id, "pdf")


if __name__ == "__main__":
    unittest.main()
