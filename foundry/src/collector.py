"""Bounded collection and normalization of public product signals."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Signal
from .providers import provider_key_for_url


TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (compatible; HermesProductFoundry/1.0; "
    "+https://github.com/Dennis-Gireesh/automations-store)"
)


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def stable_id(url: str, observed_date: str) -> str:
    """Return a deterministic identifier for one source observed on one date."""
    return sha256(f"{url}\n{observed_date}")


def _validate_url(url: str, allowed_hosts: set[str]) -> None:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if parsed.scheme != "https" or not hostname:
        raise ValueError("only HTTPS source URLs are allowed")
    if hostname.lower() not in allowed_hosts:
        raise ValueError(f"source hostname is not allowlisted: {hostname}")


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        _validate_url(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_text(url: str, allowed_hosts: set[str]) -> str:
    """Fetch a small public HTTPS response after validating every request URL."""
    normalized_hosts = {host.lower() for host in allowed_hosts}
    _validate_url(url, normalized_hosts)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = build_opener(_AllowlistedRedirectHandler(normalized_hosts))
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds 2 MiB cap")
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as error:
        raise ValueError(f"failed to fetch source: HTTP {error.code}") from error
    return content.decode(charset, errors="replace")


def _required_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError(f"required source field is missing: {pattern}")
    return match.group(1).strip()


def _required_int(text: str, pattern: str) -> int:
    return int(_required_match(text, pattern))


def _required_float(text: str, pattern: str) -> float:
    return float(_required_match(text, pattern))


def _required_text(text: str, pattern: str) -> str:
    return re.sub(r"\s+", " ", _required_match(text, pattern)).strip()


def parse_gumroad_product(html_text: str, url: str, observed_at: str,
                          independence_key: str = "gumroad") -> Signal:
    decoded = html.unescape(html_text)
    price = _required_float(decoded, r'product:price:amount[^>]+content="([0-9.]+)"')
    sales = _required_int(decoded, r'"sales_count"\s*:\s*(\d+)')
    ratings = _required_int(decoded, r'"ratings"\s*:\s*\{\s*"count"\s*:\s*(\d+)')
    title = _required_text(decoded, r"<title[^>]*>(.*?)</title>")
    return Signal(
        signal_id=stable_id(url, observed_at[:10]),
        source_url=url,
        source_type="paid_comparable",
        observed_at=observed_at,
        title=title,
        metrics={"price": price, "sales_count": sales, "rating_count": ratings},
        content_sha256=sha256(decoded),
        independence_key=independence_key,
    )


def parse_gumroad_search(html_text: str, url: str, observed_at: str,
                         independence_key: str = "gumroad") -> Signal:
    decoded = html.unescape(html_text)
    title = _required_text(decoded, r"<title[^>]*>(.*?)</title>")
    result_count_match = None
    for pattern in (
        r'data-results-count\s*=\s*"(\d+)"',
        r'"total"\s*:\s*(\d+)',
        r'\b(\d+)\s+(?:products?|results?)',
    ):
        result_count_match = re.search(pattern, decoded, flags=re.IGNORECASE)
        if result_count_match:
            break
    if result_count_match is None:
        raise ValueError("required Gumroad search result count is missing")
    result_count = int(result_count_match.group(1))
    return Signal(
        signal_id=stable_id(url, observed_at[:10]),
        source_url=url,
        source_type="market_search",
        observed_at=observed_at,
        title=title,
        metrics={"result_count": result_count},
        content_sha256=sha256(decoded),
        independence_key=independence_key,
    )


def parse_github_release(json_text: str, url: str, observed_at: str,
                         independence_key: str = "github") -> Signal:
    try:
        release = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("GitHub release response is not valid JSON") from error
    if not isinstance(release, dict):
        raise ValueError("GitHub release response must be a JSON object")
    tag_name = release.get("tag_name")
    title = release.get("name") or tag_name
    published_at = release.get("published_at")
    if not all(isinstance(value, str) and value for value in (tag_name, title, published_at)):
        raise ValueError("GitHub release response is missing required fields")
    return Signal(
        signal_id=stable_id(url, observed_at[:10]),
        source_url=url,
        source_type="official_release",
        observed_at=observed_at,
        title=title,
        metrics={"tag_name": tag_name, "published_at": published_at},
        content_sha256=sha256(json_text),
        independence_key=independence_key,
    )


def parse_pypistats_recent(json_text: str, url: str, observed_at: str,
                           independence_key: str = "pypistats") -> Signal:
    """Normalize the bounded recent-download response from PyPIStats."""
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("PyPIStats response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("PyPIStats response must be a JSON object")
    package = payload.get("package")
    data = payload.get("data")
    if not isinstance(package, str) or not package or not isinstance(data, dict):
        raise ValueError("PyPIStats response is missing package or data")
    metrics: dict[str, int] = {}
    for period in ("last_day", "last_week", "last_month"):
        value = data.get(period)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"PyPIStats response is missing valid {period}")
        metrics[period] = value
    return Signal(
        signal_id=stable_id(url, observed_at[:10]),
        source_url=url,
        source_type="adoption_signal",
        observed_at=observed_at,
        title=f"PyPI download activity for {package}",
        metrics=metrics,
        content_sha256=sha256(json_text),
        independence_key=independence_key,
    )


PARSERS: dict[str, Callable[[str, str, str, str], Signal]] = {
    "gumroad_product": parse_gumroad_product,
    "gumroad_search": parse_gumroad_search,
    "github_release": parse_github_release,
    "pypistats_recent": parse_pypistats_recent,
}


def _load_config(config: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(config, (str, Path)):
        with Path(config).open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    if not isinstance(config, Mapping):
        raise ValueError("config must be a JSON object")
    return config


def _read_fixture(fixture_dir: Path, fixture_name: str) -> str:
    fixture_root = fixture_dir.resolve()
    fixture_path = (fixture_root / fixture_name).resolve()
    try:
        fixture_path.relative_to(fixture_root)
    except ValueError as error:
        raise ValueError("fixture path escapes fixture directory") from error
    return fixture_path.read_text(encoding="utf-8")


def collect(config: Mapping[str, Any] | str | Path, observed_at: str,
            fixture_dir: str | Path | None = None) -> list[Signal]:
    """Collect configured public sources using fixtures or bounded live HTTP."""
    settings = _load_config(config)
    hosts = settings.get("allowed_hosts")
    sources = settings.get("sources")
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ValueError("config.allowed_hosts must be a list of hostnames")
    if not isinstance(sources, list):
        raise ValueError("config.sources must be a list")
    allowed_hosts = {host.lower() for host in hosts}
    root = Path(fixture_dir) if fixture_dir is not None else None
    signals: list[Signal] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("each configured source must be an object")
        url = source.get("url")
        parser_name = source.get("parser")
        independence_key = source.get("independence_key")
        if not isinstance(url, str) or not isinstance(parser_name, str):
            raise ValueError("each source requires string url and parser fields")
        if not isinstance(independence_key, str) or not independence_key.strip():
            raise ValueError("each source requires a non-empty independence_key")
        _validate_url(url, allowed_hosts)
        provider_key = provider_key_for_url(url)
        if not provider_key:
            raise ValueError("source hostname has no controlled provider key")
        if independence_key.casefold().strip() != provider_key:
            raise ValueError("configured independence_key conflicts with source provider")
        parser = PARSERS.get(parser_name)
        if parser is None:
            raise ValueError(f"unsupported source parser: {parser_name}")
        if root is None:
            response_text = fetch_text(url, allowed_hosts)
        else:
            fixture_name = source.get("fixture")
            if not isinstance(fixture_name, str):
                raise ValueError("fixture collection requires each source fixture name")
            response_text = _read_fixture(root, fixture_name)
        signal = parser(response_text, url, observed_at, independence_key=provider_key)
        configured_type = source.get("source_type")
        if configured_type is not None and configured_type != signal.source_type:
            raise ValueError("configured source type does not match parser output")
        signals.append(signal)
    return signals
