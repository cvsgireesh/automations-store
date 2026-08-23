"""Tests for the Hermes-native Foundry job installer.

These tests use a temporary Hermes home so reconciliation never reads or
changes a user's scheduler installation.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from foundry import install_jobs
from foundry.install_jobs import (
    ReconciliationError,
    desired_jobs,
    install_assets,
    load_existing_jobs,
    plan_reconcile,
    run_installer,
)


ROOT = Path(__file__).resolve().parents[2]


def persisted(job, job_id: str) -> dict:
    """Represent one desired job as Hermes persists it in jobs.json."""
    return {
        "id": job_id,
        "name": job.name,
        "prompt": job.prompt,
        "schedule": {"kind": "cron", "expr": job.schedule, "display": job.schedule},
        "schedule_display": job.schedule,
        "deliver": job.deliver,
        "skills": list(job.skills),
        "skill": job.skills[0] if job.skills else None,
        "script": job.script,
        "monitor_script": job.monitor_script,
        "monitor_url": None,
        "no_agent": job.no_agent,
        "workdir": job.workdir,
        "provider": job.provider or None,
        "model": job.model or None,
        "reasoning_effort": job.reasoning_effort or None,
        # These are the fields Hermes persists for an active recurring job.
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": None, "completed": 0},
        "base_url": None,
        "context_from": None,
        # Hermes omits attach_to_session when it was never opted in.
    }


class InstallerPlanningTests(unittest.TestCase):
    def test_reconcile_creates_each_named_job_once(self):
        existing = []
        actions = plan_reconcile(existing, desired_jobs(ROOT))
        self.assertEqual([action.kind for action in actions], ["create", "create", "create", "create"])

    def test_second_reconcile_is_noop(self):
        existing = desired_jobs(ROOT)
        self.assertEqual(plan_reconcile(existing, desired_jobs(ROOT)), [])

    def test_exact_jobs_pin_schedule_model_delivery_and_foundry_workdir(self):
        jobs = desired_jobs(ROOT)
        self.assertEqual(
            [
                (
                    job.name,
                    job.schedule,
                    job.script,
                    job.monitor_script,
                    job.provider,
                    job.model,
                    job.reasoning_effort,
                    job.skills,
                    job.deliver,
                    job.no_agent,
                )
                for job in jobs
            ],
            [
                (
                    "Product Foundry Signal Collector",
                    "10 1 * * *",
                    "product_foundry_collect.py",
                    None,
                    None,
                    None,
                    None,
                    (),
                    "local",
                    True,
                ),
                (
                    "Product Foundry Local Scout",
                    "25 1 * * *",
                    None,
                    "product_foundry_monitor.py",
                    "llamacpp",
                    "Ornith-1.0-35B",
                    None,
                    ("hermes-product-foundry",),
                    "local",
                    False,
                ),
                (
                    "Product Foundry Authority Builder",
                    "0 2 * * 0",
                    None,
                    "product_foundry_candidate.py",
                    "openai-codex",
                    "gpt-5.6-sol",
                    "high",
                    ("hermes-product-foundry",),
                    "telegram",
                    False,
                ),
                (
                    "Product Foundry Verifier",
                    "45 2 * * 0",
                    "product_foundry_verify.py",
                    None,
                    None,
                    None,
                    None,
                    (),
                    "telegram",
                    True,
                ),
            ],
        )
        self.assertEqual({job.workdir for job in jobs}, {str(ROOT / "foundry")})

    def test_agent_jobs_attach_the_full_checked_in_prompt_contracts(self):
        jobs = {job.name: job for job in desired_jobs(ROOT)}
        scout = jobs["Product Foundry Local Scout"]
        builder = jobs["Product Foundry Authority Builder"]
        self.assertEqual(scout.prompt, (ROOT / "foundry" / "prompts" / "SCOUT.md").read_text(encoding="utf-8").rstrip())
        self.assertEqual(builder.prompt, (ROOT / "foundry" / "prompts" / "BUILDER.md").read_text(encoding="utf-8").rstrip())
        for phrase in (
            "scout-candidate.json",
            "scout-receipts",
            "(cd .. && python3 -m foundry.src.orchestrator stage-candidate --candidate foundry/state/scout-candidate.json --consume)",
            "at most one",
            "Never build",
            "credentials",
            "git push",
        ):
            self.assertIn(phrase, scout.prompt)
        for phrase in ("gate.json", "passed: true", "Never access", "Never publish", "git push"):
            self.assertIn(phrase, builder.prompt)
        scout_command = plan_reconcile([], (scout,))[0].command("hermes")
        self.assertIn(scout.prompt, scout_command)

    def test_normalized_persisted_records_are_a_true_noop(self):
        jobs = desired_jobs(ROOT)
        existing = [persisted(job, f"job-{index}") for index, job in enumerate(jobs)]
        self.assertEqual(plan_reconcile(existing, jobs), [])

    def test_stale_persisted_fields_require_one_exact_name_update(self):
        jobs = desired_jobs(ROOT)
        existing = [persisted(job, f"job-{index}") for index, job in enumerate(jobs)]
        existing[1]["model"] = "wrong-model"
        existing[1]["skills"] = ["unrelated-skill"]
        existing[1]["skill"] = "unrelated-skill"
        existing[1]["monitor_url"] = "https://example.invalid/monitor"
        actions = plan_reconcile(existing, jobs)
        self.assertEqual([(action.kind, action.job.name) for action in actions], [
            ("update", "Product Foundry Local Scout"),
        ])
        command = actions[0].command("hermes")
        self.assertIn("--skill", command)
        self.assertIn("hermes-product-foundry", command)
        self.assertIn("--monitor-url", command)
        self.assertIn("", command)
        self.assertIn("--agent", command)

    def test_actual_context_from_self_is_continuity_and_is_cleared_by_an_exact_update(self):
        job = desired_jobs(ROOT)[1]
        existing = persisted(job, "scout-id")
        existing["context_from"] = ["self"]
        actions = plan_reconcile([existing], (job,))
        self.assertEqual([(action.kind, action.job_id) for action in actions], [("update", "scout-id")])
        self.assertIn("--no-continuity", actions[0].command("hermes", "default"))

    def test_unmanaged_context_base_url_and_session_attachment_fail_closed_before_mutation(self):
        job = desired_jobs(ROOT)[1]
        for field, value in (
            ("context_from", ["other-job"]),
            ("base_url", "https://example.invalid/v1"),
            ("attach_to_session", True),
        ):
            with self.subTest(field=field):
                existing = persisted(job, "scout-id")
                existing[field] = value
                with self.assertRaisesRegex(ReconciliationError, field):
                    plan_reconcile([existing], (job,))

    def test_repeat_and_active_state_are_reconciled_from_actual_hermes_shape(self):
        job = desired_jobs(ROOT)[0]
        finite = persisted(job, "collector-id")
        finite["repeat"] = {"times": 3, "completed": 1}
        actions = plan_reconcile([finite], (job,))
        self.assertEqual([action.kind for action in actions], ["update"])
        self.assertIn("--repeat", actions[0].command("hermes", "default"))
        self.assertIn("0", actions[0].command("hermes", "default"))

        paused = persisted(job, "collector-id")
        paused["enabled"] = False
        paused["state"] = "paused"
        actions = plan_reconcile([paused], (job,))
        self.assertEqual([action.kind for action in actions], ["resume"])
        self.assertEqual(actions[0].command("hermes", "default"), ["hermes", "-p", "default", "cron", "resume", "collector-id"])

        stale_pause = persisted(job, "collector-id")
        stale_pause["paused_at"] = "2026-08-23T00:00:00-05:00"
        stale_pause["paused_reason"] = "old maintenance window"
        actions = plan_reconcile([stale_pause], (job,))
        self.assertEqual([action.kind for action in actions], ["resume"])

    def test_unsafe_managed_job_ids_are_rejected_before_they_become_cli_arguments(self):
        job = desired_jobs(ROOT)[0]
        for unsafe_id in ("--help", "-leading-dash", "contains space", "path/segment", "path\\segment", "line\nbreak"):
            with self.subTest(job_id=unsafe_id):
                existing = persisted(job, unsafe_id)
                existing["enabled"] = False
                existing["state"] = "paused"
                with self.assertRaisesRegex(ReconciliationError, "unsafe scheduler id"):
                    plan_reconcile([existing], (job,))

    def test_duplicate_exact_names_fail_closed(self):
        job = desired_jobs(ROOT)[0]
        duplicate = [persisted(job, "one"), persisted(job, "two")]
        with self.assertRaises(ReconciliationError):
            plan_reconcile(duplicate, desired_jobs(ROOT))

    def test_similar_names_are_not_matched(self):
        similar = persisted(desired_jobs(ROOT)[0], "one")
        similar["name"] = "Product Foundry Signal Collector - copy"
        actions = plan_reconcile([similar], desired_jobs(ROOT))
        self.assertEqual([action.kind for action in actions], ["create", "create", "create", "create"])


class InstallerExecutionTests(unittest.TestCase):
    def test_jobs_file_is_read_only_for_planning_and_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            jobs_path = hermes_home / "cron" / "jobs.json"
            jobs_path.parent.mkdir(parents=True)
            jobs_path.write_text('{"jobs": [{"name": "unrelated"}]}', encoding="utf-8")
            self.assertEqual(load_existing_jobs(hermes_home), [{"name": "unrelated"}])
            jobs_path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ReconciliationError):
                load_existing_jobs(hermes_home)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hermes_home = base / ".hermes"
            outside = base / "outside-cron"
            hermes_home.mkdir()
            outside.mkdir()
            (hermes_home / "cron").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ReconciliationError):
                load_existing_jobs(hermes_home)

    def test_dry_run_performs_zero_writes_copies_or_scheduler_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            commands: list[list[str]] = []
            actions = run_installer(
                ROOT,
                hermes_home=hermes_home,
                dry_run=True,
                runner=lambda command, environment: commands.append(command),
            )
            self.assertEqual([action.kind for action in actions], ["create", "create", "create", "create"])
            self.assertEqual(commands, [])
            self.assertFalse(hermes_home.exists())

    def test_real_install_copies_assets_then_uses_only_create_or_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            commands: list[list[str]] = []
            actions = run_installer(
                ROOT,
                hermes_home=hermes_home,
                runner=lambda command, environment: commands.append(command),
            )
            self.assertEqual([action.kind for action in actions], ["create", "create", "create", "create"])
            self.assertEqual(len(commands), 4)
            self.assertTrue(all(command[:5] == ["hermes", "-p", "default", "cron", "create"] for command in commands))
            for name in (
                "product_foundry_collect.py",
                "product_foundry_monitor.py",
                "product_foundry_candidate.py",
                "product_foundry_verify.py",
            ):
                self.assertEqual(
                    (hermes_home / "scripts" / name).read_bytes(),
                    (ROOT / "foundry" / "scripts" / name).read_bytes(),
                )
            self.assertEqual(
                (hermes_home / "skills" / "hermes-product-foundry" / "SKILL.md").read_bytes(),
                (ROOT / "foundry" / "skill" / "SKILL.md").read_bytes(),
            )

    def test_scheduler_mutations_bind_the_planned_home_and_explicit_default_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            calls: list[tuple[list[str], dict[str, str]]] = []

            def runner(command, environment):
                calls.append((command, environment))

            run_installer(ROOT, hermes_home=hermes_home, runner=runner)
            self.assertEqual(len(calls), 4)
            for command, environment in calls:
                self.assertEqual(command[:4], ["hermes", "-p", "default", "cron"])
                self.assertEqual(environment["HERMES_HOME"], str(hermes_home))

    def test_scheduler_mutations_infer_and_pin_a_named_profile_from_its_home(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes" / "profiles" / "foundry"
            hermes_home.mkdir(parents=True)
            calls: list[tuple[list[str], dict[str, str]]] = []

            def runner(command, environment):
                calls.append((command, environment))

            run_installer(ROOT, hermes_home=hermes_home, runner=runner)
            self.assertEqual(calls[0][0][:4], ["hermes", "-p", "foundry", "cron"])
            self.assertEqual(calls[0][1]["HERMES_HOME"], str(hermes_home))

    def test_windows_mixed_case_profiles_component_pins_the_named_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes" / "Profiles" / "Foundry"
            hermes_home.mkdir(parents=True)
            calls: list[tuple[list[str], dict[str, str]]] = []

            with mock.patch.object(install_jobs, "_is_windows", return_value=True, create=True):
                run_installer(
                    ROOT,
                    hermes_home=hermes_home,
                    runner=lambda command, environment: calls.append((command, environment)),
                )
            self.assertEqual(calls[0][0][:4], ["hermes", "-p", "foundry", "cron"])
            self.assertEqual(calls[0][1]["HERMES_HOME"], str(hermes_home))

    def test_update_and_resume_mutations_also_receive_the_pinned_home_and_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            jobs = [persisted(job, f"job-{index}") for index, job in enumerate(desired_jobs(ROOT))]
            jobs[0]["enabled"] = False
            jobs[0]["state"] = "paused"
            jobs[1]["model"] = "wrong-model"
            jobs_path = hermes_home / "cron" / "jobs.json"
            jobs_path.parent.mkdir(parents=True)
            jobs_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            calls: list[tuple[list[str], dict[str, str]]] = []

            def runner(command, environment):
                calls.append((command, environment))

            actions = run_installer(ROOT, hermes_home=hermes_home, runner=runner)
            self.assertEqual([action.kind for action in actions], ["resume", "update"])
            self.assertEqual([command[4] for command, _ in calls], ["resume", "edit"])
            for command, environment in calls:
                self.assertEqual(command[:4], ["hermes", "-p", "default", "cron"])
                self.assertEqual(environment["HERMES_HOME"], str(hermes_home))

    def test_missing_named_profile_fails_before_assets_or_scheduler_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes" / "profiles" / "missing"
            calls: list[tuple[list[str], dict[str, str]]] = []
            with self.assertRaises(ReconciliationError):
                run_installer(
                    ROOT,
                    hermes_home=hermes_home,
                    runner=lambda command, environment: calls.append((command, environment)),
                )
            self.assertEqual(calls, [])
            self.assertFalse(hermes_home.exists())

    def test_unsafe_paused_job_id_fails_before_assets_or_scheduler_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            jobs = [persisted(job, f"job-{index}") for index, job in enumerate(desired_jobs(ROOT))]
            jobs[0]["id"] = "--help"
            jobs[0]["enabled"] = False
            jobs[0]["state"] = "paused"
            jobs_path = hermes_home / "cron" / "jobs.json"
            jobs_path.parent.mkdir(parents=True)
            jobs_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            commands: list[list[str]] = []
            with self.assertRaisesRegex(ReconciliationError, "unsafe scheduler id"):
                run_installer(
                    ROOT,
                    hermes_home=hermes_home,
                    runner=lambda command, environment: commands.append(command),
                )
            self.assertEqual(commands, [])
            self.assertFalse((hermes_home / "scripts").exists())
            self.assertFalse((hermes_home / "skills").exists())

    def test_platform_lock_never_follows_a_linked_lock_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hermes_home = base / ".hermes"
            outside = base / "outside-lock"
            hermes_home.mkdir()
            outside.mkdir()
            (hermes_home / ".foundry-install.lock").symlink_to(outside, target_is_directory=True)
            commands: list[list[str]] = []
            if install_jobs.fcntl is not None:
                # POSIX locks the validated Hermes-home directory itself, so
                # this unrelated filename is neither followed nor trusted.
                actions = run_installer(
                    ROOT,
                    hermes_home=hermes_home,
                    runner=lambda command, environment: commands.append(command),
                )
                self.assertEqual([action.kind for action in actions], ["create"] * 4)
                self.assertEqual(len(commands), 4)
            else:
                # Windows holds the persistent regular lock file with msvcrt.
                with self.assertRaisesRegex(ReconciliationError, "lock"):
                    run_installer(
                        ROOT,
                        hermes_home=hermes_home,
                        runner=lambda command, environment: commands.append(command),
                    )
                self.assertEqual(commands, [])
                self.assertTrue(hermes_home.is_dir())
                self.assertFalse((hermes_home / "scripts").exists())
                self.assertFalse((hermes_home / "skills").exists())

    def test_concurrent_real_installers_serialize_planning_and_create_each_managed_name_once(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            jobs_path = hermes_home / "cron" / "jobs.json"
            desired_by_name = {job.name: job for job in desired_jobs(ROOT)}
            first_command = threading.Event()
            allow_first_command = threading.Event()
            write_lock = threading.Lock()
            commands: list[str] = []
            results: list[list] = []
            errors: list[BaseException] = []

            def runner(command, environment):
                name = command[command.index("--name") + 1]
                with write_lock:
                    is_first = not commands
                    commands.append(name)
                    if is_first:
                        first_command.set()
                if is_first:
                    if not allow_first_command.wait(timeout=3):
                        raise RuntimeError("test did not release the first installer")
                with write_lock:
                    records = []
                    if jobs_path.exists():
                        records = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
                    records.append(persisted(desired_by_name[name], f"job-{len(records)}"))
                    jobs_path.parent.mkdir(parents=True, exist_ok=True)
                    jobs_path.write_text(json.dumps({"jobs": records}), encoding="utf-8")

            def invoke():
                try:
                    results.append(run_installer(ROOT, hermes_home=hermes_home, runner=runner))
                except BaseException as error:  # surfaced in the parent test thread below
                    errors.append(error)

            first = threading.Thread(target=invoke)
            second = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(first_command.wait(timeout=3))
            second.start()
            # Without a cross-process-safe home lock, the second installer
            # plans its four creates while the first runner is paused here.
            time.sleep(0.15)
            allow_first_command.set()
            first.join(timeout=5)
            second.join(timeout=5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            names = [record["name"] for record in json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]]
            self.assertEqual(names, list(desired_by_name))
            self.assertEqual(commands, list(desired_by_name))
            self.assertEqual(sorted(len(actions) for actions in results), [0, 4])
            lock_path = hermes_home / ".foundry-install.lock"
            if install_jobs.fcntl is None:
                self.assertTrue(lock_path.is_file())
                self.assertFalse(lock_path.is_symlink())
                self.assertEqual(lock_path.stat().st_nlink, 1)
            else:
                self.assertFalse(lock_path.exists())

    @unittest.skipIf(install_jobs.fcntl is None, "POSIX directory-lock regression")
    def test_posix_home_lock_survives_lock_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            jobs_path = hermes_home / "cron" / "jobs.json"
            desired_by_name = {job.name: job for job in desired_jobs(ROOT)}
            first_command = threading.Event()
            allow_first_command = threading.Event()
            write_lock = threading.Lock()
            commands: list[str] = []
            results: list[list] = []
            errors: list[BaseException] = []

            def runner(command, environment):
                name = command[command.index("--name") + 1]
                with write_lock:
                    is_first = not commands
                    commands.append(name)
                    if is_first:
                        first_command.set()
                if is_first:
                    if not allow_first_command.wait(timeout=3):
                        raise RuntimeError("test did not release the first installer")
                with write_lock:
                    records = []
                    if jobs_path.exists():
                        records = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
                    records.append(persisted(desired_by_name[name], f"job-{len(records)}"))
                    jobs_path.parent.mkdir(parents=True, exist_ok=True)
                    jobs_path.write_text(json.dumps({"jobs": records}), encoding="utf-8")

            def invoke():
                try:
                    results.append(run_installer(ROOT, hermes_home=hermes_home, runner=runner))
                except BaseException as error:
                    errors.append(error)

            first = threading.Thread(target=invoke)
            second = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(first_command.wait(timeout=3))
            # The old lock-file design lets this replacement create a second
            # lock inode.  A home-directory flock must remain authoritative.
            lock_path = hermes_home / ".foundry-install.lock"
            lock_path.unlink(missing_ok=True)
            lock_path.write_text("replacement", encoding="utf-8")
            second.start()
            try:
                time.sleep(0.15)
                self.assertEqual(commands, ["Product Foundry Signal Collector"])
            finally:
                allow_first_command.set()
                first.join(timeout=5)
                second.join(timeout=5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            names = [record["name"] for record in json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]]
            self.assertEqual(names, list(desired_by_name))
            self.assertEqual(commands, list(desired_by_name))
            self.assertEqual(sorted(len(actions) for actions in results), [0, 4])

    @unittest.skipIf(install_jobs.fcntl is None, "POSIX directory-lock regression")
    def test_posix_home_lock_fails_closed_without_no_follow_directory_primitives(self):
        for attribute in ("O_DIRECTORY", "O_NOFOLLOW"):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                hermes_home = Path(directory) / ".hermes"
                commands: list[list[str]] = []
                with mock.patch.object(install_jobs.os, attribute, 0, create=True):
                    with self.assertRaisesRegex(ReconciliationError, "secure directory locking"):
                        run_installer(
                            ROOT,
                            hermes_home=hermes_home,
                            runner=lambda command, environment: commands.append(command),
                        )
                self.assertEqual(commands, [])
                self.assertFalse((hermes_home / "scripts").exists())
                self.assertFalse((hermes_home / "skills").exists())

    def test_concurrent_missing_home_creation_revalidates_the_loser_after_file_exists(self):
        """A normal mkdir race is not a symlink/reparse failure."""
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            barrier = threading.Barrier(2)
            original_mkdir = Path.mkdir
            results: list[list] = []
            errors: list[BaseException] = []

            def racing_mkdir(path, *args, **kwargs):
                if Path(path) == hermes_home:
                    barrier.wait(timeout=3)
                return original_mkdir(path, *args, **kwargs)

            def invoke():
                try:
                    results.append(
                        run_installer(
                            ROOT,
                            hermes_home=hermes_home,
                            runner=lambda command, environment: None,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(Path, "mkdir", new=racing_mkdir):
                first = threading.Thread(target=invoke)
                second = threading.Thread(target=invoke)
                first.start()
                second.start()
                first.join(timeout=5)
                second.join(timeout=5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(len(actions) for actions in results), [4, 4])
            self.assertTrue(hermes_home.is_dir())

    def test_asset_install_rejects_symlinked_parent_and_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hermes_home = base / ".hermes"
            outside = base / "outside"
            outside.mkdir()
            hermes_home.mkdir()
            (hermes_home / "scripts").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ReconciliationError, "symlink"):
                install_assets(ROOT, hermes_home)
            self.assertFalse(any(outside.iterdir()))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / ".hermes").mkdir()
            outside = base / "outside-profiles"
            outside.mkdir()
            (base / ".hermes" / "profiles").symlink_to(outside, target_is_directory=True)
            hermes_home = base / ".hermes" / "profiles" / "foundry"
            with self.assertRaisesRegex(ReconciliationError, "symlink"):
                install_assets(ROOT, hermes_home)
            self.assertFalse(any(outside.iterdir()))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hermes_home = base / ".hermes"
            outside = base / "outside-home"
            outside.mkdir()
            hermes_home.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ReconciliationError, "symlink"):
                install_assets(ROOT, hermes_home)
            self.assertFalse(any(outside.iterdir()))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hermes_home = base / ".hermes"
            scripts = hermes_home / "scripts"
            scripts.mkdir(parents=True)
            target = base / "asset-target"
            target.write_bytes((ROOT / "foundry" / "scripts" / "product_foundry_collect.py").read_bytes())
            (scripts / "product_foundry_collect.py").symlink_to(target)
            with self.assertRaisesRegex(ReconciliationError, "symlink"):
                install_assets(ROOT, hermes_home)
            self.assertTrue((scripts / "product_foundry_collect.py").is_symlink())
            self.assertEqual(target.read_bytes(), (ROOT / "foundry" / "scripts" / "product_foundry_collect.py").read_bytes())

    def test_asset_install_repairs_requested_mode_for_identical_bytes_and_rejects_reparse_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            scripts = hermes_home / "scripts"
            scripts.mkdir(parents=True)
            destination = scripts / "product_foundry_collect.py"
            destination.write_bytes((ROOT / "foundry" / "scripts" / "product_foundry_collect.py").read_bytes())
            destination.chmod(0o600)
            install_assets(ROOT, hermes_home)
            if sys.platform.startswith("win"):
                # Windows does not expose executable mode bits through chmod;
                # verify the copied wrapper remains writable and present.
                self.assertTrue(destination.is_file())
                self.assertTrue(destination.stat().st_mode & 0o200)
            else:
                self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            with mock.patch(
                "foundry.install_jobs._is_reparse_point",
                side_effect=lambda path, *_: path == hermes_home,
            ):
                with self.assertRaisesRegex(ReconciliationError, "reparse"):
                    install_assets(ROOT, hermes_home)

    def test_copied_monitor_wrapper_resolves_repo_root_from_foundry_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory) / "repository"
            (temporary_root / "foundry").mkdir(parents=True)
            shutil.copytree(ROOT / "foundry" / "src", temporary_root / "foundry" / "src")
            hermes_home = Path(directory) / ".hermes"
            run_installer(ROOT, hermes_home=hermes_home, runner=lambda command, environment: None)
            completed = subprocess.run(
                [sys.executable, str(hermes_home / "scripts" / "product_foundry_monitor.py")],
                cwd=temporary_root / "foundry",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
