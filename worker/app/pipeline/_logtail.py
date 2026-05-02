"""Helper for surfacing the tail of a subprocess log file into the
runner's exception message, so it lands in the job's `error` column
and renders inline on the native JobDetailActivity instead of
requiring a `docker exec` into the worker container to read.

We cap the tail aggressively (a few KB / a couple dozen lines) so a
runaway log doesn't blow up the SQLite row.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

DEFAULT_MAX_BYTES = 4096
DEFAULT_MAX_LINES = 60


def tail_text(
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """Return at most max_lines lines from the END of `text`,
    truncated further to max_bytes total."""
    if not text:
        return ""
    lines = text.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_bytes:
        tail = tail[-max_bytes:]
    return tail


def tail_bytes(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """Same as tail_text but operates on raw bytes (utf-8, replace)."""
    if not data:
        return ""
    return tail_text(
        data.decode("utf-8", errors="replace"),
        max_bytes=max_bytes,
        max_lines=max_lines,
    )


def tail_file(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """Return the last max_lines / max_bytes of a log file. Empty
    string on any read error — we never want a tail probe to mask
    the original subprocess error."""
    try:
        size = path.stat().st_size
        # Read ~16 KB from the tail so we have enough material to
        # honour max_lines even with long lines.
        seek_back = max(max_bytes * 4, 16_384)
        with path.open("rb") as f:
            if size > seek_back:
                f.seek(-seek_back, 2)
            buf = f.read()
        return tail_bytes(buf, max_bytes=max_bytes, max_lines=max_lines)
    except Exception:
        return ""


def format_subprocess_error(
    name: str,
    rc: int,
    log_ref: Path | str | None,
    tail: str,
) -> str:
    """Build the canonical "<binary> exited N, see <log>" message
    plus a tail block, ready to raise as RuntimeError(...).
    """
    parts: list[str] = [f"{name} exited {rc}"]
    if log_ref is not None:
        parts.append(f"see {log_ref}")
    msg = ", ".join(parts)
    if tail:
        # Trim trailing newlines so the join below doesn't produce
        # multiple blanks.
        msg += f"\n\n--- tail ---\n{tail.rstrip()}\n"
    return msg


__all__: Iterable[str] = (
    "tail_text",
    "tail_bytes",
    "tail_file",
    "format_subprocess_error",
)
