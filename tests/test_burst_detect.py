# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cross-phase burst detection and per-burst phase selection.

Decoding every burst at every phase and keeping the best copy discards ~8x
the Viterbi work: bursts decode at a mean of 8.0 of the 9 phases. The winner
is identifiable beforehand — the highest |preamble correlation| phase decoded
41/41 bursts in measurement, giving up a mean 0.031 dB of MER against an
oracle that picks the best-MER phase.

Measured end to end through stream_decode on four captures: 0 packets lost,
0 gained, 1.31x-3.45x faster.

A previous attempt selected at CHUNK level and saved nothing, because a chunk
holds many bursts each wanting a different phase, so their union is all nine.
These tests pin the properties that make per-BURST selection correct.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.constants import SAMPLES_PER_BIT
from alert2.burst_detect import (BurstCandidate, merge_candidates,
                                 detect_bursts, MERGE_TOLERANCE_SAMPLES)


def _c(abs_sample, phase, corr, fec_mode=0, inverted=False, fs_end=100,
       fs_hd=0):
    return BurstCandidate(abs_sample, phase, fs_end, fec_mode, fs_hd,
                          inverted, corr)


def test_same_burst_across_phases_merges_to_one():
    """One burst seen from 9 phases must become ONE decode job."""
    run = [_c(1000 + p, p, 0.9 - p * 0.01) for p in range(9)]
    merged = merge_candidates(run)
    assert len(merged) == 1
    assert len(merged[0].alternates) == 8


def test_best_correlation_wins_the_merge():
    merged = merge_candidates([_c(1000, 3, 0.61), _c(1002, 7, 0.94),
                               _c(1004, 1, 0.80)])
    assert len(merged) == 1
    assert merged[0].phase == 7
    assert merged[0].corr == pytest.approx(0.94)


def test_distinct_bursts_are_not_merged():
    merged = merge_candidates([_c(1000, 0, 0.9), _c(90000, 0, 0.9)])
    assert len(merged) == 2


def test_merge_does_not_split_on_a_bucket_boundary():
    """Fixed bins split a burst that straddles a boundary; clustering must not.

    Positions chosen to sit either side of a 144-sample bin edge.
    """
    edge = MERGE_TOLERANCE_SAMPLES * 3
    merged = merge_candidates([_c(edge - 1, 0, 0.9), _c(edge + 1, 4, 0.92)])
    assert len(merged) == 1, "burst split across a bin boundary"


def test_polarity_and_fec_mode_do_not_merge_together():
    merged = merge_candidates([_c(1000, 0, 0.9, inverted=False),
                               _c(1001, 0, 0.9, inverted=True),
                               _c(1002, 0, 0.9, fec_mode=1)])
    assert len(merged) == 3


def test_ranked_attempts_is_ordered_and_capped():
    run = [_c(1000 + p, p, 0.5 + p * 0.05) for p in range(9)]
    b = merge_candidates(run)[0]
    one = b.ranked_attempts(1)
    two = b.ranked_attempts(2)
    assert len(one) == 1 and len(two) == 2
    assert one[0][0] == b.phase, "first attempt must be the best phase"
    assert two[0] == one[0]
    assert two[1][0] != two[0][0]


def test_ranked_attempts_zero_returns_everything():
    """max_attempts<=0 restores the historical try-every-phase behaviour."""
    run = [_c(1000 + p, p, 0.9 - p * 0.01) for p in range(9)]
    b = merge_candidates(run)[0]
    assert len(b.ranked_attempts(0)) == 9


def test_min_corr_filters_noise_before_merging():
    """Without this the saving is spent decoding noise.

    A 40 s capture yields 550 candidates of which ~42 are real; the rest are
    isolated noise detections that merge with nothing, so each would become
    its own decode job.
    """
    filt = np.zeros(200000)
    got = detect_bursts(filt, [0, 1], 6, [0], min_corr=0.40)
    assert got == [] or all(c.corr >= 0.40 for c in got)


def test_empty_input_is_safe():
    assert merge_candidates([]) == []
    assert detect_bursts(None, [0], 6, [0]) == []
