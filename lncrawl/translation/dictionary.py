"""One canonical namespace; legacy records are quarantined, never blindly trusted."""

import re
import unicodedata
from collections import Counter

from .models import Term
from .parsing import HAN

ORDINARY = set(
    "不会 继续 或者 直接 刚才 只要 连忙 眼睛 比如 主动 只能 轻轻 小声 不好 抬头 故意 开口 不再 嘴里 低声 有没有 一边 世界 中山装 方法 温柔 任何 任命 任务".split()
)
FRAGMENTS = ("也", "不", "没", "竟然", "缓缓", "有")


def source_name(source):
    """Outer book/system markers are typography, not part of a Chinese name."""
    markers = {"【": "】", "《": "》", "「": "」", "『": "』", "“": "”"}
    while len(source) > 2 and markers.get(source[0]) == source[-1]:
        source = source[1:-1]
    return source


def quantity_source_problem(source, contexts=()):
    if not re.match(r"^\d+(?:[-–~～]\d+)?(?:队|名|个|件|支|位|座|瓶|粒|箱|套|辆|艘|枚)", source):
        return None
    named = re.compile(
        r"(?:名为|称为|命名为|代号为|番号为|名字是|名称是)[\s：:\"“‘「『]*" + re.escape(source)
    )
    for text in contexts:
        if any(
            left + source + right in text for left, right in (("《", "》"), ("【", "】"))
        ) or named.search(text):
            return None
    return "quantity modifier is not a canonical entity name"


def source_problem(source, known=()):
    if (
        not source
        or not HAN.search(source)
        or re.search(r"[\s，。！？：；\x00-\x1f\ufffd\ud800-\udfff]", source)
    ):
        return "invalid source span"
    if source in ORDINARY:
        return "ordinary vocabulary"
    if any(
        source == entity + tail or source == "听到" + entity
        for entity in known
        for tail in FRAGMENTS
    ):
        return "entity plus grammar fragment"
    return None


def term_problem(term, known=()):
    problem = source_problem(term.source, known)
    if problem:
        return problem
    if (
        not term.translation.strip()
        or HAN.search(term.translation)
        or re.search(r"[\x00-\x1f\ufffd\ud800-\udfff]", term.translation)
        or term.translation in ("ực đẹp", "ạnh được", "ất đầu", "âu đồ")
    ):
        return "malformed translation"
    if term.type != "character" and term.gender != "unknown":
        return "unsupported gender on non-character"
    if any(source_problem(a, known) for a in [*term.aliases, *term.forms]):
        return "invalid alias/form"
    if any(
        not value.strip()
        or HAN.search(value)
        or re.search(r"[\x00-\x1f\ufffd\ud800-\udfff]", value)
        for value in term.forms.values()
    ):
        return "malformed address form translation"
    if len(set(term.aliases)) != len(term.aliases) or term.source in term.aliases:
        return "duplicate alias"
    return None


def load_legacy(data):
    """Return structured candidates and internal problems for evidence-aware vetting."""
    if data is None:
        return [], []
    records, problems = [], []
    if "entries" in data:
        if not isinstance(data["entries"], list):
            raise ValueError("Dictionary entries must be an array")
        records.extend(data["entries"])
    else:
        for section, category in (
            ("glossary", "other_term"),
            ("characters", "character"),
            ("locations", "location"),
        ):
            values = data.get(section, {})
            if isinstance(values, dict):
                for source, value in values.items():
                    item = {"translation": value} if isinstance(value, str) else dict(value)
                    records.append({"source": source, "type": category, **item})
            elif isinstance(values, list):
                records.extend({"type": category, **value} for value in values)
            else:
                raise ValueError(f"Invalid legacy dictionary section {section}")
        if not records and data:
            raise ValueError(
                "Dictionary must contain entries or legacy glossary/characters/locations"
            )
    terms = {}
    conflicts = set()
    for record in records:
        try:
            # Missing legacy state is provisional, never implicitly locked.
            # The canonical namespace uses Chinese source keys, not external IDs
            # or reference records. Unsupported fields/broken reference formats
            # are quarantined by the strict schema, without semantic API calls.
            term = Term.model_validate(record)
            problem = term_problem(term)
            if problem:
                raise ValueError(problem)
            existing = terms.get(term.source)
            if existing and (
                existing.translation != term.translation
                or existing.type != term.type
                or any(
                    key in existing.forms and existing.forms[key] != value
                    for key, value in term.forms.items()
                )
            ):
                conflicts.add(term.source)
                terms[term.source].evidence += (
                    f" Legacy conflict: {terms[term.source].translation} versus {term.translation}."
                    " Must resolve against RAW; neither mapping is trusted."
                )
                problems.append(
                    {
                        "classification": "suspicious",
                        "record": record,
                        "reason": "cross-section conflict",
                    }
                )
            elif existing:
                existing.aliases = sorted(set(existing.aliases + term.aliases))
                existing.forms.update(term.forms)
                if term.status == "locked":
                    existing.status = "locked"
                problems.append(
                    {
                        "classification": "invalid",
                        "record": record,
                        "reason": "duplicate source merged locally; locked mapping preserved",
                    }
                )
            else:
                terms[term.source] = term
        except (ValueError, TypeError, AttributeError) as exc:
            problems.append({"classification": "invalid", "record": record, "reason": str(exc)})
    for source in conflicts:
        terms[source].status = "provisional"
    return list(terms.values()), problems


def sanity(terms):
    errors, owners = [], {}
    translations = Counter(t.translation.casefold() for t in terms.values())
    for source, term in terms.items():
        problem = term_problem(term, terms)
        if problem:
            errors.append(f"{source}: {problem}")
        if translations[term.translation.casefold()] > 1:
            errors.append(f"{source}: unrelated canonical terms share translation; resolve aliases")
        for name in [source, *term.aliases, *term.forms]:
            if name in owners and owners[name] != source:
                errors.append(f"{name}: alias/canonical collision")
            owners[name] = source
    if errors:
        raise ValueError("Dictionary sanity failed: " + "; ".join(errors[:20]))


def relevant(terms, text):
    return [
        t.model_dump()
        for t in terms.values()
        if any(key in text for key in [t.source, *t.aliases, *t.forms])
    ]


def terminology_gaps(terms, raw, translated):
    """Check mappings at RAW occurrences; never replace Vietnamese prose globally.

    Longer source names take precedence over overlapping shorter entries. Address
    forms use their own mappings, and capitalization/Unicode spacing are immaterial.
    This checks attested wording locally; semantic equivalence still depends on
    the translation model, rather than a generic AI review request.
    """
    mappings = {}
    for term in terms:
        for name in [term["source"], *term.get("aliases", []), *term.get("forms", {})]:
            mappings[name] = term.get("forms", {}).get(name, term["translation"])
    if not mappings:
        return []

    def normalized(text):
        return " ".join(unicodedata.normalize("NFC", text).casefold().split())

    pattern = re.compile(
        "|".join(re.escape(name) for name in sorted(mappings, key=len, reverse=True))
    )
    target = normalized(translated)
    return sorted(
        {
            (match.group(), mappings[match.group()])
            for match in pattern.finditer(raw)
            if normalized(mappings[match.group()]) not in target
        }
    )


def export_dictionary(terms):
    sanity(terms)
    return {
        "version": 1,
        "entries": [terms[s].model_dump() for s in sorted(terms)],
        "statistics": {
            "total_terms": len(terms),
            "total_characters": sum(t.type == "character" for t in terms.values()),
            "total_locations": sum(t.type == "location" for t in terms.values()),
        },
    }
