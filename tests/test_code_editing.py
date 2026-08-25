import re

import pytest

from code_editing import EditMatchError, ReplaceResult, content_revision, replace_text


def test_content_revision_is_compact_sha256() -> None:
    revision = content_revision("hello")

    assert len(revision) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", revision)
    assert revision == content_revision("hello")
    assert revision != content_revision("hello!")
    assert len(content_revision("hello", 12)) == 12


def test_exact_replacement_returns_revision_and_location() -> None:
    result = replace_text("alpha\nbeta\ngamma", "beta", "BETA")

    assert isinstance(result, ReplaceResult)
    assert result.content == "alpha\nBETA\ngamma"
    assert result.count == 1
    assert result.match_kind == "exact"
    assert result.confidence == 1.0
    assert result.start_lines == (2,)
    assert result.changed
    assert result.revision_before == content_revision("alpha\nbeta\ngamma")
    assert result.revision_after == content_revision(result.content)


def test_exact_ambiguity_fails_with_bounded_previews() -> None:
    content = "\n".join(f"item = {index % 2}" for index in range(20))

    with pytest.raises(EditMatchError) as raised:
        replace_text(content, "item = 1", "item = 2")

    error = raised.value
    assert error.reason == "ambiguous"
    assert error.occurrences == 10
    assert 1 <= len(error.previews) <= 5
    assert all(len(preview.splitlines()) <= 9 for preview in error.previews)


def test_replace_all_never_matches_its_own_replacement() -> None:
    result = replace_text("a a", "a", "aa", replace_all=True)

    assert result.content == "aa aa"
    assert result.count == 2
    assert result.start_lines == (1, 1)


def test_crlf_and_final_newline_are_preserved() -> None:
    content = "alpha\r\nbeta\r\n"
    result = replace_text(content, "beta", "BETA")

    assert result.content == "alpha\r\nBETA\r\n"
    assert "\n" not in result.content.replace("\r\n", "")


def test_lf_without_final_newline_is_not_changed_incidentally() -> None:
    result = replace_text("alpha\nbeta", "alpha", "ALPHA")

    assert result.content == "ALPHA\nbeta"
    assert not result.content.endswith("\n")


def test_unique_fuzzy_match_adapts_uniform_indentation() -> None:
    content = (
        "class Box:\n"
        "    def value(self):\n"
        "        return 1\n"
        "\n"
        "    def other(self):\n"
        "        return 9\n"
    )
    old_text = "def value(self):\n    return 1"
    new_text = "def value(self):\n    return 2"

    result = replace_text(content, old_text, new_text)

    assert result.match_kind == "fuzzy"
    assert result.count == 1
    assert result.start_lines == (2,)
    assert "    def value(self):\n        return 2" in result.content
    assert "    def other(self):\n        return 9" in result.content


def test_fuzzy_ambiguity_rejects_equally_good_candidates() -> None:
    target = "a" * 100
    candidate = "a" * 99 + "b"

    with pytest.raises(EditMatchError) as raised:
        replace_text(f"{candidate}\n{candidate}", target, "chosen", threshold=0.95)

    error = raised.value
    assert error.reason == "ambiguous"
    assert error.occurrences == 2
    assert error.confidence is not None
    assert error.confidence > 0.98


def test_dominant_fuzzy_candidate_wins_by_confidence_margin() -> None:
    target = "a" * 100
    best = "a" * 99 + "b"
    runner_up = "a" * 90 + "b" * 10

    result = replace_text(f"{best}\n{runner_up}", target, "chosen", threshold=0.89)

    assert result.content == f"chosen\n{runner_up}"
    assert result.match_kind == "fuzzy"
    assert result.confidence > 0.98


def test_fuzzy_replace_all_uses_immutable_source() -> None:
    target = "a" * 100
    candidate = "a" * 99 + "b"
    replacement = f"{candidate}\n{candidate}"

    result = replace_text(candidate, target, replacement, replace_all=True, threshold=0.98)

    assert result.content == replacement
    assert result.count == 1


def test_fuzzy_matching_has_a_large_file_work_cap() -> None:
    content = "x\n" * 20_001

    with pytest.raises(EditMatchError) as raised:
        replace_text(content, "not-present", "replacement")

    assert raised.value.reason == "fuzzy_limit"


def test_not_found_reports_closest_candidate_without_mutation() -> None:
    with pytest.raises(EditMatchError) as raised:
        replace_text("alpha\nbeta\ngamma", "completely unrelated", "replacement")

    error = raised.value
    assert error.reason == "not_found"
    assert error.confidence is not None
    assert len(error.previews) == 1


def test_empty_and_whitespace_only_targets_fail_closed() -> None:
    with pytest.raises(EditMatchError, match="must not be empty"):
        replace_text("abc", "", "x")

    with pytest.raises(EditMatchError) as raised:
        replace_text("abc", "   ", "x")
    assert raised.value.reason == "invalid_target"
