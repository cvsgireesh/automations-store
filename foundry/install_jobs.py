"""Idempotently deploy Hermes Product Foundry scripts, skill, and cron jobs.

The installer deliberately treats ``~/.hermes/cron/jobs.json`` as a read-only
planning input.  It never edits that file directly: all scheduler mutations
are delegated to narrow, exact-job ``hermes cron create``, ``edit``, or
``resume`` commands.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

try:  # POSIX hosts use advisory flock; Windows falls back to msvcrt below.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows hosts
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - unavailable on POSIX test hosts
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX hosts
    msvcrt = None  # type: ignore[assignment]


SCRIPT_NAMES = (
    "product_foundry_collect.py",
    "product_foundry_monitor.py",
    "product_foundry_candidate.py",
    "product_foundry_verify.py",
)
SKILL_NAME = "hermes-product-foundry"
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
INSTALLER_LOCK_FILE = ".foundry-install.lock"
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_RETRY_SECONDS = 0.05


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
            "attach_to_session": None,
            "base_url": None,
            "context_from_nonself": (),
            "deliver": self.deliver,
            "enabled": True,
            "model": self.model,
            "monitor_script": self.monitor_script,
            "monitor_url": None,
            "no_agent": self.no_agent,
            "paused_at": None,
            "paused_reason": None,
            "prompt": self.prompt,
            "provider": self.provider,
            "reasoning_effort": self.reasoning_effort,
            "repeat_configured": True,
            "repeat_times": None,
            "schedule": self.schedule,
            "script": self.script,
            "skills": self.skills,
            "state": "scheduled",
            "workdir": self.workdir,
            "continuity": False,
        }


@dataclass(frozen=True)
class Action:
    """One exact-name Hermes CLI mutation planned from persisted state."""

    kind: str
    job: Job
    job_id: str | None = None

    def command(self, hermes_bin: str = "hermes", profile: str = "default") -> list[str]:
        prefix = [hermes_bin, "-p", profile, "cron"]
        if self.kind == "create":
            command = prefix + [
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

        if self.kind == "resume" and self.job_id:
            return prefix + ["resume", self.job_id]

        if self.kind != "update" or not self.job_id:
            raise ReconciliationError("only identified create, update, or resume actions are executable")
        command = prefix + [
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
            # Hermes normalizes non-positive repeat to an infinite recurring
            # job.  Passing it explicitly clears any finite repeat budget.
            "--repeat",
            "0",
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


def _context_from(record: Mapping[str, Any], job_id: str | None) -> tuple[bool, tuple[str, ...]]:
    """Return self-continuity and non-self refs from Hermes's actual shape."""
    raw = record.get("context_from")
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        values = raw
    elif raw in (None, ""):
        values = []
    else:
        raise ReconciliationError("managed job has malformed context_from")
    refs = [_text(value) for value in values]
    refs = [value for value in refs if value]
    continuity = any(value.casefold() == "self" for value in refs)
    nonself = tuple(value for value in refs if value.casefold() != "self")
    return continuity, nonself


def _repeat_shape(record: Mapping[str, Any]) -> tuple[bool, int | None]:
    raw = record.get("repeat")
    if not isinstance(raw, Mapping):
        return False, None
    value = raw.get("times")
    if value is None:
        return True, None
    if isinstance(value, bool):
        raise ReconciliationError("managed job has malformed repeat.times")
    try:
        return True, int(value)
    except (TypeError, ValueError) as error:
        raise ReconciliationError("managed job has malformed repeat.times") from error


def _attachment_value(record: Mapping[str, Any]) -> bool | None:
    value = record.get("attach_to_session")
    if value in (None, False, ""):
        return None
    if value is True:
        return True
    raise ReconciliationError("managed job has malformed attach_to_session")


def _raw_job_id(value: Any) -> str | None:
    """Preserve persisted job-ID bytes for positional CLI validation."""
    return value if isinstance(value, str) else None


def _safe_job_id(value: str | None, job_name: str) -> str:
    """Return a conservative positional ID, never a possible CLI flag/path."""
    if not isinstance(value, str) or not SAFE_JOB_ID.fullmatch(value):
        raise ReconciliationError(f"managed job has unsafe scheduler id: {job_name}")
    return value


def normalize_existing(record: Job | Mapping[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """Normalize legacy Hermes records into exactly the fields we own."""
    if isinstance(record, Job):
        return record.name, record.normalized(), None
    if not isinstance(record, Mapping):
        raise ReconciliationError("cron jobs.json contains a non-object job record")
    name = _text(record.get("name"))
    job_id = _raw_job_id(record.get("id"))
    provider = _text(record.get("provider")) or _text(record.get("model_provider"))
    continuity, nonself_context = _context_from(record, job_id)
    repeat_configured, repeat_times = _repeat_shape(record)
    normalized = {
        "attach_to_session": _attachment_value(record),
        "base_url": _text(record.get("base_url")),
        "context_from_nonself": nonself_context,
        "deliver": _text(record.get("deliver")) or "local",
        "enabled": record.get("enabled") is True,
        "model": _text(record.get("model")),
        "monitor_script": _text(record.get("monitor_script")),
        "monitor_url": _text(record.get("monitor_url")),
        "no_agent": bool(record.get("no_agent")),
        "paused_at": _text(record.get("paused_at")),
        "paused_reason": _text(record.get("paused_reason")),
        "prompt": _text(record.get("prompt")) or "",
        "provider": provider,
        "reasoning_effort": _text(record.get("reasoning_effort")),
        "repeat_configured": repeat_configured,
        "repeat_times": repeat_times,
        "schedule": _schedule(record),
        "script": _text(record.get("script")),
        "skills": _skills(record),
        "state": _text(record.get("state")) or "",
        "workdir": _workdir(record.get("workdir")),
        "continuity": continuity,
    }
    return name, normalized, job_id


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
        if actual["base_url"] is not None:
            raise ReconciliationError(f"managed job has unsupported base_url drift: {job.name}")
        if actual["attach_to_session"] is not None:
            raise ReconciliationError(f"managed job has unsupported attach_to_session drift: {job.name}")
        if actual["context_from_nonself"]:
            raise ReconciliationError(f"managed job has unsupported context_from refs: {job.name}")
        desired_values = job.normalized()
        # Resume, not edit, owns Hermes's active lifecycle quartet.
        update_fields = ("enabled", "state", "paused_at", "paused_reason")
        needs_update = any(
            actual[field] != desired_values[field]
            for field in desired_values
            if field not in update_fields
        )
        needs_resume = any(
            actual[field] != desired_values[field]
            for field in ("enabled", "state", "paused_at", "paused_reason")
        )
        if needs_update:
            actions.append(Action("update", job, _safe_job_id(job_id, job.name)))
        if needs_resume:
            actions.append(Action("resume", job, _safe_job_id(job_id, job.name)))
    return actions


def _home_ancestry(home: Path) -> tuple[Path, ...]:
    """Return the lexical Hermes-owned directories leading to ``home``."""
    anchor = home.parent.parent if home.parent.name == "profiles" else home
    paths = [anchor]
    current = anchor
    try:
        relative = home.relative_to(anchor)
    except ValueError as error:
        raise ReconciliationError("planned Hermes home has invalid ancestry") from error
    for component in relative.parts:
        current = current / component
        paths.append(current)
    return tuple(paths)


def load_existing_jobs(hermes_home: str | Path) -> list[Mapping[str, Any]]:
    """Safely read existing jobs for planning, without repairing or writing them."""
    home = Path(os.path.abspath(os.fspath(Path(hermes_home).expanduser())))
    cron_root = home / "cron"
    for parent in (*_home_ancestry(home), cron_root):
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ReconciliationError(f"cannot inspect Hermes jobs parent: {parent}") from error
        if parent.is_symlink() or _is_reparse_point(parent, parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
            raise ReconciliationError("Hermes jobs parent is not a safe regular directory")
    jobs_path = cron_root / "jobs.json"
    try:
        job_stat = jobs_path.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ReconciliationError("Hermes jobs path is unreadable") from error
    if jobs_path.is_symlink() or _is_reparse_point(jobs_path, job_stat) or not stat.S_ISREG(job_stat.st_mode):
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


def _is_reparse_point(path: Path, file_stat: os.stat_result | None = None) -> bool:
    """Recognize Windows junctions/reparse entries without following them."""
    try:
        details = file_stat if file_stat is not None else path.lstat()
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(details, "st_file_attributes", 0) & flag)


def _safe_directory_chain(home: Path, destination_parent: Path) -> None:
    """Create only lexical descendants of home, rejecting linked ancestry."""
    home_path = Path(os.path.abspath(os.fspath(home.expanduser())))
    target = Path(os.path.abspath(os.fspath(destination_parent)))
    try:
        target.relative_to(home_path)
    except ValueError as error:
        raise ReconciliationError("installed asset destination escapes planned Hermes home") from error
    # A named profile lives below <root>/profiles/<name>.  Its .hermes root
    # and profiles directory are part of the destination ancestry too, so
    # validate them rather than only the final profile directory.
    anchor = home_path.parent.parent if home_path.parent.name == "profiles" else home_path
    relative = target.relative_to(anchor)

    # A named profile may be requested before its .hermes/profiles ancestry
    # exists.  Create that lexical chain one component at a time, starting at
    # its closest existing parent, so a planted link is inspected rather than
    # followed by mkdir(parents=True).
    missing: list[Path] = []
    probe = anchor
    while True:
        try:
            probe.lstat()
            break
        except FileNotFoundError:
            missing.append(probe)
            if probe.parent == probe:
                raise ReconciliationError("planned Hermes home has no existing parent")
            probe = probe.parent
        except OSError as error:
            raise ReconciliationError(f"cannot inspect installed asset parent: {probe}") from error

    # Validate the first existing lexical ancestor too.  It may be a planted
    # `.hermes` or `profiles` link; omitting it would let creation of the next
    # component follow that link.
    chain = [probe, *reversed(missing)]
    current = anchor
    for component in relative.parts:
        current = current / component
        chain.append(current)
    for current in chain:
        try:
            details = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
                details = current.lstat()
            except OSError as error:
                raise ReconciliationError(f"cannot create installed asset parent: {current}") from error
        except OSError as error:
            raise ReconciliationError(f"cannot inspect installed asset parent: {current}") from error
        if current.is_symlink():
            raise ReconciliationError(f"installed asset parent is a symlink: {current}")
        if _is_reparse_point(current, details):
            raise ReconciliationError(f"installed asset parent is a Windows reparse point: {current}")
        if not stat.S_ISDIR(details.st_mode):
            raise ReconciliationError(f"installed asset parent is not a directory: {current}")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _safe_lock_stat(lock_path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
    """Inspect the persistent lock pathname without following a link."""
    try:
        details = lock_path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ReconciliationError("Foundry installer lock disappeared") from None
    except OSError as error:
        raise ReconciliationError("cannot inspect Foundry installer lock") from error
    if (
        lock_path.is_symlink()
        or _is_reparse_point(lock_path, details)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        raise ReconciliationError("Foundry installer lock is not a safe regular file")
    return details


def _open_installer_lock(home: Path) -> tuple[Path, int]:
    """Open one persistent safe lock inode without unlinking or replacing it."""
    _safe_directory_chain(home, home)
    lock_path = home / INSTALLER_LOCK_FILE
    before = _safe_lock_stat(lock_path, missing_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ReconciliationError("cannot open Foundry installer lock") from error
    try:
        opened = os.fstat(descriptor)
        after = _safe_lock_stat(lock_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or after is None
            or not _same_file(opened, after)
            or (before is not None and not _same_file(before, opened))
        ):
            raise ReconciliationError("Foundry installer lock changed while opening")
        if opened.st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        return lock_path, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _wait_for_installer_lock(descriptor: int) -> None:
    """Acquire a one-byte OS lock with a bounded wait on every host."""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ReconciliationError("cannot acquire Foundry installer lock") from error
                if time.monotonic() >= deadline:
                    raise ReconciliationError("timed out waiting for Foundry installer lock") from error
                time.sleep(LOCK_RETRY_SECONDS)
    if msvcrt is None:  # pragma: no cover - every supported host has one backend
        raise ReconciliationError("no supported Foundry installer lock backend")
    while True:  # pragma: no cover - exercised on Windows hosts
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise ReconciliationError("timed out waiting for Foundry installer lock") from error
            time.sleep(LOCK_RETRY_SECONDS)


def _release_installer_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows hosts
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@contextmanager
def _installer_lock(home: Path) -> Iterator[None]:
    """Serialize read-plan-assets-CLI reconciliation for one Hermes home."""
    lock_path, descriptor = _open_installer_lock(home)
    acquired = False
    try:
        _wait_for_installer_lock(descriptor)
        acquired = True
        # A replacement after open would let separate processes lock different
        # inodes, so validate the pathname again while our descriptor is held.
        current = _safe_lock_stat(lock_path)
        if current is None or not _same_file(os.fstat(descriptor), current):
            raise ReconciliationError("Foundry installer lock changed while acquiring")
        yield
    finally:
        if acquired:
            _release_installer_lock(descriptor)
        os.close(descriptor)


def _atomic_copy(source: Path, destination: Path, mode: int, home: Path) -> None:
    """Copy one approved local asset atomically through safe lexical parents."""
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise ReconciliationError(f"installer source asset is missing: {source}") from error
    if source.is_symlink():
        raise ReconciliationError(f"installer source asset is a symlink: {source}")
    if _is_reparse_point(source, source_stat):
        raise ReconciliationError(f"installer source asset is a Windows reparse point: {source}")
    if not stat.S_ISREG(source_stat.st_mode):
        raise ReconciliationError(f"installer source asset is not a regular file: {source}")
    try:
        content = source.read_bytes()
    except OSError as error:
        raise ReconciliationError(f"cannot read installer source asset: {source}") from error
    _safe_directory_chain(home, destination.parent)
    try:
        destination_stat = destination.lstat()
        if destination.is_symlink():
            raise ReconciliationError(f"installed asset destination is a symlink: {destination}")
        if _is_reparse_point(destination, destination_stat):
            raise ReconciliationError(f"installed asset destination is a Windows reparse point: {destination}")
        if not stat.S_ISREG(destination_stat.st_mode):
            raise ReconciliationError(f"installed asset destination is not a regular file: {destination}")
        if destination.read_bytes() == content:
            os.chmod(destination, mode)
            return
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ReconciliationError(f"cannot inspect installed asset: {destination}") from error
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
        _atomic_copy(scripts_root / script_name, home / "scripts" / script_name, 0o700, home)
    _atomic_copy(
        root / "foundry" / "skill" / "SKILL.md",
        home / "skills" / SKILL_NAME / "SKILL.md", 0o600, home,
    )


def _profile_for_home(home: Path) -> str:
    """Pin the explicit profile that owns an already-planned Hermes home."""
    if home.parent.name != "profiles":
        return "default"
    profile = home.name.casefold()
    if not PROFILE_NAME.fullmatch(profile):
        raise ReconciliationError(f"planned Hermes profile name is unsafe: {home.name}")
    try:
        profile_stat = home.lstat()
    except OSError as error:
        raise ReconciliationError(f"planned named Hermes profile does not exist: {home}") from error
    if home.is_symlink() or _is_reparse_point(home, profile_stat) or not stat.S_ISDIR(profile_stat.st_mode):
        raise ReconciliationError(f"planned named Hermes profile is not a safe directory: {home}")
    return profile


Runner = Callable[[list[str], Mapping[str, str]], Any]


def _subprocess_runner(command: list[str], environment: Mapping[str, str]) -> None:
    try:
        subprocess.run(command, check=True, env=dict(environment))
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReconciliationError(f"Hermes scheduler command failed: {' '.join(command[:3])}") from error


def run_installer(repo_root: str | Path | None = None, *, hermes_home: str | Path | None = None,
                  dry_run: bool = False, hermes_bin: str = "hermes", runner: Runner | None = None) -> list[Action]:
    """Serialize a real read-plan-assets-CLI reconciliation for one Hermes home."""
    root = _repo_root(repo_root)
    home = Path(hermes_home) if hermes_home is not None else Path.home() / ".hermes"
    home_path = Path(os.path.abspath(os.fspath(home.expanduser())))
    # Named profiles must already exist; reject them before creating a real
    # lock.  The dry-run branch remains wholly read-only.
    profile = _profile_for_home(home_path)
    if dry_run:
        return plan_reconcile(load_existing_jobs(home_path), desired_jobs(root))
    with _installer_lock(home_path):
        # Re-read all mutable scheduler state inside the home-scoped lock.
        profile = _profile_for_home(home_path)
        actions = plan_reconcile(load_existing_jobs(home_path), desired_jobs(root))
        install_assets(root, home_path)
        execute = runner or _subprocess_runner
        environment = dict(os.environ)
        environment["HERMES_HOME"] = str(home_path)
        for action in actions:
            execute(action.command(hermes_bin, profile), environment)
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
