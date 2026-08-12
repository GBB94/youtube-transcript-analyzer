# Retrieval Brief v1 — Corpus & Query ("the ask half")

*Turn the transcripts the pipeline already produces into a durable, queryable
knowledge base: ask a question in natural language, get an answer grounded in
what was actually said, with a citation back to the episode and the exact
timestamp. Caption acquisition is the hard, fragile half and is already built
(`DESIGN.md`); this is the easy, stable half — but "easy" only if the storage and
retrieval contracts are right from the start.*

**Status:** BUILT — R0–R7 implemented 2026-08-12 (unit-tested via injected
adapters; live-model paths are lazy optional extras verified outside CI).
Companion to `DESIGN.md` (the v3 acquisition brief).
This realizes the downstream direction anticipated in `DESIGN.md §18` ("run
extraction on each transcript → turn video into structured signal"), generalized
from feature-extraction to open-ended retrieval-augmented question answering.
Where this brief and `DESIGN.md` disagree on a shared contract, `DESIGN.md` wins;
if you change a shared contract, update both.

---

## 0. Scope & posture

Built for personal use now (one channel, ~2 years, ~100 episodes), engineered so
it can grow (many channels, incremental monitoring, a shared server index) without
a rewrite. Two consequences carried over from the acquisition brief:

- **Canonical vs. derived is sacred.** Pulling from YouTube is the slow,
  rate-limited, occasionally-blocked step. We do it **once** and treat the result
  as the source of truth we never discard. Everything downstream — readable
  Markdown, chunks, contextual blurbs, embeddings, indexes — is **derived and
  fully rebuildable** from the canonical layer with **no network access**. A
  corrupted or version-bumped index is a rebuild, never a re-pull.
- **Same two profiles, same core.** `local` (a single on-disk index, no server)
  and `server` (a shared vector store) run the identical ingest/retrieval code
  (§16).

**Reliability boundary (honest, as in `DESIGN.md §3`).** RAG makes query cost
*independent of corpus size* — a question retrieves a fixed handful of chunks
whether the corpus is 100 episodes or 10,000. What degrades with scale is
retrieval **precision**, not cost; §9 is the machinery that holds precision up.
This system answers **what is supported by the retrieved transcript passages** —
not "anything the podcast ever implied." An answer with no supporting passages is
an *insufficient-evidence* outcome, not a guess (§10).

---

## 1. Purpose

The acquisition pipeline converts video into clean, provenance-rich transcripts.
On its own that is a searchable archive, not an *answerable* one: you still have to
know which episode to open and where to scrub to. This subsystem closes that gap.
The unit of value is a **grounded answer with a deep link** — "they discussed
compute-as-a-tradeable-asset at 12:47 in EP 278" plus a clickable
`…&t=767s` — because with spoken content the user wants the exact moment a claim
was made, not merely a passage that mentions the topic.

---

## 2. Relationship to the acquisition pipeline (integration points)

This subsystem **consumes** the acquisition `Result` and never re-implements
acquisition. Concrete seams (names from `schema.py`):

- **Input is a successful `Result`** (`Outcome.success`) with `segments:
  list[Segment]` (`start`, `end`, `text`), `video_ref: VideoRef` (`platform`,
  `id`, `url`), `provenance` (`human_caption | platform_auto | local_asr |
  managed_asr | translated_caption`), `language`, and `raw_cues_ref`. All of this
  is carried through into the canonical corpus record (§5) — provenance is not
  dropped; a `platform_auto` caption and a `local_asr` transcript are different
  evidence and stay distinguishable at query time.
- **Discovery is reused as-is.** `find_videos(channel_id=…, include_shorts=False)`
  / `transcript find --channel` produce the video refs; corpus ingest pulls them
  through the existing staged pipeline. No new YouTube access path is introduced,
  so the compliance posture (`DESIGN.md §4`) and `EgressPolicy` gate are inherited
  unchanged.
- **Markdown rendering is reused/extended.** `web/markdown.py` already groups
  segments into ~45 s windows with clickable timestamp deep-links; the readable
  layer (§4) reuses that renderer. Chunking for retrieval is a **separate,
  finer-grained** pass (§6) — the 45 s reading windows are lossy and are *not* the
  retrieval unit.

**Non-negotiable inherited from `DESIGN.md`:** never flatten timestamps. Every
derived unit, down to the individual chunk, carries `{video_id, start, end}` so
every answer is deep-linkable and every retrieval is auditable.

---

## 3. Goals, non-goals & the honest boundary

**Goals:** a durable canonical corpus with an incremental (only-new) ingest;
deterministic, versioned derivation (chunk → contextualize → embed → index);
hybrid retrieval (dense + lexical) with a rerank stage; grounded answers with
episode+timestamp citations; a built-in evaluation harness so retrieval changes
are *measured, not vibe-checked*; local-first and private by default.

**Non-goals (v1):** a hosted multi-tenant service; cross-channel entity resolution
/ knowledge graph; real-time indexing of livestreams; translation (source language
only, per `DESIGN.md §3`); fine-tuning an embedding model; **automatic diarization
of every ingest.**

**Speaker attribution is an on-demand, per-episode capability — never automatic.**
Captions carry no speaker labels, and diarization (audio → Whisper → speaker
segmentation) is materially heavier than a caption pull. So it is **default off**:
the standard path stays captions-only and fast. When a specific episode warrants
"who said what" — a debate you want to attribute, a guest whose argument you want
to isolate — you opt that one video in (`corpus add … --diarize`, §11), and only it
carries speaker labels. The result is a **mixed corpus**: most episodes caption-only,
a chosen few diarized, all queried by the same retrieval layer (§9). The tooling is
built (R7, §18) so this is a one-flag operation whenever you need it, not a project.

**Honest boundary:** retrieval quality is bounded by transcript quality. Auto-
captions (~95% word accuracy, no speaker labels) will occasionally mis-render a
name or elide a word; the system surfaces `provenance` so the user can weight a
`platform_auto` answer accordingly, and never claims a confidence the source can't
support.

---

## 4. Corpus storage model (three layers)

One directory per source, three layers, plus a manifest. Default root is inside
the repo under `corpus/` and **git-ignored** (transcripts are large and
re-derivable; do not bloat history).

```
corpus/
  <source-slug>/                     # e.g. moonshots-diamandis
    manifest.json                    # index of what we have (see below)
    raw/                             # CANONICAL layer — never discarded
      2026-07-14__Q6PTLG71NGc.json   #   one CorpusRecord per video (§5)
    markdown/                        # READABLE layer — derived
      2026-07-14__Q6PTLG71NGc.md     #   deep-linked, from web/markdown.py
index/
  <source-slug>/                     # INDEX layer — derived, rebuildable, git-ignored
    lancedb/                         #   ONE store: dense vectors + native FTS (BM25) + chunk rows (§9)
    build.json                       #   versions this index was built at (§14)
```

- **Canonical layer (`raw/*.json`).** The acquisition `Result` plus corpus
  metadata (§5), segment-level timestamps preserved exactly. **This is the only
  layer that is expensive to reproduce.** Everything else is a pure function of it.
- **Readable layer (`markdown/*.md`).** Human-skimmable, deep-linked; derived
  via the existing renderer. Convenience, not a retrieval input.
- **Index layer (`index/…`).** The queryable artifacts. Blowing this away and
  rebuilding from the canonical layer must be a routine, offline, idempotent
  operation.

**Naming:** `YYYY-MM-DD__<video_id>.<ext>` — date-sortable, with the immutable
`video_id` as the stable key (titles and episode numbers are unreliable and
mutable; never key on them).

**`manifest.json`** is the ingest ledger and the basis of incrementality (§12):
per video — `video_id`, `title`, `upload_date`, `url`, `duration_s`,
`provenance`, `content_hash` (of canonical text+timing), `pulled_at`,
`markdown_built_at`, `chunker_version`, `context_version`, `embed_model`,
`indexed_at`. It answers "what do we have, and is any of it stale relative to the
current derivation versions?" without scanning every file.

---

## 5. Canonical record schema (`CorpusRecord`)

A superset of the acquisition `Result`, versioned independently. Additive over
`schema.py` — it embeds, never mutates, the `Result`.

```jsonc
{
  "corpus_schema_version": "1.0.0",
  "source_slug": "moonshots-diamandis",
  "video": {                          // from VideoRef + discovery enrichment
    "platform": "youtube", "id": "Q6PTLG71NGc",
    "url": "https://www.youtube.com/watch?v=Q6PTLG71NGc",
    "title": "…", "upload_date": "2026-07-14", "duration_s": 8960,
    "channel_id": "UC…", "channel_title": "Moonshots with Peter Diamandis",
    "chapters": [ { "start": 0.0, "title": "Cold open" }, … ]  // if published; §6
  },
  "provenance": "platform_auto",      // carried through from Result, never dropped
  "language": { "selected": "en", "spoken_detected": "en", "detection_confidence": 0.98 }, // Language, schema.py
  "segments": [ { "start": 0.0, "end": 4.2, "text": "…" } ],   // canonical timing
  "raw_cues_ref": "sha256:…",         // same handle the pipeline emits
  "content_hash": "sha256:…",         // over normalized text + timing; drives §12
  "pulled_at": "ISO-8601",
  "acquisition_attempts": [ … ]       // Result.attempts, retained for audit
}
```

Bump `CORPUS_SCHEMA_VERSION` on any behavioural change; it is a rebuild input
(§14). The record deliberately keeps `provenance` and `acquisition_attempts` so a
downstream answer can be traced all the way back to *how* the transcript was
obtained.

---

## 6. Chunking — segment-aware, boundary-respecting, timestamp-preserving

Chunking is the highest-leverage retrieval decision and is **deterministic and
versioned** (`CHUNKER_VERSION`), exactly as normalization is in `DESIGN.md §9`.

**Target.** 300–600 tokens per chunk with ~10–15% overlap (roughly one sentence /
10–15 s). Overlap prevents the "context-cliff" failure where an answer straddles a
boundary. These are defaults, not constants — they are config, and the eval
harness (§13) is how we tune them rather than guessing.

**Boundaries, in priority order:**
1. **Published chapter marks** (`video.chapters`) when present — creators mark
   topic shifts for free; respect them as hard boundaries.
2. **Utterance / sentence ends** from segment timing — prefer a natural pause over
   splitting mid-thought.
3. **Token budget** as the fallback cap.

Never split blind at a fixed token count when a natural boundary is within reach.

**`ChunkMeta` (stored on every chunk — this is what makes citations work):**

```jsonc
{
  "chunk_id": "Q6PTLG71NGc:0007",     // video_id + ordinal, stable + rebuildable
  "video_id": "Q6PTLG71NGc",
  "source_slug": "moonshots-diamandis",
  "url": "https://www.youtube.com/watch?v=Q6PTLG71NGc",
  "title": "…", "upload_date": "2026-07-14",
  "start_s": 767.0, "end_s": 812.0,   // NEVER flattened — powers &t=767s deep links
  "provenance": "platform_auto",       // carried from the record
  "speaker": null,                     // null unless this episode was ingested --diarize (§18)
  "text": "…",                         // verbatim chunk text
  "context": "…",                      // §7 contextual blurb (nullable)
  "chunker_version": "1.0.0", "context_version": "1.0.0"
}
```

---

## 7. Contextual enrichment (Anthropic Contextual Retrieval)

The single highest-leverage *ingest-time* quality move for conversational audio.
Podcasts are dense with floating reference — a chunk reading "he thinks it 10x's by
2027" is nearly unsearchable because "he," the subject, and the episode all live in
neighbouring chunks. Before embedding, prepend a short (~50–100 token)
LLM-generated context line to each chunk — e.g. *"From EP 278 (2026-07-14);
Dave is arguing that compute becomes a tradeable commodity like oil."* The chunk is
embedded and lexically indexed as **context + text**; the **verbatim `text` is
preserved separately** and is what gets quoted and cited (we never cite the
synthesized context as if it were spoken).

Published result: contextual embeddings cut retrieval failures ~49% alone, ~67%
combined with reranking (§9). Cost is controlled by generating the blurbs with a
cheap model and **prompt-caching the episode body** across its chunks, so the
transcript is read once per episode, not once per chunk.

**Contracts:** deterministic prompt, pinned model, `CONTEXT_VERSION` bumped on any
prompt/model change (a rebuild input, §14). This step calls a model provider and is
therefore **egress** — it is gated by the same posture as any other network call
(§15): permitted by default in the `local` profile against the operator's own key,
disclosed, and skippable (`--no-context`; §11, §15).

---

## 8. Embeddings

**Pluggable behind one interface**, pinned by **name + revision + dimension**
(mirrors the ASR model-pinning discipline in `DESIGN.md §7`). Default is a **local
sentence-embedding model** (e.g. a BGE/GTE-small class model): free, offline,
private — no transcript text leaves the machine. An **API embedder** (higher
recall, small per-token cost, egress) is a config swap, not a code change, because
the index is rebuildable (§14) — start local, and only upgrade if the eval harness
(§13) says recall demands it.

Embeddings are **batched** and written alongside the chunk row so the vector store
and the chunk metadata stay in one place (§9). Changing the embedder is an index
rebuild, never a re-pull.

---

## 9. Index & retrieval — hybrid, two-stage

This is the machinery that keeps precision up as the corpus grows. Two indexes over
the same chunks, and a two-stage retrieve→rerank funnel.

- **Dense** — vector search over the contextualized-chunk embeddings, stored in
  **LanceDB** (a single memory-mapped on-disk table, no server; scales from ~100
  episodes to millions of vectors, so the `local` build future-proofs the `server`
  build). The chunk row stores the vector *and* the full `ChunkMeta`, so a hit
  needs no join.
- **Lexical (BM25)** — a keyword index over the same text. Essential for spoken
  content: dense vectors fuzz proper nouns and jargon ("Gemini 3", "Kush Bavaria",
  "PO-token"); BM25 nails them exactly. Hybrid beats pure-vector on transcripts.
  **Implemented as LanceDB's native full-text index on the same table** (§20.4):
  one store holds the vectors, the BM25 index, and `ChunkMeta`, so the two sides
  cannot drift, there is no second directory to rebuild or lock, and hybrid
  fusion uses LanceDB's built-in RRF reranker. A standalone BM25 library
  (`bm25s`) is the recorded fallback only if FTS proves limiting.

**Two-stage funnel (the pattern that survives at scale):**
1. **Retrieve wide, cheap.** Hybrid — take top-N from each of dense and BM25 (N≈50)
   and fuse (Reciprocal Rank Fusion).
2. **Rerank narrow, precise.** A cross-encoder reranker scores the fused candidates
   against the query; keep the top `k` (≈8) for the answerer. Reranking a top-50 is
   cheap and lifts top-k precision substantially — it is the difference between a
   plausible-but-wrong passage and the right one winning.

**Metadata filtering is first-class.** Because every chunk carries structured
metadata (§6), retrieval can be constrained — `--since 2026-01-01`, a specific
`--channel`, or (later) a `speaker` — which both shrinks the search space and
powers time-scoped questions. Filters apply *before* ranking.

Defaults (`N`, `k`, fusion weights, rerank on/off) are **config**, set and moved by
the eval harness (§13), never hardcoded folklore.

---

## 10. Query & answer — grounded, with a discriminated outcome

Answering mirrors the **discriminated-outcome discipline** that `DESIGN.md §6`
treats as sacred. An answer is one of:

```jsonc
"answer_outcome": "grounded | insufficient_evidence | failed"
```

- **`grounded`** — the answer is supported by retrieved passages and **must** carry
  ≥1 citation. A citation is `{video_id, title, start_s, url_with_timestamp,
  provenance, quote}` where `quote` is verbatim chunk `text`. **A grounded answer
  may never carry a citation that wasn't in the retrieved set** — no fabricated
  timestamps, the schema validator rejects it (the analogue of "a success never
  carries a `reason`").
- **`insufficient_evidence`** — retrieval found nothing on-topic above threshold.
  The system says so and cites nothing. It does **not** answer from the model's
  parametric knowledge (that would silently leave the corpus — the whole point is
  grounding).
- **`failed`** — an operational fault (embedder down, index missing/stale,
  provider error), with a reason, mirroring `DESIGN.md`'s operational reasons.

**Query handling — deliberately minimal (§20.6):** the query is embedded as
asked. There is **no HyDE and no keyword-list expansion** — 2026 multi-turn RAG
evaluations found retrieval-oriented rewrites of that kind consistently *hurt*
by distorting intent. The one transformation with evidence behind it — a light
*conversational* rewrite resolving "he" / "that episode" when a question arrives
as a follow-up — is deferred until a conversational mode exists; recorded here so
its absence reads as a decision, not a gap.

**Parent-context expansion (§20.5):** chunks are sized for *retrieval* (§6), not
for reading. At prompt-assembly time each selected chunk is expanded with its ±1
neighbouring chunks from the canonical layer (free — `chunk_id` is ordinal, and
the canonical record is on disk). Expansion widens what the answerer **sees**,
never what is **cited**: a citation still names the retrieved chunk's
`{video_id, start_s}`, and the retrieval trace records both the hit and its
expansion so an audit can tell them apart.

**Prompt assembly:** the top-`k` chunks (expanded as above) are handed to Claude
with their metadata and an instruction to answer only from them, quote spans it
relies on, and attach the corresponding citations; conflicting sources are
surfaced, not silently resolved. Answer text goes to **stdout**, logs/progress to **stderr** (same I/O
convention as `DESIGN.md §12`). `--json` emits the structured answer object
(answer, `answer_outcome`, citations, retrieval trace) for downstream use.

---

## 11. Interfaces

**Library (async canonical + sync wrapper, matching `DESIGN.md §12`):**

```python
# ingest: discover → pull (existing pipeline) → store canonical + markdown
await corpus_add(source_slug, refs, policy=Policy(...))
# derive: (re)build chunks → context → embeddings → indexes from canonical
await corpus_build(source_slug, *, rebuild=False)
# ask
answer = await ask(query, *, source=None, since=None, k=8, filters={...})
answer = ask_sync(query, ...)   # wrapper; raises inside a running loop
```

**CLI (new verbs alongside `pull` / `find` / `doctor`):**

```
transcript corpus add   <slug> (--channel <id> | --file urls.txt | -) [--policy …] [--no-shorts]
                        [--diarize]          # per-episode opt-in: audio→ASR+speaker labels (default off, §18)
transcript corpus build <slug> [--rebuild] [--no-context] [--embedder local|api]
transcript corpus status <slug>                 # counts, versions, staleness (§12)
transcript ask "question" [--source <slug>] [--since DATE] [--channel <id>]
                          [--k 8] [--json]
transcript eval <slug> [--compare baseline.json]   # §13 golden-set metrics + regression gate
```

`corpus add` reuses `find` + `pull` verbatim (no new access path). `corpus build`
is offline and idempotent. `transcript doctor` gains checks for the new subsystem:
embedder model present, reranker present, index readable, index build-versions vs.
current derivation versions (§14).

---

## 12. Incremental ingest & idempotency

- **Only-new by default.** `corpus add` consults the manifest and pulls only
  `video_id`s not already present (ties to `DESIGN.md §18`, "incremental channel
  monitoring"). A scheduled re-run picks up just the new episodes.
- **Content-hash guard.** Re-ingesting an unchanged video is a no-op (the
  `content_hash` matches); a changed transcript supersedes and marks derived layers
  stale for that video only.
- **Version-driven rebuilds.** `corpus build` compares each video's stored
  `chunker_version` / `context_version` / `embed_model` against current values and
  rebuilds **only** the stale ones. A global version bump (§14) rebuilds the index;
  it never touches the canonical layer.
- **Atomic writes + a filesystem lock** (reuses `locking.py`) so two concurrent
  builds can't corrupt an index — the same singleflight/lock posture as
  `DESIGN.md §10`.

---

## 13. Evaluation harness — measure retrieval, don't vibe-check it

Non-optional, and the reason this is "built right." It is the ruler that makes
every later change to chunking, context, embedder, or `k` an objective yes/no —
the same philosophy as the `jiwer` ASR regression gate (`DESIGN.md §17`) and the
eval-harness discussion that motivated this project.

- **Golden set.** 20–40 hand-authored questions over the corpus, each labelled with
  the `video_id`(s) + rough timestamp(s) that actually answer it. Start at 20.
  Include a few **time-sensitive** questions ("what is their *current* view on X")
  whose right answer moved across the corpus — the cheap way to learn whether
  recency-weighted fusion is ever needed before building it (§20.6).
- **Retrieval metrics** (cheap, deterministic, no LLM): **recall@k** and **MRR** —
  did the answering chunk make the top-`k`, and how high? This is the primary gate;
  a generated answer can only be as good as what retrieval surfaced.
- **Answer faithfulness** (LLM-as-judge): given the question, retrieved passages,
  and the answer, score whether the answer is supported and whether its citations
  are correct. Guards hallucinated citations and ungrounded claims.
- **Diff two runs.** Baseline vs. candidate as a per-metric table
  (`transcript eval --compare`), so "context on vs. off" or "chunk 400 vs. 600" is a
  measured decision. Regressions past a threshold fail the build.

---

## 14. Versioning & rebuild triggers

Every derivation stage is versioned; the version tuple is the index's cache key
(the `policy_hash` analogue from `DESIGN.md §10`). `index/<slug>/build.json`
records the tuple the index was built at:

```
CORPUS_SCHEMA_VERSION · CHUNKER_VERSION · CONTEXT_VERSION ·
EMBED_MODEL (name+revision+dim) · RERANKER (name+revision) · INDEX_SCHEMA_VERSION
```

`corpus status` / `doctor` flag any index whose `build.json` trails current values.
Rule, inherited in spirit from `DESIGN.md`: **a version bump rebuilds derived
layers; it never re-pulls the canonical layer.** No silent behavioural change without a version
bump.

---

## 15. Security & compliance (derived-data posture)

- **No new acquisition path, so no new ToS surface.** Ingest is `find` + `pull`
  under the existing `EgressPolicy` gate and compliance posture (`DESIGN.md §4`).
- **Contextual enrichment and API embeddings are egress.** Both send transcript
  text to a model/embedding provider. In `local` they run against the operator's
  own key, are disclosed, and are individually disableable (`--no-context`,
  `--embedder local`). Nothing sends text off-machine without an explicit, visible
  choice.
- **Retention.** Derived layers inherit the ≤30-day YouTube metadata
  refresh/delete rule (`DESIGN.md §4`); canonical transcript text is operator-held
  under the same rights assertion as the pull that produced it.
- **PII.** Transcripts can contain personal data; treat the corpus as sensitive,
  keep it local by default, and keep deletion cascading (removing a canonical
  record removes its derived chunks).
- **Prompt-injection realism.** Retrieved transcript text is untrusted input to the
  answerer; the answer prompt must treat chunk text as data, not instructions.

---

## 16. Deployment profiles

| Concern            | `local`                                  | `server`                                   |
|--------------------|------------------------------------------|--------------------------------------------|
| Vector store       | LanceDB single on-disk table             | LanceDB/shared vector service, replicated  |
| BM25               | LanceDB native FTS, same table (§20.4)   | same store, or a shared search service     |
| Embedding          | local model (default)                    | local model or API, batched at scale       |
| Contextualization  | operator key, opt-in                     | batched, quota-managed                     |
| Build concurrency  | filesystem lock (`locking.py`)           | cross-process lock, scheduled reindex jobs |
| Ingest cadence     | manual / scheduled task                  | scheduled incremental monitors             |

Same core code; only the backends and concurrency differ — identical to
`DESIGN.md §14`.

---

## 17. Tech stack (additions only)

- **Chunking/tokens:** `tiktoken` (or the embedder's own tokenizer) for budgeting.
- **Vector store:** `lancedb`.
- **Lexical:** LanceDB's native FTS (BM25) on the same table — the default
  (§20.4); a standalone BM25 library (`bm25s`) only as the recorded fallback.
- **Embeddings:** `sentence-transformers` (local default) behind an interface; an
  API embedder as an optional extra.
- **Reranker:** a cross-encoder (`BGE-reranker` class) locally, or a hosted
  reranker as an optional extra.
- **Answerer:** the Claude API (existing dependency posture).
- **Eval:** reuse `pytest`; metrics are plain Python.

Packaged as new optional-dependency extras, mirroring `pyproject.toml`
(`corpus`, `retrieval`, `rerank`), so a captions-only user installs none of it.

---

## 18. Build phases

Sequenced so the **expensive, fragile part (canonical acquisition) is banked
first** and every later phase is offline and rebuildable. Estimates assume the
existing pipeline as the starting point.

| Phase | Deliverable | Rough |
|---|---|---|
| **R0** | Canonical corpus store: `CorpusRecord` schema, `raw/` + `markdown/` layers, `manifest.json`, `corpus add` (reusing `find`+`pull`), incremental only-new ingest. **Bank the transcripts.** | ~1 day |
| **R1** | Deterministic, versioned chunker (chapter/utterance-aware, overlap, `ChunkMeta` with timestamps) + fixtures, as normalization has fixtures. | ~1 day |
| **R2** | Embeddings (local default, pinned) + LanceDB dense index + `corpus build` (offline, idempotent, version-aware). Dense-only retrieval works end to end. | ~1–1.5 days |
| **R3** | Hybrid retrieval: LanceDB native FTS (BM25) + RRF fusion + cross-encoder rerank + metadata filters. | ~1 day |
| **R4** | Contextual enrichment (prompt-cached, versioned, gated) — measured on/off via R6. | ~1 day |
| **R5** | `transcript ask`: grounded answering, discriminated `answer_outcome`, ±1-chunk parent-context expansion, citations with deep links, `--json`. | ~1 day |
| **R6** | Evaluation harness: golden set, recall@k / MRR, faithfulness judge, `eval --compare` regression gate. | ~1–1.5 days |
| **R7** | On-demand diarization: `corpus add --diarize` routes a chosen episode through audio→Whisper+speaker-segmentation, populates `ChunkMeta.speaker`, enables speaker-filtered retrieval. **Default off; per-episode; mixed corpus.** Retrieval layer unchanged. | ~1–2 days |

A usable personal tool = **R0–R6**. R0 alone is the batch pull we deferred, done
in a way the rest builds on. R7 is the on-demand "decipher a single podcast"
capability — built so speaker attribution is one flag on a specific episode, never
an automatic cost on every pull.

---

## 19. Acceptance criteria

- **Canonical is reproducible-independent:** the entire `index/` can be deleted and
  rebuilt from `raw/` with **no network access**; a rebuilt index is byte-stable
  given identical versions.
- **Timestamps survive to the chunk:** every citation resolves to a working
  `…&t=<s>s` deep link; no chunk lacks `{video_id, start_s, end_s}`.
- **Grounded-answer invariant holds:** a `grounded` answer carries only citations
  drawn from the retrieved set; the validator rejects fabricated citations;
  `insufficient_evidence` cites nothing and does not answer from parametric memory.
- **Expansion never widens citations:** parent-context expansion (§10) may enlarge
  what the answerer reads, but every citation resolves to a chunk that was actually
  retrieved — a citation naming an expansion-only neighbour fails validation.
- **Incrementality:** re-running `corpus add` on an unchanged channel pulls zero
  videos; a version bump rebuilds derived layers and re-pulls nothing.
- **Retrieval gate:** recall@k on the golden set meets threshold; the
  `eval --compare` table fails the build on regression.
- **Provenance preserved end to end:** an answer sourced from a `platform_auto`
  caption is distinguishable from one sourced from `local_asr`.
- **Privacy default:** with `--embedder local --no-context`, no transcript text
  leaves the machine.

---

## 20. Decisions (resolved)

1. **Speaker attribution — RESOLVED: on-demand, not automatic.** Captions are the
   default path (R0–R6). Diarization is built as a per-episode opt-in
   (`corpus add --diarize`, R7) — default off, invoked only when a specific podcast
   needs "who said what," yielding a mixed corpus queried by the same retrieval
   layer.
2. **Contextual enrichment — RESOLVED: on by default.** Best precision for this
   pronoun-heavy, multi-speaker corpus. Cost controlled via prompt-caching the
   episode body; remains switchable (`--no-context`) and measured via R6.
3. **Embeddings — RESOLVED: local model by default.** Free, private, offline. The
   rebuildable index keeps an API-embedder upgrade a reversible config swap; revisit
   only if R6 shows recall is embedder-bound.
4. **Hybrid index — RESOLVED: one LanceDB store** (2026-08-12 review). Dense
   vectors and the BM25 full-text index live in the same LanceDB table (native FTS
   + built-in RRF hybrid query), so the two sides cannot drift and `index/<slug>/`
   holds a single artifact. A standalone BM25 library (`bm25s`) is the fallback
   only if FTS limits show up under R6 measurement.
5. **Parent-context expansion — RESOLVED: on, at answer time** (2026-08-12
   review). Retrieval stays chunk-sized; the answerer sees each hit expanded ±1
   neighbouring chunk from the canonical layer. Expansion never changes what is
   cited (§10, §19).
6. **Query transformation — RESOLVED: none in v1** (2026-08-12 review). No HyDE
   and no keyword-list expansion — published 2026 multi-turn RAG evaluations found
   they distort intent and hurt retrieval. A light conversational rewrite is
   deferred until a conversational mode exists. Recency weighting is not built;
   the golden set carries time-sensitive questions (§13) so R6 shows whether it is
   ever needed.
