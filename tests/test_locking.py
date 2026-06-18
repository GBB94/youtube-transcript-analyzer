"""UI-0 precondition: cache lock robustness. flock is released by the kernel on
process death, so a leftover lock file from a crashed holder never wedges the key — a
batch with duplicate links + concurrent requests for the same video is the worst case
the UI scope (§7) calls out."""
import threading
import time

import pytest

from transcript_tool.locking import FileLockBackend, LockTimeout


def test_leftover_lock_file_is_still_acquirable(tmp_path):
    """Simulate a dead process that left its lock file behind: with flock there is no
    live holder, so a fresh acquire succeeds immediately (no stale-lock deadlock)."""
    backend = FileLockBackend(tmp_path)
    (tmp_path / "deadbeef.lock").write_text("")        # orphaned marker, no holder
    t0 = time.monotonic()
    with backend.lock("deadbeef"):
        pass
    assert time.monotonic() - t0 < 1.0                 # acquired promptly, did not hang


def test_concurrent_holder_times_out_instead_of_hanging(tmp_path):
    held = threading.Event()
    release = threading.Event()

    def holder():
        b = FileLockBackend(tmp_path)
        with b.lock("k"):
            held.set()
            release.wait(2.0)

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(2.0)

    # A second acquirer with a short timeout must surface LockTimeout, not block forever.
    impatient = FileLockBackend(tmp_path, timeout=0.3)
    with pytest.raises(LockTimeout):
        with impatient.lock("k"):
            pass

    release.set()
    th.join()
    # Once released, the key is acquirable again.
    with FileLockBackend(tmp_path, timeout=2.0).lock("k"):
        pass


def test_lock_released_after_body(tmp_path):
    backend = FileLockBackend(tmp_path, timeout=1.0)
    with backend.lock("k"):
        pass
    with backend.lock("k"):        # immediately re-acquirable
        pass
