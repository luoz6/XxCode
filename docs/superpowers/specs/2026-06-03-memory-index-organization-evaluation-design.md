# Memory Index Organization Evaluation Design

> Version 0.1 | 2026-06-03 | Scope: define a deterministic SDD-first evaluation layer for `MEMORY.md` index and context organization quality

## 1. Objective

The recall evaluation suite measures whether a fixed query retrieves the right
memory files from a fixed `MEMORY.md` entrypoint. That is the correct first
layer, but it does not explain whether the index itself is well organized.

This design adds a second deterministic evaluation layer for `MEMORY.md`
organization. It should answer:

- whether all valid memory files are represented in the index
- whether index entries point to real files
- whether the index is parseable by the runtime parser
- whether descriptions are compact and discriminative enough to support recall
- whether the index stays inside context budget limits
- whether the generated index preserves the expected type ordering

This work evaluates the index/context organization layer only. It does not
evaluate memory extraction, semantic recall quality, or final answer quality.

## 2. Current State

The runtime memory entrypoint is `MEMORY.md`, implemented mainly in
`src/xxcode/memory/index.py`.

Relevant production APIs:

- `generate_memory_index(memory_dir)`
- `write_memory_index(memory_dir)`
- `load_memory_index(memory_dir)`
- `parse_memory_index(content)`
- `truncate_entrypoint_content(raw)`

Existing tests verify core behavior:

- empty index generation
- sorting by memory type
- description truncation
- line and byte truncation
- parsing markdown links
- ignoring `MEMORY.md` and duplicate links
- incremental add/update/remove behavior

What is missing is a scorecard that summarizes organization quality across a
curated set of index scenarios. Existing tests prove behavior, but they do not
produce index quality values such as coverage, stale-reference rate, duplicate
rate, parseability, or budget utilization.

## 3. Relationship To Recall Evaluation

The first recall benchmark answers:

- given this index and this query, did recall select the right files?

This index organization benchmark answers:

- is this index structurally healthy enough to be a good recall entrypoint?

These are related but separate. A recall case can fail because:

- the selector is weak
- the query is ambiguous
- the index description is missing or misleading
- the index references stale or duplicate files
- the index is too large and gets truncated

This second layer isolates the last three causes.

## 4. Design Goals

This design should:

1. remain fully deterministic and CI-safe
2. stay test-local in phase one
3. reuse the runtime parser, generator, and truncation constants
4. report raw metrics and scorecards instead of a single blended score
5. keep the benchmark small enough to review by hand
6. avoid any live model or LLM judge dependency

## 5. Non-Goals

This design does not:

- judge whether extracted memories are true
- evaluate whether a conversation should have produced a memory
- run semantic recall or final answer generation
- compare against external memory products
- redesign the `MEMORY.md` format

## 6. Evaluation Strategy

Use deterministic curated cases that materialize memory files and index content,
then score the index using structural and lexical checks.

There are two case families:

- `generated_index_cases`: create memory files, call `generate_memory_index(...)`,
  and verify organization properties of the generated index
- `raw_index_cases`: provide explicit `index_content` to evaluate malformed,
  duplicate, stale, generic, oversized, or otherwise risky indexes

Phase one should keep these cases inline in a test helper module, matching the
current recall evaluation approach. If the corpus grows, migrate the same case
contract to files under `tests/memory/fixtures/index_eval/`.

## 7. Case Schema

### 7.1 Generated Index Case

Each generated case should define:

- `case_id`
- `memory_files: dict[str, str]`
- `expected_indexed_filenames: set[str]`
- optional `expected_type_order: list[str]`

The helper writes `memory_files` to a temporary directory, calls
`generate_memory_index(...)`, then evaluates the generated `MEMORY.md` content.

### 7.2 Raw Index Case

Each raw case should define:

- `case_id`
- `index_content: str`
- `memory_files: dict[str, str]`
- `expected_present_filenames: set[str]`
- optional `risk_labels: set[str]`

The helper writes `memory_files` and evaluates `index_content` directly. It
should not call `generate_memory_index(...)`, because raw cases intentionally
cover broken or risky index states.

## 8. Metric Contract

### 8.1 Structural Metrics

Each case should report:

- `indexed_file_count`
- `memory_file_count`
- `coverage_rate`
- `stale_reference_rate`
- `duplicate_reference_rate`
- `parseable_line_rate`
- `memory_md_exclusion`

Definitions:

- `indexed_file_count`: number of unique non-`MEMORY.md` filenames parsed from
  the index
- `memory_file_count`: number of memory files available in the case, excluding
  `MEMORY.md`
- `coverage_rate = indexed_existing_files / memory_file_count`
- `stale_reference_rate = stale_references / raw_index_references`
- `duplicate_reference_rate = duplicate_references / raw_index_references`
- `parseable_line_rate = parseable_index_lines / candidate_index_lines`
- `memory_md_exclusion = 1.0` when the index does not include `MEMORY.md`,
  otherwise `0.0`

Raw reference counting must not rely only on `parse_memory_index(...)`, because
that parser intentionally deduplicates and drops `MEMORY.md`. The evaluation
helper should use a separate raw markdown-link scanner for duplicate and
`MEMORY.md` diagnostics, while still using `parse_memory_index(...)` for runtime
parseability.

### 8.2 Description Metrics

Each case should report:

- `description_present_rate`
- `description_budget_compliance_rate`
- `generic_description_rate`
- `discriminative_token_rate`

Definitions:

- `description_present_rate`: fraction of parsed entries with non-empty
  descriptions
- `description_budget_compliance_rate`: fraction of raw index lines that fit the
  line budget implied by `_LINE_BUDGET`
- `generic_description_rate`: fraction of descriptions matching a small
  deterministic generic phrase list, such as `note`, `misc`, `todo`,
  `information`, `general`, `stuff`, or `memory`
- `discriminative_token_rate`: fraction of entries whose description contains at
  least one meaningful token that is not already present in the filename stem

The discriminative metric is intentionally lexical. It is not a semantic quality
judge; it is a deterministic warning that descriptions may not add enough signal
for recall.

### 8.3 Budget Metrics

Each case should report:

- `line_utilization`
- `byte_utilization`
- `was_line_truncated`
- `was_byte_truncated`

Definitions:

- `line_utilization = line_count / MAX_ENTRYPOINT_LINES`
- `byte_utilization = byte_count / MAX_ENTRYPOINT_BYTES`
- `was_line_truncated` and `was_byte_truncated` come from
  `truncate_entrypoint_content(...)`

Generated benchmark cases should normally expect no truncation. Raw benchmark
cases may intentionally include truncation-risk examples.

### 8.4 Ordering Metrics

Generated cases should report:

- `type_order_adherence`

Definition:

- `type_order_adherence = 1.0` when entries generated by
  `generate_memory_index(...)` follow the expected runtime type order:
  `user`, `project`, `feedback`, `reference`
- otherwise `0.0`

Raw cases should not require type order unless the case explicitly opts in.

## 9. Scorecard Contract

Version 0.1 should expose a scorecard, not a single total score.

Recommended scorecard fields:

- `n_cases`
- `mean_coverage_rate`
- `mean_stale_reference_rate`
- `mean_duplicate_reference_rate`
- `mean_parseable_line_rate`
- `mean_description_present_rate`
- `mean_description_budget_compliance_rate`
- `mean_generic_description_rate`
- `mean_discriminative_token_rate`
- `mean_line_utilization`
- `mean_byte_utilization`
- `truncated_case_count`
- `type_order_adherence_rate`

Interpretation:

- higher is better for coverage, parseability, description presence,
  discriminative tokens, and type order adherence
- lower is better for stale references, duplicates, generic descriptions,
  utilization, and truncation count

The scorecard should also retain per-case metric objects so failures identify
the exact weak case.

## 10. Pass/Fail Policy

Use conservative deterministic thresholds:

- generated healthy cases should have `coverage_rate == 1.0`
- generated healthy cases should have `stale_reference_rate == 0.0`
- generated healthy cases should have `duplicate_reference_rate == 0.0`
- generated healthy cases should have `parseable_line_rate == 1.0`
- generated healthy cases should have `memory_md_exclusion == 1.0`
- generated healthy cases should have `was_line_truncated is False`
- generated healthy cases should have `was_byte_truncated is False`
- generated healthy cases should have `type_order_adherence == 1.0`

Raw risk cases should assert the expected metric detects the intended problem.
For example:

- stale-reference case should produce `stale_reference_rate > 0.0`
- duplicate case should produce `duplicate_reference_rate > 0.0`
- generic-description case should produce `generic_description_rate > 0.0`
- oversized case should produce a truncation signal

This avoids a benchmark that only proves ideal cases work.

## 11. Component Design

The evaluation framework should stay under `tests/`.

Suggested files:

- `tests/memory/helpers/index_eval.py`
- `tests/memory/test_index_eval.py`

Responsibilities for `index_eval.py`:

- define generated and raw case dataclasses
- materialize memory files in temporary directories
- scan raw markdown links without deduplication
- call `generate_memory_index(...)` for generated cases
- call `truncate_entrypoint_content(...)` for budget metrics
- call `parse_memory_index(...)` for runtime parseability
- compute per-case metrics
- compute scorecards
- format compact scorecard summaries

Responsibilities for `test_index_eval.py`:

- validate healthy generated index cases
- validate raw risk cases
- validate scorecard aggregation
- validate compact reporting

## 12. Data Flow

For generated cases:

1. create a temporary memory directory
2. write memory files
3. call `generate_memory_index(...)`
4. compute index organization metrics
5. assert healthy thresholds
6. aggregate scorecard values

For raw cases:

1. create a temporary memory directory
2. write memory files
3. use the case `index_content` directly
4. compute index organization metrics
5. assert the intended risk is detected
6. aggregate raw-case diagnostics separately or with risk labels

## 13. Error Handling And Failure Diagnostics

Validation errors should include:

- `case_id`
- missing expected filenames
- raw index filenames that do not exist
- memory files not represented in the index
- parseability failures
- truncation status

Metric assertion failures should include the full per-case metric dataclass so
pytest output directly identifies which property regressed.

## 14. Testing Strategy

Implementation should use TDD red-green-refactor.

Recommended order:

1. add failing raw-link scanner tests for stale, duplicate, and `MEMORY.md`
   references
2. implement raw scanning and structural metrics
3. add failing generated healthy-case tests
4. implement generated case materialization and scorecard aggregation
5. add failing description metric tests
6. implement generic-description and discriminative-token metrics
7. add failing budget metric tests
8. implement budget scorecard fields
9. run `tests/memory/test_index.py`, `tests/memory/test_index_eval.py`, and the
   existing recall evaluation tests together

## 15. Implementation Boundaries

Phase one should not modify production index behavior unless a real bug is found
while writing tests.

Preferred approach:

- keep evaluation helpers and cases under `tests/`
- reuse production parser/generator APIs
- add production changes only if existing behavior contradicts the documented
  runtime contract

## 16. Success Criteria

This design is successful when the repository gains a deterministic index
organization benchmark that can answer:

- whether `MEMORY.md` covers all available memory files
- whether it contains stale or duplicate references
- whether lines are parseable by runtime code
- whether descriptions are present, compact, and not overly generic
- whether the index stays inside line and byte budgets
- whether generated indexes preserve expected type ordering

## 17. Deferred Work

Later phases may add:

- extraction quality evaluation
- end-to-end long-memory answer evaluation
- semantic description quality judged by a live model
- real-project corpus reports
- trend reports comparing scorecards across commits
