# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bit-exactness of the vectorized soft Viterbi (rc24).

decode_soft() was rewritten from a triple-nested Python loop to numpy
array operations per trellis step. It is the same algorithm with the
same tie-breaking, so it must produce byte-identical output — these
tests assert that against the retained reference implementation
(decode_soft_ref) rather than trusting inspection.
"""
import random

import pytest

from alert2.fec import SoftViterbi, SOFT_IDEAL, quantize_soft


@pytest.mark.parametrize("fec_mode", [0])
def test_matches_reference_on_random_inputs(fec_mode):
    v = SoftViterbi(fec_mode)
    rng = random.Random(1234)
    for _ in range(120):
        n = rng.randint(v.tail + 2, 140)
        vals = [rng.randint(-2 * SOFT_IDEAL, 2 * SOFT_IDEAL)
                for _ in range(2 * n)]
        for expect_tail in (True, False):
            got_b, got_m = v.decode_soft(vals, expect_tail=expect_tail)
            ref_b, ref_m = v.decode_soft_ref(vals, expect_tail=expect_tail)
            assert got_b == ref_b, "decoded bits differ from reference"
            assert got_m == ref_m, "path metric differs from reference"


def test_matches_reference_on_saturated_and_zero_inputs():
    """Edge cases: all-confident, all-uncertain, and alternating."""
    v = SoftViterbi(0)
    n = 40
    cases = [
        [SOFT_IDEAL] * (2 * n),
        [-SOFT_IDEAL] * (2 * n),
        [0] * (2 * n),
        [SOFT_IDEAL if i % 2 else -SOFT_IDEAL for i in range(2 * n)],
        [2 * SOFT_IDEAL if i % 3 else -2 * SOFT_IDEAL
         for i in range(2 * n)],
    ]
    for vals in cases:
        for expect_tail in (True, False):
            assert (v.decode_soft(vals, expect_tail=expect_tail)
                    == v.decode_soft_ref(vals, expect_tail=expect_tail))


def test_handles_degenerate_lengths():
    v = SoftViterbi(0)
    assert v.decode_soft([]) == ([], 0)
    # length equal to the tail: reference and vectorized must agree
    n = v.tail
    vals = [SOFT_IDEAL] * (2 * n)
    assert (v.decode_soft(vals, expect_tail=False)
            == v.decode_soft_ref(vals, expect_tail=False))


def test_quantized_real_shaped_input_matches():
    """Values shaped like quantize_soft output, not just raw ints."""
    v = SoftViterbi(0)
    rng = random.Random(99)
    soft = [rng.gauss(0, 1.0) for _ in range(2 * 200)]
    vals = quantize_soft(soft, 1.0)
    for expect_tail in (True, False):
        assert (v.decode_soft(vals, expect_tail=expect_tail)
                == v.decode_soft_ref(vals, expect_tail=expect_tail))
