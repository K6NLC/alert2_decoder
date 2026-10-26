# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Preamble-correlation gate on framesync candidates.

At --max-fs-hd 6 the framesync search returns hundreds of candidates and
almost none are real: on a 123 s QTH capture, 356 candidates yielded 55 real
bursts and 301 spurious. Profiling put Viterbi+RS at 53% of CPU and
quantize_soft at 23%, nearly all of it spent on candidates that never
decode. Framesync HD cannot separate them (real packets occur at raw HD
7-11), but the 80-bit preamble can.

MEASURED, four captures, 70 real bursts / 924 spurious:
    real     min 0.610   (one decoding at MER -1.23 dB)
    spurious max 0.559
    gate 0.40 -> 2.2-2.5x less CPU, packet sets byte-identical

This gate can only REMOVE decode attempts. Unlike a recovery rung it is not
superset-safe by construction, so these tests pin that it is off by default,
fails open on anything unscoreable, and never rejects a real burst.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.candidate_gate import (preamble_correlation, admit,
                                   PREAMBLE_BITS, DEFAULT_THRESHOLD, _reference)


def _clean_preamble(fec_mode=0, invert=False, scale=1.0, tail=40):
    ref = _reference(fec_mode)
    body = (-ref if invert else ref) * scale
    return list(np.concatenate([body, np.zeros(tail)]))


def test_perfect_preamble_scores_one():
    soft = _clean_preamble()
    c = preamble_correlation(soft, PREAMBLE_BITS, 0)
    assert c == pytest.approx(1.0, abs=1e-9)


def test_correlation_is_amplitude_invariant():
    """Capture gain must not move the gate."""
    a = preamble_correlation(_clean_preamble(scale=0.01), PREAMBLE_BITS, 0)
    b = preamble_correlation(_clean_preamble(scale=100.0), PREAMBLE_BITS, 0)
    assert a == pytest.approx(b, abs=1e-9)


def test_noise_scores_far_below_threshold():
    rng = np.random.default_rng(0)
    scores = []
    for _ in range(200):
        soft = list(rng.normal(0, 1.0, PREAMBLE_BITS + 20))
        c = preamble_correlation(soft, PREAMBLE_BITS, 0)
        if c is not None:
            scores.append(abs(c))
    assert max(scores) < DEFAULT_THRESHOLD, (
        f"pure noise reached {max(scores):.3f}, at or above the gate")


def test_inverted_polarity_needs_the_flag():
    soft = _clean_preamble(invert=True)
    assert preamble_correlation(soft, PREAMBLE_BITS, 0) == pytest.approx(-1.0,
                                                                        abs=1e-9)
    assert preamble_correlation(soft, PREAMBLE_BITS, 0,
                                inverted=True) == pytest.approx(1.0, abs=1e-9)


def test_gate_is_off_by_default():
    from alert2.decoder import Alert2Decoder
    assert Alert2Decoder().candidate_gate == 0.0


def test_zero_threshold_admits_everything():
    rng = np.random.default_rng(1)
    soft = list(rng.normal(0, 1, PREAMBLE_BITS + 10))
    assert admit(soft, PREAMBLE_BITS, 0, 0.0) is True
    assert admit(soft, PREAMBLE_BITS, 0, -1.0) is True


def test_unscoreable_candidates_are_admitted_not_rejected():
    """Fail OPEN: a missing measurement must never cost a packet."""
    soft = _clean_preamble()
    assert admit(soft, 5, 0, DEFAULT_THRESHOLD) is True          # start < 0
    assert admit(soft, 10_000, 0, DEFAULT_THRESHOLD) is True     # past end
    assert admit(None, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD) is True
    assert admit([0.0] * (PREAMBLE_BITS + 5), PREAMBLE_BITS, 0,
                 DEFAULT_THRESHOLD) is True                      # all zeros


def test_threshold_sits_below_every_observed_real_burst():
    """0.610 was the weakest real burst across four captures."""
    assert DEFAULT_THRESHOLD < 0.610
    assert DEFAULT_THRESHOLD > 0.30, "too low to reject much"


def test_gate_admits_a_degraded_but_real_preamble():
    """A real burst with heavy noise must still clear the gate."""
    rng = np.random.default_rng(2)
    ref = _reference(0)
    for sigma in (0.3, 0.5, 0.7):
        soft = list(ref + rng.normal(0, sigma, PREAMBLE_BITS)) + [0.0] * 20
        c = preamble_correlation(soft, PREAMBLE_BITS, 0)
        assert c > DEFAULT_THRESHOLD, (
            f"sigma={sigma} scored {c:.3f}, below the gate — a real burst "
            f"would be dropped")


# ---------------------------------------------------------------------------
# Polarity independence
#
# rc31 shipped a SIGNED gate. The multiphase pass pre-inverts its soft
# samples AND passes soft_inverted=True, so decode_bits inverts a second
# time and `src` arrives back at the ORIGINAL polarity while `bits` are
# inverted. That cancels harmlessly for the Viterbi — it only ever compares
# quantised soft against those same bits — but the gate was the first code
# to read src's absolute polarity, and it scored every real multiphase
# candidate as a large NEGATIVE correlation and rejected it.
#
# Measured effect: all mf-phase* wins vanished (19 of 20 packets fell back
# to the burst path) and the marginal MER floor fell 6.24 -> 5.56 dB, while
# the unit tests all passed because they only ever exercised one polarity.
# ---------------------------------------------------------------------------

def test_gate_admits_either_polarity():
    """A real burst must pass regardless of which way the sign fell."""
    normal = _clean_preamble(invert=False)
    flipped = _clean_preamble(invert=True)
    assert admit(normal, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD) is True
    assert admit(flipped, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD) is True, (
        "an inverted real burst was rejected — this is the rc31 bug that "
        "silently disabled multiphase")


def test_gate_uses_magnitude_not_sign():
    """|corr| keeps the discrimination without the polarity fragility."""
    flipped = _clean_preamble(invert=True)
    c = preamble_correlation(flipped, PREAMBLE_BITS, 0)
    assert c < -0.9, "expected a strongly negative signed correlation"
    assert admit(flipped, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD) is True


def test_noise_still_rejected_under_magnitude():
    """Taking |corr| must not let noise through the gate."""
    rng = np.random.default_rng(7)
    worst = 0.0
    for _ in range(300):
        soft = list(rng.normal(0, 1.0, PREAMBLE_BITS + 20))
        c = preamble_correlation(soft, PREAMBLE_BITS, 0)
        if c is not None:
            worst = max(worst, abs(c))
    assert worst < DEFAULT_THRESHOLD, (
        f"pure noise reached |corr|={worst:.3f}, at or above the gate")


def test_admit_is_symmetric_in_the_inverted_flag():
    """Callers disagree about who inverts; the gate must not care."""
    for invert in (False, True):
        soft = _clean_preamble(invert=invert)
        a = admit(soft, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD, inverted=False)
        b = admit(soft, PREAMBLE_BITS, 0, DEFAULT_THRESHOLD, inverted=True)
        assert a is b is True
