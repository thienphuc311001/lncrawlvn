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
concrete input error.

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
include the RAW source, required Vietnamese wording, actual realization, reason and
location. A failed translation chunk receives at most one targeted repair containing
only the failed RAW IDs, their aligned VietPhrase region, findings and relevant
confirmed mappings. The same deterministic validator runs once after repair. A
persistent failure stops with the exact findings; there is no recursive repair or
repair-time dictionary mutation. Full-chapter assembly is an audit, not a second
repair loop.

## Checkpoints and outputs

Existing parsed chapters, alignments, frozen dictionaries, completed chunks and
validated chapters are reused when their input and dictionary hashes remain
compatible. Old non-confirmed terminology is quarantined as `IGNORE`; an incompatible
or contradictory inherited confirmed mapping fails clearly rather than being guessed.

The job writes:

- `translated.json` and the CLI's final Vietnamese TXT/export;
- `dictionary.json` and `frozen-dictionary.json` containing confirmed entries only;
- `dictionary-audit.json` and the existing resolution report for ignored candidates;
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
