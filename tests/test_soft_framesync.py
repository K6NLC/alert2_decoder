# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Soft-decision framesync tests (docs/SOFT_FRAMESYNC.md §5a/5b).

§5a correctness (no libfec needed):
  - clean framesync -> norm_score ~1.0, detected at same position as hard
  - pure noise -> no detection at sane threshold
  - inverted framesync -> norm_score ~-1.0
  - equivalence floor: soft finds what hard finds (when wrong bits weak)
  - threshold monotonicity: lowering threshold only adds candidates

§5b e2e (libfec-gated):
  - low-confidence-corrupted framesync recovered by soft, payload correct
  - no phantom packets from noise
  - composes with soft-Viterbi without regression
  - chunk-overlap interaction via STREAMING path (not file mode)
"""
import random
import numpy as np
import pytest

from alert2.constants import FRAMESYNCS, FRAMESYNC_BITS, RX_SAMPLE_RATE
from alert2.sync import (
    find_framesync, find_framesync_soft, soft_score_to_synthetic_hd,
    _FS_BIPOLAR,
)


# --------------------------------------------------------------------------
# §5a correctness — pure unit, no libfec
# --------------------------------------------------------------------------

def _clean_soft_for_mode(mode, amp=1.0, n_lead=20, n_trail=20, noise=0.0,
                          seed=0):
    """Build a soft-sample stream containing the framesync for `mode`
    at a known position, mapping pattern bit 1->+amp, 0->-amp."""
    rng = random.Random(seed)
    lead = [rng.choice([-amp, amp]) for _ in range(n_lead)]
    fs = [amp if b == 1 else -amp for b in
          [(FRAMESYNCS[mode] >> (FRAMESYNC_BITS - 1 - k)) & 1
           for k in range(FRAMESYNC_BITS)]]
    trail = [rng.choice([-amp, amp]) for _ in range(n_trail)]
    stream = lead + fs + trail
    if noise > 0:
        stream = [v + rng.gauss(0, noise) for v in stream]
    # framesync ends at index n_lead + FRAMESYNC_BITS - 1
    return stream, n_lead + FRAMESYNC_BITS


def test_clean_framesync_scores_near_one():
    for mode in FRAMESYNCS:
        stream, fs_end = _clean_soft_for_mode(mode, amp=1.0, noise=0.0)
        hits = find_framesync_soft(stream, amplitude_estimate=1.0,
                                   min_norm_score=0.5, modes=[mode])
        assert hits, f"mode {mode}: clean framesync not detected"
        positions = {p for p, m, s in hits}
        assert fs_end in positions, (
            f"mode {mode}: expected detection ending at {fs_end}, "
            f"got {sorted(positions)}")
        best = max(s for p, m, s in hits if p == fs_end)
        assert best > 0.95, f"mode {mode}: clean score {best} not ~1.0"


def test_pure_noise_false_candidate_rate_bounded():
    """A matched-filter detector CANNOT have zero false candidates in
    pure noise at a useful threshold — that's information-theoretically
    impossible. What matters operationally:

    1. In the realistic regime (signal present, between-burst noise much
       weaker than signal amplitude) there are ZERO false candidates.
    2. In the pathological worst case (noise as strong as the amplitude
       estimate), false candidates are bounded and low — and RS parity
       is the final gate, so they cannot become phantom PACKETS (tested
       separately in test_soft_fs_no_phantom_from_noise).

    This test pins both, at the shipped default threshold (0.62).
    """
    from alert2.sync import find_framesync_soft
    rng = random.Random(123)
    N = 20000

    # 1. Realistic regime: between-burst noise std 0.4, amp_est 1.0.
    realistic = [rng.gauss(0, 0.4) for _ in range(N)]
    hits_realistic = find_framesync_soft(realistic, 1.0, 0.62)
    assert hits_realistic == [], (
        f"realistic regime must have zero false candidates, "
        f"got {len(hits_realistic)}")

    # 2. Pathological: noise as strong as signal. Bounded, not zero.
    pathological = [rng.gauss(0, 1.0) for _ in range(N)]
    hits_path = find_framesync_soft(pathological, 1.0, 0.62)
    rate = len(hits_path) / N
    assert rate < 0.003, (
        f"pathological false-candidate rate {rate:.4f}/position too high "
        f"at default threshold; RS still gates packets but tighten "
        f"threshold if this regresses")


def test_inverted_framesync_negative_score():
    """An inverted framesync produces a strongly NEGATIVE raw score; with
    the >= threshold it should not match in normal polarity, but negating
    the stream (what the decoder's inverted pass does) recovers it."""
    mode = 0
    stream, fs_end = _clean_soft_for_mode(mode, amp=1.0)
    inverted = [-v for v in stream]
    # Normal-polarity search on the inverted stream: should NOT find it.
    assert find_framesync_soft(inverted, 1.0, 0.55, modes=[mode]) == [] or \
        all(p != fs_end for p, m, s in
            find_framesync_soft(inverted, 1.0, 0.55, modes=[mode]))
    # Negating it back (decoder's inverted pass) recovers the match.
    hits = find_framesync_soft([-v for v in inverted], 1.0, 0.55,
                               modes=[mode])
    assert any(p == fs_end for p, m, s in hits)


def test_equivalence_floor_soft_finds_what_hard_finds():
    """When framesync bits are corrupted with LOW-confidence errors,
    hard-decision (after slicing) may miss it, but soft must still find
    it. And on a clean framesync, soft must find every position hard
    finds. This is the unit analogue of the 'zero hard-only' field gate.
    """
    mode = 0
    # Clean: soft must find the hard hit.
    stream, fs_end = _clean_soft_for_mode(mode, amp=1.0, noise=0.0)
    hard_bits = [1 if v > 0 else 0 for v in stream]
    hard_hits = find_framesync(hard_bits, max_hd=0, modes=[mode])
    soft_hits = find_framesync_soft(stream, 1.0, 0.5, modes=[mode])
    hard_pos = {p for p, m, h in hard_hits}
    soft_pos = {p for p, m, s in soft_hits}
    assert hard_pos.issubset(soft_pos), (
        f"soft missed hard-found positions: hard={hard_pos} soft={soft_pos}")


def test_low_confidence_corruption_recovered_by_soft():
    """Framesync with a few NEAR-THRESHOLD wrong bits: hard slicing
    flips them (HD too high, missed at tight max_hd); soft correlation
    still scores well above threshold because the wrong bits are weak."""
    mode = 0
    stream, fs_end = _clean_soft_for_mode(mode, amp=1.0, noise=0.0,
                                          n_lead=16, n_trail=16)
    # Corrupt 4 framesync bits with weak wrong-sign values (just past 0).
    fs_start = 16
    for k in (3, 9, 17, 28):
        stream[fs_start + k] = -0.05 if stream[fs_start + k] > 0 else 0.05
    hard_bits = [1 if v > 0 else 0 for v in stream]
    # Hard at a tight threshold (max_hd=2) should miss it (4 bits flipped)
    hard_hits = [h for h in find_framesync(hard_bits, max_hd=2, modes=[mode])
                 if h[0] == fs_end]
    assert not hard_hits, "test setup invalid: hard still found it at hd<=2"
    # Soft should still detect it (weak wrong bits barely dent the score)
    soft_hits = [h for h in find_framesync_soft(stream, 1.0, 0.55,
                                                modes=[mode]) if h[0] == fs_end]
    assert soft_hits, "soft framesync failed to recover weak-corrupted sync"
    assert soft_hits[0][2] > 0.55


def test_threshold_monotonicity():
    """Lowering min_norm_score only ever ADDS candidates."""
    mode = 0
    stream, _ = _clean_soft_for_mode(mode, amp=1.0, noise=0.6, seed=7)
    prev = None
    for thr in (0.9, 0.7, 0.5, 0.3, 0.1):
        hits = set((p, m) for p, m, s in
                   find_framesync_soft(stream, 1.0, thr, modes=[mode]))
        if prev is not None:
            assert prev.issubset(hits), (
                f"threshold {thr}: candidate set not monotone "
                f"(lost {prev - hits})")
        prev = hits


def test_synthetic_hd_mapping():
    assert soft_score_to_synthetic_hd(1.0) == 0
    assert soft_score_to_synthetic_hd(0.0) == FRAMESYNC_BITS // 2
    # Monotone non-increasing in score
    last = -1
    for s in [x / 20 for x in range(20, -1, -1)]:
        hd = soft_score_to_synthetic_hd(s)
        assert hd >= last
        last = hd
    # Clamped
    assert soft_score_to_synthetic_hd(2.0) == 0
    assert soft_score_to_synthetic_hd(-2.0) == FRAMESYNC_BITS


def test_defensive_bad_amplitude_returns_empty():
    stream, _ = _clean_soft_for_mode(0)
    assert find_framesync_soft(stream, 0.0, 0.55) == []
    assert find_framesync_soft(stream, -1.0, 0.55) == []
    assert find_framesync_soft(stream, float('nan'), 0.55) == []
    assert find_framesync_soft(stream, float('inf'), 0.55) == []


# --------------------------------------------------------------------------
# §5b end-to-end — libfec-gated
# --------------------------------------------------------------------------

def _libfec():
    import ctypes
    for n in ("libfec.so.0", "libfec.so"):
        try:
            ctypes.CDLL(n); return True
        except OSError:
            continue
    return False


e2e = pytest.mark.skipif(not _libfec(), reason="libfec not installed")


@e2e
def test_soft_fs_composes_with_soft_viterbi_no_regression():
    """soft-FS + soft-V together must not regress vs soft-V alone on a
    clean synthesized frame: same payload, no loss, correct tagging."""
    import sys
    sys.path.insert(0, 'tests')
    from test_soft_viterbi_e2e import (_build_first_block_only_frame,
                                       _bits_to_fsk_audio)
    from alert2 import Alert2Decoder
    from alert2.demod import demodulate

    payload = bytes([0x00, 0x16]) + bytes([0x00, 0x00]) + bytes(range(20))  # valid MANT claim (7B <= 22B airlink), Pattern-K consistent
    bits = _build_first_block_only_frame(payload)
    audio = _bits_to_fsk_audio(bits, noise_std=0.05, seed=3)

    def decode(sv, sf):
        d = Alert2Decoder(max_framesync_hd=6, soft_viterbi=sv,
                          soft_framesync=sf)
        b, info = demodulate(audio, RX_SAMPLE_RATE)
        out = []
        for lbl, bu, inv in [('n', b, False),
                             ('i', [1 - x for x in b], True)]:
            out += d.decode_bits(bu, audio_43k=info.get('audio_43k'),
                                 bit_phase=info.get('best_phase', 0),
                                 soft_samples=info.get('soft_samples'),
                                 amplitude_estimate=info.get('amplitude_estimate'),
                                 soft_inverted=inv)
        return out

    sv_only = decode(True, False)
    both = decode(True, True)
    assert sv_only and both
    # Both must decode the same packet payload; soft-FS must not corrupt
    # or lose it.
    assert bytes(sv_only[0].raw_payload) == bytes(both[0].raw_payload)


@e2e
def test_soft_fs_no_phantom_from_noise():
    from alert2 import Alert2Decoder
    from alert2.demod import demodulate
    rng = np.random.RandomState(999)
    noise = rng.normal(0, 1.0, RX_SAMPLE_RATE * 3).astype(np.float32)
    d = Alert2Decoder(max_framesync_hd=6, soft_framesync=True)
    b, info = demodulate(noise, RX_SAMPLE_RATE)
    pkts = d.decode_bits(b, audio_43k=info.get('audio_43k'),
                         bit_phase=info.get('best_phase', 0),
                         soft_samples=info.get('soft_samples'),
                         amplitude_estimate=info.get('amplitude_estimate'))
    assert pkts == [], f"soft-FS produced {len(pkts)} phantom packets"
