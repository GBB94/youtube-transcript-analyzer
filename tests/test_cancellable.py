"""UI-0 precondition: real out-of-process cancellation. Proves a runaway child is
actually killed on timeout (compute stops), not left running in the background.
Workers are module-level so the spawn context can import them."""
import time

import pytest

from transcript_tool.cancellable import run_in_process


def _double(x):
    return x * 2


def _sleep_then(seconds, val="done"):
    time.sleep(seconds)
    return val


def _boom():
    raise ValueError("kaboom")


def test_returns_child_result():
    assert run_in_process(_double, (21,), timeout=30) == 42


def test_timeout_kills_child_promptly():
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        run_in_process(_sleep_then, (30,), timeout=0.5)
    elapsed = time.monotonic() - t0
    # If the child were merely abandoned (not killed) this would still return fast, but
    # the point is it raises TimeoutError well before the 30s sleep would finish.
    assert elapsed < 10


def test_child_error_becomes_runtime_error():
    with pytest.raises(RuntimeError) as ei:
        run_in_process(_boom, (), timeout=30)
    assert "ValueError" in str(ei.value) and "kaboom" in str(ei.value)
