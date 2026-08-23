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
import unittest

from foundry.install_jobs import (
    ReconciliationError,
    desired_jobs,
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
        "continuity": False,
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

    def test_dry_run_performs_zero_writes_copies_or_scheduler_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / ".hermes"
            commands: list[list[str]] = []
            actions = run_installer(
                ROOT,
                hermes_home=hermes_home,
                dry_run=True,
                runner=lambda command: commands.append(command),
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
                runner=lambda command: commands.append(command),
            )
            self.assertEqual([action.kind for action in actions], ["create", "create", "create", "create"])
            self.assertEqual(len(commands), 4)
            self.assertTrue(all(command[:3] == ["hermes", "cron", "create"] for command in commands))
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

    def test_copied_monitor_wrapper_resolves_repo_root_from_foundry_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory) / "repository"
            (temporary_root / "foundry").mkdir(parents=True)
            shutil.copytree(ROOT / "foundry" / "src", temporary_root / "foundry" / "src")
            hermes_home = Path(directory) / ".hermes"
            run_installer(ROOT, hermes_home=hermes_home, runner=lambda command: None)
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
