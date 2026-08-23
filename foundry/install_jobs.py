"""Idempotently deploy Hermes Product Foundry scripts, skill, and cron jobs.

The installer deliberately treats ``~/.hermes/cron/jobs.json`` as a read-only
planning input.  It never edits that file directly: all scheduler mutations
are delegated to ``hermes cron create`` or ``hermes cron edit``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence


SCRIPT_NAMES = (
    "product_foundry_collect.py",
    "product_foundry_monitor.py",
    "product_foundry_candidate.py",
    "product_foundry_verify.py",
)
SKILL_NAME = "hermes-product-foundry"


class ReconciliationError(RuntimeError):
    """A scheduler state is unsafe to reconcile automatically."""


@dataclass(frozen=True)
class Job:
    """The small, fully normalized subset of a Hermes cron job we manage."""

    name: str
    schedule: str
    prompt: str
    script: str | None = None
    monitor_script: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    skills: tuple[str, ...] = ()
    deliver: str = "local"
    no_agent: bool = False
    workdir: str = ""

    @property
    def skill(self) -> str | None:
        """Compatibility view for Hermes's singular skill terminology."""
        return self.skills[0] if self.skills else None

    def normalized(self) -> dict[str, Any]:
        return {
            "deliver": self.deliver,
            "model": self.model,
            "monitor_script": self.monitor_script,
            "monitor_url": None,
            "no_agent": self.no_agent,
            "prompt": self.prompt,
            "provider": self.provider,
            "reasoning_effort": self.reasoning_effort,
            "schedule": self.schedule,
            "script": self.script,
            "skills": self.skills,
            "workdir": self.workdir,
            "continuity": False,
        }


@dataclass(frozen=True)
class Action:
    """One explicit Hermes CLI mutation, or a planned no-op-free create."""

    kind: str
    job: Job
    job_id: str | None = None

    def command(self, hermes_bin: str = "hermes") -> list[str]:
        if self.kind == "create":
            command = [
                hermes_bin,
                "cron",
                "create",
                self.job.schedule,
                self.job.prompt,
                "--name",
                self.job.name,
                "--deliver",
                self.job.deliver,
                "--workdir",
                self.job.workdir,
            ]
            for skill in self.job.skills:
                command.extend(["--skill", skill])
            if self.job.script:
                command.extend(["--script", self.job.script])
            if self.job.monitor_script:
                command.extend(["--monitor-script", self.job.monitor_script])
            if self.job.no_agent:
                command.append("--no-agent")
            if self.job.provider:
                command.extend(["--provider", self.job.provider])
            if self.job.model:
                command.extend(["--model", self.job.model])
            if self.job.reasoning_effort:
                command.extend(["--reasoning-effort", self.job.reasoning_effort])
            return command

        if self.kind != "update" or not self.job_id:
            raise ReconciliationError("only create or identified update actions are executable")
        command = [
            hermes_bin,
            "cron",
            "edit",
            self.job_id,
            "--schedule",
            self.job.schedule,
            "--prompt",
            self.job.prompt,
            "--name",
            self.job.name,
            "--deliver",
            self.job.deliver,
            "--workdir",
            self.job.workdir,
            # Hermes documents an empty value as the safe way to clear fields.
            "--script",
            self.job.script or "",
            "--monitor-script",
            self.job.monitor_script or "",
            "--monitor-url",
            "",
            "--provider",
            self.job.provider or "",
            "--model",
            self.job.model or "",
            "--reasoning-effort",
            self.job.reasoning_effort or "",
            "--no-agent" if self.job.no_agent else "--agent",
            "--no-continuity",
        ]
        if self.job.skills:
            for skill in self.job.skills:
                command.extend(["--skill", skill])
        else:
            command.append("--clear-skills")
        return command


def _repo_root(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    root = root.resolve()
    if not root.is_dir():
        raise ReconciliationError(f"repository root is not a directory: {root}")
    return root


def _prompt_contract(repo_root: Path, name: str) -> str:
    prompt_path = repo_root / "foundry" / "prompts" / name
    try:
        prompt = prompt_path.read_text(encoding="utf-8").rstrip()
    except OSError as error:
        raise ReconciliationError(f"required Foundry prompt contract is missing: {prompt_path}") from error
    if not prompt.strip():
        raise ReconciliationError(f"required Foundry prompt contract is empty: {prompt_path}")
    return prompt


def desired_jobs(repo_root: str | Path | None = None) -> tuple[Job, ...]:
    """Return the exact four approved Foundry jobs with explicit inference pins."""
    root = _repo_root(repo_root)
    workdir = str(root / "foundry")
    scout_prompt = _prompt_contract(root, "SCOUT.md")
    builder_prompt = _prompt_contract(root, "BUILDER.md")
    return (
        Job(
            "Product Foundry Signal Collector",
            "10 1 * * *",
            "Collect bounded normalized Foundry signals. Emit output only for a meaningful change.",
            script="product_foundry_collect.py",
            no_agent=True,
            deliver="local",
            workdir=workdir,
        ),
        Job(
            "Product Foundry Local Scout",
            "25 1 * * *",
            scout_prompt,
            monitor_script="product_foundry_monitor.py",
            provider="llamacpp",
            model="Ornith-1.0-35B",
            skills=(SKILL_NAME,),
            deliver="local",
            workdir=workdir,
        ),
        Job(
            "Product Foundry Authority Builder",
            "0 2 * * 0",
            builder_prompt,
            monitor_script="product_foundry_candidate.py",
            provider="openai-codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            skills=(SKILL_NAME,),
            deliver="telegram",
            workdir=workdir,
        ),
        Job(
            "Product Foundry Verifier",
            "45 2 * * 0",
            "Verify a fresh gated release locally. Emit output only for a verified release or failure.",
            script="product_foundry_verify.py",
            no_agent=True,
            deliver="telegram",
            workdir=workdir,
        ),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _skills(record: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw_skills = record.get("skills")
    if isinstance(raw_skills, str):
        raw_items: Iterable[Any] = [raw_skills]
    elif isinstance(raw_skills, (list, tuple)):
        raw_items = raw_skills
    elif raw_skills is None:
        raw_items = []
    else:
        raw_items = [raw_skills]
    for item in raw_items:
        text = _text(item)
        if text and text not in values:
            values.append(text)
    legacy = _text(record.get("skill"))
    if legacy and legacy not in values:
        values.append(legacy)
    return tuple(values)


def _schedule(record: Mapping[str, Any]) -> str | None:
    schedule = record.get("schedule")
    if isinstance(schedule, Mapping):
        for key in ("expr", "display", "value"):
            value = _text(schedule.get(key))
            if value:
                return value
        return None
    return _text(schedule) or _text(record.get("schedule_display"))


def _workdir(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def normalize_existing(record: Job | Mapping[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """Normalize legacy Hermes records into exactly the fields we own."""
    if isinstance(record, Job):
        return record.name, record.normalized(), None
    if not isinstance(record, Mapping):
        raise ReconciliationError("cron jobs.json contains a non-object job record")
    name = _text(record.get("name"))
    provider = _text(record.get("provider")) or _text(record.get("model_provider"))
    normalized = {
        "deliver": _text(record.get("deliver")) or "local",
        "model": _text(record.get("model")),
        "monitor_script": _text(record.get("monitor_script")),
        "monitor_url": _text(record.get("monitor_url")),
        "no_agent": bool(record.get("no_agent")),
        "prompt": _text(record.get("prompt")) or "",
        "provider": provider,
        "reasoning_effort": _text(record.get("reasoning_effort")),
        "schedule": _schedule(record),
        "script": _text(record.get("script")),
        "skills": _skills(record),
        "workdir": _workdir(record.get("workdir")),
        "continuity": bool(record.get("continuity")),
    }
    return name, normalized, _text(record.get("id"))


def plan_reconcile(existing: Sequence[Job | Mapping[str, Any]], desired: Sequence[Job]) -> list[Action]:
    """Plan exact-name-only reconciliation without mutating the scheduler."""
    desired_names = [job.name for job in desired]
    if len(desired_names) != len(set(desired_names)):
        raise ReconciliationError("desired job names must be unique")
    matches: dict[str, tuple[dict[str, Any], str | None]] = {}
    desired_name_set = set(desired_names)
    for record in existing:
        name, normalized, job_id = normalize_existing(record)
        if name not in desired_name_set:
            continue
        if name in matches:
            raise ReconciliationError(f"duplicate exact cron job name: {name}")
        matches[name] = (normalized, job_id)

    actions: list[Action] = []
    for job in desired:
        match = matches.get(job.name)
        if match is None:
            actions.append(Action("create", job))
            continue
        actual, job_id = match
        if actual != job.normalized():
            if not job_id:
                raise ReconciliationError(f"managed job lacks a scheduler id: {job.name}")
            actions.append(Action("update", job, job_id))
    return actions


def load_existing_jobs(hermes_home: str | Path) -> list[Mapping[str, Any]]:
    """Safely read existing jobs for planning, without repairing or writing them."""
    jobs_path = Path(hermes_home) / "cron" / "jobs.json"
    if not jobs_path.exists():
        return []
    if not jobs_path.is_file():
        raise ReconciliationError("Hermes jobs path is not a regular file")
    try:
        payload = json.loads(jobs_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReconciliationError("Hermes jobs.json is unreadable or malformed; refusing to mutate") from error
    if isinstance(payload, Mapping):
        jobs = payload.get("jobs")
    else:
        jobs = payload
    if not isinstance(jobs, list) or not all(isinstance(job, Mapping) for job in jobs):
        raise ReconciliationError("Hermes jobs.json must contain a list of job objects")
    return [dict(job) for job in jobs]


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    """Copy one approved local asset atomically, skipping identical bytes."""
    if not source.is_file():
        raise ReconciliationError(f"installer source asset is missing: {source}")
    content = source.read_bytes()
    try:
        if destination.is_file() and destination.read_bytes() == content:
            return
    except OSError as error:
        raise ReconciliationError(f"cannot inspect installed asset: {destination}") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def install_assets(repo_root: str | Path, hermes_home: str | Path) -> None:
    """Atomically deploy only the approved wrappers and one Foundry skill."""
    root = _repo_root(repo_root)
    home = Path(hermes_home)
    scripts_root = root / "foundry" / "scripts"
    for script_name in SCRIPT_NAMES:
        _atomic_copy(scripts_root / script_name, home / "scripts" / script_name, 0o700)
    _atomic_copy(
        root / "foundry" / "skill" / "SKILL.md",
        home / "skills" / SKILL_NAME / "SKILL.md",
        0o600,
    )


Runner = Callable[[list[str]], Any]


def _subprocess_runner(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReconciliationError(f"Hermes scheduler command failed: {' '.join(command[:3])}") from error


def run_installer(repo_root: str | Path | None = None, *, hermes_home: str | Path | None = None,
                  dry_run: bool = False, hermes_bin: str = "hermes", runner: Runner | None = None) -> list[Action]:
    """Plan then, only when requested, deploy assets and invoke Hermes CLI changes."""
    root = _repo_root(repo_root)
    home = Path(hermes_home) if hermes_home is not None else Path.home() / ".hermes"
    actions = plan_reconcile(load_existing_jobs(home), desired_jobs(root))
    if dry_run:
        return actions
    install_assets(root, home)
    execute = runner or _subprocess_runner
    for action in actions:
        execute(action.command(hermes_bin))
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install idempotent Hermes Product Foundry jobs")
    parser.add_argument("--dry-run", action="store_true", help="plan only; perform no writes, copies, or Hermes calls")
    parser.add_argument("--hermes-bin", default="hermes", help="Hermes CLI executable for real reconciliation")
    arguments = parser.parse_args(argv)
    try:
        actions = run_installer(dry_run=arguments.dry_run, hermes_bin=arguments.hermes_bin)
    except ReconciliationError as error:
        print(f"foundry installer: {error}", file=sys.stderr)
        return 1
    if not actions:
        print("no changes")
        return 0
    for action in actions:
        print(f"{action.kind} {action.job.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
