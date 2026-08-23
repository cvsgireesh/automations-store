"""Deterministic ZIP release builder that refuses unaudited source trees."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping
import zipfile

from .audits import AuditFinding, audit_tree


REQUIRED_METADATA = (
    "test_command",
    "tested_platforms",
    "source_revision",
    "price",
    "update_policy_end_date",
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class PackagingError(ValueError):
    """Raised when a source tree or release description is not safe to ship."""


@dataclass(frozen=True)
class ReleaseManifest:
    archive_sha256: str
    files: dict[str, str]
    test_command: str
    tested_platforms: tuple[str, ...]
    source_revision: str
    price: str | int | float
    update_policy_end_date: str


def _validate_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    if not isinstance(metadata, Mapping):
        raise PackagingError("release metadata must be a mapping")
    missing = [field for field in REQUIRED_METADATA if field not in metadata]
    if missing:
        raise PackagingError(f"release metadata is missing: {', '.join(missing)}")
    values = dict(metadata)
    if not isinstance(values["test_command"], str) or not values["test_command"].strip():
        raise PackagingError("test_command must be a non-empty string")
    if not isinstance(values["tested_platforms"], list) or not values["tested_platforms"] or not all(
        isinstance(platform, str) and platform.strip() for platform in values["tested_platforms"]
    ):
        raise PackagingError("tested_platforms must be a non-empty list of strings")
    for field in ("source_revision", "update_policy_end_date"):
        if not isinstance(values[field], str) or not values[field].strip():
            raise PackagingError(f"{field} must be a non-empty string")
    if not isinstance(values["price"], (str, int, float)) or isinstance(values["price"], bool):
        raise PackagingError("price must be a string or number")
    launchers = values.get("launchers", [])
    if not isinstance(launchers, list) or not all(
        isinstance(launcher, str) and launcher.endswith(".sh") and not Path(launcher).is_absolute()
        for launcher in launchers
    ):
        raise PackagingError("launchers must be a list of relative .sh paths")
    return values, {Path(launcher).as_posix() for launcher in launchers}


def _source_files(source_root: Path) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for path in source_root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files.append((path.relative_to(source_root).as_posix(), path.read_bytes()))
    return sorted(files)


def _zip_info(name: str, executable: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _manifest_payload(files: dict[str, str], metadata: Mapping[str, Any]) -> bytes:
    payload = {
        "files": files,
        "price": metadata["price"],
        "source_revision": metadata["source_revision"],
        "test_command": metadata["test_command"],
        "tested_platforms": metadata["tested_platforms"],
        "update_policy_end_date": metadata["update_policy_end_date"],
    }
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_release(source: str | Path, output_zip: str | Path,
                  metadata: Mapping[str, Any]) -> ReleaseManifest:
    """Audit and atomically build a byte-for-byte reproducible public ZIP release."""
    source_root = Path(source)
    destination = Path(output_zip)
    metadata_values, launchers = _validate_metadata(metadata)
    findings: list[AuditFinding] = audit_tree(source_root)
    if findings:
        codes = ", ".join(sorted({finding.code for finding in findings}))
        raise PackagingError(f"release audit failed: {codes}")
    if not source_root.is_dir():
        raise PackagingError("source must be a directory")
    try:
        destination.resolve().relative_to(source_root.resolve())
    except ValueError:
        pass
    else:
        raise PackagingError("output ZIP must be outside the source directory")

    source_files = _source_files(source_root)
    source_names = {name for name, _ in source_files}
    if not launchers.issubset(source_names):
        raise PackagingError("declared launcher is not a source file")
    file_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in source_files}
    manifest_name = "RELEASE-MANIFEST.json"
    members = source_files + [(manifest_name, _manifest_payload(file_hashes, metadata_values))]
    members.sort(key=lambda member: member[0])

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in members:
                archive.writestr(_zip_info(name, name in launchers), content)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    return ReleaseManifest(
        archive_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        files=file_hashes,
        test_command=metadata_values["test_command"],
        tested_platforms=tuple(metadata_values["tested_platforms"]),
        source_revision=metadata_values["source_revision"],
        price=metadata_values["price"],
        update_policy_end_date=metadata_values["update_policy_end_date"],
    )
