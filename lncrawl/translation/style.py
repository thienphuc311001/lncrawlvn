"""Chinese address/title register policy: Sino-Vietnamese by default.

An address or title form is an identity reference plus a semantic function, not
free prose.  The resolver must therefore not choose a Vietnamese rendering only
because it is the most natural modern conversational wording.  The fixed order
is:

1. preserve the canonical character identity;
2. preserve the semantic function of the title/address form;
3. preserve the novel's established Sino-Vietnamese style;
4. prefer consistency with existing confirmed terminology;
5. only then optimize for modern Vietnamese naturalness.

This module is intentionally local and dependency-free: it derives the batch
register from confirmed terminology, detects address/title shapes, and either
normalizes or rejects a form whose register contradicts that batch.  Semantic
work (is this a literal family relationship or a social address form?) still
belongs to the resolver; it receives the profile and the detected shape.
"""

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Dict

SINO_VIETNAMESE = "sino-vietnamese"
MODERN = "modern"
AUTO = "auto"
REGISTER_ENVIRONMENT = "TRANSLATION_DICTIONARY_REGISTER"
MIN_EVIDENCE_ENVIRONMENT = "TRANSLATION_DICTIONARY_REGISTER_MIN_EVIDENCE"
MIN_DOMINANCE_ENVIRONMENT = "TRANSLATION_DICTIONARY_REGISTER_MIN_DOMINANCE"
DEFAULT_MIN_EVIDENCE = 2
DEFAULT_MIN_DOMINANCE = 0.6
CHARACTER_TYPES = frozenset({"character", "character_form"})

# Character-reference kinds.  Every confirmed Chinese reference form belongs to
# exactly one of these, so compatible forms can be compared within a class.
KINDS = (
    "literal_kinship",
    "social_honorific",
    "official_title",
    "rank",
    "role_reference",
    "nickname",
    "alias",
)


@dataclass(frozen=True)
class AddressSpec:
    """Preferred renderings for one Chinese address/title suffix."""

    kind: str
    honorific: str
    modern: str
    kinship: bool = False
    title_first: bool = False


# Suffix -> spec.  The Sino-Vietnamese rendering is the established project
# reading; ``modern`` is recorded only so a mismatch can be detected and
# explained, never to be preferred without an explicit register override.
ADDRESS_SPECS: Dict[str, AddressSpec] = {
    # Kinship-style address forms (social honorifics unless RAW proves kinship).
    "哥": AddressSpec("social_honorific", "ca", "anh", kinship=True),
    "姐": AddressSpec("social_honorific", "tỷ", "chị", kinship=True),
    "弟": AddressSpec("social_honorific", "đệ", "em", kinship=True),
    "妹": AddressSpec("social_honorific", "muội", "em", kinship=True),
    "叔": AddressSpec("social_honorific", "thúc", "chú", kinship=True),
    "伯": AddressSpec("social_honorific", "bá", "bác", kinship=True),
    "嫂": AddressSpec("social_honorific", "tẩu", "chị dâu", kinship=True),
    "婶": AddressSpec("social_honorific", "thẩm", "thím", kinship=True),
    "爷": AddressSpec("social_honorific", "gia", "ông", kinship=True),
    "少爷": AddressSpec("social_honorific", "thiếu gia", "cậu chủ", kinship=True),
    "小姐": AddressSpec("social_honorific", "tiểu thư", "cô", kinship=True),
    "太太": AddressSpec("social_honorific", "phu nhân", "bà", kinship=True),
    "夫人": AddressSpec("social_honorific", "phu nhân", "bà", kinship=True),
    "公子": AddressSpec("social_honorific", "công tử", "cậu", kinship=True),
    "姑娘": AddressSpec("social_honorific", "cô nương", "cô gái", kinship=True),
    "师兄": AddressSpec("social_honorific", "sư huynh", "anh"),
    "师姐": AddressSpec("social_honorific", "sư tỷ", "chị"),
    "师弟": AddressSpec("social_honorific", "sư đệ", "em"),
    "师妹": AddressSpec("social_honorific", "sư muội", "em"),
    "前辈": AddressSpec("social_honorific", "tiền bối", "tiền bối"),
    "先生": AddressSpec("social_honorific", "tiên sinh", "ông"),
    "大人": AddressSpec("social_honorific", "đại nhân", "ngài"),
    "阁下": AddressSpec("social_honorific", "các hạ", "ngài"),
    "殿下": AddressSpec("social_honorific", "điện hạ", "ngài"),
    # Historical forms of address and office references.  These are kept as
    # Sino-Vietnamese readings rather than modern paraphrases because the
    # configured register is part of the frozen dictionary contract.
    "大伴": AddressSpec("social_honorific", "Đại bạn", "attendant"),
    "尚书": AddressSpec("official_title", "Thượng thư", "minister"),
    # Official titles and offices.
    "将军": AddressSpec("official_title", "tướng quân", "tướng"),
    "科长": AddressSpec("official_title", "khoa trưởng", "trưởng khoa"),
    "处长": AddressSpec("official_title", "xử trưởng", "trưởng phòng"),
    "署长": AddressSpec("official_title", "thự trưởng", "trưởng sở"),
    "部长": AddressSpec("official_title", "bộ trưởng", "trưởng ban"),
    "长官": AddressSpec("official_title", "trưởng quan", "sếp", title_first=True),
    # Ranks, roles and standing references.
    "帅": AddressSpec("rank", "soái", "marshal"),
    "缇帅": AddressSpec("role_reference", "Đề soái", "commander"),
    "侍班": AddressSpec("role_reference", "Thị ban", "attendant"),
    "上校": AddressSpec("rank", "thượng tá", "thượng tá"),
    "中校": AddressSpec("rank", "trung tá", "trung tá"),
    "少校": AddressSpec("rank", "thiếu tá", "thiếu tá"),
    "队长": AddressSpec("rank", "đội trưởng", "đội trưởng"),
    "主任": AddressSpec("role_reference", "chủ nhiệm", "chủ nhiệm"),
    "掌门": AddressSpec("role_reference", "chưởng môn", "chưởng môn"),
    "探员": AddressSpec("role_reference", "thám viên", "điều tra viên"),
    "警官": AddressSpec("role_reference", "cảnh quan", "cảnh sát"),
    "医生": AddressSpec("role_reference", "y sư", "bác sĩ"),
    "老师": AddressSpec("role_reference", "lão sư", "thầy giáo"),
}

# Prefix titles are parsed separately from suffix titles because the person
# name follows the office: 大司徒王国光.  The trailing person must be a complete
# canonical identity; a shorter substring is never a valid canonical source.
TITLE_PREFIX_SPECS: Dict[str, AddressSpec] = {
    "大司徒": AddressSpec("official_title", "Đại Tư đồ", "grand minister", title_first=True),
}
HISTORICAL_REFERENCE_SUFFIXES = frozenset({"大伴", "尚书", "帅", "缇帅", "侍班"})

# Longest suffix first so 副署长/少爷/师姐 are matched before 署长/爷/姐.
ADDRESS_SUFFIX = re.compile(
    "(?:"
    + "|".join(
        re.escape(suffix) for suffix in sorted(ADDRESS_SPECS, key=len, reverse=True)
    )
    + ")$"
)
KINSHIP_SUFFIXES = tuple(
    suffix for suffix, spec in ADDRESS_SPECS.items() if spec.kinship
)
NICKNAME = re.compile(r"^(?:老|小|阿)[\u3400-\u9fff]{1,3}$")
NICKNAME_SPECS = {
    "老": AddressSpec("nickname", "lão", "ông", title_first=True),
    "小": AddressSpec("nickname", "tiểu", "bé", title_first=True),
    "阿": AddressSpec("nickname", "a", "bé", title_first=True),
}
ORDINAL_HONORIFIC = re.compile(r"^([一二三四五六七八九十])(爷|哥|叔|伯)$")
NUMERALS = {
    "一": "Nhất",
    "二": "Nhị",
    "三": "Tam",
    "四": "Tứ",
    "五": "Ngũ",
    "六": "Lục",
    "七": "Thất",
    "八": "Bát",
    "九": "Cửu",
    "十": "Thập",
}
LITERAL_KINSHIP_CUE = re.compile(
    r"(?:亲|血亲|同父|同母|亲生|嫡亲|胞)[哥姐弟妹]"
    r"|(?:堂|表)[哥姐弟妹]"
    r"|(?:亲|亲生|嫡)(?:叔叔|伯伯)"
)
COMPOUND_SURNAMES = (
    "Âu Dương",
    "Tư Mã",
    "Gia Cát",
    "Thượng Quan",
    "Mộ Dung",
    "Đông Phương",
    "Độc Cô",
    "Hoàng Phủ",
    "Trưởng Tôn",
    "Hạ Hầu",
    "Công Tôn",
)
HAN = re.compile(r"[\u3400-\u9fff]")
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
SPACE = re.compile(r"\s+")

# Vietnamese register markers, as whole phrases.  Longest match wins, so
# ``cô nương`` is a Sino form even though ``cô`` alone is colloquial.  A
# rendering with no marker is unclassified and is never rewritten.
SINO_MARKERS = frozenset(
    {
        # Kinship-style honorifics and standing references.
        "tỷ",
        "muội",
        "đệ",
        "ca",
        "huynh",
        "gia",
        "thúc",
        "bá",
        "tẩu",
        "thẩm",
        "lão",
        "tiểu",
        "a",
        "phó",
        "thiếu gia",
        "tiểu thư",
        "công tử",
        "cô nương",
        "phu nhân",
        "tiên sinh",
        "đại nhân",
        "các hạ",
        "điện hạ",
        "bệ hạ",
        "tiền bối",
        "sư huynh",
        "sư tỷ",
        "sư đệ",
        "sư muội",
        # Official titles, ranks and offices.
        "tướng quân",
        "khoa trưởng",
        "xử trưởng",
        "thự trưởng",
        "bộ trưởng",
        "trưởng quan",
        "chủ nhiệm",
        "chưởng môn",
        "thám viên",
        "cảnh quan",
        "y sư",
        "lão sư",
        "đội trưởng",
        "thượng tá",
        "đại tá",
        "trung tá",
        "thiếu tá",
        "thượng tướng",
        "trung tướng",
        "thiếu tướng",
        "quận chúa",
        "công chúa",
        "vương gia",
        "thái tử",
        "phò mã",
        "công công",
        "đại bạn",
        "thượng thư",
        "soái",
        "đề soái",
        "thị ban",
        "đại tư đồ",
    }
)
MODERN_MARKERS = frozenset(
    {
        "anh",
        "chị",
        "em",
        "ông",
        "bà",
        "cô",
        "chú",
        "bác",
        "cậu",
        "mợ",
        "dì",
        "thím",
        "sếp",
        "ngài",
        "nhóc",
        "con",
        "cháu",
        "tướng",
        "anh trai",
        "chị gái",
        "em trai",
        "em gái",
        "chị dâu",
        "ông chủ",
        "cậu chủ",
        "cô gái",
        "cảnh sát",
        "bác sĩ",
        "thầy giáo",
        "cô giáo",
        "điều tra viên",
        "trưởng khoa",
        "trưởng phòng",
        "trưởng sở",
        "trưởng ban",
    }
)
MAX_MARKER_WORDS = max(
    len(marker.split()) for marker in SINO_MARKERS | MODERN_MARKERS
)


def words(value):
    return [word.casefold() for word in WORD.findall(unicodedata.normalize("NFC", value or ""))]


def register_of(value):
    """Classify a Vietnamese address rendering as Sino-Vietnamese or modern.

    ``None`` means the rendering carries no known register marker; such a value
    is left untouched by the policy.
    """
    tokens = words(value)
    if not tokens:
        return None
    for size in range(min(MAX_MARKER_WORDS, len(tokens)), 0, -1):
        for index in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[index : index + size])
            if phrase in SINO_MARKERS:
                return SINO_VIETNAMESE
            if phrase in MODERN_MARKERS:
                return MODERN
    return None


def configured_register():
    """Explicit register override; ``sino-vietnamese`` is the project default."""
    value = (os.getenv(REGISTER_ENVIRONMENT) or "").strip().casefold()
    if value == AUTO:
        return AUTO
    if value == MODERN:
        return MODERN
    return SINO_VIETNAMESE


def address_spec(form, canonical="", contexts=()):
    """Return the address/title shape of a Chinese reference form, or ``None``.

    The returned kind is one of ``literal_kinship``, ``social_honorific``,
    ``official_title``, ``rank``, ``role_reference`` or ``nickname``.  A form
    without a known address shape stays a plain alias and is never rewritten.
    """
    form = form or ""
    if not form or not HAN.search(form):
        return None
    contexts = [text for text in contexts or () if text and form in text]
    literal = bool(LITERAL_KINSHIP_CUE.search(form)) or any(
        LITERAL_KINSHIP_CUE.search(text) for text in contexts
    )
    ordinal = ORDINAL_HONORIFIC.fullmatch(form)
    if ordinal:
        spec = ADDRESS_SPECS[ordinal.group(2)]
        return _spec(
            spec,
            honorific=f"{NUMERALS[ordinal.group(1)]} {spec.honorific}",
            suffix=ordinal.group(2),
            surname="",
            literal=literal,
        )
    if NICKNAME.fullmatch(form):
        spec = NICKNAME_SPECS[form[0]]
        return _spec(
            spec,
            honorific=spec.honorific,
            suffix="",
            surname="",
            literal=False,
            title_prefix="",
        )
    for prefix in sorted(TITLE_PREFIX_SPECS, key=len, reverse=True):
        if not form.startswith(prefix):
            continue
        person = form[len(prefix) :]
        if len(person) < 2 or not re.fullmatch(r"[\u3400-\u9fff]+", person):
            return None
        spec = TITLE_PREFIX_SPECS[prefix]
        result = _spec(
            spec,
            honorific=spec.honorific,
            suffix="",
            surname="",
            literal=literal,
            title_prefix="",
        )
        result.update(
            prefix_reference=True,
            title_prefix_han=prefix,
            person_source=person,
        )
        return result
    suffix, title_prefix = None, ""
    for candidate in sorted(ADDRESS_SPECS, key=len, reverse=True):
        if form.endswith("副" + candidate):
            suffix, title_prefix = candidate, "Phó"
            break
        if form.endswith(candidate):
            suffix = candidate
            break
    if suffix is None:
        return None
    spec = ADDRESS_SPECS[suffix]
    consumed = len(suffix) + (1 if title_prefix else 0)
    prefix = form[:-consumed]
    if prefix and not re.fullmatch(r"[\u3400-\u9fff]+", prefix):
        return None
    honorific = spec.honorific
    numeral = re.fullmatch(r"(.+)([一二三四五六七八九十])", prefix)
    if numeral and form.endswith(("爷", "哥", "叔", "伯")):
        honorific = f"{NUMERALS[numeral.group(2)]} {honorific}"
        prefix = numeral.group(1)
    return _spec(
        spec,
        honorific=honorific,
        suffix=suffix,
        surname=prefix,
        literal=literal,
        title_prefix=title_prefix,
    )


def _spec(spec, honorific, suffix, surname, literal, title_prefix=None):
    return {
        "kind": spec.kind,
        "honorific": honorific,
        "modern": spec.modern,
        "suffix": suffix,
        "surname": surname,
        "title_prefix": title_prefix if title_prefix is not None else "",
        "title_first": spec.title_first or bool(title_prefix),
        "literal_kinship_hint": bool(literal and spec.kinship),
        "prefix_reference": False,
        "title_prefix_han": "",
        "person_source": "",
    }


def title_prefix_parts(form):
    """Return ``(prefix, trailing_person)`` for a known title prefix."""
    for prefix in sorted(TITLE_PREFIX_SPECS, key=len, reverse=True):
        if form.startswith(prefix):
            person = form[len(prefix) :]
            if len(person) >= 2 and re.fullmatch(r"[\u3400-\u9fff]+", person):
                return prefix, person
    return None


def person_token(han, canonical_source, canonical_translation, allow_suffix=False):
    """Map a Chinese surname/given fragment onto the canonical Vietnamese name."""
    han = han or ""
    if not han or not canonical_source or not canonical_translation:
        return None
    if canonical_source.startswith(han):
        index = 0
    elif allow_suffix and canonical_source.endswith(han):
        index = -1
    else:
        return None
    tokens = canonical_translation.split()
    if not tokens:
        return None
    if index == 0 and len(tokens) >= 2 and " ".join(tokens[:2]) in COMPOUND_SURNAMES:
        return " ".join(tokens[:2])
    return tokens[index]


def preferred_form(form, canonical_source, canonical_translation, spec):
    """Deterministic established-register rendering of one Chinese address form."""
    if spec is None:
        return None
    honorific = spec["honorific"]
    if not honorific:
        return None
    if spec.get("prefix_reference"):
        if spec.get("person_source") != canonical_source:
            return None
        text = f"{honorific} {canonical_translation}"
        return text[:1].upper() + text[1:]
    person = None
    if not spec["suffix"] and form and form[0] in NICKNAME_SPECS:
        person = person_token(
            form[1:], canonical_source, canonical_translation, allow_suffix=True
        )
    elif spec["surname"]:
        person = person_token(spec["surname"], canonical_source, canonical_translation)
        if person is None:
            return None
    honorific = (
        f"{spec['title_prefix']} {honorific}" if spec["title_prefix"] else honorific
    )
    if person is None:
        return honorific[:1].upper() + honorific[1:]
    if spec["title_first"]:
        text = f"{honorific} {person}"
        return text[:1].upper() + text[1:]
    return f"{person} {honorific}"


def conflict(spec, translation, register):
    """Explain why a rendered address form contradicts the batch register."""
    if spec is None or not translation or register not in (SINO_VIETNAMESE, MODERN):
        return None
    actual = register_of(translation)
    if actual is None or actual == register:
        return None
    return (
        f"{spec['kind']} reference uses {actual} wording "
        f"while the batch register is {register}"
    )


def normalize_forms(term, register, contexts=()):
    """Keep one established-register rendering per confirmed Chinese form.

    Returns ``(kinds, normalizations, removed)``.  A form whose wording
    contradicts the batch register is rewritten to the deterministic
    established rendering when the shape permits, and otherwise dropped.  The
    canonical identity and the canonical translation are never modified, and no
    second Vietnamese rendering is ever added for one Chinese key.
    """
    kinds, normalizations, removed = {}, [], []
    if getattr(term, "type", None) not in CHARACTER_TYPES:
        return kinds, normalizations, removed
    source = getattr(term, "source", "")
    declared = {
        name: kind
        for name, kind in dict(getattr(term, "form_kinds", None) or {}).items()
        if kind in KINDS
    }
    for name, value in list(getattr(term, "forms", {}).items()):
        spec = address_spec(name, source, contexts)
        if spec is None:
            continue
        kinds[name] = declared.get(name, spec["kind"])
        reason = conflict(spec, value, register)
        historical = bool(
            register == SINO_VIETNAMESE
            and (
                spec.get("suffix") in HISTORICAL_REFERENCE_SUFFIXES
                or spec.get("prefix_reference")
                and spec.get("title_prefix_han") in TITLE_PREFIX_SPECS
            )
        )
        if reason is None and not historical:
            continue
        replacement = preferred_form(name, source, term.translation, spec)
        if replacement == value:
            continue
        if (
            replacement
            and replacement != value
            and (historical or register_of(replacement) == register)
        ):
            term.forms[name] = replacement
            normalizations.append(
                {
                    "source": name,
                    "from": value,
                    "to": replacement,
                    "kind": spec["kind"],
                    "reason": reason or "deterministic historical reference rendering",
                }
            )
            continue
        term.forms.pop(name, None)
        kinds.pop(name, None)
        removed.append({"source": name, "reason": reason, "field": "forms"})
    if kinds:
        term.form_kinds = {
            name: kind for name, kind in kinds.items() if name in term.forms
        }
        term.address_register = register
    return kinds, normalizations, removed
def _value(term, name, default=None):
    if isinstance(term, dict):
        return term.get(name, default)
    return getattr(term, name, default)


def _dominant(counter, dominance_min):
    total = sum(counter.values())
    if not total:
        return "unestablished"
    leader, count = counter.most_common(1)[0]
    return leader if count / total >= dominance_min else "unestablished"


def profile(terms, min_evidence=None, min_dominance=None):
    """Derive the established register from confirmed address forms.

    Comparison is class-scoped: kinship-style honorifics are compared with
    kinship-style honorifics, official titles with official titles.  A class
    without evidence falls back to the batch register, which is what the
    resolver receives for that candidate.
    """
    evidence_min = min_evidence or thresholds()[0]
    dominance_min = min_dominance or thresholds()[1]
    counts, classes, samples = Counter(), {}, []
    for term in terms or ():
        if _value(term, "type") not in CHARACTER_TYPES:
            continue
        source = _value(term, "source", "") or ""
        declared = _value(term, "form_kinds", {}) or {}
        for name, value in (_value(term, "forms", {}) or {}).items():
            spec = address_spec(name, source)
            if spec is None:
                continue
            kind = declared.get(name) if declared.get(name) in KINDS else spec["kind"]
            actual = register_of(value)
            if actual is None:
                continue
            counts[actual] += 1
            classes.setdefault(kind, Counter())[actual] += 1
            if len(samples) < 8:
                samples.append(
                    {
                        "form": name,
                        "translation": value,
                        "kind": kind,
                        "register": actual,
                    }
                )
    total = sum(counts.values())
    dominant = None
    if total >= evidence_min and counts:
        leader, count = counts.most_common(1)[0]
        if count / total >= dominance_min:
            dominant = leader
    return {
        "dominant": dominant or "unestablished",
        "evidence": total,
        "counts": dict(counts),
        "classes": {
            kind: {**dict(entry), "dominant": _dominant(entry, dominance_min)}
            for kind, entry in sorted(classes.items())
        },
        "samples": samples,
        "thresholds": {"min_evidence": evidence_min, "min_dominance": dominance_min},
    }


def effective_register(configured, derived=None):
    """Resolve the register actually enforced for a batch."""
    if configured in (SINO_VIETNAMESE, MODERN):
        return configured, "configured"
    dominant = (derived or {}).get("dominant")
    if dominant in (SINO_VIETNAMESE, MODERN):
        return dominant, "confirmed-forms"
    return SINO_VIETNAMESE, "baseline"


def stored_register(data, terms=()):
    """Register recorded by a previous freeze, so re-freezing is stable."""
    nested = data.get("dictionary") if isinstance(data, dict) else None
    for value in (
        data.get("register") if isinstance(data, dict) else None,
        nested.get("register") if isinstance(nested, dict) else None,
        *[_value(term, "address_register") for term in terms or ()],
    ):
        if value in (SINO_VIETNAMESE, MODERN):
            return value
    return None


def resolver_context(register, register_source, derived=None):
    """Compact batch style context attached to one resolver request."""
    derived = derived or {}
    return {
        "register": register,
        "register_source": register_source,
        "priority": [
            "canonical character identity",
            "semantic function of the title/address form",
            "established Sino-Vietnamese style",
            "existing confirmed terminology",
            "modern Vietnamese naturalness",
        ],
        "rule": (
            "One preferred Vietnamese rendering per confirmed Chinese reference "
            "form. Address/title forms keep the batch register; never mix "
            "colloquial kinship words into a Sino-Vietnamese dictionary unless "
            "RAW clearly requires modern conversational Vietnamese."
        ),
        "derived_from_confirmed": derived.get("dominant", "unestablished"),
        "evidence": derived.get("evidence", 0),
        "by_kind": {
            kind: entry.get("dominant")
            for kind, entry in (derived.get("classes") or {}).items()
        },
        "examples": derived.get("samples", [])[:6],
    }


def address_payload(
    form,
    canonical_source="",
    canonical_translation="",
    register=SINO_VIETNAMESE,
    contexts=(),
):
    """Per-candidate address metadata for the resolver and the audit."""
    spec = address_spec(form, canonical_source, contexts)
    if spec is None:
        return None
    payload = {**spec, "expected_register": register}
    preferred = (
        preferred_form(form, canonical_source, canonical_translation, spec)
        if canonical_translation
        else None
    )
    if preferred:
        payload["preferred_rendering"] = preferred
    return payload


def positive_number(name, default, cast):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def thresholds():
    return (
        positive_number(MIN_EVIDENCE_ENVIRONMENT, DEFAULT_MIN_EVIDENCE, int),
        positive_number(MIN_DOMINANCE_ENVIRONMENT, DEFAULT_MIN_DOMINANCE, float),
    )
