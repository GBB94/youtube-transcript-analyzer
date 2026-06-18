"""Security helpers. Phase 1 doesn't shell out, but these encode the rules later
phases (ytdlp_subs, local_whisper) must use, so they exist from the start.
"""
from __future__ import annotations

import re
import shlex
from typing import Sequence
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def assert_safe_url(url: str, allowed_hosts: Sequence[str] = ()) -> None:
    """Reject anything that isn't an http(s) URL on an allowlisted host.
    An untrusted URL must never silently activate every yt-dlp extractor."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"disallowed URL scheme: {parsed.scheme!r}")
    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise ValueError(f"host not allowlisted: {parsed.hostname!r}")


def build_subprocess_args(binary: str, flags: Sequence[str],
                          positionals: Sequence[str]) -> list[str]:
    """Always argument arrays, never a shell string. A `--` separator precedes
    untrusted positionals so a value like '-something' or a URL starting with '-'
    cannot be parsed as a flag (argument injection) even without a shell."""
    return [binary, *flags, "--", *positionals]


_SECRET = re.compile(r"(?i)(token|key|secret|cookie|authorization)=([^&\s]+)")


def redact(text: str) -> str:
    """Redact obvious secrets and provider request IDs from anything logged."""
    return _SECRET.sub(r"\1=<redacted>", text)


def safe_temp_name(content_hash: str) -> str:
    """Content-addressed temp filename; NEVER derive names from remote titles."""
    return "media_" + re.sub(r"[^a-f0-9]", "", content_hash.split(":")[-1])[:32]


def quote_for_log(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(redact(a)) for a in args)
