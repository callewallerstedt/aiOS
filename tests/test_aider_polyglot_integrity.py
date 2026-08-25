"""Offline regression checks for the immutable Aider Polyglot fixture."""

from bench import aider_polyglot


OFFICIAL_COMMIT = "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"
OFFICIAL_RAW_SHA256 = {
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


def test_manifest_is_the_exact_official_raw_fixture():
    assert aider_polyglot.UPSTREAM_COMMIT == OFFICIAL_COMMIT
    assert aider_polyglot.UPSTREAM_RAW.endswith(f"/{OFFICIAL_COMMIT}")
    assert aider_polyglot.UPSTREAM_SHA256 == OFFICIAL_RAW_SHA256


def test_every_manifest_path_has_one_pinned_digest():
    manifest_paths = [
        upstream_path
        for exercise in aider_polyglot.EXERCISES
        for upstream_path in aider_polyglot.file_manifest(exercise).values()
    ]
    assert len(manifest_paths) == 22
    assert len(set(manifest_paths)) == 22
    assert set(manifest_paths) == set(aider_polyglot.UPSTREAM_SHA256)


def test_verifier_runs_unittest_instead_of_executing_definition_only_files():
    for exercise in aider_polyglot.EXERCISES:
        checks = aider_polyglot._checks(exercise)
        assert 'cli("-m", "unittest",' in checks
        assert repr(exercise.test_file) in checks
        assert f"python -m unittest {exercise.test_file}" in aider_polyglot._brief(exercise)
