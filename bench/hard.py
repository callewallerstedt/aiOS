"""The hard tier: tasks that measure the harness, not the model's recall.

The four suites in `suites.py` are deliberately small, and a good model can
one-shot most of them from the brief alone. That makes them a poor test of the
thing we actually care about here -- the loop around the model. These three are
chosen so that a model reasoning in a vacuum will fail them and a model with
working tools will pass:

    hard/race      the bug does not reproduce single-threaded, and the repo's
                   own test passes. Reading the code is not enough; the agent
                   has to reason about interleaving and the checks hammer the
                   result with real threads.
    hard/perf      correct but quadratic. The only way to know you fixed it is
                   to run it, so this measures whether the harness lets the
                   agent execute code and read the result.
    hard/rename    one identifier, eleven call sites, two of them reached
                   dynamically through a config file and a getattr. Whoever
                   edits only the files they can guess fails.

They are still small -- the biggest fixture is a dozen short files -- because a
benchmark you will not run teaches you nothing.
"""

from __future__ import annotations

from .suites import Task

# --------------------------------------------------------------- hard/race


_RACE_BRIEF = """\
`pipeline/registry.py` is shared by every worker thread in the pipeline. Two
things go wrong in production, and neither reproduces with a single thread:

* Expensive resources are built more than once for the same key. `builds`
  climbs past the number of distinct keys, and two workers end up holding
  different objects for what should be one shared resource.
* The status endpoint intermittently dies with "dictionary changed size during
  iteration", and we have seen callers mutate the registry by accident through
  the value `snapshot()` handed them.

Make the registry safe to use from many threads at once.

Keep the public API exactly as it is -- `get_or_create`, `forget`, `snapshot`
and `builds` -- and keep single-threaded behaviour identical. `pipeline/worker.py`
must keep working unchanged.

`tests/test_registry.py` passes today and will still pass if you change
nothing: it is single-threaded, so it cannot see either problem. It is not the
specification. Do not edit anything under `tests/`.
"""

_RACE_FILES = {
    "README.md": """\
# pipeline

A worker pool that shares expensive resources through a registry.

    python tests/test_registry.py
""",
    "pipeline/__init__.py": "",
    "pipeline/registry.py": '''\
"""One shared object per key, built on first use."""


class Registry:
    """A registry of expensive-to-build resources shared by the workers."""

    def __init__(self):
        self._items = {}
        self._builds = 0

    def get_or_create(self, key, factory):
        """Return the object registered for `key`, building it if it is new."""
        if key not in self._items:
            value = factory()
            self._builds += 1
            self._items[key] = value
        return self._items[key]

    def forget(self, key):
        """Drop `key` if it is present. Returns True if something was removed."""
        return self._items.pop(key, None) is not None

    def snapshot(self):
        """Everything currently held, for the status endpoint."""
        return self._items

    @property
    def builds(self):
        """How many times a factory has actually run."""
        return self._builds
''',
    "pipeline/worker.py": '''\
"""The workers that share the registry."""

import threading


def run_pool(registry, keys, factory, threads=8):
    """Ask for every key from `threads` workers at once. Returns the results."""
    results = []
    lock = threading.Lock()

    def work():
        for key in keys:
            value = registry.get_or_create(key, factory)
            with lock:
                results.append((key, value))

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return results


def status(registry):
    """What the status endpoint reports."""
    return {key: type(value).__name__ for key, value in registry.snapshot().items()}
''',
    "tests/test_registry.py": '''\
"""Single-threaded smoke test. Not the specification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.registry import Registry


def test_a_key_is_built_once():
    registry = Registry()
    registry.get_or_create("a", lambda: object())
    registry.get_or_create("a", lambda: object())
    assert registry.builds == 1


def test_forget_removes_a_key():
    registry = Registry()
    registry.get_or_create("a", lambda: object())
    assert registry.forget("a") is True
    assert registry.forget("a") is False


def test_snapshot_lists_the_keys():
    registry = Registry()
    registry.get_or_create("a", lambda: object())
    registry.get_or_create("b", lambda: object())
    assert sorted(registry.snapshot()) == ["a", "b"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
''',
}

_RACE_CHECKS = '''
import threading
import time

from pipeline.registry import Registry
from pipeline.worker import run_pool, status


def slow_factory():
    """Expensive enough that every waiting thread is inside the window."""
    time.sleep(0.05)
    return object()


@case("one key under twelve threads builds exactly once")
def _():
    registry = Registry()
    results = []
    barrier = threading.Barrier(12)

    def work():
        barrier.wait()
        results.append(registry.get_or_create("shared", slow_factory))

    threads = [threading.Thread(target=work) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert registry.builds == 1, f"the factory ran {registry.builds} times for one key"
    assert len(results) == 12, f"only {len(results)} threads returned"
    first = results[0]
    assert all(value is first for value in results), "threads got different objects for one key"


@case("many keys under many threads build once each")
def _():
    registry = Registry()
    keys = [f"k{index}" for index in range(8)]
    results = run_pool(registry, keys, slow_factory, threads=6)
    assert registry.builds == len(keys), f"{len(keys)} keys built {registry.builds} times"
    by_key = {}
    for key, value in results:
        by_key.setdefault(key, value)
        assert by_key[key] is value, f"{key} came back as two different objects"


@case("the status endpoint survives a registry being written")
def _():
    registry = Registry()
    stop = threading.Event()
    failures = []

    def writer():
        for index in range(400):
            if stop.is_set():
                return
            registry.get_or_create(f"w{index}", object)

    def reader():
        while not stop.is_set():
            try:
                status(registry)
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    threads[0].start()
    threads[1].start()
    threads[0].join(30)
    stop.set()
    threads[1].join(30)
    assert not failures, f"reading the registry while it was written raised {failures[0]}"


@case("snapshot hands out a copy, not the live table")
def _():
    registry = Registry()
    registry.get_or_create("a", object)
    taken = registry.snapshot()
    taken["injected"] = object()
    taken.pop("a", None)
    assert "injected" not in registry.snapshot(), "a caller could inject a key through snapshot()"
    assert "a" in registry.snapshot(), "a caller could delete a key through snapshot()"


@case("single-threaded behaviour is unchanged")
def _():
    registry = Registry()
    first = registry.get_or_create("a", object)
    again = registry.get_or_create("a", object)
    assert first is again
    assert registry.builds == 1
    assert registry.forget("a") is True
    assert registry.forget("a") is False
    assert registry.snapshot() == {}
    rebuilt = registry.get_or_create("a", object)
    assert rebuilt is not first, "forget() should let the key be rebuilt"
    assert registry.builds == 2


@case("the public API still has the same shape")
def _():
    registry = Registry()
    for name in ("get_or_create", "forget", "snapshot"):
        assert callable(getattr(registry, name, None)), f"{name} is missing"
    assert isinstance(Registry.builds, property), "builds should still be a property"


@case("the repository's own test still passes")
def _():
    result = cli("tests/test_registry.py")
    assert result.returncode == 0, (result.stdout + result.stderr)[-300:]
'''


# --------------------------------------------------------------- hard/perf


_PERF_BRIEF = """\
The nightly session report has become unusable: on a real day of traffic
(millions of events) `analytics/sessions.py` takes over an hour. It is correct,
just far too slow -- the work it does grows with the square of the input.

Make it fast. `sessionise` should handle a few hundred thousand events in
seconds, not hours.

    python bench_report.py 200000

is in the repository so you can measure before and after; it prints the elapsed
time and a digest of the result.

What must not change:

* The signature `sessionise(events, gap=1800)` and the shape of what it returns
  -- a list of dicts with `user`, `start`, `end` and `events` keys.
* The rules: events are grouped per user, and an event more than `gap` seconds
  after the previous event for that user starts a new session. Exactly `gap`
  seconds is still the same session.
* The order of the returned list: by session start time, and where two sessions
  start at the same moment, by user.
* `top_users` keeps its behaviour and its output ordering.

`python tests/test_sessions.py` passes today and must still pass.
Do not edit anything under `tests/`.
"""

_PERF_FILES = {
    "README.md": """\
# analytics

Turns a stream of events into sessions for the nightly report.

    python tests/test_sessions.py
    python bench_report.py 200000
""",
    "analytics/__init__.py": "",
    "analytics/sessions.py": '''\
"""Group raw events into user sessions."""

DEFAULT_GAP = 1800


def sessionise(events, gap=DEFAULT_GAP):
    """Group `events` into sessions, one list entry per session.

    An event more than `gap` seconds after the previous event for the same user
    opens a new session. Sessions come back ordered by start time, ties broken
    by user.
    """
    sessions = []
    for event in sorted(events, key=lambda row: (row["ts"], row["user"])):
        found = None
        for session in sessions:
            if session["user"] == event["user"] and event["ts"] - session["end"] <= gap:
                found = session
        if found is None:
            sessions.append({
                "user": event["user"],
                "start": event["ts"],
                "end": event["ts"],
                "events": 1,
            })
        else:
            found["end"] = event["ts"]
            found["events"] += 1
    return sessions


def top_users(sessions, limit=10):
    """Users by total session count, most sessions first, ties alphabetical."""
    users = []
    for session in sessions:
        if session["user"] not in users:
            users.append(session["user"])
    counted = []
    for user in users:
        total = 0
        for session in sessions:
            if session["user"] == user:
                total += 1
        counted.append((user, total))
    return sorted(counted, key=lambda row: (-row[1], row[0]))[:limit]
''',
    "bench_report.py": '''\
"""Time the session report on generated traffic.

    python bench_report.py 200000
"""

import random
import sys
import time

from analytics.sessions import sessionise, top_users


def generate(count, users=400, seed=7):
    """A day of plausible traffic: bursty, out of order, repeatable."""
    rng = random.Random(seed)
    events = []
    for index in range(count):
        events.append({
            "user": f"u{rng.randrange(users)}",
            "ts": index * 3 + rng.randrange(3),
            "action": "view",
        })
    rng.shuffle(events)
    return events


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    events = generate(count)

    started = time.perf_counter()
    sessions = sessionise(events)
    elapsed = time.perf_counter() - started

    print(f"events   {count}")
    print(f"sessions {len(sessions)}")
    print(f"top      {top_users(sessions, 3)}")
    print(f"elapsed  {elapsed:.2f}s")


if __name__ == "__main__":
    main()
''',
    "tests/test_sessions.py": '''\
"""Single-scale smoke test. Not the specification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.sessions import sessionise, top_users


def test_a_gap_opens_a_new_session():
    events = [
        {"user": "a", "ts": 0},
        {"user": "a", "ts": 100},
        {"user": "a", "ts": 4000},
    ]
    sessions = sessionise(events, gap=1800)
    assert len(sessions) == 2
    assert sessions[0] == {"user": "a", "start": 0, "end": 100, "events": 2}
    assert sessions[1] == {"user": "a", "start": 4000, "end": 4000, "events": 1}


def test_users_do_not_share_sessions():
    events = [
        {"user": "a", "ts": 0},
        {"user": "b", "ts": 10},
        {"user": "a", "ts": 20},
    ]
    sessions = sessionise(events)
    assert len(sessions) == 2
    assert sessions[0]["user"] == "a" and sessions[0]["events"] == 2


def test_top_users_counts_sessions():
    events = [
        {"user": "a", "ts": 0},
        {"user": "a", "ts": 9000},
        {"user": "b", "ts": 0},
    ]
    assert top_users(sessionise(events)) == [("a", 2), ("b", 1)]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
''',
}

# The timed check runs in its own process so a still-quadratic solution comes
# back as "too slow" instead of hanging the whole verifier.
_PERF_TIMED_DRIVER = """\
import time
from bench_report import generate
from analytics.sessions import sessionise, top_users

events = generate(200000)
started = time.perf_counter()
sessions = sessionise(events)
top = top_users(sessions, 3)
print('ELAPSED', time.perf_counter() - started)
print('SESSIONS', len(sessions))
"""

_PERF_CHECKS = '''
import random
import time

from analytics.sessions import sessionise, top_users


def reference(events, gap=1800):
    """An independent implementation of the documented rules."""
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
                sessions.append({"user": user, "start": start, "end": previous, "events": count})
                start = stamp
                count = 0
            previous = stamp
            count += 1
        sessions.append({"user": user, "start": start, "end": previous, "events": count})
    return sorted(sessions, key=lambda row: (row["start"], row["user"]))


@case("the documented grouping rules still hold")
def _():
    events = [
        {"user": "a", "ts": 0},
        {"user": "a", "ts": 100},
        {"user": "a", "ts": 4000},
        {"user": "b", "ts": 50},
    ]
    got = sessionise(events, gap=1800)
    assert got == reference(events, 1800), got


@case("exactly gap seconds is still the same session")
def _():
    events = [{"user": "a", "ts": 0}, {"user": "a", "ts": 1800}, {"user": "a", "ts": 3601}]
    got = sessionise(events, gap=1800)
    assert len(got) == 2, got
    assert got[0] == {"user": "a", "start": 0, "end": 1800, "events": 2}, got[0]


@case("edge cases behave")
def _():
    assert sessionise([]) == []
    single = sessionise([{"user": "a", "ts": 5}])
    assert single == [{"user": "a", "start": 5, "end": 5, "events": 1}], single
    # Events arriving out of order must not open spurious sessions.
    shuffled = [{"user": "a", "ts": ts} for ts in (900, 0, 450)]
    assert sessionise(shuffled) == [{"user": "a", "start": 0, "end": 900, "events": 3}]


@case("it agrees with an independent implementation on messy input")
def _():
    rng = random.Random(11)
    events = [{"user": f"u{rng.randrange(40)}", "ts": rng.randrange(200000)} for _ in range(4000)]
    got = sessionise(events, gap=1800)
    expected = reference(events, 1800)
    assert got == expected, f"{len(got)} sessions, expected {len(expected)}"


@case("top_users is unchanged")
def _():
    events = [{"user": "a", "ts": 0}, {"user": "a", "ts": 9000}, {"user": "b", "ts": 0}]
    sessions = sessionise(events)
    assert top_users(sessions) == [("a", 2), ("b", 1)]
    assert top_users(sessions, 1) == [("a", 2)]


@case("the signature was not changed")
def _():
    import inspect

    parameters = list(inspect.signature(sessionise).parameters)
    assert parameters[:2] == ["events", "gap"], parameters
    assert inspect.signature(sessionise).parameters["gap"].default == 1800


@case("200k events finish in seconds, not hours")
def _():
    driver = %(driver)r
    try:
        result = subprocess.run([sys.executable, "-c", driver], cwd=WORKSPACE,
                                capture_output=True, text=True, timeout=45,
                                creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AssertionError("200k events had still not finished after 45s; this is still quadratic")
    assert result.returncode == 0, (result.stdout + result.stderr)[-300:]
    elapsed = float(next(line for line in result.stdout.splitlines()
                         if line.startswith("ELAPSED")).split()[1])
    assert elapsed < 25, f"200k events took {elapsed:.1f}s"


@case("the repository's own test still passes")
def _():
    result = cli("tests/test_sessions.py")
    assert result.returncode == 0, (result.stdout + result.stderr)[-300:]
''' % {"driver": _PERF_TIMED_DRIVER}


# ------------------------------------------------------------- hard/rename


_RENAME_BRIEF = """\
Rename the stage entry point in this project from `transform` to `apply_stage`,
and make its `options` argument keyword-only.

So `transform(payload, options)` becomes `apply_stage(payload, *, options)`
everywhere it is defined and everywhere it is called.

When you are done:

* The name `transform` must not appear anywhere in the project's Python source
  or its JSON config -- not as a definition, not as a call, not as a string.
* `python run.py sample.json` must print exactly what it prints today.
* Every stage must still be reachable the way the loader reaches it. Note that
  the loader finds stages by name at runtime, and that `plugins.json` does not
  spell the entry point out for every stage.

Nothing about the pipeline's behaviour changes. This is a rename and a
signature change, and it has to be complete.
"""

_RENAME_FILES = {
    "README.md": """\
# stages

A tiny plugin pipeline. `plugins.json` lists the stages; each stage module
exposes an entry point that the loader looks up by name.

    python run.py sample.json
""",
    "plugins.json": """\
{
  "stages": [
    {"module": "stages.clean", "entry": "transform"},
    {"module": "stages.enrich"},
    {"module": "stages.export", "entry": "transform"}
  ]
}
""",
    "sample.json": """\
{"name": "  ada lovelace  ", "city": "  stockholm  "}
""",
    "core/__init__.py": "",
    "core/pipeline.py": '''\
"""The shared rule applier every stage is built on."""


def transform(payload, options):
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
DEFAULT_ENTRY = "transform"


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

from core.pipeline import transform

BUILTINS = {"identity": transform}


def describe():
    """The names of the built-in stages."""
    return sorted(BUILTINS)
''',
    "stages/__init__.py": "",
    "stages/clean.py": '''\
"""Trim the text fields."""

from core import pipeline

RULES = {"name": str.strip, "city": str.strip}


def transform(payload, options):
    """Stage entry point."""
    return pipeline.transform(payload, RULES)
''',
    "stages/enrich.py": '''\
"""Title-case the name and derive the initials."""

from core import pipeline


def transform(payload, options):
    """Stage entry point."""
    enriched = pipeline.transform(payload, {"name": str.title})
    enriched["initials"] = "".join(word[0] for word in enriched["name"].split())
    return enriched
''',
    "stages/export.py": '''\
"""Render the finished record as one line."""

from core import pipeline


def transform(payload, options):
    """Stage entry point."""
    row = pipeline.transform(payload, {"city": str.upper})
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
        payload = entry(payload, None)
    print(payload["line"])


if __name__ == "__main__":
    main()
''',
}

_RENAME_CHECKS = '''
import inspect
import json
import re
from pathlib import Path

EXPECTED_LINE = "Ada Lovelace (AL) - STOCKHOLM"


@case("the pipeline still produces the same line")
def _():
    result = cli("run.py", "sample.json")
    assert result.returncode == 0, (result.stdout + result.stderr)[-400:]
    assert result.stdout.strip() == EXPECTED_LINE, repr(result.stdout)


@case("every stage defines apply_stage and no longer defines transform")
def _():
    import importlib

    for name in ("core.pipeline", "stages.clean", "stages.enrich", "stages.export"):
        module = importlib.import_module(name)
        assert callable(getattr(module, "apply_stage", None)), f"{name}.apply_stage is missing"
        assert not hasattr(module, "transform"), f"{name}.transform is still there"


@case("options is keyword-only")
def _():
    from core.pipeline import apply_stage

    parameters = inspect.signature(apply_stage).parameters
    assert list(parameters) == ["payload", "options"], list(parameters)
    assert parameters["options"].kind is inspect.Parameter.KEYWORD_ONLY, \\
        "options is still positional"
    try:
        apply_stage({"a": "b"}, {})
    except TypeError:
        pass
    else:
        raise AssertionError("a positional options argument should now be a TypeError")


@case("the stages are still reachable the way the loader reaches them")
def _():
    from core.loader import load_stages

    stages = load_stages(str(Path(WORKSPACE) / "plugins.json"))
    assert [name for name, _entry in stages] == ["stages.clean", "stages.enrich", "stages.export"], stages
    for name, entry in stages:
        assert entry.__name__ == "apply_stage", f"{name} resolved to {entry.__name__}"


@case("the stage that does not name its entry point still resolves")
def _():
    # plugins.json leaves stages.enrich to the loader's default, so the default
    # has to have been renamed too.
    from core import loader

    assert loader.DEFAULT_ENTRY == "apply_stage", f"DEFAULT_ENTRY is {loader.DEFAULT_ENTRY!r}"


@case("the built-in registry points at the renamed function")
def _():
    from core.registry import BUILTINS, describe

    assert describe() == ["identity"]
    assert BUILTINS["identity"].__name__ == "apply_stage"


@case("the name transform is gone from the source and the config")
def _():
    stale = []
    for path in sorted(Path(WORKSPACE).rglob("*")):
        if path.suffix not in {".py", ".json"} or ".git" in path.parts:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(body.splitlines(), 1):
            if re.search(r"\\btransform\\b", line):
                stale.append(f"{path.relative_to(WORKSPACE)}:{number}: {line.strip()[:70]}")
    assert not stale, "transform still appears in " + "; ".join(stale[:4])
'''


HARD_FIXTURES: tuple[Task, ...] = (
    Task(
        id="hard/race",
        suite="hard",
        title="A registry two threads can share",
        brief=_RACE_BRIEF,
        files=_RACE_FILES,
        checks=_RACE_CHECKS,
        protected=("tests/test_registry.py",),
    ),
    Task(
        id="hard/perf",
        suite="hard",
        title="A report that takes an hour",
        brief=_PERF_BRIEF,
        files=_PERF_FILES,
        checks=_PERF_CHECKS,
        protected=("tests/test_sessions.py",),
    ),
    Task(
        id="hard/rename",
        suite="hard",
        title="One rename, eleven call sites",
        brief=_RENAME_BRIEF,
        files=_RENAME_FILES,
        checks=_RENAME_CHECKS,
    ),
)
