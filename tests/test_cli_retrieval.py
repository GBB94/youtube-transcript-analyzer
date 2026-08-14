"""CLI wiring for the retrieval half: new verbs parse, gates hold, doctor
reports the retrieval section. No network, no models."""
import json

from transcript_tool.cli import build_parser, main


def test_new_verbs_parse():
    p = build_parser()
    add = p.parse_args(["corpus", "add", "pod", "--channel", "@x", "--diarize"])
    assert add.slug == "pod" and add.diarize
    build = p.parse_args(["corpus", "build", "pod", "--no-context", "--embedder", "local"])
    assert build.no_context and build.embedder == "local"
    ask = p.parse_args(["ask", "what about compute?", "--k", "4", "--json"])
    assert ask.question == "what about compute?" and ask.k == 4 and ask.json
    ev = p.parse_args(["eval", "pod", "--compare", "base.json", "--threshold", "0.1"])
    assert ev.compare == "base.json" and ev.threshold == 0.1


def test_corpus_add_is_gated_on_public_url(tmp_path, capsys):
    rc = main(["corpus", "add", "pod", "aaaaaaaaaaa",
               "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 2
    assert "--enable-public-url" in capsys.readouterr().err


def test_ask_without_a_corpus_is_usage_error(tmp_path, capsys):
    rc = main(["ask", "anything", "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 2
    assert "--source" in capsys.readouterr().err


def test_eval_without_golden_is_usage_error(tmp_path, capsys):
    rc = main(["eval", "pod", "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 2
    assert "golden" in capsys.readouterr().err


def test_doctor_reports_the_retrieval_section(tmp_path, capsys):
    main(["doctor", "--strategies", "uploaded_caption",
          "--corpus-root", str(tmp_path / "corpus")])
    out = capsys.readouterr()
    assert "retrieval:" in out.err and "lancedb" in out.err
    payload = json.loads(out.out)
    assert "retrieval" in payload and isinstance(payload["retrieval"]["ready"], bool)


def test_corpus_status_runs_on_seeded_store(tmp_path, capsys):
    import asyncio
    from transcript_tool.corpus.ingest import corpus_add
    from transcript_tool.corpus.store import CorpusStore
    from transcript_tool.policy import Policy
    from transcript_tool.schema import (
        Language, Provenance, Result, Segment, TimestampType, VideoRef,
    )

    vid = "aaaaaaaaaaa"
    ref = VideoRef(platform="youtube", id=vid,
                   url=f"https://www.youtube.com/watch?v={vid}", source="url")
    result = Result.make_success(
        ref, provenance=Provenance.platform_auto, text="hello there.",
        segments=[Segment(start=0.0, end=2.0, text="hello there.")],
        language=Language(requested=["en"], selected="en"),
        timestamp_type=TimestampType.caption_cue, raw_text="hello there.")

    async def pull(r, policy, cache):
        return result

    store = CorpusStore(tmp_path / "corpus")
    asyncio.run(corpus_add(store, "pod", [ref], policy=Policy(), pull=pull))

    rc = main(["corpus", "status", "pod", "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["videos"] == 1 and payload["stale"] == 1   # not yet indexed


def test_retrieve_verb_emits_grounded_payload(tmp_path, capsys, monkeypatch):
    """`transcript retrieve` is the external-answerer seam: JSON hits with full
    ChunkMeta, deep links, and the grounding contract embedded in the payload."""
    from test_index_build import FakeEmbedder, _seed, _tokens
    from transcript_tool.retrieval.build import corpus_build
    from transcript_tool.retrieval.chunk import ChunkConfig
    import transcript_tool.retrieval.embed as embed_mod

    store = _seed(tmp_path)
    corpus_build(store, "pod", embedder=FakeEmbedder(),
                 chunk_config=ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1),
                 token_counter=_tokens)
    monkeypatch.setattr(embed_mod, "get_embedder", lambda kind: FakeEmbedder())

    rc = main(["retrieve", "compute tradeable commodity", "--source", "pod",
               "--no-rerank", "--k", "3",
               "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "Cite only chunk_ids" in payload["contract"]
    assert payload["sources"] == ["pod"] and payload["merged_by"] is None
    assert payload["versions"]["pod"]["embed_model"] == "fake-bow@1#32"
    top = payload["hits"][0]
    assert "compute" in top["text"]
    assert top["url_with_timestamp"].endswith(f"t={int(top['start_s'])}s")
    assert "search_text" not in top          # verbatim text and context only
    assert top["fused_rank"] == 1

    # No hits -> exit 1; missing index -> exit 2.
    rc = main(["retrieve", "anything", "--source", "pod",
               "--no-rerank", "--corpus-root", str(tmp_path / "corpus"),
               "--speaker", "NOBODY"])
    assert rc == 1
    rc = main(["retrieve", "anything", "--source", "ghost",
               "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 2


def test_retrieve_across_all_sources_merges_with_attribution(tmp_path, capsys, monkeypatch):
    """--source all searches every corpus and merges honestly: rank-interleaved
    without rerank, every hit still naming its source_slug."""
    import asyncio
    from test_index_build import FakeEmbedder, _tokens, _success, _ref, SENTS_A, SENTS_B
    from transcript_tool.corpus.ingest import corpus_add
    from transcript_tool.corpus.records import CorpusVideo
    from transcript_tool.corpus.store import CorpusStore
    from transcript_tool.policy import Policy
    from transcript_tool.retrieval.build import corpus_build
    from transcript_tool.retrieval.chunk import ChunkConfig
    import transcript_tool.retrieval.embed as embed_mod

    store = CorpusStore(tmp_path / "corpus", index_root=tmp_path / "index")
    cfg = ChunkConfig(max_tokens=30, min_tokens=8, overlap_ratio=0.1)
    for slug, vid, sents in (("showa", "aaaaaaaaaaa", SENTS_A),
                             ("showb", "bbbbbbbbbbb", SENTS_B)):
        result = _success(vid, sents)

        async def pull(ref, policy, cache, _r=result):
            return _r

        meta = {vid: CorpusVideo(id=vid, url=f"https://youtu.be/{vid}", title=slug)}
        asyncio.run(corpus_add(store, slug, [_ref(vid)], policy=Policy(),
                               meta=meta, pull=pull))
        corpus_build(store, slug, embedder=FakeEmbedder(), chunk_config=cfg,
                     token_counter=_tokens)
    monkeypatch.setattr(embed_mod, "get_embedder", lambda kind: FakeEmbedder())

    rc = main(["retrieve", "compute commodity fusion capital", "--source", "all",
               "--no-rerank", "--k", "6", "--corpus-root", str(tmp_path / "corpus")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"] == ["showa", "showb"]
    assert payload["merged_by"] == "per_index_rank"
    assert set(payload["versions"]) == {"showa", "showb"}
    slugs_hit = {h["source_slug"] for h in payload["hits"]}
    assert slugs_hit == {"showa", "showb"}       # both corpora contribute


def test_merge_traces_refuses_mixed_rerank_states():
    import pytest
    from transcript_tool.retrieval.retrieve import RetrievalTrace, merge_traces
    a = RetrievalTrace(query="q", where=None, n_candidates=50, reranked=True)
    b = RetrievalTrace(query="q", where=None, n_candidates=50, reranked=False)
    with pytest.raises(ValueError):
        merge_traces([a, b], k=4)
