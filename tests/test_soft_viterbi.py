# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Soft-decision Viterbi correctness tests.

Covers design-doc section 6a:
  - Identity at high SNR (soft == hard == original)
  - No regression on clean signals
  - Quantization edge cases

These tests do NOT require libfec — they exercise the CC stage in
isolation (Viterbi/SoftViterbi) plus the pure-Python quantizer and
amplitude estimator. RS-dependent decode_*_soft paths are covered by
the existing ground-truth WAV tests when libfec is present.

The performance (SNR-gain) measurement is intentionally NOT here; that
is the real-capture A/B in section 6c, run in the field, not a unit
test. A synthetic sweep can be added later as a slow test.
"""
import math
import random

import pytest

from alert2.fec import (
    Viterbi, SoftViterbi, quantize_soft, estimate_amplitude,
    conv_encode, bits_to_bytes,
)


# ---------------------------------------------------------------------------
# Quantizer edge cases (design doc 6a.3)
# ---------------------------------------------------------------------------

def test_quantize_all_zero_input():
    """All-zero soft input must not divide by zero; returns all zeros."""
    out = quantize_soft([0.0] * 32, amplitude_estimate=0.0)
    assert out == [0] * 32


def test_quantize_zero_amplitude_returns_zeros():
    """amplitude_estimate <= 0 -> defensive all-zeros (no confidence)."""
    assert quantize_soft([1.0, -2.0, 3.0], 0.0) == [0, 0, 0]
    assert quantize_soft([1.0, -2.0, 3.0], -1.0) == [0, 0, 0]


def test_quantize_nonfinite_amplitude_returns_zeros():
    assert quantize_soft([1.0, 2.0], float('nan')) == [0, 0]
    assert quantize_soft([1.0, 2.0], float('inf')) == [0, 0]


def test_quantize_single_outlier_clips():
    """A huge outlier clips at the rail rather than dominating."""
    # amplitude ~1.0, so SOFT_IDEAL(6) maps a +1.0 sample to +6.
    # A +1000 sample must clip to +7 (4-bit max), not 6000.
    out = quantize_soft([1.0, 1000.0, -1000.0], amplitude_estimate=1.0)
    assert out[0] == 6      # clean sample -> +IDEAL
    assert out[1] == 7      # clip at +hi
    assert out[2] == -8     # clip at -lo


def test_quantize_nonfinite_values_become_zero():
    out = quantize_soft([float('nan'), float('inf'), 1.0],
                        amplitude_estimate=1.0)
    assert out[0] == 0
    assert out[1] == 0
    assert out[2] == 6


def test_quantize_range_bounds():
    """Output always within signed 4-bit range."""
    random.seed(1)
    vals = [random.uniform(-50, 50) for _ in range(1000)]
    out = quantize_soft(vals, amplitude_estimate=2.0)
    assert all(-8 <= q <= 7 for q in out)


def test_estimate_amplitude_median_robust_to_outliers():
    """Median-abs estimate is not skewed by a few large spikes."""
    clean = [1.0, -1.0, 1.0, -1.0] * 50          # |.| == 1.0
    spiked = clean + [500.0, -500.0, 999.0]      # a few big spikes
    est = estimate_amplitude(spiked)
    # Median absolute value should still be ~1.0, not pulled toward 500
    assert abs(est - 1.0) < 0.2


def test_estimate_amplitude_empty():
    assert estimate_amplitude([]) == 0.0
    assert estimate_amplitude([float('nan'), float('inf')]) == 0.0


# ---------------------------------------------------------------------------
# Soft Viterbi identity at high SNR (design doc 6a.1)
# ---------------------------------------------------------------------------

def _make_soft_from_bits(code_bits, ideal=6, noise=0.0, seed=0):
    """Map hard code bits to noisy soft values, then quantize.

    bit 1 -> +ideal-ish, bit 0 -> -ideal-ish, plus optional Gaussian noise.
    Returns quantized soft values (what SoftViterbi.decode_soft expects).
    """
    rng = random.Random(seed)
    floaty = []
    for b in code_bits:
        base = 1.0 if b == 1 else -1.0
        if noise > 0:
            base += rng.gauss(0.0, noise)
        floaty.append(base)
    # amplitude estimate ~1.0 (clean symbol magnitude)
    return quantize_soft(floaty, amplitude_estimate=1.0)


def test_soft_viterbi_identity_clean():
    """At zero noise, soft Viterbi recovers the exact input bits and
    agrees with hard Viterbi."""
    random.seed(42)
    info_bits = [random.randint(0, 1) for _ in range(160)]
    code = conv_encode(info_bits, fec_mode=0, append_tail=True)

    # Hard
    hv = Viterbi(fec_mode=0)
    hard_dec, hard_pm = hv.decode(code, expect_tail=True)
    assert hard_dec == info_bits
    assert hard_pm == 0

    # Soft, no noise
    soft_vals = _make_soft_from_bits(code, noise=0.0)
    sv = SoftViterbi(fec_mode=0)
    soft_dec, soft_pm = sv.decode_soft(soft_vals, expect_tail=True)
    assert soft_dec == info_bits
    assert soft_dec == hard_dec
    # Clean quantized symbols sit exactly at +/-IDEAL, so the winning
    # path metric is exactly 0.
    assert soft_pm == 0


def test_soft_viterbi_recovers_under_light_noise():
    """With light Gaussian noise that doesn't flip many hard bits, soft
    Viterbi still recovers the original payload."""
    random.seed(7)
    info_bits = [random.randint(0, 1) for _ in range(200)]
    code = conv_encode(info_bits, fec_mode=0, append_tail=True)
    soft_vals = _make_soft_from_bits(code, noise=0.25, seed=99)
    sv = SoftViterbi(fec_mode=0)
    soft_dec, _ = sv.decode_soft(soft_vals, expect_tail=True)
    assert soft_dec == info_bits


def test_soft_viterbi_beats_hard_at_threshold():
    """At a noise level high enough to cause hard-decision bit errors,
    soft Viterbi should do at least as well as hard (usually better).

    This is a sanity check, NOT the formal gain measurement (that's the
    field A/B). We assert soft's error count <= hard's across several
    trials, and strictly better on aggregate.
    """
    hard_errors_total = 0
    soft_errors_total = 0
    trials = 12
    for t in range(trials):
        rnd = random.Random(1000 + t)
        info_bits = [rnd.randint(0, 1) for _ in range(160)]
        code = conv_encode(info_bits, fec_mode=0, append_tail=True)

        # Build noisy floats once; derive BOTH hard bits and soft values
        # from the SAME noisy waveform so the comparison is fair.
        noise = 0.62
        floaty = []
        for b in code:
            base = (1.0 if b == 1 else -1.0) + rnd.gauss(0.0, noise)
            floaty.append(base)
        hard_code = [1 if v > 0 else 0 for v in floaty]
        soft_vals = quantize_soft(floaty, amplitude_estimate=1.0)

        hv = Viterbi(fec_mode=0)
        hd, _ = hv.decode(hard_code, expect_tail=True)
        sv = SoftViterbi(fec_mode=0)
        sd, _ = sv.decode_soft(soft_vals, expect_tail=True)

        hard_errors_total += sum(1 for a, b in zip(hd, info_bits) if a != b)
        soft_errors_total += sum(1 for a, b in zip(sd, info_bits) if a != b)

    # Soft must not be worse in aggregate, and should be strictly better
    # at this noise level (the whole point of soft decisions).
    assert soft_errors_total <= hard_errors_total
    assert soft_errors_total < hard_errors_total, (
        f"expected soft < hard; got soft={soft_errors_total} "
        f"hard={hard_errors_total}")


def test_soft_viterbi_no_tail_mode():
    """decode_soft with expect_tail=False picks min-metric end state and
    keeps all decoded bits."""
    random.seed(3)
    info_bits = [random.randint(0, 1) for _ in range(64)]
    # Encode WITHOUT tail
    code = conv_encode(info_bits, fec_mode=0, append_tail=False)
    soft_vals = _make_soft_from_bits(code, noise=0.0)
    sv = SoftViterbi(fec_mode=0)
    dec, _ = sv.decode_soft(soft_vals, expect_tail=False)
    # Without a tail the final few bits may be ambiguous, but the bulk
    # of the sequence must match.
    matching = sum(1 for a, b in zip(dec, info_bits) if a == b)
    assert matching >= len(info_bits) - 6



# ---------------------------------------------------------------------------
# Inversion symmetry: negating soft values must decode the inverted bits
# (this is what soft_inverted=True relies on in decode_bits)
# ---------------------------------------------------------------------------

def test_soft_inversion_symmetry():
    """Negating every soft value is equivalent to bit-inverting the
    code stream. This underpins the soft_inverted polarity path."""
    random.seed(11)
    info_bits = [random.randint(0, 1) for _ in range(120)]
    code = conv_encode(info_bits, fec_mode=0, append_tail=True)

    soft_vals = _make_soft_from_bits(code, noise=0.0)
    neg_soft = [-v for v in soft_vals]

    sv = SoftViterbi(fec_mode=0)
    dec_normal, _ = sv.decode_soft(soft_vals, expect_tail=True)
    dec_negated, _ = sv.decode_soft(neg_soft, expect_tail=True)

    assert dec_normal == info_bits
    # Decoding the negated soft stream is the same as decoding the
    # bit-inverted code stream:
    inv_code = [1 - c for c in code]
    hv = Viterbi(fec_mode=0)
    dec_inv_hard, _ = hv.decode(inv_code, expect_tail=True)
    assert dec_negated == dec_inv_hard
