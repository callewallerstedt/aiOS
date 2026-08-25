# Harness benchmark

A small, honest measurement of the aiOS CODE harness. Not a leaderboard entry —
the question is narrower and more useful:

> For a task this harness should find easy, what does it cost in tokens, wall
> clock and tool calls, and does the change actually work?

```bash
python bench/run_bench.py --tasks 8
python bench/run_bench.py --tasks 8 --model qwen/qwen3-coder --label cheap
python -m bench.run_bench --tasks tweak=3 --profile legacy --label before
```

## Answering "did that harness change help?"

`--profile legacy` restores the pre-2026-08 prompt and verification rules, so a
change to the loop is two runs rather than an opinion. Run three of each: a
single run of these tasks has a ~2x token spread, and the first conclusion drawn
from one run of each profile was wrong in both directions.

The `tweak` suite exists for this. Every other suite asks a hard question; that
one asks the easy question — "darken this grey", "sort by the other field" — in
a repo with enough bulk that reading a whole file costs something. That is where
the harness was losing, so that is where a harness change shows up.

Measured on 2026-08-07, deepseek-v4-flash, three runs each, medians:

| | legacy | lean |
|---|---|---|
| tokens per run | 136,288 | 80,318 |
| seconds per pass | 49.4 | 31.5 |
| tool calls per task | 6 | 4 |
| **worst task, tool calls** | **17** | **5** |
| **worst task, tokens** | **127,693** | **37,573** |

Both profiles passed 9/9. The medians moved by a third; the worst cases moved by
three times, which is the number that matters — the complaint was never the
average session.

## How it works

Each [HumanEval](https://github.com/openai/human-eval) problem becomes a
*repository task* rather than a completion prompt. The agent gets a real git
repo containing a stub file and a brief, and has to navigate, edit and finish —
so the run exercises the whole loop (read → edit → verify → review), not just
the model's ability to write a function body.

Verification runs the problem's hidden test in a separate process against
whatever ended up on disk. The agent's own claim of success is never consulted,
which is the same rule the in-harness reviewer follows.

Problems are sampled with a fixed stride, so two runs are comparable and a lucky
streak of easy problems cannot flatter a harness change.

## Pinned Aider Polyglot Python subset

`aider_polyglot` is a deterministic, low-cost public-source suite containing
exactly six Python exercises, in this order: `phone-number`, `wordy`,
`bowling`, `forth`, `poker`, and `zipper`.

```bash
python -m bench.run_bench --tasks aider_polyglot=3 --label aider-python-smoke
```

The source is
[Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark/tree/7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f),
pinned to commit `7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f`. On first
selection, aiOS downloads the exact base instruction, Python-specific
instruction append, starter module, and unittest from raw GitHub. Every file is
SHA-256 checked and cached under
`bench/data/aider-polyglot/<commit>/python/exercises/practice/`; later runs use
the cache. The instruction files and upstream unittest are protected from
edits. The external verifier runs that unittest directly with the current
Python interpreter.

This is an aiOS harness-regression subset, **not an Aider leaderboard run**. It
uses only 6 Python tasks rather than the official 225-task, six-language suite,
and it does not reproduce Aider's prompt, edit format, retry policy, or scoring.
Do not publish its pass rate as an Aider Polyglot score. Exercise content is
attributed to Exercism by the upstream repository and remains subject to the
corresponding Exercism track license.

## Pinned Aider Refactoring subset

`aider_refactor` is a fixed, size-stratified five-task subset of the official
89-task Aider refactoring benchmark. The selected Python modules range from
17,015 to 120,534 raw bytes, so this suite tests whether a harness can extract
complete methods from genuinely large files without eliding code.

```bash
python -m bench.run_bench --tasks aider_refactor=5 --label aider-refactor-proof
```

The source is
[Aider-AI/refactor-benchmark](https://github.com/Aider-AI/refactor-benchmark/tree/c90dfb67d829f4da2759955a69111fc5f3b0e0fd),
pinned to commit `c90dfb67d829f4da2759955a69111fc5f3b0e0fd`. Before a
run is created, aiOS downloads or reads from cache the instruction, source, and
upstream test for every selected task. Every artifact has an explicit SHA-256
pin. A mismatch aborts preflight before any runner starts.

The upstream test remains runnable in the isolated workspace, but scoring does
not trust it. aiOS generates an external AST grader which independently checks
the same upstream criteria: valid Python, a complete top-level function within
10% of the original method's AST size, and the original class reduced to within
10% of its expected post-extraction AST size.

This is a public **subset**, not an official full-suite or leaderboard result.
Its percentage must not be compared with published Aider benchmark scores.

## Reading the results

Results are written to `bench/results/<timestamp>-<label>.json`.

| metric | why it is there |
|---|---|
| `pass_rate` | correctness, measured externally |
| `tokens_per_pass` | the metric that matters — a cheap failure is not efficient |
| `cache_hit_rate` | input dominates these tasks; this is the lever, not output length |
| `tool_calls_per_task` | counts real tool calls, not activity rows |
| `seconds_per_task` | includes the review pass |

`tool_calls` deliberately excludes thinking, plan and review activities. A
thinking block emits a started/update/completed triple, so counting rows made
three tools out of one thought and reported ~20 calls for a task that made
three.

## Caveats worth keeping in mind

- HumanEval problems are small and self-contained. A good score here says the
  loop is sound; it says nothing about large-repository navigation.
- Token totals include the independent reviewer, which runs on every completed
  job. That is deliberate — reporting a job's cost without it would make the
  harness look cheaper than it is.
- Providers report usage differently. Only provider-reported figures are summed;
  nothing is estimated.
