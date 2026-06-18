"""UI translation layer over the engine's Result (docs/UI_SCOPE.md §5). Maps
provenance -> badge and reason -> plain-language row copy. Retry eligibility is driven
off result.retry.eligible, NOT these strings — the engine separates them."""
from __future__ import annotations

from ..schema import Provenance, Reason, Result

PROVENANCE_BADGE = {
    Provenance.human_caption: "Human captions",
    Provenance.platform_auto: "Auto captions",
    Provenance.local_asr: "Local ASR",
    Provenance.managed_asr: "Managed ASR",
    Provenance.translated_caption: "Translated (auto)",
}

REASON_COPY = {
    Reason.captions_unavailable: "No captions available for this video.",
    Reason.language_unavailable: "Captions exist, but not in your language(s).",
    Reason.no_acceptable_transcript: "A transcript was found but failed quality checks.",
    Reason.no_speech: "No speech detected in the audio.",
    Reason.private: "This video is private.",
    Reason.removed: "This video has been removed.",
    Reason.members_only: "Not accessible (members-only).",
    Reason.age_restricted: "Not accessible (age-restricted).",
    Reason.geoblocked: "Not accessible (region-locked).",
    Reason.live: "Live stream — try again once it's archived.",
    Reason.bot_gated: "YouTube blocked the request (anti-bot).",
    Reason.access_challenge: "YouTube blocked the request (anti-bot).",
    Reason.rate_limited: "Temporary problem — retry.",
    Reason.timeout: "Temporary problem — retry.",
    Reason.provider_error: "Temporary problem — retry.",
    Reason.audio_download_failed: "Temporary problem — retry.",
    Reason.po_token_rejected: "Setup issue — see `transcript doctor`.",
    Reason.missing_po_token_provider: "Setup issue — see `transcript doctor`.",
    Reason.missing_js_runtime: "Setup issue — see `transcript doctor`.",
    Reason.missing_dependency: "Setup issue — see `transcript doctor`.",
    Reason.invalid_input: "Not a valid YouTube link or file.",
    Reason.resource_limit_exceeded: "Hit a size/time limit.",
}


def badge_for(result: Result) -> str:
    return PROVENANCE_BADGE.get(result.provenance, "Transcript") if result.provenance else "Transcript"


def message_for(result: Result) -> str:
    if result.reason is None:
        return ""
    return REASON_COPY.get(result.reason, f"Could not transcribe ({result.reason.value}).")


def retry_allowed(result: Result) -> bool:
    """Drive Retry off the engine's explicit retry flag, never the reason string."""
    return bool(result.retry and result.retry.eligible)
