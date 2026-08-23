"""Fail-closed audits for source trees intended for public release."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


FORBIDDEN_CLAIMS = {
    "unsupported_social_proof": re.compile(
        r"\b(?:hundreds|thousands) of (?:developers|customers|users)\b"
        r"|\btrusted\s+by\s+\d[\d,]*(?:\.\d+)?\+?\s+(?:developers?|customers?|users?|buyers?)\b",
        re.I,
    ),
    "unsupported_bestseller": re.compile(r"\bbest[ -]?seller\b", re.I),
    "unsupported_lifetime": re.compile(r"\blifetime updates?\b", re.I),
    "unsupported_testimonial": re.compile(
        r"\b(?:a\s+)?(?:buyer|customer|user)\s+(?:says|said|writes|wrote|reports|reported)\b"
        r"|<(?:testimonial|review)\b"
        r"|(?:class|id|data-[\w-]+)\s*=\s*['\"][^'\"]*\b(?:testimonial|review)s?\b"
        r"|[\"“][^\"”\r\n]{3,}[\"”]\s*(?:[-–—]|&(?:mdash|ndash);)\s*"
        r"[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}(?:\s*,\s*[^\r\n]{2,80})?",
        re.I,
    ),
    "unsupported_customer_count": re.compile(r"\b\d[\d,]*(?:\.\d+)?\+?\s+(?:customers?|users?|buyers?)\b", re.I),
    "unsupported_sales_figure": re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\+?\s+(?:sales|sold)\b"
        r"|\$\d[\d,]*(?:\.\d+)?\s+(?:in\s+)?(?:revenue|earnings?)\b"
        r"|\b(?:revenue|earnings?)\s+(?:of\s+)?\$?\d[\d,]*(?:\.\d+)?\b",
        re.I,
    ),
    "unsupported_rating_claim": re.compile(
        r"\b(?:rated|rating\s+of|rated\s+at)\s+\d(?:\.\d)?\s*(?:out\s+of\s+\d(?:\.\d)?\s*)?(?:stars?|/\s*5)\b"
        r"|\b\d(?:\.\d)?\s*stars?\b",
        re.I,
    ),
    "unsupported_discount_claim": re.compile(
        r"\b(?:save|sale|discount(?:ed)?)\s+(?:of\s+)?\d+(?:\.\d+)?%"
        r"|\b\d+(?:\.\d+)?%\s*off\b"
        r"|<(?:s|strike|del)\b[^>]*>\s*\$?\d",
        re.I,
    ),
    "unsupported_urgency_scarcity": re.compile(
        r"\b(?:limited[-\s]?time(?:\s+offer)?|buy\s+now|act\s+now|last\s+chance|"
        r"before\s+(?:midnight|tonight|it'?s\s+gone)|only\s+\d+\s+(?:left|remaining)|"
        r"while\s+supplies\s+last)\b",
        re.I,
    ),
    "unsupported_benchmark": re.compile(
        r"\b\d+(?:\.\d+)?\s*x\s+(?:faster|slower|cheaper|more\s+efficient)\b"
        r"|\$\d+(?:\.\d+)?\s*(?:per|/)\s*\w+"
        r"|\b(?:in\s+)?\d+(?:\.\d+)?\s*(?:ms|milliseconds|seconds?)\b"
        r"|\b\d+(?:\.\d+)?%\s+(?:accuracy|faster|slower|cheaper|reduction|improvement)\b"
        r"|\b\d[\d,]*\s+(?:requests?|jobs?|tasks?)\s+per\s+(?:second|minute|hour)\b",
        re.I,
    ),
    "unsupported_affiliation_claim": re.compile(r"\b(?:official(?:ly)?|partnered|certified|endorsed)\b", re.I),
}
MARKDOWN_CROSSED_OUT_PRICE = re.compile(r"~~\s*\$?\d[\d,]*(?:\.\d+)?\s*~~")
CSS_CROSSED_OUT_PRICE = re.compile(
    r"(?:[.#][\w-]*(?:old|original|was)[\w-]*price[\w-]*|"
    r"[.#][\w-]*price[\w-]*(?:old|original|was)[\w-]*)\s*\{[^}]{0,240}"
    r"\btext-decoration(?:-line)?\s*:\s*[^;}]*\bline-through\b"
    r"|style\s*=\s*['\"][^'\"]*\btext-decoration(?:-line)?\s*:\s*[^'\"]*\bline-through\b[^'\"]*['\"][^>]*>\s*\$?\d",
    re.I,
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s$<{]{12,}"),
)
PLACEHOLDER_PATTERN = re.compile(r"\bYOUR_[A-Z0-9_]+\b")
SENSITIVE_NAME_PATTERN = re.compile(r"(?:^|[._-])(?:auth|session|state)(?:$|[._-])", re.I)
THIRD_PARTY_DIRECTORY_NAMES = {"third_party", "third-party", "thirdparty", "vendor"}
COMPATIBLE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0"}
NOTICE_FILE_NAME = "THIRD_PARTY_NOTICES.md"
NOTICE_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.M)
NO_THIRD_PARTY_CODE_PATTERN = re.compile(r"\bno\s+third[-\s]party\s+code\b", re.I)


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
    if MARKDOWN_CROSSED_OUT_PRICE.search(text) or CSS_CROSSED_OUT_PRICE.search(text):
        findings.append(AuditFinding("unsupported_discount_claim", path))
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


def _third_party_components(source_root: Path) -> set[str]:
    components: set[str] = set()
    for directory in sorted(source_root.rglob("*")):
        if not directory.is_dir() or directory.is_symlink():
            continue
        if directory.name.casefold() not in THIRD_PARTY_DIRECTORY_NAMES:
            continue
        directory_identity = directory.relative_to(source_root).as_posix()
        for child in sorted(directory.iterdir()):
            components.add(f"{directory_identity}/{child.name}")
    return components


def _notice_entries(notice_text: str) -> tuple[dict[str, tuple[str, str]], set[str], set[str]]:
    """Parse markdown notice sections into component -> (source URL, license)."""
    matches = list(NOTICE_SECTION_PATTERN.finditer(notice_text))
    entries: dict[str, tuple[str, str]] = {}
    invalid: set[str] = set()
    duplicates: set[str] = set()
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        if name in entries or name in invalid:
            duplicates.add(name)
            continue
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(notice_text)
        section = notice_text[match.end():section_end]
        source = re.search(r"^\s*(?:[-*]\s*)?Source URL:\s*(https?://\S+)\s*$", section, re.M | re.I)
        license_name = re.search(r"^\s*(?:[-*]\s*)?License:\s*([^\r\n]+?)\s*$", section, re.M | re.I)
        if not source or not license_name:
            invalid.add(name)
            continue
        entries[name] = (source.group(1), license_name.group(1).strip().strip("`"))
    return entries, invalid, duplicates


def _audit_third_party_notices(source_root: Path) -> list[AuditFinding]:
    components = _third_party_components(source_root)
    notice_path = source_root / NOTICE_FILE_NAME
    if not notice_path.is_file() or notice_path.is_symlink():
        return [AuditFinding("missing_third_party_notices", NOTICE_FILE_NAME)] if components else []
    notice_text = notice_path.read_text(encoding="utf-8", errors="replace")
    entries, invalid_entries, duplicate_entries = _notice_entries(notice_text)
    findings: list[AuditFinding] = []
    if not components and not entries and not invalid_entries and not duplicate_entries:
        if not NO_THIRD_PARTY_CODE_PATTERN.search(notice_text):
            findings.append(AuditFinding("invalid_third_party_notices", NOTICE_FILE_NAME))
    for component in sorted(components - entries.keys() - invalid_entries):
        findings.append(AuditFinding("missing_third_party_component_notice", component))
    for component in sorted(entries.keys() - components):
        findings.append(AuditFinding("unmatched_third_party_notice", component))
    for component in sorted(invalid_entries):
        findings.append(AuditFinding("invalid_third_party_notice", component))
    for component in sorted(duplicate_entries):
        findings.append(AuditFinding("duplicate_third_party_notice", component))
    for component, (_, license_name) in sorted(entries.items()):
        if license_name not in COMPATIBLE_LICENSES:
            findings.append(AuditFinding("incompatible_third_party_license", component, license_name))
    return findings


def audit_tree(root: str | Path) -> list[AuditFinding]:
    """Audit every source file without following links; findings are path-sorted."""
    source_root = Path(root)
    if not source_root.is_dir() or source_root.is_symlink():
        return [AuditFinding("invalid_source_root", source_root.as_posix())]

    findings: list[AuditFinding] = []
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        for directory in sorted(directories):
            directory_path = current_path / directory
            relative_path = directory_path.relative_to(source_root).as_posix()
            if directory_path.is_symlink():
                findings.append(AuditFinding("symlink", relative_path))
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

    findings.extend(_audit_third_party_notices(source_root))
    return sorted(findings, key=lambda finding: (finding.path, finding.code, finding.detail))
