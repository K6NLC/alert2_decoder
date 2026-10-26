# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the FEC pipeline (scrambler, RS, conv, Viterbi)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from alert2.fec import (
    scramble_block, descramble_block,
    rs_encode, rs_decode,
    conv_encode, Viterbi,
    bytes_to_bits, bits_to_bytes,
    decode_first_block,
)


def test_scrambler_roundtrip():
    """Scrambling then descrambling should recover original data."""
    test_data = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0,
                       0x00, 0xFF, 0x55, 0xAA, 0x01, 0xFE, 0x80, 0x7F,
                       0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    assert len(test_data) == 24
    scrambled = scramble_block(test_data)
    descrambled = descramble_block(scrambled)
    assert descrambled == test_data, \
        f"scramble/descramble roundtrip failed:\n  in:  {test_data.hex()}\n  out: {descrambled.hex()}"


def test_scrambler_zeros_predictable():
    """Scrambling all-zeros should produce the MLS sequence (deterministic).

    We just check the first 4 bytes are NON-zero (proving the scrambler
    actually scrambles) and that running it twice gives the same result.
    """
    zeros = bytes(24)
    s1 = scramble_block(zeros)
    s2 = scramble_block(zeros)
    assert s1 == s2, "Scrambler not deterministic"
    assert s1[:4] != bytes(4), "Scrambler produced zeros!"


def test_rs_roundtrip_no_errors():
    test = bytes(range(24))
    encoded = rs_encode(test, 16)
    assert len(encoded) == 40
    decoded, n_corr = rs_decode(encoded, 24, 16)
    assert decoded == test
    assert n_corr == 0


def test_rs_corrects_errors():
    test = bytes(range(24))
    encoded = rs_encode(test, 16)
    corrupted = bytearray(encoded)
    # Add 7 errors (RS(40,24) corrects up to 8 errors)
    for pos in [3, 7, 11, 15, 19, 25, 35]:
        corrupted[pos] ^= 0xFF
    decoded, n_corr = rs_decode(bytes(corrupted), 24, 16)
    assert decoded == test, f"Expected recovery, got {decoded}"
    assert n_corr == 7


def test_rs_fails_too_many_errors():
    test = bytes(range(24))
    encoded = rs_encode(test, 16)
    corrupted = bytearray(encoded)
    # Add 9 errors (more than RS(40,24) can correct)
    for pos in [1, 3, 5, 7, 9, 11, 13, 15, 17]:
        corrupted[pos] ^= 0xFF
    decoded, n_corr = rs_decode(bytes(corrupted), 24, 16)
    assert decoded is None
    assert n_corr == -1


def test_viterbi_no_errors():
    data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1, 1] * 3
    encoded = conv_encode(data, fec_mode=0, append_tail=True)
    assert len(encoded) == 2 * (len(data) + 6)
    v = Viterbi()
    decoded, pm = v.decode(encoded, expect_tail=True)
    assert pm == 0, f"Expected pm=0 (no errors), got {pm}"
    assert decoded == data


def test_viterbi_corrects_errors():
    data = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1, 1] * 3
    encoded = conv_encode(data, fec_mode=0, append_tail=True)
    corrupted = list(encoded)
    # Flip a few well-separated bits
    corrupted[5] ^= 1
    corrupted[20] ^= 1
    corrupted[40] ^= 1
    v = Viterbi()
    decoded, pm = v.decode(corrupted, expect_tail=True)
    assert decoded == data, f"Viterbi failed to correct: {pm} errors detected"


def test_full_first_block_roundtrip():
    """Full encode -> decode pipeline: scramble -> RS -> conv -> Viterbi -> RS -> descramble."""
    # Some test data, 24 bytes
    test_pdu = bytes([0x00, 0x23, 0x06, 0x00, 0x00, 0x1A, 0x03, 0x07,
                      0x01, 0x11, 0xF9, 0x24, 0x74, 0x4D, 0x01, 0x15,
                      0x0A, 0x11, 0x00, 0x08, 0x34, 0x41, 0x5C, 0xF7])
    # Encode
    scrambled = scramble_block(test_pdu)
    rs_encoded = rs_encode(scrambled, 16)
    cc_bits = conv_encode(bytes_to_bits(rs_encoded), fec_mode=0, append_tail=True)

    # Decode
    decoded, n_corr = decode_first_block(cc_bits, fec_mode=0)
    assert decoded == test_pdu, \
        f"Roundtrip failed:\n  in:  {test_pdu.hex()}\n  out: {decoded.hex() if decoded else None}"
    assert n_corr == 0


if __name__ == '__main__':
    print("Running FEC tests...")
    test_scrambler_roundtrip(); print("  ✓ scrambler roundtrip")
    test_scrambler_zeros_predictable(); print("  ✓ scrambler zeros is deterministic + non-zero")
    test_rs_roundtrip_no_errors(); print("  ✓ RS roundtrip (no errors)")
    test_rs_corrects_errors(); print("  ✓ RS corrects 7 errors")
    test_rs_fails_too_many_errors(); print("  ✓ RS fails on 9 errors")
    test_viterbi_no_errors(); print("  ✓ Viterbi roundtrip (no errors)")
    test_viterbi_corrects_errors(); print("  ✓ Viterbi corrects 3 errors")
    test_full_first_block_roundtrip(); print("  ✓ Full first-block roundtrip")
    print("All tests passed!")
