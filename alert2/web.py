# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Web UI for the ALERT2 decoder.

Provides a lightweight, read-only browser interface for monitoring decoded
packets in real time. Activated with the --web-ui CLI flag.

Architecture:
- WebOutput is a PacketOutput handler that pushes each emitted packet into
  a thread-safe ring buffer.
- An HTTP server runs in a background thread, serving:
    /              - main HTML page (packet list + detail panel)
    /source/N      - per-source detail page with SNR mini-chart
    /api/packets   - JSON list of recent packets (with ?since=N pagination)
    /api/stats     - JSON of current StatsCollector summary
    /api/sources   - JSON of per-source aggregates
    /api/source/N  - JSON of one source's full data
    /export/csv    - download CSV of current buffer
    /static/*      - static assets (HTML, CSS, JS embedded in templates)

No external dependencies beyond Python stdlib. No write endpoints — the
web UI is purely read-only and cannot affect the running decoder.
"""
import json
import math
import threading
import time
import datetime
import csv
import io
import os
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Dict, Any

from .types import Alert2Packet
from .output import PacketOutput, packet_to_json_dict, StatsCollector


_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN, +/-Infinity) with None.

    json.dumps emits these as the bare tokens `NaN`, `Infinity`,
    `-Infinity` — accepted by Python's json but rejected by the
    browser's JSON.parse (and by the JSON spec). A single non-finite
    value anywhere in a response makes the whole response unparseable
    client-side. Sensor values decoded from corrupted/erasure bytes can
    encode IEEE NaN/Inf; this guarantees we never serialize one.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _read_template(name: str) -> str:
    """Read a template file from the templates/ directory.

    Templates contain UTF-8 punctuation (· U+00B7 separators, — em
    dashes). MUST specify encoding='utf-8' explicitly: Python's open()
    otherwise uses the platform default, which on the deployment Pi is
    ISO-8859-1 (the system locale). That misreads each UTF-8 byte pair
    as two Latin-1 chars, so "·" renders as "Â·" and "—" as "â€"" in
    the web UI (mojibake). This is locale-independent now.
    """
    path = os.path.join(_TEMPLATES_DIR, name)
    with open(path, encoding='utf-8') as f:
        return f.read()


def _packet_to_web_dict(pkt: Alert2Packet, seq: int) -> Dict[str, Any]:
    """Convert a packet to the dict structure used by the web API.

    This is a flatter, more web-friendly version of packet_to_json_dict —
    avoids deep nesting and includes a sequence number for client-side
    pagination.
    """
    rx_iso = datetime.datetime.fromtimestamp(pkt.rx_time).isoformat()
    d = {
        'seq': seq,
        'rx_time_iso': rx_iso,
        'rx_time_unix': pkt.rx_time,
        'fec_mode': pkt.fec_mode,
        'framesync_hd': pkt.framesync_hd,
        'rs_corrections': pkt.rs_corrections,
        'snr_db': pkt.snr_db,
        'mer_db': pkt.mer_db,
        'mer_evm_pct': pkt.mer_evm_pct,
        'mer_symbols': pkt.mer_symbols,
        'mer_coverage': pkt.mer_coverage,
        # Demod-chain diagnostics — rendered in the packet detail panel
        # only, deliberately not in the row summary or per-source averages.
        'mer_eq_db': pkt.mer_eq_db,
        'mer_eq_gain_db': pkt.mer_eq_gain_db,
        'mer_alpha': pkt.mer_alpha,
        'truncated': pkt.truncated,
        'eq_recovered': getattr(pkt, 'eq_recovered', False),
        'eq_stage': getattr(pkt, 'eq_stage', None),
        'burst_scoped': getattr(pkt, 'burst_scoped', False),
        'presync_raw_hd': getattr(pkt, 'presync_raw_hd', None),
        'rs_block_corr': getattr(pkt, 'rs_block_corr', None),
        'presync_found': getattr(pkt, 'presync_found', False),
        'decode_path': pkt.decode_path,
        'src': None,
        'dest': None,
        'path': [],
        'port': None,
        'hop': None,
        'mant_payload_length': None,
        'app_pdu_id': None,
        'app_test_flag': None,
        'srp_ts': None,
        'srp_summary': None,
        'sensors': [],
        'reports': [],
        'app_payload_hex': pkt.application_payload.hex() if pkt.application_payload else None,
    }
    # Signal-quality components (SNR is derived from these; export both so
    # the CSV and per-source page carry the full picture, not just the ratio).
    d['noise_rms'] = pkt.noise_rms
    d['signal_rms'] = pkt.signal_rms
    d['raw_pdu_hex'] = pkt.raw_payload.hex() if pkt.raw_payload else None
    d['airlink_version'] = None
    if pkt.airlink:
        d['airlink_version'] = pkt.airlink.version
        d['airlink_length'] = pkt.airlink.length
    d['frame_pdu_count'] = pkt.frame_pdu_count
    d['frame_index'] = pkt.frame_index
    d['frame_sources'] = list(pkt.frame_sources) if pkt.frame_sources else None
    if pkt.mant:
        m = pkt.mant
        d['src'] = m.source_addr
        d['dest'] = m.dest_addr
        d['path'] = list(m.added_sas) if m.added_sas else []
        d['port'] = m.port
        d['hop'] = m.hop_limit
        d['mant_payload_length'] = m.payload_length
        d['mant_tssr'] = bool(m.tssr)
        d['mant_add_path'] = bool(m.add_path)
        # Full MANT header set (previously omitted from the web dict, which
        # left these columns blank in the CSV export).
        d['mant_version'] = m.version
        d['mant_proto_id'] = m.proto_id
        d['mant_da_present'] = bool(m.da_present)
        d['mant_encrypted'] = bool(m.encrypted)
        d['mant_ack'] = bool(m.ack)
        d['mant_added_header'] = bool(m.added_header)
        d['mant_header_bytes'] = m.header_bytes
    if pkt.srp_pdu:
        d['app_pdu_id'] = pkt.srp_pdu.pdu_id
        d['app_test_flag'] = bool(pkt.srp_pdu.test_flag)
        d['srp_ts'] = pkt.srp_pdu.timestamp
        if pkt.srp_pdu.reports:
            d['srp_summary'] = str(pkt.srp_pdu)
            # Flatten readings into a list for easy table display
            for rpt in pkt.srp_pdu.reports:
                rpt_data = {'type': rpt.__class__.__name__,
                            'description': str(rpt)}
                d['reports'].append(rpt_data)
                if hasattr(rpt, 'readings'):
                    for r in rpt.readings:
                        d['sensors'].append({
                            'sensor_id': r.sensor_id,
                            'sensor_name': r.sensor_name,
                            'value': r.value,
                            'value_type': r.value_type,
                        })
    return d


class WebOutput(PacketOutput):
    """Output handler that buffers packets for the web UI to query."""

    def __init__(self, buffer_size: int = 500,
                 stats: Optional[StatsCollector] = None,
                 max_tracked_sources: int = 500):
        self.buffer_size = buffer_size
        self.max_tracked_sources = max_tracked_sources
        self._buffer: deque = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        self._next_seq = 1
        self.stats = stats
        # Track per-source recent SNR history for mini-chart
        # Capped at 200 most-recent entries per source.
        # Number of unique sources also capped (LRU eviction) so a
        # misbehaving signal flood can't grow this dict without bound.
        self._snr_history: Dict[int, deque] = {}
        # Last-seen wall time per source — used for LRU eviction
        self._snr_last_seen: Dict[int, float] = {}
        self._eviction_warned = False

    def emit(self, pkt: Alert2Packet) -> None:
        with self._lock:
            d = _packet_to_web_dict(pkt, self._next_seq)
            self._buffer.append(d)
            self._next_seq += 1
            # SNR history per source
            if pkt.mant and (pkt.snr_db is not None or pkt.mer_db is not None):
                src = pkt.mant.source_addr
                # If we'd exceed the cap, evict the least-recently-seen source
                if src not in self._snr_history and len(self._snr_history) >= self.max_tracked_sources:
                    # Find oldest by last-seen time
                    oldest_src = min(self._snr_last_seen,
                                     key=self._snr_last_seen.get)
                    del self._snr_history[oldest_src]
                    del self._snr_last_seen[oldest_src]
                    if not self._eviction_warned:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"WebOutput: tracked-source cap "
                            f"({self.max_tracked_sources}) reached; "
                            f"evicting oldest. First eviction: src={oldest_src}")
                        self._eviction_warned = True
                if src not in self._snr_history:
                    self._snr_history[src] = deque(maxlen=200)
                self._snr_history[src].append({
                    'rx_time_unix': pkt.rx_time,
                    'snr_db': pkt.snr_db,
                    'mer_db': pkt.mer_db,
                    'mer_eq_db': pkt.mer_eq_db,
                    'mer_alpha': pkt.mer_alpha,
                    'rs_corrections': pkt.rs_corrections,
                    'truncated': pkt.truncated,
                })
                self._snr_last_seen[src] = pkt.rx_time

    def note_attempt(self, fec_mode: int, fs_hd: int, succeeded: bool) -> None:
        # No-op — stats is already tracking attempts via its own emit path
        pass

    def close(self) -> None:
        # No flushing needed; HTTP server is shut down separately
        pass

    def get_packets_since(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [p for p in self._buffer if p['seq'] > since_seq]

    def get_all_packets(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def get_source_packets(self, src: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [p for p in self._buffer if p.get('src') == src]

    def get_source_history(self, src: int) -> List[Dict[str, Any]]:
        with self._lock:
            hist = self._snr_history.get(src, deque())
            return list(hist)

    def get_sources_summary(self) -> List[Dict[str, Any]]:
        """Per-source aggregates."""
        with self._lock:
            buckets: Dict[int, Dict[str, Any]] = {}
            for p in self._buffer:
                src = p.get('src')
                if src is None:
                    continue
                b = buckets.setdefault(src, {
                    'src': src, 'count': 0,
                    'snr_sum': 0.0, 'snr_count': 0,
                    'snr_min': None, 'snr_max': None,
                    'mer_sum': 0.0, 'mer_count': 0,
                    'mer_min': None, 'mer_max': None,
                    'mer_eq_sum': 0.0, 'mer_eq_count': 0,
                    'alpha_sum': 0.0, 'alpha_count': 0,
                    'last_rx_iso': None, 'last_seq': 0,
                    'truncated_count': 0,
                    'rs_corr_count': 0,
                    'last_pdu_id': None,
                })
                b['count'] += 1
                if p.get('snr_db') is not None:
                    s = p['snr_db']
                    b['snr_sum'] += s
                    b['snr_count'] += 1
                    if b['snr_min'] is None or s < b['snr_min']:
                        b['snr_min'] = s
                    if b['snr_max'] is None or s > b['snr_max']:
                        b['snr_max'] = s
                if p.get('mer_db') is not None:
                    m = p['mer_db']
                    b['mer_sum'] += m
                    b['mer_count'] += 1
                    if b['mer_min'] is None or m < b['mer_min']:
                        b['mer_min'] = m
                    if b['mer_max'] is None or m > b['mer_max']:
                        b['mer_max'] = m
                if p.get('mer_eq_db') is not None:
                    b['mer_eq_sum'] += p['mer_eq_db']
                    b['mer_eq_count'] += 1
                if p.get('mer_alpha') is not None:
                    b['alpha_sum'] += p['mer_alpha']
                    b['alpha_count'] += 1
                if p.get('truncated'):
                    b['truncated_count'] += 1
                if p.get('rs_corrections', 0) > 0:
                    b['rs_corr_count'] += 1
                if p['seq'] > b['last_seq']:
                    b['last_seq'] = p['seq']
                    b['last_rx_iso'] = p['rx_time_iso']
                    b['last_pdu_id'] = p.get('app_pdu_id')
            # Compute averages, sort by last seen (most recent first)
            result = []
            for b in buckets.values():
                if b['snr_count']:
                    b['snr_avg'] = round(b['snr_sum'] / b['snr_count'], 1)
                else:
                    b['snr_avg'] = None
                if b['mer_count']:
                    b['mer_avg'] = round(b['mer_sum'] / b['mer_count'], 1)
                else:
                    b['mer_avg'] = None
                if b['mer_eq_count']:
                    b['mer_eq_avg'] = round(
                        b['mer_eq_sum'] / b['mer_eq_count'], 1)
                else:
                    b['mer_eq_avg'] = None
                if b['alpha_count']:
                    b['alpha_avg'] = round(
                        b['alpha_sum'] / b['alpha_count'], 4)
                else:
                    b['alpha_avg'] = None
                del b['snr_sum'], b['snr_count']
                del b['mer_sum'], b['mer_count']
                del b['mer_eq_sum'], b['mer_eq_count']
                del b['alpha_sum'], b['alpha_count']
                result.append(b)
            result.sort(key=lambda x: -x['last_seq'])
            return result


class _WebUIHandler(BaseHTTPRequestHandler):
    """HTTP handler for the web UI. Configured via class attributes
    (set on the subclass by start_web_server)."""

    web_output: Optional[WebOutput] = None
    stats: Optional[StatsCollector] = None
    decoder_start_time: float = 0
    poll_interval_sec: int = 5

    def log_message(self, format, *args):
        # Suppress default request logging — too noisy.
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        # allow_nan=False makes json.dumps raise ValueError on any NaN or
        # Infinity rather than emit the literal tokens `NaN`/`Infinity`,
        # which are valid Python json output but INVALID JSON that the
        # browser's JSON.parse rejects. A single non-finite float (e.g. a
        # sensor value decoded from corrupted bytes, or an SNR edge case)
        # anywhere in the payload would otherwise poison the entire
        # response — and, since such a packet lives in the ring buffer,
        # every subsequent /api/packets response too, until it ages out.
        # That presents as "packet feed silently freezes after running a
        # while" (the client's await resp.json() throws and is swallowed)
        # while /api/stats and CSV export keep working. We pre-sanitize
        # non-finite floats to null so the output is always valid JSON.
        body = json.dumps(_json_safe(data), default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = 'text/plain') -> None:
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self) -> None:
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Not found')

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except BrokenPipeError:
            # Client closed connection — happens normally
            pass
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(f'Internal error: {e}'.encode())
            except Exception:
                pass

    def _handle_get(self) -> None:
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)

        if path == '/':
            html = _read_template('index.html')
            html = html.replace('__POLL_INTERVAL__', str(self.poll_interval_sec * 1000))
            self._send_html(html)
            return

        if path.startswith('/source/'):
            try:
                src = int(path.split('/')[-1])
            except ValueError:
                self._send_404()
                return
            html = _read_template('source.html')
            html = html.replace('__SOURCE_ID__', str(src))
            html = html.replace('__POLL_INTERVAL__', str(self.poll_interval_sec * 1000))
            self._send_html(html)
            return

        if path == '/api/packets':
            since = int(query.get('since', ['0'])[0])
            packets = self.web_output.get_packets_since(since)
            # Newest first for display
            packets = list(reversed(packets))
            self._send_json({
                'packets': packets,
                'highest_seq': max((p['seq'] for p in packets), default=since),
            })
            return

        if path == '/api/stats':
            data = {
                'decoder_uptime_sec': time.time() - self.decoder_start_time,
                'now_iso': datetime.datetime.now().isoformat(),
            }
            if self.stats:
                data.update(self.stats.get_summary())
            self._send_json(data)
            return

        if path == '/api/sources':
            self._send_json({'sources': self.web_output.get_sources_summary()})
            return

        if path.startswith('/api/source/'):
            try:
                src = int(path.split('/')[-1])
            except ValueError:
                self._send_404()
                return
            packets = self.web_output.get_source_packets(src)
            history = self.web_output.get_source_history(src)
            # Compute per-source aggregate
            summary = None
            for s in self.web_output.get_sources_summary():
                if s['src'] == src:
                    summary = s
                    break
            self._send_json({
                'src': src, 'packets': list(reversed(packets)),
                'history': history, 'summary': summary,
            })
            return

        if path == '/export/csv':
            buf = io.StringIO()
            from .output import CSV_FIELDS, packet_to_dict
            writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS,
                                     extrasaction='ignore')
            writer.writeheader()
            # The web buffer holds web dicts; these now carry the full field
            # set (MANT header, RMS components, raw PDU hex), so the export
            # is complete rather than the reduced subset it used to be.
            # The web buffer holds web dicts (not full Alert2Packet objects).
            # The web dict now carries the full field set, so the export is
            # complete. Map web-dict keys to the CSV column names.
            def _b(v):
                # bool/None -> CSV-friendly: '' for None, 1/0 for bool
                if v is None:
                    return ''
                if isinstance(v, bool):
                    return 1 if v else 0
                return v
            for p in self.web_output.get_all_packets():
                row = {
                    'rx_time_iso': p.get('rx_time_iso', ''),
                    'rx_time_unix': p.get('rx_time_unix', ''),
                    'fec_mode': p.get('fec_mode', ''),
                    'framesync_hd': p.get('framesync_hd', '') or '',
                    'rs_corrections': p.get('rs_corrections', '') or 0,
                    'airlink_version': _b(p.get('airlink_version')),
                    'airlink_length': _b(p.get('airlink_length')),
                    'mant_version': _b(p.get('mant_version')),
                    'mant_proto_id': _b(p.get('mant_proto_id')),
                    'mant_source': p.get('src', '') or '',
                    'mant_dest': _b(p.get('dest')),
                    'mant_added_sas': ';'.join(str(x) for x in p.get('path', [])),
                    'mant_port': _b(p.get('port')),
                    'mant_hop_limit': _b(p.get('hop')),
                    'mant_payload_length': p.get('mant_payload_length', '') or '',
                    'mant_tssr': _b(p.get('mant_tssr')),
                    'mant_add_path': _b(p.get('mant_add_path')),
                    'mant_da_present': _b(p.get('mant_da_present')),
                    'mant_encrypted': _b(p.get('mant_encrypted')),
                    'mant_ack': _b(p.get('mant_ack')),
                    'mant_added_header': _b(p.get('mant_added_header')),
                    'mant_header_bytes': _b(p.get('mant_header_bytes')),
                    'app_pdu_id': p.get('app_pdu_id', '') or '',
                    'app_test_flag': '1' if p.get('app_test_flag') else '0',
                    'truncated': '1' if p.get('truncated') else '0',
                    'srp_summary': p.get('srp_summary', '') or '',
                    'snr_db': f"{p['snr_db']:.2f}" if p.get('snr_db') is not None else '',
                    'noise_rms': f"{p['noise_rms']:.6f}" if p.get('noise_rms') is not None else '',
                    'signal_rms': f"{p['signal_rms']:.6f}" if p.get('signal_rms') is not None else '',
                    'app_payload_hex': p.get('app_payload_hex', '') or '',
                    'raw_pdu_hex': p.get('raw_pdu_hex', '') or '',
                    'decode_path': p.get('decode_path', '') or '',
                }
                writer.writerow(row)
            csv_text = buf.getvalue()
            now = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header(
                'Content-Disposition',
                f'attachment; filename="alert2_export_{now}.csv"')
            self.send_header('Content-Length', str(len(csv_text.encode())))
            self.end_headers()
            self.wfile.write(csv_text.encode())
            return

        self._send_404()


def start_web_server(web_output: WebOutput,
                     stats: Optional[StatsCollector],
                     bind: str = '127.0.0.1',
                     port: int = 8080,
                     poll_interval_sec: int = 5) -> ThreadingHTTPServer:
    """Start the web UI HTTP server in a background thread.

    Returns the server object so the caller can stop it on shutdown.
    """
    # Build a per-instance subclass so handler attributes are isolated
    handler_class = type('_WebUIHandlerInstance', (_WebUIHandler,), {
        'web_output': web_output,
        'stats': stats,
        'decoder_start_time': time.time(),
        'poll_interval_sec': poll_interval_sec,
    })
    server = ThreadingHTTPServer((bind, port), handler_class)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name='alert2-web-ui')
    thread.start()
    return server
