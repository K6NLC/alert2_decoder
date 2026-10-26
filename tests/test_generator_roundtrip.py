# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Generator <-> decoder round-trip tests.

Every frame the generator produces must decode byte-exact through the
real decoder pipeline (RRC audio -> demodulate -> Alert2Decoder). This
pins the two directions together: if either side's framing drifts,
these fail.

Requires libfec (same dependency as the decoder's RS path).
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.generator import (
    make_sensor_frame, make_v2_query_frame, make_raw_frame,
    build_mant_pdu, build_airlink_bits, build_tx_audio, TxTiming,
    build_ind_get, test_tone, encode_fp2,
)
from alert2.srp import decode_fp2


def _libfec_available():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec_available(),
                            reason="libfec not installed")


def _decode(bits):
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    audio = build_tx_audio(bits, TxTiming())
    demod_bits, _ = demodulate(audio, 43200)
    return Alert2Decoder().decode_bits(demod_bits)


@libfec
def test_sensor_frame_roundtrip():
    pkts = _decode(make_sensor_frame(723, [(5, 12.34), (8, 128)],
                                     timestamp=39600))
    assert len(pkts) == 1
    m = pkts[0].mant
    assert m.source_addr == 723 and m.port == 0
    # SRP control byte 0x74 = TS flag + pdu_id 7 (matches field traffic)
    assert pkts[0].application_payload[0] == 0x74


@libfec
def test_v2_query_roundtrip():
    pkts = _decode(make_v2_query_frame(999, 4601))
    assert len(pkts) == 1
    assert pkts[0].application_payload == bytes.fromhex("0b0152")
    assert pkts[0].mant.dest_addr == 4601
    assert pkts[0].mant.port == 8


@libfec
def test_multiblock_roundtrip():
    payload = bytes(range(100))  # forces 3 follow-on blocks at FEC 0
    pdu = build_mant_pdu(payload, source_addr=4601, dest_addr=1, port=1)
    pkts = _decode(build_airlink_bits([pdu]))
    assert len(pkts) == 1
    assert pkts[0].application_payload == payload


@libfec
def test_aggregated_multi_pdu_roundtrip():
    p1 = build_mant_pdu(b"\x74\x00\x01\x01\x05\x05\x11\x2a", 733)
    p2 = build_mant_pdu(b"\x74\x00\x02\x01\x05\x08\x11\x80", 753)
    p3 = build_mant_pdu(build_ind_get(), 999, dest_addr=4601, port=8)
    pkts = _decode(build_airlink_bits([p1, p2, p3]))
    assert [p.mant.source_addr for p in pkts] == [733, 753, 999]


def test_ind_get_bytes():
    assert build_ind_get() == bytes.fromhex("0b0152")
    assert build_ind_get([0x52, 0x75]) == bytes.fromhex("0b025275")


def test_fp2_roundtrip():
    for v in (0.0, 12.34, -7.5, 128.0, -799.9, 7999.0):
        assert abs(decode_fp2(encode_fp2(v)) - v) < 0.005


def test_half_amplitude_tone():
    full = test_tone(100, amplitude=0.8)
    half = test_tone(100, amplitude=0.8, half=True)
    assert np.isclose(np.max(np.abs(half)),
                      np.max(np.abs(full)) / 2, rtol=0.01)
