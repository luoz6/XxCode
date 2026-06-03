# Memory End-To-End Effectiveness Evaluation Design

> Version 0.1 | 2026-06-03 | Scope: define a deterministic SDD-first evaluation layer for final-answer memory effectiveness

## 1. Objective

The existing deterministic memory evaluation layers answer upstream questions:

- recall quality: did the recall stage select the right memory files?
- recall stability: did recall stay stable under controlled perturbations?
- index organization: is `MEMORY.md` structurally useful for recall?
- extraction quality: did extraction output valid, grounded, durable memories?

They do not answer the final product question:

- when memory is available, does the assistant's final answer actually use the
  right memory and avoid the wrong memory?

This design adds a fourth deterministic evaluation layer for end-to-end memory
effectiveness. Version 0.1 evaluates the final answer stage using a
deterministic assistant surrogate rather than a live LLM.

The suite should produce concrete numbers for:

- memory usage in final answers
- preference adherence
- stale or obsolete memory avoidance
- answer grounding in recalled memory
- memory-enabled answer lift over a no-memory baseline

## 2. Current State

The repository already has deterministic memory and context evaluation helpers:

- `tests/memory/helpers/recall_eval.py`
- `tests/memory/helpers/index_eval.py`
- `tests/memory/helpers/extraction_eval.py`
- `tests/context/helpers/context_eval.py`

The context evaluation layer already checks whether memory-related content is
injected into the prepared context snapshot. That means this design should not
duplicate context snapshot validation.

Instead, this layer evaluates what happens after context construction:

1. given a user query
2. given a deterministic set of recalled memory snippets
3. given optional no-memory baseline output
4. does the produced answer satisfy memory-dependent expectations?

## 3. Problem To Solve

Current tests can prove that memory can be extracted, indexed, recalled, and
placed into context. They cannot yet prove that the final answer benefits from
memory.

For example, all upstream layers could pass while the final answer still:

- ignores a recalled user preference
- uses a stale memory instead of the latest memory
- leaks an obsolete or forbidden fact
- answers generically when memory should make the answer specific
- claims facts not present in either the query or recalled memory

The missing layer is a deterministic benchmark that scores answer behavior
against memory-dependent gold expectations.

## 4. Design Goals

This design should:

1. remain fully deterministic and CI-safe
2. avoid live model calls in version 0.1
3. separate final-answer effectiveness from recall, index, and extraction
   quality
4. produce per-case metrics and aggregate scorecards
5. support red-green-refactor TDD implementation
6. make weak cases directly readable from pytest output

## 5. Non-Goals

Version 0.1 does not:

- evaluate live LLM answer quality
- replace recall, index, extraction, or context snapshot evaluation
- judge style, fluency, creativity, or general helpfulness
- measure latency, token cost, or tool-use behavior
- run multi-session production memory orchestration end to end

Live-model answer evaluation and richer A/B product scoring are deferred to
later versions.

## 6. Chosen Evaluation Strategy

The chosen strategy is a deterministic final-answer surrogate:

- benchmark cases define a query, recalled memory snippets, and gold answer
  expectations
- a deterministic assistant builds an answer from the query and available
  recalled memories
- metric helpers score whether the answer used required memory facts, followed
  preferences, avoided stale memory, and improved over a no-memory baseline

This is a surrogate for final LLM behavior, not a claim that it is equivalent to
production generation. Its purpose is to create a frozen yardstick for
regression testing and case design.

## 7. Relationship To Existing Evaluation Layers

This fourth layer should be interpreted as downstream from the first three
memory layers and adjacent to context engineering evaluation:

- recall evaluation answers: "Were the right memories selected?"
- context evaluation answers: "Were selected memories present in the prepared
  context snapshot?"
- end-to-end effectiveness answers: "Did the final answer use the memory
  correctly?"

Version 0.1 should avoid re-testing the full production context pipeline unless
implementation friction proves that a narrow reuse is valuable. The benchmark
may accept recalled memory snippets directly as test input.

This keeps failure diagnosis clear:

- if recall selected the wrong file, recall eval should fail
- if context omitted recalled memory, context eval should fail
- if an answer ignores a correct recalled memory, this end-to-end eval should
  fail

## 8. Benchmark Dataset Design

### 8.1 Fixture Location

The first implementation should keep curated cases inline in a test-local helper
module, matching the current recall/index/extraction evaluation pattern.

Recommended helper location:

- `tests/memory/helpers/effectiveness_eval.py`

Recommended test location:

- `tests/memory/test_effectiveness_eval.py`

If the corpus grows large, cases may later move to:

- `tests/memory/fixtures/effectiveness_eval/`

That migration is deferred for version 0.1.

### 8.2 Case Shape

Each benchmark case should define:

- `case_id: str`
- `query: str`
- `recalled_memories: dict[str, str]`
- `answer: str | None`
- `answer_claims: set[str] | None`
- `baseline_answer: str | None`
- `expected_answer_facts: set[str]`
- `expected_memory_facts_used: set[str]`
- `forbidden_answer_facts: set[str]`
- `expected_preferences_applied: set[str]`
- `obsolete_facts: set[str]`
- `risk_labels: set[str]`

Field meanings:

- `recalled_memories`: mapping of `filename -> memory text` available to the
  deterministic assistant; version 0.1 treats this as already recalled input
- `answer`: optional pre-written answer to score directly; when this is not
  `None`, the evaluator must not call the deterministic assistant
- `answer_claims`: optional explicit claims present in `answer`; retained for
  future grounding analysis but not scored in version 0.1
- `baseline_answer`: optional no-memory answer used for memory-lift comparison
- `expected_answer_facts`: facts that should appear in the final answer
- `expected_memory_facts_used`: subset of facts that must be supported by
  recalled memory, not only by the query
- `forbidden_answer_facts`: facts that must not appear in the final answer
- `expected_preferences_applied`: durable preferences that should affect the
  final answer
- `obsolete_facts`: outdated facts that must not appear in the final answer
- `risk_labels`: optional labels such as `stale-memory`, `missing-preference`,
  `generic-answer`, or `ungrounded-answer`

If a serialized fixture format is introduced later and lacks native set
support, the loader may normalize list values into sets. The semantic contract
remains set-based unless a future version adds order-aware answer metrics.

### 8.3 Case Categories

Version 0.1 should include at least these categories:

1. preference adherence: answer follows a recalled user preference
2. project constraint usage: answer includes a recalled project-specific rule
3. stale memory avoidance: answer uses latest fact and suppresses obsolete fact
4. no-memory baseline lift: memory-enabled answer is more specific than baseline
5. generic-answer risk: answer fails to use available memory
6. ungrounded-answer risk: answer introduces a fact absent from recalled memory

The suite should be small and curated. The purpose is clear diagnostic signal,
not broad benchmark scale.

### 8.4 Generated-Answer And Prewritten-Answer Modes

Version 0.1 has two explicit case modes:

1. generated-answer mode: `answer is None`, so the deterministic assistant
   generates the answer from `query` and `recalled_memories`
2. prewritten-answer mode: `answer` is a non-empty string, so the evaluator
   scores that answer directly and does not call the deterministic assistant

Healthy cases should usually use generated-answer mode. Risk cases should
usually use prewritten-answer mode so they can simulate failures the
deterministic assistant should not naturally produce, such as:

- ignoring available memory
- using an obsolete fact
- introducing an ungrounded fact
- producing a generic answer when memory should make the answer specific

This separation keeps generation and detection decoupled. The deterministic
assistant is responsible for healthy deterministic behavior; prewritten answers
are responsible for evaluator risk coverage.

## 9. Deterministic Assistant Contract

### 9.1 Input

The deterministic assistant receives:

- the user query
- recalled memory snippets
- optional case metadata

It should not call a live model.

### 9.2 Output

It returns an answer string. Version 0.1 may implement this with simple lexical
rules:

1. tokenize query and memory text with `[a-z0-9]+`
2. select recalled memory facts whose tokens overlap with the query
3. include selected memory facts in a stable answer template
4. optionally apply preference phrases when the case expects preference use
5. break ties deterministically by filename

The deterministic assistant should not be responsible for generating bad or
risk outputs. Risk outputs are provided through the case `answer` field and
scored directly.

### 9.3 Answer Selection Policy

For every case, the evaluator selects the answer with this rule:

```python
if case.answer is not None:
    answer = case.answer
else:
    answer = deterministic_assistant(case.query, case.recalled_memories)
```

This makes the two test purposes explicit:

- generated-answer cases verify that a deterministic answer path can use
  correct memory
- prewritten-answer cases verify that the evaluator detects known failure modes

### 9.4 Why Not A Live LLM

Live LLM output would make first-version CI noisy and would blur failure
diagnosis. A bad score could mean:

- recall failed
- context construction failed
- the model ignored context
- the evaluator mis-scored a paraphrase
- sampling changed

Version 0.1 intentionally removes live generation from the equation. Live-model
evaluation can be added later as an optional, non-CI benchmark.

## 10. Metric Contract

### 10.1 Token Normalization

All lexical fact matching in version 0.1 uses the same normalization rule:

- lowercase the text
- extract tokens with regex `[a-z0-9]+`
- compare facts by token-subset matching

Token order, negation, and paraphrase are not considered in version 0.1. False
positives on token-identical but semantically opposite text are a known
limitation.

### 10.2 Per-Case Metrics

Each case should report:

- `answer_fact_coverage`
- `memory_fact_usage_rate`
- `preference_adherence_rate`
- `forbidden_fact_absence_rate`
- `obsolete_fact_suppression_rate`
- `memory_lift`
- `memory_lift_delta`

Definitions:

- `answer_fact_coverage = represented expected_answer_facts / expected_answer_facts`
- `memory_fact_usage_rate = represented expected_memory_facts_used / expected_memory_facts_used`
- `preference_adherence_rate = represented expected_preferences_applied / expected_preferences_applied`
- `forbidden_fact_absence_rate = forbidden facts absent from answer / forbidden_answer_facts`
- `obsolete_fact_suppression_rate = obsolete facts absent from answer / obsolete_facts`
- `memory_lift = 1.0` when the memory-enabled answer covers more expected facts
  than `baseline_answer`, otherwise `0.0`
- `memory_lift_delta = answer_fact_coverage - baseline_answer_fact_coverage`

### 10.3 Optional Metric Behavior

Some metrics have no denominator for some cases. Version 0.1 should use `None`
for not-applicable per-case metrics and exclude those cases from corresponding
scorecard means.

Rules:

- if `expected_memory_facts_used` is empty, `memory_fact_usage_rate = None`
- if `expected_preferences_applied` is empty, `preference_adherence_rate = None`
- if `forbidden_answer_facts` is empty, `forbidden_fact_absence_rate = None`
- if `obsolete_facts` is empty, `obsolete_fact_suppression_rate = None`
- if `baseline_answer is None`, `memory_lift = None`
- if `baseline_answer is None`, `memory_lift_delta = None`

`answer_fact_coverage` should be `1.0` when `expected_answer_facts` is empty.
Because an empty expectation set can hide weak case design, curated version 0.1
cases with empty `expected_answer_facts` must use a risk label that explains
why no positive answer fact is expected, such as `noise-only` or
`forbidden-only`.

### 10.4 Grounding Contract

Version 0.1 does not compute a separate `grounding_rate`. The earlier
grounding-like requirement is represented by `memory_fact_usage_rate` and by
forbidden/obsolete fact absence metrics.

`answer_claims` is included in the case shape only as a future extension point
for live or semantic answer evaluation. It should not affect version 0.1
scorecards.

## 11. Aggregate Reporting

Version 0.1 should expose a scorecard rather than a single blended score.

Recommended scorecard fields:

- `n_cases`
- `mean_answer_fact_coverage`
- `n_memory_usage_cases`
- `mean_memory_fact_usage_rate`
- `n_preference_cases`
- `mean_preference_adherence_rate`
- `n_forbidden_cases`
- `mean_forbidden_fact_absence_rate`
- `n_obsolete_cases`
- `mean_obsolete_fact_suppression_rate`
- `n_lift_cases`
- `memory_lift_rate`
- `mean_memory_lift_delta`

Each mean field excludes cases where the corresponding per-case metric is
`None`. The companion `n_*` field reports the denominator.

The suite should retain raw per-case metrics so weak cases can be inspected
without relying only on aggregate means.

## 12. Pass/Fail Policy

The evaluation layer should both report values and fail on deterministic
regressions.

Recommended threshold style:

- healthy curated cases assert exact expected answer facts are represented
- risk cases assert the intended weakness is exposed
- aggregate tests assert scorecard floors

Initial floors should be conservative:

- `mean_answer_fact_coverage >= 0.85`
- `mean_memory_fact_usage_rate >= 0.85`
- `mean_preference_adherence_rate >= 0.85`
- `mean_forbidden_fact_absence_rate >= 0.95`
- `mean_obsolete_fact_suppression_rate >= 0.95`
- `memory_lift_rate >= 0.80`
- `mean_memory_lift_delta > 0.0`

Exact floors may be adjusted during TDD once the curated corpus is implemented,
but the suite should avoid a single blended total score.

## 13. Component Design

The evaluation framework should remain test-local.

Add:

- `tests/memory/helpers/effectiveness_eval.py`
- `tests/memory/test_effectiveness_eval.py`

Helper responsibilities:

- define case and metric dataclasses
- run the deterministic assistant
- compute per-case metrics
- compute aggregate scorecards
- provide curated benchmark cases
- format compact pytest-readable summaries

The helper may reuse lexical utility patterns from `recall_eval.py`,
`extraction_eval.py`, and `context_eval.py`, but should not import private
production APIs.

## 14. Data Flow

For each effectiveness case:

1. build deterministic assistant input from query and recalled memories
2. generate or accept a deterministic answer
3. compare answer against expected facts, memory facts, preferences, forbidden
   facts, and obsolete facts
4. compare memory-enabled answer with `baseline_answer` when present
5. compute per-case metrics
6. aggregate metrics into a scorecard
7. print a compact summary for pytest output

Version 0.1 does not need to materialize files on disk because the recall stage
has already been evaluated separately.

## 15. Error Handling And Failure Diagnostics

The suite should fail loudly when a case is invalid.

Required validation:

- every `expected_memory_facts_used` fact must be present in at least one
  recalled memory snippet
- every `expected_preferences_applied` fact must also be present in
  `expected_answer_facts`
- `forbidden_answer_facts` must not overlap `expected_answer_facts`
- `obsolete_facts` must not overlap `expected_answer_facts`
- if `baseline_answer` is not `None`, it must be non-empty after trimming
- if `answer` is not `None`, it must be non-empty after trimming
- if `expected_answer_facts` is empty, `risk_labels` must explain why
- case ids must be unique within curated benchmark helpers

Failure output should include:

- `case_id`
- query
- answer
- expected answer facts
- missing expected facts
- used memory facts
- leaked forbidden facts
- leaked obsolete facts
- metric values

## 16. Testing Strategy

Implementation should follow TDD red-green-refactor:

1. add minimal failing tests for token matching and metric computation
2. add answer-selection tests for generated-answer and prewritten-answer modes
3. add deterministic assistant behavior for healthy generated-answer cases
4. add optional metric and scorecard aggregation tests
5. add curated healthy cases
6. add risk cases that intentionally expose weaknesses through prewritten
   answers
7. add final memory-suite regression

The first implementation should avoid production code changes.

## 17. Implementation Boundaries For Phase One

Preferred approach:

- test-local helper only
- no production code changes
- no live model calls
- no network access

Fallback approach if needed:

- reuse existing public helper functions from `tests/context/helpers` or
  `tests/memory/helpers`, but avoid coupling this layer to context snapshot
  internals

This boundary keeps the fourth layer focused on final-answer effectiveness
rather than re-testing the entire memory pipeline.

## 18. Success Criteria

This design is successful when the repository gains a deterministic benchmark
that can answer:

- did the final answer use recalled memory?
- did the answer follow recalled preferences?
- did the answer avoid stale or forbidden memory?
- did memory produce measurable lift over a no-memory answer?
- which answer-effectiveness cases are weak?

## 19. Deferred Work

Later versions may add:

- live LLM final-answer evaluation
- model-judge or rubric-based semantic scoring
- multi-session memory update and query scenarios
- order-aware answer metrics
- paraphrase-aware fact matching
- optional A/B benchmark scripts outside CI

Those are intentionally deferred so version 0.1 remains deterministic and
shippable.
