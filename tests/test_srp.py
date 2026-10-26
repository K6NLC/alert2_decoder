# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the SRP application-layer parser."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from alert2.srp import (
    parse_srp_pdu, decode_value, decode_fp2,
    GeneralSensorReport, TippingBucketReport, MultiSensorReport,
    UnknownReport, RECOMMENDED_SENSOR_NAMES,
)


def test_decode_fp2_simple():
    """C4 D2 should decode to -12.34 per spec."""
    val = decode_fp2(bytes.fromhex('C4D2'))
    # 0xC4D2 = 1100 0100 1101 0010
    # sign=1, exp=10=2, mantissa=0010011010010 = 1234
    # value = -1 * 10^-2 * 1234 = -12.34
    assert abs(val - (-12.34)) < 0.001, f"Expected -12.34, got {val}"


def test_decode_fp2_max():
    """1F 3F should decode to 7999."""
    val = decode_fp2(bytes.fromhex('1F3F'))
    # 0x1F3F = 0001 1111 0011 1111
    # sign=0, exp=00=0, mantissa=1111100111111 = 7999
    # value = 1 * 10^0 * 7999 = 7999
    assert val == 7999


def test_decode_value_uint():
    val, t = decode_value(0x1, 2, bytes.fromhex('0064'))
    assert val == 100
    assert t == 'uint'


def test_decode_value_int_negative():
    val, t = decode_value(0x2, 1, bytes.fromhex('FF'))
    assert val == -1
    assert t == 'int'


def test_decode_value_float32():
    """4-byte float — 8.04 per spec example = 0x4100A3D7."""
    val, t = decode_value(0x3, 4, bytes.fromhex('4100A3D7'))
    assert abs(val - 8.04) < 0.001
    assert t == 'float32'


def test_parse_srp_gsr_simple():
    """Spec Figure 4-1: pH and water temp report."""
    # 70 = control byte (PDU ID = 7 = disabled)
    # 01 = type GSR, 0A = length 10
    # then S=12 FL=34 V=4100A3D7 (pH=8.04)
    # then S=13 FL=22 V=0276 (temp=630)
    pdu_hex = '70010A1234' + '4100A3D7' + '13220276'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert pdu.version == 0
    assert pdu.test_flag is False
    assert pdu.has_timestamp is False
    assert pdu.pdu_id == 7
    assert len(pdu.reports) == 1
    rpt = pdu.reports[0]
    assert isinstance(rpt, GeneralSensorReport)
    assert len(rpt.readings) == 2
    assert rpt.readings[0].sensor_id == 18  # 0x12
    assert abs(rpt.readings[0].value - 8.04) < 0.001
    assert rpt.readings[1].sensor_id == 19  # 0x13
    assert rpt.readings[1].value == 630


def test_parse_srp_tbrg():
    """Spec Figure 4-2: rain gage report."""
    # 50 = control byte (PDU ID = 5)
    # 02 = type TBRG, 0A = length 10
    # 00 = sensor 0, 14 = FL (uint, 4 bytes), 00000068 = accum 104
    # 14 0F 0A 02 = tip offsets 20, 15, 10, 2
    pdu_hex = '50020A0014' + '00000068' + '140F0A02'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert pdu.version == 0
    assert len(pdu.reports) == 1
    rpt = pdu.reports[0]
    assert isinstance(rpt, TippingBucketReport)
    assert rpt.sensor_id == 0
    assert rpt.accumulator == 104
    assert rpt.tip_offsets == [20, 15, 10, 2]


def test_parse_srp_msr_type3():
    """Spec Figure 4-3 layout — Type 3 MSR with 5 sensors set.

    Note: spec example uses control byte 0x7C (TS bit set, expecting
    timestamp service from lower layer), but here we test the bytes
    directly. We use control 0x78 (same flags minus TS) so no TS is
    expected to follow.
    """
    # Control 0x78 = 01111000 — pdu_id=7, test set, no TS
    # Type 3, length 8, flags 9B = 10011011
    pdu_hex = '7803089B' + '00EA' + '29' + '08' + '0109' + '7F'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert pdu.test_flag is True
    assert len(pdu.reports) == 1, f"Got {len(pdu.reports)} reports, errors: {pdu.parse_errors}"
    rpt = pdu.reports[0]
    assert isinstance(rpt, MultiSensorReport)
    assert rpt.units == 'english'
    # 5 sensors set in flags 0x9B = 10011011 (bits 0,1,3,4,7) -> AT, RH, WS, WD, BV
    assert len(rpt.readings) == 5


def test_parse_srp_with_timestamp():
    """Control byte with TS bit set should consume 2 bytes after."""
    # 04 = 0000 0100, only TS bit set
    # 0078 = 120 second timestamp
    # 01 02 0011 00 = type GSR, length 2, sensor 0x11, FL=0x00 (zero-length)
    pdu_hex = '04' + '0078' + '0102' + '00' + '00'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert pdu.has_timestamp is True
    assert pdu.timestamp == 120


def test_parse_srp_unknown_type():
    """Unknown report types should be preserved as raw bytes."""
    # Control 00, type 99 (0x63), length 2, two bytes of data
    pdu_hex = '00' + '63' + '02' + 'ABCD'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert len(pdu.reports) == 1
    assert isinstance(pdu.reports[0], UnknownReport)
    assert pdu.reports[0].report_type == 0x63
    assert pdu.reports[0].raw_bytes == bytes.fromhex('ABCD')


def test_parse_srp_admin_sensor_id():
    """Sensor ID not in recommended list should still decode with raw ID."""
    # Control 0x00, Type 1 (GSR), Length 0x03
    # Sensor ID 77 (0x4D), FL 0x11 (1 byte uint), value 0x2A = 42
    pdu_hex = '00' + '01' + '03' + '4D' + '11' + '2A'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    assert len(pdu.reports) == 1
    rpt = pdu.reports[0]
    assert isinstance(rpt, GeneralSensorReport)
    assert len(rpt.readings) == 1
    r = rpt.readings[0]
    assert r.sensor_id == 77
    assert r.sensor_name is None  # No recommended name
    assert r.value == 42


def test_recommended_sensor_names_match_spec():
    """Verify our recommended sensor names match the spec."""
    expected = {0: 'Rain', 7: 'Stage', 8: 'Battery Voltage'}
    for sid, name in expected.items():
        assert RECOMMENDED_SENSOR_NAMES[sid] == name


def test_parse_srp_padding_fill():
    """Trailing 0x55 padding bytes should be ignored."""
    # GSR with sensor 0, value 42, then 4 bytes of 0x55 padding
    pdu_hex = '00' + '01' + '03' + '00' + '11' + '2A' + '55555555'
    pdu = parse_srp_pdu(bytes.fromhex(pdu_hex))
    # Should have one GSR with sensor 0, and not throw on padding
    assert len(pdu.reports) >= 1
    rpt = pdu.reports[0]
    assert isinstance(rpt, GeneralSensorReport)


if __name__ == '__main__':
    print("Running SRP tests...")
    test_decode_fp2_simple(); print("  ✓ FP2 simple decode (-12.34)")
    test_decode_fp2_max(); print("  ✓ FP2 max value (7999)")
    test_decode_value_uint(); print("  ✓ decode unsigned int")
    test_decode_value_int_negative(); print("  ✓ decode signed int negative")
    test_decode_value_float32(); print("  ✓ decode float32 (8.04)")
    test_parse_srp_gsr_simple(); print("  ✓ Parse GSR with pH and temp")
    test_parse_srp_tbrg(); print("  ✓ Parse TBRG with 4 tips")
    test_parse_srp_msr_type3(); print("  ✓ Parse MSR Type 3 (English)")
    test_parse_srp_with_timestamp(); print("  ✓ Parse SRP with timestamp")
    test_parse_srp_unknown_type(); print("  ✓ Unknown report type preserved")
    test_parse_srp_admin_sensor_id(); print("  ✓ Admin-defined sensor ID")
    test_recommended_sensor_names_match_spec(); print("  ✓ Recommended sensor names")
    test_parse_srp_padding_fill(); print("  ✓ Trailing 0x55 padding handled")
    print("All SRP tests passed!")


# ---------------------------------------------------------------------------
# MSR-IND IND Temperature width fix (2026-05-27 regression test)
# ---------------------------------------------------------------------------

def test_msr_ind_temp_is_2_bytes_unsigned():
    """MSR-IND IND Temperature must be 2 bytes UNSIGNED (per Application
    v1.3 spec §2.4.7 / Figure 2-12), NOT 1 byte signed.

    The earlier 1-byte interpretation silently shifted all subsequent
    fields by one byte, making Messages Received / Messages Sent
    counters appear incorrect.

    Real payload from 2026-05-24 22:02:45 (src=4600 status report):
      hex: 70 05 0a 3f 03 90 01 06 34 51 16 ce 00
      Expected decode:
        Clock=3, Battery=14.4V, IND Temp=26.2°C,
        Msg Rx=13393, Msg Tx=5838, Status Bits=0
    """
    from alert2.srp import parse_srp_pdu
    payload = bytes.fromhex('70050a3f03900106345116ce00')
    pdu = parse_srp_pdu(payload)
    assert pdu is not None
    s = str(pdu)
    # Check that IND Temp parses as ~26.2 (not 1)
    assert 'IND Temp=26.2' in s, f"IND Temp wrong in: {s}"
    # And the message counters are the right values
    assert 'Messages Received=13393' in s, f"Messages Received wrong in: {s}"
    assert 'Messages Sent=5838' in s, f"Messages Sent wrong in: {s}"
    # And Status Bits is the actual last byte (0), not a misaligned read
    assert 'Status Bits=0' in s, f"Status Bits wrong in: {s}"


def test_msr_ind_with_timestamp_parses_correctly():
    """MSR-IND with TS-present control byte (4601-style report).

    Real payload from 2026-05-24 23:00:16 (src=4601 status report):
      hex: 74 54 6c 05 0a 3f 00 85 01 05 ca 8e a8 01 00
      Control byte 0x74 means TS present.
      Expected: TS=21612, Clock=0, Battery=13.3V, IND Temp=26.1°C,
                Msg Rx=51854, Msg Tx=43009, Status Bits=0
    """
    from alert2.srp import parse_srp_pdu
    payload = bytes.fromhex('74546c050a3f00850105ca8ea80100')
    pdu = parse_srp_pdu(payload)
    assert pdu is not None
    s = str(pdu)
    assert 'ts=21612' in s, f"Timestamp wrong in: {s}"
    assert 'IND Temp=26.1' in s, f"IND Temp wrong in: {s}"
    assert 'Messages Received=51854' in s, f"Messages Received wrong in: {s}"
    assert 'Messages Sent=43009' in s, f"Messages Sent wrong in: {s}"
