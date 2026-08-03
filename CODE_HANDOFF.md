# aiOS CODE provider handoff

aiOS treats a CODE job as a logical session that can contain more than one
provider-native session. A handoff keeps the aiOS job id, cwd, event log, and
chat surface stable while it closes the source provider segment and starts a
fresh native session for the target provider.

## Why the bridge is provider-neutral

Codex, Claude Code, and Cursor can each resume their own native session ids,
but one provider cannot resume another provider's transcript id. aiOS therefore
does not translate or relabel a Claude session id as a Codex or Cursor id.
Instead it writes a versioned manifest under:

```
code_jobs/<job-id>/handoffs/<handoff-id>.json
```

The target provider receives that manifest in the first turn of a new native
session. The manifest includes:

- logical session id, title, and current working directory;
- source and target provider/model settings;
- original task brief and latest conversation summary;
- extracted constraints and decisions, plus any explicit metadata values;
- pending questions;
- changed file paths from normalized agent events and `git status`;
- bounded recent agent output and provider-neutral lifecycle events;
- the user's optional continuation instruction;
- explicit native-continuation limitations.

Raw command output, hidden reasoning, credentials, provider-specific tool state,
and the full native transcript are intentionally not copied into the bridge.
The target is instructed to inspect the shared working tree before editing.

## Lifecycle and API

`POST /api/code/jobs/<job-id>/handoff` (and its `/api/phone/...` alias) accepts:

```json
{
  "provider": "codex",
  "model": "gpt-5.6-sol",
  "reasoning": "high",
  "fast": true,
  "instruction": "Continue the current task and finish the tests."
}
```

The target provider must differ from the current provider and its exact model,
reasoning/intelligence, and fast-mode selection must pass the same live
capability validation used when creating a job. If the source is active, aiOS
interrupts it and waits for its turn owner to unwind before changing provider
metadata. It then:

1. writes the manifest;
2. ends the current `provider_sessions` segment;
3. appends a new target segment with an empty native session id;
4. emits a `provider_switch` chat event;
5. queues the bridge prompt for the target;
6. records the target's native id when the provider initializes.

Normal API consumers continue using the same job id. The current provider,
model, and latest native id remain available in the existing top-level job
fields, while `provider_sessions` and `handoffs` retain the chain.

## Voice controls and UI

The desktop voice agent exposes `code_handoff`; the Android realtime voice
agent exposes `pc_code_handoff`. Both require the exact target provider/model
settings and accept an optional continuation instruction. Web, desktop, and
Android chat surfaces render `provider_switch` as a dedicated handoff event and
show the new provider/model in session metadata.

## Limitations

- Cross-provider handoff always creates a new native target session. Native
  same-session continuation is impossible across providers.
- Hidden reasoning, provider memory, approvals, in-flight tool processes, and
  provider-only transcript details cannot be reconstructed.
- The context extractor is deterministic and bounded. It preserves the task,
  recent conversation, explicit metadata, and matching constraint/decision
  lines; it is not a semantic transcript clone.
- An active source turn is interrupted. Partial on-disk edits remain in the
  shared cwd and are included through working-tree paths, but incomplete
  provider-side operations cannot be resumed.
- Same-provider model changes continue to use the existing message API and
  native resume path; `handoff` is intentionally for switching providers.

Provider-native resume behavior is based on the local CLI capabilities and the
official references for [Codex app-server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/cli-usage), and
[Cursor CLI sessions](https://docs.cursor.com/en/cli/using).
