"""Chapter IDs, semantic alignment coverage, and safe-boundary chunking."""

import re

from .models import Chapter

HEADING = re.compile(
    r"^\s*(?:第\s*([零〇一二两三四五六七八九十百千万\d]+)\s*[章回节]|(?:chapter|chương)\s+(\d+)\b)(.*)$",
    re.I,
)
HAN = re.compile(r"[\u3400-\u9fff]")


def chapter_number(value):
    if value.isdigit():
        return int(value)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total = section = number = 0
    for c in value:
        if c in digits:
            number = digits[c]
        elif c == "万":
            total += (section + number) * 10000
            section = number = 0
        else:
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[c]
            number = 0
    return total + section + number


def parse_chapters(text):
    if "\ufffd" in text or "\x00" in text:
        raise ValueError("Input contains invalid text; provide valid UTF-8 files")
    chapters = []
    current = None
    preamble = []
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        match = HEADING.match(line)
        if match:
            current = Chapter(
                number=chapter_number(match[1] or match[2]), title=line, paragraphs=[]
            )
            chapters.append(current)
        elif line and not re.fullmatch(r"[-+=]{5,}", line):
            (current.paragraphs if current else preamble).append(line)
    if not chapters:
        raise ValueError("Chapter headings required: 第1章, Chapter 1, or Chương 1")
    numbers = [ch.number for ch in chapters]
    if len(numbers) != len(set(numbers)) or numbers != sorted(numbers):
        raise ValueError("Chapter numbers must be unique and increasing")
    if any(not ch.paragraphs for ch in chapters):
        raise ValueError("Empty chapter body")
    # Export metadata is allowed, arbitrary unheaded source content must not be dropped.
    if preamble and not any(p.startswith("Source:") for p in preamble):
        raise ValueError("Unheaded content before first chapter; remove metadata or add a heading")
    return chapters


def pair_chapters(raw, vp):
    if [c.number for c in raw] != [c.number for c in vp]:
        raise ValueError("RAW and VIETPHRASE chapter numbers/order do not match")
    if any(not HAN.search("".join(c.paragraphs)) for c in raw):
        raise ValueError("RAW chapters must contain Chinese source text")
    return list(zip(raw, vp))


def validate_alignment(alignment, raw, vp):
    if not alignment.confirmed:
        raise ValueError(f"Semantic alignment unconfirmed for chapter {raw.number}")
    for side, chapter in (("raw", raw), ("vp", vp)):
        flattened = [i for group in alignment.groups for i in getattr(group, side)]
        if flattened != list(range(len(chapter.paragraphs))):
            raise ValueError(f"Alignment must cover every {side} paragraph once, monotonically")


def make_chunks(alignment, raw, vp):
    size = sum(map(len, raw.paragraphs))
    target = (
        size
        if size < 7000
        else (size / 2 if size <= 12000 else size / 3 if size <= 18000 else 5000)
    )
    chunks, groups, length = [], [], 0
    for group in alignment.groups:
        groups.append(group)
        length += sum(len(raw.paragraphs[i]) for i in group.raw)
        if length >= target and group.safe_break:
            chunks.append(groups)
            groups, length = [], 0
    if groups:
        chunks.append(groups)
    result = []
    for groups in chunks:
        # Stable source paragraph IDs also anchor validation and targeted repair.
        result.append(
            {
                "raw": [{"id": i, "text": raw.paragraphs[i]} for g in groups for i in g.raw],
                "vp": [{"id": i, "text": vp.paragraphs[i]} for g in groups for i in g.vp],
            }
        )
    return result
