"""One canonical namespace; legacy records are quarantined, never blindly trusted."""

import re
import unicodedata
from collections import Counter, defaultdict

from .models import CONFIRMED, DICTIONARY_VERSION, IGNORE, Term
from .parsing import HAN

ORDINARY = set(
    "不会 继续 或者 直接 刚才 只要 连忙 眼睛 比如 主动 只能 轻轻 小声 不好 抬头 故意 开口 不再 嘴里 低声 有没有 一边 世界 中山装 方法 温柔 任何 任命 任务".split()
)
FRAGMENTS = ("也", "不", "没", "竟然", "缓缓", "有")
CHARACTER_TYPES = {"character", "character_form"}
REFERENCE_SUFFIXES = (
    "副署长",
    "副科长",
    "探员",
    "署长",
    "科长",
    "处长",
    "将军",
    "上校",
    "中校",
    "少校",
    "队长",
    "长官",
    "主任",
    "警官",
    "医生",
    "老师",
    "先生",
    "小姐",
    "夫人",
    "阁下",
    "殿下",
    "大人",
    "前辈",
    "师兄",
    "师姐",
    "师弟",
    "师妹",
    "掌门",
)
REFERENCE_SUFFIX = re.compile(
    r"(?:" + "|".join(map(re.escape, REFERENCE_SUFFIXES)) + r")$"
)
NICKNAME = re.compile(r"^(?:老|小|阿)[\u3400-\u9fff]{1,3}$")
IDENTITY_CUE = re.compile(
    r"(?:就是|是|名为|名字是|叫做|称为|原名|又名|也叫|被称为|"
    r"升为|晋升为|改称|改任|成为)"
)


def source_name(source):
    """Outer book/system markers are typography, not part of a Chinese name."""
    markers = {"【": "】", "《": "》", "「": "」", "『": "』", "“": "”"}
    while len(source) > 2 and markers.get(source[0]) == source[-1]:
        source = source[1:-1]
    return source


def quantity_source_problem(source, contexts=()):
    contexts = list(contexts)
    numeric = re.match(
        r"^\d+(?:[-–~～]\d+)?(?:队|名|个|件|支|位|座|瓶|粒|箱|套|辆|艘|枚|块|颗|株|片)",
        source,
    )
    measure_prefix = re.match(r"^(?:块|颗|株|片)(.+)$", source)
    if not numeric and not measure_prefix:
        return None
    if measure_prefix:
        occurrences = [
            match
            for text in contexts
            for match in re.finditer(re.escape(source), text)
        ]
        if not occurrences:
            return None
        # A leading measure word is a fragment only when every attested use is
        # preceded by a quantity/demonstrative. Named markers and naming cues win.
        for text in contexts:
            if source in text and re.search(
                r"(?:\d|[这那一两几多])" + re.escape(source), text
            ) is None:
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


def _identity_evidence(name, canonical, contexts=(), evidence=""):
    """Return whether RAW/evidence explicitly links a reference to a person."""
    values = [*contexts, evidence]
    if not name.startswith(canonical) and any(
        name + canonical in value or canonical + name in value
        for value in values
        if value
    ):
        return True
    pair = re.compile(
        r"(?:"
        + re.escape(name)
        + r".{0,24}"
        + IDENTITY_CUE.pattern
        + r".{0,24}"
        + re.escape(canonical)
        + r"|"
        + re.escape(canonical)
        + r".{0,24}"
        + IDENTITY_CUE.pattern
        + r".{0,24}"
        + re.escape(name)
        + r")"
    )
    return any(pair.search(value) for value in values if value)


def identity_form_problem(term, name, contexts=(), require_evidence=False):
    """Reject contextual sentence extensions from character aliases/forms.

    A form is accepted only as an independently shaped reference expression or
    when RAW/evidence explicitly links it to the canonical character.  A name
    followed by arbitrary Han text is never made safe by substring attestation.
    """
    if term.type not in CHARACTER_TYPES or name == term.source:
        return None
    if source_problem(name):
        return "invalid alias/form"
    evidence = _identity_evidence(name, term.source, contexts, term.evidence)
    suffix = name[len(term.source) :] if name.startswith(term.source) else ""
    if suffix:
        if REFERENCE_SUFFIX.search(suffix) or evidence:
            return None
        return "canonical character plus contextual residue"
    if REFERENCE_SUFFIX.search(name) or NICKNAME.fullmatch(name):
        if require_evidence and not evidence:
            return "character reference identity is not independently proven"
        return None
    if evidence:
        return None
    if require_evidence:
        return "character reference identity is not independently proven"
    return "unrecognized character reference form"


def clean_identity_forms(term, contexts=(), require_evidence=False):
    """Remove contaminated character forms while preserving the canonical term."""
    if term.type not in CHARACTER_TYPES:
        return [], []
    removed, preserved = [], []
    aliases = []
    for name in term.aliases:
        problem = identity_form_problem(term, name, contexts, require_evidence)
        if problem:
            removed.append({"source": name, "reason": problem, "field": "aliases"})
        else:
            aliases.append(name)
            preserved.append(name)
    forms = {}
    for name, translation in term.forms.items():
        problem = identity_form_problem(term, name, contexts, require_evidence)
        if problem:
            removed.append({"source": name, "reason": problem, "field": "forms"})
        else:
            forms[name] = translation
            preserved.append(name)
    term.aliases = sorted(set(aliases))
    term.forms = forms
    return removed, sorted(set(preserved))


def term_problem(term, known=()):
    if term.status == "report_only":
        if (
            not term.source
            or not HAN.search(term.source)
            or re.search(r"[\x00-\x1f\ufffd\ud800-\udfff]", term.source)
        ):
            return "invalid report-only source span"
        return None
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
    if term.type not in ("character", "character_form") and term.gender != "unknown":
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
            record = dict(record)
            legacy_status = str(record.get("status", "")).casefold()
            if legacy_status in {
                "locked",
                "confirmed",
                "verified_local_consensus",
                "trusted",
                "trusted_user_mapping",
                "trusted user mapping",
            } or record.get("trusted") is True or record.get("user_confirmed") is True:
                record["status"] = "locked"
                record["enforceable"] = True
            elif legacy_status in {"confirmed", "ignore", "report_only"}:
                record["status"] = "report_only"
                record["enforceable"] = False
            # v5/v6 checkpoints sometimes stored a report-only fallback as a
            # provisional term.  Migrate it before strict validation so it can
            # never re-enter chapter enforcement on resume.
            if record.get("fallback") == "report_only" and record.get("status") != "locked":
                record["status"] = "report_only"
                record["enforceable"] = False
            # Missing legacy state is provisional, never implicitly locked.
            # The canonical namespace uses Chinese source keys, not external IDs
            # or reference records. Unsupported fields/broken reference formats
            # are quarantined by the strict schema, without semantic API calls.
            if record.get("canonical_id") or (
                "canonical_source" in record and record.get("canonical_source")
            ):
                raise ValueError("broken canonical reference")
            term = Term.model_validate(record)
            term.aliases = sorted(alias for alias in term.aliases if alias != term.source)
            term.forms = {
                name: value for name, value in term.forms.items() if name != term.source
            }
            removed, preserved = clean_identity_forms(term)
            if removed:
                problems.append(
                    {
                        "classification": "form_cleanup",
                        "source": term.source,
                        "removed_forms": removed,
                        "preserved_forms": preserved,
                        "reason": "removed contextual character pseudo-forms",
                    }
                )
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
                if existing.status == "locked" and term.status != "locked":
                    problems.append(
                        {
                            "classification": "suspicious",
                            "record": record,
                            "reason": "ignored non-confirmed contradictory mapping; locked mapping preserved",
                        }
                    )
                    continue
                if term.status == "locked" and existing.status != "locked":
                    terms[term.source] = term
                    problems.append(
                        {
                            "classification": "suspicious",
                            "record": record,
                            "reason": "confirmed mapping replaced non-confirmed contradictory mapping",
                        }
                    )
                    continue
                conflicts.add(term.source)
                terms[term.source].evidence += (
                    f" Legacy conflict: {terms[term.source].translation} versus {term.translation}."
                    " Must resolve against RAW; neither mapping is trusted."
                )
                problems.append(
                    {
                        "classification": "fatal"
                        if existing.status == "locked" and term.status == "locked"
                        else "suspicious",
                        "record": record,
                        "reason": "two locked canonical mappings conflict"
                        if existing.status == "locked" and term.status == "locked"
                        else "cross-section conflict",
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
            problems.append(
                {
                    "classification": "fatal" if "canonical reference" in str(exc) else "invalid",
                    "record": record,
                    "reason": str(exc),
                }
            )
    for source in conflicts:
        terms[source].status = "provisional"
    return list(terms.values()), problems


def sanity(terms, allow_shared_translations=True):
    errors, owners = [], {}
    # Only confirmed mappings are part of the strict namespace.  Ignored
    # diagnostics are deliberately not allowed to make a batch fail.
    strict = {source: term for source, term in terms.items() if term.enforceable}
    for source, term in strict.items():
        problem = term_problem(term, terms)
        if problem:
            errors.append(f"{source}: {problem}")
        if term.type in CHARACTER_TYPES:
            for name in [*term.aliases, *term.forms]:
                form_problem = identity_form_problem(term, name)
                if form_problem:
                    errors.append(f"{source}.{name}: {form_problem}")
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
        if t.enforceable and any(key in text for key in [t.source, *t.aliases, *t.forms])
    ]


def term_is_enforceable(term):
    """Return the frozen enforcement decision without guessing from wording.

    A missing field is treated as an old, pre-v2 record: locked entries remain
    strict, while an entirely unlabelled legacy mapping keeps the historical
    behavior used by direct helper callers.  Explicit provisional/review/
    report-only state is never strict.
    """
    runtime = term.get("runtime_state") if isinstance(term, dict) else getattr(term, "runtime_state", None)
    if runtime in {CONFIRMED, IGNORE}:
        return runtime == CONFIRMED
    status = term.get("status") if isinstance(term, dict) else getattr(term, "status", None)
    if status in {"report_only", "needs_review", "unresolved"}:
        return False
    if isinstance(term, dict):
        if "enforceable" in term:
            return bool(term["enforceable"])
        if "status" not in term:
            return True
        return status == "locked"
    return bool(getattr(term, "enforceable", False))


def _term_value(term, field, default=None):
    return term.get(field, default) if isinstance(term, dict) else getattr(term, field, default)


def _mapping_entries(terms):
    """Yield source-aware frozen mappings, excluding non-enforceable metadata."""
    if isinstance(terms, dict):
        terms = terms.values()
    result = []
    for term in terms:
        if not term_is_enforceable(term):
            continue
        source = _term_value(term, "source", "")
        translation = _term_value(term, "translation", "")
        forms = _term_value(term, "forms", {}) or {}
        aliases = _term_value(term, "aliases", []) or []
        for name in [source, *aliases, *forms]:
            result.append(
                {
                    "source": name,
                    "canonical_source": source,
                    "required_translation": forms.get(name, translation),
                }
            )
    return [item for item in result if item["source"]]


def _context(raw, start, end, radius=80):
    """Return context separately from an exact terminology span."""
    left = raw[max(0, start - radius) : start]
    right = raw[end : min(len(raw), end + radius)]
    return left, right


def _selected_source_occurrences(raw, mappings):
    """Select exact, non-overlapping confirmed keys from RAW.

    Matching is leftmost-longest, but the selected span is always the actual
    dictionary key.  In particular, a context character after a name can
    never be absorbed into the finding or its required translation.
    """
    by_start = defaultdict(list)
    for mapping in mappings:
        name = mapping["source"]
        for match in re.finditer(re.escape(name), raw):
            start, end = match.span()
            if raw[start:end] != name:
                # ``re.escape`` should make this impossible. Keep the check
                # explicit so a future field-mapping change cannot create an
                # expanded terminology occurrence.
                raise ValueError(
                    f"Internal terminology matcher error at {start}:{end}: "
                    f"{raw[start:end]!r} != {name!r}"
                )
            by_start[start].append({**mapping, "start": start, "end": end})

    selected = []
    cursor = 0
    for start in sorted(by_start):
        if start < cursor:
            continue
        occurrence = max(
            by_start[start],
            key=lambda item: (item["end"] - item["start"], item["source"]),
        )
        end = occurrence["end"]
        if raw[start:end] != occurrence["source"]:
            raise ValueError(
                f"Internal terminology matcher error at {start}:{end}: "
                f"{raw[start:end]!r} != {occurrence['source']!r}"
            )
        left, right = _context(raw, start, end)
        selected.append(
            {
                **occurrence,
                "matched_source": occurrence["source"],
                "left_context": left,
                "right_context": right,
                "context": left + occurrence["source"] + right,
            }
        )
        cursor = end
    return selected


def terminology_occurrences(terms, raw):
    """Return exact confirmed terminology spans in one RAW string.

    This is intentionally separate from ``terminology_findings`` so tests,
    resume diagnostics and repair tooling can inspect spans even when the
    translation already satisfies every mapping.
    """
    return _selected_source_occurrences(raw, _mapping_entries(terms))


def _normalized_count(text, expected):
    normalized_text = " ".join(unicodedata.normalize("NFC", text).casefold().split())
    normalized_expected = " ".join(unicodedata.normalize("NFC", expected).casefold().split())
    if not normalized_expected:
        return 0
    return len(list(re.finditer(re.escape(normalized_expected), normalized_text)))


def _actual_variant(expected, translated):
    """Best-effort diagnostic only; this never drives replacement or validation."""
    words = list(re.finditer(r"[\wÀ-ỹĐđ]+", translated, re.UNICODE))
    expected_words = [word for word in re.findall(r"[\wÀ-ỹĐđ]+", expected, re.UNICODE) if len(word) > 1]
    for expected_word in expected_words:
        for index, match in enumerate(words):
            if match.group().casefold() == expected_word.casefold():
                left = max(0, index - 1)
                right = min(len(words), index + 1)
                return translated[words[left].start() : words[right - 1].end()]
    return ""


def terminology_findings(terms, raw, translated, location=None):
    """Return structured failures for enforceable mappings in one RAW unit."""
    mappings = _mapping_entries(terms)
    if not mappings:
        return []
    by_name = {item["source"]: item for item in mappings}
    selected = terminology_occurrences(mappings, raw)
    grouped = Counter(item["source"] for item in selected)
    # Different Chinese identities may intentionally share one Vietnamese
    # target.  Count that target once across its confirmed source group;
    # validating each source against the same global surface independently
    # would produce a false failure whenever the target appears more than once.
    target_groups = defaultdict(list)
    for name, count in grouped.items():
        target_groups[unicodedata.normalize("NFC", by_name[name]["required_translation"]).casefold()].append(
            (name, count)
        )
    findings = []
    for group in target_groups.values():
        expected = by_name[group[0][0]]["required_translation"]
        matched_total = _normalized_count(translated, expected)
        # Presence is the reliable local contract. Exact surface counts are
        # not: natural Vietnamese may repeat a name while joining dialogue,
        # and two Chinese identities may share the same target wording.
        if matched_total:
            continue
        for name, source_occurrences in sorted(group):
            mapping = by_name[name]
            actual = _actual_variant(expected, translated)
            source_spans = [item for item in selected if item["source"] == name]
            occurrence = source_spans[0]
            reason = "confirmed_mapping_mismatch" if actual else "required_term_missing"
            findings.append(
                {
                    "type": "terminology",
                    "source": name,
                    "matched_source": occurrence["matched_source"],
                    "canonical_source": mapping["canonical_source"],
                    "required_translation": expected,
                    "actual_text": actual,
                    "reason": reason,
                    "start": occurrence["start"],
                    "end": occurrence["end"],
                    "left_context": occurrence["left_context"],
                    "right_context": occurrence["right_context"],
                    "context": occurrence["context"],
                    "source_spans": [
                        {
                            "matched_source": item["matched_source"],
                            "start": item["start"],
                            "end": item["end"],
                            "left_context": item["left_context"],
                            "right_context": item["right_context"],
                        }
                        for item in source_spans
                    ],
                    "source_occurrences": source_occurrences,
                    "expected_occurrences": source_occurrences,
                    "matched_occurrences": matched_total,
                    "location": location,
                }
            )
    return findings


def terminology_gaps(terms, raw, translated):
    """Check mappings at RAW occurrences; never replace Vietnamese prose globally.

    Longer source names take precedence over overlapping shorter entries. Address
    forms use their own mappings, and capitalization/Unicode spacing are immaterial.
    This checks attested wording locally; semantic equivalence still depends on
    the translation model, rather than a generic AI review request.
    """
    return sorted(
        {
            (item["source"], item["required_translation"])
            for item in terminology_findings(terms, raw, translated)
        }
    )


def export_dictionary(terms, include_ignored=False):
    """Serialize the frozen namespace, with optional non-enforced diagnostics.

    The old implementation exported every provisional/report-only record into
    the runtime dictionary.  That made a diagnostic candidate look like a
    mandatory mapping after resume.  The default is now intentionally strict:
    only CONFIRMED entries are exported.  ``include_ignored`` is for an audit
    artifact, never for prompts or validation.
    """
    sanity(terms, allow_shared_translations=True)

    def exported(term):
        value = term.model_dump()
        value["runtime_state"] = CONFIRMED if term.enforceable else IGNORE
        if value.get("status") == "locked" and value.get("enforceable") is True:
            value.pop("enforceable", None)
        if value.get("semantic_resolution") == "resolved":
            value.pop("semantic_resolution", None)
        if not value.get("needs_review"):
            value.pop("needs_review", None)
        if value.get("resolution_reason") is None:
            value.pop("resolution_reason", None)
        for field in ("confidence", "resolver_source", "fallback"):
            if value.get(field) is None:
                value.pop(field, None)
        return value

    strict = [terms[s] for s in sorted(terms) if terms[s].enforceable]
    ignored = [terms[s] for s in sorted(terms) if not terms[s].enforceable]
    result = {
        "version": DICTIONARY_VERSION,
        "entries": [exported(term) for term in strict],
        "statistics": {
            "total_terms": len(strict),
            "enforceable_terms": len(strict),
            "ignored_terms": len(ignored),
            "report_only_terms": len(ignored),
            "total_characters": sum(
                t.type in ("character", "character_form") for t in strict
            ),
            "total_locations": sum(t.type == "location" for t in strict),
        },
    }
    if include_ignored:
        result["ignored"] = [exported(term) for term in ignored]
    return result
