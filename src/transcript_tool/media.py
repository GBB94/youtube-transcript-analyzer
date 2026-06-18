"""Media acquisition (audio) for the URL -> ASR path. Live-only; not exercised in
CI. Uses the same safe-subprocess discipline as ytdlp_subs.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .policy import Policy
from .schema import Reason, VideoRef
from .security import assert_safe_url, build_subprocess_args


class MediaError(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


def acquire_audio(ref: VideoRef, policy: Policy) -> str:
    """Download bestaudio to a temp file and return its path. Raises MediaError
    mapped to the right reason on failure."""
    if not policy.egress.allow_public_url:
        raise MediaError(Reason.missing_dependency)
    assert_safe_url(ref.url or "", policy.egress.allowed_hosts)
    workdir = Path(tempfile.mkdtemp(prefix="ytaudio_"))
    flags = ["-f", "bestaudio", "-x", "--audio-format", "m4a", "-o", "%(id)s.%(ext)s"]
    args = build_subprocess_args("yt-dlp", flags, [ref.url])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, cwd=str(workdir), timeout=600)
    except FileNotFoundError:
        raise MediaError(Reason.missing_dependency)
    except subprocess.TimeoutExpired:
        raise MediaError(Reason.timeout)
    files = sorted(workdir.glob("*"))
    if proc.returncode != 0 or not files:
        from .strategies.ytdlp_subs import map_ytdlp_error
        raise MediaError(map_ytdlp_error(proc.stderr) if proc.returncode else Reason.audio_download_failed)
    return str(files[0])
