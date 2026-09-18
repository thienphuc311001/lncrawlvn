# Chinese → Vietnamese translation pipeline

The translation pipeline is deliberately two-phase:

```text
RAW + VietPhrase
  → parse and structurally align
  → extract/filter RAW candidates
  → use aligned VietPhrase as evidence
  → canonicalize and resolve plausible ambiguity
  → freeze CONFIRMED dictionary
  → translate RAW chunks with aligned VietPhrase hints
  → deterministic validation
  → zero or one targeted repair per chunk
  → final global audit and export
```

## Source priority

The sources have fixed roles:

1. The frozen confirmed dictionary is the terminology authority.
2. RAW Chinese is the semantic/source-of-meaning authority.
3. VietPhrase is auxiliary evidence for readings, segmentation and wording.
4. Model judgment produces natural Vietnamese where the first three do not decide.

Consequently, a confirmed mapping always overrides VietPhrase wording, and RAW
meaning always overrides a bad VietPhrase interpretation. VietPhrase is never
scanned independently to invent Chinese dictionary keys and is never used as a
mandatory reference translation.

## Parsing and alignment

RAW and VietPhrase are parsed independently. Volume markers, chapter identities,
order, complete bodies and normalized line endings are retained. Chapters pair by
structural identity, including volume where available; a missing chapter does not
silently shift every later pair. Paragraph alignment is monotonic and deterministic,
with only proven adjacent split/merge recovery. Ambiguous structures fail with a
concrete input error. Current RAW TXT exports carry blank safe-block boundaries
(≤3000 characters per block) that the parser already treats as block boundaries,
so no alignment change was needed for them.

## Terminology

Candidate discovery starts from RAW spans. Structural noise, ordinary vocabulary,
quantity fragments, malformed spans, timestamps and generic phrases are recorded in
the audit as `IGNORE`; they are not resolver candidates, dictionary entries or
validator constraints. Offsets remain metadata and can never become a source string.

Aligned VietPhrase text is attached to each RAW occurrence as evidence. It can
support a stable reading, but it cannot confirm a generic word or prove an alias by
itself. Canonicalization happens before insertion, so a proven full name and alias
share one identity with Chinese-keyed address forms. Different Chinese identities
may share the same Vietnamese target; only one source identity having contradictory
targets is a conflict.

Runtime terminology has exactly two decisions:

```text
CONFIRMED → enters the frozen dictionary and is strictly validated
IGNORE    → diagnostics only; never blocks translation
```

Legacy records are migrated conservatively. Locked, explicitly confirmed or trusted
user mappings can become `CONFIRMED`; provisional, report-only, unresolved and
ambiguous records become `IGNORE`. A resolver `REVIEW`, reject, malformed result or
uncertain alias is `IGNORE`, not a provisional runtime mapping. The resolver is only
called for plausible RAW candidates that survive local filtering.

The dictionary is sanity-checked before freeze for valid source/target text,
duplicate canonical identities, alias ownership and contradictory mappings. The
frozen artifact contains confirmed entries only. Its content hash is carried by
every translation chunk and chapter. After freeze, no resolver call, alias merge,
mapping change or dictionary mutation is permitted.

Character aliases/forms are identity references, not context translations. Resolver
metadata is cleaned before insertion: canonical-name-plus-predicate fragments such as
`邱途来` and `邱途接` are removed, while independently supported title, honorific,
kinship and nickname forms such as `邱科长`, `邱探员`, `唐姐` and `老邱` may remain. A
form must be independently attested in RAW and have a reference shape or explicit
co-reference evidence; a context window or VietPhrase phrase cannot become a form.
Legacy frozen dictionaries and resolved-term checkpoints go through the same cleanup.
Cleanup writes a concise audit and produces a new dictionary hash.

## Translation style and address register

A Chinese title, honorific, kinship-style address, rank or official form of address is
an identity reference with a semantic function, not free prose. The resolver may not
choose a Vietnamese rendering only because it is the most natural modern conversational
wording. The fixed priority is:

```text
1. preserve the canonical character identity
2. preserve the semantic function of the title/address form
3. preserve the novel's established Sino-Vietnamese style
4. prefer consistency with existing confirmed terminology
5. only then optimize for modern Vietnamese naturalness
```

Every confirmed Chinese reference form is classified as exactly one of
`literal_kinship`, `social_honorific`, `official_title`, `rank`, `role_reference`,
`nickname` or `alias`, so a new form is compared against confirmed forms of the same
class. The batch register is `sino-vietnamese` by default
(`TRANSLATION_DICTIONARY_REGISTER`); `modern` expects modern Vietnamese, and `auto`
derives the register from confirmed terminology and falls back to the Sino-Vietnamese
baseline. The register recorded by a frozen dictionary or by the confirmed terms
outranks an auto-derived profile, so re-freezing a checkpoint stays a fixed point.

```text
inherited + locally confirmed + resolver-accepted terms
        ↓ derive the class-scoped style profile (local, no model calls)
batch register  →  resolver request (translation_style + per-candidate address_form)
                →  deterministic guard before freeze
                →  frozen dictionary + dictionary-style-profile.json
```

The guard only touches forms whose Chinese shape has a known address suffix. A form
whose wording contradicts the batch register is rewritten to the deterministic
established rendering when the shape is unambiguous (`唐姐` → `Đường tỷ`,
`老秦` → `Lão Tần`, `秦四爷` → `Tần Tứ gia`, `邱科长` → `Khoa trưởng Khâu`,
`邱长官` → `Trưởng quan Khâu`, `唐副署长` → `Phó thự trưởng Đường`), and otherwise
dropped with an audit record instead of being guessed. The canonical identity and its
canonical translation are never modified, `唐姐` is never promoted to its own character
identity, and exactly one preferred Vietnamese rendering is kept per confirmed Chinese
key. A surname-prefixed address form is never promoted into its own character identity:
RAW that does not prove the full name keeps it a report-only `IGNORE` reference in
candidate discovery, and a resolver result that returns it as a bare canonical source is
rejected so it can only exist as a form of the proven identity (bare nicknames such as
`老秦` and bare titles stay eligible, because they can be the only attested way a
character is named). RAW alone decides whether a kinship word is
a literal family relationship or only a social address form; that answer is recorded as
a hint and never used to mix registers. Rewrites and drops are written to
`dictionary-register-cleanup.json` and counted as `register_normalizations` and
`register_conflicts` in the dictionary report.

## Translation contract

Each request receives only:

```text
RAW chunk                  meaning source
matching VietPhrase chunk auxiliary hint
relevant frozen mappings   mandatory terminology
```

The prompt requires complete RAW coverage, natural Vietnamese, no summary or
invention, and no dictionary creation. VietPhrase is not copied mechanically.
Provider retry/key rotation remains separate from semantic repair: an API success
still has to pass local validation.

## Validation and repair

Local validation is small and deterministic:

- non-empty title and segments with exact IDs and source order;
- invalid Unicode/control text and obvious Chinese residue;
- source-aware presence of each relevant confirmed mapping;
- chapter/title shape, placeholders and proven length/order checks already supported
  by the application.

Differences from VietPhrase wording alone never fail validation. Terminology findings
use exact confirmed canonical/alias/form keys only. Each occurrence preserves `start`,
`end`, `matched_source`, and separate left/right context; the invariant
`raw[start:end] == matched_source` is checked before a finding is emitted. Longest-match
selection applies only among confirmed keys and never absorbs adjacent Chinese,
punctuation, ASCII or numbers. Required Vietnamese text comes only from the frozen
mapping. Findings include the exact RAW source, required Vietnamese wording, actual
realization, reason and location. A failed translation chunk receives at most one
targeted repair containing only the failed RAW IDs, their aligned VietPhrase region,
findings and relevant confirmed mappings. The same deterministic validator runs once
after repair. A persistent failure stops with the exact findings; there is no recursive
repair or repair-time dictionary mutation. On resume, serialized findings are never
trusted: the current RAW, frozen dictionary and translation recompute local findings
before any repair. Full-chapter assembly is an audit, not a second repair loop.

## Checkpoints and outputs

Existing parsed chapters, alignments, frozen dictionaries, completed chunks and
validated chapters are reused when their input and dictionary hashes remain
compatible. Old non-confirmed terminology is quarantined as `IGNORE`; an incompatible
or contradictory inherited confirmed mapping fails clearly rather than being guessed.
When a compatible frozen dictionary is cleaned, completed chapters are revalidated
against the new hash. Passing chapters are rewritten with the cleaned hash; failing
chapters fall back to chunk-level checkpoint validation and repair, so unaffected work
is reused. Old validation findings naming removed forms are overwritten by current
RAW/dictionary/output findings.

The job writes:

- `translated.json` and the CLI's final Vietnamese TXT/export;
- `dictionary.json` and `frozen-dictionary.json` containing confirmed entries only;
- `dictionary-audit.json` and the existing resolution report for ignored candidates;
- `dictionary-style-profile.json`, `dictionary-register-cleanup.json` and
  `dictionary-form-cleanup.json` for the derived style and local cleanup audits;
- parser, alignment, chunk, chapter and validation checkpoints.

Normal logs report parsed/aligned chapters, RAW candidates, ignored candidates,
resolver candidates, confirmed terms, translated/validated chunks, repair attempts
and final audit status without logging every ignored phrase.

## Deliberately removed from the active path

The production pipeline no longer uses provisional enforcement, report-only
validation, semantic-conflict passes, resolver fallback mappings, repeated resolver
corrections, repeated targeted repairs, whole-novel VietPhrase prompts, or runtime
dictionary mutation. Compatibility schemas and legacy fields remain only where they
are needed to read old checkpoints safely; they do not affect translation-time
enforcement.
