from code_verification import VerificationLedger, classify_command


def test_clean_ledger_allows_completion():
    ledger = VerificationLedger()

    result = ledger.decision("planned")

    assert result["allowed"] is True
    assert result["state"] == "clean"
    assert ledger.snapshot()["changed_path_hashes"] == {}


def test_non_code_only_changes_are_exempt():
    ledger = VerificationLedger()
    ledger.mark_mutation("README.md", "docs-1")
    ledger.mark_mutation("aios_ui/web/icon.svg", "asset-1")

    result = ledger.decision("distributed")

    assert result["allowed"] is True
    assert result["state"] == "passed"
    assert result["source_paths"] == []
    assert set(result["non_code_paths"]) == {"README.md", "aios_ui/web/icon.svg"}


def test_unknown_extension_is_conservatively_treated_as_source():
    ledger = VerificationLedger()
    ledger.mark_mutation("build/Project.CustomRules", "rules-1")

    result = ledger.decision("direct")

    assert result["allowed"] is False
    assert result["state"] == "unverified"


def test_direct_edit_accepts_fresh_automatic_syntax_diagnostics():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "code_jobs.py",
        "python-1",
        diagnostic_status="passed",
        diagnostic_checker="python -m py_compile",
    )
    ledger.mark_mutation(
        "aios_ui/web/js/code.js",
        "js-1",
        diagnostic_status="clean",
        diagnostic_checker="node --check",
    )

    direct = ledger.decision("direct")
    planned = ledger.decision("planned")

    assert direct["allowed"] is True
    assert direct["state"] == "passed"
    assert direct["automatic_diagnostics_passed"] is True
    assert planned["allowed"] is False
    assert planned["state"] == "unverified"
    assert planned["requires_explicit_verification"] is True


def test_planned_diagnostic_only_explains_that_behavioral_evidence_is_missing():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "aios_ui/web/js/code.js",
        "js-1",
        diagnostic_status="passed",
        diagnostic_checker="node --check",
    )

    result = ledger.decision("planned")

    assert result["allowed"] is False
    assert result["state"] == "unverified"
    assert "syntax diagnostics passed" in result["reason"]
    assert "focused behavior" in result["reason"]


def test_automatic_diagnostic_requires_checker_and_all_source_paths():
    ledger = VerificationLedger()
    ledger.mark_mutation("one.py", "one", "passed", "py_compile")
    ledger.mark_mutation("two.py", "two", "passed", "")

    result = ledger.decision("direct")

    assert result["allowed"] is False
    assert result["automatic_diagnostics_passed"] is False


def test_planned_and_distributed_source_changes_require_explicit_pass():
    for strategy in ("planned", "distributed"):
        ledger = VerificationLedger()
        ledger.mark_mutation("code_jobs.py", "source-1", "passed", "py_compile")
        evidence = ledger.record_command(
            "python -m pytest tests/test_code_jobs.py -q",
            0,
            "12 passed",
            1.25,
        )

        result = ledger.decision(strategy)

        assert evidence["kind"] == "test"
        assert result["allowed"] is True
        assert result["state"] == "passed"
        assert result["passing_evidence_count"] == 1


def test_non_verification_command_cannot_unlock_completion():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")
    evidence = ledger.record_command("git status --short", 0, "M code_jobs.py")

    result = ledger.decision("direct")

    assert evidence["kind"] == "non_verification"
    assert evidence["status"] == "ignored"
    assert result["allowed"] is False
    assert result["state"] == "unverified"


def test_shell_masking_and_metadata_commands_are_not_verification():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")

    masked = ledger.record_command("pytest tests/test_code_jobs.py || exit 0", 0)
    metadata = ledger.record_command("pytest --collect-only", 0)

    assert masked["kind"] == "non_verification"
    assert metadata["kind"] == "non_verification"
    assert ledger.decision("planned")["allowed"] is False


def test_recognizes_core_verification_kinds():
    cases = {
        "python -m pytest tests/test_code_jobs.py -q": "test",
        "pytest tests/test_code_jobs.py -v": "test",
        "npm.cmd run lint": "lint",
        "pyright code_jobs.py": "typecheck",
        "pnpm run build": "build",
        "python -m py_compile code_jobs.py": "syntax",
    }

    for command, expected in cases.items():
        ledger = VerificationLedger()
        record = ledger.record_command(command, 0)
        assert record["kind"] == expected
        assert record["verification"] is True


def test_read_only_powershell_exit_report_preserves_verification_and_native_status():
    success = VerificationLedger().record_command(
        'npm test 2>&1; echo "TEST_EXIT=$LASTEXITCODE"',
        0,
        "11 tests passed\nTEST_EXIT=0",
    )
    failure = VerificationLedger().record_command(
        'npm run build 2>&1; Write-Output "BUILD_STATUS=$LASTEXITCODE"',
        0,
        "build failed\nBUILD_STATUS=7",
    )
    missing = VerificationLedger().record_command(
        'python -m pytest -q; echo "PYTEST_EXIT=$LASTEXITCODE"',
        0,
        "marker was unexpectedly omitted",
    )

    assert success["kind"] == "test" and success["status"] == "passed"
    assert success["exit_code"] == 0
    assert failure["kind"] == "build" and failure["status"] == "failed"
    assert failure["exit_code"] == 7
    assert missing["kind"] == "test" and missing["status"] == "failed"
    assert missing["exit_code"] == -1


def test_arbitrary_compound_commands_remain_non_verification():
    for command in (
        "npm test; echo done",
        "npm test && exit 0",
        'npm test; echo "EXIT=0"',
        'npm test; echo "EXIT=$?"',
    ):
        assert classify_command(command) == "non_verification"


def test_conventional_interpreter_test_scripts_are_verification():
    for command in (
        "python tests/test_pricing.py",
        "python test.py",
        "python feature_test.py",
        "node tests/session.test.js",
        "node session.test.js",
        "node session.spec.js",
    ):
        assert VerificationLedger().record_command(command, 0)["kind"] == "test"


def test_ordinary_interpreter_scripts_are_not_verification():
    for command in (
        "python run_server.py",
        "python deploy.py",
        "node server.js",
        "node build-assets.js",
        "python tests/generate_fixtures.py",
        "node tests/build-fixtures.js",
    ):
        evidence = VerificationLedger().record_command(command, 0)
        assert evidence["kind"] == "non_verification"
        assert evidence["verification"] is False


def test_artifact_backed_script_is_ad_hoc_verification():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")

    evidence = ledger.record_command(
        r"python C:\Temp\aios_verify_123.py",
        0,
        "ok",
        artifact_path=r"C:\Temp\aios_verify_123.py",
    )

    assert evidence["kind"] == "ad_hoc"
    assert ledger.decision("planned")["allowed"] is True


def test_failed_command_remains_failed_until_same_command_passes():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")
    command = "python -m pytest tests/test_code_jobs.py -q"
    ledger.record_command(command, 1, "1 failed")
    ledger.record_command("ruff check code_jobs.py", 0, "All checks passed")

    unresolved = ledger.decision("planned")
    ledger.record_command(command, 0, "10 passed")
    resolved = ledger.decision("planned")

    assert unresolved["allowed"] is False
    assert unresolved["state"] == "failed"
    assert unresolved["failing_evidence_count"] == 1
    assert resolved["allowed"] is True
    assert resolved["state"] == "passed"


def test_fresh_failed_diagnostic_blocks_even_with_passing_command():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1", "failed", "py_compile")
    ledger.record_command("ruff check code_jobs.py", 0, "All checks passed")

    result = ledger.decision("direct")

    assert result["allowed"] is False
    assert result["state"] == "failed"
    assert result["failed_diagnostic_paths"] == ["code_jobs.py"]


def test_evidence_becomes_stale_after_any_later_generation():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")
    ledger.record_command("python -m py_compile code_jobs.py", 0)
    assert ledger.decision("planned")["state"] == "passed"

    ledger.mark_mutation("README.md", "docs-2")
    stale = ledger.decision("planned")

    assert stale["allowed"] is False
    assert stale["state"] == "stale"
    assert stale["generation"] == 2


def test_changed_path_hashes_track_latest_content_per_path():
    ledger = VerificationLedger()
    ledger.mark_mutation(r"aios_ui\web\js\code.js", "hash-1")
    ledger.mark_mutation("aios_ui/web/js/code.js", "hash-2")

    snapshot = ledger.snapshot()

    assert snapshot["generation"] == 2
    assert snapshot["changed_path_hashes"] == {"aios_ui/web/js/code.js": "hash-2"}
    assert snapshot["paths"][0]["mutation_count"] == 2


def test_completion_gate_allows_two_continuations_then_exhausts():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")

    first = ledger.block_completion("planned", max_attempts=2)
    second = ledger.block_completion("planned", max_attempts=2)
    exhausted = ledger.block_completion("planned", max_attempts=2)

    assert first["allowed"] is False
    assert first["blocked"] is True
    assert first["continuation"] is True
    assert first["attempt"] == 1
    assert second["blocked"] is True
    assert second["attempt"] == 2
    assert exhausted["allowed"] is False
    assert exhausted["blocked"] is False
    assert exhausted["continuation"] is False
    assert exhausted["exhausted"] is True
    assert ledger.snapshot()["completion_blocks"] == 2


def test_success_bypasses_completion_block_without_consuming_attempt():
    ledger = VerificationLedger()
    ledger.mark_mutation("code_jobs.py", "source-1")
    ledger.record_command("python -m py_compile code_jobs.py", 0)

    result = ledger.block_completion("planned")

    assert result["allowed"] is True
    assert result["blocked"] is False
    assert result["exhausted"] is False
    assert result["attempt"] == 0


def test_failed_current_generation_evidence_precedes_clean_and_non_code_exemptions():
    clean = VerificationLedger()
    clean.record_command("python -m pytest", 1, "collection failed")

    docs = VerificationLedger()
    docs.mark_mutation("README.md", "docs-1")
    docs.record_command("python -m pytest", 1, "1 failed")

    assert clean.decision("planned")["state"] == "failed"
    assert clean.decision("planned")["source_paths"] == []
    assert docs.decision("direct")["state"] == "failed"


def test_failed_svg_diagnostic_blocks_asset_exemption():
    ledger = VerificationLedger()
    ledger.mark_mutation("aios_ui/web/icon.svg", "asset-1", "failed", "xml")

    result = ledger.decision("direct")

    assert result["allowed"] is False
    assert result["state"] == "failed"
    assert result["failed_diagnostic_paths"] == ["aios_ui/web/icon.svg"]


def test_direct_content_and_presentation_edits_are_exempt():
    ledger = VerificationLedger()
    for index, path in enumerate(
        ("web/site.css", "web/index.html", "README.md", "slides/demo.pptx"),
        1,
    ):
        ledger.mark_mutation(path, f"hash-{index}")

    direct = ledger.decision("direct")
    planned = ledger.decision("planned")

    assert direct["allowed"] is True
    assert direct["state"] == "passed"
    assert direct["direct_exempt_paths"] == ["web/index.html", "web/site.css"]
    assert set(direct["non_code_paths"]) == {"README.md", "slides/demo.pptx"}
    assert planned["allowed"] is False


def test_unrelated_path_bound_checks_do_not_unlock_changed_source():
    cases = (
        ("python -m py_compile unrelated.py", "syntax"),
        ("ruff check unrelated.py", "lint"),
        ("pyright unrelated.py", "typecheck"),
    )
    for command, kind in cases:
        ledger = VerificationLedger()
        ledger.mark_mutation("src/changed.py", "source-1")

        evidence = ledger.record_command(command, 0)
        result = ledger.decision("planned")

        assert evidence["kind"] == kind
        assert evidence["coverage_mode"] == "targets"
        assert evidence["coverage_targets"] == ["unrelated.py"]
        assert result["allowed"] is False
        assert result["state"] == "unverified"
        assert result["passing_evidence_count"] == 0
        assert result["ignored_passing_evidence_count"] == 1
        assert result["verification_covered_paths"] == []


def test_targeted_check_union_must_cover_every_changed_source():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/one.py", "one")
    ledger.mark_mutation("src/two.py", "two")
    ledger.record_command("python -m py_compile src/one.py", 0)

    partial = ledger.decision("planned")
    ledger.record_command("ruff check src/two.py", 0)
    complete = ledger.decision("planned")

    assert partial["allowed"] is False
    assert partial["verification_covered_paths"] == ["src/one.py"]
    assert complete["allowed"] is True
    assert complete["passing_evidence_count"] == 2
    assert complete["verification_covered_paths"] == ["src/one.py", "src/two.py"]


def test_targeted_directory_covers_changed_descendants():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/one.py", "one")
    ledger.mark_mutation("src/lib/two.py", "two")

    evidence = ledger.record_command("pyright src", 0)
    result = ledger.decision("planned")

    assert evidence["coverage_targets"] == ["src"]
    assert result["allowed"] is True


def test_project_wide_checker_without_explicit_targets_remains_broad():
    ledger = VerificationLedger()
    ledger.mark_mutation("web/app.ts", "source-1")

    evidence = ledger.record_command("npm.cmd run lint", 0)

    assert evidence["coverage_mode"] == "all"
    assert ledger.decision("planned")["allowed"] is True


def test_bare_target_does_not_cover_same_basename_in_another_directory():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/app.py", "source-1")

    ledger.record_command("python -m py_compile app.py", 0)

    assert ledger.decision("planned")["allowed"] is False


def test_make_clean_is_not_verification():
    for command in ("make clean", "nmake /f Makefile clean", "make distclean"):
        evidence = VerificationLedger().record_command(command, 0)
        assert evidence["kind"] == "non_verification"
        assert evidence["verification"] is False

    assert VerificationLedger().record_command("make clean all", 0)["kind"] == "build"


def test_snapshot_round_trip_revalidates_coverage_metadata():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/app.py", "source-1")
    ledger.record_command("python -m py_compile src/app.py", 0, "ok", 0.25)
    snapshot = ledger.snapshot()

    restored = VerificationLedger.from_snapshot(snapshot)

    assert snapshot["schema_version"] == 4
    assert restored.snapshot() == snapshot
    assert restored.decision("planned")["allowed"] is True


def test_schema_one_restore_derives_target_coverage_instead_of_trusting_old_evidence():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/changed.py", "source-1")
    ledger.record_command("python -m py_compile unrelated.py", 0)
    snapshot = ledger.snapshot()
    snapshot["schema_version"] = 1
    for evidence in snapshot["evidence"]:
        evidence.pop("coverage_mode", None)
        evidence.pop("coverage_targets", None)

    restored = VerificationLedger.from_snapshot(snapshot)
    decision = restored.decision("planned")

    assert restored.snapshot()["evidence"][0]["coverage_targets"] == ["unrelated.py"]
    assert decision["allowed"] is False
    assert decision["ignored_passing_evidence_count"] == 1


def test_new_turn_carries_session_changes_without_blocking_an_unrelated_followup():
    ledger = VerificationLedger()
    ledger.mark_mutation("src/app.py", "source-1")
    ledger.block_completion("planned", max_attempts=2)
    ledger.block_completion("planned", max_attempts=2)
    snapshot = ledger.snapshot()

    same_turn = VerificationLedger.from_snapshot(snapshot)
    new_turn = VerificationLedger.from_snapshot(snapshot, new_turn=True)

    assert same_turn.snapshot()["completion_blocks"] == 2
    assert new_turn.snapshot()["completion_blocks"] == 0
    assert new_turn.generation == ledger.generation
    assert new_turn.snapshot()["changed_path_hashes"] == {"src/app.py": "source-1"}
    decision = new_turn.block_completion("planned", max_attempts=2)
    assert decision["allowed"] is True
    assert decision["attempt"] == 0
    assert decision["source_paths"] == []
    assert decision["carried_source_paths"] == ["src/app.py"]
    assert new_turn.snapshot()["current_changed_path_hashes"] == {}
    assert new_turn.snapshot()["carried_path_hashes"] == {"src/app.py": "source-1"}

    same_turn.begin_turn()
    assert same_turn.snapshot()["completion_blocks"] == 0


def test_mutating_a_carried_path_makes_it_current_and_requires_fresh_proof():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "src/app.py", "session-change", "passed", "python-ast", previous_hash="original"
    )
    carried = VerificationLedger.from_snapshot(ledger.snapshot(), new_turn=True)

    carried.mark_mutation(
        "src/app.py", "followup-change", "passed", "python-ast", previous_hash="session-change"
    )

    decision = carried.decision("planned")
    assert decision["allowed"] is False
    assert decision["source_paths"] == ["src/app.py"]
    assert decision["carried_source_paths"] == []
    assert carried.snapshot()["current_changed_path_hashes"] == {
        "src/app.py": "followup-change"
    }


def test_reverting_a_followup_edit_restores_the_carried_session_change():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "src/app.py", "session-change", "passed", "python-ast", previous_hash="original"
    )
    carried = VerificationLedger.from_snapshot(ledger.snapshot(), new_turn=True)
    carried.mark_mutation(
        "src/app.py", "temporary", "passed", "python-ast", previous_hash="session-change"
    )
    carried.mark_mutation(
        "src/app.py", "session-change", "passed", "python-ast", previous_hash="temporary"
    )

    decision = carried.decision("planned")
    assert decision["allowed"] is True
    assert decision["source_paths"] == []
    assert decision["carried_source_paths"] == ["src/app.py"]
    assert carried.snapshot()["changed_path_hashes"] == {"src/app.py": "session-change"}


def test_same_content_rewrite_does_not_advance_generation_or_stale_evidence():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "src/app.py", "changed", "passed", "python-ast", previous_hash="original"
    )
    ledger.record_command("python -m py_compile src/app.py", 0)
    generation = ledger.generation

    result = ledger.mark_mutation(
        "src/app.py", "changed", "passed", "python-ast", previous_hash="changed"
    )

    assert result["unchanged"] is True
    assert ledger.generation == generation
    assert ledger.decision("planned")["allowed"] is True


def test_new_file_deleted_again_is_removed_from_net_turn_mutations():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "app.js", "app-new", "passed", "node-check", previous_hash="app-old"
    )
    ledger.mark_mutation(
        "scratch_test.py", "scratch-new", "passed", "python-ast", previous_hash="deleted"
    )
    ledger.record_command("python scratch_test.py", 0, explicit_verification=True)
    generation = ledger.generation

    reverted = ledger.mark_mutation("scratch_test.py", "deleted", previous_hash="scratch-new")
    decision = ledger.decision("direct")

    assert reverted["reverted"] is True
    assert ledger.generation == generation
    assert decision["allowed"] is True
    assert decision["source_paths"] == ["app.js"]
    assert decision["automatic_diagnostics_passed"] is True


def test_existing_file_revert_drops_only_that_net_mutation():
    ledger = VerificationLedger()
    ledger.mark_mutation(
        "app.py", "app-new", "passed", "python-ast", previous_hash="app-old"
    )
    ledger.mark_mutation(
        "config.py", "config-new", "passed", "python-ast", previous_hash="config-old"
    )
    generation = ledger.generation

    ledger.mark_mutation(
        "config.py", "config-old", "passed", "python-ast", previous_hash="config-new"
    )
    decision = ledger.decision("direct")

    assert ledger.generation == generation
    assert decision["source_paths"] == ["app.py"]
    assert decision["allowed"] is True


def test_explicit_prompt_acceptance_command_can_verify_project_behavior():
    ledger = VerificationLedger()
    ledger.mark_mutation("core/pipeline.py", "source-1", "passed", "python-ast")

    evidence = ledger.record_command(
        "python run.py sample.json",
        0,
        "Ada Lovelace (AL) - STOCKHOLM",
        explicit_verification=True,
    )

    assert evidence["kind"] == "ad_hoc"
    assert evidence["verification"] is True
    assert evidence["explicit_prompt_command"] is True
    assert ledger.decision("planned")["allowed"] is True


def test_unquoted_project_command_remains_non_verification():
    ledger = VerificationLedger()
    ledger.mark_mutation("core/pipeline.py", "source-1", "passed", "python-ast")

    evidence = ledger.record_command("python run.py sample.json", 0)

    assert evidence["kind"] == "non_verification"
    assert evidence["verification"] is False
    assert ledger.decision("planned")["allowed"] is False


def test_explicit_chained_or_dry_run_command_remains_non_verification():
    for command in (
        "python run.py sample.json && exit 0",
        "python run.py sample.json --dry-run",
    ):
        evidence = VerificationLedger().record_command(
            command,
            0,
            explicit_verification=True,
        )
        assert evidence["kind"] == "non_verification"
        assert evidence["verification"] is False
