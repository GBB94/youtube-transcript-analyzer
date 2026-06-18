"""Cancellable out-of-process execution (UI-0 precondition; aligns with the Phase 7
server-profile note about decoding untrusted media in a supervised child).

`asyncio.wait_for(asyncio.to_thread(fn))` does NOT stop the underlying work on timeout
— the thread keeps running the (CPU-bound, GIL-holding) ASR to completion in the
background. For a batch UI where a per-item timeout or a user "Cancel" must actually
free the box, run the heavy work in a CHILD PROCESS the caller can terminate.

Design:
- `spawn` context (macOS-safe; never `fork`, which is unsafe after threads / with the
  ObjC runtime). The target function and its args must be picklable; the real
  `faster_whisper_transcriber` is module-level and its inputs are plain dataclasses.
- The child loads its own model inside the process — no inherited multi-GB state.
- The result is drained from the queue BEFORE join() to avoid the large-object
  queue-flush deadlock.
- On timeout: terminate → (grace) → kill, so compute genuinely stops.
"""
from __future__ import annotations

import multiprocessing as mp
import queue
from typing import Any, Callable, Sequence


def _worker(q, func: Callable, args: Sequence) -> None:
    try:
        q.put(("ok", func(*args)))
    except BaseException as e:  # noqa: BLE001 — ship a description back, never hang
        try:
            q.put(("err", repr(e)))
        except Exception:
            q.put(("err", "child raised an unpicklable error"))


def run_in_process(func: Callable, args: Sequence = (), timeout: float | None = None,
                   *, grace_seconds: float = 3.0, ctx_method: str = "spawn") -> Any:
    """Run `func(*args)` in a killable child process.

    Returns the function's result. Raises TimeoutError (after terminating/killing the
    child) if it does not finish within `timeout`. Raises RuntimeError if the child
    crashes or returns an error.
    """
    ctx = mp.get_context(ctx_method)
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(q, func, args), daemon=True)
    p.start()
    try:
        # Drain the result first (prevents the child blocking on a large queue put).
        try:
            status, payload = q.get(timeout=timeout)
        except queue.Empty:
            _terminate(p, grace_seconds)
            raise TimeoutError("execution exceeded timeout; child process terminated")
        # Got a result; let the (now-finishing) child exit cleanly.
        p.join(grace_seconds)
        if status == "ok":
            return payload
        raise RuntimeError(f"child process error: {payload}")
    finally:
        if p.is_alive():
            _terminate(p, grace_seconds)


def _terminate(p, grace_seconds: float) -> None:
    if not p.is_alive():
        return
    p.terminate()
    p.join(grace_seconds)
    if p.is_alive():
        p.kill()
        p.join()
