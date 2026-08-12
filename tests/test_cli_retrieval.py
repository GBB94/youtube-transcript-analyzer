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
