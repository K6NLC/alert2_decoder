# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for the alert2.web module.

Tests the WebOutput ring buffer behavior, packet-to-dict conversion,
and the HTTP API endpoints (using a real local server).
"""
import os
import sys
import json
import time
import threading
import urllib.request
import urllib.error
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from alert2.types import (Alert2Packet, AirLinkHeader, MantHeader)
from alert2.web import WebOutput, _packet_to_web_dict, start_web_server
from alert2.output import StatsCollector


def make_packet(src=769, ts=5181, snr=12.5, rs_corr=0, fs_hd=0,
                truncated=False, rx_time=None):
    """Build a minimal Alert2Packet for testing."""
    if rx_time is None:
        rx_time = time.time()
    airlink = AirLinkHeader(version=0, length=39)
    mant = MantHeader(version=0, source_addr=src, payload_length=29,
                       hop_limit=0, port=0, add_path=True,
                       num_added_sas=1, added_sas=[4601],
                       header_bytes=9)
    return Alert2Packet(
        fec_mode=0, rx_time=rx_time, rs_corrections=rs_corr,
        airlink=airlink, mant=mant,
        raw_payload=b'\x00' * 38,
        application_payload=b'\x00' * 29,
        srp_pdu=None,
        framesync_hd=fs_hd,
        sample_offset=0,
        decode_path='matched_filter',
        snr_db=snr,
        truncated=truncated,
    )


class TestPacketToWebDict(unittest.TestCase):

    def test_basic_fields_present(self):
        pkt = make_packet(src=720, ts=1234, snr=8.5)
        d = _packet_to_web_dict(pkt, seq=42)
        self.assertEqual(d['seq'], 42)
        self.assertEqual(d['src'], 720)
        self.assertEqual(d['fec_mode'], 0)
        self.assertEqual(d['snr_db'], 8.5)
        self.assertEqual(d['hop'], 0)
        self.assertEqual(d['port'], 0)
        self.assertFalse(d['truncated'])
        self.assertEqual(d['path'], [4601])

    def test_truncated_packet(self):
        pkt = make_packet(truncated=True)
        d = _packet_to_web_dict(pkt, seq=1)
        self.assertTrue(d['truncated'])

    def test_no_mant_no_crash(self):
        # Edge case: packet without MANT header (shouldn't really happen,
        # but the converter should handle it).
        pkt = Alert2Packet(fec_mode=0, rx_time=time.time())
        d = _packet_to_web_dict(pkt, seq=1)
        self.assertIsNone(d['src'])

    def test_iso_timestamp(self):
        pkt = make_packet()
        d = _packet_to_web_dict(pkt, seq=1)
        # Should be parseable as ISO
        from datetime import datetime
        datetime.fromisoformat(d['rx_time_iso'])


class TestWebOutputBuffer(unittest.TestCase):

    def test_emit_increments_seq(self):
        wo = WebOutput(buffer_size=10)
        wo.emit(make_packet())
        wo.emit(make_packet())
        wo.emit(make_packet())
        all_pkts = wo.get_all_packets()
        self.assertEqual(len(all_pkts), 3)
        self.assertEqual([p['seq'] for p in all_pkts], [1, 2, 3])

    def test_buffer_size_cap(self):
        wo = WebOutput(buffer_size=5)
        for _ in range(20):
            wo.emit(make_packet())
        all_pkts = wo.get_all_packets()
        # Should keep only the 5 most recent
        self.assertEqual(len(all_pkts), 5)
        # Sequence numbers should be 16, 17, 18, 19, 20
        self.assertEqual([p['seq'] for p in all_pkts], [16, 17, 18, 19, 20])

    def test_get_packets_since(self):
        wo = WebOutput(buffer_size=20)
        for _ in range(10):
            wo.emit(make_packet())
        recent = wo.get_packets_since(since_seq=7)
        # Packets with seq > 7 means seq=8, 9, 10
        self.assertEqual(len(recent), 3)
        self.assertEqual([p['seq'] for p in recent], [8, 9, 10])

    def test_get_source_packets_filters(self):
        wo = WebOutput(buffer_size=20)
        wo.emit(make_packet(src=769))
        wo.emit(make_packet(src=720))
        wo.emit(make_packet(src=769))
        wo.emit(make_packet(src=716))
        s769 = wo.get_source_packets(769)
        self.assertEqual(len(s769), 2)
        for p in s769:
            self.assertEqual(p['src'], 769)
        s720 = wo.get_source_packets(720)
        self.assertEqual(len(s720), 1)
        self.assertEqual(wo.get_source_packets(999), [])

    def test_snr_history_per_source(self):
        wo = WebOutput(buffer_size=20)
        wo.emit(make_packet(src=769, snr=10.0))
        wo.emit(make_packet(src=769, snr=11.0))
        wo.emit(make_packet(src=720, snr=5.0))
        h_769 = wo.get_source_history(769)
        h_720 = wo.get_source_history(720)
        self.assertEqual(len(h_769), 2)
        self.assertEqual(len(h_720), 1)
        self.assertEqual([h['snr_db'] for h in h_769], [10.0, 11.0])

    def test_snr_history_capped(self):
        wo = WebOutput(buffer_size=10000)
        for i in range(300):
            wo.emit(make_packet(src=769, snr=float(i)))
        # SNR history is capped at 200 per source
        h = wo.get_source_history(769)
        self.assertEqual(len(h), 200)
        # Should be the most recent 200 (snr 100 to 299)
        self.assertEqual(h[0]['snr_db'], 100.0)
        self.assertEqual(h[-1]['snr_db'], 299.0)

    def test_max_tracked_sources_lru_eviction(self):
        """When max_tracked_sources is exceeded, the least-recently-seen
        source should be evicted from _snr_history."""
        wo = WebOutput(buffer_size=10000, max_tracked_sources=5)
        # Emit from 5 different sources at staggered times
        for i, src in enumerate([701, 702, 703, 704, 705]):
            wo.emit(make_packet(src=src, snr=10.0, rx_time=1000.0 + i))
        self.assertEqual(len(wo._snr_history), 5)
        # Emit a 6th source — oldest (701) should be evicted
        wo.emit(make_packet(src=706, snr=10.0, rx_time=1010.0))
        self.assertEqual(len(wo._snr_history), 5)
        self.assertNotIn(701, wo._snr_history)
        self.assertIn(706, wo._snr_history)
        # Now emit again from 702 — refreshes its last-seen
        wo.emit(make_packet(src=702, snr=10.0, rx_time=1020.0))
        # Emit yet another new source — 703 should now be the oldest
        wo.emit(make_packet(src=707, snr=10.0, rx_time=1030.0))
        self.assertEqual(len(wo._snr_history), 5)
        self.assertNotIn(703, wo._snr_history)
        self.assertIn(702, wo._snr_history)
        self.assertIn(707, wo._snr_history)

    def test_max_tracked_sources_default_is_500(self):
        wo = WebOutput()
        self.assertEqual(wo.max_tracked_sources, 500)

    def test_sources_summary_aggregates(self):
        wo = WebOutput(buffer_size=20)
        wo.emit(make_packet(src=769, snr=10.0, rs_corr=0))
        wo.emit(make_packet(src=769, snr=12.0, rs_corr=1))
        wo.emit(make_packet(src=720, snr=5.0, truncated=True))
        summary = wo.get_sources_summary()
        # Find each source
        s769 = next(s for s in summary if s['src'] == 769)
        s720 = next(s for s in summary if s['src'] == 720)
        self.assertEqual(s769['count'], 2)
        self.assertEqual(s769['snr_min'], 10.0)
        self.assertEqual(s769['snr_max'], 12.0)
        self.assertEqual(s769['snr_avg'], 11.0)
        self.assertEqual(s769['rs_corr_count'], 1)
        self.assertEqual(s769['truncated_count'], 0)
        self.assertEqual(s720['count'], 1)
        self.assertEqual(s720['truncated_count'], 1)


def _find_free_port():
    """Find an unused TCP port for testing."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestHttpEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.wo = WebOutput(buffer_size=100)
        cls.stats = StatsCollector()
        # Populate some packets
        for i in range(5):
            pkt = make_packet(src=769 + (i % 2), snr=10 + i)
            cls.wo.emit(pkt)
            cls.stats.emit(pkt)
        cls.port = _find_free_port()
        cls.server = start_web_server(cls.wo, cls.stats,
                                       bind='127.0.0.1', port=cls.port)
        # Give server a moment to bind
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        url = f'http://127.0.0.1:{self.port}{path}'
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return r.status, r.read().decode('utf-8')

    def _get_json(self, path):
        status, body = self._get(path)
        return status, json.loads(body)

    def test_root_serves_html(self):
        status, body = self._get('/')
        self.assertEqual(status, 200)
        self.assertIn('<!DOCTYPE html>', body)
        self.assertIn('ALERT2 Decoder', body)

    def test_source_page_serves_html(self):
        status, body = self._get('/source/769')
        self.assertEqual(status, 200)
        self.assertIn('Source 769', body)

    def test_api_packets_returns_buffer(self):
        status, data = self._get_json('/api/packets')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['packets']), 5)
        # Should be ordered newest-first
        self.assertGreater(data['packets'][0]['seq'],
                            data['packets'][-1]['seq'])

    def test_api_packets_pagination(self):
        # Get only packets after seq 3
        status, data = self._get_json('/api/packets?since=3')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['packets']), 2)
        for p in data['packets']:
            self.assertGreater(p['seq'], 3)

    def test_api_sources_returns_aggregates(self):
        status, data = self._get_json('/api/sources')
        self.assertEqual(status, 200)
        srcs = data['sources']
        # We added 3 packets from src=769, 2 from 770
        self.assertEqual(len(srcs), 2)
        s769 = next(s for s in srcs if s['src'] == 769)
        s770 = next(s for s in srcs if s['src'] == 770)
        self.assertEqual(s769['count'], 3)
        self.assertEqual(s770['count'], 2)

    def test_api_source_returns_history(self):
        status, data = self._get_json('/api/source/769')
        self.assertEqual(status, 200)
        self.assertEqual(data['src'], 769)
        self.assertEqual(len(data['packets']), 3)

    def test_api_source_unknown_id_returns_empty(self):
        status, data = self._get_json('/api/source/99999')
        self.assertEqual(status, 200)
        self.assertEqual(data['packets'], [])
        self.assertIsNone(data['summary'])

    def test_api_stats_includes_uptime(self):
        status, data = self._get_json('/api/stats')
        self.assertEqual(status, 200)
        self.assertIn('decoder_uptime_sec', data)
        self.assertGreater(data['decoder_uptime_sec'], 0)

    def test_export_csv(self):
        status, body = self._get('/export/csv')
        self.assertEqual(status, 200)
        # Should have a header row + 5 data rows
        lines = body.strip().split('\n')
        self.assertGreaterEqual(len(lines), 6)
        self.assertIn('mant_source', lines[0])

    def test_unknown_path_returns_404(self):
        try:
            self._get('/this-path-does-not-exist')
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_invalid_source_id_returns_404(self):
        try:
            self._get('/source/not-a-number')
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class TestJsonSafe(unittest.TestCase):
    """Non-finite floats must never reach the JSON output, or the
    browser's JSON.parse rejects the whole response and the packet
    feed silently freezes (stats/CSV unaffected). Regression test for
    the 'feed stops after running a while' bug."""

    def test_json_safe_replaces_non_finite(self):
        from alert2.web import _json_safe
        out = _json_safe({'a': float('nan'), 'b': float('inf'),
                          'c': float('-inf'), 'd': 7.5, 'e': 0})
        self.assertIsNone(out['a'])
        self.assertIsNone(out['b'])
        self.assertIsNone(out['c'])
        self.assertEqual(out['d'], 7.5)
        self.assertEqual(out['e'], 0)

    def test_json_safe_recurses_nested(self):
        from alert2.web import _json_safe
        out = _json_safe({'packets': [
            {'snr_db': float('nan'),
             'sensors': [{'value': float('inf')}, {'value': 3.3}]}]})
        self.assertIsNone(out['packets'][0]['snr_db'])
        self.assertIsNone(out['packets'][0]['sensors'][0]['value'])
        self.assertEqual(out['packets'][0]['sensors'][1]['value'], 3.3)

    def test_sanitized_output_is_valid_json(self):
        import json
        from alert2.web import _json_safe
        payload = {'packets': [{'snr_db': float('nan'),
                                'value': float('inf')}]}
        text = json.dumps(_json_safe(payload))
        # Strict JSON parse (rejects NaN/Infinity like the browser does)
        reparsed = json.loads(text, parse_constant=_reject_const)
        self.assertIsNone(reparsed['packets'][0]['snr_db'])


def _reject_const(c):
    raise ValueError(f"Non-finite constant {c} in JSON (browser would reject)")


class TestBundling(unittest.TestCase):
    """Multi-PDU frame bundling: when one AirLink frame carries several
    stacked MANT PDUs, each emitted packet should be stamped with the
    shared frame info so the UI can show 'bundled with src X, src Y'."""

    def test_bundling_fields_in_web_dict(self):
        # A bundled packet: frame of 3, this is index 1
        p = make_packet(src=771)
        p.frame_pdu_count = 3
        p.frame_index = 1
        p.frame_sources = [731, 771, 733]
        d = _packet_to_web_dict(p, 0)
        self.assertEqual(d['frame_pdu_count'], 3)
        self.assertEqual(d['frame_index'], 1)
        self.assertEqual(d['frame_sources'], [731, 771, 733])

    def test_unbundled_defaults(self):
        # A normal single-PDU packet defaults to count 1, no sources
        p = make_packet(src=769)
        d = _packet_to_web_dict(p, 0)
        self.assertEqual(d['frame_pdu_count'], 1)
        self.assertIsNone(d['frame_sources'])


if __name__ == '__main__':
    print("Running web UI tests...")
    unittest.main(verbosity=2)
