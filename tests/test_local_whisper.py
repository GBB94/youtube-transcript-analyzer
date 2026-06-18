import asyncio
import time

from transcript_tool.policy import Policy
from transcript_tool.provisioning import ModelSpec, ModelUnavailable
from transcript_tool.schema import Outcome, Provenance, Reason, TimestampType, VideoRef
from transcript_tool.strategies.local_whisper import (
    ASRResult, ASRSegment, LocalWhisperStrategy,
)
from transcript_tool import asr_eval


def _audio(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF....")            # content irrelevant; transcriber is faked
    return VideoRef(platform="local", source="uploaded_file", path=str(p))


def _policy():
    return Policy(enabled_strategies=("local_whisper",), mode="prefer-captions")


def test_local_whisper_success(tmp_path):
    def fake(media_path, langs, spec):
        return ASRResult(
            segments=[ASRSegment(0.0, 2.0, "spoken words here", 0.01),
                      ASRSegment(2.0, 4.0, "more speech", 0.02)],
            language="en", language_probability=0.97, raw_ref="sha256:deadbeef")
    s = LocalWhisperStrategy(transcriber=fake)
    res = asyncio.run(s.fetch(_audio(tmp_path), _policy()))
    assert res.outcome is Outcome.success
    assert res.provenance is Provenance.local_asr
    assert res.timestamp_type is TimestampType.asr_segment
    assert res.language.detection_confidence == 0.97
    assert res.model.compute_type == "int8"


def test_no_speech(tmp_path):
    def fake(media_path, langs, spec):
        return ASRResult(segments=[ASRSegment(0.0, 2.0, "", 0.99)], language="en")
    res = asyncio.run(LocalWhisperStrategy(transcriber=fake).fetch(_audio(tmp_path), _policy()))
    assert res.outcome is Outcome.unavailable
    assert res.reason is Reason.no_speech


def test_missing_model_is_missing_dependency(tmp_path):
    def fake(media_path, langs, spec):
        raise ModelUnavailable("not provisioned")
    res = asyncio.run(LocalWhisperStrategy(transcriber=fake).fetch(_audio(tmp_path), _policy()))
    assert res.outcome is Outcome.failed
    assert res.reason is Reason.missing_dependency


def test_timeout(tmp_path):
    def slow(media_path, langs, spec):
        time.sleep(0.5)
        return ASRResult(segments=[ASRSegment(0.0, 1.0, "late", 0.0)])
    s = LocalWhisperStrategy(transcriber=slow, timeout_s=0)
    res = asyncio.run(s.fetch(_audio(tmp_path), _policy()))
    assert res.reason is Reason.timeout


def test_captions_only_skips_asr(tmp_path):
    s = LocalWhisperStrategy(transcriber=lambda *a: None)
    assert s.applicable(_audio(tmp_path), Policy(enabled_strategies=("local_whisper",),
                                                 mode="captions-only")) is False


# --- jiwer regression harness -------------------------------------------------

def test_wer_cer():
    assert asr_eval.wer("the cat sat", "the cat sat") == 0.0
    assert asr_eval.wer("the cat sat on the mat", "the cat sat") > 0.0


def test_regression_pass_and_fail():
    clips = [asr_eval.Clip(path="a.wav", language="en", reference="the quick brown fox")]
    good = asr_eval.run_regression(clips, lambda p, l: "the quick brown fox")
    assert good.passed and good.mean_wer == 0.0
    bad = asr_eval.run_regression(clips, lambda p, l: "completely different words entirely",
                                  max_wer=0.1)
    assert not bad.passed
