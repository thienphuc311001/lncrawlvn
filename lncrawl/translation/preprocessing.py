"""Whole-batch terminology discovery, occurrence indexing and evidence selection: local only."""

import json
import re
import unicodedata
from collections import Counter, defaultdict

from . import style
from .dictionary import (
    REFERENCE_SUFFIX,
    reference_shape,
    quantity_source_problem,
    source_name,
    source_problem,
)
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
        "顾:Cố 方:Phương 杜:Đỗ 孟:Mạnh 齐:Tề 宁:Ninh 温:Ôn 祝:Chúc 龙:Long 蓝:Lam 万:Vạn"
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
    "大伴",
    "尚书",
    "帅",
    "缇帅",
    "侍班",
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
    "卫",
    "部",
    "司",
    "局",
    "院",
    "党",
    "营",
    "军",
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
    "枪",
    "印",
    "令",
    "符",
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
    "京城",
    "上殿",
    "三千石",
    "两块陨石",
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
# A locally discovered title/address shape is not a character identity.  It
# stays a report-only reference unless RAW proves the full identity; a bare
# kinship address such as 唐姐 must never become its own dictionary character.
TITLE_PATTERN = re.compile(
    r"(?:副)?(?:"
    + "|".join(
        sorted(
            {
                "署长",
                "科长",
                "处长",
                "将军",
                "上校",
                "队长",
                "长官",
                "先生",
                "小姐",
                "掌门",
                "师兄",
                "师姐",
                "师弟",
                "师妹",
                "前辈",
                "大人",
                "阁下",
                "殿下",
                *style.ADDRESS_SPECS,
                *style.KINSHIP_SUFFIXES,
            },
            key=len,
            reverse=True,
        )
    )
    + r")$"
)

# Candidate classes are deliberately explicit.  They are used both by local
# filtering and by the resolver prompt; character ownership is only one of
# these policies, never the default for every Chinese span.
ENTITY_CLASS_POLICIES = {
    "character": {"term_type": "character", "needs_identity_proof": True},
    "character_reference": {"term_type": "character", "needs_identity_proof": True},
    "location": {"term_type": "location", "needs_named_entity_proof": True},
    "book_title": {"term_type": "book_title", "needs_named_work_proof": True},
    "historical_work": {"term_type": "historical_work", "needs_named_work_proof": True},
    "organization": {"term_type": "organization", "needs_stable_name_proof": True},
    "artifact": {"term_type": "artifact", "needs_named_item_proof": True},
    "technique": {"term_type": "technique", "needs_named_technique_proof": True},
    "honorific": {"term_type": "honorific", "needs_stable_name_proof": True},
    "official_title": {"term_type": "official_title", "needs_stable_name_proof": True},
    "historical_office": {"term_type": "historical_office", "needs_stable_name_proof": True},
    "event": {"term_type": "event", "needs_named_event_proof": True},
    "concept": {"term_type": "concept", "needs_named_concept_proof": True},
    "proper_noun": {"term_type": "proper_noun", "needs_named_entity_proof": True},
    "generic": {"term_type": "generic_phrase", "ignore": True},
    "malformed": {"term_type": "unknown", "ignore": True},
}

LOCATION_SUFFIXES = ("宫", "城", "府", "殿", "台", "楼", "关", "门", "陵", "寺", "观", "园", "苑", "堂", "阁")
WORK_SUFFIXES = ("实录", "会计录", "算术", "算法大全", "演义", "括囊", "志", "录", "书", "图", "经", "传", "集")
ORGANIZATION_SUFFIXES = ("卫", "部", "署", "司", "局", "院", "会", "教", "门", "党", "派", "营", "军")
ARTIFACT_SUFFIXES = ("丹", "剑", "刀", "枪", "印", "令", "符", "琴", "鼎", "珠", "石", "杖", "弓", "甲")
TECHNIQUE_SUFFIXES = ("诀", "功法", "剑法", "刀法", "掌", "拳", "阵", "式", "杀", "术")
KNOWN_OFFICE_TERMS = frozenset({"大司徒", "尚书", "侍班", "缇帅", "大伴", "帅"})
GENERIC_PLACE_TERMS = frozenset({"京城", "三大殿", "上殿", "城中", "宫中", "府中", "殿内"})
GENERIC_GROUP_TERMS = frozenset({"主战派", "两派", "七派"})
LOCATION_CUE = re.compile(r"(?:在|到|往|从|入|进|驻|居|经过|位于|离开|宫中|城中|府中|殿内|殿中)")
WORK_CUE = re.compile(r"(?:编|著|作|读|刊|修|载|录入|成书|撰|校)")
ORGANIZATION_CUE = re.compile(r"(?:隶属|所属|加入|统领|统辖|率领|官|衙门|组织|党羽|一派)")
ITEM_CUE = re.compile(r"(?:一枚|一颗|一柄|一把|一件|服下|吞下|炼制|祭出|佩戴|取出|收起|名为|称为)")
TECHNIQUE_CUE = re.compile(r"(?:使出|施展|发动|运转|练习|修炼|施放|一招|招式|绝招|功法)")
EVENT_CUE = re.compile(r"(?:起兵|攻打|围城|逼宫|之乱|之变|战役|大战|战事|叛乱)")
CONCEPT_CUE = re.compile(r"(?:制度|学说|理论|法则|体系|思想|主义|制度)")
MALFORMED_PREFIXES = ("且", "今日", "今", "书")


def _has_marker(source, contexts, left="《", right="》"):
    return any(f"{left}{source}{right}" in context for context in contexts)


def _character_reference_proven(source, contexts):
    """Small local gate used only for references to a person."""
    identity = re.compile(
        re.escape(source) + r".{0,18}(?:就是|是|名为|叫做|称为|即|乃)[\u3400-\u9fff]{2,8}"
    )
    reverse = re.compile(
        r"[\u3400-\u9fff]{2,8}.{0,18}(?:就是|是|名为|叫做|称为|即|乃)" + re.escape(source)
    )
    if any(identity.search(context) or reverse.search(context) for context in contexts):
        return True
    address = style.address_spec(source, contexts=contexts)
    if not address:
        return False
    if address.get("prefix_reference") and len(address.get("person_source", "")) >= 3:
        return True
    suffix = address.get("suffix")
    surname = address.get("surname")
    role = re.compile(
        r"[\u3400-\u9fff]{2,8}(?:担任|官至|升为|晋升为|改任|任职|任|成为)"
        r"[\u3400-\u9fff]{0,8}"
        + re.escape(suffix or "")
    )
    return bool(
        suffix
        and surname
        and any(
            surname in context and role.search(context)
            for context in contexts
        )
    )


def classify_entity(source, reasons, contexts, frequency=0, inherited=False):
    """Classify before confirmation; each class gets its own evidence policy."""
    if inherited:
        return "unknown", "inherited confirmed terminology"
    contexts = [context for context in contexts or () if context]
    named_signals = {
        "explicit_named_marker",
        "explicit_naming_context",
        "possible_person_alias",
    }
    named_candidate = bool(named_signals.intersection(reasons))
    if source in GENERIC_VERB_PHRASES and not named_candidate:
        return "generic", "ordinary verb phrase"
    if (
        source in GENERIC_COMMON_PHRASES
        or source in GENERIC_PLACE_TERMS
        or source in GENERIC_GROUP_TERMS
    ) and not named_candidate:
        return "generic", "ordinary or generic compositional phrase"
    if re.match(r"^\d+(?:[-–~～]\d+)?(?:种|类|个|名|件|队|位|块|颗|枚)", source) and not named_candidate:
        return "generic", "quantity or numeric descriptive phrase"
    if (
        any(marker in source for marker in DESCRIPTIVE_MARKERS)
        and not TITLE_PATTERN.search(source)
        and not named_candidate
    ):
        return "generic", "compositional descriptive phrase"
    if source.startswith(MALFORMED_PREFIXES) and len(source) > 2:
        if source.startswith(("且", "今", "今日")) and (style.address_spec(source) or source[-1:] in {"伯", "生"}):
            return "malformed", "leading contextual residue"
        if source.startswith("书") and len(source) <= 4:
            return "malformed", "contained fragment"
    if source.startswith("了") and len(source) > 3 and any(source.endswith(suffix) for suffix in TECHNIQUE_SUFFIXES):
        return "malformed", "leading aspect residue"

    if source in KNOWN_OFFICE_TERMS:
        return "official_title", "historical office/title vocabulary"

    address = style.address_spec(source, contexts=contexts)
    if address or TITLE_PATTERN.search(source):
        if address and address.get("prefix_reference"):
            return "character_reference", "title prefix reference"
        surname = address.get("surname") if address else ""
        suffix = address.get("suffix") if address else ""
        if surname and surname[:1] in SURNAMES:
            return "character_reference", "surname plus title/reference"
        if source in KNOWN_OFFICE_TERMS or suffix in {"尚书", "侍班", "缇帅", "大伴", "帅"}:
            if frequency >= 2 or _has_marker(source, contexts) or any(
                re.search(r"(?:任|担任|官至|升为|改任|任职|属于|称为)", context)
                for context in contexts
            ):
                return "official_title", "stable historical office/title usage"
            return "official_title", "historical office/title requires confirmation"
        # A standalone expression such as 世子殿下 can be stable terminology;
        # it is not automatically a person identity.
        if len(source) >= 3 and (frequency >= 2 or named_signals.intersection(reasons)):
            return "honorific", "stable standalone honorific"
        return "character_reference", "title/reference identity requires proof"

    if "person_name_pattern" in reasons:
        return "character", "person-name morphology"

    if _has_marker(source, contexts, "《", "》"):
        return "book_title", "explicit named work marker"
    if "explicit_named_marker" in reasons and source.endswith(WORK_SUFFIXES):
        return "book_title", "named work marker"
    if any(source.endswith(suffix) for suffix in WORK_SUFFIXES) and any(
        WORK_CUE.search(context) or source in context for context in contexts
    ):
        return "historical_work", "named work context"

    if source.endswith(LOCATION_SUFFIXES) and len(source) >= 3 and source not in GENERIC_PLACE_TERMS:
        if "explicit_named_marker" in reasons or any(LOCATION_CUE.search(context) for context in contexts) or frequency >= 2:
            return "location", "proper place morphology and location context"
    if source.endswith(ORGANIZATION_SUFFIXES) and len(source) >= 3:
        if "explicit_named_marker" in reasons or any(ORGANIZATION_CUE.search(context) for context in contexts) or frequency >= 2:
            return "organization", "stable organization/faction context"
    if source.endswith(TECHNIQUE_SUFFIXES) and len(source) >= 3:
        if "explicit_named_marker" in reasons or any(TECHNIQUE_CUE.search(context) for context in contexts) or frequency >= 2:
            return "technique", "named technique/ability context"
    if source.endswith(ARTIFACT_SUFFIXES) and len(source) >= 3:
        if "explicit_named_marker" in reasons or any(ITEM_CUE.search(context) for context in contexts) or frequency >= 2:
            return "artifact", "named item context"
    if any(EVENT_CUE.search(context) for context in contexts) and frequency >= 1:
        return "event", "named event context"
    if any(CONCEPT_CUE.search(context) for context in contexts) and frequency >= 2:
        return "concept", "stable named concept context"
    if named_signals.intersection(reasons) or "repeated_entity_or_genre_suffix" in reasons:
        return "proper_noun", "explicit or repeated named-entity signal"
    return "unknown", "classification remains uncertain"


def class_confirmation(candidate, entity_class=None):
    """Apply class-specific deterministic evidence, never identity rules globally."""
    entity_class = entity_class or candidate.get("entity_class", "unknown")
    contexts = [item.get("raw", "") for item in candidate.get("representative_evidence", [])]
    source = candidate.get("source", "")
    reasons = set(candidate.get("reasons", []))
    frequency = candidate.get("frequency", 0)
    if entity_class in {"generic", "malformed"}:
        return {"confirmed": False, "reason": "generic_or_malformed_candidate", "evidence": {}}
    if entity_class in {"character", "character_reference"}:
        return {
            "confirmed": entity_class == "character" or _character_reference_proven(source, contexts),
            "reason": "character_identity_requires_owner_proof",
            "evidence": {"identity_evidence": contexts[:3]},
        }
    explicit = "explicit_named_marker" in reasons
    repeated = frequency >= 2
    if entity_class in {"location", "proper_noun"}:
        positive = explicit or repeated or any(LOCATION_CUE.search(context) for context in contexts)
        return {"confirmed": positive, "reason": "stable_proper_location_or_entity", "evidence": {
            "proper_name_evidence": explicit or "proper_name_pattern" in reasons,
            "location_context_count": sum(bool(LOCATION_CUE.search(context)) for context in contexts),
            "repeated_occurrences": frequency,
        }}
    if entity_class in {"book_title", "historical_work"}:
        positive = explicit or any(WORK_CUE.search(context) for context in contexts) or repeated
        return {"confirmed": positive, "reason": "specific_named_work", "evidence": {
            "title_marker_evidence": explicit,
            "work_context": [context for context in contexts if WORK_CUE.search(context)][:3],
            "repeated_occurrences": frequency,
        }}
    if entity_class == "organization":
        positive = explicit or repeated or any(ORGANIZATION_CUE.search(context) for context in contexts)
        return {"confirmed": positive, "reason": "stable_named_organization", "evidence": {
            "organization_context": [context for context in contexts if ORGANIZATION_CUE.search(context)][:3],
            "stable_reference_count": frequency,
        }}
    if entity_class == "artifact":
        positive = explicit or repeated or any(ITEM_CUE.search(context) for context in contexts)
        return {"confirmed": positive, "reason": "specific_named_item", "evidence": {
            "named_item_context": [context for context in contexts if ITEM_CUE.search(context)][:3],
            "repeat_count": frequency,
        }}
    if entity_class == "technique":
        positive = explicit or repeated or any(TECHNIQUE_CUE.search(context) for context in contexts)
        return {"confirmed": positive, "reason": "named_technique_or_ability", "evidence": {
            "use_context": [context for context in contexts if TECHNIQUE_CUE.search(context)][:3],
            "repeat_count": frequency,
        }}
    if entity_class in {"honorific", "official_title", "historical_office"}:
        positive = repeated or explicit or any(
            re.search(r"(?:任|担任|官至|升为|改任|任职|属于|称为)", context)
            for context in contexts
        )
        return {"confirmed": positive, "reason": "stable_title_or_honorific", "evidence": {
            "title_context": contexts[:3],
            "repeated_occurrences": frequency,
        }}
    if entity_class in {"event", "concept"}:
        positive = explicit or repeated
        return {"confirmed": positive, "reason": f"named_{entity_class}", "evidence": {
            "repeat_count": frequency,
            "context": contexts[:3],
        }}
    return {"confirmed": explicit or repeated, "reason": "stable_named_entity", "evidence": {
        "repeat_count": frequency,
        "context": contexts[:3],
    }}


def classify_candidate(source, reasons, contexts, frequency=0, inherited=False):
    """Compatibility wrapper returning only local rejection classifications."""
    if inherited:
        return None
    entity_class, reason = classify_entity(source, reasons, contexts, frequency, inherited)
    if entity_class == "generic":
        if source in GENERIC_VERB_PHRASES:
            return "verb_phrase", reason
        if re.match(r"^\d", source):
            return "descriptive_phrase", reason
        return "common_noun", reason
    if entity_class == "malformed":
        return "malformed", reason
    if entity_class == "character_reference" and not _character_reference_proven(source, contexts):
        return "character_form", reason
    if entity_class in {"official_title", "historical_office"} and frequency < 2 and not any(
        re.search(r"(?:任|担任|官至|升为|改任|任职|属于|称为)", context)
        for context in contexts
    ) and not any(f"《{source}》" in context or f"【{source}】" in context for context in contexts):
        return "official_title", "standalone title is not stable terminology"
    return None


def remove_contextual_person_extensions(reasons):
    """Drop fixed-length surname-pattern extensions of an already found name.

    A three-character substring is not independently a character reference just
    because a two-character name before it was detected. Formal title/role
    endings remain eligible; ordinary predicate residue does not.
    """
    for source in list(reasons):
        if "person_name_pattern" not in reasons[source]:
            continue
        prefix = next(
            (
                candidate
                for candidate in reasons
                if candidate != source
                and "person_name_pattern" in reasons[candidate]
                and source.startswith(candidate)
            ),
            None,
        )
        if prefix and not REFERENCE_SUFFIX.search(source):
            reasons[source].discard("person_name_pattern")
            if not reasons[source]:
                del reasons[source]


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
            if re.search(
                r"老|小|阿|科长|署长|将军|先生|小姐|大伴|尚书|帅|缇帅|侍班|大司徒|叫做|名叫",
                units[o["unit"]]["raw"],
            )
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
            + r"](?:副)?(?:"
            + "|".join(
                re.escape(suffix)
                for suffix in sorted(style.ADDRESS_SPECS, key=len, reverse=True)
            )
            + r")",
            text,
        ):
            reasons[match.group()].add("named_title_or_address_form")
        # Historical offices can be compound role references rather than
        # surname+title strings (for example 东宫侍班).  Discover their exact
        # RAW span for evidence/audit, but let the classifier decide whether
        # the role is actually attributable to a person.
        for suffix in sorted(style.ADDRESS_SPECS, key=len, reverse=True):
            for match in re.finditer(
                r"[\u3400-\u9fff]{1,3}" + re.escape(suffix),
                text,
            ):
                prefix = match.group()[: -len(suffix)]
                if prefix and not set(prefix).intersection(GRAMMAR):
                    reasons[match.group()].add("named_title_or_address_form")
        for prefix in sorted(style.TITLE_PREFIX_SPECS, key=len, reverse=True):
            surname_class = "".join(SURNAMES)
            for match in re.finditer(
                re.escape(prefix)
                + "["
                + surname_class
                # Historical personal names in this extractor are the same
                # two/three Han-character shape used by person_name_pattern;
                # stopping at two given-name characters prevents a following
                # verb/adverb from being absorbed into the title form.
                + r"][\u3400-\u9fff]{1,2}",
                text,
            ):
                # Do not promote a title expression ending in an immediately
                # attached predicate/action character to a named reference.
                if match.group()[-1] not in "来去入出上下再曾笑接站":
                    reasons[match.group()].add("named_title_or_address_form")
        # Capture standalone titles/roles and malformed title-shaped spans so
        # they can be audited without pretending they are character names.
        for run in HAN_RUN.findall(text):
            for suffix in sorted(style.ADDRESS_SPECS, key=len, reverse=True):
                if run.endswith(suffix) and 1 <= len(run) - len(suffix) <= 4:
                    reasons[run].add("named_title_or_address_form")
            if run.startswith("了") and len(run) > 3 and run.endswith(TECHNIQUE_SUFFIXES):
                reasons[run].add("malformed_noise_shape")
                reasons[run[1:]].add("named_technique_context")
        # Location evidence is often carried by a local preposition rather
        # than 《...》 markers (进入乾清宫, 在紫禁城, 位于文华殿).
        for match in re.finditer(
            r"(?:在|到|往|从|入|进|驻|居|经过|位于|离开)([\u3400-\u9fff]{2,12})(?:中|内|上)?",
            text,
        ):
            value = match.group(1)
            value = value.lstrip("在到往从入进驻居经过离开")
            value = value.rstrip("中内上")
            if value.endswith(LOCATION_SUFFIXES) and len(value) >= 3:
                reasons[value].add("named_location_context")
        # Named works, items and techniques can be explicit without title
        # punctuation; the cue-based spans are still bounded to Han text.
        for match in re.finditer(
            r"(?:编|著|读|刊|修|载|撰|校|记录)[了过着的 ]*([\u3400-\u9fff]{2,16})",
            text,
        ):
            value = match.group(1)
            if value.endswith(WORK_SUFFIXES):
                reasons[value].add("named_work_context")
        for match in re.finditer(
            r"([\u3400-\u9fff]{2,16}(?:"
            + "|".join(map(re.escape, sorted(WORK_SUFFIXES, key=len, reverse=True)))
            + r"))",
            text,
        ):
            reasons[match.group(1)].add("named_work_context")
        for match in re.finditer(
            r"(?:使出|施展|发动|运转|练习|修炼|施放|一招|招式|绝招|功法)[了过着的 ]*([\u3400-\u9fff]{2,16})",
            text,
        ):
            value = match.group(1).lstrip("了")
            if value and len(value) >= 3:
                reasons[value].add("named_technique_context")
        for match in re.finditer(
            r"(?:炼制|祭出|佩戴|取出|收起|服下|吞下)[了过着的 ]*([\u3400-\u9fff]{2,12})",
            text,
        ):
            value = match.group(1).lstrip("了")
            if value and value.endswith(ARTIFACT_SUFFIXES) and len(value) >= 3:
                reasons[value].add("named_item_context")
        if text.startswith("书"):
            for run in HAN_RUN.findall(text):
                if run.startswith("书") and 2 <= len(run) <= 4:
                    reasons[run].add("malformed_noise_shape")
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
        for office in KNOWN_OFFICE_TERMS:
            if office in text:
                reasons[office].add("historical_office_context")
        for match in re.finditer(
            r"(?:任意|任何)[\u3400-\u9fff]{1,12}(?:抓捕|派遣|调动|处理)[\u3400-\u9fff]{0,8}", text
        ):
            reasons[match.group()].add("compositional_descriptive_phrase")
    remove_contextual_person_extensions(reasons)
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
        inherited_source = any(
            source in [term.source, *term.aliases, *term.forms] for term in inherited
        )
        entity_class, entity_reason = classify_entity(
            source,
            reasons[source],
            contexts,
            counts[source],
            inherited=inherited_source,
        )
        if issue or classification or (
            reasons[source] == {"person_name_pattern"} and counts[source] < 2
        ):
            reason = issue or (
                classification[1] if classification else "unconfirmed single person-name pattern"
            )
            category = classification[0] if classification else entity_class
            rejected[source] = reason
            report_only[source] = {
                "source": source,
                "classification": category,
                "entity_class": entity_class,
                "shape": reference_shape(source),
                "status": "report_only",
                "state": "IGNORE",
                "enforceable": False,
                "reason": reason,
                "reasons": sorted(reasons[source]),
                "frequency": 0,
                "occurrences": [],
                "entity_reason": entity_reason,
            }
            continue
        candidates[source] = {
            "source": source,
            "reasons": sorted(reasons[source]),
            "classification": (
                "character_name"
                if "person_name_pattern" in reasons[source]
                else "character_form"
                if entity_class == "character_reference"
                else entity_class
            ),
            "shape": reference_shape(source),
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
            "entity_class": entity_class,
            "entity_reason": entity_reason,
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
            "entity_class": "generic",
            "shape": reference_shape(source),
            "status": "report_only",
            "state": "IGNORE",
            "enforceable": False,
            "reason": rejected[source],
            "reasons": candidates[source]["reasons"],
            "frequency": 0,
            "occurrences": [],
            "entity_reason": rejected[source],
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
        candidate["class_evidence"] = class_confirmation(candidate)
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
    """Confirm only class-specific deterministic evidence with a stable reading."""
    variants = candidate["vietphrase_variants"]
    min_contexts = getattr(policy, "min_contexts", 3) if policy else 3
    min_dominance = getattr(policy, "min_dominance", 0.95) if policy else 0.95
    min_coverage = getattr(policy, "min_coverage", 0.95) if policy else 0.95
    entity_class = candidate.get("entity_class", "character")
    if not variants:
        return None
    translation, count = max(variants.items(), key=lambda pair: pair[1])
    class_evidence = candidate.get("class_evidence") or class_confirmation(candidate)
    if entity_class not in {"character", "character_reference"}:
        # Explicitly marked works and strongly evidenced places can be locked
        # locally when VietPhrase supplies one stable rendering.  Other named
        # items still go through the class-aware resolver, preserving existing
        # resolver review behavior for ambiguous artifacts/techniques.
        if entity_class not in {"book_title", "historical_work", "location"}:
            return None
        minimum_occurrences = 1 if "explicit_named_marker" in candidate.get("reasons", []) else min_contexts
        if len(candidate["occurrences"]) < minimum_occurrences or not class_evidence.get("confirmed"):
            return None
        total = sum(variants.values())
        if count / max(1, total) < min_dominance or candidate.get("mapping_coverage", 0) < min_coverage:
            return None
        return Term(
            source=candidate["source"],
            translation=translation,
            type=ENTITY_CLASS_POLICIES[entity_class]["term_type"],
            status="locked",
            gender="unknown",
            semantic_resolution="resolved",
            needs_review=False,
            enforceable=True,
            confidence=count / max(1, total),
            resolver_source="local_class_consensus",
            evidence=class_evidence.get("reason", "class-specific local evidence"),
            entity_evidence=class_evidence.get("evidence", {}),
        )
    if len(candidate["occurrences"]) < min_contexts:
        return None
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
    is_person = entity_class == "character" and "person_name_pattern" in candidate["reasons"]
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
