"""R7 — on-demand diarization (§3, §18): per-episode opt-in, mixed corpus, the
same retrieval layer, speaker-filtered ask. Diarizer / audio / pull all injected."""
import asyncio

from transcript_tool.corpus.diarize import diarize_ingest
from transcript_tool.corpus.ingest import corpus_add
from transcript_tool.corpus.records import CorpusVideo
from transcript_tool.corpus.store import CorpusStore
from transcript_tool.policy import EgressPolicy, Policy
from transcript_tool.retrieval.build import corpus_build
from transcript_tool.retrieval.chunk import ChunkConfig
from transcript_tool.retrieval.index import ChunkIndex
from transcript_tool.retrieval.retrieve import Filters, retrieve
from transcript_tool.schema import (
    Language, Provenance, Result, Segment, TimestampType, VideoRef,
)

from test_index_build import FakeEmbedder, _tokens

VID_CAP, VID_DIA = "aaaaaaaaaaa", "bbbbbbbbbbb"
CFG = ChunkConfig(max_tokens=20, min_tokens=5, overlap_ratio=0.1)

CAP_SENTS = ["the housing market is softening in coastal cities.",
             "robotaxis are scaling faster than projected."] * 2
DIA_SENTS = ["i think compute becomes the dominant tradeable commodity.",
             "i disagree completely and here is my counterargument.",
             "well let us look at the actual capex numbers together.",
             "fine but the futures market already prices this in."]


def _ref(vid):
    return VideoRef(platform="youtube", id=vid,
                    url=f"https://www.youtube.com/watch?v={vid}", source="url")


def _success(ref, sentences, provenance=Provenance.platform_auto):
    segs = [Segment(start=i * 5.0, end=(i + 1) * 5.0, text=t)
            for i, t in enumerate(sentences)]
    return Result.make_success(
        ref, provenance=provenance, text=" ".join(sentences), segments=segs,
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.asr_segment, raw_text=" ".join(sentences))


class FakeDiarizer:
    """Alternates two speakers by segment index."""
    def __init__(self):
        self.calls = []

    def assign_speakers(self, audio_path, segments):
        self.calls.append(audio_path)
        return [f"SPEAKER_{i % 2:02d}" for i in range(len(segments))]


def _audio_fn(tmp_path):
    def acquire(ref, policy):
        workdir = tmp_path / f"audio_{ref.id}"
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / f"{ref.id}.m4a"
        path.write_bytes(b"fake-audio")
        return str(path)
    return acquire


async def _asr_pull(ref, policy, cache):
    # The diarize path must have re-routed to ASR over the acquired audio file.
    assert policy.mode == "asr-only" and policy.enabled_strategies == ("local_whisper",)
    assert ref.source == "uploaded_file" and ref.path.endswith(".m4a")
    return _success(ref, DIA_SENTS, provenance=Provenance.local_asr)


def _policy():
    return Policy(egress=EgressPolicy(allow_network=True, allow_public_url=True))


def _seed_caption(store):
    async def pull(ref, policy, cache):
        return _success(ref, CAP_SENTS)
    meta = {VID_CAP: CorpusVideo(id=VID_CAP, url=f"https://youtu.be/{VID_CAP}",
                                 title="EP cap", upload_date="2026-07-01")}
    asyncio.run(corpus_add(store, "pod", [_ref(VID_CAP)], policy=Policy(),
                           meta=meta, pull=pull))


def test_diarize_is_per_episode_and_yields_a_mixed_corpus(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    _seed_caption(store)

    diarizer = FakeDiarizer()
    report = diarize_ingest(store, "pod", [_ref(VID_DIA)], policy=_policy(),
                            meta={VID_DIA: CorpusVideo(id=VID_DIA,
                                                       url=f"https://youtu.be/{VID_DIA}",
                                                       title="EP dia",
                                                       upload_date="2026-07-14")},
                            diarizer=diarizer, pull=_asr_pull,
                            audio_fn=_audio_fn(tmp_path))
    assert report.pulled == [VID_DIA] and not report.failed
    assert diarizer.calls and diarizer.calls[0].endswith(f"{VID_DIA}.m4a")

    # Mixed corpus: only the chosen episode carries speakers (§3).
    assert store.load_record("pod", VID_DIA).diarized
    assert not store.load_record("pod", VID_CAP).diarized
    assert store.entry("pod", VID_DIA)["diarized"] is True
    assert store.entry("pod", VID_DIA)["provenance"] == "local_asr"

    # The acquired audio never outlives the ingest.
    assert not (tmp_path / f"audio_{VID_DIA}").exists()


def test_speaker_labels_flow_to_retrieval_filters(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    _seed_caption(store)
    diarize_ingest(store, "pod", [_ref(VID_DIA)], policy=_policy(),
                   diarizer=FakeDiarizer(), pull=_asr_pull,
                   audio_fn=_audio_fn(tmp_path))
    corpus_build(store, "pod", embedder=FakeEmbedder(), chunk_config=CFG,
                 token_counter=_tokens)

    index = ChunkIndex(store.index_dir("pod"))
    # The SAME retrieval layer queries both kinds (§3)...
    both = retrieve(index, "housing compute commodity", embedder=FakeEmbedder())
    assert {h.chunk["video_id"] for h in both.hits} >= {VID_CAP, VID_DIA}

    # ...and --speaker narrows to the diarized episode's labelled chunks.
    s0 = retrieve(index, "compute commodity capex", embedder=FakeEmbedder(),
                  filters=Filters(speaker="SPEAKER_00"))
    assert s0.hits and all(h.chunk["speaker"] == "SPEAKER_00" for h in s0.hits)
    assert all(h.chunk["video_id"] == VID_DIA for h in s0.hits)


def test_already_diarized_episode_is_skipped_without_force(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    kw = dict(policy=_policy(), diarizer=FakeDiarizer(), pull=_asr_pull,
              audio_fn=_audio_fn(tmp_path))
    diarize_ingest(store, "pod", [_ref(VID_DIA)], **kw)
    report = diarize_ingest(store, "pod", [_ref(VID_DIA)], **kw)
    assert report.skipped_existing == [VID_DIA] and not report.pulled


def test_diarizing_a_caption_episode_supersedes_it(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    _seed_caption(store)
    store.update_entry("pod", VID_CAP, indexed_at="2026-08-01T00:00:00+00:00")

    async def asr_pull(ref, policy, cache):
        return _success(ref, DIA_SENTS, provenance=Provenance.local_asr)

    report = diarize_ingest(store, "pod", [_ref(VID_CAP)], policy=_policy(),
                            diarizer=FakeDiarizer(), pull=asr_pull,
                            audio_fn=_audio_fn(tmp_path))
    assert report.superseded == [VID_CAP]
    entry = store.entry("pod", VID_CAP)
    assert entry["diarized"] is True and entry["indexed_at"] is None  # stamps reset
    raws = list((tmp_path / "corpus" / "pod" / "raw").glob(f"*{VID_CAP}.json"))
    assert len(raws) == 1                                   # one raw file per video


def test_diarizer_failure_is_reported_and_banks_nothing(tmp_path):
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")

    class BrokenDiarizer:
        def assign_speakers(self, audio_path, segments):
            raise RuntimeError("no voices found")

    report = diarize_ingest(store, "pod", [_ref(VID_DIA)], policy=_policy(),
                            diarizer=BrokenDiarizer(), pull=_asr_pull,
                            audio_fn=_audio_fn(tmp_path))
    assert report.failed and "diarization_error" in report.failed[0]["reason"]
    assert store.video_ids("pod") == set()
    assert not (tmp_path / f"audio_{VID_DIA}").exists()     # cleanup still ran


def test_diarize_requires_the_public_url_gate(tmp_path):
    import pytest
    from transcript_tool.media import MediaError
    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    with pytest.raises(MediaError):
        diarize_ingest(store, "pod", [_ref(VID_DIA)], policy=Policy(),
                       diarizer=FakeDiarizer(), pull=_asr_pull)
