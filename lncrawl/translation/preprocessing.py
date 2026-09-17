"""Whole-batch terminology discovery, occurrence indexing and evidence selection: local only."""

import json
import re
import unicodedata
from collections import Counter, defaultdict

from .dictionary import quantity_source_problem, source_name, source_problem
from .models import Term

HAN_RUN = re.compile(r"[\u3400-\u9fff]+")
WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)
VP_COMMON = set(
    "là người đã đang của và với này kia một cái có không nói hỏi nhìn nghe đi đến rồi thì mà hắn nàng anh cô ông bà trưởng phó đội cục thành phố trợ lý phương pháp chén nhỏ vui".split()
)
# A deliberately small local reading lexicon permits safe lowercase VietPhrase
# names too. Unknown or colloquially translated given names stay semantic work.
GIVEN_READINGS = {
    "舒": "thư",
    "曼": "mạn",
    "菲": "phỉ",
    "玉": "ngọc",
    "洲": "châu",
    "灵": "linh",
    "霜": "sương",
    "小": "tiểu",
    "七": "thất",
    "六": "lục",
    "明": "minh",
    "华": "hoa",
    "雪": "tuyết",
    "云": "vân",
    "天": "thiên",
    "龙": "long",
    "海": "hải",
    "风": "phong",
    "月": "nguyệt",
    "雨": "vũ",
    "秋": "thu",
    "春": "xuân",
    "青": "thanh",
    "兰": "lan",
    "芳": "phương",
    "红": "hồng",
    "梅": "mai",
    "彩": "thải",
    "炎": "viêm",
    "火": "hỏa",
    "伟": "vĩ",
    "文": "văn",
    "武": "vũ",
    "杰": "kiệt",
    "峰": "phong",
}
SURNAMES = dict(
    pair.split(":")
    for pair in (
        "秦:Tần 邱:Khâu 阎:Diêm 唐:Đường 叶:Diệp 贾:Giả 李:Lý 王:Vương 张:Trương "
        "刘:Lưu 陈:Trần 杨:Dương 黄:Hoàng 赵:Triệu 周:Chu 吴:Ngô 徐:Từ 孙:Tôn 胡:Hồ "
        "朱:Chu 高:Cao 林:Lâm 何:Hà 郭:Quách 马:Mã 罗:La 梁:Lương 宋:Tống 郑:Trịnh "
        "谢:Tạ 韩:Hàn 曹:Tào 许:Hứa 沈:Thẩm 袁:Viên 冯:Phùng 苏:Tô 吕:Lữ 丁:Đinh "
        "任:Nhậm 姚:Diêu 傅:Phó 石:Thạch 陆:Lục 白:Bạch 魏:Ngụy 江:Giang 萧:Tiêu "
        "顾:Cố 方:Phương 杜:Đỗ 孟:Mạnh 齐:Tề 宁:Ninh 温:Ôn 祝:Chúc 龙:Long 蓝:Lam"
    ).split()
)
GRAMMAR = set(
    "的是了着在又也就说看向对他她我你它这那个一和与将到听不有没要让把很为被从则似既正能还但却着些们于以都只所更再已并而吗呢吧后前给及中内外"
)
PERSON_CUES = re.compile(
    r"^(?:说|问|答|笑|看|听|走|来|点头|摇头|皱眉|是|的|，|。|！|？|：|、|[“”])"
)
SUFFIXES = (
    "探查署",
    "政治部",
    "特勤部",
    "商会",
    "教会",
    "公司",
    "小队",
    "基地",
    "学院",
    "学府",
    "署长",
    "科长",
    "处长",
    "将军",
    "上校",
    "队长",
    "长官",
    "女士",
    "先生",
    "小姐",
    "王朝",
    "王国",
    "境界",
    "异能",
    "能力",
    "组织",
    "教派",
    "功法",
    "宗",
    "派",
    "城",
    "市",
    "宫",
    "殿",
    "术",
    "诀",
    "丹",
    "石",
    "剑",
    "刀",
)


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def representative_evidence(candidate, units, budget=2200):
    """First/last, diverse chapters, variants and address cues; never AI summaries."""
    occurrences = candidate["occurrences"]
    if not occurrences:
        return []
    chosen = [occurrences[0], occurrences[-1]]
    # Prioritize differing readings and explicit address contexts before filling
    # the remaining budget with evenly spaced chapters.
    seen_variants = set()
    for occurrence in occurrences:
        unit = units[occurrence["unit"]]
        if any(v not in seen_variants for v in occurrence.get("variants", [])):
            chosen.append(occurrence)
            seen_variants.update(occurrence.get("variants", []))
    address = next(
        (
            o
            for o in occurrences
            if re.search(r"老|小|阿|科长|署长|将军|先生|小姐|叫做|名叫", units[o["unit"]]["raw"])
        ),
        None,
    )
    if address:
        chosen.append(address)
    # Spread over the entire novel as well as preserving variant/first/last evidence.
    chosen += [occurrences[int((len(occurrences) - 1) * fraction / 5)] for fraction in range(1, 5)]
    seen_chapters = set()
    for occurrence in occurrences:
        chapter = units[occurrence["unit"]]["chapter_key"]
        if chapter not in seen_chapters:
            chosen.append(occurrence)
            seen_chapters.add(chapter)
    result, seen, size = [], set(), 0
    for occurrence in chosen:
        if occurrence["unit"] in seen:
            continue
        seen.add(occurrence["unit"])
        unit = units[occurrence["unit"]]
        offset = occurrence["positions"][0]
        item = {
            "chapter": unit["chapter_key"],
            "paragraph_ids": unit["paragraph_ids"],
            "raw": unit["raw"][max(0, offset - 100) : offset + len(candidate["source"]) + 180],
            "vp": unit["vp"][:420],
            "variants": occurrence.get("variants", []),
        }
        length = len(json.dumps(item, ensure_ascii=False))
        if result and size + length > budget:
            continue
        result.append(item)
        size += length
    return result


def build_index(pairs, alignments, inherited):
    units = []
    for raw, vp in pairs:
        units.append(
            {"chapter_key": raw.key, "paragraph_ids": [-1], "raw": raw.title, "vp": vp.title}
        )
        for group in alignments[raw.key].groups:
            units.append(
                {
                    "chapter_key": raw.key,
                    "paragraph_ids": group.raw,
                    "raw": "\n".join(raw.paragraphs[i] for i in group.raw),
                    "vp": "\n".join(vp.paragraphs[i] for i in group.vp),
                }
            )
    counts, reasons = Counter(), defaultdict(set)
    for unit in units:
        text = unit["raw"]
        for run in HAN_RUN.findall(text):
            for length in range(2, min(8, len(run)) + 1):
                counts.update(run[index : index + length] for index in range(len(run) - length + 1))
        for match in re.finditer(r"【([^】\n]{2,32})】|《([^》\n]{2,32})》", text):
            reasons[source_name(match.group(1) or match.group(2))].add("explicit_named_marker")
        for index, char in enumerate(text):
            if char not in SURNAMES:
                continue
            if (
                index
                and HAN_RUN.fullmatch(text[index - 1])
                and text[index - 1] not in GRAMMAR
                and not text[:index].endswith(
                    ("署长", "科长", "将军", "上校", "队长", "长官", "叫做", "名叫")
                )
            ):
                continue
            for length in (2, 3):
                source = text[index : index + length]
                if (
                    len(source) == length
                    and HAN_RUN.fullmatch(source)
                    and not set(source[1:]).intersection(GRAMMAR)
                    and PERSON_CUES.match(text[index + length :])
                ):
                    reasons[source].add("person_name_pattern")
        for match in re.finditer(
            "["
            + "".join(SURNAMES)
            + r"](?:副)?(?:署长|科长|处长|将军|上校|队长|长官|先生|小姐|掌门)",
            text,
        ):
            reasons[match.group()].add("named_title_or_address_form")
        for match in re.finditer("(?:老|小|阿)[" + "".join(SURNAMES) + "]", text):
            reasons[match.group()].add("possible_person_alias")
        for match in re.finditer(
            r"(?:名为|称为|代号为|命名为)[：:“\"‘「『]*([\u3400-\u9fff]{2,8})", text
        ):
            reasons[match.group(1)].add("explicit_naming_context")
    for source, count in counts.items():
        suffix = next((ending for ending in SUFFIXES if source.endswith(ending)), None)
        if count >= 2 and suffix and not set(source[: -len(suffix)]).intersection(GRAMMAR):
            reasons[source].add("repeated_entity_or_genre_suffix")
    # Repeated expressions qualify for review only with a naming/genre signal;
    # frequency alone cannot turn ordinary prose into canonical terminology.
    for term in inherited:
        for source in [term.source, *term.aliases, *term.forms]:
            reasons[source].add("inherited_dictionary")
    texts = [unit["raw"] for unit in units]
    rejected, candidates = {}, {}
    for source in sorted(reasons):
        issue = source_problem(source) or quantity_source_problem(source, texts)
        if re.search(r"[+＋]\d+|^\d+(?:天|小时|分钟|岁)(?:$|[（(])", source) and not any(
            "《" + source + "》" in text for text in texts
        ):
            issue = "dynamic status/duration value, not a stable named entity"
        if issue or (reasons[source] == {"person_name_pattern"} and counts[source] < 2):
            rejected[source] = issue or "unconfirmed single person-name pattern"
            continue
        candidates[source] = {
            "source": source,
            "reasons": sorted(reasons[source]),
            "frequency": 0,
            "occurrences": [],
            "vietphrase_variants": {},
        }
    # Remove lexical suffix fragments only when a longer candidate has exactly
    # the same whole-novel occurrence frequency. Preserve independently used names.
    fragments = set()
    for source in candidates:
        if candidates[source]["reasons"] != ["repeated_entity_or_genre_suffix"]:
            continue
        for longer in candidates:
            if source != longer and source in longer and counts[source] == counts[longer]:
                fragments.add(source)
                break
    for source in fragments:
        rejected[source] = "contained lexical fragment with no independent occurrence"
        del candidates[source]
    variants = {source: Counter() for source in candidates}
    by_length = defaultdict(set)
    special_sources = []
    for source in candidates:
        if HAN_RUN.fullmatch(source):
            by_length[len(source)].add(source)
        else:
            special_sources.append(source)
    for unit_id, unit in enumerate(units):
        found = defaultdict(list)
        for source in special_sources:
            found[source].extend(
                match.start() for match in re.finditer(re.escape(source), unit["raw"])
            )
        for run in HAN_RUN.finditer(unit["raw"]):
            value = run.group()
            for length, eligible in by_length.items():
                for index in range(len(value) - length + 1):
                    source = value[index : index + length]
                    if source in eligible:
                        found[source].append(run.start() + index)
        words = WORDS.findall(unicodedata.normalize("NFC", unit["vp"]))
        for source, positions in found.items():
            if not positions:
                continue
            proposals = []
            if "person_name_pattern" in candidates[source]["reasons"] and source[0] in SURNAMES:
                surname = normalized(SURNAMES[source[0]])
                for index, word in enumerate(words):
                    selected = words[index : index + len(source)]
                    if (
                        normalized(word) == surname
                        and len(selected) == len(source)
                        and not any(normalized(token) in VP_COMMON for token in selected[1:])
                    ):
                        proposals.append(" ".join(value.capitalize() for value in selected))
            if "explicit_named_marker" in candidates[source]["reasons"]:
                raw_markers = re.findall(r"【([^】]+)】|《([^》]+)》", unit["raw"])
                vp_markers = re.findall(r"【([^】]+)】|《([^》]+)》|«([^»]+)»", unit["vp"])
                raw_values = [a or b for a, b in raw_markers]
                if len(raw_values) == len(vp_markers) and raw_values.count(source) == 1:
                    proposals.append(
                        next(
                            value for value in vp_markers[raw_values.index(source)] if value
                        ).strip()
                    )
            proposals = sorted(set(proposals))
            variants[source].update(proposals)
            candidate = candidates[source]
            candidate["frequency"] += len(positions)
            candidate["occurrences"].append(
                {"unit": unit_id, "positions": positions, "variants": proposals}
            )
    for source, candidate in candidates.items():
        candidate["vietphrase_variants"] = dict(variants[source].most_common())
        candidate["representative_evidence"] = representative_evidence(candidate, units)
    return {"units": units, "candidates": candidates, "rejected": rejected}


def local_resolution(candidate):
    """Conservative stable full-name reading, provisional with no inferred alias/gender."""
    variants = candidate["vietphrase_variants"]
    if (
        candidate["reasons"] != ["person_name_pattern"]
        or len(candidate["occurrences"]) < 3
        or not variants
    ):
        return None
    translation, count = max(variants.items(), key=lambda pair: pair[1])
    readings = [
        SURNAMES.get(candidate["source"][0]),
        *[GIVEN_READINGS.get(c) for c in candidate["source"][1:]],
    ]
    if any(reading is None for reading in readings) or normalized(translation) != normalized(
        " ".join(readings)
    ):
        return None
    total = sum(variants.values())
    # Require distinct aligned contexts, a clear surname, one reading per unit,
    # and >=95% consensus. No numeric or character-offset RAW/VP matching.
    if (
        count / total < 0.95
        or count / len(candidate["occurrences"]) < 0.95
        or len(translation.split()) != len(candidate["source"])
    ):
        return None
    return Term(
        source=candidate["source"],
        translation=translation,
        type="character",
        status="provisional",
        gender="unknown",
        evidence=f"Local full-name pattern and {count}/{total} VietPhrase consensus; identity/gender not inferred",
    )


def batches(items, budget=18000):
    """Dynamic context-size batches; each source retains its own evidence/output."""
    page, size = [], 0
    for item in items:
        length = len(json.dumps(item, ensure_ascii=False))
        if length > budget:
            raise ValueError(
                f"Representative evidence exceeds resolver budget for {item['source']}"
            )
        if page and size + length > budget:
            yield page
            page, size = [], 0
        page.append(item)
        size += length
    if page:
        yield page
