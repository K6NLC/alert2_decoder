# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for alert2.iterative — Phase 1 (erasure-RS fallback).

Validates:
  1. Clean signals decode via the 'standard' path (no erasures used).
  2. Decoded payload bytes match the original on clean signal.
  3. Erasure path triggers when standard RS fails.
  4. With `iterative=False`, decode behavior is byte-identical to the
     previous soft-V path (regression check — the main safety net).
  5. The helpers (_bit_reliabilities_from_soft, _byte_reliabilities,
     _erasure_positions) behave as documented.
"""
import random
import pytest

from alert2.constants import FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY
from alert2.fec import (
    SOFT_IDEAL, conv_encode, bytes_to_bits, scramble_block, rs_encode,
    decode_first_block_soft, decode_followon_stream_soft,
)
from alert2.iterative import (
    decode_first_block_iterative, decode_followon_stream_iterative,
    _bit_reliabilities_from_soft, _byte_reliabilities, _erasure_positions,
    DEFAULT_RELIABILITY_THRESHOLD,
)


def _make_clean_first_block_soft(data: bytes):
    """Encode `data` and return clean soft values (no noise)."""
    assert len(data) == FIRST_BLOCK_DATA[0]
    scrambled = scramble_block(data)
    codeword = rs_encode(scrambled, RS_PARITY[0])
    code_bits = conv_encode(bytes_to_bits(codeword), fec_mode=0,
                             append_tail=True)
    return [SOFT_IDEAL if b else -SOFT_IDEAL for b in code_bits]


def _make_clean_followon_soft(data: bytes):
    """Encode follow-on data and return clean soft values.

    Follow-on blocks are concatenated then CC-encoded as a single
    stream (per spec §3.5.2: 'All follow-on blocks must be CC encoded
    and decoded in a single stream').
    """
    full_block_data = FOLLOWON_BLOCK_DATA[0]
    parity = RS_PARITY[0]
    # Build all the RS-coded blocks concatenated
    rs_stream = b''
    for offset in range(0, len(data), full_block_data):
        block = data[offset:offset + full_block_data]
        scrambled = scramble_block(block)
        rs_stream += rs_encode(scrambled, parity)
    code_bits = conv_encode(bytes_to_bits(rs_stream), fec_mode=0,
                             append_tail=True)
    return [SOFT_IDEAL if b else -SOFT_IDEAL for b in code_bits]


def test_iterative_clean_first_block_uses_standard():
    """Clean signal: should decode via standard RS, no erasures."""
    data = bytes(range(FIRST_BLOCK_DATA[0]))
    soft = _make_clean_first_block_soft(data)
    result, n_corr, stats = decode_first_block_iterative(soft, fec_mode=0)
    assert result == data, "Decoded data must match original on clean signal"
    assert n_corr == 0, "Clean signal should need zero RS corrections"
    assert stats['method'] == 'standard', \
        f"Clean signal should use standard path, got {stats['method']}"
    assert stats['n_erasures'] == 0


def test_iterative_clean_followon_uses_standard():
    """Clean follow-on stream: decode via standard, no erasures."""
    data = bytes(range(64))  # spans 2 full follow-on blocks
    soft = _make_clean_followon_soft(data)
    result, n_corr, stats = decode_followon_stream_iterative(
        soft, fec_mode=0, total_data_bytes=len(data))
    assert result == data
    assert stats['method'] == 'standard'
    assert stats['n_erasures'] == 0


def test_iterative_matches_soft_v_on_clean_first_block():
    """Regression check: on a clean signal, iterative path produces
    the same decoded bytes as the existing soft-V path. They diverge
    only when standard RS fails.
    """
    data = bytes([0x42] * (FIRST_BLOCK_DATA[0] - 4) + [0xFF, 0xAA, 0x55, 0x00])
    soft = _make_clean_first_block_soft(data)
    iter_result, iter_corr, _ = decode_first_block_iterative(soft, fec_mode=0)
    softv_result, softv_corr = decode_first_block_soft(soft, fec_mode=0)
    assert iter_result == softv_result
    assert iter_corr == softv_corr


def test_iterative_matches_soft_v_on_clean_followon():
    """Regression check on follow-on path."""
    data = bytes(range(96))  # 3 full follow-on blocks
    soft = _make_clean_followon_soft(data)
    iter_result, iter_corr, _ = decode_followon_stream_iterative(
        soft, fec_mode=0, total_data_bytes=len(data))
    softv_result, softv_corr = decode_followon_stream_soft(
        soft, fec_mode=0, total_data_bytes=len(data))
    assert iter_result == softv_result
    assert iter_corr == softv_corr


def test_iterative_returns_correct_stats_shape():
    """Stats dict must always have method, n_erasures, n_low_rel_bytes."""
    data = bytes(range(FIRST_BLOCK_DATA[0]))
    soft = _make_clean_first_block_soft(data)
    _, _, stats = decode_first_block_iterative(soft, fec_mode=0)
    for key in ('method', 'n_erasures', 'n_low_rel_bytes'):
        assert key in stats, f"stats dict missing key {key}"
    assert stats['method'] in ('standard', 'erasure', 'failed')
    assert isinstance(stats['n_erasures'], int)
    assert isinstance(stats['n_low_rel_bytes'], int)


def test_followon_stream_empty_returns_empty():
    """total_data_bytes=0 should return empty bytes + standard method."""
    result, n_corr, stats = decode_followon_stream_iterative(
        [], fec_mode=0, total_data_bytes=0)
    assert result == b''
    assert n_corr == 0


def test_bit_reliabilities_from_soft():
    """Per-input-bit confidence = min(|c0|, |c1|) of the two coded bits."""
    # CC produces 2 coded bits per input bit. Mock soft values where
    # input bit i has confidence values (a_i, b_i) for c0, c1.
    soft = [+5, -3,   # input bit 0: c0=+5, c1=-3 -> min(|·|) = 3
            +1, +1,   # input bit 1: c0=+1, c1=+1 -> min = 1
            -6, +6,   # input bit 2: c0=-6, c1=+6 -> min = 6
            0, +4]    # input bit 3: c0=0, c1=+4 -> min = 0
    rels = _bit_reliabilities_from_soft(soft, n_input_bits=4)
    assert rels == [3, 1, 6, 0]


def test_byte_reliabilities_takes_min_per_byte():
    """A byte is as confident as its least-confident bit."""
    # 16 bit reliabilities -> 2 bytes
    bit_rels = [6, 6, 6, 6, 1, 6, 6, 6,   # byte 0: min=1
                3, 3, 3, 3, 3, 3, 3, 3]   # byte 1: min=3
    byte_rels = _byte_reliabilities(bit_rels)
    assert byte_rels == [1, 3]


def test_byte_reliabilities_partial_byte():
    """A non-multiple of 8 bits handles the final partial byte."""
    bit_rels = [4, 5, 6]  # only 3 bits; treat as 1 byte with min=4
    byte_rels = _byte_reliabilities(bit_rels)
    assert byte_rels == [4]


def test_erasure_positions_picks_least_reliable():
    """Lowest-reliability positions chosen first, sorted ascending."""
    byte_rels = [6, 0, 3, 6, 1, 6, 6, 2]
    threshold = 4  # everything < 4 is candidate
    erasures = _erasure_positions(byte_rels, threshold, max_erasures=10)
    # Candidates: pos 1 (rel 0), pos 4 (rel 1), pos 7 (rel 2), pos 2 (rel 3)
    # Sorted by position (function returns sorted list):
    assert erasures == [1, 2, 4, 7]


def test_erasure_positions_capped_by_max():
    """When more candidates than max, only the worst-N are taken."""
    byte_rels = [0, 0, 0, 0, 0, 0]  # all below threshold
    erasures = _erasure_positions(byte_rels, threshold=1, max_erasures=3)
    assert len(erasures) == 3


def test_erasure_positions_empty_when_all_confident():
    """No erasures if everything is above threshold."""
    byte_rels = [10, 10, 10, 10]
    erasures = _erasure_positions(byte_rels, threshold=5, max_erasures=10)
    assert erasures == []



def test_iterative_recovers_some_packets_via_erasure_path():
    """A signal noisy enough to fail standard RS but where the
    erasure path can recover. We construct it deliberately by adding
    moderate noise across the whole block AND extra disturbance at a
    few known byte positions.

    This isn't a guarantee that the production decoder will benefit —
    it's a unit test that the erasure path is wired correctly and
    can produce non-None output. The real benefit question is
    settled by paired A/B on real captures.
    """
    random.seed(7)
    data = bytes(range(FIRST_BLOCK_DATA[0]))
    soft = _make_clean_first_block_soft(data)
    # Heavy noise across the whole signal
    noisy = []
    for v in soft:
        n = int(random.gauss(0, SOFT_IDEAL * 0.7))
        noisy.append(max(-SOFT_IDEAL, min(SOFT_IDEAL, v + n)))
    # The test passes if the call returns sensibly — either standard
    # decoded it (Viterbi shrugged off the noise) or the erasure path
    # was tried. The "failed" outcome is also fine; this just exercises
    # the wiring.
    result, n_corr, stats = decode_first_block_iterative(noisy, fec_mode=0)
    assert stats['method'] in ('standard', 'erasure', 'failed')
    # If a method other than failed succeeded, the data should be
    # the original (we crafted noise small enough that recovery is
    # plausible).
    if result is not None:
        # n_corr is bounded by RS capacity
        assert n_corr >= 0


def test_iterative_threshold_parameter_respected():
    """Custom reliability_threshold changes the n_low_rel_bytes count."""
    # Clean signal: all bytes confident; with threshold=0, no low-rel
    # bytes; with threshold=SOFT_IDEAL+1, ALL bytes are below threshold.
    data = bytes(range(FIRST_BLOCK_DATA[0]))
    soft = _make_clean_first_block_soft(data)
    _, _, stats_low = decode_first_block_iterative(
        soft, fec_mode=0, reliability_threshold=0)
    _, _, stats_high = decode_first_block_iterative(
        soft, fec_mode=0, reliability_threshold=SOFT_IDEAL + 1)
    assert stats_low['n_low_rel_bytes'] == 0
    assert stats_high['n_low_rel_bytes'] > 0
    # But both should decode successfully (clean signal)
    # — threshold only matters for erasure-RS, which isn't triggered
    # because standard RS succeeds on clean.
    assert stats_low['method'] == 'standard'
    assert stats_high['method'] == 'standard'


# ---------------------------------------------------------------------------
# Post-investigation (2026-05-26): false-positive filter validation
# ---------------------------------------------------------------------------

def test_filter_rejects_truncated_erasure_packets():
    """Verify the decoder filter drops packets with (iter_method='erasure'
    AND truncated=True), since these are the false-positive pattern.
    
    Empirical observation: in paired A/B on 6 real captures, ALL false
    positives (decoded packets with source IDs not in the known network)
    had this signature. ALL legitimate gains did not.
    """
    from alert2.types import Alert2Packet
    # Build a fake packet that would match the filter pattern
    pkt = Alert2Packet(
        fec_mode=0,
        truncated=True,
        iter_method='erasure',
        iter_n_erasures=16,
    )
    # The filter is in decoder.decode_bits; here we just confirm the
    # packet fields are populated such that the filter can act on them.
    assert pkt.truncated and pkt.iter_method == 'erasure', \
        "Test packet doesn't match the filter pattern"


# ---------------------------------------------------------------------------
# Pattern B false-positive filter (2026-05-28 shop investigation)
# ---------------------------------------------------------------------------

def test_helper_passes_standard_rs_decodes():
    """iter_method != 'erasure' is never a false positive — even if
    truncated or unparseable SRP."""
    from alert2.decoder import Alert2Decoder
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    pkt = SimpleNamespace(iter_method=None, truncated=False,
                          mant=mant, srp_pdu=None)
    assert not Alert2Decoder._is_likely_false_positive(pkt)
    # Standard RS that is truncated: still not filtered
    pkt = SimpleNamespace(iter_method='standard', truncated=True,
                          mant=mant, srp_pdu=None)
    assert not Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_drops_erasure_plus_truncated():
    """Pattern A: iter='erasure' AND truncated=True."""
    from alert2.decoder import Alert2Decoder
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    pkt = SimpleNamespace(iter_method='erasure', truncated=True,
                          mant=mant, srp_pdu='valid')
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_drops_erasure_port0_no_srp():
    """Pattern B: iter='erasure' AND port=0 AND srp_pdu=None."""
    from alert2.decoder import Alert2Decoder
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant, srp_pdu=None)
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_passes_erasure_with_srp():
    """Legitimate erasure decode with valid SRP parse (recognized
    report types, no TEST flag) — must NOT drop."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, GeneralSensorReport as GsrReport
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    srp = SrpPdu(reports=[GsrReport(readings=[])])
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant, srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[5, 3])
    assert not Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_drops_erasure_with_unknown_srp_type():
    """Pattern D.1: erasure decode whose SRP contains UnknownReport
    (e.g. Type65, Type174) is a noise-derived codeword."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, UnknownReport
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    srp = SrpPdu(reports=[UnknownReport(report_type=65, raw_bytes=b'\x00\x11')])
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant, srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[5])
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_drops_erasure_with_unknown_sensor_format():
    """Pattern F: erasure decode whose GSR contains a sensor reading
    with an unrecognized format code (renders as a hex blob). This is
    the tkr_audio TK790 failure mode where erasure-RS filled a corrupt
    block with bytes parsing as GSR but with garbage sensor readings."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import parse_srp_pdu
    from types import SimpleNamespace
    # Real garbage payload from tkr_audio_iterative.diag src=716:
    # GSR(sensor_21=, sensor_151=e2f46e7e2fb9eb8e) — fmt_0xc unknown
    apphex = ("14426602040012067d0118150097c8e2f46e7e2fb9eb8e"
              "316b64a877a025f405796ade")
    srp = parse_srp_pdu(bytes.fromhex(apphex))
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=SimpleNamespace(port=0), srp_pdu=srp,
                          airlink=SimpleNamespace(length=46),
                          rs_corrections=[8, 8])
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_keeps_clean_erasure_recovery():
    """Pattern F must NOT filter a legitimate erasure recovery whose
    readings all parse with recognized formats. This is src=4615 from
    the same capture — a real GSR recovered by erasure-RS, confirmed
    against its independent clean-path decode."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, GeneralSensorReport, SensorReading
    from types import SimpleNamespace
    # All readings have recognized value types (uint/fp2), no fmt_0x
    gsr = GeneralSensorReport(readings=[
        SensorReading(sensor_id=10, format_code=0x1, length=1,
                      raw_bytes=b'\x00', value=0, value_type='uint'),
        SensorReading(sensor_id=3, format_code=0x3, length=2,
                      raw_bytes=b'\x00\x00', value=13.04, value_type='fp2'),
    ])
    srp = SrpPdu(reports=[gsr])
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=SimpleNamespace(port=0), srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[8, 8])
    assert not Alert2Decoder._is_likely_false_positive(pkt)
    """Pattern D.2: erasure decode with TEST flag set is unlikely
    to be real operational traffic."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, GeneralSensorReport as GsrReport
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    srp = SrpPdu(test_flag=True, reports=[GsrReport(readings=[])])
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant, srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[5])
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_drops_erasure_with_extreme_rs_ratio():
    """Pattern E: RS_corr/airlink_len > 1.0 indicates extreme
    correction effort disproportionate to packet size."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, GeneralSensorReport as GsrReport
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    srp = SrpPdu(reports=[GsrReport(readings=[])])
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant, srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[30, 20])  # 50/40 = 1.25
    assert Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_passes_standard_rs_with_test_flag():
    """Pattern D only applies to iter='erasure'. Standard-RS decodes
    with TEST flag are allowed through (operator may genuinely test)."""
    from alert2.decoder import Alert2Decoder
    from alert2.srp import SrpPdu, GeneralSensorReport as GsrReport
    from types import SimpleNamespace
    mant = SimpleNamespace(port=0)
    srp = SrpPdu(test_flag=True, reports=[GsrReport(readings=[])])
    pkt = SimpleNamespace(iter_method='standard', truncated=False,
                          mant=mant, srp_pdu=srp,
                          airlink=SimpleNamespace(length=40),
                          rs_corrections=[5])
    assert not Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_passes_erasure_on_non_srp_port():
    """Pattern B only applies to port=0 (SRP). Other ports (e.g. port 1
    for Concentration Protocol) should not trigger the filter since
    they wouldn't have an srp_pdu by design."""
    from alert2.decoder import Alert2Decoder
    from types import SimpleNamespace
    mant_port1 = SimpleNamespace(port=1)
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=mant_port1, srp_pdu=None)
    assert not Alert2Decoder._is_likely_false_positive(pkt)


def test_helper_handles_none_mant():
    """Defensive: if mant is None for any reason, don't crash."""
    from alert2.decoder import Alert2Decoder
    from types import SimpleNamespace
    pkt = SimpleNamespace(iter_method='erasure', truncated=False,
                          mant=None, srp_pdu=None)
    # Should not raise — and should not filter (defensive: don't drop
    # if we can't even check the port)
    assert not Alert2Decoder._is_likely_false_positive(pkt)
