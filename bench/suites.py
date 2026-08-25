"""The benchmark tasks, and how each one is checked.

Design rules, all of them learned the expensive way:

* A task is a **repository**, not a prompt. The agent gets a git repo and a
  brief, and has to find the code, understand it, change it and stop. That
  exercises the whole loop -- read, edit, verify, review -- rather than the
  model's ability to write a function body from a docstring.
* Verification is **hidden and external**. The checks below never ship into the
  workspace, and they run in a separate process against whatever ended up on
  disk. The agent's own account of what it did is never consulted.
* The visible failing test is a **repro, not the specification**. Every hidden
  check set is deliberately wider than the test in the repo, so patching the
  symptom -- or special-casing the exact input -- does not score.
* Local fixtures are small on purpose. Public stress suites are explicit,
  count-bounded opt-ins; a benchmark you will not run teaches you nothing.

Ten families, each measuring something different:

    tweak      a one-line change in a bulky repo. Cost, not cleverness (tweak.py)
    bugfix     can it localise a defect from a failing test?
    feature    can it thread one change through several files?
    precision  does it follow a written contract exactly, including edge cases?
    hard       the tier that needs working tools rather than recall (hard.py)
    engineering production-shaped security and streaming repositories
    humaneval  the classic set, as repository tasks (breadth, not depth)
    aider_polyglot  pinned public Python exercises with upstream unittests
    aider_refactor  pinned public large-file extraction tasks with AST grading
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import DATA_DIR, aider_polyglot, aider_refactor

HUMANEVAL_PATH = DATA_DIR / "HumanEval.jsonl.gz"
HUMANEVAL_COMMIT = "6d43fb980f9fee3c892a914eda09951f772ad10d"
HUMANEVAL_URL = f"https://raw.githubusercontent.com/openai/human-eval/{HUMANEVAL_COMMIT}/data/HumanEval.jsonl.gz"
HUMANEVAL_SOURCE = f"https://github.com/openai/human-eval/tree/{HUMANEVAL_COMMIT}"
HUMANEVAL_SHA256 = "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
# A fixed stride across the set. Two runs then draw the same problems, so a
# lucky streak of easy ones cannot flatter a harness change.
HUMANEVAL_STRIDE = 7
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------- verification

# Injected above every hidden check set so a task definition only has to write
# the cases themselves. The contract with the caller is one line of JSON on
# stdout, whatever else the checked code printed.
VERIFIER_PREAMBLE = '''\
import json, os, sys, subprocess, traceback
WORKSPACE = sys.argv[1]
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
sys.path.insert(0, WORKSPACE)
_CASES = []


def case(name):
    """Register one named check. An assertion failure is a failed check."""
    def register(fn):
        _CASES.append((name, fn))
        return fn
    return register


def cli(*args, timeout=30):
    """Run the repo's own entry point the way a user would."""
    return subprocess.run([sys.executable, *args], cwd=WORKSPACE, capture_output=True,
                          text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)


def _report():
    results = []
    for name, fn in _CASES:
        try:
            fn()
        except AssertionError as exc:
            results.append({"name": name, "passed": False, "detail": str(exc)[:300] or "assertion failed"})
        except Exception as exc:
            results.append({"name": name, "passed": False,
                            "detail": f"{type(exc).__name__}: {exc}"[:300]})
        else:
            results.append({"name": name, "passed": True, "detail": ""})
    print("__BENCH__" + json.dumps({
        "passed": all(row["passed"] for row in results) and bool(results),
        "checks": results,
    }))
'''

VERIFIER_POSTAMBLE = '''

_report()
'''


@dataclass(frozen=True)
class Task:
    """One benchmark task: a repository, a brief, and hidden checks."""

    id: str
    suite: str
    title: str
    brief: str
    files: dict[str, str]
    checks: str
    # Files the brief forbids touching. Scored: rewriting the test you were
    # asked to satisfy is not a passing change, however green it looks.
    protected: tuple[str, ...] = field(default_factory=tuple)
    # Custom multi-model runs set these so each task can use a different harness
    # while still sharing one run folder and one scoreboard.
    provider: str = ""
    model: str = ""
    reasoning: str = ""
    fast: bool = False
    # Public suites carry enough provenance for callers to identify the exact
    # source rather than treating a convenient subset as a leaderboard run.
    source: str = ""
    provenance: dict[str, object] = field(default_factory=dict)

    @property
    def verifier(self) -> str:
        return VERIFIER_PREAMBLE + self.checks + VERIFIER_POSTAMBLE

    def build(self, workspace: Path) -> None:
        """Materialise the repo. Git history matters: the harness reviews a diff."""
        workspace.mkdir(parents=True, exist_ok=True)
        for name, body in self.files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        for args in (
            ["init", "-q"],
            ["add", "-A"],
            ["-c", "user.email=bench@aios", "-c", "user.name=aiOS bench", "commit", "-qm", "task fixture"],
        ):
            subprocess.run(
                ["git", "-C", str(workspace), *args],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )


# ------------------------------------------------------------------- fixtures


_ROUNDING_BRIEF = """\
`python tests/test_pricing.py` fails on this repository.

Find why and fix `store/pricing.py` so the whole test file passes. The money
rules -- whole cents, exactness to the öre, and how to round -- are stated in
that module's docstring, and they are the specification. The failing test is
one symptom of breaking them, not the whole rule.

Do not edit anything under `tests/`; that file is the specification.
"""

_ROUNDING_FILES = {
    "README.md": """\
# store

Pricing for the till. Everything downstream assumes `line_total` and
`basket_total` return whole cents as `int`.

    python tests/test_pricing.py
""",
    "store/__init__.py": "",
    "store/pricing.py": '''\
"""Money, in cents.

A price arrives as a decimal number of kronor (0.07 is seven öre). Every total
this module returns is an integer number of cents, rounded half up at the last
step and never before it -- half an öre is a whole öre, so 0.005 kronor is 1.
"""


def line_total(price, quantity):
    """Total cents for `quantity` items at `price` kronor each."""
    return int(price * quantity * 100)


def basket_total(lines):
    """Total cents for an iterable of (name, quantity, price) rows."""
    return sum(line_total(price, quantity) for _name, quantity, price in lines)


def format_cents(cents):
    """Render whole cents as kronor: 2145 -> '21.45'."""
    return f"{cents // 100}.{cents % 100:02d}"
''',
    "tests/test_pricing.py": '''\
"""The specification. Do not edit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store.pricing import basket_total, format_cents, line_total


def test_line_total_is_exact_for_awkward_prices():
    assert line_total(0.07, 3) == 21
    assert line_total(0.29, 3) == 87
    assert line_total(1.15, 2) == 230


def test_basket_totals_add_up():
    assert basket_total([("apple", 3, 0.07), ("pear", 3, 0.29)]) == 108


def test_format_cents_pads_the_ore():
    assert format_cents(2145) == "21.45"
    assert format_cents(7) == "0.07"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
''',
}

_ROUNDING_CHECKS = '''
from store.pricing import basket_total, format_cents, line_total


@case("the reported failure is fixed")
def _():
    assert line_total(0.29, 3) == 87, f"0.29 x 3 gave {line_total(0.29, 3)}"
    assert line_total(1.15, 2) == 230, f"1.15 x 2 gave {line_total(1.15, 2)}"


@case("float truncation is gone across the price range")
def _():
    # Every pair here loses a cent under `int(price * quantity * 100)`, so
    # special-casing the two in the repo's test does not get you through.
    for price, quantity, expected in [
        (0.15, 3, 45), (0.35, 3, 105), (0.37, 3, 111), (0.09, 5, 45),
        (0.57, 2, 114), (0.41, 5, 205), (0.47, 5, 235), (0.51, 5, 255),
    ]:
        got = line_total(price, quantity)
        assert got == expected, f"{price} x {quantity} gave {got}, expected {expected}"


@case("exact prices were not broken on the way past")
def _():
    for price, quantity, expected in [(0.07, 3, 21), (2.02, 5, 1010), (4.35, 3, 1305)]:
        got = line_total(price, quantity)
        assert got == expected, f"{price} x {quantity} gave {got}, expected {expected}"


@case("rounding is half up, not banker's")
def _():
    # The docstring says half an öre is a whole öre. round() disagrees.
    assert line_total(0.005, 1) == 1, f"0.005 gave {line_total(0.005, 1)}"
    assert line_total(0.015, 1) == 2, f"0.015 gave {line_total(0.015, 1)}"


@case("baskets still add up")
def _():
    assert basket_total([("apple", 3, 0.29), ("pear", 2, 1.15)]) == 317
    assert basket_total([]) == 0


@case("totals are still whole cents")
def _():
    value = line_total(1.99, 3)
    assert isinstance(value, int) and not isinstance(value, bool), f"got {type(value).__name__}"


@case("format_cents was left working")
def _():
    assert format_cents(2145) == "21.45"
    assert format_cents(7) == "0.07"
    assert format_cents(0) == "0.00"
'''


_PARSER_BRIEF = """\
`python tests/test_config.py` fails on this repository.

`settings/reader.py` strips inline `#` comments before it looks at quoting, so a
`#` inside a quoted value truncates the value. Fix the reader so the documented
format in `README.md` is honoured.

Do not edit anything under `tests/`; that file is the specification.
"""

_PARSER_FILES = {
    "README.md": """\
# settings

A tiny INI-ish config format.

    key = value            # everything after an unquoted # is a comment
    key = "quoted value"   # quotes survive, and a # inside them is data
    key = 'single works'   # either quote style

Rules:

* Blank lines and lines whose first non-space character is `#` are ignored.
* Whitespace around the key and the value is stripped.
* A quoted value keeps its inner spaces and its `#` characters verbatim.
* A repeated key takes the last value.

    python tests/test_config.py
""",
    "settings/__init__.py": "",
    "settings/reader.py": '''\
"""Read the config format described in README.md."""


def parse(text):
    """Parse config text into a dict of str -> str."""
    values = {}
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = unquote(value.strip())
    return values


def unquote(value):
    """Drop one matching pair of surrounding quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\"'":
        return value[1:-1]
    return value
''',
    "tests/test_config.py": '''\
"""The specification. Do not edit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.reader import parse


def test_a_hash_inside_quotes_is_data():
    assert parse('channel = "#general"') == {"channel": "#general"}
    assert parse("note = 'tea # 2 sugars'") == {"note": "tea # 2 sugars"}


def test_an_unquoted_hash_still_starts_a_comment():
    assert parse("host = localhost # the dev box") == {"host": "localhost"}


def test_blank_and_comment_lines_are_ignored():
    assert parse("\\n  # nothing here\\nport = 8080\\n") == {"port": "8080"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
''',
}

_PARSER_CHECKS = '''
from settings.reader import parse


@case("the reported failure is fixed")
def _():
    assert parse('channel = "#general"') == {"channel": "#general"}


@case("unquoted comments are still stripped")
def _():
    assert parse("host = localhost # the dev box") == {"host": "localhost"}
    assert parse("host = localhost#tight") == {"host": "localhost"}


@case("both quote styles keep their contents verbatim")
def _():
    assert parse("note = 'tea # 2 sugars'") == {"note": "tea # 2 sugars"}
    assert parse('note = "  spaced  "') == {"note": "  spaced  "}
    assert parse("""empty = ''""") == {"empty": ""}


@case("a comment can still follow a quoted value")
def _():
    assert parse('channel = "#general"  # where we talk') == {"channel": "#general"}


@case("blank, comment and malformed lines are ignored")
def _():
    assert parse("\\n   # nothing\\nport = 8080\\nrubbish\\n") == {"port": "8080"}
    assert parse("") == {}


@case("a repeated key takes the last value")
def _():
    assert parse("a = 1\\na = 2") == {"a": "2"}


@case("an = inside a value survives")
def _():
    assert parse("url = https://x/?a=1&b=2") == {"url": "https://x/?a=1&b=2"}
'''


_FEATURE_BRIEF = """\
Add a `--top N` option to this word-count tool.

Required behaviour:

* `wordcount/stats.py` gains `top_words(text, limit)`. It returns a list of
  `(word, count)` pairs, longest count first, and ties broken alphabetically.
  A `limit` of 0 or less returns an empty list.
* `report.py` gains a `--top N` flag, default 5, that limits how many rows the
  report prints. The existing output format -- one `word: count` line per row,
  most frequent first -- does not change.
* `python report.py sample.txt --top 2` therefore prints exactly two lines.

Words are what `stats.count_words` already considers words; do not change that
definition. Keep the existing functions working -- `report.py` with no flag
must behave exactly as it does now.
"""

_FEATURE_FILES = {
    "README.md": """\
# wordcount

    python report.py sample.txt

Prints the most frequent words in a file, one `word: count` line each.
""",
    "wordcount/__init__.py": "",
    "wordcount/stats.py": '''\
"""Word statistics."""

import re
from collections import Counter

WORD = re.compile(r"[a-z0-9']+")


def count_words(text):
    """Count words, case-insensitively. Apostrophes are part of a word."""
    return Counter(WORD.findall(text.lower()))


def ranked(text):
    """Every word, most frequent first, ties broken alphabetically."""
    counts = count_words(text)
    return sorted(counts.items(), key=lambda row: (-row[1], row[0]))
''',
    "report.py": '''\
"""Print the most frequent words in a file."""

import argparse
from pathlib import Path

from wordcount.stats import ranked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args()

    text = Path(args.path).read_text(encoding="utf-8")
    for word, count in ranked(text):
        print(f"{word}: {count}")


if __name__ == "__main__":
    main()
''',
    "sample.txt": (
        "the quick brown fox jumps over the lazy dog\n"
        "the dog barks and the fox runs\n"
        "a quick fox is a lazy dog's envy\n"
    ),
}

_FEATURE_CHECKS = '''
from wordcount.stats import count_words, ranked, top_words

TEXT = "b b b a a c"


@case("top_words ranks by count, then alphabetically")
def _():
    assert top_words(TEXT, 3) == [("b", 3), ("a", 2), ("c", 1)], top_words(TEXT, 3)


@case("top_words honours the limit")
def _():
    assert top_words(TEXT, 1) == [("b", 3)]
    assert top_words(TEXT, 99) == [("b", 3), ("a", 2), ("c", 1)]


@case("a limit of zero or less returns nothing")
def _():
    assert top_words(TEXT, 0) == []
    assert top_words(TEXT, -3) == []


@case("the existing word definition was not changed")
def _():
    assert count_words("Don't stop, don't") == {"don't": 2, "stop": 1}
    assert ranked("a b b")[0] == ("b", 2)


@case("--top limits the printed rows")
def _():
    result = cli("report.py", "sample.txt", "--top", "2")
    assert result.returncode == 0, result.stderr[:300]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2, f"expected 2 rows, got {len(lines)}: {lines}"
    assert lines[0].startswith("the: "), lines[0]


@case("the default is five rows")
def _():
    result = cli("report.py", "sample.txt")
    assert result.returncode == 0, result.stderr[:300]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 5, f"expected 5 rows by default, got {len(lines)}"


@case("the output format is unchanged")
def _():
    result = cli("report.py", "sample.txt", "--top", "1")
    assert result.stdout.strip() == "the: 4", repr(result.stdout)
'''


_PRECISION_BRIEF = """\
Implement `normalise_phone` in `phone/normalise.py`.

`SPEC.md` is the contract and it is exhaustive: every rule and every edge case
you need is written down there. Read it before you write anything.

Two files are frozen. Do not edit `SPEC.md` or `phone/contract.py` -- import the
constants from `contract` rather than repeating their values.
"""

_PRECISION_FILES = {
    "SPEC.md": """\
# normalise_phone(raw) -> str

Turn a Swedish phone number a human typed into one canonical form.

## Output shape

`+46` followed by the subscriber number, with no leading zero, no spaces and no
punctuation. `08-123 45 67` becomes `+4681234567`.

## Rules, in order

1. Strip every space, hyphen, slash, full stop and parenthesis from `raw`.
2. `+46...` stays as it is (after stripping), with any `0` immediately after the
   country code removed: `+46 08 123` -> `+468123`.
3. `0046...` is the same number written differently: replace the leading `0046`
   with `+46`, then apply rule 2.
4. A number starting with a single `0` is domestic: replace that `0` with `+46`.
5. Anything else -- no leading `0`, no country code -- is already a subscriber
   number: prefix `+46`.

## Edge cases, all required

* `raw` that is empty or only punctuation raises `ValueError`.
* Any character left after stripping that is not a digit, and is not a leading
  `+`, raises `ValueError`. A `+` anywhere except position 0 is invalid.
* A `+` followed by a country code other than `46` is returned stripped but
  otherwise untouched: `+44 20 7946` -> `+44207946`.
* A `+46` result must contain at least `MIN_DIGITS` digits after the country
  code, or `ValueError` is raised. Foreign numbers are not length-checked.
* `raw` is not necessarily a string. A non-string input raises `TypeError`.
* Leading and trailing whitespace in `raw` is not an error.
""",
    "phone/__init__.py": "",
    "phone/contract.py": '''\
"""Frozen constants. Do not edit this file."""

COUNTRY_CODE = "+46"
INTERNATIONAL_PREFIX = "0046"
# Digits required after the country code before a number is plausible.
MIN_DIGITS = 6
# Characters a human might type that carry no information.
NOISE = " -/.()\\t"
''',
    "phone/normalise.py": '''\
"""Canonical phone numbers. See SPEC.md."""

from .contract import COUNTRY_CODE, INTERNATIONAL_PREFIX, MIN_DIGITS, NOISE


def normalise_phone(raw):
    """Return `raw` as a canonical +46 number. See SPEC.md for the rules."""
    raise NotImplementedError
''',
    "README.md": """\
# phone

One function, one specification. `SPEC.md` is the contract.
""",
}

_PRECISION_CHECKS = '''
from phone.normalise import normalise_phone


@case("domestic numbers gain the country code")
def _():
    assert normalise_phone("08-123 45 67") == "+4681234567"
    assert normalise_phone("070 123 45 67") == "+46701234567"


@case("an existing +46 is kept, and a 0 after it is dropped")
def _():
    assert normalise_phone("+46 8 123 45 67") == "+4681234567"
    assert normalise_phone("+46 08 123 45 67") == "+4681234567"


@case("0046 is the same number as +46")
def _():
    assert normalise_phone("0046 8 123 45 67") == "+4681234567"
    assert normalise_phone("0046081234567") == "+4681234567"


@case("a bare subscriber number is prefixed")
def _():
    assert normalise_phone("8123 45 67") == "+4681234567"


@case("noise characters are stripped")
def _():
    assert normalise_phone(" (08) 123-45.67 ") == "+4681234567"
    assert normalise_phone("08/1234567") == "+4681234567"


@case("empty or punctuation-only input raises ValueError")
def _():
    for raw in ["", "   ", "-- () --", "."]:
        try:
            normalise_phone(raw)
        except ValueError:
            continue
        raise AssertionError(f"{raw!r} should raise ValueError")


@case("letters and a misplaced + raise ValueError")
def _():
    for raw in ["08-123 four", "08+1234567", "++46812345678", "0812345x67"]:
        try:
            normalise_phone(raw)
        except ValueError:
            continue
        raise AssertionError(f"{raw!r} should raise ValueError")


@case("a foreign country code is stripped but not rewritten")
def _():
    assert normalise_phone("+44 20 7946 0958") == "+442079460958"


@case("too few digits raises ValueError")
def _():
    try:
        normalise_phone("08-12")
    except ValueError:
        return
    raise AssertionError("a 4-digit number should raise ValueError")


@case("a non-string raises TypeError")
def _():
    for raw in [None, 812345678, ["08"]]:
        try:
            normalise_phone(raw)
        except TypeError:
            continue
        raise AssertionError(f"{raw!r} should raise TypeError")


@case("the frozen constants are imported, not copied")
def _():
    import inspect

    from phone import normalise

    source = inspect.getsource(normalise)
    assert "contract" in source, "SPEC.md says to import the constants from contract"
'''


_FIXTURES: tuple[Task, ...] = (
    Task(
        id="bugfix/rounding",
        suite="bugfix",
        title="Money that loses a cent",
        brief=_ROUNDING_BRIEF,
        files=_ROUNDING_FILES,
        checks=_ROUNDING_CHECKS,
        protected=("tests/test_pricing.py",),
    ),
    Task(
        id="bugfix/parser",
        suite="bugfix",
        title="A # inside quotes is data",
        brief=_PARSER_BRIEF,
        files=_PARSER_FILES,
        checks=_PARSER_CHECKS,
        protected=("tests/test_config.py",),
    ),
    Task(
        id="feature/top-words",
        suite="feature",
        title="Thread --top through two files",
        brief=_FEATURE_BRIEF,
        files=_FEATURE_FILES,
        checks=_FEATURE_CHECKS,
    ),
    Task(
        id="precision/phone",
        suite="precision",
        title="Follow a written contract exactly",
        brief=_PRECISION_BRIEF,
        files=_PRECISION_FILES,
        checks=_PRECISION_CHECKS,
        protected=("SPEC.md", "phone/contract.py"),
    ),
)


# ------------------------------------------------------------------ humaneval


_HUMANEVAL_BRIEF = """\
`solution.py` in this repository contains one unfinished function, `{entry}`.

Implement it so it does exactly what its docstring describes. Keep the existing
signature, edit the file in place, and do not add new files or write tests.
"""

# Deliberately assembled by concatenation, not str.format: a HumanEval test is
# arbitrary Python and most of them contain braces, which format() would try to
# read as fields.
def _humaneval_checks(test: str, entry: str) -> str:
    return (
        "\nimport solution\n\n"
        + test.rstrip()
        + "\n\n\n@case(\"the hidden test passes\")\ndef _():\n"
        + f"    check(getattr(solution, {entry!r}))\n"
    )


def _humaneval_problems() -> list[dict]:
    try:
        payload = HUMANEVAL_PATH.read_bytes()
    except FileNotFoundError:
        HUMANEVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(HUMANEVAL_URL, headers={"User-Agent": "aiOS-benchmark/1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != HUMANEVAL_SHA256:
            raise RuntimeError(
                f"HumanEval integrity check failed: expected {HUMANEVAL_SHA256}, got {digest}"
            )
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".HumanEval.", suffix=".tmp",
                dir=HUMANEVAL_PATH.parent, delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(payload)
            os.replace(temporary, HUMANEVAL_PATH)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != HUMANEVAL_SHA256:
        raise RuntimeError(
            f"HumanEval cache integrity check failed: expected {HUMANEVAL_SHA256}, got {digest}"
        )
    with gzip.open(HUMANEVAL_PATH, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _humaneval_task(problem: dict) -> Task:
    entry = str(problem["entry_point"])
    return Task(
        id=f"humaneval/{problem['task_id'].replace('/', '-')}",
        suite="humaneval",
        title=f"Finish {entry}()",
        brief=_HUMANEVAL_BRIEF.format(entry=entry),
        files={
            "solution.py": problem["prompt"].rstrip() + "\n    pass\n",
            "README.md": "# bench task\n\n`solution.py` holds one unfinished function.\n",
        },
        # The problem ships its own `check(candidate)`; it becomes our one case.
        checks=_humaneval_checks(str(problem["test"]), entry),
        source=HUMANEVAL_SOURCE,
        provenance={
            "benchmark": "OpenAI HumanEval",
            "repository": "https://github.com/openai/human-eval",
            "commit": HUMANEVAL_COMMIT,
            "task_id": str(problem["task_id"]),
            "language": "python",
            "subset": True,
            "repository_adaptation": True,
            "leaderboard_comparable": False,
        },
    )


# --------------------------------------------------------------------- picking


def _fixture_pool() -> tuple[Task, ...]:
    """Every repository fixture, including the hard tier.

    `hard.py` is imported here rather than at module level because it needs
    `Task` from this module, and a top-level import either way is a cycle.
    """
    from .hard import HARD_FIXTURES
    from .engineering import ENGINEERING_FIXTURES
    from .tweak import TWEAK_FIXTURES

    return _FIXTURES + HARD_FIXTURES + ENGINEERING_FIXTURES + TWEAK_FIXTURES


SUITES: dict[str, dict[str, object]] = {
    "tweak": {
        "label": "Tweak",
        "detail": "One-line changes in a bulky repo. Measures cost, not cleverness.",
        "max": 3,
    },
    "bugfix": {
        "label": "Bug fix",
        "detail": "A failing test, a real defect. Can it localise and fix it?",
        "max": 2,
    },
    "feature": {
        "label": "Feature",
        "detail": "One change threaded through several files, to a written spec.",
        "max": 1,
    },
    "precision": {
        "label": "Precision",
        "detail": "An exhaustive contract with edge cases and frozen files.",
        "max": 1,
    },
    "hard": {
        "label": "Hard",
        "detail": "A race, a quadratic report, a rename. Tools required, not recall.",
        "max": 3,
    },
    "engineering": {
        "label": "Repository engineering",
        "detail": "Security and streaming changes with hidden adversarial checks across a real package shape.",
        "max": 2,
    },
    "humaneval": {
        "label": "HumanEval",
        "detail": "Pinned OpenAI problems adapted to repository tasks; not leaderboard-comparable.",
        "max": 24,
        "source": HUMANEVAL_SOURCE,
        "provenance": {"benchmark": "OpenAI HumanEval", "commit": HUMANEVAL_COMMIT,
                       "subset": True, "repository_adaptation": True},
        "leaderboard_comparable": False,
    },
    "aider_polyglot": {
        "label": "Aider Polyglot · Python",
        "detail": "Pinned six-task Python subset with upstream unittests; not leaderboard-comparable.",
        "max": len(aider_polyglot.EXERCISES),
        "source": aider_polyglot.UPSTREAM_REPOSITORY,
        "provenance": aider_polyglot.SUITE_PROVENANCE,
        "leaderboard_comparable": False,
    },
    "aider_refactor": {
        "label": "Aider Refactoring · Large Python",
        "detail": "Pinned five-task size-stratified subset with external AST grading; not leaderboard-comparable.",
        "max": len(aider_refactor.EXERCISES),
        "source": aider_refactor.UPSTREAM_REPOSITORY,
        "provenance": aider_refactor.SUITE_PROVENANCE,
        "leaderboard_comparable": False,
    },
}

DEFAULT_COUNTS = {"tweak": 3, "bugfix": 2, "feature": 1, "precision": 1, "hard": 1, "humaneval": 2}


def suite_catalogue() -> list[dict]:
    """What the picker in the UI offers."""
    catalogue = []
    for name, meta in SUITES.items():
        entry = {
            "id": name,
            "label": meta["label"],
            "detail": meta["detail"],
            "max": meta["max"],
            "default": DEFAULT_COUNTS.get(name, 0),
        }
        for field_name in ("source", "provenance", "leaderboard_comparable"):
            if field_name in meta:
                value = meta[field_name]
                entry[field_name] = dict(value) if isinstance(value, dict) else value
        entry["official"] = bool(meta.get("source"))
        if entry["official"] and meta.get("leaderboard_comparable") is False:
            entry["comparability_note"] = "Public benchmark subset/adaptation; do not compare its percentage to a full leaderboard score."
        catalogue.append(entry)
    return catalogue


def select(counts: dict) -> list[Task]:
    """Pick tasks for a run. Deterministic: the same counts give the same set."""
    chosen: list[Task] = []
    for suite in SUITES:
        wanted = max(0, int(counts.get(suite) or 0))
        if not wanted:
            continue
        if suite == "humaneval":
            problems = _humaneval_problems()
            for index in range(min(wanted, int(SUITES[suite]["max"]))):
                chosen.append(_humaneval_task(problems[(index * HUMANEVAL_STRIDE) % len(problems)]))
            continue
        if suite == "aider_polyglot":
            chosen.extend(aider_polyglot.tasks(min(wanted, int(SUITES[suite]["max"]))))
            continue
        if suite == "aider_refactor":
            chosen.extend(aider_refactor.tasks(min(wanted, int(SUITES[suite]["max"]))))
            continue
        pool = [task for task in _fixture_pool() if task.suite == suite]
        chosen.extend(pool[:wanted])
    return chosen


def task_by_id(task_id: str, counts: dict) -> Task | None:
    for task in select(counts):
        if task.id == task_id:
            return task
    return None
