"""Canonical dictionary, local candidate scan, and deterministic patch authority."""
from __future__ import annotations
import copy
import re
from collections import defaultdict
from .models import DICTIONARY_VERSION, DictionaryPatch, Entry

HAN = r"[\u3400-\u9fff]"
HAN_RE = re.compile(HAN + "+")
BAD_ENDINGS = set("笑看说道问答走来去想听见着了过地得的和与把将被就又才还都也很更在是有要以于")
PRONOUNS = {"他", "她", "它", "你", "我", "他们", "她们", "它们", "我们", "你们", "自己", "别人"}
SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费岑薛雷贺倪汤滕殷罗毕郝安常乐于时傅皮卞齐康伍余元卜顾孟平黄穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗宣丁邓郁单杭洪包左石崔吉龚程嵇邢滑裴陆荣翁荀羊惠甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司黎薄印宿白怀蒲台从鄂索咸赖卓蔺屠蒙池乔阴胥能苍闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿边扈燕冀郏浦尚农温别庄晏柴瞿阎连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
ACTION = "说|道|问|答|笑|看|想|走|来|去|听|见|点头|摇头|皱眉|开口"
ENTITY_SUFFIX = "公司|集团|组织|协会|学院|宗门|帮派|神殿|城市|小镇|村庄|山脉|大厦|公园|宫殿|能力|异能|技能|功法|法术|神器|宝物|系统|计划|机关|衙门|研究所"
DECLARED = re.compile(rf"(?:名叫|名字叫|叫做|被称为|自称|号称|名为)\s*({HAN}{{2,6}})")
NAMED = re.compile(rf"({HAN}{{2,8}}(?:{ENTITY_SUFFIX}))")
ACTION_NAME = re.compile(rf"(?:^|[。！？；，、：“「])\s*({HAN}{{2,4}})(?={ACTION})")
SUBJECT_NAME = re.compile(rf"(?:^|[。！？；，、：“「])\s*({HAN}{{2,3}})(?=把|被|没有|没|不|已|就|还|这才|是|在|向|从|将|跟|与)")
TITLE_ALIAS = re.compile(rf"({HAN}{{1,3}}(?:探员|长官|先生|小姐|老板|队长|局长|老师|哥|姐))")
TITLE_END = re.compile(r"(?:探员|长官|先生|小姐|老板|队长|局长|老师|哥|姐)$")
POST_QUOTED = re.compile(rf"《({HAN}{{2,8}})》")
COMPOUND_SURNAMES = {"欧阳", "上官", "司马", "诸葛", "夏侯", "东方", "慕容", "皇甫", "宇文", "令狐"}
BAD_NAME_START = set("此那这时后前正已刚再又才便都也还很下个从以而因在对把被将让和与其别全尤关")
GENERIC_SOURCES = {
    "使者", "众人", "大家", "朝廷", "大人", "先生", "小姐", "老人", "孩子", "男人", "女人",
    "全都", "尤其", "关键", "武功", "能力", "然后", "但是", "不然", "什么", "如何", "怎么",
}
FRAGMENT_START = re.compile(r"^(?:不是|但是|虽然|因为|由于|所以|如果|否则|正在|已经|从来|从不|不能|你说|全都|尤其|关键|别|自己|别人|他们|她们|我们|你们|他|她|它)")
FRAGMENT_END = re.compile(r"(?:不是|但是|来自于|由于|所以|因为|如果|从|以|于|而|着|了|过|呢|吧|吗|么|要|却|再|还|都|也|全)$")
GRAMMATICAL_FRAGMENT = re.compile(r"(?:不是|但是|来自于|由于|所以|因为|如果|正在|已经|不能|从不|以为|觉得|然后)")
ACTION_PHRASE = re.compile(r"^(?:做|进行|使用|采取|拿起|看见|听见|说出|告诉|回答|询问|攻击|杀死|救下|离开|进入|走出|来到|站在|坐在|获得|得到|拥有|成为|变成|继续|开始|准备|完成|执行|运用|开启|关闭|吃掉|喝掉|写下|记下|练习)(?:[\u3400-\u9fff]{1,10})$")
INSTITUTION_SUFFIXES = ("公司", "集团", "组织", "协会", "学院", "宗门", "帮派", "神殿", "委员会", "研究所", "局", "司", "署", "衙门", "机关")
LOCATION_SUFFIXES = ("城市", "城", "小镇", "村庄", "村", "山脉", "山", "大厦", "公园", "宫殿", "楼", "岛")
TERM_SUFFIXES = (
    "数列", "曲线", "方盖", "太极图", "先天图", "制度", "学说", "理论", "方法", "算法", "定理", "公式",
    "原则", "效应", "定律", "结构", "关系", "模型", "概念", "体系", "主义", "思想", "技法", "功法",
    "法术", "剑法", "拳法", "图", "术", "诀", "阵", "曲", "率", "形", "体",
)

class DictionaryConflict(ValueError):
    pass


CANONICAL_TYPES = {
    "character", "location", "institution", "book_title", "memorial", "book_section", "term",
}
LEGACY_TYPE_MAP = {
    "organization": "institution",
    "faction": "institution",
    "place": "location",
    "ability": "term",
    "technique": "term",
    "item": "term",
    "artifact": "term",
    "weapon": "term",
    "concept": "term",
    "work": "book_title",
}

def _clean_text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DictionaryConflict(f"{field} must be non-empty and trimmed")
    return value

def source_problem(source):
    if (not isinstance(source, str) or source in PRONOUNS or source in GENERIC_SOURCES
            or not HAN_RE.fullmatch(source)):
        return "pronoun, generic role, or non-Chinese source"
    if len(source) < 2 or len(source) > 12 or (len(source) >= 3 and source[0] in SURNAMES
                            and any(char in BAD_ENDINGS for char in source[-1:])):
        return "contextual/action fragment"
    if FRAGMENT_START.search(source) or FRAGMENT_END.search(source) or GRAMMATICAL_FRAGMENT.search(source):
        return "sentence fragment"
    if ACTION_PHRASE.fullmatch(source):
        return "verb phrase"
    if len(source) >= 3 and source[0] in BAD_NAME_START and source[:2] not in COMPOUND_SURNAMES:
        return "contextual/action fragment"
    if re.search(r"(?:拿起|起来|转身|点头|摇头|看着|说着|想着|听着|轻声)$", source):
        return "contextual/action fragment"
    if re.search(r"[，。！？；：\s]", source):
        return "sentence fragment"
    return None


def possible_type(source, evidence=()):
    """Infer the narrow glossary class from morphology and sentence context."""
    if source_problem(source):
        return None
    evidence_text = "\n".join(
        item.get("text", "") if isinstance(item, dict) else str(item) for item in evidence
    )
    if source in re.findall(rf"《({HAN}{{2,12}})》", evidence_text):
        return "book_title"
    if source.endswith(INSTITUTION_SUFFIXES):
        return "institution"
    if source.endswith(LOCATION_SUFFIXES):
        return "location"
    if source.endswith(TERM_SUFFIXES):
        return "term"
    if len(source) in (2, 3, 4) and (source[:2] in COMPOUND_SURNAMES or source[0] in SURNAMES):
        return "character"
    if ("被称为" in evidence_text or "名为" in evidence_text or "称作" in evidence_text
            or "定理" in evidence_text or "公式" in evidence_text or "数列" in evidence_text):
        return "term"
    # Keep a narrow unknown bucket for recurring, independently named identities
    # which do not expose their class through Chinese suffixes.
    if len(source) >= 3 and evidence_text and any(marker in evidence_text for marker in ("名字", "名称", "名叫", "出自", "著有")):
        return "unknown"
    return None


def candidate_filter_reason(candidate):
    """Reject non-glossary candidates before a resolver request is constructed."""
    source = candidate.get("source", "") if isinstance(candidate, dict) else str(candidate)
    evidence = candidate.get("evidence", []) if isinstance(candidate, dict) else []
    problem = source_problem(source)
    if problem:
        return problem
    if isinstance(candidate, dict) and candidate.get("possible_type") in {
        "character", "location", "institution", "book_title", "term",
    }:
        return None
    return None if possible_type(source, evidence) is not None else "no independent identity or reusable-term signal"


def best_evidence(values, limit=5):
    """Keep useful sentence samples while preserving chapter diversity and range."""
    unique = []
    for value in values:
        if isinstance(value, str):
            value = {"chapter": None, "text": value}
        if not isinstance(value, dict) or not isinstance(value.get("text"), str) or not value["text"].strip():
            continue
        chapter = value.get("chapter")
        record = {"chapter": chapter if isinstance(chapter, int) else None,
                  "text": value["text"]}
        if not any(item["chapter"] == record["chapter"] and item["text"] == record["text"]
                   for item in unique):
            unique.append(record)
    if len(unique) <= limit:
        return unique
    context_markers = (
        "先生", "长官", "皇帝", "皇后", "协会", "宗门", "学院", "书院", "城", "担任", "任命",
        "名叫", "名为", "被称为", "称作", "创立", "提出", "发明", "著有", "来自",
    )

    def score(record):
        text = record["text"]
        length_score = min(len(text), 220) - max(0, len(text) - 220) // 4
        context_score = 80 * sum(marker in text for marker in context_markers)
        return context_score + length_score

    chapters = {}
    for record in unique:
        chapter = record.get("chapter")
        chapters.setdefault(chapter, []).append(record)
    per_chapter = [max(records, key=score) for records in chapters.values()]
    if len(per_chapter) > limit:
        ordered = sorted(per_chapter, key=lambda item: (
            item.get("chapter") is None, item.get("chapter") or 0,
        ))
        chosen = [ordered[0]]
        if limit > 1:
            chosen.append(ordered[-1])
        remaining = [item for item in ordered[1:-1] if item not in chosen]
        chosen.extend(sorted(remaining, key=score, reverse=True)[:limit - len(chosen)])
    else:
        chosen = list(per_chapter)
        remaining = [item for item in unique if item not in chosen]
        chosen.extend(sorted(remaining, key=score, reverse=True)[:limit - len(chosen)])
    return sorted(chosen, key=lambda item: (item.get("chapter") is None,
                                           item.get("chapter") or 0, -score(item)))

def _entry(record, legacy=False, migrations=None, allow_legacy_types=False):
    if not isinstance(record, dict):
        raise DictionaryConflict("Dictionary entry must be an object")
    if not legacy and set(record) - {"source", "translation", "type", "gender", "status", "aliases"}:
        raise DictionaryConflict(f"Unexpected canonical entry keys for {record.get('source')!r}")
    if record.get("status") != "locked":
        raise DictionaryConflict(f"Dictionary entry {record.get('source')!r} is not locked")
    source = record.get("source")
    kind = record.get("type")
    if kind in LEGACY_TYPE_MAP and not allow_legacy_types:
        raise DictionaryConflict(f"Legacy type {kind!r} is invalid in schema v{DICTIONARY_VERSION}")
    canonical_kind = LEGACY_TYPE_MAP.get(kind, kind)
    if canonical_kind not in CANONICAL_TYPES:
        raise DictionaryConflict(f"Locked type {kind!r} needs an explicit schema migration")
    aliases = []
    for alias in record.get("aliases", []):
        if isinstance(alias, dict):
            if not legacy and set(alias) - {"source", "translation"}:
                raise DictionaryConflict(f"Unexpected alias keys in entry {source!r}")
            aliases.append(alias)
        elif legacy and isinstance(alias, str):
            value = (record.get("forms") or {}).get(alias)
            if value:
                aliases.append({"source": alias, "translation": value})
        else:
            raise DictionaryConflict(f"Invalid alias in entry {source!r}")
    if legacy:
        for alias_source, target in (record.get("forms") or {}).items():
            aliases.append({"source": alias_source, "translation": target})
    unique = {}
    for alias in aliases:
        if not isinstance(alias, dict) or not alias.get("source") or not alias.get("translation"):
            raise DictionaryConflict(f"Invalid alias in entry {source!r}")
        previous = unique.get(alias["source"])
        if previous and previous != alias:
            raise DictionaryConflict(f"Conflicting duplicate alias {alias['source']!r}")
        unique[alias["source"]] = {
            "source": alias["source"], "translation": alias["translation"],
        }
    gender = record.get("gender", "unknown" if canonical_kind == "character" else "not_applicable")
    if gender is None:
        normalized_gender = "unknown" if canonical_kind == "character" else "not_applicable"
        if migrations is not None:
            migrations.append({
                "source": source, "field": "gender", "from": None, "to": normalized_gender,
            })
        gender = normalized_gender
    if canonical_kind != "character" and gender != "not_applicable":
        if migrations is not None:
            migrations.append({
                "source": source, "field": "gender", "from": gender, "to": "not_applicable",
            })
        gender = "not_applicable"
    if kind != canonical_kind and migrations is not None:
        migrations.append({
            "source": source, "field": "type", "from": kind, "to": canonical_kind,
        })
    canonical = {
        "source": source,
        "translation": record.get("translation"),
        "type": canonical_kind,
        "gender": gender,
        "status": "locked",
        "aliases": list(unique.values()),
    }
    try:
        return Entry.model_validate(canonical)
    except (ValueError, TypeError) as exc:
        raise DictionaryConflict(f"Invalid locked dictionary entry {source!r}: {exc}") from exc


def load_dictionary(data, migrations=None):
    """Load canonical v10 state and explicitly migrate every supported old type."""
    if data is None:
        return {"version": DICTIONARY_VERSION, "entries": []}
    if isinstance(data, list):
        records, legacy, source_version = data, False, None
    elif isinstance(data, dict):
        records = data.get("entries")
        source_version = data.get("version", DICTIONARY_VERSION)
        if not isinstance(source_version, int) or source_version > DICTIONARY_VERSION:
            raise DictionaryConflict(f"Unsupported dictionary version {source_version!r}")
        legacy = source_version < DICTIONARY_VERSION
        if not isinstance(records, list):
            raise DictionaryConflict("Dictionary requires an entries array")
        if not legacy and set(data) - {"version", "entries", "unresolved", "rejected"}:
            raise DictionaryConflict("Unexpected dictionary keys")
    else:
        raise DictionaryConflict("Invalid dictionary JSON")
    result = {"version": DICTIONARY_VERSION, "entries": []}
    for record in records:
        entry = _entry(record, legacy, migrations,
                       allow_legacy_types=legacy or source_version is None)
        result["entries"].append(entry.model_dump())
    if migrations is not None and source_version is not None and source_version != DICTIONARY_VERSION:
        migrations.insert(0, {
            "field": "version", "from": source_version, "to": DICTIONARY_VERSION,
        })
    validate_dictionary(result)
    return result

def load_unresolved(data):
    """Read carry-forward review metadata without adding it to canonical entries."""
    if not isinstance(data, dict):
        return []
    records = data.get("unresolved", [])
    if not isinstance(records, list):
        raise DictionaryConflict("Dictionary unresolved metadata must be an array")
    result = []
    for item in records:
        if not isinstance(item, dict) or set(item) - {
            "source", "possible_type", "reason", "evidence", "chapters", "frequency",
            "occurrences", "occurrences_by_chapter", "first_seen_chapter",
            "last_seen_chapter", "resolve_attempts", "last_error",
            "last_attempt_input_hash",
        }:
            raise DictionaryConflict("Invalid unresolved metadata")
        source = _clean_text(item.get("source"), "unresolved source")
        raw_evidence = item.get("evidence", [])
        if not isinstance(raw_evidence, list) or not all(
            isinstance(value, (str, dict)) for value in raw_evidence
        ):
            raise DictionaryConflict(f"Invalid unresolved evidence for {source}")
        chapter_numbers = sorted({
            value for value in (item.get("chapters", []) or []) if isinstance(value, int)
        })
        first = item.get("first_seen_chapter")
        last = item.get("last_seen_chapter")
        first = first if isinstance(first, int) else None
        last = last if isinstance(last, int) else None
        if first is None and chapter_numbers:
            first = chapter_numbers[0]
        if last is None and chapter_numbers:
            last = chapter_numbers[-1]
        evidence = []
        for index, value in enumerate(raw_evidence):
            if isinstance(value, str):
                chapter = chapter_numbers[min(index, len(chapter_numbers) - 1)] if chapter_numbers else first
                record = {"chapter": chapter, "text": value}
            else:
                record = {"chapter": value.get("chapter"), "text": value.get("text", "")}
            if not isinstance(record["text"], str) or not record["text"].strip():
                continue
            if not any(existing["text"] == record["text"]
                       and existing.get("chapter") == record.get("chapter") for existing in evidence):
                evidence.append(record)
        evidence = best_evidence(evidence)
        by_chapter = item.get("occurrences_by_chapter")
        if not isinstance(by_chapter, dict):
            by_chapter = {}
        by_chapter = {
            str(chapter): max(0, count) for chapter, count in by_chapter.items()
            if str(chapter).isdigit() and isinstance(count, int)
        }
        if not by_chapter and chapter_numbers:
            legacy_count = max(1, int(item.get("occurrences", item.get("frequency", len(evidence) or 1))))
            quotient, remainder = divmod(legacy_count, len(chapter_numbers))
            by_chapter = {
                str(chapter): max(1, quotient + (index < remainder))
                for index, chapter in enumerate(chapter_numbers)
            }
        elif not by_chapter and last is not None:
            by_chapter = {str(last): max(1, int(item.get(
                "occurrences", item.get("frequency", len(evidence) or 1))))}
        occurrences = sum(by_chapter.values()) or max(
            1, int(item.get("occurrences", item.get("frequency", len(evidence) or 1)))
        )
        inferred = possible_type(source, evidence)
        kind = item.get("possible_type")
        if kind not in {"character", "location", "institution", "book_title", "term", "unknown"}:
            kind = None
        if inferred and inferred != "unknown":
            kind = inferred
        elif kind is None:
            kind = inferred or "unknown"
        record = {
            "source": source,
            "possible_type": kind,
            "reason": str(item.get("reason") or "insufficient evidence"),
            "evidence": evidence,
            "first_seen_chapter": first,
            "last_seen_chapter": last,
            "chapters": chapter_numbers,
            "occurrences": occurrences,
            "occurrences_by_chapter": by_chapter,
            "resolve_attempts": max(0, int(item.get("resolve_attempts", 0) or 0)),
            "last_attempt_input_hash": item.get("last_attempt_input_hash"),
        }
        if item.get("last_error"):
            record["last_error"] = str(item["last_error"])
        if not candidate_filter_reason(record):
            result.append(record)
    deduplicated = {}
    for record in result:
        old = deduplicated.get(record["source"])
        if old is None:
            deduplicated[record["source"]] = record
            continue
        old_counts = old.get("occurrences_by_chapter", {})
        new_counts = record.get("occurrences_by_chapter", {})
        counts = {str(chapter): max(int(old_counts.get(str(chapter), 0)),
                                    int(new_counts.get(str(chapter), 0)))
                  for chapter in set(old_counts) | set(new_counts)}
        old_unattributed = max(0, old["occurrences"] - sum(old_counts.values()))
        new_unattributed = max(0, record["occurrences"] - sum(new_counts.values()))
        old["occurrences_by_chapter"] = counts
        old["occurrences"] = sum(counts.values()) + max(old_unattributed, new_unattributed)
        old["evidence"] = best_evidence(old["evidence"] + record["evidence"])
        old["chapters"] = sorted(set(old.get("chapters", [])) | set(record.get("chapters", [])))
        seen = [value for value in (old.get("first_seen_chapter"), record.get("first_seen_chapter"))
                if isinstance(value, int)]
        old["first_seen_chapter"] = min(seen) if seen else None
        seen = [value for value in (old.get("last_seen_chapter"), record.get("last_seen_chapter"))
                if isinstance(value, int)]
        old["last_seen_chapter"] = max(seen) if seen else None
        if old.get("possible_type") == "unknown" and record.get("possible_type") != "unknown":
            old["possible_type"] = record["possible_type"]
        old["resolve_attempts"] = max(old["resolve_attempts"], record["resolve_attempts"])
        old["last_attempt_input_hash"] = record.get("last_attempt_input_hash") or old.get("last_attempt_input_hash")
        old["last_error"] = record.get("last_error") or old.get("last_error")
    return [deduplicated[source] for source in sorted(deduplicated)]


def load_rejected(data):
    """Read the compact resolver rejection cache, kept outside glossary entries."""
    if not isinstance(data, dict) or not isinstance(data.get("rejected"), list):
        return []
    unique = {}
    for item in data["rejected"]:
        if (isinstance(item, dict) and isinstance(item.get("source"), str)
                and item["source"].strip() and isinstance(item.get("reason"), str)):
            unique[item["source"]] = {
                "source": item["source"], "reason": item["reason"][:300],
            }
    return [unique[source] for source in sorted(unique)]

def validate_dictionary(dictionary):
    if not isinstance(dictionary, dict) or dictionary.get("version") != DICTIONARY_VERSION:
        raise DictionaryConflict(f"Dictionary state must use schema version {DICTIONARY_VERSION}")
    if set(dictionary) != {"version", "entries"} or not isinstance(dictionary["entries"], list):
        raise DictionaryConflict("Canonical dictionary has invalid top-level keys")
    owned = {}
    canonical_sources = set()
    for raw_entry in dictionary["entries"]:
        try:
            entry = Entry.model_validate(raw_entry)
        except (ValueError, TypeError) as exc:
            raise DictionaryConflict(f"Invalid canonical dictionary entry: {exc}") from exc
        if entry.type != "character" and entry.gender != "not_applicable":
            raise DictionaryConflict(f"Non-character entry must use not_applicable gender: {entry.source}")
        if entry.source in canonical_sources:
            raise DictionaryConflict(f"Duplicate canonical source: {entry.source}")
        canonical_sources.add(entry.source)
        local_sources = [entry.source] + [alias.source for alias in entry.aliases]
        if len(local_sources) != len(set(local_sources)):
            raise DictionaryConflict(f"Duplicate alias or self-alias for {entry.source}")
        for source, translation in [(entry.source, entry.translation)] + [(a.source, a.translation) for a in entry.aliases]:
            _clean_text(source, "source")
            _clean_text(translation, "translation")
            owner = owned.get(source)
            if owner and owner != (entry.source, translation):
                raise DictionaryConflict(f"Conflicting locked ownership for {source}")
            owned[source] = (entry.source, translation)
    return owned

def _alias_owner_proven(entries, canonical, alias, raw):
    if any(canonical["source"] in line and alias in line for line in raw.splitlines()):
        return True
    if canonical["type"] != "character":
        return False
    peers = [entry for entry in entries.values()
             if entry["type"] == "character" and entry["source"] != canonical["source"]]
    surname = canonical["source"][0]
    if alias.startswith(surname) and TITLE_END.search(alias):
        return not any(entry["source"].startswith(surname) for entry in peers)
    nickname = canonical["source"][-1]
    if alias.startswith(nickname) and alias.endswith(("哥", "姐")):
        return not any(entry["source"].endswith(nickname) for entry in peers)
    return False

def merge_patch(dictionary, patch, raw, allow_unattested=False):
    """Validate all operations against a copy, then return one atomic merged value."""
    patch = DictionaryPatch.model_validate(patch)
    merged = copy.deepcopy(dictionary)
    owned = validate_dictionary(merged)
    entries = {entry["source"]: entry for entry in merged["entries"]}
    operations = sorted(patch.confirmed, key=lambda item: 0 if item.operation == "add_entry" else 1)
    for operation in operations:
        if operation.operation == "add_entry":
            entry = operation.entry.model_dump()
            source = entry["source"]
            if source_problem(source):
                raise DictionaryConflict(f"Invalid canonical source {source!r}: {source_problem(source)}")
            if not allow_unattested and source not in raw:
                raise DictionaryConflict(f"Unattested canonical source {source}")
            if source in entries:
                if entries[source] == entry:
                    continue
                raise DictionaryConflict(f"Locked canonical source already exists: {source}")
            if source in owned:
                raise DictionaryConflict(f"Canonical source owned as alias: {source}")
            local_aliases = {source}
            for alias in entry["aliases"]:
                if source_problem(alias["source"]) or alias["source"] in local_aliases:
                    raise DictionaryConflict(f"Invalid alias {alias['source']}")
                if alias["source"] in owned:
                    raise DictionaryConflict(f"Locked alias conflict: {alias['source']}")
                local_aliases.add(alias["source"])
                if not allow_unattested and alias["source"] not in raw:
                    raise DictionaryConflict(f"Unattested alias {alias['source']}")
                if not allow_unattested and not _alias_owner_proven(entries, entry, alias["source"], raw):
                    raise DictionaryConflict(f"Insufficient alias owner evidence: {alias['source']}")
            entries[source] = entry
            merged["entries"].append(entry)
            owned[source] = (source, entry["translation"])
            for alias in entry["aliases"]:
                owned[alias["source"]] = (source, alias["translation"])
        else:
            source = operation.canonical_source
            alias = operation.alias.model_dump()
            if source not in entries:
                raise DictionaryConflict(f"Unknown canonical owner {source}")
            if source_problem(alias["source"]):
                raise DictionaryConflict(f"Invalid alias {alias['source']}")
            if not allow_unattested and alias["source"] not in raw:
                raise DictionaryConflict(f"Unattested alias {alias['source']}")
            if not allow_unattested and not _alias_owner_proven(entries, entries[source], alias["source"], raw):
                raise DictionaryConflict(f"Insufficient alias owner evidence: {alias['source']}")
            owner = owned.get(alias["source"])
            if owner:
                if owner == (source, alias["translation"]):
                    continue
                raise DictionaryConflict(f"Locked alias conflict: {alias['source']}")
            entries[source]["aliases"].append(alias)
            owned[alias["source"]] = (source, alias["translation"])
    merged["entries"].sort(key=lambda item: item["source"])
    for entry in merged["entries"]:
        entry["aliases"].sort(key=lambda item: item["source"])
    validate_dictionary(merged)
    return merged

def relevant_entries(dictionary, raw):
    sources = {entry["source"] for entry, _, _ in locked_matches(dictionary, raw)}
    return [entry for entry in dictionary["entries"] if entry["source"] in sources]

_MATCHER_CACHE = {}

def _matcher(dictionary):
    key = id(dictionary)
    cached = _MATCHER_CACHE.get(key)
    if cached and cached[0] is dictionary:
        return cached[1]
    trie = {}
    for entry in dictionary["entries"]:
        for source, target in [(entry["source"], entry["translation"])] + [
            (alias["source"], alias["translation"]) for alias in entry["aliases"]
        ]:
            node = trie
            for char in source:
                node = node.setdefault(char, {})
            node.setdefault("", []).append((entry, source, target))
    if len(_MATCHER_CACHE) >= 8:
        _MATCHER_CACHE.pop(next(iter(_MATCHER_CACHE)))
    _MATCHER_CACHE[key] = (dictionary, trie)
    return trie

def locked_matches(dictionary, raw):
    """Select longest non-overlapping locked source surfaces in RAW order."""
    trie = _matcher(dictionary)
    candidates = []
    for position in range(len(raw)):
        node = trie
        end = position
        while end < len(raw) and raw[end] in node:
            node = node[raw[end]]
            end += 1
            candidates.extend((position, end, entry, source, target)
                              for entry, source, target in node.get("", ()))
    occupied = bytearray(len(raw))
    selected = []
    for position, end, entry, source, target in sorted(
        candidates, key=lambda item: (-(item[1] - item[0]), item[0], item[3])
    ):
        # 里甲 is also a historical institution, but in "公式里甲乙丙" the
        # characters cross a locative word and an enumerated variable name.
        if source == "里甲" and raw[end:end + 2] == "乙丙":
            continue
        if any(occupied[position:end]):
            continue
        occupied[position:end] = b"\x01" * (end - position)
        selected.append((position, entry, source, target))
    return [(entry, source, target) for _, entry, source, target in sorted(selected, key=lambda item: item[0])]

def scan(chapters, dictionary, unresolved=(), post=False):
    """Collect likely unknown named entities from active RAW only."""
    owned = validate_dictionary(dictionary)
    prior = {item["source"] for item in unresolved}
    found = defaultdict(lambda: {"frequency": 0, "chapters": set(), "evidence": []})
    for chapter in chapters:
        for paragraph in chapter.paragraphs:
            hits = set()
            patterns = (DECLARED, NAMED, ACTION_NAME, SUBJECT_NAME, TITLE_ALIAS, POST_QUOTED) if post else (
                DECLARED, NAMED, ACTION_NAME, SUBJECT_NAME, TITLE_ALIAS)
            for pattern in patterns:
                for match in pattern.finditer(paragraph):
                    candidate = match[1]
                    if pattern in (ACTION_NAME, SUBJECT_NAME):
                        # Only a clause-start, surname-led subject is evidence of
                        # a name; arbitrary suffixes before verbs are fragments.
                        surname_width = 2 if candidate[:2] in COMPOUND_SURNAMES else 1
                        if (candidate[0] not in SURNAMES and surname_width == 1) or len(candidate) > surname_width + 2:
                            continue
                        if candidate[0] in BAD_NAME_START:
                            continue
                    if candidate in owned or candidate in prior or source_problem(candidate):
                        continue
                    if any(candidate in known for known in owned):
                        continue
                    hits.add(candidate)
            for candidate in hits:
                record = found[candidate]
                record["frequency"] += paragraph.count(candidate)
                record["chapters"].add(chapter.number)
                pos = paragraph.find(candidate)
                start = max(paragraph.rfind(mark, 0, pos) for mark in "。！？!?；\n") + 1
                ends = [paragraph.find(mark, pos + len(candidate)) for mark in "。！？!?；\n"]
                ends = [end for end in ends if end >= 0]
                end = min(ends) + 1 if ends else len(paragraph)
                sentence = paragraph[start:end].strip()
                if sentence and not any(
                    evidence["text"] == sentence and evidence["chapter"] == chapter.number
                    for evidence in record["evidence"]
                ):
                    record["evidence"].append({"chapter": chapter.number, "text": sentence})
    results = []
    for source, value in sorted(found.items(), key=lambda item: (-item[1]["frequency"], item[0])):
        item = {
            "source": source, "frequency": value["frequency"],
            "occurrences": value["frequency"], "chapters": sorted(value["chapters"]),
            "occurrences_by_chapter": {str(chapter.number): sum(
                paragraph.count(source) for paragraph in chapter.paragraphs
            ) for chapter in chapters if chapter.number in value["chapters"]},
            "evidence": best_evidence(value["evidence"]),
        }
        item["possible_type"] = possible_type(source, item["evidence"]) or "unknown"
        if not candidate_filter_reason(item):
            results.append(item)
    return results
