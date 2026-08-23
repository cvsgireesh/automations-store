"""Controlled source ownership keys used by evidence collection and gating."""

from urllib.parse import urlsplit


def provider_key_for_url(url: str) -> str:
    """Return the only accepted evidence-provider key for an HTTPS source URL."""
    hostname = urlsplit(url).hostname
    if not hostname:
        return ""
    host = hostname.casefold()
    if host in {"github.com", "api.github.com"}:
        return "github"
    if host == "pypistats.org":
        return "pypistats"
    if host == "gumroad.com" or host.endswith(".gumroad.com"):
        return "gumroad"
    return ""
