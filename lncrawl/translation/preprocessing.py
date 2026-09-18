"""Whole-batch terminology discovery, occurrence indexing and evidence selection: local only."""

import json
import re
import unicodedata
from collections import Counter, defaultdict

from .dictionary import quantity_source_problem, source_name, source_problem
from .models import Term

HAN_RUN = re.compile(r"[\u3400-\u9fff]+")
WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)
# A deliberately small local reading lexicon permits safe lowercase VietPhrase
# names too. Unknown or colloquially translated given names stay semantic work.
GIVEN_READINGS = {
    "极": "cực",
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
    "政": "chính",
    "光": "quang",
    "石": "thạch",
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

# These are classification signals, not a blanket stop-list.  A phrase that
# is explicitly named, has a proved identity, or is inherited from the book
# dictionary is kept for semantic review even when one of its characters is a
# common word.
GENERIC_VERB_PHRASES = {"会派", "会让", "会把", "可以看", "能够看"}
GENERIC_COMMON_PHRASES = {
    "世界基石",
    "任意抓捕处长",
    "增进感情的方式",
    "方法",
    "方式",
    "感情",
    "基石",
}
DESCRIPTIVE_MARKERS = {
    "方式",
    "方法",
    "感情",
    "任意",
    "任何",
    "增进",
    "抓捕",
    "数量",
    "办法",
}
TITLE_PATTERN = re.compile(r"(?:副)?(?:署长|科长|处长|将军|上校|队长|长官|先生|小姐|掌门)$")


def classify_candidate(source, reasons, contexts, frequency=0, inherited=False):
    """Classify obvious ordinary-language candidates before resolver work.

    The classifier deliberately returns ``None`` for uncertain material.  A
    positive classification is only used for conservative local rejection or
    report-only bookkeeping; it never promotes a candidate into a dictionary
    entry.
    """
    if inherited:
        return None
    named_signals = {
        "explicit_named_marker",
        "explicit_naming_context",
        "possible_person_alias",
    }
    if named_signals.intersection(reasons):
        return None
    if source in GENERIC_VERB_PHRASES:
        return "verb_phrase", "ordinary verb phrase"
    if source in GENERIC_COMMON_PHRASES:
        return "common_noun", "ordinary compositional noun phrase"
    if re.match(r"^\d+(?:[-–~～]\d+)?(?:种|类|个|名|件|种)", source):
        return "descriptive_phrase", "numeric descriptive phrase"
    if any(marker in source for marker in DESCRIPTIVE_MARKERS) and not TITLE_PATTERN.search(source):
        return "descriptive_phrase", "compositional descriptive phrase"
    if TITLE_PATTERN.search(source):
        # A title/address remains a plausible candidate when RAW proves an
        # identity relationship.  Otherwise it is audit-only, never a new
        # canonical character identity.
        identity = re.compile(
            re.escape(source) + r".{0,18}(?:就是|是|名为|叫做|称为)[\u3400-\u9fff]{2,8}"
        )
        reverse = re.compile(
            r"[\u3400-\u9fff]{2,8}.{0,18}(?:就是|是|名为|叫做|称为)" + re.escape(source)
        )
        if not any(identity.search(context) or reverse.search(context) for context in contexts):
            return "character_form", "title/reference identity is not proven locally"
    # A suffix by itself is not enough evidence.  Keep uncertain novel terms
    # for the resolver; this is what protects short fictional concepts.
    return None


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
        # Capture only high-signal ordinary-language shapes for audit.  These
        # never enter the resolver and are not later treated as terminology.
        for match in re.finditer(
            r"\d+(?:[-–~～]\d+)?(?:种|类|个|名|件)[\u3400-\u9fff]{2,32}", text
        ):
            reasons[match.group()].add("numeric_descriptive_phrase")
        for phrase in GENERIC_VERB_PHRASES | GENERIC_COMMON_PHRASES:
            if phrase in text:
                reasons[phrase].add("local_generic_phrase")
        for match in re.finditer(
            r"(?:任意|任何)[\u3400-\u9fff]{1,12}(?:抓捕|派遣|调动|处理)[\u3400-\u9fff]{0,8}", text
        ):
            reasons[match.group()].add("compositional_descriptive_phrase")
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
    rejected, report_only, candidates = {}, {}, {}
    contexts = texts
    for source in sorted(reasons):
        issue = source_problem(source) or quantity_source_problem(source, texts)
        if re.search(r"[+＋]\d+|^\d+(?:天|小时|分钟|岁)(?:$|[（(])", source) and not any(
            "《" + source + "》" in text for text in texts
        ):
            issue = "dynamic status/duration value, not a stable named entity"
        classification = classify_candidate(
            source,
            reasons[source],
            contexts,
            counts[source],
            inherited=any(
                source in [term.source, *term.aliases, *term.forms] for term in inherited
            ),
        )
        if issue or classification or (
            reasons[source] == {"person_name_pattern"} and counts[source] < 2
        ):
            reason = issue or (
                classification[1] if classification else "unconfirmed single person-name pattern"
            )
            category = classification[0] if classification else (
                "common_noun" if issue == "ordinary vocabulary" else "unknown"
            )
            rejected[source] = reason
            report_only[source] = {
                "source": source,
                "classification": category,
                "status": "report_only",
                "state": "IGNORE",
                "enforceable": False,
                "reason": reason,
                "reasons": sorted(reasons[source]),
                "frequency": 0,
                "occurrences": [],
            }
            continue
        candidates[source] = {
            "source": source,
            "reasons": sorted(reasons[source]),
            "classification": (
                "character_name"
                if "person_name_pattern" in reasons[source]
                else "character_form"
                if "named_title_or_address_form" in reasons[source]
                else "proper_noun"
                if {
                    "explicit_named_marker",
                    "explicit_naming_context",
                    "repeated_entity_or_genre_suffix",
                }.intersection(reasons[source])
                else "unknown"
            ),
            "frequency": 0,
            "occurrences": [],
            "vietphrase_variants": {},
            "verified_mapping_count": 0,
            "dominant_translation": None,
            "dominant_confidence": 0.0,
            "mapping_coverage": 0.0,
            "proper_name_pattern_confidence": 0.0,
            "inherited_agreement": False,
            "source_conflicts": [],
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
        report_only[source] = {
            "source": source,
            "classification": "common_noun",
            "status": "report_only",
            "state": "IGNORE",
            "enforceable": False,
            "reason": rejected[source],
            "reasons": candidates[source]["reasons"],
            "frequency": 0,
            "occurrences": [],
        }
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
            expected_reading = (
                [SURNAMES.get(source[0]), *[GIVEN_READINGS.get(char) for char in source[1:]]]
                if source and source[0] in SURNAMES
                else [GIVEN_READINGS.get(char) for char in source]
            )
            if expected_reading and all(expected_reading):
                expected_words = [normalized(word) for word in expected_reading]
                vp_words = [normalized(word) for word in words]
                if any(
                    vp_words[index : index + len(expected_words)] == expected_words
                    for index in range(max(0, len(vp_words) - len(expected_words) + 1))
                ):
                    proposals.append(" ".join(value.capitalize() for value in expected_reading))
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
            for proposal in proposals:
                variants[source][proposal] += len(positions)
            candidate = candidates[source]
            candidate["frequency"] += len(positions)
            candidate["occurrences"].append(
                {"unit": unit_id, "positions": positions, "variants": proposals}
            )
    for source, candidate in candidates.items():
        candidate["vietphrase_variants"] = dict(variants[source].most_common())
        candidate["verified_mapping_count"] = sum(variants[source].values())
        if variants[source]:
            dominant, count = variants[source].most_common(1)[0]
            candidate["dominant_translation"] = dominant
            candidate["dominant_confidence"] = count / max(1, candidate["frequency"])
            candidate["mapping_coverage"] = candidate["verified_mapping_count"] / max(
                1, candidate["frequency"]
            )
        if "person_name_pattern" in candidate["reasons"]:
            candidate["proper_name_pattern_confidence"] = min(
                1.0, candidate["frequency"] / max(3, len(candidate["occurrences"]))
            )
        inherited_terms = [
            term for term in inherited
            if source in [term.source, *term.aliases, *term.forms]
        ]
        candidate["inherited_agreement"] = bool(
            inherited_terms
            and candidate.get("dominant_translation")
            and any(
                normalized(term.forms.get(source, term.translation))
                == normalized(candidate["dominant_translation"])
                for term in inherited_terms
            )
        )
        candidate["representative_evidence"] = representative_evidence(candidate, units)
    # Keep rejected/generic evidence separate from plausible resolver work.
    # This makes the distinction survive checkpoints and gives the report/UI a
    # useful audit trail without polluting the frozen enforceable namespace.
    for source, record in report_only.items():
        occurrences = []
        for unit_id, unit in enumerate(units):
            positions = [match.start() for match in re.finditer(re.escape(source), unit["raw"])]
            if positions:
                occurrences.append({"unit": unit_id, "positions": positions})
        record["occurrences"] = occurrences
        record["frequency"] = sum(len(item["positions"]) for item in occurrences)
    return {
        "units": units,
        "candidates": candidates,
        "rejected": rejected,
        "report_only": report_only,
        "metrics": {
            "raw_candidates": len(candidates) + len(report_only),
            "resolver_candidates": len(candidates),
            "locally_rejected_generic": len(report_only),
        },
    }


def local_resolution(candidate, policy=None):
    """Confirm only a conservative stable full-name reading."""
    variants = candidate["vietphrase_variants"]
    min_contexts = getattr(policy, "min_contexts", 3) if policy else 3
    min_dominance = getattr(policy, "min_dominance", 0.95) if policy else 0.95
    min_coverage = getattr(policy, "min_coverage", 0.95) if policy else 0.95
    if len(candidate["occurrences"]) < min_contexts or not variants:
        return None
    translation, count = max(variants.items(), key=lambda pair: pair[1])
    readings = (
        [SURNAMES.get(candidate["source"][0]), *[GIVEN_READINGS.get(c) for c in candidate["source"][1:]]]
        if candidate["source"][0] in SURNAMES
        else [GIVEN_READINGS.get(c) for c in candidate["source"]]
    )
    if any(reading is None for reading in readings) or normalized(translation) != normalized(
        " ".join(readings)
    ):
        return None
    total = sum(variants.values())
    # Require distinct aligned contexts, a clear surname, one reading per unit,
    # and >=95% consensus. No numeric or character-offset RAW/VP matching.
    if (
        count / total < min_dominance
        or candidate.get("mapping_coverage", 0) < min_coverage
        or len(translation.split()) != len(candidate["source"])
    ):
        return None
    is_person = "person_name_pattern" in candidate["reasons"]
    if not is_person:
        # A stable reading is useful evidence, but it does not establish that
        # an ordinary phrase is a named term.  Keep it out of the runtime
        # namespace until a complete confirmed decision exists.
        return None
    return Term(
        source=candidate["source"],
        translation=translation,
        type="character" if is_person else "unknown",
        # A deterministic full-name reading has already passed the local
        # evidence gates.  It is confirmed for runtime; uncertain candidates
        # return None and remain IGNORE diagnostics instead of provisional
        # enforcement.
        status="locked",
        gender="unknown",
        semantic_resolution="resolved",
        needs_review=False,
        resolution_reason=None,
        enforceable=True,
        confidence=count / max(1, total),
        resolver_source="local_consensus",
        evidence=f"Local reading and {count}/{total} VietPhrase consensus; identity/gender not inferred",
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
