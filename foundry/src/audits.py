"""Fail-closed audits for source trees intended for public release."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


FORBIDDEN_CLAIMS = {
    "unsupported_social_proof": re.compile(r"\b(?:hundreds|thousands) of (?:developers|customers|users)\b", re.I),
    "unsupported_bestseller": re.compile(r"\bbest[ -]?seller\b", re.I),
    "unsupported_lifetime": re.compile(r"\blifetime updates?\b", re.I),
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s$<{]{12,}"),
)
PLACEHOLDER_PATTERN = re.compile(r"\bYOUR_[A-Z0-9_]+\b")
SENSITIVE_NAME_PATTERN = re.compile(r"(?:^|[._-])(?:auth|session|state)(?:$|[._-])", re.I)
THIRD_PARTY_DIRECTORY_NAMES = {"third_party", "third-party", "thirdparty", "vendor", "vendors"}
NOTICE_NAMES = {"notice", "notice.txt", "third_party_notices", "third-party-notices"}


@dataclass(frozen=True)
class AuditFinding:
    """One public-release violation, with a source-relative path."""

    code: str
    path: str
    detail: str = ""


def audit_text(text: str, path: str = "") -> list[AuditFinding]:
    """Return every forbidden claim, secret-like value, or unresolved placeholder."""
    findings: list[AuditFinding] = []
    for code, pattern in FORBIDDEN_CLAIMS.items():
        if pattern.search(text):
            findings.append(AuditFinding(code, path))
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        findings.append(AuditFinding("secret_like_value", path))
    if PLACEHOLDER_PATTERN.search(text):
        findings.append(AuditFinding("unresolved_placeholder", path))
    return findings


def _is_sensitive_path(path: Path) -> bool:
    name = path.name
    return name == ".env" or name.startswith(".env.") or bool(SENSITIVE_NAME_PATTERN.search(name))


def _is_binary(content: bytes) -> bool:
    return b"\0" in content


def _has_notice(directory: Path) -> bool:
    for candidate in directory.rglob("*"):
        if candidate.is_file() and candidate.name.casefold() in NOTICE_NAMES:
            return True
    return False


def audit_tree(root: str | Path) -> list[AuditFinding]:
    """Audit every source file without following links; findings are path-sorted."""
    source_root = Path(root)
    if not source_root.is_dir() or source_root.is_symlink():
        return [AuditFinding("invalid_source_root", source_root.as_posix())]

    findings: list[AuditFinding] = []
    third_party_directories: list[Path] = []
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        for directory in sorted(directories):
            directory_path = current_path / directory
            relative_path = directory_path.relative_to(source_root).as_posix()
            if directory_path.is_symlink():
                findings.append(AuditFinding("symlink", relative_path))
            elif directory.casefold() in THIRD_PARTY_DIRECTORY_NAMES:
                third_party_directories.append(directory_path)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]

        for filename in sorted(filenames):
            file_path = current_path / filename
            relative_path = file_path.relative_to(source_root).as_posix()
            if file_path.is_symlink():
                findings.append(AuditFinding("symlink", relative_path))
                continue
            if _is_sensitive_path(file_path):
                findings.append(AuditFinding("sensitive_file", relative_path))
            content = file_path.read_bytes()
            if _is_binary(content):
                if file_path.stat().st_mode & 0o111:
                    findings.append(AuditFinding("executable_binary", relative_path))
                continue
            findings.extend(audit_text(content.decode("utf-8", errors="replace"), relative_path))

    for directory in third_party_directories:
        if not _has_notice(directory):
            findings.append(AuditFinding("missing_third_party_notice", directory.relative_to(source_root).as_posix()))
    return sorted(findings, key=lambda finding: (finding.path, finding.code, finding.detail))
