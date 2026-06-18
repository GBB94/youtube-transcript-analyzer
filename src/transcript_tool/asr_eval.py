"""ASR regression harness (Phase 4).

A change-detection guard, NOT a research-grade quality benchmark. You don't own
Whisper's accuracy, but you do own model size, compute type, VAD, decoding, and
upgrade decisions — this catches regressions when those change.

Usage (Phase 4 test/CI): 5-10 licensed clips per *supported* language, with
checked-in reference transcripts, generous thresholds. English-only if v1 is
English-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import jiwer

# Normalize before scoring so casing/punctuation/whitespace don't dominate WER.
_TRANSFORM = jiwer.Compose([
    jiwer.ToLowerCase(),
    jiwer.RemovePunctuation(),
    jiwer.RemoveMultipleSpaces(),
    jiwer.Strip(),
    jiwer.ReduceToListOfListOfWords(),
])


def wer(reference: str, hypothesis: str) -> float:
    return jiwer.wer(reference, hypothesis,
                     reference_transform=_TRANSFORM, hypothesis_transform=_TRANSFORM)


def cer(reference: str, hypothesis: str) -> float:
    return jiwer.cer(reference, hypothesis)


@dataclass
class Clip:
    path: str
    language: str
    reference: str


@dataclass
class ClipScore:
    path: str
    language: str
    wer: float
    cer: float
    passed: bool


@dataclass
class RegressionReport:
    scores: list[ClipScore]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.scores)

    @property
    def mean_wer(self) -> float:
        return sum(s.wer for s in self.scores) / len(self.scores) if self.scores else 0.0


def run_regression(clips: list[Clip], transcribe_text: Callable[[str, str], str],
                   max_wer: float = 0.35, max_cer: float = 0.25) -> RegressionReport:
    """transcribe_text(path, language) -> hypothesis string. Generous defaults; the
    point is to fire on a regression, not to certify absolute quality."""
    scores: list[ClipScore] = []
    for clip in clips:
        hyp = transcribe_text(clip.path, clip.language)
        w, c = wer(clip.reference, hyp), cer(clip.reference, hyp)
        scores.append(ClipScore(clip.path, clip.language, round(w, 4), round(c, 4),
                                passed=(w <= max_wer and c <= max_cer)))
    return RegressionReport(scores=scores)
