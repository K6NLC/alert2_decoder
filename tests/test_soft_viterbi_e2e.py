# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
End-to-end soft-Viterbi integration test.

Unlike test_soft_viterbi.py (which exercises the CC stage in isolation),
this builds a *complete* ALERT2 AirLink frame — RS-encoded, scrambled,
CC-encoded, framesync-prefixed, FSK-shaped into an audio waveform — then
runs it through the real Alert2Decoder with soft_viterbi on and off.

Requires libfec (RS stage). Skipped automatically if unavailable.

This is the test that proves the decoder plumbing works: soft samples
flowing demod -> decode_bits -> _try_decode_at -> decode_*_soft -> RS,
and the resulting packet matching the hard-path packet on clean signals.
"""
import random
import numpy as np
import pytest

from alert2 import Alert2Decoder
from alert2.constants import (
    SAMPLES_PER_BIT, RX_SAMPLE_RATE, RRC_TAPS, FRAMESYNC_M0,
    FIRST_BLOCK_DATA, RS_PARITY,
)
from alert2.fec import scramble_block, rs_encode, conv_encode


def _libfec_available():
    try:
        import ctypes
        ctypes.CDLL("libfec.so.0")
        return True
    except OSError:
        try:
            import ctypes
            ctypes.CDLL("libfec.so")
            return True
        except OSError:
            return False


pytestmark = pytest.mark.skipif(
    not _libfec_available(),
    reason="libfec not installed; RS stage unavailable")


def _build_first_block_only_frame(payload_24: bytes) -> list:
    """Build the bit stream for a single-first-block ALERT2 frame.

    24 data bytes (FEC mode 0 first block) that fit entirely in the
    first block — no follow-on — so the test is self-contained and
    doesn't depend on follow-on stream framing.

    Returns a list of bits: [framesync(32)] + [CC(first block + tail)].
    """
    assert len(payload_24) == FIRST_BLOCK_DATA[0]  # 24

    # 1. Scramble (per-block reset)
    scrambled = scramble_block(payload_24)
    # 2. RS encode -> 24 + 16 = 40 bytes
    rs_cw = rs_encode(scrambled, RS_PARITY[0])
    assert len(rs_cw) == FIRST_BLOCK_DATA[0] + RS_PARITY[0]
    # 3. RS bytes -> bits (MSB first)
    rs_bits = []
    for byte in rs_cw:
        for i in range(7, -1, -1):
            rs_bits.append((byte >> i) & 1)
    # 4. CC encode with tail
    cc_bits = conv_encode(rs_bits, fec_mode=0, append_tail=True)
    # Pad CC stream to byte alignment the way the spec/ decoder expects:
    # decoder slices first_block_total*8*2 + 12 then pads. We just append
    # 4 zero bits so total is ...*2 + 16, matching offset math.
    while len(cc_bits) % 16 != 0:
        cc_bits.append(0)

    # 5. Framesync prefix (32 bits, MSB first)
    fs_bits = [(FRAMESYNC_M0 >> i) & 1 for i in range(31, -1, -1)]

    # A little bitsync lead-in (alternating) so framesync detection is
    # comfortable, then framesync, then payload.
    lead = [1, 0] * 24
    return lead + fs_bits + cc_bits


def _bits_to_fsk_audio(bits, sps=SAMPLES_PER_BIT, noise_std=0.0, seed=0):
    """Turn a bit stream into an RRC-shaped FSK-ish baseband waveform at
    RX_SAMPLE_RATE, optionally adding Gaussian noise.

    bit 1 -> +1 level, bit 0 -> -1 level (NRZ), then RRC pulse-shaped —
    this matches what the demod's matched filter expects.
    """
    nrz = np.repeat(np.array([1.0 if b else -1.0 for b in bits]),
                    sps).astype(np.float64)
    shaped = np.convolve(nrz, RRC_TAPS, mode='same')
    # Normalize to ~unit amplitude
    shaped = shaped / (np.std(shaped) + 1e-9)
    if noise_std > 0:
        rng = np.random.RandomState(seed)
        shaped = shaped + rng.normal(0.0, noise_std, size=len(shaped))
    return shaped.astype(np.float32)


def _decode(audio, soft):
    """Run the audio through Alert2Decoder via demodulate(), return list
    of packets. Mirrors what run_file_mode does."""
    from alert2.demod import demodulate
    dec = Alert2Decoder(max_framesync_hd=6, soft_viterbi=soft)
    bits, info = demodulate(audio, RX_SAMPLE_RATE)
    pkts = []
    for label, b_use, inv in [('n', bits, False),
                              ('i', [1 - b for b in bits], True)]:
        pkts.extend(dec.decode_bits(
            b_use,
            audio_43k=info.get('audio_43k'),
            bit_phase=info.get('best_phase', 0),
            soft_samples=info.get('soft_samples'),
            amplitude_estimate=info.get('amplitude_estimate'),
            soft_inverted=inv))
    return pkts


def test_clean_frame_soft_equals_hard():
    """A clean synthesized frame must decode identically with hard and
    soft Viterbi, and the soft packet must be tagged viterbi_mode='soft'.

    This proves the soft plumbing (demod -> decode_bits -> _try_decode_at
    -> decode_*_soft -> RS) produces a byte-identical result to the hard
    path on a clean signal — i.e. soft Viterbi is a safe drop-in.
    """
    payload = bytes([0x00, 0x16]) + bytes([0x00, 0x00]) + bytes(range(20))  # valid MANT claim (7B <= 22B airlink), Pattern-K consistent  # AirLink len=0x16
    bits = _build_first_block_only_frame(payload)
    audio = _bits_to_fsk_audio(bits, noise_std=0.0)

    hard_pkts = _decode(audio, soft=False)
    soft_pkts = _decode(audio, soft=True)

    assert len(hard_pkts) >= 1, "hard decode produced no packets"
    assert len(soft_pkts) >= 1, "soft decode produced no packets"

    h = hard_pkts[0]
    s = soft_pkts[0]
    # Byte-for-byte identical decoded output
    assert bytes(h.raw_payload) == bytes(s.raw_payload)
    assert h.fec_mode == s.fec_mode == 0
    assert h.rs_corrections == s.rs_corrections
    # Tagging is correct
    assert h.viterbi_mode == 'hard'
    assert s.viterbi_mode == 'soft'


def test_soft_matches_hard_across_light_noise():
    """Across light noise that both decoders can handle, soft and hard
    must produce the SAME decoded packet bytes (soft is not introducing
    errors). This is the regression guard from design-doc 6c criterion
    'no hard-only packets / no soft corruption'.

    We don't assert a packet is always recovered (low noise can still
    occasionally break a short synthetic frame); we assert that WHENEVER
    both decode something, they agree, and soft never decodes garbage
    when hard got it right.
    """
    payload = bytes([0x00, 0x16]) + bytes([0x00, 0x00]) + bytes(range(20))  # valid MANT claim (7B <= 22B airlink), Pattern-K consistent
    bits = _build_first_block_only_frame(payload)

    agreements = 0
    comparisons = 0
    for noise in [0.0, 0.05, 0.1, 0.15, 0.2]:
        for seed in range(5):
            audio = _bits_to_fsk_audio(bits, noise_std=noise, seed=seed)
            h = _decode(audio, soft=False)
            s = _decode(audio, soft=True)
            if h and s:
                comparisons += 1
                hb = bytes(h[0].raw_payload)
                sb = bytes(s[0].raw_payload)
                assert hb == sb, (
                    f"soft/hard disagree at noise={noise} seed={seed}: "
                    f"hard={hb.hex()} soft={sb.hex()}")
                agreements += 1
    assert comparisons > 0, "no successful decodes to compare"
    assert agreements == comparisons


def test_soft_cc_gain_through_rs_pipeline():
    """Soft-decision Viterbi must, at the CC level feeding RS, produce
    no more byte errors than hard from the SAME noisy waveform — and
    fewer on aggregate at the threshold. This is the rigorous form of
    'soft helps', measured where the comparison is apples-to-apples
    (same received samples into both decoders).

    We compare the RS *codeword* recovered by hard vs soft Viterbi
    against the known-correct codeword, counting byte errors that RS
    would then have to correct (RS mode 0 corrects up to 8).
    """
    from alert2.fec import (Viterbi, SoftViterbi, quantize_soft,
                            scramble_block, rs_encode, bits_to_bytes)
    from alert2.constants import FIRST_BLOCK_DATA, RS_PARITY

    payload = bytes([0x00, 0x16]) + bytes([0x00, 0x00]) + bytes(range(20))  # valid MANT claim (7B <= 22B airlink), Pattern-K consistent
    scrambled = scramble_block(payload)
    rs_cw = rs_encode(scrambled, RS_PARITY[0])          # 40 bytes, truth
    rs_bits = []
    for byte in rs_cw:
        for i in range(7, -1, -1):
            rs_bits.append((byte >> i) & 1)
    code = conv_encode(rs_bits, fec_mode=0, append_tail=True)

    hard_err_total = 0
    soft_err_total = 0
    trials = 16
    noise = 0.95  # threshold region for this code rate
    for t in range(trials):
        rng = random.Random(5000 + t)
        floaty = [(1.0 if c else -1.0) + rng.gauss(0.0, noise) for c in code]
        hard_code = [1 if v > 0 else 0 for v in floaty]
        soft_q = quantize_soft(floaty, amplitude_estimate=1.0)

        hd, _ = Viterbi(0).decode(hard_code, expect_tail=True)
        sd, _ = SoftViterbi(0).decode_soft(soft_q, expect_tail=True)

        hard_bytes = bits_to_bytes(hd)[:len(rs_cw)]
        soft_bytes = bits_to_bytes(sd)[:len(rs_cw)]
        hard_err_total += sum(1 for a, b in zip(hard_bytes, rs_cw) if a != b)
        soft_err_total += sum(1 for a, b in zip(soft_bytes, rs_cw) if a != b)

    assert soft_err_total <= hard_err_total
    assert soft_err_total < hard_err_total, (
        f"soft gave no CC-level benefit: soft={soft_err_total} "
        f"hard={hard_err_total} byte-errors over {trials} trials")


def test_no_false_packets_from_pure_noise():
    """Soft Viterbi must not hallucinate packets from pure noise.
    RS parity makes a false positive astronomically unlikely; this
    guards the plumbing (e.g., a bug that bypasses RS validation)."""
    rng = np.random.RandomState(12345)
    noise_audio = rng.normal(0.0, 1.0, size=RX_SAMPLE_RATE * 3).astype(np.float32)
    pkts = _decode(noise_audio, soft=True)
    assert pkts == [], f"soft Viterbi produced {len(pkts)} phantom packets"
