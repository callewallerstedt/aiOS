"""The benchmark fixtures have to be honest in both directions.

A benchmark task is worthless if the untouched repository already passes, and
actively misleading if a correct solution fails. Every fixture is therefore
checked twice here: once as the agent receives it (must fail) and once with a
reference solution applied (must pass).

The reference solutions are also the answer key. If a fixture is ever changed,
this file says out loud what "solved" was supposed to mean.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import aider_polyglot, runner, suites  # noqa: E402


REFERENCE = {
    "bugfix/rounding": {
        "store/pricing.py": '''\
"""Money, in cents."""

from decimal import ROUND_HALF_UP, Decimal


def line_total(price, quantity):
    """Total cents for `quantity` items at `price` kronor each."""
    cents = Decimal(str(price)) * Decimal(str(quantity)) * 100
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def basket_total(lines):
    return sum(line_total(price, quantity) for _name, quantity, price in lines)


def format_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"
''',
    },
    "bugfix/parser": {
        "settings/reader.py": '''\
"""Read the config format described in README.md."""


def strip_comment(line):
    """Cut the line at the first # that is not inside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def parse(text):
    values = {}
    for raw in text.splitlines():
        line = strip_comment(raw).strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = unquote(value.strip())
    return values


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\"'":
        return value[1:-1]
    return value
''',
    },
    "feature/top-words": {
        "wordcount/stats.py": '''\
"""Word statistics."""

import re
from collections import Counter

WORD = re.compile(r"[a-z0-9']+")


def count_words(text):
    return Counter(WORD.findall(text.lower()))


def ranked(text):
    counts = count_words(text)
    return sorted(counts.items(), key=lambda row: (-row[1], row[0]))


def top_words(text, limit):
    """The `limit` most frequent words, most frequent first."""
    if limit <= 0:
        return []
    return ranked(text)[:limit]
''',
        "report.py": '''\
"""Print the most frequent words in a file."""

import argparse
from pathlib import Path

from wordcount.stats import top_words


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    text = Path(args.path).read_text(encoding="utf-8")
    for word, count in top_words(text, args.top):
        print(f"{word}: {count}")


if __name__ == "__main__":
    main()
''',
    },
    "precision/phone": {
        "phone/normalise.py": '''\
"""Canonical phone numbers. See SPEC.md."""

from .contract import COUNTRY_CODE, INTERNATIONAL_PREFIX, MIN_DIGITS, NOISE


def normalise_phone(raw):
    if not isinstance(raw, str):
        raise TypeError("a phone number arrives as text")
    cleaned = raw.strip()
    for char in NOISE:
        cleaned = cleaned.replace(char, "")
    if not cleaned:
        raise ValueError("there is no number in that")

    body = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not body.isdigit():
        raise ValueError(f"not a phone number: {raw!r}")

    if cleaned.startswith("+"):
        if not body.startswith(COUNTRY_CODE[1:]):
            return "+" + body
        subscriber = body[len(COUNTRY_CODE) - 1:]
    elif cleaned.startswith(INTERNATIONAL_PREFIX):
        subscriber = body[len(INTERNATIONAL_PREFIX):]
    elif cleaned.startswith("0"):
        subscriber = body[1:]
    else:
        subscriber = body

    if subscriber.startswith("0"):
        subscriber = subscriber[1:]
    if len(subscriber) < MIN_DIGITS:
        raise ValueError(f"too few digits: {raw!r}")
    return COUNTRY_CODE + subscriber
''',
    },
}

REFERENCE["hard/race"] = {
    "pipeline/registry.py": '''\
"""One shared object per key, built on first use."""

import threading


class Registry:
    """A registry of expensive-to-build resources shared by the workers."""

    def __init__(self):
        self._items = {}
        self._builds = 0
        self._lock = threading.Lock()

    def get_or_create(self, key, factory):
        """Return the object registered for `key`, building it if it is new."""
        with self._lock:
            if key in self._items:
                return self._items[key]
        value = factory()
        with self._lock:
            # Another thread may have won the race while the factory ran; the
            # first result is the shared one and this one is discarded.
            if key in self._items:
                return self._items[key]
            self._items[key] = value
            self._builds += 1
            return value

    def forget(self, key):
        """Drop `key` if it is present. Returns True if something was removed."""
        with self._lock:
            return self._items.pop(key, None) is not None

    def snapshot(self):
        """Everything currently held, for the status endpoint."""
        with self._lock:
            return dict(self._items)

    @property
    def builds(self):
        """How many times a factory has actually run."""
        with self._lock:
            return self._builds
''',
}

REFERENCE["hard/perf"] = {
    "analytics/sessions.py": '''\
"""Group raw events into user sessions."""

DEFAULT_GAP = 1800


def sessionise(events, gap=DEFAULT_GAP):
    """Group `events` into sessions, one list entry per session."""
    per_user = {}
    for event in events:
        per_user.setdefault(event["user"], []).append(event["ts"])

    sessions = []
    for user, stamps in per_user.items():
        stamps.sort()
        start = previous = stamps[0]
        count = 0
        for stamp in stamps:
            if stamp - previous > gap:
                sessions.append({"user": user, "start": start, "end": previous,
                                 "events": count})
                start = stamp
                count = 0
            previous = stamp
            count += 1
        sessions.append({"user": user, "start": start, "end": previous, "events": count})
    sessions.sort(key=lambda row: (row["start"], row["user"]))
    return sessions


def top_users(sessions, limit=10):
    """Users by total session count, most sessions first, ties alphabetical."""
    counts = {}
    for session in sessions:
        counts[session["user"]] = counts.get(session["user"], 0) + 1
    return sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:limit]
''',
}

REFERENCE["hard/rename"] = {
    "core/pipeline.py": '''\
"""The shared rule applier every stage is built on."""


def apply_stage(payload, *, options):
    """Return a copy of `payload` with each rule in `options` applied."""
    result = dict(payload)
    for key, rule in (options or {}).items():
        if key in result:
            result[key] = rule(result[key])
    return result
''',
    "core/loader.py": '''\
"""Find the stages listed in plugins.json."""

import importlib
import json
from pathlib import Path

# Used for any stage whose spec does not name its entry point.
DEFAULT_ENTRY = "apply_stage"


def load_stages(config_path):
    """Return [(module name, entry callable)] in the order the config lists."""
    specs = json.loads(Path(config_path).read_text(encoding="utf-8"))["stages"]
    stages = []
    for spec in specs:
        module = importlib.import_module(spec["module"])
        entry = getattr(module, spec.get("entry", DEFAULT_ENTRY))
        stages.append((spec["module"], entry))
    return stages
''',
    "core/registry.py": '''\
"""The built-in stages, for callers that do not want a config file."""

from core.pipeline import apply_stage

BUILTINS = {"identity": apply_stage}


def describe():
    """The names of the built-in stages."""
    return sorted(BUILTINS)
''',
    "stages/clean.py": '''\
"""Trim the text fields."""

from core import pipeline

RULES = {"name": str.strip, "city": str.strip}


def apply_stage(payload, *, options):
    """Stage entry point."""
    return pipeline.apply_stage(payload, options=RULES)
''',
    "stages/enrich.py": '''\
"""Title-case the name and derive the initials."""

from core import pipeline


def apply_stage(payload, *, options):
    """Stage entry point."""
    enriched = pipeline.apply_stage(payload, options={"name": str.title})
    enriched["initials"] = "".join(word[0] for word in enriched["name"].split())
    return enriched
''',
    "stages/export.py": '''\
"""Render the finished record as one line."""

from core import pipeline


def apply_stage(payload, *, options):
    """Stage entry point."""
    row = pipeline.apply_stage(payload, options={"city": str.upper})
    return {"line": f"{row['name']} ({row['initials']}) - {row['city']}"}
''',
    "run.py": '''\
"""Run every stage in plugins.json over one JSON record."""

import json
import sys
from pathlib import Path

from core.loader import load_stages


def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for _name, entry in load_stages("plugins.json"):
        payload = entry(payload, options=None)
    print(payload["line"])


if __name__ == "__main__":
    main()
''',
    "plugins.json": '''\
{
  "stages": [
    {"module": "stages.clean", "entry": "apply_stage"},
    {"module": "stages.enrich"},
    {"module": "stages.export", "entry": "apply_stage"}
  ]
}
''',
}


FIXTURES = [task for task in suites.select({"bugfix": 99, "feature": 99, "precision": 99, "hard": 99})]


def _verify(task, workspace, tmp_path):
    result = runner.verify(task, workspace, tmp_path / "verify")
    checks = runner.protected_checks(task, workspace) + (result.get("checks") or [])
    passed = bool(result.get("passed")) and all(row["passed"] for row in checks)
    return passed, result, checks


@pytest.mark.parametrize("task", FIXTURES, ids=lambda task: task.id)
def test_the_untouched_repository_fails(task, tmp_path):
    """If the fixture already passes, the task measures nothing."""
    workspace = tmp_path / "work"
    task.build(workspace)
    passed, result, _ = _verify(task, workspace, tmp_path)
    assert not passed, f"{task.id} passes without any work: {result}"


@pytest.mark.parametrize("task", FIXTURES, ids=lambda task: task.id)
def test_the_reference_solution_passes(task, tmp_path):
    """And if a correct solution fails, the task is a trap, not a benchmark."""
    workspace = tmp_path / "work"
    task.build(workspace)
    for name, body in REFERENCE[task.id].items():
        (workspace / name).write_text(body, encoding="utf-8")

    passed, result, checks = _verify(task, workspace, tmp_path)
    failed = [row for row in checks if not row["passed"]]
    assert passed, f"{task.id} rejects its own reference solution: {failed or result}"


@pytest.mark.parametrize("task", [t for t in FIXTURES if t.protected], ids=lambda task: task.id)
def test_editing_a_frozen_file_fails_the_task(task, tmp_path):
    """Rewriting the test you were asked to satisfy is not a passing change."""
    workspace = tmp_path / "work"
    task.build(workspace)
    for name, body in REFERENCE[task.id].items():
        (workspace / name).write_text(body, encoding="utf-8")
    (workspace / task.protected[0]).write_text("# gone\n", encoding="utf-8")

    passed, _, checks = _verify(task, workspace, tmp_path)
    assert not passed
    assert any(task.protected[0] in row["name"] and not row["passed"] for row in checks)


def test_the_visible_repro_actually_fails_for_bug_fixes(tmp_path):
    """Each bugfix task ships a failing test, because that is the repro."""
    import subprocess

    for task in [t for t in FIXTURES if t.suite == "bugfix"]:
        workspace = tmp_path / task.id.replace("/", "-")
        task.build(workspace)
        test_file = next(name for name in task.files if name.startswith("tests/"))
        result = subprocess.run(
            [sys.executable, test_file], cwd=str(workspace), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0, f"{task.id}: {test_file} is supposed to fail"


def test_task_selection_is_deterministic_and_bounded():
    counts = {"bugfix": 2, "feature": 1, "precision": 1}
    assert [task.id for task in suites.select(counts)] == [task.id for task in suites.select(counts)]
    # Asking for more than a suite holds gives you the suite, not an error.
    assert len(suites.select({"feature": 99})) == 1
    assert suites.select({}) == []


def test_every_suite_in_the_catalogue_can_be_selected():
    for entry in suites.suite_catalogue():
        if entry["id"] in {"humaneval", "aider_polyglot", "aider_refactor"}:
            continue  # cached public datasets have focused tests elsewhere
        assert suites.select({entry["id"]: 1}), entry["id"]


def test_humaneval_official_source_pin_is_exact():
    commit = "6d43fb980f9fee3c892a914eda09951f772ad10d"
    assert suites.HUMANEVAL_COMMIT == commit
    assert suites.HUMANEVAL_STRIDE == 7
    assert suites.SUITES["humaneval"]["max"] == 24
    assert suites.HUMANEVAL_SOURCE == f"https://github.com/openai/human-eval/tree/{commit}"
    assert suites.HUMANEVAL_URL == (
        f"https://raw.githubusercontent.com/openai/human-eval/{commit}/data/HumanEval.jsonl.gz"
    )
    assert suites.HUMANEVAL_SHA256 == "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"


def test_humaneval_download_is_commit_pinned_hashed_and_cached(tmp_path, monkeypatch):
    problem = {
        "task_id": "HumanEval/0", "entry_point": "answer", "prompt": "def answer():\n",
        "test": "def check(candidate):\n    assert candidate() == 42\n",
    }
    payload = gzip.compress((json.dumps(problem) + "\n").encode("utf-8"))
    cache = tmp_path / "HumanEval.jsonl.gz"
    monkeypatch.setattr(suites, "HUMANEVAL_PATH", cache)
    monkeypatch.setattr(suites, "HUMANEVAL_SHA256", hashlib.sha256(payload).hexdigest())
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return payload

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(suites.urllib.request, "urlopen", fake_urlopen)
    assert suites._humaneval_problems() == [problem]
    assert requested == [(suites.HUMANEVAL_URL, 60)]
    assert cache.read_bytes() == payload

    monkeypatch.setattr(
        suites.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert suites._humaneval_problems() == [problem]


def test_humaneval_rejects_a_corrupt_cache_without_network(tmp_path, monkeypatch):
    cache = tmp_path / "HumanEval.jsonl.gz"
    cache.write_bytes(b"damaged")
    monkeypatch.setattr(suites, "HUMANEVAL_PATH", cache)
    monkeypatch.setattr(suites, "HUMANEVAL_SHA256", hashlib.sha256(b"expected").hexdigest())
    monkeypatch.setattr(
        suites.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )

    with pytest.raises(RuntimeError, match="cache integrity check failed"):
        suites._humaneval_problems()


def test_humaneval_stride_subset_builds_and_verifies_outside_the_workspace(tmp_path, monkeypatch):
    problems = []
    for index in range(164):
        entry = f"answer_{index}"
        problems.append({
            "task_id": f"HumanEval/{index}",
            "entry_point": entry,
            "prompt": f"def {entry}():\n",
            "canonical_solution": f"    return {index}\n",
            "test": f"def check(candidate):\n    assert candidate() == {index}\n",
        })
    monkeypatch.setattr(suites, "_humaneval_problems", lambda: problems)

    tasks = suites.select({"humaneval": 999})
    expected_indices = list(range(0, 24 * suites.HUMANEVAL_STRIDE, suites.HUMANEVAL_STRIDE))
    assert [task.provenance["task_id"] for task in tasks] == [
        f"HumanEval/{index}" for index in expected_indices
    ]
    assert all(task.source == suites.HUMANEVAL_SOURCE for task in tasks)
    assert all(task.provenance["leaderboard_comparable"] is False for task in tasks)

    for position in (0, len(tasks) - 1):
        task = tasks[position]
        problem = problems[expected_indices[position]]
        workspace = tmp_path / f"workspace-{position}"
        verify_dir = tmp_path / "external-verifiers" / str(position)
        task.build(workspace)
        assert sorted(path.name for path in workspace.iterdir() if path.name != ".git") == [
            "README.md", "solution.py",
        ]
        assert runner.verify(task, workspace, verify_dir / "untouched")["passed"] is False

        (workspace / "solution.py").write_text(
            problem["prompt"] + problem["canonical_solution"], encoding="utf-8",
        )
        assert runner.verify(task, workspace, verify_dir / "canonical")["passed"] is True
        verifier = verify_dir / "canonical" / f"{task.id.replace('/', '-')}.py"
        assert verifier.exists()
        assert workspace.resolve() not in verifier.resolve().parents


def _cache_fake_aider_subset(tmp_path, monkeypatch):
    """Supply tiny valid stand-ins so suite tests never depend on the network."""
    payloads = {}
    for exercise in aider_polyglot.EXERCISES:
        for workspace_name, upstream_path in aider_polyglot.file_manifest(exercise).items():
            if workspace_name == exercise.module:
                payload = b"VALUE = 0\n"
            elif workspace_name == exercise.test_file:
                imported = exercise.module.removesuffix(".py")
                payload = (
                    "import unittest\n"
                    f"import {imported}\n\n"
                    "class ExerciseTest(unittest.TestCase):\n"
                    "    def test_solution(self):\n"
                    f"        self.assertEqual({imported}.VALUE, 1)\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ).encode("utf-8")
            else:
                payload = f"# exact fake {workspace_name}\n".encode("utf-8")
            payloads[upstream_path] = payload

    monkeypatch.setattr(aider_polyglot, "CACHE_DIR", tmp_path / "aider-cache")
    monkeypatch.setattr(
        aider_polyglot,
        "UPSTREAM_SHA256",
        {path: hashlib.sha256(payload).hexdigest() for path, payload in payloads.items()},
    )
    for upstream_path, payload in payloads.items():
        target = aider_polyglot.cache_path(upstream_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def no_network(*_args, **_kwargs):
        raise AssertionError("a cached suite test attempted the network")

    monkeypatch.setattr(aider_polyglot.urllib.request, "urlopen", no_network)
    return payloads


def test_aider_polyglot_subset_is_pinned_deterministic_and_provenanced(tmp_path, monkeypatch):
    payloads = _cache_fake_aider_subset(tmp_path, monkeypatch)

    tasks = suites.select({"aider_polyglot": 99})
    assert [task.id for task in tasks] == [
        "aider_polyglot/phone-number",
        "aider_polyglot/wordy",
        "aider_polyglot/bowling",
        "aider_polyglot/forth",
        "aider_polyglot/poker",
        "aider_polyglot/zipper",
    ]
    assert [task.id for task in suites.select({"aider_polyglot": 2})] == [
        "aider_polyglot/phone-number",
        "aider_polyglot/wordy",
    ]

    for task, exercise in zip(tasks, aider_polyglot.EXERCISES):
        manifest = aider_polyglot.file_manifest(exercise)
        assert task.files[exercise.module].encode("utf-8") == payloads[manifest[exercise.module]]
        assert exercise.test_file in task.protected
        assert ".docs/instructions.md" in task.protected
        assert task.source.endswith(f"/{exercise.upstream_root}")
        assert task.provenance["commit"] == aider_polyglot.UPSTREAM_COMMIT
        assert task.provenance["exercise"] == exercise.slug
        assert task.provenance["leaderboard_comparable"] is False

    entry = next(row for row in suites.suite_catalogue() if row["id"] == "aider_polyglot")
    assert entry["default"] == 0
    assert entry["max"] == 6
    assert entry["source"] == aider_polyglot.UPSTREAM_REPOSITORY
    assert entry["provenance"]["commit"] == aider_polyglot.UPSTREAM_COMMIT
    assert entry["leaderboard_comparable"] is False
    assert "not leaderboard-comparable" in entry["detail"]


def test_aider_polyglot_external_verifier_runs_the_protected_upstream_unittest(
    tmp_path, monkeypatch,
):
    _cache_fake_aider_subset(tmp_path, monkeypatch)
    task = suites.select({"aider_polyglot": 1})[0]
    workspace = tmp_path / "work"
    task.build(workspace)

    untouched = runner.verify(task, workspace, tmp_path / "verify")
    assert untouched["passed"] is False
    assert untouched["checks"][0]["name"] == "upstream phone_number_test.py passes"

    (workspace / "phone_number.py").write_text("VALUE = 1\n", encoding="utf-8")
    solved = runner.verify(task, workspace, tmp_path / "verify")
    assert solved["passed"] is True

    (workspace / "phone_number_test.py").write_text(
        "# rewritten to hide a bad solution\n", encoding="utf-8",
    )
    protected = runner.protected_checks(task, workspace)
    assert any(
        row["name"] == "phone_number_test.py left untouched" and not row["passed"]
        for row in protected
    )


def test_aider_polyglot_download_is_commit_pinned_hashed_and_cached(tmp_path, monkeypatch):
    exercise = aider_polyglot.EXERCISES[0]
    upstream_path = aider_polyglot.file_manifest(exercise)[".docs/instructions.md"]
    payload = b"# byte-exact pinned instruction\n"
    monkeypatch.setattr(aider_polyglot, "CACHE_DIR", tmp_path / "aider-cache")
    monkeypatch.setattr(
        aider_polyglot,
        "UPSTREAM_SHA256",
        {upstream_path: hashlib.sha256(payload).hexdigest()},
    )

    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return payload

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(aider_polyglot.urllib.request, "urlopen", fake_urlopen)
    assert aider_polyglot.load_pinned_text(upstream_path) == payload.decode("utf-8")
    assert requested == [(f"{aider_polyglot.UPSTREAM_RAW}/{upstream_path}", 60)]
    assert aider_polyglot.cache_path(upstream_path).read_bytes() == payload

    monkeypatch.setattr(
        aider_polyglot.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert aider_polyglot.load_pinned_text(upstream_path) == payload.decode("utf-8")


def test_aider_polyglot_rejects_a_corrupt_cache_without_using_network(tmp_path, monkeypatch):
    exercise = aider_polyglot.EXERCISES[0]
    upstream_path = aider_polyglot.file_manifest(exercise)[".docs/instructions.md"]
    good = b"# expected\n"
    monkeypatch.setattr(aider_polyglot, "CACHE_DIR", tmp_path / "aider-cache")
    monkeypatch.setattr(
        aider_polyglot,
        "UPSTREAM_SHA256",
        {upstream_path: hashlib.sha256(good).hexdigest()},
    )
    target = aider_polyglot.cache_path(upstream_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"damaged\n")
    monkeypatch.setattr(
        aider_polyglot.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )

    with pytest.raises(RuntimeError, match="integrity check failed"):
        aider_polyglot.load_pinned_text(upstream_path)
