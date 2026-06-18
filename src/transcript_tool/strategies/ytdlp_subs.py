"""ytdlp_subs (Phase 3) — caption tracks via yt-dlp.

Robust caption extractor. REQUIRES an external JS runtime (Deno/Node) for full
YouTube support and may require a pinned PO-token provider plugin. Cookies are
opt-in only. The subprocess is invoked with an argument array and `--` before the
URL (see security.build_subprocess_args). The runner is injectable so error-mapping
and .vtt parsing are unit-testable without network.

The runner contract: run(args) -> CompletedProc(returncode, stdout, stderr) and
writes subtitle files into the work dir we pass via -o.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..normalize import normalize_caption_file
from ..policy import Policy
from ..quality import evaluate, rejected, rejection_reasons
from ..schema import (
    Attempt, Cost, Language, Provenance, Reason, Result, Segment, TimestampType, VideoRef,
)
from ..security import assert_safe_url, build_subprocess_args
from .api_captions import extract_video_id


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    workdir: Path


Runner = Callable[[list[str], Path], ProcResult]


def _real_runner(args: list[str], workdir: Path) -> ProcResult:
    # No shell. Caller already inserted `--` before the URL.
    proc = subprocess.run(args, capture_output=True, text=True, cwd=str(workdir), timeout=120)
    return ProcResult(proc.returncode, proc.stdout, proc.stderr, workdir)


def map_ytdlp_error(stderr: str) -> Reason:
    s = stderr.lower()
    if "sign in to confirm you" in s or "confirm you're not a bot" in s:
        return Reason.bot_gated
    if "po token" in s or "po_token" in s:
        return Reason.po_token_rejected
    if "no supported javascript runtime" in s or "js runtime" in s:
        return Reason.missing_js_runtime
    if "http error 429" in s or "too many requests" in s:
        return Reason.rate_limited
    if "private" in s or "members-only" in s or "join this channel" in s:
        return Reason.members_only
    if "video unavailable" in s or "has been removed" in s:
        return Reason.removed
    if "age" in s and "restricted" in s:
        return Reason.age_restricted
    if "is not available in your country" in s or "geo" in s:
        return Reason.geoblocked
    if "requested format is not available" in s or "no subtitles" in s:
        return Reason.captions_unavailable
    return Reason.provider_error


class YtdlpSubsStrategy:
    name = "ytdlp_subs"

    def __init__(self, runner: Optional[Runner] = None):
        self.runner = runner or _real_runner

    def applicable(self, ref: VideoRef, policy: Policy) -> bool:
        return (
            "ytdlp_subs" in policy.enabled_strategies
            and ref.source == "url"
            and policy.egress.allow_public_url
            and ref.url is not None
        )

    async def fetch(self, ref: VideoRef, policy: Policy) -> Result:
        t0 = time.monotonic()
        try:
            assert_safe_url(ref.url, policy.egress.allowed_hosts)
        except ValueError:
            return self._fail(ref, Reason.invalid_input, t0)

        workdir = Path(tempfile.mkdtemp(prefix="ytdlp_"))
        lang = (policy.languages[0] if policy.languages else "en")
        flags = [
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-lang", lang, "--sub-format", "vtt",
            "-o", "%(id)s.%(ext)s",
        ]
        # cookies are opt-in only and never auto-read (omitted unless explicitly configured)
        args = build_subprocess_args("yt-dlp", flags, [ref.url])

        try:
            proc = self.runner(args, workdir)
        except FileNotFoundError:
            return self._fail(ref, Reason.missing_dependency, t0)        # yt-dlp not installed
        except subprocess.TimeoutExpired:
            return self._fail(ref, Reason.timeout, t0)

        vtt = self._find_vtt(workdir, extract_video_id(ref))
        if proc.returncode != 0 and vtt is None:
            return self._classify(ref, map_ytdlp_error(proc.stderr), t0)
        if vtt is None:
            return self._unavail(ref, Reason.captions_unavailable, t0)

        raw = vtt.read_text(encoding="utf-8", errors="replace")
        cues, text, cues_ref = normalize_caption_file(raw)
        if not text.strip():
            return self._unavail(ref, Reason.captions_unavailable, t0)

        gates = evaluate(cues, text, policy.quality)
        if rejected(gates):
            res = self._unavail(ref, Reason.no_acceptable_transcript, t0)
            res.quality = gates
            res.attempts[0].quality_rejections = rejection_reasons(gates)
            return res

        is_auto = ".auto." in vtt.name or vtt.name.endswith(f".{lang}.vtt") is False
        result = Result.make_success(
            ref,
            provenance=Provenance.platform_auto if "auto" in vtt.name else Provenance.human_caption,
            text=text,
            segments=[Segment(start=c.start, end=c.end, text=c.text) for c in cues],
            language=Language(requested=list(policy.languages), selected=lang,
                              track_language=lang, detection_method=None),
            timestamp_type=TimestampType.caption_cue,
            raw_text=text, raw_cues_ref=cues_ref, track_id=lang,
            duration_seconds=cues[-1].end if cues else 0.0, quality=gates,
        )
        result.attempts = [self._attempt(True, t0)]
        return result

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _find_vtt(workdir: Path, vid: Optional[str]) -> Optional[Path]:
        vtts = sorted(workdir.glob("*.vtt"))
        if not vtts:
            return None
        # prefer manual (no '.auto.') over auto-generated
        manual = [p for p in vtts if "auto" not in p.name]
        return (manual or vtts)[0]

    def _attempt(self, ok, t0, reason=None):
        return Attempt(strategy=self.name, ok=ok, reason=reason,
                       latency_ms=int((time.monotonic() - t0) * 1000),
                       cost=Cost(amount=0.0, unit="none", estimated=False))

    def _classify(self, ref, reason: Reason, t0):
        from ..schema import classify_reason, Outcome
        bucket, _ = classify_reason(reason)
        return self._unavail(ref, reason, t0) if bucket is Outcome.unavailable \
            else self._fail(ref, reason, t0)

    def _fail(self, ref, reason, t0):
        res = Result.make_failed(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        return res

    def _unavail(self, ref, reason, t0):
        res = Result.make_unavailable(ref, reason)
        res.attempts = [self._attempt(False, t0, reason)]
        return res
