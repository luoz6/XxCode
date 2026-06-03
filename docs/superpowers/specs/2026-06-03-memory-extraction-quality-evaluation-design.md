# Memory Extraction Quality Evaluation Design

> Version 0.1 | 2026-06-03 | Scope: define a deterministic SDD-first evaluation layer for memory extraction output quality

## 1. Objective

The recall evaluation suite measures whether existing memories can be selected
for a query. The index organization suite measures whether `MEMORY.md` is a
healthy recall entrypoint. Neither layer answers whether the system writes good
memory files in the first place.

This design adds a third deterministic evaluation layer for memory extraction
quality. It should answer:

- whether expected durable facts are captured from a conversation
- whether extracted memory files are valid and parseable
- whether memory type classification is correct
- whether extracted content is grounded in the source conversation
- whether ephemeral or noisy details are suppressed
- whether duplicate or conflicting memories are handled correctly

This work evaluates extraction output quality only. It does not evaluate live
LLM extraction behavior, sub-agent orchestration, final answer quality, or
semantic recall.

## 2. Current State

The runtime extraction path lives mainly in
`src/xxcode/memory/extraction.py`.

Relevant production pieces:

- `ExtractionController.should_extract(...)`
- `ExtractionController.schedule(...)`
- `ExtractionController._run_extraction(...)`
- `build_extraction_prompt(...)`
- `build_extraction_registry(...)`
- `_load_extraction_system_prompt(...)`
- `parse_memory_file(...)`
- `MemoryStore`
- `write_memory_index(...)`

Existing tests already cover:

- extraction throttling and mutual exclusion
- prompt construction
- extraction prompt file loading and fallback
- extraction-safe tool registry filtering
- write/edit path restrictions
- state serialization fields
- scheduling and pending-context behavior
- a mock pipeline setup that does not run the real sub-agent

These tests prove that the extraction machinery can be prepared and scheduled.
They do not score the quality of the memory files produced by an extraction
run.

## 3. Relationship To Earlier Evaluation Layers

The three deterministic layers should be interpreted separately:

- recall quality: given an index and query, did recall select the right files?
- index organization: is `MEMORY.md` structurally healthy and useful?
- extraction quality: did the extraction output produce the right durable
  memory files?

The third layer comes before the other two in the product pipeline, but after
them in implementation order because extraction quality is harder to define.

A weak end-to-end memory outcome can come from:

- not extracting a durable fact
- writing noisy or ungrounded memory
- assigning the wrong memory type
- duplicating or conflicting with existing memory
- generating a poor `MEMORY.md`
- failing to recall the right memory later

This design isolates the first four causes.

## 4. Chosen Evaluation Strategy

Version 0.1 uses deterministic extraction-output evaluation.

Each benchmark case provides:

- a source conversation
- optional existing memory files
- expected memory facts
- candidate extraction output files
- optional expected deletions or updates

The helper materializes the candidate extraction output in a temporary memory
directory, parses the resulting markdown memory files with production
`parse_memory_file(...)`, and computes lexical/structural metrics.

The suite does not call a live model and does not run the real extraction
sub-agent. This keeps the benchmark deterministic, CI-safe, and focused on
the quality contract of extraction output rather than on model sampling or tool
execution behavior.

## 5. Design Goals

This design should:

1. remain deterministic and offline
2. stay test-local in phase one
3. reuse production memory parsing and indexing code
4. measure extraction output quality with concrete numbers
5. separate quality dimensions instead of producing one blended score
6. support TDD red-green-refactor implementation
7. leave production extraction behavior unchanged in version 0.1

## 6. Non-Goals

This design does not:

- run a live LLM judge
- run the real extraction sub-agent
- evaluate API model quality
- evaluate final assistant answers
- evaluate semantic recall
- redesign the memory file format
- introduce product-facing extraction score APIs

## 7. Scope Boundary For Version 0.1

Version 0.1 evaluates the output artifact, not the generator.

That means a case can intentionally provide "candidate extraction output" as
markdown files. The evaluator then asks:

- if these were the files produced by extraction, how good would that output be?

This is narrower than asking:

- would the current LLM extraction agent produce these files?

The narrower question is intentional. It gives the project a stable target for
what good extraction output means before adding any live-model or sub-agent
benchmarking.

## 8. Case Schema

### 8.1 Fixture Location

Version 0.1 cases should live inline in a test helper module, matching the
current recall and index evaluation approach.

Recommended helper path:

- `tests/memory/helpers/extraction_eval.py`

If the corpus grows, the same case contract may later move to:

- `tests/memory/fixtures/extraction_eval/`

### 8.2 Case Shape

Each case should define:

- `case_id`
- `conversation: list[dict]`
- `existing_memory_files: dict[str, str]`
- `candidate_memory_files: dict[str, str]`
- `expected_memory_filenames: set[str]`
- `expected_facts: set[str]`
- `expected_types: dict[str, str]`
- optional `candidate_claims: set[str]`
- optional `source_evidence: dict[str, set[str]]`
- optional `forbidden_facts: set[str]`
- optional `duplicate_facts: set[str]`
- optional `expected_latest_facts: set[str]`
- optional `obsolete_facts: set[str]`
- optional `expected_updated_filenames: set[str]`
- optional `expected_deleted_filenames: set[str]`
- optional `risk_labels: set[str]`

Field meanings:

- `conversation`: source messages that extraction is allowed to use
- `existing_memory_files`: memory files present before extraction
- `candidate_memory_files`: memory files after the candidate extraction output
- `expected_memory_filenames`: memory filenames that should exist after
  extraction
- `expected_facts`: durable facts that should be represented after extraction
- `expected_types`: expected memory type per candidate filename
- `candidate_claims`: curated claims to score for grounding; when omitted,
  helpers should default this to `expected_facts`
- `source_evidence`: optional evidence override; maps each scored claim to
  substrings that must appear in the conversation to ground that claim. When a
  claim has no explicit evidence override, helpers should use the claim text
  itself as the evidence string.
- `forbidden_facts`: facts that should not appear in memory output because they
  are ephemeral, trivial, already obsolete, or noise
- `duplicate_facts`: facts already represented by `existing_memory_files` that
  should not be newly represented by an additional created memory file
- `expected_latest_facts`: facts that should remain after a conflict/update case
- `obsolete_facts`: superseded facts that should not remain after a
  conflict/update case
- `expected_updated_filenames`: existing files that should be changed by the
  candidate output
- `expected_deleted_filenames`: existing files that should be absent after the
  candidate output
- `risk_labels`: human-readable labels for negative cases, such as
  `noise`, `duplicate`, `wrong-type`, `ungrounded`, or `conflict`

`expected_memory_filenames` and `candidate_memory_files` are intentionally
separate despite their similar names. The former is the gold set of filenames
that should exist after extraction; the latter is the full candidate output file
map being evaluated.

### 8.3 Candidate Output Semantics

`candidate_memory_files` represents the full post-extraction memory directory,
excluding `MEMORY.md`.

This is explicit because extraction can create, update, or delete files:

- create: filename exists in `candidate_memory_files` but not in
  `existing_memory_files`
- update: filename exists in both maps but content differs
- delete: filename exists in `existing_memory_files` but not in
  `candidate_memory_files`
- no-op: filename exists in both maps with identical content

Version 0.1 should not infer edits from tool traces. It should compare the two
file maps directly.

## 9. Metric Contract

### 9.1 Validity Metrics

Each case should report:

- `write_validity_rate`
- `field_completeness_rate`
- `memory_type_accuracy`

Definitions:

- `write_validity_rate`: fraction of candidate markdown files that parse with
  `parse_memory_file(...)`
- `field_completeness_rate`: fraction of parseable candidate files with non-empty
  `name`, `description`, `content`, and valid `metadata.type`
- `memory_type_accuracy`: fraction of expected typed outputs whose parsed memory
  type matches `expected_types`

Version 0.1 treats `metadata.type` as valid only when it resolves to one of:

- `user`
- `project`
- `feedback`
- `reference`

If a case has no typed expectations, `memory_type_accuracy` should be `None` and
excluded from aggregate type accuracy.

### 9.2 Coverage Metrics

Each case should report:

- `expected_memory_coverage`
- `expected_fact_coverage`

Definitions:

- `expected_memory_coverage`: fraction of `expected_memory_filenames` that
  exist in `candidate_memory_files`
- `expected_fact_coverage`: fraction of `expected_facts` represented by
  candidate memory content

Version 0.1 uses deterministic lexical matching. A fact is represented when all
normalized tokens from that expected fact are present in at least one candidate
memory's `name`, `description`, or `content`.

Normalization is fixed for version 0.1:

1. lowercase the input text
2. extract tokens with the regex `[a-z0-9]+`
3. treat a fact as represented when every token from the fact is present in the
   token set of at least one candidate memory's combined `name`, `description`,
   and `content`

This is intentionally not semantic equivalence. It is a stable first-pass
signal for whether curated expected information is present.

### 9.3 Grounding Metrics

Each case should report:

- `grounding_rate`

Definitions:

- `grounding_rate`: fraction of candidate extracted facts whose configured
  evidence substrings appear in the source conversation

Version 0.1 grounding should be evidence-driven, not free-form semantic
judgment. Each claim in `candidate_claims` maps to one or more required evidence
strings. If `source_evidence` provides an override for a claim, helpers should
use that set. Otherwise helpers should use the claim text itself as the single
evidence string. The evaluator normalizes both conversation text and evidence
strings with the same `[a-z0-9]+` token rule used for fact coverage, then checks
whether every evidence token is present in the normalized conversation token
set.

When `candidate_claims` is omitted, helpers should use `expected_facts` as the
grounding claim set. Version 0.1 should not attempt open-ended hallucination
detection for arbitrary text not listed in `candidate_claims`.

### 9.4 Noise And Suppression Metrics

Each case should report:

- `noise_suppression_rate`
- `forbidden_fact_leak_count`

Definitions:

- `noise_suppression_rate`: fraction of configured `forbidden_facts` absent from
  candidate output
- `forbidden_fact_leak_count`: number of forbidden facts represented in
  candidate output

A forbidden fact is represented by candidate output using the same lexical
matching rule as `expected_fact_coverage`: every normalized token from the
forbidden fact appears in at least one candidate memory's combined `name`,
`description`, and `content`.

Forbidden facts should cover deterministic examples such as:

- one-off task details
- temporary debugging steps
- tool output snippets
- trivial greetings
- information superseded later in the conversation

### 9.5 Duplicate And Conflict Metrics

Each case should report:

- `duplicate_control_rate`
- `conflict_update_correctness`

Definitions:

- `duplicate_control_rate`: `1.0` when candidate output does not create a new
  memory that duplicates an existing memory fact, otherwise `0.0`
- `conflict_update_correctness`: `1.0` when a case with an expected update ends
  in the configured latest fact and does not retain the obsolete conflicting
  fact, otherwise `0.0`

Version 0.1 uses curated case metadata to identify duplicate and conflict
expectations. `duplicate_facts` defines facts that are already present before
extraction and should not appear in newly created files. `expected_latest_facts`
and `obsolete_facts` define the conflict/update expectation. An obsolete fact is
considered retained if the lexical fact matcher finds it in any candidate
memory's `name`, `description`, or `content`; it does not require the old slug
or filename to remain. The evaluator should not attempt global duplicate
detection across arbitrary memory content.

If `duplicate_facts` is empty, `duplicate_control_rate` should be `None` and
excluded from aggregate duplicate rate.

If both `expected_latest_facts` and `obsolete_facts` are empty,
`conflict_update_correctness` should be `None` and excluded from aggregate
conflict rate.

## 10. Aggregate Reporting

Version 0.1 should expose a scorecard, not a single blended score.

Recommended scorecard fields:

- `n_cases`
- `mean_write_validity_rate`
- `mean_field_completeness_rate`
- `mean_expected_memory_coverage`
- `mean_expected_fact_coverage`
- `mean_grounding_rate`
- `mean_noise_suppression_rate`
- `total_forbidden_fact_leak_count`
- `n_type_cases`
- `mean_memory_type_accuracy`
- `n_duplicate_cases`
- `mean_duplicate_control_rate`
- `n_conflict_cases`
- `mean_conflict_update_correctness`

Each optional metric family should include its own case count so aggregates are
interpretable. For example, `mean_duplicate_control_rate=1.0` over
`n_duplicate_cases=1` means something different from the same rate over 20
cases.

The suite should retain raw per-case metrics for diagnostics.

## 11. Pass/Fail Policy

Version 0.1 should use conservative deterministic thresholds.

Recommended healthy-case assertions:

- `write_validity_rate == 1.0`
- `field_completeness_rate == 1.0`
- `expected_memory_coverage == 1.0`
- `expected_fact_coverage == 1.0`
- `grounding_rate == 1.0`
- `noise_suppression_rate == 1.0`

These healthy-case assertions apply to positive benchmark cases whose candidate
output is intended to be good. They are not meant to apply to intentionally
broken risk cases.

Recommended risk-case assertions:

- wrong-type case should produce `memory_type_accuracy < 1.0`
- missing-fact case should produce `expected_fact_coverage < 1.0`
- ungrounded case should produce `grounding_rate < 1.0`
- noise-leak case should produce `forbidden_fact_leak_count > 0`
- duplicate case should produce `duplicate_control_rate == 0.0`
- conflict case should produce `conflict_update_correctness == 0.0`

Aggregate tests should assert per-metric thresholds rather than one blended
score.

## 12. Component Design

The evaluation framework should stay under `tests/`.

Suggested files:

- `tests/memory/helpers/extraction_eval.py`
- `tests/memory/test_extraction_eval.py`

Responsibilities for `extraction_eval.py`:

- define extraction eval case dataclasses
- materialize existing and candidate memory directories
- parse candidate memory files through production `parse_memory_file(...)`
- treat `parse_memory_file(...) -> None` as an invalid candidate output for
  metric purposes, not as a helper crash
- use the returned `MemoryEntry` fields `name`, `description`, `content`, and
  `metadata` for quality checks
- normalize conversation and memory text
- compute validity metrics
- compute coverage metrics
- compute grounding metrics
- compute noise metrics
- compute duplicate/conflict metrics from curated metadata
- aggregate scorecards
- format compact scorecard summaries

Responsibilities for `test_extraction_eval.py`:

- validate healthy extraction cases
- validate malformed output cases
- validate missing expected fact cases
- validate wrong type cases
- validate noise suppression and leak cases
- validate duplicate and conflict cases
- validate scorecard aggregation
- validate compact report formatting

No production helper should be added in phase one unless implementation proves a
small reusable parser or validator belongs in `src/xxcode/memory/`.

## 13. Data Flow

For each extraction output case:

1. materialize `existing_memory_files` in a temporary `before` directory
2. materialize `candidate_memory_files` in a temporary `after` directory
3. parse candidate files with `parse_memory_file(...)`
4. compare existing and candidate file maps to classify create/update/delete/no-op
5. flatten source conversation into normalized text
6. flatten candidate memory `name`, `description`, and `content` into
   normalized text
7. compute validity, coverage, grounding, noise, duplicate, and conflict metrics
8. aggregate metrics into a scorecard
9. print a compact scorecard summary in pytest output

The helper may call `write_memory_index(...)` after materializing candidate
files if future tests need to validate index generation, but version 0.1 should
not make index metrics part of extraction quality. Index organization is already
covered by the second evaluation layer.

## 14. Error Handling And Diagnostics

Evaluation failures should include:

- `case_id`
- expected facts
- forbidden facts
- candidate filenames
- invalid candidate filenames
- missing expected facts
- leaked forbidden facts
- wrong type details
- duplicate/conflict details
- per-case metric values

Invalid case definitions should fail before metric computation when:

- `expected_types` references an unknown memory type
- `expected_types` references a filename not present in
  `candidate_memory_files`
- `source_evidence` references a claim not listed in `candidate_claims` or
  `expected_facts`
- `expected_updated_filenames` references a filename not present in
  `existing_memory_files`
- `expected_deleted_filenames` references a filename not present in
  `existing_memory_files`

Malformed candidate memory files are not invalid case definitions. They are
valid risk inputs and should reduce `write_validity_rate`.

## 15. Testing Strategy

Implementation should use TDD red-green-refactor.

Recommended order:

1. add failing tests for valid candidate memory parsing and scorecard skeleton
2. implement case dataclasses, materialization, parsing, and validity metrics
3. add failing tests for expected fact coverage
4. implement lexical fact matching
5. add failing tests for grounding and forbidden fact leakage
6. implement evidence and noise checks
7. add failing tests for wrong type, duplicate, and conflict cases
8. implement optional metric aggregation with `n_*_cases`
9. add compact scorecard formatting
10. run existing extraction, recall eval, and index eval tests together
11. run full `tests/memory`

The first implementation should stay small. Corpus growth, live-model
benchmarks, and sub-agent tool-trace replay should be deferred.

## 16. Implementation Boundaries

Phase one should not modify production extraction behavior.

Preferred approach:

- keep evaluation helpers under `tests/memory/helpers/`
- keep curated cases inline in helper code
- reuse production `parse_memory_file(...)` and memory type handling
- avoid live API calls
- avoid running real `SubAgent`

Fallback approach if needed:

- add a narrowly scoped production validator only if tests reveal a format check
  that should be shared by runtime and evaluation

Any fallback production change should preserve current runtime behavior unless a
separate bug fix is explicitly justified.

## 17. Success Criteria

This design is successful when the repository gains a deterministic extraction
quality benchmark that can answer:

- whether candidate memory output is parseable
- whether expected durable facts were captured
- whether memory types were assigned correctly
- whether output is grounded in the source conversation
- whether noisy or forbidden facts were suppressed
- whether duplicate writes are avoided
- whether conflicting updates keep the latest fact and drop obsolete facts
- which specific cases are weak

## 18. Deferred Work

Later phases may add:

- mock SubAgent tool-trace replay
- live-model extraction benchmark runs
- semantic grounding judged by a frozen local model or live judge
- cross-session long-term consistency evaluation
- end-to-end answer impact evaluation
- trend reports comparing scorecards across commits
