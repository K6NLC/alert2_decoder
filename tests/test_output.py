# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the output module."""
import sys
import os
import csv
import json
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from alert2.types import Alert2Packet, AirLinkHeader, MantHeader
from alert2.output import (
    StdoutOutput, LogFileOutput, CsvOutput, JsonlOutput,
    StatsCollector, MultiOutput,
    packet_to_dict, packet_to_json_dict, format_packet_human,
)


def make_test_packet():
    """Create a sample Alert2Packet for testing."""
    return Alert2Packet(
        fec_mode=0,
        rx_time=1746239645.123,
        rs_corrections=2,
        airlink=AirLinkHeader(version=0, reserved=0, length=26),
        mant=MantHeader(
            version=0, proto_id=0, source_addr=775, port=0,
            hop_limit=0, payload_length=26, header_bytes=6,
            tssr=True, add_path=True,
        ),
        raw_payload=bytes.fromhex('0600001a03070111f924744d01150a11000834415cf7'),
        application_payload=bytes.fromhex('0111f924744d01150a11000834415cf7'),
        framesync_hd=0,
        decode_path='matched-filter',
    )


def test_format_packet_human():
    pkt = make_test_packet()
    s = format_packet_human(pkt)
    assert '[FEC0]' in s
    assert 'src=775' in s
    assert 'len=26' in s
    assert 'FS_hd=0' in s


def test_packet_to_dict():
    pkt = make_test_packet()
    d = packet_to_dict(pkt)
    assert d['mant_source'] == 775
    assert d['fec_mode'] == 0
    assert d['mant_payload_length'] == 26
    assert d['app_payload_hex'] == '0111f924744d01150a11000834415cf7'


def test_packet_to_json_dict():
    pkt = make_test_packet()
    d = packet_to_json_dict(pkt)
    assert d['mant']['source_addr'] == 775
    assert d['fec_mode'] == 0
    # Should be JSON-serializable
    json_str = json.dumps(d)
    assert 'source_addr' in json_str


def test_log_file_output():
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.log')
        out = LogFileOutput(path)
        out.emit(pkt)
        out.emit(pkt)
        out.close()
        assert os.path.exists(path)
        with open(path) as f:
            lines = f.readlines()
        # Expected: SESSION_START + 2 packets + SESSION_END
        assert len(lines) == 4
        assert lines[0].startswith('# SESSION_START at ')
        assert lines[-1].startswith('# SESSION_END at ')
        # Packet lines
        for line in lines[1:3]:
            assert 'src=775' in line
            assert '[FEC0]' in line


def test_log_file_session_markers_appended():
    """Multiple open/close cycles append new SESSION_START/END pairs
    without duplicating the (currently nonexistent) header."""
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.log')
        # Session 1
        out = LogFileOutput(path)
        out.emit(pkt)
        out.close()
        # Session 2 - same file, append
        out = LogFileOutput(path)
        out.emit(pkt)
        out.close()
        with open(path) as f:
            content = f.read()
        starts = content.count('# SESSION_START')
        ends = content.count('# SESSION_END')
        assert starts == 2
        assert ends == 2


def test_diag_output():
    """DiagOutput writes per-packet diagnostic info."""
    from alert2.output import DiagOutput
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.diag')
        out = DiagOutput(path)
        out.emit(pkt)
        out.close()
        assert os.path.exists(path)
        content = open(path).read()
        # Header
        assert 'ALERT2 decoder diagnostic log' in content
        # Session markers (rc6+)
        assert '# SESSION_START at ' in content
        assert '# SESSION_END at ' in content
        # Source line
        assert 'src=775' in content
        # RF metrics
        # (test packet has no SNR set, so we see SNR=?)
        # Byte counts
        assert 'airlink_len=' in content
        assert 'mant_payload=' in content
        # Hex payloads
        assert 'mant_pdu_hex=' in content
        assert 'app_payload_hex=' in content


def test_diag_session_markers_include_version():
    """SESSION_START marker should include the alert2 version."""
    from alert2.output import DiagOutput
    from alert2 import __version__
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.diag')
        out = DiagOutput(path)
        out.close()
        content = open(path).read()
        # Version appears in SESSION_START line for downstream
        # analysis to know which decoder version produced this log
        assert __version__ in content
        # ISO 8601 timestamp format (YYYY-MM-DD with T separator)
        import re
        start_match = re.search(
            r'# SESSION_START at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\+\-]\d{2}:\d{2})',
            content)
        assert start_match, "SESSION_START should have ISO 8601 timestamp"


def test_csv_output():
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.csv')
        out = CsvOutput(path)
        out.emit(pkt)
        out.emit(pkt)
        out.close()
        assert os.path.exists(path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        for row in rows:
            assert row['mant_source'] == '775'
            assert row['fec_mode'] == '0'


def test_csv_append_no_double_header():
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.csv')
        # First session
        out = CsvOutput(path)
        out.emit(pkt)
        out.close()
        # Second session — should append without writing another header
        out = CsvOutput(path)
        out.emit(pkt)
        out.close()
        with open(path) as f:
            lines = f.readlines()
        # Header line + 2 data lines
        assert len(lines) == 3, f"Expected 3 lines, got {len(lines)}"
        assert lines[0].startswith('rx_time_iso')


def test_jsonl_output():
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.jsonl')
        out = JsonlOutput(path)
        out.emit(pkt)
        out.emit(pkt)
        out.close()
        assert os.path.exists(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert obj['mant']['source_addr'] == 775


def test_udp_emit_output():
    """UdpEmitOutput sends each packet as a JSON datagram."""
    import socket
    from alert2.output import UdpEmitOutput, packet_to_forwarder_dict
    pkt = make_test_packet()

    # Bind a UDP socket to receive
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    server.settimeout(2.0)

    # Send via UdpEmitOutput
    out = UdpEmitOutput('127.0.0.1', port)
    out.emit(pkt)

    # Receive and verify
    data, _ = server.recvfrom(65536)
    obj = json.loads(data)
    assert obj['src'] == 775
    assert obj['fec_mode'] == 0
    assert 'rx_time_unix' in obj
    assert 'raw_payload_hex' in obj
    assert 'application_payload_hex' in obj
    # Check that it's flat (no nested 'mant' object like jsonl)
    assert 'mant' not in obj

    out.close()
    server.close()


def test_packet_to_forwarder_dict_format():
    """The forwarder dict has flat structure expected by alert2_forwarder."""
    from alert2.output import packet_to_forwarder_dict
    pkt = make_test_packet()
    d = packet_to_forwarder_dict(pkt)
    # Required fields for ASCII forwarder
    required = ['rx_time_unix', 'fec_mode', 'snr_db', 'rs_corrections',
                 'framesync_hd', 'truncated', 'src', 'added_sas', 'port',
                 'hop_limit', 'mant_payload_length', 'raw_payload_hex',
                 'application_payload_hex', 'airlink_length']
    for field in required:
        assert field in d, f"Missing field: {field}"
    # All hex strings should be lowercase, no spaces
    assert d['raw_payload_hex'] == d['raw_payload_hex'].lower()
    assert ' ' not in d['raw_payload_hex']


def test_stats_collector():
    stats = StatsCollector()
    pkt = make_test_packet()
    stats.emit(pkt)
    stats.emit(pkt)
    stats.note_attempt(0, 0, True)
    stats.note_attempt(0, 5, False)
    stats.note_attempt(0, 0, True)
    s = stats.get_summary()
    assert s['packets_decoded'] == 2
    assert s['packets_by_fec_mode'] == {0: 2}
    assert s['unique_source_addresses'] == 1
    assert s['framesync_attempts'] == 3
    assert s['framesync_successes'] == 2
    assert abs(s['framesync_success_rate'] - 2/3) < 0.001


def test_noise_floor_trace_30min():
    """30-min noise floor trace buckets packets by rx_time and reports
    median SNR per bucket.

    SNR_BUCKET_SECONDS = 1800 (30 min), so buckets are:
        [0, 1799], [1800, 3599], [3600, 5399], ...
    """
    from alert2.types import Alert2Packet, AirLinkHeader, MantHeader

    def make_snr_packet(rx_time, snr):
        return Alert2Packet(
            fec_mode=0, rx_time=rx_time, rs_corrections=0,
            airlink=AirLinkHeader(version=0, length=26),
            mant=MantHeader(version=0, source_addr=775, payload_length=26,
                             hop_limit=0, port=0, add_path=True,
                             num_added_sas=0, header_bytes=6),
            raw_payload=b'\x00' * 26,
            application_payload=b'\x00' * 26,
            framesync_hd=0, snr_db=snr,
        )

    stats = StatsCollector()
    # Bucket 0 (start=0):    three packets at 12, 13, 14 dB → median 13
    stats.emit(make_snr_packet(100,  12.0))
    stats.emit(make_snr_packet(900,  13.0))
    stats.emit(make_snr_packet(1700, 14.0))
    # Bucket 3600 (start=3600): one packet at 8 dB (low — noise event!)
    stats.emit(make_snr_packet(3700, 8.0))

    trace = stats.get_noise_floor_trace()
    assert len(trace) == 2
    assert trace[0]['bucket_start_unix'] == 0
    assert trace[0]['packet_count'] == 3
    assert trace[0]['snr_median_db'] == 13.0
    assert trace[1]['bucket_start_unix'] == 3600
    assert trace[1]['packet_count'] == 1
    assert trace[1]['snr_median_db'] == 8.0
    # And it should be in get_summary
    s = stats.get_summary()
    assert 'noise_floor_trace_30min' in s
    assert len(s['noise_floor_trace_30min']) == 2


def test_multi_output():
    pkt = make_test_packet()
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, 'a.log')
        csv_path = os.path.join(tmpdir, 'a.csv')
        stats = StatsCollector()
        multi = MultiOutput([
            LogFileOutput(log_path),
            CsvOutput(csv_path),
            stats,
        ])
        multi.emit(pkt)
        multi.close()
        assert os.path.exists(log_path)
        assert os.path.exists(csv_path)
        assert stats.packet_count == 1


def test_multi_output_one_failure_does_not_break_others():
    """If one output throws, others should still receive packets."""
    pkt = make_test_packet()

    class BrokenOutput:
        def emit(self, p): raise RuntimeError("boom")
        def note_attempt(self, *a, **kw): raise RuntimeError("boom")
        def note_burst(self, *a, **kw): raise RuntimeError("boom")
        def close(self): raise RuntimeError("boom")

    stats = StatsCollector()
    multi = MultiOutput([BrokenOutput(), stats])
    multi.emit(pkt)  # Should not raise
    multi.close()    # Should not raise
    assert stats.packet_count == 1


def test_jsonl_to_stdout():
    """Test that jsonl path '-' goes to stdout."""
    import io
    from contextlib import redirect_stdout
    pkt = make_test_packet()
    out = JsonlOutput('-')
    buf = io.StringIO()
    # Replace the file handle for the test
    old_fh = out._fh
    out._fh = buf
    out.emit(pkt)
    out._fh = old_fh
    output = buf.getvalue()
    obj = json.loads(output.strip())
    assert obj['mant']['source_addr'] == 775


if __name__ == '__main__':
    print("Running output tests...")
    test_format_packet_human(); print("  ✓ format_packet_human")
    test_packet_to_dict(); print("  ✓ packet_to_dict")
    test_packet_to_json_dict(); print("  ✓ packet_to_json_dict")
    test_log_file_output(); print("  ✓ LogFileOutput")
    test_csv_output(); print("  ✓ CsvOutput")
    test_csv_append_no_double_header(); print("  ✓ CSV append doesn't duplicate header")
    test_jsonl_output(); print("  ✓ JsonlOutput")
    test_stats_collector(); print("  ✓ StatsCollector")
    test_multi_output(); print("  ✓ MultiOutput")
    test_multi_output_one_failure_does_not_break_others(); print("  ✓ MultiOutput error isolation")
    test_jsonl_to_stdout(); print("  ✓ JsonlOutput to stdout")
    print("All tests passed!")
