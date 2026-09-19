"""One canonical namespace; legacy records are quarantined, never blindly trusted."""

import re
import unicodedata
from collections import Counter, defaultdict

from . import style
from .models import (
    CONFIRMED,
    DICTIONARY_VERSION,
    IGNORE,
    IDENTITY_EVIDENCE_TYPES,
    IdentityEvidence,
    Term,
)
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
    "大伴",
    "尚书",
    "帅",
    "缇帅",
    "侍班",
)
REFERENCE_SUFFIX = re.compile(
    r"(?:" + "|".join(map(re.escape, REFERENCE_SUFFIXES)) + r")$"
)
NICKNAME = re.compile(r"^(?:老|小|阿)[\u3400-\u9fff]{1,3}$")
# Non-enforceable audit records: they document local cleanup instead of
# rejecting a checkpoint.
AUDIT_ONLY_CLASSIFICATIONS = frozenset(
    {"form_cleanup", "register_form_cleanup", "canonical_cleanup"}
)
IDENTITY_CUE = re.compile(
    r"(?:就是|是|名为|名字是|叫做|称为|原名|又名|也叫|被称为|"
    r"升为|晋升为|改称|改任|成为|即|乃)"
)
HISTORICAL_EVIDENCE_REQUIRED = frozenset({"侍班"})

DIRECT_EVIDENCE = frozenset(
    {
        "DIRECT_EXPLICIT_LINK",
        "DIRECT_FULL_NAME_WITH_TITLE",
        "DIRECT_ALIAS_DECLARATION",
    }
)
INDIRECT_EVIDENCE = frozenset(
    {
        "INDIRECT_REPEATED_CONTEXT",
        "INDIRECT_UNIQUE_SURNAME_TITLE",
        "INDIRECT_ROLE_CONTINUITY",
        "INDIRECT_LOCAL_COREFERENCE",
    }
)
NEGATIVE_EVIDENCE = frozenset(
    {
        "NEGATIVE_COMPETING_IDENTITY",
        "NEGATIVE_TRUNCATED_IDENTITY",
        "NEGATIVE_CONTEXTUAL_RESIDUE",
        "NEGATIVE_ROLE_ONLY_AMBIGUOUS",
        "NEGATIVE_MULTIPLE_POSSIBLE_OWNERS",
    }
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


def reference_shape(name, canonical=""):
    """Classify a possible character reference before identity evaluation."""
    if not name:
        return "unknown"
    if name == canonical and canonical:
        return "full_name"
    parts = style.title_prefix_parts(name)
    if parts:
        _prefix, person = parts
        if canonical and person == canonical:
            return "title_prefix_plus_full_name"
        if canonical and canonical.startswith(person):
            return "truncated_name"
        return "title_prefix_plus_partial_name"
    if canonical and name.startswith(canonical):
        suffix = name[len(canonical) :]
        if suffix and REFERENCE_SUFFIX.search(suffix):
            return "full_name_plus_title_suffix"
        if suffix:
            return "contextual_extension"
    spec = style.address_spec(name, canonical)
    if spec:
        if spec.get("surname"):
            if canonical and name[0] == canonical[0]:
                return "surname_plus_title"
            return "surname_plus_title"
        if spec.get("suffix") or name in style.ADDRESS_SPECS:
            return "role_only"
        if name.startswith(("老", "小", "阿")):
            return "nickname"
    if canonical and canonical.startswith(name) and len(name) < len(canonical):
        return "truncated_name"
    return "alias" if canonical else "unknown"


def _evidence_records(term_or_records=(), extra=()):
    records = []
    values = []
    if term_or_records is not None:
        if isinstance(term_or_records, str):
            values.append(term_or_records)
        else:
            values.extend(term_or_records or ())
    values.extend(extra or ())
    for item in values:
        if isinstance(item, IdentityEvidence):
            records.append(item)
        elif isinstance(item, dict):
            try:
                records.append(IdentityEvidence.model_validate(item))
            except (ValueError, TypeError):
                continue
    return records


def _record_is_attested(record, contexts, candidate, canonical):
    excerpt = record.raw_excerpt.strip()
    if not excerpt or not any(excerpt in context for context in contexts):
        return False
    if candidate not in excerpt:
        return False
    if record.type in DIRECT_EVIDENCE and canonical not in excerpt:
        return False
    return True


def _link_excerpt(candidate, canonical, contexts):
    cue = r"(?:就是|也就是|即|乃|是|名为|名字是|叫做|称为|原名|又名|被称为|人称)"
    patterns = (
        re.compile(re.escape(candidate) + r".{0,24}" + cue + r".{0,24}" + re.escape(canonical)),
        re.compile(re.escape(canonical) + r".{0,24}" + cue + r".{0,24}" + re.escape(candidate)),
    )
    for context in contexts:
        for pattern in patterns:
            match = pattern.search(context)
            if match:
                return match.group(0)
    return ""


def _role_excerpt(candidate, canonical, contexts):
    spec = style.address_spec(candidate, canonical)
    suffix = spec.get("suffix") if spec else ""
    if not suffix and spec and spec.get("prefix_reference"):
        suffix = spec.get("title_prefix_han", "")
    if not suffix:
        return ""
    role = r"(?:任|担任|官至|升为|晋升为|改任|成为|负责|任职|充任|为)"
    pattern = re.compile(
        re.escape(canonical) + r".{0,20}" + role + r".{0,12}" + re.escape(suffix)
    )
    for context in contexts:
        match = pattern.search(context)
        if match:
            return match.group(0)
    return ""


def evaluate_reference_evidence(
    candidate,
    canonical,
    contexts=(),
    evidence=(),
    evidence_types=(),
    competing_identities=(),
):
    """Return a deterministic confirmation decision for one character form.

    RAW excerpts are the only source of identity.  Resolver-provided evidence
    types are accepted only after their exact excerpts are found in RAW.
    """
    contexts = [context for context in contexts or () if context]
    shape = reference_shape(candidate, canonical)
    records = _evidence_records(evidence)
    supplied_types = set(evidence_types or ())
    valid_records = [
        record
        for record in records
        if record.type in IDENTITY_EVIDENCE_TYPES
        and _record_is_attested(record, contexts, candidate, canonical)
    ]
    valid_types = {record.type for record in valid_records}
    negative = set(competing_identities or ())
    negative_types = set()
    if competing_identities:
        negative_types.add("NEGATIVE_COMPETING_IDENTITY")
    if shape in {"truncated_name", "title_prefix_plus_partial_name", "contextual_extension"}:
        negative_types.add(
            "NEGATIVE_TRUNCATED_IDENTITY"
            if shape != "contextual_extension"
            else "NEGATIVE_CONTEXTUAL_RESIDUE"
        )
    if shape == "role_only" and len(competing_identities or ()) > 1:
        negative_types.add("NEGATIVE_ROLE_ONLY_AMBIGUOUS")
    explicit_excerpt = _link_excerpt(candidate, canonical, contexts)
    role_excerpt = _role_excerpt(candidate, canonical, contexts)
    if explicit_excerpt:
        valid_types.add("DIRECT_EXPLICIT_LINK")
        valid_records.append(
            IdentityEvidence(type="DIRECT_EXPLICIT_LINK", raw_excerpt=explicit_excerpt)
        )
    # A complete canonical name plus a recognized title is direct structural
    # evidence.  A surname/title or role-only form still needs RAW proof.
    if (
        shape in {"title_prefix_plus_full_name", "full_name_plus_title_suffix"}
        and any(candidate in text for text in contexts)
    ):
        valid_types.add("DIRECT_FULL_NAME_WITH_TITLE")
        valid_records.append(
            IdentityEvidence(
                type="DIRECT_FULL_NAME_WITH_TITLE",
                raw_excerpt=next((text for text in contexts if candidate in text), candidate),
            )
        )
    if role_excerpt:
        valid_types.add("INDIRECT_ROLE_CONTINUITY")
        valid_records.append(
            IdentityEvidence(type="INDIRECT_ROLE_CONTINUITY", raw_excerpt=role_excerpt)
        )
    occurrences = sum(context.count(candidate) for context in contexts)
    if occurrences >= 2:
        valid_types.add("INDIRECT_REPEATED_CONTEXT")
        valid_records.append(
            IdentityEvidence(
                type="INDIRECT_REPEATED_CONTEXT",
                raw_excerpt=next((text for text in contexts if candidate in text), candidate),
            )
        )
    if (
        shape == "surname_plus_title"
        and canonical
        and candidate[:1] == canonical[:1]
        and not competing_identities
        and role_excerpt
    ):
        valid_types.add("INDIRECT_UNIQUE_SURNAME_TITLE")
        valid_records.append(
            IdentityEvidence(type="INDIRECT_UNIQUE_SURNAME_TITLE", raw_excerpt=role_excerpt)
        )
    # The resolver may have supplied a type without a valid excerpt.  Preserve
    # it for audit visibility, but never let it confirm the relationship.
    unverified_types = supplied_types - valid_types
    structural_negative = negative_types - {
        "NEGATIVE_COMPETING_IDENTITY",
        "NEGATIVE_MULTIPLE_POSSIBLE_OWNERS",
    }
    hard_negative = bool(structural_negative) or bool(
        valid_types.intersection(
            NEGATIVE_EVIDENCE
            - {"NEGATIVE_COMPETING_IDENTITY", "NEGATIVE_MULTIPLE_POSSIBLE_OWNERS"}
        )
    )
    direct = bool(valid_types.intersection(DIRECT_EVIDENCE))
    indirect = valid_types.intersection(INDIRECT_EVIDENCE)
    confirmed = not hard_negative and (
        direct
        or (
            len(indirect) >= 2
            and not competing_identities
        )
    )
    if shape == "role_only" and not direct and len(indirect) < 2:
        confirmed = False
        if competing_identities:
            negative_types.add("NEGATIVE_ROLE_ONLY_AMBIGUOUS")
    if shape in {"truncated_name", "title_prefix_plus_partial_name", "contextual_extension"}:
        confirmed = False
    reason = (
        "direct_evidence"
        if direct and confirmed
        else "strong_indirect_evidence"
        if confirmed
        else "competing_identity"
        if competing_identities
        else "truncated_identity"
        if shape in {"truncated_name", "title_prefix_plus_partial_name"}
        else "contextual_residue"
        if shape == "contextual_extension"
        else "insufficient_identity_evidence"
    )
    return {
        "confirmed": confirmed,
        "shape": shape,
        "reason": reason,
        "evidence_types": sorted(valid_types | negative_types),
        "evidence": valid_records,
        "unverified_evidence_types": sorted(unverified_types),
        "competing_identities": sorted(negative),
    }


def _title_prefix_problem(name, canonical):
    """Reject title-prefix references whose trailing name is incomplete."""
    parts = style.title_prefix_parts(name)
    if not parts:
        return None
    _prefix, person = parts
    if person == canonical:
        return None
    if canonical.startswith(person) and len(canonical) > len(person):
        return "truncated title-prefixed canonical reference"
    return "title-prefixed reference does not match canonical identity"


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
    # Structured evidence has already been locally checked before a term is
    # frozen.  Keep its compact excerpt in ``term.evidence`` for old sanity
    # callers, while accepting the structured fields as an audit-preserving
    # confirmation source too.
    if (
        term.identity_evidence
        and term.evidence_types
        and any(record.raw_excerpt for record in term.identity_evidence)
    ):
        evidence = evidence or bool(
            set(term.evidence_types).intersection(DIRECT_EVIDENCE | INDIRECT_EVIDENCE)
        )
    title_problem = _title_prefix_problem(name, term.source)
    if title_problem:
        return title_problem
    spec = style.address_spec(name, term.source, contexts)
    if spec and spec.get("prefix_reference"):
        if spec.get("person_source") != term.source:
            return "title-prefixed reference does not match canonical identity"
        if spec.get("person_source") in HISTORICAL_EVIDENCE_REQUIRED and not evidence:
            return "character reference identity is not independently proven"
        return None
    suffix = name[len(term.source) :] if name.startswith(term.source) else ""
    if suffix:
        if REFERENCE_SUFFIX.search(suffix) or evidence:
            if suffix in HISTORICAL_EVIDENCE_REQUIRED and not evidence:
                return "character reference identity is not independently proven"
            return None
        return "canonical character plus contextual residue"
    if spec or style.ADDRESS_SUFFIX.search(name) or NICKNAME.fullmatch(name):
        if (
            require_evidence
            or (spec and spec.get("suffix") in HISTORICAL_EVIDENCE_REQUIRED)
        ) and not evidence:
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
    forms = {}
    for name, translation in term.forms.items():
        problem = identity_form_problem(term, name, contexts, require_evidence)
        if problem:
            removed.append({"source": name, "reason": problem, "field": "forms"})
        else:
            forms[name] = translation
            preserved.append(name)
    aliases = []
    for name in term.aliases:
        # A form carries the required Vietnamese rendering, so it owns the
        # key when a resolver or legacy record also emitted it as an alias.
        if name in forms:
            continue
        problem = identity_form_problem(term, name, contexts, require_evidence)
        if problem:
            removed.append({"source": name, "reason": problem, "field": "aliases"})
        else:
            aliases.append(name)
            preserved.append(name)
    term.aliases = sorted(set(aliases))
    term.forms = forms
    return removed, sorted(set(preserved))


def canonical_source_problem(source):
    """Title-prefixed references are forms, never canonical identities."""
    if style.title_prefix_parts(source):
        return "title-prefixed reference must be stored as a form of the full identity"
    return None


def _title_reference_candidates(source, terms):
    parts = style.title_prefix_parts(source)
    if not parts:
        return []
    prefix, fragment = parts
    candidates = set()
    for name, term in terms.items():
        if name == source or not term.enforceable or term.type not in CHARACTER_TYPES:
            continue
        if name.startswith(fragment) and len(name) > len(fragment):
            candidates.add(name)
        for reference in [*term.aliases, *term.forms]:
            ref_parts = style.title_prefix_parts(reference)
            if ref_parts and ref_parts[0] == prefix:
                person = ref_parts[1]
                if person.startswith(fragment) and len(person) > len(fragment):
                    candidates.add(name)
    return sorted(candidates, key=lambda value: (-len(value), value))


def migrate_title_prefixed_identities(terms, problems):
    """Merge malformed title-prefixed canonical records into full identities."""
    for source, malformed in list(terms.items()):
        if not style.title_prefix_parts(source):
            continue
        candidates = _title_reference_candidates(source, terms)
        if candidates:
            target_source = candidates[0]
            target = terms[target_source]
            prefix = style.title_prefix_parts(source)[0]
            full_form = prefix + target_source
            target.forms.setdefault(
                full_form,
                style.preferred_form(
                    full_form,
                    target_source,
                    target.translation,
                    style.address_spec(full_form, target_source),
                )
                or malformed.forms.get(full_form, malformed.translation),
            )
            for name, value in malformed.forms.items():
                if name == full_form or (
                    style.title_prefix_parts(name)
                    and style.title_prefix_parts(name)[1] == target_source
                ):
                    target.forms.setdefault(name, value)
            target.aliases = sorted(
                set(target.aliases + [name for name in malformed.aliases if name != full_form])
                - set(target.forms)
            )
            terms.pop(source)
            problems.append(
                {
                    "classification": "canonical_cleanup",
                    "source": source,
                    "target": target_source,
                    "migrated_forms": [full_form],
                    "reason": "migrated truncated title-prefixed canonical extraction",
                }
            )
            continue
        malformed.status = "report_only"
        malformed.enforceable = False
        malformed.runtime_state = IGNORE
        problems.append(
            {
                "classification": "canonical_cleanup",
                "source": source,
                "reason": "title-prefixed reference has no proven full canonical identity",
            }
        )


def vet_confirmed_reference_forms(terms, contexts, problems):
    """Re-check legacy frozen forms against the current RAW evidence.

    Legacy canonical translations remain reusable, but an old form is not
    allowed to regain enforcement merely because it was once serialized.  A
    form that cannot be proven is removed and retained only in the audit trail.
    """
    contexts = [context for context in contexts or () if context]
    for term in terms:
        if not term.enforceable or term.type not in CHARACTER_TYPES:
            continue
        removed = []
        for name in list(term.forms):
            decision = evaluate_reference_evidence(
                name,
                term.source,
                [*contexts, term.evidence] if term.evidence else contexts,
                term.identity_evidence,
                term.evidence_types,
                term.competing_identities,
            )
            if decision["confirmed"]:
                term.candidate_shape = term.candidate_shape or decision["shape"]
                term.evidence_types = sorted(
                    set(term.evidence_types) | set(decision["evidence_types"])
                )
                continue
            # Pre-v5 locked dictionaries treated recognized address mappings as
            # user-confirmed terminology.  Preserve that compatibility for
            # ordinary known forms during reload; historically evidence-gated
            # roles such as 侍班 still require current RAW proof.
            legacy_spec = style.address_spec(name, term.source)
            if (
                legacy_spec
                and legacy_spec.get("suffix") not in HISTORICAL_EVIDENCE_REQUIRED
                and decision["shape"] not in {"truncated_name", "contextual_extension"}
            ):
                continue
            removed.append(
                {
                    "source": name,
                    "reason": decision["reason"],
                    "field": "forms",
                    "evidence_types": decision["evidence_types"],
                }
            )
            term.forms.pop(name, None)
            term.form_kinds.pop(name, None)
        if removed:
            term.aliases = sorted(set(term.aliases) - set(term.forms))
            problems.append(
                {
                    "classification": "form_cleanup",
                    "source": term.source,
                    "removed_forms": removed,
                    "preserved_forms": sorted(term.forms),
                    "reason": "removed legacy forms without current identity evidence",
                }
            )


def normalize_term_register(term, register, contexts=()):
    """Apply the batch address/title register to one confirmed term.

    Returns ``(normalizations, removed)``.  The canonical identity and its
    canonical translation are never modified.
    """
    normalizations, removed = [], []
    if term.type not in style.CHARACTER_TYPES:
        return normalizations, removed
    _kinds, normalizations, removed = style.normalize_forms(term, register, contexts)
    return normalizations, removed





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
    problem = canonical_source_problem(term.source)
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
            # Keep title-prefixed references intact until the canonical
            # migration pass can attach their complete form to the real person.
            if canonical_source_problem(term.source):
                removed, preserved = [], sorted(term.forms)
            else:
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
                if canonical_source_problem(term.source):
                    term.status = "report_only"
                    term.enforceable = False
                    term.runtime_state = IGNORE
                    problems.append(
                        {
                            "classification": "canonical_cleanup",
                            "source": term.source,
                            "reason": problem,
                        }
                    )
                else:
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
                existing.aliases = sorted(set(existing.aliases) - set(existing.forms))
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
    migrate_title_prefixed_identities(terms, problems)
    register = style.stored_register(data, terms.values())
    if register is None:
        register, _source = style.effective_register(
            style.configured_register(), style.profile(terms.values())
        )
    for term in terms.values():
        normalized, removed = normalize_term_register(term, register)
        if not normalized and not removed:
            continue
        problems.append(
            {
                "classification": "register_form_cleanup",
                "source": term.source,
                "register": register,
                "normalizations": normalized,
                "removed_forms": removed,
                "preserved_forms": sorted(term.forms),
                "reason": "aligned address/title forms with the batch register",
            }
        )
    return list(terms.values()), problems


def sanity(terms, allow_shared_translations=True):
    errors, owners, error_keys = [], {}, set()

    def add_error(message):
        if message not in error_keys:
            error_keys.add(message)
            errors.append(message)

    register, _source = style.effective_register(
        style.configured_register(),
        style.profile(term for term in terms.values() if term.enforceable),
    )
    # Only confirmed mappings are part of the strict namespace.  Ignored
    # diagnostics are deliberately not allowed to make a batch fail.
    strict = {source: term for source, term in terms.items() if term.enforceable}
    for source, term in strict.items():
        problem = term_problem(term, terms)
        if problem:
            add_error(f"{source}: {problem}")
        if term.type in CHARACTER_TYPES:
            for name in dict.fromkeys([*term.aliases, *term.forms]):
                form_problem = identity_form_problem(term, name)
                if form_problem:
                    add_error(f"{source}.{name}: {form_problem}")
            for name, value in term.forms.items():
                spec = style.address_spec(name, source)
                register_problem = style.conflict(spec, value, register)
                if register_problem:
                    add_error(f"{source}.{name}: {register_problem}")
        for name in [source, *term.aliases, *term.forms]:
            if name in owners and owners[name] != source:
                add_error(f"{name}: alias/canonical collision")
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
    result, seen = [], set()
    for term in terms:
        if not term_is_enforceable(term):
            continue
        source = _term_value(term, "source", "")
        translation = _term_value(term, "translation", "")
        forms = _term_value(term, "forms", {}) or {}
        aliases = _term_value(term, "aliases", []) or []
        for name in [source, *forms, *[alias for alias in aliases if alias not in forms]]:
            key = (source, name, forms.get(name, translation))
            if key in seen:
                continue
            seen.add(key)
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
        # Forms carry their required rendering; do not export the same Chinese
        # key again as a bare alias.
        value["aliases"] = [
            alias for alias in value.get("aliases", []) if alias not in value.get("forms", {})
        ]
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
        if not value.get("form_kinds"):
            value.pop("form_kinds", None)
        if value.get("candidate_shape") is None:
            value.pop("candidate_shape", None)
        if not value.get("identity_evidence"):
            value.pop("identity_evidence", None)
        if not value.get("evidence_types"):
            value.pop("evidence_types", None)
        if not value.get("competing_identities"):
            value.pop("competing_identities", None)
        if value.get("address_register") is None:
            value.pop("address_register", None)
        return value

    strict = [terms[s] for s in sorted(terms) if terms[s].enforceable]
    ignored = [terms[s] for s in sorted(terms) if not terms[s].enforceable]
    register = style.stored_register(None, strict)
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
    if register:
        result["register"] = register
    if include_ignored:
        result["ignored"] = [exported(term) for term in ignored]
    return result
