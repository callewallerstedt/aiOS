# aiOS CODE harness research and benchmark campaign

Date: 2026-08-23

## Outcome

aiOS remained the most efficient harness in the matched aiOS/Oh My Pi/Hermes
comparison. The highest-confidence new harness change is progressive tool-schema
disclosure: two repeated four-task A/B pairs kept all 8 tasks passing while
reducing total tokens by 13.3%, canonical prompt tokens by 14.0%, model requests
by 10.8%, tool calls by 7.8%, provider cost by 8.4%, and wall time by 1.7%.

The campaign made no Opus or Fable calls. Provider-reported BENCH spend was
$0.791735832 for 126 task attempts and 13,367,438 tokens. One pre-proxy Kimi
run lacked authoritative cost and adds an estimated $0.01401, for an approximate
campaign total of $0.806.

## Matched harness baseline

The existing 24-task Ox Alpha campaign used coder-only configurations.

| Harness | Pass | Score | Persisted total tokens | Total time | Tool calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| aiOS | 23/24 | 87.6 | 1,137,363 | 1,929.9 s | 186 |
| Oh My Pi | 23/24 | 75.6 | 3,882,460 | 3,247.2 s | 247 |
| Hermes | 16/24 | 55.8 | 2,005,380 | 6,022.0 s | unavailable |

Hermes stopped early and missing failed/timeout artifacts contain no usage, so
its full-lane token total is a lower bound. On the 17 tasks with complete usage
for all three harnesses, aiOS passed 17/17 with 375,682 tokens; OMP passed 17/17
with 1,491,134; Hermes passed 16/17 with 2,005,380. OMP used 3.97x aiOS's
canonical prompt tokens and 2.00x its time; Hermes used 5.34x tokens and 5.00x
time.

Run artifacts:

- `bench/runs/20260822-221900-763b/run.json` (aiOS)
- `bench/runs/20260822-221900-1cd0/run.json` (OMP)
- `bench/runs/20260822-221900-515e/run.json` (Hermes)

## Progressive schema A/B

All four lanes ran the same four tasks with GPT-5.6 Luna. Two repetitions were
run with the complete tool schema and two with progressive disclosure.

| Profile | Pass | Avg score | Total tokens | Canonical prompt | Cost | Time | Requests | Tools |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Full schemas | 8/8 | 86.2 | 642,365 | 609,556 | $0.077637 | 567.2 s | 83 | 102 |
| Progressive | 8/8 | 87.2 | 557,197 | 524,112 | $0.071139 | 557.4 s | 74 | 94 |

Run artifacts:

- `bench/runs/20260823-212820-fe13/run.json` (full, repetition 1)
- `bench/runs/20260823-212831-64a8/run.json` (progressive, repetition 1)
- `bench/runs/20260823-213338-a576/run.json` (full, repetition 2)
- `bench/runs/20260823-213349-cd2b/run.json` (progressive, repetition 2)

## Cheap-model screen

The exact initial screen used nine tasks, one repetition, coder-only aiOS, and
no scout/consultant/reviewer work.

| Model | Pass | Score | Total tokens | Cost | Time |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna | 8/9 | 85.6 | 325,449 | $0.043523 | 293.8 s |
| MiMo V2.5 | 8/9 | 79.9 | 602,098 | $0.033474 | 835.7 s |
| DeepSeek V4 Flash 0731 | 8/9 | 77.5 | 951,514 | $0.021453 | 670.6 s |
| Ox Alpha | 8/9 | 76.3 | 599,539 | $0.000000 | 943.1 s |
| Qwen3.7 Flash | 8/9 | 75.2 | 2,146,081 | $0.049942 | 622.3 s |

Luna was the quality/latency leader. DeepSeek remained the lowest-cost paid
default despite using more tokens. MiMo and Qwen were dominated on this screen.
Every model failed exactly one task, so these results support routing rather
than a claim that any model is universally reliable.

New candidates did not enter the recommended frontier. KAT and Ling finished
at 8/9 and 7/9 but cost more time/tokens; Hy3, Nex N2 Mini, Laguna S 2.1, and
Solar Pro 4 were stopped after the fixed campaign time boundary. Nex remains
experimental only; the partial lead-coder result does not prove worker quality.

## Same-task raw harness micro-comparison

The same Ox Alpha rounding task was run through four harnesses. aiOS and Kimi
used max reasoning; OMP and Hermes used high, so this is a useful execution
trace rather than a broad model-quality verdict.

| Harness | Result | Total tokens | Time | Tool calls | First edit |
| --- | ---: | ---: | ---: | ---: | ---: |
| aiOS | pass | 19,972 | 71.0 s | 4 | 54.1 s |
| Kimi Code | pass | 27,024 | 50.0 s | 4 | 30.0 s |
| OMP | pass | 48,492 | 79.6 s | 4 | 28.0 s |
| Hermes | fail | 121,772 | 340.5 s | unavailable | unavailable |

Kimi was fastest on this single micro-task; aiOS used the fewest tokens. The
Kimi adapter is BENCH-only. Its real OpenRouter key now remains behind a
localhost exact-model proxy with observed-soft spend enforcement and artifact
audits, but Kimi Bash still has host-user filesystem permissions. It is not an
OS sandbox.

## Implemented harness changes

- Progressive, capability-lazy tool schemas with an exact optional-tool enum
  and runtime authorization kept in lockstep.
- Same-coder completion audit only for dense structured requests, bounded to
  four audit rounds/eight tool calls, preserving original operator constraints.
- Per-turn provider-reported token circuit breakers, bounded rounds, and a
  truthful incomplete handoff. An exhausted token budget makes no extra model
  call; other stop paths get at most one no-tools, reasoning-off, 384-token,
  bounded-context close.
- Exact OpenRouter reasoning capability handling: none, toggle-only, or
  enumerated effort. `minimal`, `max`, and `ultra` are preserved exactly.
- Generation IDs persisted for all roles, enabling resolved model/provider,
  native token, and exact-cost lookup.
- Correct Hermes canonical prompt accounting: input + cache read + cache write.
- Kimi Code BENCH adapter with isolated per-job state, exact tool/profile
  verification, a parent-side OpenRouter proxy, and home/workspace secret audit.
- BENCH group preflight now fails the whole group when a selected aiOS model is
  disabled or invalid, instead of silently producing incomparable lanes.

## Primary-source design references

- OMP append-only context and compaction:
  https://github.com/can1357/oh-my-pi/blob/160ed439ac0df594347e7d7018b813a7ffdb5e81/packages/agent/src/append-only-context.ts
  and https://github.com/can1357/oh-my-pi/blob/main/docs/compaction.md
- Kimi progressive tool disclosure:
  https://github.com/MoonshotAI/kimi-code/blob/main/apps/kimi-code/CHANGELOG.md
- Hermes dual context compression and caching:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md
- OpenRouter reasoning controls:
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- OpenRouter generation accounting:
  https://openrouter.ai/docs/api/api-reference/generations/get-generation

## Verification and limits

- Focused release regression: 174 passed.
- New stop-path tests: 5 passed.
- Edited Python modules compile; UI JavaScript syntax checks pass.
- The relaunched WebView2 CODE window and managed localhost listeners were
  inspected live.
- This is materially better, not literally perfect. The hard telemetry task is
  stochastic across full lanes, observed-soft provider cost caps can overshoot
  by one in-flight request, raw Kimi lacks an OS filesystem sandbox, and legacy
  tests outside the focused release slice retain stale/config-dependent
  assumptions.
