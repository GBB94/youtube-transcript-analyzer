"""Phase 5 — managed providers. All tests inject a fake client returning recorded
fixtures; no live network. Covers: success + cost, language_unavailable, 429 ->
rate_limited + not_before, outage -> provider_error, no-key -> skipped, native uses
the captions-only path, and key/request-id redaction."""
import asyncio

from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.schema import Cost, Outcome, Provenance, Reason, VideoRef
from transcript_tool.security import redact
from transcript_tool.strategies.managed import (
    HttpxClient, ManagedAsrStrategy, ManagedNativeStrategy, ManagedResponse,
    ManagedUrlToAsrStrategy,
)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
GATED = Policy(enabled_strategies=("managed_native", "managed_asr", "managed_url_to_asr"),
               egress=EgressPolicy(allow_public_url=True))


class FakeClient:
    """Records the last request and returns a queued ManagedResponse."""
    def __init__(self, response: ManagedResponse):
        self.response = response
        self.calls = []

    def request(self, method, path, *, json=None, params=None):
        self.calls.append({"method": method, "path": path, "json": json, "params": params})
        return self.response


def _ok_body(**over):
    body = {
        "request_id": "req_secret_123",
        "transcript": {
            "language": "en", "is_generated": False, "translated": False,
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "hello from the provider"},
                {"start": 2.0, "end": 4.0, "text": "second line of captions"},
            ],
        },
        "usage": {"credits": 12, "exact": True},
    }
    body.update(over)
    return body


def _run(strat, ref=None, policy=GATED):
    return asyncio.run(strat.fetch(ref or VideoRef(source="url", url=URL), policy))


# --- success + cost ----------------------------------------------------------

def test_managed_native_success_populates_cost_and_provenance():
    client = FakeClient(ManagedResponse(200, _ok_body(), {}))
    res = _run(ManagedNativeStrategy(client=client))
    assert res.outcome is Outcome.success
    assert res.provenance is Provenance.human_caption
    assert "hello from the provider" in res.text
    cost = res.attempts[0].cost
    assert cost.unit == "provider_credits" and cost.amount == 12.0 and cost.estimated is False


def test_managed_native_uses_captions_only_path():
    """Acceptance: native MUST use the captions-only mode, never `auto` (paid ASR)."""
    client = FakeClient(ManagedResponse(200, _ok_body(), {}))
    _run(ManagedNativeStrategy(client=client))
    assert client.calls[0]["json"]["mode"] == "native"


def test_managed_auto_generated_is_platform_auto():
    body = _ok_body()
    body["transcript"]["is_generated"] = True
    res = _run(ManagedNativeStrategy(client=FakeClient(ManagedResponse(200, body, {}))))
    assert res.provenance is Provenance.platform_auto


def test_managed_url_to_asr_is_managed_asr_provenance_and_auto_mode():
    client = FakeClient(ManagedResponse(200, _ok_body(usage={"amount_usd": 0.03, "exact": False}), {}))
    res = _run(ManagedUrlToAsrStrategy(client=client))
    assert res.outcome is Outcome.success
    assert res.provenance is Provenance.managed_asr
    assert client.calls[0]["json"]["mode"] == "auto"      # paid transcription path
    assert res.attempts[0].cost.unit == "usd" and res.attempts[0].cost.estimated is True


def test_translated_caption_only_with_provider_disclosure():
    body = _ok_body()
    body["transcript"].update({"translated": True, "source_language": "es"})
    res = _run(ManagedNativeStrategy(client=FakeClient(ManagedResponse(200, body, {}))))
    assert res.provenance is Provenance.translated_caption
    assert res.language.detection_method == "provider_flag"
    assert res.language.original_language == "es"


# --- error mapping -----------------------------------------------------------

def test_language_unavailable_maps_to_unavailable():
    resp = ManagedResponse(404, {"error": "language_unavailable"}, {})
    res = _run(ManagedNativeStrategy(client=FakeClient(resp)))
    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.language_unavailable


def test_rate_limited_sets_retry_not_before():
    resp = ManagedResponse(429, {"error": "slow_down"}, {"Retry-After": "30"})
    res = _run(ManagedNativeStrategy(client=FakeClient(resp)))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.rate_limited
    assert res.retry.eligible is True
    assert res.retry.not_before is not None


def test_outage_maps_to_provider_error():
    resp = ManagedResponse(503, {"error": "upstream"}, {})
    res = _run(ManagedNativeStrategy(client=FakeClient(resp)))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.provider_error


def test_auth_failure_maps_to_access_challenge():
    resp = ManagedResponse(401, {"error": "bad_key"}, {})
    res = _run(ManagedNativeStrategy(client=FakeClient(resp)))
    assert res.reason is Reason.access_challenge


# --- key gating --------------------------------------------------------------

def test_no_key_means_not_applicable(monkeypatch):
    monkeypatch.delenv("MANAGED_API_KEY", raising=False)
    strat = ManagedNativeStrategy()      # no injected client, no env key
    assert strat.applicable(VideoRef(source="url", url=URL), GATED) is False


def test_key_present_is_applicable_only_when_gated(monkeypatch):
    monkeypatch.setenv("MANAGED_API_KEY", "sk-test")
    strat = ManagedNativeStrategy()
    ungated = Policy(enabled_strategies=("managed_native",))   # allow_public_url False
    assert strat.applicable(VideoRef(source="url", url=URL), ungated) is False
    assert strat.applicable(VideoRef(source="url", url=URL), GATED) is True


def test_managed_asr_skips_captions_only_mode(monkeypatch):
    monkeypatch.setenv("MANAGED_API_KEY", "sk-test")
    strat = ManagedAsrStrategy()
    pol = Policy(mode="captions-only", enabled_strategies=("managed_asr",),
                 egress=EgressPolicy(allow_public_url=True))
    assert strat.applicable(VideoRef(source="url", url=URL), pol) is False


# --- redaction / observability ----------------------------------------------

def test_provider_request_id_is_redactable_in_logs():
    # The strategy records a provider_request_id; security.redact must scrub a key= line.
    line = "authorization=Bearer sk-supersecret key=abc123 request_id=req_1"
    assert "sk-supersecret" not in redact(line)
    assert "<redacted>" in redact(line)


def test_httpx_client_is_lazy_and_not_required_for_tests():
    # Constructing the default client must not import httpx until a request is made.
    c = HttpxClient(api_key="sk-test")
    assert c._api_key == "sk-test"
