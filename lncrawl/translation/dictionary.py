"""Canonical dictionary, local candidate scan, and deterministic patch authority."""
from __future__ import annotations
import copy
import re
from collections import defaultdict
from .models import DICTIONARY_VERSION, DictionaryPatch, Entry

HAN = r"[\u3400-\u9fff]"
HAN_RE = re.compile(HAN + "+")
BAD_ENDINGS = set("笑看说道问答走来去想听见着了过地得的和与把将被就又才还都也很更在是有")
PRONOUNS = {"他", "她", "你", "我", "他们", "她们", "我们", "你们"}
SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费岑薛雷贺倪汤滕殷罗毕郝安常乐于时傅皮卞齐康伍余元卜顾孟平黄穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗宣丁邓郁单杭洪包左石崔吉龚程嵇邢滑裴陆荣翁荀羊惠甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司黎薄印宿白怀蒲台从鄂索咸赖卓蔺屠蒙池乔阴胥能苍闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿边扈燕冀郏浦尚农温别庄晏柴瞿阎连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
ACTION = "说|道|问|答|笑|看|想|走|来|去|听|见|点头|摇头|皱眉|开口"
ENTITY_SUFFIX = "公司|集团|组织|协会|学院|宗门|帮派|神殿|城市|小镇|村庄|山脉|大厦|公园|宫殿|能力|异能|技能|功法|法术|神器|宝物|系统|计划"
DECLARED = re.compile(rf"(?:名叫|名字叫|叫做|被称为|自称|号称|名为)\s*({HAN}{{2,6}})")
NAMED = re.compile(rf"({HAN}{{2,8}}(?:{ENTITY_SUFFIX}))")
ACTION_NAME = re.compile(rf"(?:^|[。！？；，、：“「])\s*({HAN}{{2,4}})(?={ACTION})")
SUBJECT_NAME = re.compile(rf"(?:^|[。！？；，、：“「])\s*({HAN}{{2,3}})(?=把|被|没有|没|不|已|就|还|这才|是|在|向|从|将|跟|与)")
TITLE_ALIAS = re.compile(rf"({HAN}{{1,3}}(?:探员|长官|先生|小姐|老板|队长|局长|老师|哥|姐))")
TITLE_END = re.compile(r"(?:探员|长官|先生|小姐|老板|队长|局长|老师|哥|姐)$")
POST_QUOTED = re.compile(rf"《({HAN}{{2,8}})》")
COMPOUND_SURNAMES = {"欧阳", "上官", "司马", "诸葛", "夏侯", "东方", "慕容", "皇甫", "宇文", "令狐"}
BAD_NAME_START = set("此那这时后前正已刚再又才便都也还很")
GENERIC_SOURCES = {"使者", "众人", "大家", "朝廷", "大人", "先生", "小姐", "老人", "孩子", "男人", "女人"}

class DictionaryConflict(ValueError):
    pass


CANONICAL_TYPES = {
    "character", "location", "institution", "book_title", "memorial", "book_section", "term",
}
LEGACY_TYPE_MAP = {
    "organization": "institution",
    "faction": "institution",
    "ability": "term",
    "technique": "term",
    "item": "term",
    "artifact": "term",
    "weapon": "term",
    "concept": "term",
}

def _clean_text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DictionaryConflict(f"{field} must be non-empty and trimmed")
    return value

def source_problem(source):
    if source in PRONOUNS or source in GENERIC_SOURCES or not HAN_RE.search(source):
        return "pronoun, generic role, or non-Chinese source"
    if len(source) > 12 or (len(source) >= 3 and source[0] in SURNAMES
                            and any(char in BAD_ENDINGS for char in source[-1:])):
        return "contextual/action fragment"
    if re.search(r"(?:拿起|起来|转身|点头|摇头|看着|说着|想着|听着|轻声)$", source):
        return "contextual/action fragment"
    if re.search(r"[，。！？；：\s]", source):
        return "sentence fragment"
    return None

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
        if not legacy and set(data) - {"version", "entries", "unresolved"}:
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
            "source", "possible_type", "reason", "evidence",
            "first_seen_chapter", "last_seen_chapter",
        }:
            raise DictionaryConflict("Invalid unresolved metadata")
        source = _clean_text(item.get("source"), "unresolved source")
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
            raise DictionaryConflict(f"Invalid unresolved evidence for {source}")
        result.append({**item, "source": source, "evidence": evidence})
    return result

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
        if entry.type == "character" and TITLE_END.search(entry.source):
            raise DictionaryConflict(f"Title/reference cannot be a canonical character: {entry.source}")
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
            if entry["type"] == "character" and TITLE_END.search(source):
                raise DictionaryConflict(f"Title/reference cannot be a canonical character: {source}")
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
                if len(record["evidence"]) < 3:
                    pos = paragraph.find(candidate)
                    record["evidence"].append(paragraph[max(0, pos - 65):pos + len(candidate) + 100])
    return [
        {"source": source, "frequency": value["frequency"], "chapters": sorted(value["chapters"]),
         "evidence": value["evidence"]}
        for source, value in sorted(found.items(), key=lambda item: (-item[1]["frequency"], item[0]))
    ]
