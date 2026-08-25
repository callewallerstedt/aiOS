"""Adaptive, provider-neutral policy primitives for the aiOS CODE harness.

The module intentionally has no project or third-party dependencies so task
routing and context budgeting can run before provider clients are initialized.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Final


DIRECT_CONTEXT_TOKENS: Final = 32_768
PLANNED_CONTEXT_TOKENS: Final = 65_536
DISTRIBUTED_CONTEXT_TOKENS: Final = 131_072
MIN_OUTPUT_RESERVE_TOKENS: Final = 16_384
OUTPUT_RESERVE_RATIO: Final = 0.15
# The fixed floor above is sized for cloud windows. On a local 32k model it
# would withhold half the context for a reply the model will never write, so
# the floor is itself capped at a share of the window.
MAX_OUTPUT_RESERVE_RATIO: Final = 0.25

_STRATEGY_OVERRIDES: Final = {
    "[direct]": "direct",
    "[planned]": "planned",
    "[distributed]": "distributed",
}

_DISTRIBUTED_PATTERNS: Final = (
    (re.compile(r"\b(?:deep(?:ly)?\s+research|research\s+(?:deeply|in[- ]depth))\b"), "deep research requested"),
    (re.compile(r"\bresearch\b.*\b(?:compare|versus|vs\.?|multiple|several)\b"), "multi-source research requested"),
    (re.compile(r"\bresearch\b.*\b(?:architecture|harness|refactor|overhaul)\b"), "research-led architecture change requested"),
    (re.compile(r"\b(?:overhaul|repo[- ]wide|repository[- ]wide|codebase[- ]wide)\b"), "repository-scale change requested"),
    (re.compile(r"\b(?:parallel|concurrently|independent workstreams?|agent teams?)\b"), "parallel independent work requested"),
    (re.compile(r"\b(?:entire|whole)\s+(?:repo(?:sitory)?|codebase|project)\b"), "whole-project scope requested"),
    (re.compile(r"\b(?:large|huge)\s+(?:task|change|migration|refactor|feature|implementation)\b"), "explicitly large task requested"),
    (re.compile(r"\b(?:migrate|rewrite|replace|update)\s+(?:all|every)\b"), "bulk migration requested"),
)

_PLANNED_PATTERNS: Final = (
    (re.compile(r"\b(?:multiple|several|many)\s+(?:files?|modules?|components?|packages?)\b"), "multiple coupled files indicated"),
    (re.compile(r"\b(?:architecture|architectural|design|refactor|migration|feature)\b"), "design work indicated"),
    (re.compile(r"\b(?:investigate|diagnose|uncertain|unknown|figure out|root cause)\b"), "exploration is required"),
    (re.compile(r"\b(?:end[- ]to[- ]end|cross[- ]cutting|backward compatibility)\b"), "cross-cutting behavior indicated"),
    (re.compile(r"\b(?:rename|replace)\b.{0,200}\beverywhere\b"), "cross-file symbol migration indicated"),
    (re.compile(r"\beverywhere\b.{0,100}\b(?:defined|called|referenced|used)\b"), "cross-file symbol migration indicated"),
    (re.compile(r"\ball\s+(?:call sites?|references?|occurrences?|usages?)\b"), "cross-file symbol migration indicated"),
)

# A named file makes scope bounded, not necessarily safe.  These tasks can hide
# cross-cutting invariants inside one implementation file, so they must keep the
# planner/explicit-verification path unless the operator explicitly overrides
# the strategy.
RISKY_TASK_PATTERN: Final = re.compile(
    r"\b(?:auth(?:entication|orization)?|permissions?|security|protocol|"
    r"state[-_ ]machine|streaming|concurr(?:ent|ency)|thread[-_ ]safe|"
    r"data[-_ ]race|race[-_ ]condition|"
    r"transactions?|rollback|migrations?|parsers?|decoders?|serializ(?:e|er|ation)|"
    r"backward(?:s)?[-_ ]compatib(?:ility|le))\b"
)

_DIRECT_ACTION_PATTERN: Final = re.compile(
    r"\b(?:fix|change|update|replace|rename|remove|add|correct|make|darken|lighten|tweak|"
    r"complete|implement|adjust|align|center|decrease|display|format|hide|increase|move|"
    r"reduce|reorder|set|show|sort|should|toggle)\b"
)
_FILE_REFERENCE_PATTERN: Final = re.compile(
    r"(?<![A-Za-z0-9._-])(?:[A-Za-z]:[\\/])?(?:[A-Za-z0-9_.-]+[\\/])*"
    r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}(?![A-Za-z0-9._-])"
)
_DIRECT_SCOPE_PATTERNS: Final = (
    (re.compile(r"\b(?:typo|spelling|copy|label|log line|one[- ]line|single line|small edit|localized)\b"), "small localized edit indicated"),
    (re.compile(r"\b(?:colou?r|button|css|style|spacing|padding|margin|opacity|font|icon|heading|text)\b"), "localized interface styling indicated"),
    (_FILE_REFERENCE_PATTERN, "specific file identified"),
)

_FOLLOWUP_OPERATION_PATTERN: Final = re.compile(
    r"\b(?:run|launch|start|stop|restart|open|close|switch|select|enable|disable|"
    r"try|retry|show|play|pause|connect|disconnect)\b.{0,80}"
    r"\b(?:it|that|this|them|again|now|right now)\b"
    r"|\b(?:now|right now)\b.{0,80}\b(?:run|launch|start|stop|restart|open|close|switch)\b"
)
_FOLLOWUP_CHANGE_PATTERN: Final = re.compile(
    r"\b(?:add|build|change|code|create|delete|edit|feature|fix|implement|migrate|"
    r"modify|patch|refactor|remove|rewrite|script|update)\b"
)
_FOLLOWUP_CONTEXT_PATTERN: Final = re.compile(
    r"^(?:and|also|actually|instead|now|then|make sure|do not|don't)\b|"
    r"\b(?:it|that|this|same one)\b"
)
_HIGH_IMPACT_OPERATION_PATTERN: Final = re.compile(
    r"\b(?:deploy|production|publish|release|drop\s+(?:the\s+)?(?:database|table)|"
    r"delete\s+(?:all|every)|factory\s+reset|format\s+(?:the\s+)?drive)\b"
)


@dataclass(frozen=True, slots=True)
class TaskStrategy:
    """Execution shape selected for one user task."""

    name: str
    reasons: list[str]
    score: int
    use_scout: bool
    use_planner: bool
    allow_subagents: bool
    working_context_tokens: int


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Verified-or-conservative behavior switches for a model family."""

    model: str
    edit_mode: str
    tool_schema_mode: str
    context_mode: str
    context_tokens: int
    conservative: bool


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Token allocation for one context window."""

    window_tokens: int
    working_tokens: int
    output_reserve_tokens: int


def _strategy(
    name: str,
    reasons: list[str],
    *,
    score: int | None = None,
    use_scout: bool | None = None,
    use_planner: bool | None = None,
) -> TaskStrategy:
    if name == "direct":
        return TaskStrategy(
            name=name,
            reasons=reasons,
            score=20 if score is None else score,
            use_scout=False,
            use_planner=False,
            allow_subagents=False,
            working_context_tokens=DIRECT_CONTEXT_TOKENS,
        )
    if name == "distributed":
        return TaskStrategy(
            name=name,
            reasons=reasons,
            score=90 if score is None else score,
            use_scout=True,
            use_planner=True,
            allow_subagents=True,
            working_context_tokens=DISTRIBUTED_CONTEXT_TOKENS,
        )
    return TaskStrategy(
        name="planned",
        reasons=reasons,
        score=55 if score is None else score,
        use_scout=True if use_scout is None else use_scout,
        use_planner=True if use_planner is None else use_planner,
        allow_subagents=False,
        working_context_tokens=PLANNED_CONTEXT_TOKENS,
    )


def classify_task(text: str, *, continuation: bool = False) -> TaskStrategy:
    """Classify a request without spending a model call.

    The classifier is deliberately conservative: ambiguous work is planned,
    while distributed execution requires an explicit scale, research, or
    parallelism signal. Bracketed strategy tags always win.
    """

    normalized = " ".join(str(text or "").lower().split())

    for marker, name in _STRATEGY_OVERRIDES.items():
        if marker in normalized:
            return _strategy(name, [f"explicit {marker} override"], score={"direct": 10, "planned": 50, "distributed": 100}[name])

    named_files = {
        match.group(0).replace("\\", "/").casefold()
        for match in _FILE_REFERENCE_PATTERN.finditer(normalized)
    }
    supporting_suffixes = (".md", ".rst", ".txt")
    implementation_files = {
        path for path in named_files
        if not path.endswith(supporting_suffixes)
        and "/tests/" not in f"/{path}"
        and "/.docs/" not in f"/{path}"
        and not path.rsplit("/", 1)[-1].startswith("test_")
        and not path.rsplit("/", 1)[-1].endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    }

    distributed_reasons = list(dict.fromkeys(
        reason for pattern, reason in _DISTRIBUTED_PATTERNS if pattern.search(normalized)
    ))
    if distributed_reasons:
        return _strategy(
            "distributed",
            distributed_reasons,
            score=min(100, 86 + (4 * len(distributed_reasons))),
        )

    if RISKY_TASK_PATTERN.search(normalized):
        return _strategy(
            "planned",
            ["risk-sensitive behavior requires explicit verification"],
            score=64,
            use_scout=not (0 < len(named_files) <= 2),
        )

    # Once a persistent session already owns the repository map and provider
    # history, a pure "do it now" instruction is execution, not a fresh design
    # task.  Keep high-impact or code-changing follow-ups on the normal
    # classifier; only context-dependent operational commands take this path.
    if (
        continuation
        and _FOLLOWUP_OPERATION_PATTERN.search(normalized)
        and not _FOLLOWUP_CHANGE_PATTERN.search(normalized)
        and not _HIGH_IMPACT_OPERATION_PATTERN.search(normalized)
    ):
        return _strategy(
            "direct",
            ["existing session context localizes this operational follow-up"],
            score=12,
        )

    if (
        continuation
        and len(normalized) <= 240
        and _FOLLOWUP_CONTEXT_PATTERN.search(normalized)
        and _DIRECT_ACTION_PATTERN.search(normalized)
        and not _HIGH_IMPACT_OPERATION_PATTERN.search(normalized)
        and not any(pattern.search(normalized) for pattern, _reason in _PLANNED_PATTERNS)
    ):
        return _strategy(
            "direct",
            ["existing session context localizes this short follow-up"],
            score=14,
        )

    planned_reasons = list(dict.fromkeys(
        reason for pattern, reason in _PLANNED_PATTERNS if pattern.search(normalized)
    ))
    if planned_reasons:
        return _strategy(
            "planned",
            planned_reasons,
            score=min(80, 52 + (5 * len(planned_reasons))),
            # A deterministic project map is enough when the operator already
            # named the files. Preserve the planner's design judgement without
            # paying a second model to rediscover known paths.
            use_scout=not (0 < len(named_files) <= 2),
        )

    direct_reasons = list(dict.fromkeys(
        reason for pattern, reason in _DIRECT_SCOPE_PATTERNS if pattern.search(normalized)
    ))
    # Detailed acceptance criteria are useful, not evidence of broad scope. A
    # brief that names one or two exact files can safely skip Scout/Planner even
    # when its prose exceeds the old 320-character shortcut. Scale signals have
    # already returned through the planned/distributed branches above.
    bounded_named_scope = 0 < len(named_files) <= 2
    bounded_implementation_scope = (
        0 < len(implementation_files) <= 2 and len(named_files) <= 6
    )
    if (
        _DIRECT_ACTION_PATTERN.search(normalized)
        and direct_reasons
        and (len(normalized) <= 320 or bounded_named_scope or bounded_implementation_scope)
    ):
        return _strategy(
            "direct",
            direct_reasons,
            score=max(10, 24 - (3 * len(direct_reasons))),
        )

    if len(normalized) <= 420 and _DIRECT_ACTION_PATTERN.search(normalized):
        return _strategy(
            "planned",
            ["a short vague change needs scoped discovery before coding"],
            score=44,
            use_scout=True,
            use_planner=False,
        )

    return _strategy("planned", ["scope is not clearly localized; planning is the safe default"])


def named_file_references(text: str, limit: int = 8) -> list[str]:
    """Return bounded, de-duplicated file paths explicitly present in a request."""

    maximum = max(0, min(int(limit or 0), 32))
    if maximum == 0:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _FILE_REFERENCE_PATTERN.finditer(str(text or "")):
        path = match.group(0).replace("\\", "/")
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
        if len(found) >= maximum:
            break
    return found


def resolve_model_profile(model: str, context_tokens: int = 0) -> ModelProfile:
    """Resolve model-family behavior without inventing unsupported API fields."""

    model_name = str(model or "").strip()
    normalized = model_name.lower()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    known_context = max(0, int(context_tokens or 0))

    is_deepseek_v4_flash = "deepseek" in compact and "v4" in compact and "flash" in compact
    is_mimo = "mimo" in compact
    is_kimi = "kimi" in compact
    is_step_37 = "step37" in compact
    is_gemini = "gemini" in compact
    is_deepseek = "deepseek" in compact
    # Real Ollama tags are `qwen3:14b` or `hf.co/unsloth/...:UD-Q2_K_XL`, and
    # none of them start with the prefixes this used to look for -- so every
    # locally served model was classified as an unknown cloud model and lost the
    # handling meant for it. A hosted id always carries a vendor slash and no
    # colon outside it; a bare `name:tag` does not.
    head = normalized.split("/", 1)[0]
    is_local = (
        normalized.startswith(("local/", "ollama/", "lmstudio/", "hf.co/", "huggingface.co/"))
        or "localhost" in normalized
        or "127.0.0.1" in normalized
        or (":" in head and "/" not in head)
    )

    # A quantized local model reproduces `old_text` imperfectly far more often
    # than a hosted one -- a stray space costs it the edit and buys another
    # read/retry cycle it can barely afford at 32k. Give it the same forgiving
    # matcher the replacement-heavy families already use.
    robust_replace = is_deepseek_v4_flash or is_mimo or is_kimi or is_step_37 or is_local
    recognized = robust_replace or is_gemini or is_deepseek or is_local

    return ModelProfile(
        model=model_name,
        edit_mode="robust_replace" if robust_replace else "exact_replace",
        tool_schema_mode="inline_tool_descriptors" if is_gemini else "native_tool_schemas",
        context_mode="append_only_context" if is_deepseek or is_local else "bounded_context",
        context_tokens=known_context,
        conservative=not recognized,
    )


def estimate_tokens(text: str) -> int:
    """Return a conservative dependency-free token estimate.

    It is not a tokenizer replacement. The estimate intentionally favors a
    little unused context over an unexpected overflow, especially for source
    code, punctuation-heavy text, and non-ASCII content.
    """

    value = str(text or "")
    if not value:
        return 0

    utf8_bytes = len(value.encode("utf-8"))
    lexical_units = len(re.findall(r"\w+|[^\w\s]", value, flags=re.UNICODE))
    return max(1, math.ceil(utf8_bytes / 4), math.ceil(lexical_units * 1.15))


def context_budget(strategy: TaskStrategy | str, context_tokens: int) -> ContextBudget:
    """Allocate the provider's real context window while preserving output room.

    Strategy-sized values are conservative fallbacks only when provider
    metadata does not expose a window. Once the actual model window is known,
    task routing must not introduce a second artificial context ceiling: doing
    so made long tool turns compact at 32k/65k even on million-token models.
    For known windows, 15 percent or 16,384 tokens (whichever is larger) is
    withheld for the model's next response, but never more than a quarter of
    the window: on a 32k local model the flat floor alone was taking half of
    it, which left too little history to finish reading a single file.
    """

    selected = strategy if isinstance(strategy, TaskStrategy) else _strategy(str(strategy).strip().lower(), ["context budget lookup"])
    window = max(0, int(context_tokens or 0))
    if window == 0:
        return ContextBudget(
            window_tokens=0,
            working_tokens=selected.working_context_tokens,
            output_reserve_tokens=0,
        )

    reserve = min(
        window,
        max(MIN_OUTPUT_RESERVE_TOKENS, math.ceil(window * OUTPUT_RESERVE_RATIO)),
        max(1, math.ceil(window * MAX_OUTPUT_RESERVE_RATIO)),
    )
    working = max(0, window - reserve)
    return ContextBudget(
        window_tokens=window,
        working_tokens=working,
        output_reserve_tokens=reserve,
    )


__all__ = [
    "ContextBudget",
    "ModelProfile",
    "TaskStrategy",
    "classify_task",
    "RISKY_TASK_PATTERN",
    "context_budget",
    "estimate_tokens",
    "named_file_references",
    "resolve_model_profile",
]
