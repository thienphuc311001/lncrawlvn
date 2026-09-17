# Translation pipeline: request reduction and verification

Pipeline and parser version 5 retain the mandatory global dictionary step, completing
and freezing it before any translation. The changes are architectural: deterministic
work moves to local code, semantic terminology decisions are batched, and successful
translations are accepted without another model request.

## Previous operation inventory

This inventory was checked against the previous `Pipeline` implementation at `79abe3f`.

| Previous AI operation | Classification | Current behavior |
| --- | --- | --- |
| `ALIGN`, including coverage correction | Deterministic/local | Local logical-line, chapter identity/order and blank-block proof; limited anchored split/merge recovery; otherwise stop for manual review. |
| `DISCOVER`, per chapter/chunk terminology discovery | Deterministic/local | Whole-batch local candidate extraction and filtering, including inherited keys. |
| `EVIDENCE`, occurrence pages | Redundant AI | Exhaustive local occurrence index; no model occurrence collection or summaries. |
| `EVIDENCE`, hierarchical summary aggregation | Redundant AI | Deterministic representative sampling of first/last, variant, address and distributed chapter contexts. |
| `RESOLVE`, one candidate at a time | Conditionally semantic | Valid inherited mappings and conservative stable full names resolve locally; ambiguous candidates use `BATCH_RESOLVE`. |
| `RESOLVE`, inherited dictionary vetting and corrections | Conditionally semantic | Schema/namespace audit locally; only concrete semantic conflicts use batches; malformed independent model decisions receive bounded corrections. |
| `CONTEXT`, whole chapter analysis | Mergeable | Removed as a separate operation; translation infers context from local metadata, current RAW/VP and small prior finalized context. |
| `TRANSLATE`, current chunk | Semantic/AI required | Retained, with the frozen whole-batch dictionary hash and only relevant entries. |
| `VALIDATE`, chunk/chapter/final review | Local-first | Removed as an AI operation; concrete checks are deterministic. Successful outputs incur no review request. |
| `REPAIR`, failed translated IDs | Conditionally semantic | Retained only after actual local findings; exact failed IDs and attested RAW/VP, unaffected text preserved. |
| Missed-term resolution and repeated chapter reconciliation | Avoidable mutation/review | Removed from normal translation. New unresolved important terms stop explicitly and require a reviewed dictionary/new batch. |

Production uses three prompt families: `BATCH_RESOLVE` (terminology or semantic
conflicts), `TRANSLATE`, and targeted `REPAIR`. The single-term `resolve` helper remains
for diagnostic canonical-guard tests; the production pipeline never invokes it.
Legacy prompt/schema definitions remain available for compatibility tests, without
being requested in normal execution.

## Exact order and safeguards

```text
Parse the complete RAW and VietPhrase inputs                         LOCAL
Validate chapter/volume identities and order                       LOCAL
Align actual logical lines and blank blocks                        LOCAL
Build deterministic paragraph chunks                               LOCAL
Scan the complete novel and extract/filter candidates              LOCAL
Aggregate all occurrence positions and VietPhrase proposals        LOCAL
Select representative evidence                                    LOCAL
Audit/reuse valid inherited sources, aliases and forms             LOCAL
Analyze consensus; resolve verified stable full names provisionally LOCAL
Batch inherited semantic conflicts, if any                         GEMINI
Batch unresolved ambiguous terminology                             GEMINI
Audit dictionary structure and remaining namespace conflicts       LOCAL
Resolve remaining concrete semantic conflicts, if any              GEMINI
Freeze canonical dictionary; assign stable SHA-256 hash            LOCAL
Build minimal chapter/chunk/continuity context                      LOCAL
Translate current RAW chunk with aligned VP and frozen terminology GEMINI
Validate chunk and completed chapter                               LOCAL
Repair only exact failed IDs, if findings exist                    GEMINI
Validate final chapter ordering and dictionary hashes; export      LOCAL
```

Chunk construction occurs immediately after alignment and does not need semantic
terminology decisions. No translation starts until all dictionary decisions and audits
complete. Chapters may run concurrently afterward; chunks within a chapter are sequential.

Logical lines use real newlines, not editor wrapping. Blank runs normalize to one block
boundary. Same counts and boundaries prove one-to-one pairing. Recovery of unequal
line counts requires corresponding blank blocks and matching nonempty normalized
punctuation signatures; whole blocks retain monotonic complete ID coverage. An
ambiguous pairing never falls back to an AI alignment guess.

The terminology index stores aligned RAW/VP units once, occurrence positions for every
candidate, frequencies, per-unit proposal counts, and representative evidence. VP
proposals are aligned-unit co-occurrence evidence, not character-offset mappings or
guaranteed word-level equivalence. Character consensus requires at least three distinct
units and at least 95% dominance and coverage, plus an exact locally verified
Sino-Vietnamese name reading. Unknown readings, identity/gender and address relationships
remain unresolved semantic work. Locally created names are provisional, with unknown
gender and no inferred aliases. Rules deliberately favor uncertainty over a wrong lock.

Resolver batches use a serialized context budget (18,000 characters per candidate
page), not a fixed number of terms. Each source receives independent evidence and a
strict decision/eligibility/term record. Valid independent decisions checkpoint
immediately; missing, malformed or duplicated decisions retry only their sources, at
most twice. Source attestation, canonical actor identity, Chinese-keyed address forms,
locked mappings, historical rank forms and inherited gender/type guards remain enforced.

Structural dictionary audit is local: strict record schema, invalid type/status/gender,
empty or invalid Unicode keys/translations, duplicate aliases, malformed forms,
duplicate sources, namespace collisions and duplicate canonical translations. The
schema uses source keys as canonical identity, with inline aliases/forms; unsupported
external IDs or reference formats are quarantined locally rather than silently accepted.
Equivalent duplicate sources merge locally, retaining locked state. Genuine conflicting
canonical mappings or identities receive semantic review. Valid cumulative inherited
keys absent from the current RAW are retained, recorded explicitly in the local audit,
and do not cause requests just to rediscover earlier-batch terms.

The frozen namespace rejects additions. Its stable content hash is checked before
each chunk and final export, catching mutation of nested records as well. All finalized
chapters and translation/repair payloads carry the same global hash. Reported new
important, attested unresolved terms create `frozen-conflicts/` and stop without
silently changing that dictionary or rewriting finalized chapters.

Local output validation checks empty text/title, exact IDs/order/coverage, Chinese
residue, frozen source-aware longest-name/form wording, invalid Unicode, malformed
title shape and changed chapter numbers, extreme length changes, literal placeholder/markup corruption and
suspicious duplicate output for unrelated long RAW paragraphs. Unknown extra IDs fail
closed because they cannot be paired safely. Repair receives only failed RAW IDs and
their aligned VP windows, exact findings, frozen entries and context-only neighbors.
Patches must return exactly those IDs. Unaffected text stays unchanged; unresolved
findings stop after two attempts.

Deterministic scanning is conservative candidate discovery, not a proof of exhaustive
semantic entity recognition. Local output checks also cannot prove subtle speaker,
pronoun, negation or narrative equivalence. The translation prompt retains RAW authority
and faithfulness requirements, and reports genuinely unresolved important terminology
explicitly. Human editorial review remains useful for semantic quality.

## Checkpoints, identity and request accounting

Checkpoints retain parsed chapters, proven alignment, the complete terminology/occurrence
index and sampled evidence, independent resolutions, local audits, frozen dictionary,
model responses, chunks and finalized chapters. Atomic writes and the execution lease
remain in place. Job and preprocessing identity includes full RAW, VP and inherited
dictionary content, pipeline version, fixed model order, production prompts and relevant
schemas. Model response caches include task/prompt/payload/schema/version/models; chunk
and chapter reuse also checks pipeline input identity and the global dictionary hash.
Changing a material contract cannot silently reuse an old completed batch.

`request-statistics.json`, API snapshots, CLI progress and the UI expose:

| Counter | Meaning |
| --- | --- |
| `logical_operations.terminology_resolver` | Uncached batched ambiguous-terminology operations, including targeted semantic decision corrections. |
| `logical_operations.semantic_dictionary_conflict` | Uncached batches for concrete inherited/remaining semantic conflicts. |
| `logical_operations.translation` | Uncached chunk translation operations. |
| `logical_operations.repair` | Uncached targeted repairs caused by local findings. |
| `requests.<operation>` / `total_requests` | Actual started API attempts, including rotation and retries. |
| `retry` | Started attempts after the first attempt of the same logical operation, including key/model rotation. |
| `model_fallback` | Model changes within an operation, or starting on a fallback because primary pairs are unavailable. |
| `cache_hits` | Reused model response caches, adding zero requests. Chapter/chunk reuse also adds zero requests. |
| `local_ai_requests` | Explicit zeros for alignment, scanning, occurrence aggregation, evidence selection, structural audit, chunking and normal output validation. |

Activity events and `requests.jsonl` include operation and reason, model/key slot,
status and failure/cooldown category. Credentials never appear. The model order, key
rotation, daily pair exclusion, 60-second RPM/TPM pair cooldown, authentication/key
exclusion and bounded temporary-provider failover policy remain unchanged.

Version 5 gets a separate content identity from previous pipelines. Unfinished legacy
parser jobs must be resubmitted with their original inputs; completed old downloads
remain available. Repeating an unchanged current batch reuses completed work.

## Verification

Offline tests in `tests/test_translation_optimized.py` prove the following, alongside
the existing scheduler, canonical-guard, parser, API, volume and CLI regression suites:

| Required behavior | Test coverage |
| --- | --- |
| Deterministic RAW/VP alignment | Actual logical lines, visual wrapping irrelevance, normalized blank runs, proven split/merge plus unchanged blocks, ambiguous structures rejected. |
| Obvious terminology causes no Gemini preprocessing | Verified consistent full name is provisional; exactly one translation call and zero resolver calls. Ordinary fragments/unknown colloquial readings cannot resolve locally as names. |
| Ambiguous terminology requires Gemini | `老秦` and `秦科长` use one source-tagged batch; proved forms attach to the canonical actor. |
| Full-novel dictionary before chapter 1 | A term introduced only in chapter 2 resolves before the first translation. |
| Structural dictionary errors remain local | Invalid gender/type/status, empty translation, source controls, duplicate aliases, malformed forms and unsupported references quarantined with zero resolver calls. Identical inherited duplicates preserve locked state locally. |
| Semantic conflict classification | Conflicting inherited canonical mappings use `semantic_dictionary_conflict`, not the structural audit or ordinary resolver counter. |
| Frozen dictionary before every translation | Provider asserts the frozen checkpoint already exists; multiple chunks use the same hash; mutations and late resolution are refused. |
| Successful translation has no AI review/repair | Provider rejects unnecessary prompt families; success has zero alignment/scanning/evidence/context/validation/repair calls. |
| Failed validation causes targeted repair | Chinese residue and empty text repair only failed RAW IDs; neighbors/unaffected output remain unchanged. Local checks cover wording, title, placeholders, order and unexpected IDs. |
| Independent partial resolver handling | One malformed result retries only that source; unrelated valid decisions remain checkpointed. |
| Resume spends no quota on completed work | Interrupted chapter 2 reuses the frozen dictionary and chapter 1; completed rerun invokes no provider calls. |
| Material cache identity and physical attempts | Input, prompt and model changes alter identity; injected quota/fallback counts distinguish one logical translation from two API attempts. |
| New term after freeze is explicit | Attested unresolved term stops with a report, unchanged frozen dictionary and no final artifacts. |

Frontend tests verify frozen hash, readable request reasons, logical/physical statistics,
local zero rows and existing error/model/key/cooldown activity. Production build checks
TypeScript and rendering.

Validation results: 102 backend tests and 7 frontend tests passed; Ruff checks,
`git diff --check`, and the frontend production build passed. The browser was also
checked against the completed live batch, including the frozen hash and expanded
logical/API statistics with all seven local operation rows at zero.

Measured on the supplied 100-chapter RAW/VP files: all chapters pair locally, with 9,357
body paragraphs after documented exporter-title cleanup. The whole-batch offline scan
took approximately 1.3 seconds, found 413 candidates, provisionally resolved 7 verified
names, and packed 406 remaining semantic candidates into an estimated 34 batches.
**Measured preprocessing API requests: zero.** The 34 figure is an initial-page estimate,
not a completed full-novel API run; corrections/conflicts/provider retries can add calls.
The previous architecture required at least 100 alignment operations and one or more
discovery operations per chapter, before per-term occurrence summaries and resolution.

A live first-chapter CLI test completed with a frozen four-entry dictionary, two logical
terminology resolver operations (including one targeted canonical decision correction),
one translation operation and zero AI review/repair operations. Six physical API attempts
included two daily-quota key failures and one temporary HTTP 503. The third key succeeded
on the primary model; no model fallback was needed. Repeating the command reused outputs
with zero new API calls. Test outputs/checkpoints remain private local runtime artifacts
and are not committed to the repository. `--first 1` tests a one-chapter batch; omit it
to build the mandatory global dictionary for all 100 chapters.
