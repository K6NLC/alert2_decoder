# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Stage 2: all-phase burst candidates.

The phase that decodes a burst best is uniformly random over all nine
(measured 10.3%-12.2% each across 1269 multiphase winners, QTH 2026-08-10)
and has no memory (consecutive winners agree 9.9%, chance 11.1%). So there
is nothing to predict and partial phase coverage is structurally lossy.

Full coverage became affordable once find_framesync was vectorised:
profiling rc29 over 30 s of audio at 3 phases put Viterbi+RS at 53% of CPU,
quantize_soft at 23%, and the framesync scan at 1.3%.

These tests pin the mechanics that make merging candidates across phases
correct — chiefly that positions are compared in ABSOLUTE samples, since a
candidate's fs_end_bit is relative to its own phase.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.constants import SAMPLES_PER_BIT
from alert2.stream import collect_candidates_all_phases


class _FakeDecoder:
    require_bitsync = False
    max_fs_hd = 4
    fec_modes = [0]


def _fake_collect(monkeypatch, per_phase):
    """Stub the single-phase collector with scripted per-phase results."""
    calls = {'n': 0}

    def fake(decoder, bits, soft, amp):
        out = per_phase[calls['n'] % len(per_phase)]
        calls['n'] += 1
        return out
    import alert2.stream as st
    monkeypatch.setattr(st, '_collect_burst_candidates', fake)
    monkeypatch.setattr(st, 'slice_bits', lambda x: [0] * 64)
    return calls


def test_same_burst_seen_from_two_phases_merges_once(monkeypatch):
    """A burst at absolute symbol S must not be decoded once per phase.

    Phase 0 sees it at fe=100; phase 3 sees the same burst at fe=100 too,
    but three samples later in absolute terms — still inside the same
    symbol, so it is one burst.
    """
    per_phase = [
        [(100, 0, False, 44)],   # from phase 0
        [(100, 0, False, 44)],   # from phase 3
    ]
    _fake_collect(monkeypatch, per_phase)
    got = collect_candidates_all_phases(
        _FakeDecoder(), np.zeros(20000), [0, 3])
    assert len(got) == 1, got


def test_distinct_bursts_are_not_merged(monkeypatch):
    per_phase = [
        [(100, 0, False, 44)],
        [(400, 0, False, 44)],
    ]
    _fake_collect(monkeypatch, per_phase)
    got = collect_candidates_all_phases(
        _FakeDecoder(), np.zeros(20000), [0, 1])
    assert len(got) == 2


def test_polarity_and_fec_mode_are_part_of_the_key(monkeypatch):
    per_phase = [
        [(100, 0, False, 44), (100, 0, True, 44)],
        [(100, 1, False, 44)],
    ]
    _fake_collect(monkeypatch, per_phase)
    got = collect_candidates_all_phases(
        _FakeDecoder(), np.zeros(20000), [0, 1])
    assert len(got) == 3, "inverted and fec_mode must not collapse together"


def test_candidate_carries_its_own_phase(monkeypatch):
    _fake_collect(monkeypatch, [[(100, 0, False, 44)]])
    got = collect_candidates_all_phases(
        _FakeDecoder(), np.zeros(20000), [7])
    assert len(got[0]) == 5
    assert got[0][4] == 7, "phase must travel with the candidate"


def test_known_airlink_length_preferred_over_unknown(monkeypatch):
    """A sized extract beats a guessed one when both describe one burst."""
    per_phase = [
        [(100, 0, False, None)],
        [(100, 0, False, 61)],
    ]
    _fake_collect(monkeypatch, per_phase)
    got = collect_candidates_all_phases(
        _FakeDecoder(), np.zeros(20000), [0, 1])
    assert len(got) == 1
    assert got[0][3] == 61


def test_burst_pos_key_is_phase_independent():
    """decode_bursts must not re-decode one burst once per source phase."""
    import inspect
    from alert2 import burst
    src = inspect.getsource(burst.decode_bursts)
    assert 'cand_phase' in src
    i = src.index('pos_key')
    window = src[i:i + 300]
    assert 'SAMPLES_PER_BIT' in window, (
        "pos_key still uses a phase-relative bit index; candidates from "
        "different phases would not merge")


def test_decode_bursts_accepts_both_tuple_shapes():
    """The 4-tuple form must keep working for the single-phase path."""
    import inspect
    from alert2 import burst
    src = inspect.getsource(burst.decode_bursts)
    assert 'len(cand) == 5' in src
    assert 'cand_phase = phase' in src, "4-tuple fallback was dropped"
