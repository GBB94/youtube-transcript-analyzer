# YouTube Transcript Analyzer

Video is a rich, under-mined source of signal -- competitor webinars, product walkthroughs, founder talks, conference sessions, prospect content. The intelligence is in the audio but locked in a format you can't search, score, or pipe anywhere.

Pulling transcripts reliably is the hard part: captions get disabled, auto-captions don't exist, and platforms actively block automated access. Any single method has a failure mode that takes the whole tool down.

## Approach

Staged, policy-driven escalation:

1. **Cheap caption strategies first** -- official captions, auto-generated captions
2. **Audio-based ASR as the floor** -- when captions are unavailable or unreliable

Each stage has clear success/failure criteria before escalating to the next.

## Status

Project scaffolding. Brief and architecture coming soon.
