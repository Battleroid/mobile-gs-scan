"""Regression tests for the SfM subprocess streaming fix.

The original ``_glomap_step`` used ``subprocess.run(capture_output=
True)``, which holds every stdout byte in memory until the child
exits. That's fine for the seconds-long feature-extractor step but
disastrous for ``glomap mapper`` / ``colmap automatic_reconstructor``
— multi-minute runs where the user sees an empty ``glomap.log`` and a
frozen JobLogPanel for the whole duration even though the binary is
alive and making progress.

These tests pin down two invariants the streaming Popen-based
rewrite must preserve:

* Lines reach ``log_path`` while the subprocess is still running
  (not all-at-once at exit), so the UI's per-job log poller sees
  forward motion. The test runs a Python one-liner that prints,
  sleeps, prints again, and checks the log file at the midpoint.
* Non-zero exit raises ``RuntimeError`` with the canonical
  ``format_subprocess_error`` shape, and the tail block in the
  message comes from the log file (not from in-memory stdout, which
  no longer exists post-streaming).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.pipeline.sfm import _glomap_step


def test_glomap_step_streams_lines_to_log_during_run(tmp_path: Path):
    """Lines must land in ``log_path`` *during* the subprocess run,
    not just after it exits. Regression for the buffered-stdout
    bug where the JobLogPanel saw nothing for 30+ minutes during
    ``glomap mapper``."""
    log_path = tmp_path / "glomap.log"
    # The one-liner prints "line1", sleeps 1s, prints "line2".
    # ``flush=True`` matches what the colmap / glomap binaries do
    # (their LOG() macros flush after every line); without that
    # flush the streaming would have nothing to forward and the
    # test would be vacuously green.
    cmd = [
        "python3", "-c",
        "import time, sys; "
        "print('line1', flush=True); "
        "time.sleep(1.0); "
        "print('line2', flush=True)",
    ]

    done = threading.Event()

    def _runner():
        try:
            _glomap_step(cmd=cmd, log_path=log_path, step_name="streamer")
        finally:
            done.set()

    t = threading.Thread(target=_runner)
    t.start()
    try:
        # Poll: by ~500 ms the child has emitted "line1" but is
        # still sleeping. With buffered capture the log would only
        # contain the header ``=== streamer ===`` until t=1s; with
        # streaming the log already contains "line1".
        deadline = time.monotonic() + 0.8
        observed_line1_early = False
        while time.monotonic() < deadline:
            if log_path.exists() and "line1" in log_path.read_text():
                observed_line1_early = True
                break
            time.sleep(0.05)
        assert observed_line1_early, (
            "line1 must appear in the log before the subprocess "
            "exits — buffered-stdout regression. Log content at "
            f"check: {log_path.read_text() if log_path.exists() else '<missing>'!r}"
        )
    finally:
        done.wait(timeout=5.0)
        t.join(timeout=5.0)

    # Final state: both lines should be present, in order, under
    # the step header.
    content = log_path.read_text()
    assert "=== streamer ===" in content
    assert content.index("line1") < content.index("line2"), (
        f"lines arrived out of order in {content!r}"
    )


def test_glomap_step_raises_with_log_tail_on_nonzero_exit(tmp_path: Path):
    """Non-zero exit raises with the canonical ``<name> exited N,
    see <log>\\n--- tail ---\\n...`` message. The tail must come
    from the log file (the streaming rewrite drops in-memory
    stdout entirely; if the error path still reached for it the
    message would be empty)."""
    log_path = tmp_path / "fail.log"
    cmd = [
        "python3", "-c",
        "import sys; print('boom output', flush=True); sys.exit(7)",
    ]
    with pytest.raises(RuntimeError) as exc_info:
        _glomap_step(cmd=cmd, log_path=log_path, step_name="boom")

    msg = str(exc_info.value)
    assert "boom exited 7" in msg
    assert str(log_path) in msg
    assert "--- tail ---" in msg
    assert "boom output" in msg, (
        f"tail must include the child's last stdout line; got: {msg!r}"
    )
