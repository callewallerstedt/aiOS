"""Safe, token-efficient text replacement primitives for the CODE harness.

The public API deliberately separates matching from filesystem I/O.  Callers
can inspect the returned revision and content before deciding how to persist it.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import reduce
from math import gcd
from typing import Literal, Sequence


_REVISION_LENGTH = 16
_MIN_REVISION_LENGTH = 12
_MAX_PREVIEWS = 5
_PREVIEW_CONTEXT_LINES = 2
_PREVIEW_MATCH_LINES = 5
_PREVIEW_MAX_LINES = 9
_PREVIEW_MAX_COLUMNS = 120

_FALLBACK_THRESHOLD = 0.80
_DOMINANT_FUZZY_MIN_CONFIDENCE = 0.97
_DOMINANT_FUZZY_MARGIN = 0.08

# Fuzzy matching is intentionally bounded. Exact matching is still available
# for larger inputs and is linear in the source size.
_MAX_FUZZY_FILE_CHARS = 2_000_000
_MAX_FUZZY_FILE_LINES = 20_000
_MAX_FUZZY_TARGET_CHARS = 50_000
_MAX_FUZZY_TARGET_LINES = 500
_MAX_FUZZY_WINDOWS = 20_000
_MAX_FUZZY_LINE_COMPARISONS = 100_000
_MAX_FUZZY_CHARACTER_WORK = 20_000_000
_MAX_FUZZY_REPLACEMENTS = 256


MatchKind = Literal["exact", "fuzzy"]


def content_revision(content: str, length: int = _REVISION_LENGTH) -> str:
    """Return a short, content-derived SHA-256 revision.

    Twelve hexadecimal characters is the minimum accepted size. The default is
    sixteen, which remains compact in prompts while avoiding four-hex-style
    snapshot collisions.
    """

    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(length, int) or isinstance(length, bool):
        raise TypeError("length must be an integer")
    if length < _MIN_REVISION_LENGTH or length > 64:
        raise ValueError("length must be between 12 and 64 hexadecimal characters")
    digest = hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()
    return digest[:length]


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Result of a successful in-memory replacement."""

    content: str
    count: int
    revision_before: str
    revision_after: str
    match_kind: MatchKind
    confidence: float
    start_lines: tuple[int, ...]

    @property
    def changed(self) -> bool:
        return self.revision_before != self.revision_after


class EditMatchError(ValueError):
    """Raised when a replacement cannot be located safely and unambiguously."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "match_error",
        occurrences: int = 0,
        confidence: float | None = None,
        previews: Sequence[str] = (),
    ) -> None:
        bounded_previews = tuple(previews[:_MAX_PREVIEWS])
        super().__init__(message)
        self.reason = reason
        self.occurrences = max(0, int(occurrences))
        self.confidence = confidence
        self.previews = bounded_previews


@dataclass(frozen=True, slots=True)
class _Candidate:
    start_index: int
    end_index: int
    start_line: int
    line_index: int
    line_count: int
    actual_text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class _FuzzyScan:
    best: _Candidate | None
    second_best_score: float
    above_threshold_count: int
    preview_candidates: tuple[_Candidate, ...]


@dataclass(slots=True)
class _FuzzyBudget:
    line_comparisons: int = _MAX_FUZZY_LINE_COMPARISONS
    character_work: int = _MAX_FUZZY_CHARACTER_WORK

    def consume(self, left: Sequence[str], right: Sequence[str]) -> None:
        comparisons = len(left)
        character_work = sum(len(a) + len(b) for a, b in zip(left, right))
        if comparisons > self.line_comparisons or character_work > self.character_work:
            raise _FuzzyLimitError("fuzzy comparison work budget exceeded")
        self.line_comparisons -= comparisons
        self.character_work -= character_work


class _FuzzyLimitError(RuntimeError):
    pass


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    bare_lf = text.count("\n") - crlf
    return "\r\n" if crlf > bare_lf else "\n"


def _restore_line_endings(text: str, line_ending: str) -> str:
    return text if line_ending == "\n" else text.replace("\n", "\r\n")


def _find_exact_occurrences(content: str, target: str) -> list[int]:
    indices: list[int] = []
    cursor = 0
    while cursor <= len(content) - len(target):
        index = content.find(target, cursor)
        if index < 0:
            break
        indices.append(index)
        cursor = index + len(target)
    return indices


def _line_number_at(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _preview_for_line(lines: Sequence[str], line_index: int, match_line_count: int) -> str:
    start = max(0, line_index - _PREVIEW_CONTEXT_LINES)
    shown_match_lines = max(1, min(match_line_count, _PREVIEW_MATCH_LINES))
    end = min(len(lines), line_index + shown_match_lines + _PREVIEW_CONTEXT_LINES)
    if end - start > _PREVIEW_MAX_LINES:
        end = start + _PREVIEW_MAX_LINES

    rendered: list[str] = []
    match_end = line_index + max(1, match_line_count)
    for offset in range(start, end):
        raw = lines[offset]
        clipped = raw if len(raw) <= _PREVIEW_MAX_COLUMNS else f"{raw[:_PREVIEW_MAX_COLUMNS]}..."
        marker = ">" if line_index <= offset < match_end else " "
        rendered.append(f"{marker} {offset + 1:>6} | {clipped}")
    if line_index + match_line_count > end:
        rendered.append("  ...")
    return "\n".join(rendered)


def _exact_ambiguity_error(content: str, target: str, indices: Sequence[int]) -> EditMatchError:
    lines = content.split("\n")
    line_count = len(target.split("\n"))
    previews = [
        _preview_for_line(lines, _line_number_at(content, index) - 1, line_count)
        for index in indices[:_MAX_PREVIEWS]
    ]
    suffix = f" Showing the first {_MAX_PREVIEWS}." if len(indices) > _MAX_PREVIEWS else ""
    message = (
        f"Exact old_text is ambiguous: found {len(indices)} non-overlapping occurrences."
        f" Add surrounding context to identify one occurrence.{suffix}"
    )
    return EditMatchError(
        message,
        reason="ambiguous",
        occurrences=len(indices),
        confidence=1.0,
        previews=previews,
    )


_UNICODE_TRANSLATION = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "`": "'",
        "\u00b4": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def _normalize_for_fuzzy(line: str) -> str:
    normalized = unicodedata.normalize("NFC", line.strip()).translate(_UNICODE_TRANSLATION)
    return re.sub(r"[ \t]+", " ", normalized)


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _relative_indent_depths(lines: Sequence[str]) -> list[int]:
    indents = [len(_leading_whitespace(line)) for line in lines]
    non_empty = [indents[i] for i, line in enumerate(lines) if line.strip()]
    if not non_empty:
        return [0] * len(lines)
    minimum = min(non_empty)
    steps = [indent - minimum for indent in non_empty if indent > minimum]
    unit = min(steps) if steps else 1
    return [
        0 if not line.strip() else round((indents[i] - minimum) / unit)
        for i, line in enumerate(lines)
    ]


def _normalize_lines(lines: Sequence[str], include_depth: bool) -> list[str]:
    depths = _relative_indent_depths(lines) if include_depth else [0] * len(lines)
    return [f"{depths[i]}|{_normalize_for_fuzzy(line)}" for i, line in enumerate(lines)]


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _line_offsets(lines: Sequence[str]) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    for index, line in enumerate(lines):
        offsets.append(cursor)
        cursor += len(line)
        if index < len(lines) - 1:
            cursor += 1
    return offsets


def _overlaps(start: int, end: int, excluded: Sequence[tuple[int, int]]) -> bool:
    return any(start < excluded_end and end > excluded_start for excluded_start, excluded_end in excluded)


def _validate_fuzzy_size(content: str, target: str, content_lines: Sequence[str], target_lines: Sequence[str]) -> None:
    windows = max(0, len(content_lines) - len(target_lines) + 1)
    if len(content) > _MAX_FUZZY_FILE_CHARS:
        detail = f"file has {len(content):,} characters (limit {_MAX_FUZZY_FILE_CHARS:,})"
    elif len(content_lines) > _MAX_FUZZY_FILE_LINES:
        detail = f"file has {len(content_lines):,} lines (limit {_MAX_FUZZY_FILE_LINES:,})"
    elif len(target) > _MAX_FUZZY_TARGET_CHARS:
        detail = f"old_text has {len(target):,} characters (limit {_MAX_FUZZY_TARGET_CHARS:,})"
    elif len(target_lines) > _MAX_FUZZY_TARGET_LINES:
        detail = f"old_text has {len(target_lines):,} lines (limit {_MAX_FUZZY_TARGET_LINES:,})"
    elif windows > _MAX_FUZZY_WINDOWS:
        detail = f"search has {windows:,} candidate windows (limit {_MAX_FUZZY_WINDOWS:,})"
    else:
        return
    raise EditMatchError(
        f"Fuzzy matching skipped because {detail}. Use an exact excerpt or a narrower target.",
        reason="fuzzy_limit",
    )


def _scan_fuzzy(
    content_lines: Sequence[str],
    target_lines: Sequence[str],
    offsets: Sequence[int],
    threshold: float,
    *,
    include_depth: bool,
    excluded: Sequence[tuple[int, int]],
    budget: _FuzzyBudget,
) -> _FuzzyScan:
    target_normalized = _normalize_lines(target_lines, include_depth)
    best: _Candidate | None = None
    second_best_score = -1.0
    above_threshold_count = 0
    preview_candidates: list[_Candidate] = []
    target_line_count = len(target_lines)

    for line_index in range(0, len(content_lines) - target_line_count + 1):
        window = content_lines[line_index : line_index + target_line_count]
        actual_text = "\n".join(window)
        start_index = offsets[line_index]
        end_index = start_index + len(actual_text)
        if not actual_text or _overlaps(start_index, end_index, excluded):
            continue

        window_normalized = _normalize_lines(window, include_depth)
        budget.consume(target_normalized, window_normalized)
        confidence = sum(
            _similarity(target_line, window_line)
            for target_line, window_line in zip(target_normalized, window_normalized)
        ) / target_line_count
        candidate = _Candidate(
            start_index=start_index,
            end_index=end_index,
            start_line=line_index + 1,
            line_index=line_index,
            line_count=target_line_count,
            actual_text=actual_text,
            confidence=confidence,
        )

        if best is None or confidence > best.confidence:
            second_best_score = best.confidence if best is not None else second_best_score
            best = candidate
        elif confidence > second_best_score:
            second_best_score = confidence

        if confidence >= threshold:
            above_threshold_count += 1
            if len(preview_candidates) < _MAX_PREVIEWS:
                preview_candidates.append(candidate)

    return _FuzzyScan(
        best=best,
        second_best_score=max(0.0, second_best_score),
        above_threshold_count=above_threshold_count,
        preview_candidates=tuple(preview_candidates),
    )


def _choose_fuzzy_candidate(
    content_lines: Sequence[str],
    target_lines: Sequence[str],
    offsets: Sequence[int],
    threshold: float,
    *,
    excluded: Sequence[tuple[int, int]],
    budget: _FuzzyBudget,
) -> tuple[_Candidate | None, _Candidate | None]:
    scan = _scan_fuzzy(
        content_lines,
        target_lines,
        offsets,
        threshold,
        include_depth=True,
        excluded=excluded,
        budget=budget,
    )
    if scan.best and _FALLBACK_THRESHOLD <= scan.best.confidence < threshold:
        without_depth = _scan_fuzzy(
            content_lines,
            target_lines,
            offsets,
            threshold,
            include_depth=False,
            excluded=excluded,
            budget=budget,
        )
        if without_depth.best and without_depth.best.confidence > scan.best.confidence:
            scan = without_depth

    if scan.above_threshold_count == 0:
        return None, scan.best
    if scan.above_threshold_count == 1:
        # The sole above-threshold match is necessarily the global best.
        return scan.best, scan.best
    if (
        scan.best is not None
        and scan.best.confidence >= _DOMINANT_FUZZY_MIN_CONFIDENCE
        and scan.best.confidence - scan.second_best_score >= _DOMINANT_FUZZY_MARGIN
    ):
        return scan.best, scan.best

    previews = [
        _preview_for_line(content_lines, candidate.line_index, candidate.line_count)
        for candidate in scan.preview_candidates
    ]
    confidence = scan.best.confidence if scan.best else None
    raise EditMatchError(
        (
            f"Fuzzy old_text is ambiguous: found {scan.above_threshold_count} candidates at or above "
            f"the {threshold:.3f} threshold. Add surrounding context."
        ),
        reason="ambiguous",
        occurrences=scan.above_threshold_count,
        confidence=confidence,
        previews=previews,
    )


@dataclass(frozen=True, slots=True)
class _IndentProfile:
    lines: tuple[str, ...]
    prefixes: tuple[str, ...]
    char: str | None
    mixed: bool
    non_empty_count: int
    unit: int


def _indent_profile(text: str) -> _IndentProfile:
    lines = tuple(text.split("\n"))
    prefixes = tuple(_leading_whitespace(line) for line in lines)
    non_empty_prefixes = [prefixes[i] for i, line in enumerate(lines) if line.strip()]
    used = {character for prefix in non_empty_prefixes for character in prefix}
    mixed = len(used) > 1
    char = next(iter(used)) if len(used) == 1 else None
    widths = [len(prefix) for prefix in non_empty_prefixes if prefix]
    unit = reduce(gcd, widths) if widths else 0
    return _IndentProfile(
        lines=lines,
        prefixes=prefixes,
        char=char,
        mixed=mixed,
        non_empty_count=len(non_empty_prefixes),
        unit=unit,
    )


def _is_indentation_only_rewrite(old_text: str, new_text: str) -> bool:
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    return len(old_lines) == len(new_lines) and all(
        old_line.strip() == new_line.strip() for old_line, new_line in zip(old_lines, new_lines)
    )


def _uniform_indent_delta(old: _IndentProfile, actual: _IndentProfile) -> int | None:
    deltas = [
        len(actual.prefixes[index]) - len(old.prefixes[index])
        for index in range(min(len(old.lines), len(actual.lines)))
        if old.lines[index].strip() and actual.lines[index].strip()
    ]
    if not deltas or any(delta != deltas[0] for delta in deltas[1:]):
        return None
    return deltas[0]


def _convert_leading_tabs_to_spaces(text: str, spaces_per_tab: int) -> str:
    converted: list[str] = []
    for line in text.split("\n"):
        prefix = _leading_whitespace(line)
        tab_count = len(prefix) if prefix and set(prefix) == {"\t"} else 0
        converted.append(" " * (tab_count * spaces_per_tab) + line[len(prefix) :] if tab_count else line)
    return "\n".join(converted)


def _maybe_convert_tabs(old: _IndentProfile, actual: _IndentProfile, new: _IndentProfile, new_text: str) -> str | None:
    if old.char != "\t" or actual.char != " " or new.char != " " and new.char != "\t":
        return None
    ratios: list[int] = []
    for index in range(min(len(old.lines), len(actual.lines))):
        if not old.lines[index].strip() or not actual.lines[index].strip():
            continue
        old_width = len(old.prefixes[index])
        actual_width = len(actual.prefixes[index])
        if old_width == 0:
            continue
        if actual_width % old_width:
            return None
        ratios.append(actual_width // old_width)
    if not ratios or ratios[0] <= 0 or any(ratio != ratios[0] for ratio in ratios[1:]):
        return None
    if new.char == " ":
        return new_text
    return _convert_leading_tabs_to_spaces(new_text, ratios[0])


def _adjust_indentation(old_text: str, actual_text: str, new_text: str) -> str:
    if old_text == actual_text or _is_indentation_only_rewrite(old_text, new_text):
        return new_text

    old = _indent_profile(old_text)
    actual = _indent_profile(actual_text)
    new = _indent_profile(new_text)
    if not old.non_empty_count or not actual.non_empty_count or not new.non_empty_count:
        return new_text
    if old.mixed or actual.mixed or new.mixed:
        return new_text

    if old.char and actual.char and old.char != actual.char:
        converted = _maybe_convert_tabs(old, actual, new, new_text)
        return converted if converted is not None else new_text

    delta = _uniform_indent_delta(old, actual)
    if delta is None or delta == 0:
        return new_text
    if new.char and actual.char and new.char != actual.char:
        return new_text

    indent_char = actual.char or old.char or " "
    adjusted: list[str] = []
    for line in new_text.split("\n"):
        if not line.strip():
            adjusted.append(line)
        elif delta > 0:
            adjusted.append(indent_char * delta + line)
        else:
            remove = min(-delta, len(_leading_whitespace(line)))
            adjusted.append(line[remove:])
    return "\n".join(adjusted)


def _apply_replacements(content: str, replacements: Sequence[tuple[int, int, str]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements, key=lambda item: item[0]):
        if start < cursor:
            raise EditMatchError("Selected replacement ranges overlap.", reason="ambiguous")
        parts.append(content[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts)


def _result(
    original_content: str,
    normalized_output: str,
    line_ending: str,
    *,
    count: int,
    match_kind: MatchKind,
    confidence: float,
    start_lines: Sequence[int],
) -> ReplaceResult:
    output = _restore_line_endings(normalized_output, line_ending)
    return ReplaceResult(
        content=output,
        count=count,
        revision_before=content_revision(original_content),
        revision_after=content_revision(output),
        match_kind=match_kind,
        confidence=confidence,
        start_lines=tuple(start_lines),
    )


def replace_text(
    content: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    fuzzy: bool = True,
    threshold: float = 0.95,
) -> ReplaceResult:
    """Replace text without silently guessing at ambiguous locations.

    Exact matching always runs first. A single replacement rejects duplicate
    exact occurrences. ``replace_all`` replaces every non-overlapping exact
    occurrence against the immutable source. If no exact occurrence exists,
    optional fuzzy matching searches same-line-count windows and accepts only a
    unique high-confidence candidate or a clearly dominant candidate.
    """

    if not all(isinstance(value, str) for value in (content, old_text, new_text)):
        raise TypeError("content, old_text, and new_text must be strings")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold):
        raise ValueError("threshold must be a finite number between 0 and 1")
    threshold = float(threshold)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")

    line_ending = _detect_line_ending(content)
    normalized_content = _normalize_newlines(content)
    normalized_old = _normalize_newlines(old_text)
    normalized_new = _normalize_newlines(new_text)
    if not normalized_old:
        raise EditMatchError("old_text must not be empty.", reason="invalid_target")

    exact_indices = _find_exact_occurrences(normalized_content, normalized_old)
    if exact_indices:
        if not replace_all and len(exact_indices) > 1:
            raise _exact_ambiguity_error(normalized_content, normalized_old, exact_indices)
        selected = exact_indices if replace_all else exact_indices[:1]
        replacements = [
            (index, index + len(normalized_old), normalized_new)
            for index in selected
        ]
        output = _apply_replacements(normalized_content, replacements)
        return _result(
            content,
            output,
            line_ending,
            count=len(selected),
            match_kind="exact",
            confidence=1.0,
            start_lines=[_line_number_at(normalized_content, index) for index in selected],
        )

    if not fuzzy:
        raise EditMatchError("old_text was not found exactly and fuzzy matching is disabled.", reason="not_found")
    if not normalized_old.strip():
        raise EditMatchError(
            "Whitespace-only old_text cannot be matched fuzzily; provide an exact excerpt with context.",
            reason="invalid_target",
        )

    content_lines = normalized_content.split("\n")
    target_lines = normalized_old.split("\n")
    if len(target_lines) > len(content_lines):
        raise EditMatchError("old_text has more lines than the content.", reason="not_found")
    _validate_fuzzy_size(normalized_content, normalized_old, content_lines, target_lines)

    offsets = _line_offsets(content_lines)
    budget = _FuzzyBudget()
    selected_candidates: list[_Candidate] = []
    replacements: list[tuple[int, int, str]] = []
    closest: _Candidate | None = None

    try:
        while True:
            excluded = [(candidate.start_index, candidate.end_index) for candidate in selected_candidates]
            candidate, current_closest = _choose_fuzzy_candidate(
                content_lines,
                target_lines,
                offsets,
                threshold,
                excluded=excluded,
                budget=budget,
            )
            if current_closest is not None and (closest is None or current_closest.confidence > closest.confidence):
                closest = current_closest
            if candidate is None:
                break

            adjusted_new = _adjust_indentation(normalized_old, candidate.actual_text, normalized_new)
            selected_candidates.append(candidate)
            replacements.append((candidate.start_index, candidate.end_index, adjusted_new))
            if not replace_all:
                break
            if len(selected_candidates) >= _MAX_FUZZY_REPLACEMENTS:
                raise _FuzzyLimitError(f"fuzzy replacement count exceeded {_MAX_FUZZY_REPLACEMENTS}")
    except _FuzzyLimitError as error:
        raise EditMatchError(
            f"Fuzzy matching stopped safely: {error}. Use a more exact or narrower old_text excerpt.",
            reason="fuzzy_limit",
        ) from error

    if not selected_candidates:
        previews: list[str] = []
        confidence = None
        detail = ""
        if closest is not None:
            confidence = closest.confidence
            previews.append(_preview_for_line(content_lines, closest.line_index, closest.line_count))
            detail = f" Closest same-line window scored {confidence:.3f}."
        raise EditMatchError(
            f"old_text was not found at or above the {threshold:.3f} fuzzy threshold.{detail}",
            reason="not_found",
            confidence=confidence,
            previews=previews,
        )

    output = _apply_replacements(normalized_content, replacements)
    return _result(
        content,
        output,
        line_ending,
        count=len(selected_candidates),
        match_kind="fuzzy",
        confidence=min(candidate.confidence for candidate in selected_candidates),
        start_lines=[candidate.start_line for candidate in selected_candidates],
    )


__all__ = ["EditMatchError", "ReplaceResult", "content_revision", "replace_text"]
