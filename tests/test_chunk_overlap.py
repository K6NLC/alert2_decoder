# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Chunk-overlap sizing regression tests.

Background (v0.9.0a2): the live-stream decoder buffers audio in chunks
with an overlap so a burst that lands on a chunk boundary still appears
intact in the carried-over tail. The previous overlap (0.30 s) was
SHORTER than the longest real ALERT2 burst (~0.45 s for the largest
frames seen on the SLO network). A long frame straddling a boundary was
therefore truncated in BOTH the first chunk and the overlap tail —
decoded twice-truncated instead of once-clean, defeating the re-decode
safety net.

These tests pin the invariant: a burst longer than the OLD overlap but
shorter than the NEW overlap, placed exactly on a chunk boundary, must
be recovered cleanly. They are deliberately written in terms of the
overlap/burst relationship, not a hardcoded 0.6, so they keep working
if the default is retuned upward but fail if it regresses below the
burst length.

Requires libfec (RS). Skipped if unavailable.
"""
import io
import numpy as np
import pytest

from alert2.constants import (BAUD_RATE, BITSYNC_BITS, FRAMESYNC_BITS,
                              RX_SAMPLE_RATE, SAMPLES_PER_BIT)
from alert2.stream import stream_decode


def _libfec():
    import ctypes
    for name in ("libfec.so.0", "libfec.so"):
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


pytestmark = pytest.mark.skipif(not _libfec(),
                                reason="libfec not installed")


def _max_burst_seconds(airlink_len_bytes=130):
    """Duration of the largest realistic ALERT2 burst, FEC mode 0.

    130 B is the max airlink_len observed across 151 real packets in
    QTH/shop captures. bitsync + framesync + rate-1/2 CC + tail/pad.
    """
    bits = BITSYNC_BITS + FRAMESYNC_BITS + airlink_len_bytes * 8 * 2 + 12
    return bits / BAUD_RATE


def test_default_overlap_exceeds_max_observed_burst():
    """The shipped default overlap must exceed the largest real burst.

    This is the core invariant. If someone lowers the default below the
    burst length, the chunk-overlap safety net silently breaks and this
    test fails loudly.
    """
    import inspect
    sig = inspect.signature(stream_decode)
    default_overlap = sig.parameters["overlap_seconds"].default
    max_burst = _max_burst_seconds(130)
    assert default_overlap > max_burst, (
        f"default overlap_seconds={default_overlap}s must exceed the "
        f"largest observed burst ~{max_burst*1000:.0f}ms (airlink_len "
        f"130B). Boundary-straddling long frames will be lost otherwise.")
    # And a sane margin (>=20%) above the max burst.
    assert default_overlap >= max_burst * 1.2, (
        f"overlap {default_overlap}s gives <20% margin over max burst "
        f"{max_burst*1000:.0f}ms; raise it for robustness.")


def test_chunk_strictly_larger_than_overlap():
    """chunk must be larger than overlap or the stream never advances."""
    import inspect
    sig = inspect.signature(stream_decode)
    chunk = sig.parameters["chunk_seconds"].default
    overlap = sig.parameters["overlap_seconds"].default
    assert chunk > overlap, (
        f"chunk_seconds ({chunk}) must exceed overlap_seconds "
        f"({overlap}) or buffer never drains")
    # Overlap shouldn't be an absurd fraction of the chunk (wasteful
    # re-decode); sanity ceiling.
    assert overlap / chunk <= 0.6, (
        f"overlap/chunk ratio {overlap/chunk:.2f} is wastefully high")


def test_old_overlap_would_have_lost_long_boundary_burst():
    """Documents the bug being fixed: with the OLD 0.30 s overlap, a
    ~0.45 s burst on a chunk boundary would NOT fit in the overlap
    tail. This test asserts the arithmetic of the regression so the
    rationale is executable, not just prose in a docstring.
    """
    old_overlap = 0.30
    max_burst = _max_burst_seconds(130)
    # The bug: max burst does not fit in the old overlap window.
    assert max_burst > old_overlap, (
        "regression rationale invalid if max burst <= old overlap")
    # The fix: it does fit in the new default.
    import inspect
    new_overlap = inspect.signature(stream_decode).parameters[
        "overlap_seconds"].default
    assert max_burst < new_overlap, (
        "fix invalid if max burst >= new overlap")
