"""Pinned Aider Polyglot Python tasks for cheap harness regression runs.

This is deliberately a fixed six-task subset, not an implementation of the
official 225-task Aider leaderboard protocol.  The exercise files are fetched
from one immutable upstream commit and verified before they enter the cache.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import DATA_DIR

if TYPE_CHECKING:
    from .suites import Task


UPSTREAM_REPOSITORY = "https://github.com/Aider-AI/polyglot-benchmark"
UPSTREAM_COMMIT = "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"
UPSTREAM_RAW = f"https://raw.githubusercontent.com/Aider-AI/polyglot-benchmark/{UPSTREAM_COMMIT}"
CACHE_DIR = DATA_DIR / "aider-polyglot" / UPSTREAM_COMMIT
SUBSET_NOTE = (
    "Fixed six-task Python subset for aiOS harness regression; it is not "
    "comparable to the official 225-task Aider Polyglot leaderboard."
)


@dataclass(frozen=True)
class Exercise:
    slug: str
    title: str
    module: str
    test_file: str
    append_instructions: bool = True

    @property
    def upstream_root(self) -> str:
        return f"python/exercises/practice/{self.slug}"


EXERCISES = (
    Exercise("phone-number", "Phone Number", "phone_number.py", "phone_number_test.py"),
    Exercise("wordy", "Wordy", "wordy.py", "wordy_test.py"),
    Exercise("bowling", "Bowling", "bowling.py", "bowling_test.py"),
    Exercise("forth", "Forth", "forth.py", "forth_test.py"),
    Exercise("poker", "Poker", "poker.py", "poker_test.py", False),
    Exercise("zipper", "Zipper", "zipper.py", "zipper_test.py", False),
)


def file_manifest(exercise: Exercise) -> dict[str, str]:
    """Map workspace paths to the exact files in the pinned upstream tree."""
    root = exercise.upstream_root
    manifest = {
        ".docs/instructions.md": f"{root}/.docs/instructions.md",
        exercise.module: f"{root}/{exercise.module}",
        exercise.test_file: f"{root}/{exercise.test_file}",
    }
    if exercise.append_instructions:
        manifest[".docs/instructions.append.md"] = f"{root}/.docs/instructions.append.md"
    return manifest


# SHA-256 of the raw, LF-terminated Git blobs at UPSTREAM_COMMIT.  Do not hash
# a text-mode checkout here: core.autocrlf may rewrite the bytes on Windows.
# Verifying both downloads and cache hits prevents a mutable network response
# or damaged cache from quietly changing what two harness runs compare.
UPSTREAM_SHA256 = {
    "python/exercises/practice/phone-number/.docs/instructions.md":
        "ca1e2c1c43454c151e519a2f784439621c243bb2824715c69a8e0904abc473a5",
    "python/exercises/practice/phone-number/.docs/instructions.append.md":
        "c093e2195d15a182ea018a10043a58945b5238d8f1d9a8d10dade09a7c70e5de",
    "python/exercises/practice/phone-number/phone_number.py":
        "be4018194beaf2c67df5d95e277d5cf7a7344c03a3c64d283aabcf0a2a0480d7",
    "python/exercises/practice/phone-number/phone_number_test.py":
        "68c6fc5c281f12eb0a0d5f4e9c963e33b5f1a69576cccce2c712bec9788b6c8e",
    "python/exercises/practice/wordy/.docs/instructions.md":
        "b9e24dd95b27fa04f706e4dd9e5c7f430684b1af13a1cfde34faee705300aeee",
    "python/exercises/practice/wordy/.docs/instructions.append.md":
        "485af0a0036cda1cf3415813f8a8317472b56de1af6d9a3e591d38a1af015e19",
    "python/exercises/practice/wordy/wordy.py":
        "3a8e9cf28b599898ff62c4714ad747b95ec84e8e04034b3dbf14b9f40afe0ee1",
    "python/exercises/practice/wordy/wordy_test.py":
        "3c8bf5b17e14c8f8953107c0ae9600be0c0d96e578cf442526dbedf0c57889da",
    "python/exercises/practice/bowling/.docs/instructions.md":
        "38a7d249928abfaac3e24d47222283a9bc3a6c5c599d9cea1d43bbe55eb8de1c",
    "python/exercises/practice/bowling/.docs/instructions.append.md":
        "51b23a84cccf816778935b8dce09c0f88fc0deda3bdc896152a2c65ec9d341fc",
    "python/exercises/practice/bowling/bowling.py":
        "a356c0682b6c0c04e9e30a7da603b2014c227426b43bbe37f34cd99913d368b6",
    "python/exercises/practice/bowling/bowling_test.py":
        "6b9a80b6835828f575ce0aba66876850f75c59f764014ac509fe1df7e3a9ee20",
    "python/exercises/practice/forth/.docs/instructions.md":
        "06875da79159e3783cde3d006e405f8329e16837b09b0b4b8225efc3d27a6d9c",
    "python/exercises/practice/forth/.docs/instructions.append.md":
        "ec3a69fad548faa5f17b2ef73cebfdd06759225c1b0f4ed805c7e0a630cdcd77",
    "python/exercises/practice/forth/forth.py":
        "9acd281e02feff4fc5ae766063a1ce1290b625bb930cc8f54c15a0109610e562",
    "python/exercises/practice/forth/forth_test.py":
        "05f78a51ce5b3e18439088442925c7cea5b1fb1703bc10832dd048c057b7639c",
    "python/exercises/practice/poker/.docs/instructions.md":
        "62427ba25c07f8c57519f93cbfc293dd3eb9e8b74a33aae3645281433b8941ea",
    "python/exercises/practice/poker/poker.py":
        "6a4b5adab3ba9261fb4f9e2b09abc549b9ea37bf0774edc3c7759cec9a8b41fe",
    "python/exercises/practice/poker/poker_test.py":
        "b39f023208318973e18a341449aa076f7595ff98f07174082a2a1b05c84ad8fd",
    "python/exercises/practice/zipper/.docs/instructions.md":
        "30c3fa8f9de3afdd691f3ff72e0407581da6676426ad7f175ef1553504df6066",
    "python/exercises/practice/zipper/zipper.py":
        "66276107d448f53a12509da6f503280e0dca4a6bbd9d690a774fb0972a55f926",
    "python/exercises/practice/zipper/zipper_test.py":
        "a3cf6e5ebcf6b2171bcc80fa0ce81b425af23b522f222e841fa2abd3119e95b2",
}


SUITE_PROVENANCE = {
    "benchmark": "Aider Polyglot",
    "repository": UPSTREAM_REPOSITORY,
    "commit": UPSTREAM_COMMIT,
    "language": "python",
    "tasks": [exercise.slug for exercise in EXERCISES],
    "subset": True,
    "leaderboard_comparable": False,
}


def cache_path(upstream_path: str) -> Path:
    """Return the cache target for a known pinned file, rejecting arbitrary paths."""
    if upstream_path not in UPSTREAM_SHA256:
        raise ValueError(f"unknown Aider Polyglot file: {upstream_path}")
    return CACHE_DIR.joinpath(*upstream_path.split("/"))


def _verified(payload: bytes, upstream_path: str) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    expected = UPSTREAM_SHA256[upstream_path]
    if digest != expected:
        raise RuntimeError(
            f"Aider Polyglot integrity check failed for {upstream_path}: "
            f"expected {expected}, got {digest}"
        )
    return payload


def _cache_atomically(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
        os.replace(temporary, target)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def load_pinned_text(upstream_path: str) -> str:
    """Read one exact upstream file, downloading it only when not cached."""
    target = cache_path(upstream_path)
    try:
        payload = target.read_bytes()
    except FileNotFoundError:
        request = urllib.request.Request(
            f"{UPSTREAM_RAW}/{upstream_path}",
            headers={"User-Agent": "aiOS-benchmark/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except Exception as exc:
            raise RuntimeError(f"could not download pinned Aider Polyglot file {upstream_path}: {exc}") from exc
        _verified(payload, upstream_path)
        _cache_atomically(target, payload)
    return _verified(payload, upstream_path).decode("utf-8")


def _brief(exercise: Exercise) -> str:
    return f"""\
Complete the pinned upstream Aider Polyglot Python exercise in `{exercise.module}`.

Read `.docs/instructions.md` and, when present,
`.docs/instructions.append.md`; together they are the exact exercise
specification. Run
`python -m unittest {exercise.test_file}` while you work. Do not edit either
instruction file or `{exercise.test_file}`.

This is {SUBSET_NOTE[0].lower() + SUBSET_NOTE[1:]}
"""


def _checks(exercise: Exercise) -> str:
    return f'''\
@case("upstream {exercise.test_file} passes")
def upstream_unittest_file_passes():
    result = cli("-m", "unittest", {exercise.test_file!r}, timeout=120)
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    assert result.returncode == 0, detail[-300:] or f"unittest exited {{result.returncode}}"
'''


def _readme(exercise: Exercise) -> str:
    source = f"{UPSTREAM_REPOSITORY}/tree/{UPSTREAM_COMMIT}/{exercise.upstream_root}"
    return f"""\
# Aider Polyglot Python: {exercise.title}

Source: {source}

The instruction files, starter module, and unittest are byte-pinned to the
commit above. Only `{exercise.module}` is part of the solution.

{SUBSET_NOTE}
"""


def tasks(limit: int | None = None) -> tuple["Task", ...]:
    """Materialise the deterministic prefix requested by the suite picker."""
    from .suites import Task

    count = len(EXERCISES) if limit is None else max(0, min(len(EXERCISES), int(limit)))
    selected: list[Task] = []
    for exercise in EXERCISES[:count]:
        manifest = file_manifest(exercise)
        files = {name: load_pinned_text(path) for name, path in manifest.items()}
        files["README.md"] = _readme(exercise)
        source = f"{UPSTREAM_REPOSITORY}/tree/{UPSTREAM_COMMIT}/{exercise.upstream_root}"
        selected.append(Task(
            id=f"aider_polyglot/{exercise.slug}",
            suite="aider_polyglot",
            title=exercise.title,
            brief=_brief(exercise),
            files=files,
            checks=_checks(exercise),
            protected=tuple(name for name in manifest if name.startswith(".docs/"))
            + (exercise.test_file,),
            source=source,
            provenance={**SUITE_PROVENANCE, "exercise": exercise.slug},
        ))
    return tuple(selected)


__all__ = [
    "CACHE_DIR",
    "EXERCISES",
    "SUBSET_NOTE",
    "SUITE_PROVENANCE",
    "UPSTREAM_COMMIT",
    "UPSTREAM_REPOSITORY",
    "UPSTREAM_SHA256",
    "cache_path",
    "file_manifest",
    "load_pinned_text",
    "tasks",
]
