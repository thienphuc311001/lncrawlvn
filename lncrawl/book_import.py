"""Offline EPUB/TXT imports, including independent books inside ZIP uploads.

Archives are inspected before reads; nothing is extracted by archive paths.
Imported books are staged and published atomically with independent identifiers.
"""

import io
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, List, Tuple
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Comment, Doctype, ProcessingInstruction
from lxml import etree

from .library import Library
from .utils.file_tools import atomic_write, safe_filename

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000
MAX_COMPRESSION_RATIO = 200

_HEADING = re.compile(
    r"^\s*(?:(?:chương|chuong|chapter|chap\.?|book|volume|quyển|quyen)\s+(?:(?:thứ|thu)\s+)?"
    r"(?:\d+|[ivxlcdm]+)\b|第\s*[零〇一二两兩三四五六七八九十百千万萬億亿\d]+\s*[章节節卷回]).*$",
    re.IGNORECASE,
)
_ALLOWED_TAGS = frozenset(
    "p div span br hr h1 h2 h3 h4 h5 h6 blockquote pre code em strong b i u s "
    "small sub sup ul ol li dl dt dd table thead tbody tfoot tr th td ruby rt rp a".split()
)
_DROP_TAGS = frozenset(
    "script style iframe frame frameset object embed applet svg math link meta base "
    "form input button select textarea audio video source track canvas template noscript".split()
)


class _Budget:
    def __init__(self) -> None:
        self.remaining = MAX_EXPANDED_BYTES
        self.entries = 0

    def reserve(self, size: int) -> None:
        if size < 0 or size > self.remaining:
            raise ValueError("Expanded content exceeds the 200 MiB request limit")
        self.remaining -= size

    def archive(self, archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
        infos = archive.infolist()
        self.entries += len(infos)
        if self.entries > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Archive entry count exceeds the request limit")
        return infos


def _safe_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    parts = name.rstrip("/").split("/")
    if (
        not name or "\x00" in info.orig_filename or "\\" in name
        or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise ValueError("Unsafe archive member path")
    mode = info.external_attr >> 16
    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ValueError("Archive links and special files are not supported")
    if info.flag_bits & 1:
        raise ValueError("Encrypted archive entries are not supported")
    if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO:
        raise ValueError("Archive compression ratio is unsafe")


def _ignored(name: str) -> bool:
    return any(part == "__MACOSX" or part.startswith(".") for part in PurePosixPath(name).parts)


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info) as source:
        content = source.read(info.file_size + 1)
    if len(content) != info.file_size:
        raise ValueError("Archive member size does not match its directory entry")
    return content


def _sanitize_html(content: Any) -> str:
    # The standard-library HTML parser never resolves external entities or URLs.
    soup = BeautifulSoup(content, "html.parser")
    for node in list(soup.find_all(string=lambda value: isinstance(value, (Comment, Doctype, ProcessingInstruction)))):
        node.extract()
    for node in list(soup.find_all(True)):
        if node.name in _DROP_TAGS:
            node.decompose()
    root = soup.body if soup.body is not None else soup
    for node in list(root.find_all(True)):
        if node.name == "img":
            alt = node.get("alt", "")
            node.replace_with(str(alt))
        elif node.name not in _ALLOWED_TAGS:
            node.unwrap()
        else:
            # No URLs, styling, events, namespaces or resource-bearing attributes.
            node.attrs = {}
    return "".join(str(node) for node in root.contents).strip()



def _parse_txt(data: bytes, filename: str) -> Tuple[dict, List[dict]]:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text or not text.strip():
        raise ValueError("TXT must contain readable UTF-8 or BOM-marked UTF-16 text")
    title = PurePosixPath(filename).stem.strip() or "Untitled"
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if _HEADING.match(line)]
    sections = []
    if not starts:
        sections.append((title, text))
    else:
        if "\n".join(lines[:starts[0]]).strip():
            sections.append((title, "\n".join(lines[:starts[0]])))
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            sections.append((lines[start].strip(), "\n".join(lines[start:end])))
    chapters = []
    for chapter_title, body in sections:
        # Preserve line breaks and all prose, including source chapter headings.
        paragraphs = re.split(r"\n[ \t]*\n", body)
        html = "".join("<p>" + escape(paragraph).replace("\n", "<br/>") + "</p>" for paragraph in paragraphs if paragraph)
        chapters.append({"id": len(chapters) + 1, "title": chapter_title, "url": "", "body": html})
    return {"title": title, "url": "", "author": "", "tags": []}, chapters


def _xml(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    if any(isinstance(node, etree._Entity) for node in root.iter()):
        raise ValueError("XML entities are not supported")
    return root


def _local(node) -> str:
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def _reference(base: str, href: str) -> str:
    parsed = urlsplit(href)
    path = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("EPUB references must remain inside the archive")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if joined in (".", "..") or joined.startswith("../"):
        raise ValueError("EPUB reference escapes the archive")
    return joined


def _parse_epub(data: bytes, filename: str, budget: _Budget) -> Tuple[dict, List[dict]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = budget.archive(archive)
        members = {}
        for info in infos:
            _safe_member(info)
            if info.filename in members:
                raise ValueError("Duplicate EPUB archive member")
            members[info.filename] = info
        budget.reserve(sum(info.file_size for info in infos))
        read_members = set()

        def read(name: str) -> bytes:
            if name not in members or members[name].is_dir():
                raise ValueError(f"Missing EPUB member: {name}")
            # Repeated spine references must not bypass the expansion budget.
            if name in read_members:
                budget.reserve(members[name].file_size)
            read_members.add(name)
            return _read_member(archive, members[name])

        container = _xml(read("META-INF/container.xml"))
        rootfile = next((node for node in container.iter() if _local(node) == "rootfile"), None)
        if rootfile is None:
            raise ValueError("EPUB has no package document")
        package_path = _reference("", rootfile.get("full-path", ""))
        package = _xml(read(package_path))
        metadata_node = next((node for node in package if _local(node) == "metadata"), None)
        values = {}
        if metadata_node is not None:
            for node in metadata_node:
                values.setdefault(_local(node), []).append("".join(node.itertext()).strip())
        metadata = {
            "title": next(iter(values.get("title", [])), "") or PurePosixPath(filename).stem.strip() or "Untitled",
            "author": ", ".join(values.get("creator", [])),
            "language": next(iter(values.get("language", [])), None),
            "synopsis": _sanitize_html(next(iter(values.get("description", [])), "")),
            "tags": values.get("subject", []),
            "url": "",
            "cover_url": "",
        }
        manifest_node = next((node for node in package if _local(node) == "manifest"), None)
        spine = next((node for node in package if _local(node) == "spine"), None)
        if manifest_node is None or spine is None:
            raise ValueError("EPUB must contain a manifest and reading spine")
        manifest = {}
        for item in manifest_node:
            if _local(item) != "item":
                continue
            item_id = item.get("id")
            if not item_id or item_id in manifest:
                raise ValueError("EPUB manifest identifiers must be unique")
            manifest[item_id] = (_reference(package_path, item.get("href", "")), item)
        titles = {}
        for path, item in manifest.values():
            if "nav" in item.get("properties", "").split():
                nav = BeautifulSoup(read(path), "html.parser")
                tocs = [node for node in nav.find_all("nav") if "toc" in str(node.get("epub:type", node.get("type", ""))).split()]
                for section in tocs:
                    for link in section.find_all("a", href=True):
                        target = _reference(path, link["href"])
                        titles.setdefault(target, link.get_text(" ", strip=True))
        ncx = manifest.get(spine.get("toc"))
        if ncx:
            ncx_path, _ = ncx
            for point in _xml(read(ncx_path)).iter():
                if _local(point) != "navPoint":
                    continue
                label = next((node for node in point if _local(node) == "navLabel"), None)
                target = next((node for node in point if _local(node) == "content"), None)
                if label is not None and target is not None:
                    titles.setdefault(_reference(ncx_path, target.get("src", "")), "".join(label.itertext()).strip())
        chapters = []
        for itemref in spine:
            if _local(itemref) != "itemref":
                continue
            entry = manifest.get(itemref.get("idref"))
            if entry is None:
                raise ValueError("EPUB spine references a missing manifest item")
            path, item = entry
            if item.get("media-type") not in ("application/xhtml+xml", "text/html"):
                raise ValueError("EPUB spine contains an unsupported document type")
            if "nav" in item.get("properties", "").split():
                continue
            body = _sanitize_html(read(path))
            soup = BeautifulSoup(body, "html.parser")
            if not soup.get_text(" ", strip=True):
                continue  # Image-only cover pages have no importable prose.
            heading = soup.find(re.compile(r"^h[1-6]$"))
            title = titles.get(path) or (heading.get_text(" ", strip=True) if heading else "") or PurePosixPath(path).stem
            chapters.append({"id": len(chapters) + 1, "title": title, "url": "", "body": body})
        if not chapters:
            raise ValueError("EPUB contains no readable chapters")
        return metadata, chapters


def _persist(library: Library, metadata: dict, chapters: List[dict]) -> dict:
    library.root.mkdir(parents=True, exist_ok=True)
    book_id = (safe_filename(metadata["title"]) or "novel")[:80] + "-" + uuid.uuid4().hex
    saved_at = time.time()
    metadata = {**metadata, "book_id": book_id, "saved_at": saved_at,
                "toc": [{"id": chapter["id"], "title": chapter["title"], "url": ""} for chapter in chapters]}
    destination = library.root / book_id
    with tempfile.TemporaryDirectory(prefix=".import-", dir=library.root) as temporary:
        staging = Library(root=Path(temporary))
        for chapter in chapters:
            staging.save_chapter(book_id, chapter)
        with atomic_write(staging.root / book_id / "book.json", "w") as output:
            json.dump(metadata, output, ensure_ascii=False, indent=2)
        # Reserve the name without overwriting any existing directory, then
        # atomically replace our own empty reservation with the complete book.
        destination.mkdir()
        try:
            os.replace(staging.root / book_id, destination)
        except Exception:
            shutil.rmtree(destination)
            raise
    return {"book_id": book_id, "title": metadata["title"], "author": metadata.get("author", ""),
            "url": "", "cover_url": "", "total_chapters": len(chapters),
            "saved_count": len(chapters), "saved_at": saved_at}


def import_books(library: Library, files: List[Tuple[str, BinaryIO]]) -> dict:
    """Import seekable uploaded files, returning books/errors/skipped outcomes.

    The HTTP owner enforces the 50 MiB combined upload limit. Expanded archive
    bytes and entry count are shared across this entire call, including EPUBs
    nested inside outer ZIPs. A failed book never publishes partial metadata.
    """
    result: Dict[str, Any] = {"books": [], "errors": [], "skipped": []}
    budget = _Budget()

    def error(filename: str, exception: Exception) -> None:
        result["errors"].append({"filename": filename, "error": str(exception) or exception.__class__.__name__})

    def book(filename: str, data: bytes) -> None:
        try:
            if PurePosixPath(filename).suffix.lower() == ".epub":
                metadata, chapters = _parse_epub(data, filename, budget)
            else:
                metadata, chapters = _parse_txt(data, filename)
            result["books"].append(_persist(library, metadata, chapters))
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, etree.LxmlError) as exception:
            error(filename, exception)

    for filename, source in files:
        extension = PurePosixPath(filename).suffix.lower()
        if extension not in (".epub", ".txt", ".zip"):
            error(filename, ValueError("Supported upload formats are EPUB, TXT and ZIP"))
            continue
        try:
            source.seek(0)
            if extension != ".zip":
                data = source.read(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("Upload exceeds the 50 MiB limit")
                budget.reserve(len(data))
                book(filename, data)
                continue
            with zipfile.ZipFile(source) as archive:
                infos = budget.archive(archive)
                # Reject an oversized outer archive before decompressing any entry.
                budget.reserve(sum(
                    info.file_size for info in infos
                    if not info.is_dir() and not _ignored(info.filename)
                    and PurePosixPath(info.filename).suffix.lower() in (".epub", ".txt")
                ))
                seen = set()
                for info in infos:
                    member_name = f"{filename}/{info.filename}"
                    try:
                        _safe_member(info)
                        if info.filename in seen:
                            raise ValueError("Duplicate ZIP archive member")
                        seen.add(info.filename)
                        if info.is_dir() or _ignored(info.filename):
                            continue
                        if PurePosixPath(info.filename).suffix.lower() not in (".epub", ".txt"):
                            result["skipped"].append(member_name)
                            continue
                        book(member_name, _read_member(archive, info))
                    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exception:
                        error(member_name, exception)
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exception:
            error(filename, exception)
    return result
