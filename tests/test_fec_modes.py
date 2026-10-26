# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
FEC modes 1 and 2 (k=9 punctured convolutional code, 20 RS parity).

Pins the puncturing/depuncturing helpers, the per-mode on-air lengths
from AirLink spec Fig. 3-8, and a byte-exact generator -> decoder round
trip through the real pipeline for every mode, in both the hard and soft
decode paths and with the iterative/SOVA rungs enabled.
"""
import random

import pytest

np = pytest.importorskip("numpy")

from alert2.constants import FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY
from alert2.fec import (
    conv_encode, cc_bits_for_input, puncture_bits, depuncture_soft,
    first_block_cc_bits, first_block_cc_bits_padded,
    followon_cc_bits, followon_cc_bits_padded,
    SoftViterbi, Viterbi, SOFT_IDEAL,
    decode_first_block, decode_first_block_soft,
    decode_followon_stream, decode_followon_stream_soft,
    scramble_block, rs_encode, bytes_to_bits,
)
from alert2.generator import (
    build_mant_pdu, build_airlink_bits, build_tx_audio, TxTiming,
)


def _libfec_available():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec_available(),
                            reason="libfec not installed")


# ---------------------------------------------------------------------------
# Lengths: AirLink spec Figure 3-8 (bytes after RS + CC, incl. 2-byte tail)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,first_bytes", [(0, 82), (1, 98), (2, 100)])
def test_first_block_on_air_length_matches_spec(mode, first_bytes):
    assert first_block_cc_bits_padded(mode) == first_bytes * 8
    # unpadded = exact punctured count incl. tail
    n_in = (FIRST_BLOCK_DATA[mode] + RS_PARITY[mode]) * 8 \
        + (Viterbi(mode).k - 1)
    assert first_block_cc_bits(mode) == cc_bits_for_input(n_in, mode, False)


@pytest.mark.parametrize("mode,final_max", [(0, 98), (1, 98), (2, 100)])
def test_single_full_followon_block_length_matches_spec(mode, final_max):
    # one full follow-on block that is also the final block (carries tail)
    rs = FOLLOWON_BLOCK_DATA[mode] + RS_PARITY[mode]
    assert followon_cc_bits_padded(rs, mode) == final_max * 8


@pytest.mark.parametrize("mode,final_min", [(0, 36), (1, 34), (2, 30)])
def test_one_byte_final_block_length_matches_spec(mode, final_min):
    """Spec Fig. 3-8 'Final Block' minimum. The table is the nominal
    bytes + ceil(bytes/rate_den) + 2 formula; for a punctured stream the
    exact bit count can fall one byte short of it when the RS byte count
    does not divide the puncture period (mode 1, 21 bytes: 264 bits =
    33 B vs the table's 34 B). The difference is trailing pad after the
    tail, which the decoder never reads, so only the >= bound is pinned
    here. Validated bit-exact on real mode-2 traffic; a real odd-length
    mode-1 frame has not been seen yet."""
    rs = 1 + RS_PARITY[mode]
    n = followon_cc_bits_padded(rs, mode)
    assert n % 8 == 0
    assert final_min * 8 - 8 <= n <= final_min * 8


def test_mode0_helpers_equal_legacy_arithmetic():
    assert first_block_cc_bits(0) == 40 * 8 * 2 + 12
    assert first_block_cc_bits_padded(0) == 40 * 8 * 2 + 16
    assert followon_cc_bits(48, 0) == 48 * 8 * 2 + 12
    assert followon_cc_bits_padded(48, 0) == 48 * 8 * 2 + 16


# ---------------------------------------------------------------------------
# Puncturing
# ---------------------------------------------------------------------------

def test_puncture_mode1_drops_third_of_four():
    pairs = list(range(1, 13))          # 6 steps = 12 code bits
    assert puncture_bits(pairs, 1) == [1, 2, 4, 5, 6, 8, 9, 10, 12]


def test_puncture_mode2_drops_third_and_sixth_of_six():
    pairs = list(range(1, 13))
    assert puncture_bits(pairs, 2) == [1, 2, 4, 5, 7, 8, 10, 11]


@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize("n_steps", [1, 2, 3, 4, 5, 6, 7, 11, 300])
def test_depuncture_inverts_puncture_with_erasures(mode, n_steps):
    pairs = [random.choice((-SOFT_IDEAL, SOFT_IDEAL))
             for _ in range(2 * n_steps)]
    kept = puncture_bits(pairs, mode)
    assert len(kept) == cc_bits_for_input(n_steps, mode, False)
    full = depuncture_soft(kept, mode, n_steps)
    assert len(full) == 2 * n_steps
    for a, b in zip(full, pairs):
        assert a == 0 or a == b
    assert sum(1 for a in full if a != 0) == len(kept)


def test_depuncture_mode0_is_identity():
    v = [1, -2, 3, -4]
    assert depuncture_soft(v, 0) == v


# ---------------------------------------------------------------------------
# Viterbi k=9 on the depunctured stream
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [1, 2])
def test_k9_viterbi_recovers_punctured_stream_clean(mode):
    random.seed(mode)
    data = [random.randint(0, 1) for _ in range(400)]
    cc = conv_encode(data, fec_mode=mode, append_tail=True)
    v = SoftViterbi(mode)
    assert v.k == 9
    soft = depuncture_soft([SOFT_IDEAL if b else -SOFT_IDEAL for b in cc],
                           mode, len(data) + 8)
    out, _ = v.decode_soft(soft, expect_tail=True)
    assert list(out[:len(data)]) == data


@pytest.mark.parametrize("mode", [1, 2])
def test_k9_viterbi_corrects_sparse_errors(mode):
    random.seed(10 + mode)
    data = [random.randint(0, 1) for _ in range(400)]
    cc = conv_encode(data, fec_mode=mode, append_tail=True)
    # flip ~1.5% of surviving code bits, spread apart
    cc = list(cc)
    for i in range(7, len(cc), 70):
        cc[i] ^= 1
    v = SoftViterbi(mode)
    soft = depuncture_soft([SOFT_IDEAL if b else -SOFT_IDEAL for b in cc],
                           mode, len(data) + 8)
    out, _ = v.decode_soft(soft, expect_tail=True)
    assert list(out[:len(data)]) == data


# ---------------------------------------------------------------------------
# Block-level RS + CC round trips (hard and soft)
# ---------------------------------------------------------------------------

def _first_block_cc(mode, payload):
    data = payload.ljust(FIRST_BLOCK_DATA[mode], b"\x55")
    rs = rs_encode(scramble_block(data), RS_PARITY[mode])
    return data, conv_encode(bytes_to_bits(rs), fec_mode=mode,
                             append_tail=True)


@libfec
@pytest.mark.parametrize("mode", [0, 1, 2])
def test_first_block_hard_and_soft_roundtrip(mode):
    data, cc = _first_block_cc(mode, bytes(range(1, 21)))
    out, n = decode_first_block(list(cc), mode)
    assert out == data and n == 0
    soft = [SOFT_IDEAL if b else -SOFT_IDEAL for b in cc]
    out, n = decode_first_block_soft(soft, mode)
    assert out == data and n == 0


@libfec
@pytest.mark.parametrize("mode", [1, 2])
def test_first_block_soft_survives_noise(mode):
    rng = np.random.default_rng(mode)
    data, cc = _first_block_cc(mode, bytes(range(40, 60)))
    soft = np.array([SOFT_IDEAL if b else -SOFT_IDEAL for b in cc],
                    dtype=float)
    soft = np.clip(np.rint(soft + rng.normal(0, 2.5, soft.size)),
                   -7, 7).astype(int)
    out, n = decode_first_block_soft(list(soft), mode)
    assert out == data


@libfec
@pytest.mark.parametrize("mode", [0, 1, 2])
def test_followon_stream_roundtrip(mode):
    full = FOLLOWON_BLOCK_DATA[mode]
    payload = bytes((i * 7) & 0xFF for i in range(full + 5))   # 2 blocks
    rs = b""
    for off in range(0, len(payload), full):
        rs += rs_encode(scramble_block(payload[off:off + full]),
                        RS_PARITY[mode])
    cc = conv_encode(bytes_to_bits(rs), fec_mode=mode, append_tail=True)
    assert len(cc) == followon_cc_bits(len(rs), mode)
    out, n = decode_followon_stream(list(cc), mode, len(payload))
    assert out == payload
    soft = [SOFT_IDEAL if b else -SOFT_IDEAL for b in cc]
    out, n = decode_followon_stream_soft(soft, mode, len(payload))
    assert out == payload


# ---------------------------------------------------------------------------
# Full pipeline: generator audio -> demod -> decoder, every mode
# ---------------------------------------------------------------------------

def _pipeline(bits, modes, **dec):
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    audio = build_tx_audio(bits, TxTiming())
    demod_bits, _ = demodulate(audio, 43200)
    return Alert2Decoder(fec_modes=modes, **dec).decode_bits(demod_bits)


def _frame(mode, n_payload):
    app = bytes((i * 13 + 1) & 0xFF for i in range(n_payload))
    pdu = build_mant_pdu(app, 777, port=1)
    return build_airlink_bits([pdu], fec_mode=mode), app


@libfec
@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("n_payload", [4, 60, 150])
def test_generator_decoder_roundtrip_all_modes(mode, n_payload):
    bits, app = _frame(mode, n_payload)
    pkts = _pipeline(bits, [0, 1, 2])
    assert len(pkts) == 1
    p = pkts[0]
    assert p.fec_mode == mode
    assert not p.truncated
    assert p.application_payload == app
    assert p.mant.source_addr == 777


@libfec
@pytest.mark.parametrize("mode", [1, 2])
def test_mode_not_searched_is_not_decoded(mode):
    bits, _ = _frame(mode, 20)
    assert _pipeline(bits, [0]) == []


@libfec
@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize("iterative", ["demod", "sova"])
def test_iterative_rungs_do_not_break_punctured_modes(mode, iterative):
    bits, app = _frame(mode, 90)
    pkts = _pipeline(bits, [0, 1, 2], iterative=iterative)
    assert len(pkts) == 1 and pkts[0].application_payload == app


@libfec
@pytest.mark.parametrize("mode", [1, 2])
def test_hard_path_roundtrip_all_modes(mode):
    bits, app = _frame(mode, 90)
    pkts = _pipeline(bits, [0, 1, 2], soft_viterbi=False)
    assert len(pkts) == 1 and pkts[0].application_payload == app


@libfec
def test_frame_bit_length_mode_aware():
    from alert2.burst import frame_bit_length
    for mode in (0, 1, 2):
        for n in (4, 60, 150):
            bits, _ = _frame(mode, n)
            al_len = n + 6  # MANT header (approx) — only need an upper bound
            # frame_bit_length must cover the real frame (minus sync words)
            assert frame_bit_length(len(build_mant_pdu(
                bytes(n), 777, port=1)), mode) >= len(bits) - 48 - 32
