# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Output modules for decoded packets.

Provides several pluggable output handlers:
  - StdoutOutput:    Default human-readable output to terminal
  - LogFileOutput:   Append packets to a log file (rotating by date)
  - CsvOutput:       Structured CSV format
  - JsonlOutput:     One JSON object per line
  - StatsCollector:  Decoder statistics (counts, distributions)

Multiple outputs can be active at once. Each handler implements:
    emit(packet)       — called when a packet is decoded
    note_attempt()     — called when a framesync candidate is tried (for stats)
    note_burst()       — called when a burst region is detected (for stats)
    close()            — called at end of session, flushes anything pending
"""
import csv
import json
import socket
import sys
import time
import datetime
import os
import threading
from typing import Optional, TextIO, List, Dict
from collections import Counter, defaultdict
from .types import Alert2Packet


# =============================================================================
# Base class
# =============================================================================

class PacketOutput:
    """Base class for packet output handlers.

    Subclasses override emit() and optionally close().
    """

    def emit(self, pkt: Alert2Packet) -> None:
        """Called for each successfully decoded packet."""
        pass

    def note_stream_health(self, info: dict) -> None:
        """Periodic live-pipeline health report (rc22).

        info keys: elapsed_s, samples_read, samples_expected,
        deficit_samples, deficit_s, deficit_pct, rt_factor.
        Only called by the streaming path; file decodes never call it.
        """
        pass

    def note_attempt(self, fec_mode: int, fs_hd: int, succeeded: bool) -> None:
        """Called when a framesync candidate is tried (success or failure)."""
        pass

    def note_framesync_event(self, fec_mode: int, fs_hd: int,
                              decoded_pkts: list, fs_end_bit: int) -> None:
        """Called once per framesync attempt with the resulting packets.

        Unlike note_attempt (which only takes a boolean for stats),
        this passes the actual decoded packets (post-filter) so display
        outputs can show source IDs. Failed attempts get an empty list.
        Default no-op; FramesyncDisplayOutput and DiagOutput override.
        """
        pass

    def note_burst(self, start_sample: int, duration_samples: int) -> None:
        """Called when a burst region is detected by the demodulator."""
        pass

    def close(self) -> None:
        """Called at end of session — flush buffers, close files."""
        pass


# =============================================================================
# Stdout (current default behavior)
# =============================================================================

def format_packet_human(pkt: Alert2Packet, raw: bool = False) -> str:
    """Format a packet for human-readable output (terminal display)."""
    parts = [f"[FEC{pkt.fec_mode}]"]
    if pkt.mant:
        m = pkt.mant
        parts.append(f"src={m.source_addr}")
        if m.dest_addr is not None:
            parts.append(f"dst={m.dest_addr}")
        if m.added_sas:
            parts.append(f"path={m.added_sas}")
        parts.append(f"port={m.port}")
        parts.append(f"hop={m.hop_limit}")
        parts.append(f"len={m.payload_length}")
        if m.encrypted:
            parts.append("ENC")
        if m.ack:
            parts.append("ACK")
    if pkt.rs_corrections:
        parts.append(f"RS_corr={pkt.rs_corrections}")
    if pkt.framesync_hd is not None:
        parts.append(f"FS_hd={pkt.framesync_hd}")
    if pkt.snr_db is not None:
        parts.append(f"SNR={pkt.snr_db:.1f}dB")
    if pkt.mer_db is not None:
        parts.append(f"MER={pkt.mer_db:.1f}dB")
    if pkt.mer_eq_db is not None:
        parts.append(f"MER_eq={pkt.mer_eq_db:.1f}dB")
    if pkt.truncated:
        parts.append("[TRUNCATED]")

    # Show decoded SRP if available, otherwise raw payload
    if pkt.srp_pdu and pkt.srp_pdu.reports:
        parts.append(str(pkt.srp_pdu))
    elif raw and pkt.raw_payload:
        parts.append(f"raw={pkt.raw_payload.hex()}")
    elif pkt.application_payload:
        parts.append(f"payload={pkt.application_payload.hex()}")
    return " ".join(parts)


class StdoutOutput(PacketOutput):
    """Print packets to stdout in human-readable format."""

    def __init__(self, raw: bool = False, include_timestamp: bool = False):
        self.raw = raw
        self.include_timestamp = include_timestamp

    def emit(self, pkt: Alert2Packet) -> None:
        line = format_packet_human(pkt, raw=self.raw)
        if self.include_timestamp:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = f"[{ts}] {line}"
        print(line)
        sys.stdout.flush()


# =============================================================================
# PRIORITY 1: Log file output
# =============================================================================

class LogFileOutput(PacketOutput):
    """Append decoded packets to a log file in human-readable format.

    If `rotate_daily` is True, file name is suffixed with the date and
    a new file is started each day at local midnight.

    Args:
        path: log file path (or path template like 'alert2-{date}.log'
              if rotate_daily=True, where {date} is replaced with YYYY-MM-DD)
        raw: include raw MANT PDU bytes instead of just app payload
        rotate_daily: open a new file each calendar day
    """

    def __init__(self, path: str, raw: bool = False,
                 rotate_daily: bool = False):
        self.path_template = path
        self.raw = raw
        self.rotate_daily = rotate_daily
        self._fh: Optional[TextIO] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()
        self._open_file()

    def _today_str(self) -> str:
        return datetime.date.today().isoformat()

    def _resolve_path(self) -> str:
        if self.rotate_daily:
            today = self._today_str()
            if '{date}' in self.path_template:
                return self.path_template.replace('{date}', today)
            # Insert date before extension
            base, ext = os.path.splitext(self.path_template)
            return f"{base}-{today}{ext}"
        return self.path_template

    def _open_file(self) -> None:
        path = self._resolve_path()
        # Make parent directory if needed
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        # Track whether this is a fresh file before opening in append mode
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, 'a', buffering=1, encoding='utf-8')  # Line-buffered
        self._current_date = self._today_str()
        # Session-start marker — see DiagOutput._open_file for rationale.
        # Written on every file open including after daily rotation.
        try:
            import socket
            from . import __version__ as _ver
            host = socket.gethostname()
        except Exception:
            _ver = '?'
            host = '?'
        now_iso = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        try:
            self._fh.write(f"# SESSION_START at {now_iso} by alert2 {_ver} "
                           f"on {host}\n")
        except Exception:
            pass

    def _maybe_rotate(self) -> None:
        if not self.rotate_daily:
            return
        today = self._today_str()
        if today != self._current_date:
            if self._fh:
                self._fh.close()
            self._open_file()

    def emit(self, pkt: Alert2Packet) -> None:
        with self._lock:
            self._maybe_rotate()
            ts = datetime.datetime.fromtimestamp(pkt.rx_time).strftime(
                '%Y-%m-%d %H:%M:%S.%f')[:-3]
            line = f"[{ts}] {format_packet_human(pkt, raw=self.raw)}"
            self._fh.write(line + '\n')

    def close(self) -> None:
        with self._lock:
            if self._fh:
                try:
                    now_iso = datetime.datetime.now().astimezone().isoformat(
                        timespec='seconds')
                    self._fh.write(f"# SESSION_END at {now_iso}\n")
                except Exception:
                    pass
                self._fh.close()
                self._fh = None


# =============================================================================
# PRIORITY 2: CSV output
# =============================================================================

# CSV column order — keep stable for downstream compatibility
CSV_FIELDS = [
    'rx_time_iso',       # ISO 8601 timestamp
    'rx_time_unix',      # Unix epoch float
    'fec_mode',
    'framesync_hd',
    'rs_corrections',
    'airlink_version',
    'airlink_length',
    'mant_version',
    'mant_proto_id',
    'mant_source',
    'mant_dest',
    'mant_added_sas',    # Comma-separated list of repeater SAs (Add Path)
    'mant_port',
    'mant_hop_limit',
    'mant_payload_length',
    'mant_tssr',
    'mant_add_path',
    'mant_da_present',
    'mant_encrypted',
    'mant_ack',
    'mant_added_header',
    'mant_header_bytes',
    'app_pdu_id',        # SRP PDU ID (rotates 0-6, 7=disabled) for dup detection
    'app_test_flag',     # SRP test flag
    'truncated',         # 1 if follow-on RS failed; data is incomplete
    'srp_summary',       # Compact human-readable summary of decoded SRP
    'snr_db',            # Signal-to-noise ratio in dB (FM quieting)
    'noise_rms',         # Pre-burst RMS amplitude (FM noise level)
    'signal_rms',        # In-burst RMS amplitude (lower = better lock)
    'app_payload_hex',
    'raw_pdu_hex',
    'decode_path',
]


def packet_to_dict(pkt: Alert2Packet) -> Dict[str, str]:
    """Convert a packet to a flat dict matching CSV_FIELDS."""
    rx_iso = datetime.datetime.fromtimestamp(pkt.rx_time).isoformat()
    d = {
        'rx_time_iso': rx_iso,
        'rx_time_unix': f"{pkt.rx_time:.6f}",
        'fec_mode': pkt.fec_mode,
        'framesync_hd': pkt.framesync_hd if pkt.framesync_hd is not None else '',
        'rs_corrections': pkt.rs_corrections,
        'airlink_version': '',
        'airlink_length': '',
        'mant_version': '',
        'mant_proto_id': '',
        'mant_source': '',
        'mant_dest': '',
        'mant_added_sas': '',
        'mant_port': '',
        'mant_hop_limit': '',
        'mant_payload_length': '',
        'mant_tssr': '',
        'mant_add_path': '',
        'mant_da_present': '',
        'mant_encrypted': '',
        'mant_ack': '',
        'mant_added_header': '',
        'mant_header_bytes': '',
        'app_pdu_id': '',
        'app_test_flag': '',
        'truncated': int(pkt.truncated) if pkt.truncated else 0,
        'srp_summary': '',
        'snr_db': '' if pkt.snr_db is None else f"{pkt.snr_db:.2f}",
        'noise_rms': '' if pkt.noise_rms is None else f"{pkt.noise_rms:.6f}",
        'signal_rms': '' if pkt.signal_rms is None else f"{pkt.signal_rms:.6f}",
        'app_payload_hex': pkt.application_payload.hex() if pkt.application_payload else '',
        'raw_pdu_hex': pkt.raw_payload.hex() if pkt.raw_payload else '',
        'decode_path': pkt.decode_path or '',
    }
    if pkt.airlink:
        d['airlink_version'] = pkt.airlink.version
        d['airlink_length'] = pkt.airlink.length
    if pkt.mant:
        m = pkt.mant
        d['mant_version'] = m.version
        d['mant_proto_id'] = m.proto_id
        d['mant_source'] = m.source_addr
        d['mant_dest'] = m.dest_addr if m.dest_addr is not None else ''
        d['mant_added_sas'] = ','.join(str(s) for s in m.added_sas) if m.added_sas else ''
        d['mant_port'] = m.port
        d['mant_hop_limit'] = m.hop_limit
        d['mant_payload_length'] = m.payload_length
        d['mant_tssr'] = int(m.tssr)
        d['mant_add_path'] = int(m.add_path)
        d['mant_da_present'] = int(m.da_present)
        d['mant_encrypted'] = int(m.encrypted)
        d['mant_ack'] = int(m.ack)
        d['mant_added_header'] = int(m.added_header)
        d['mant_header_bytes'] = m.header_bytes
    if pkt.srp_pdu and pkt.srp_pdu.reports:
        d['srp_summary'] = str(pkt.srp_pdu)
    if pkt.srp_pdu is not None:
        d['app_pdu_id'] = pkt.srp_pdu.pdu_id
        d['app_test_flag'] = int(pkt.srp_pdu.test_flag)
    return d


class DiagOutput(PacketOutput):
    """Diagnostic output for in-depth analysis of decoder behavior.

    Writes one record per decoded packet with all the per-block info
    that's normally hidden: RS corrections, framesync Hamming distance,
    SNR, raw MANT PDU hex, truncation flag, and the application payload
    hex. Useful for understanding which packets are decoding well and
    which are marginal, especially when correlated with WAV captures.

    Format is a simple structured text log (not JSON, not CSV) — meant
    to be human-readable as well as machine-parseable.
    """

    def __init__(self, path: str, rotate_daily: bool = False,
                 log_fs_failures: bool = False,
                 fs_failures_hd_max: int = 2):
        """
        Args:
            path: diag file path
            rotate_daily: open a new file each calendar day
            log_fs_failures: if True, write `# FS_FAIL` markers for
                framesync candidates that didn't produce any packet.
                These are commented with `#` so existing parsers
                (burst_counter, msr_status_analysis) ignore them.
            fs_failures_hd_max: only log failures at this HD or lower
                (default 2). Higher HD values are mostly noise that
                bit-matched the framesync pattern; logging them
                inflates the diag without informational value.
        """
        self.path = path
        self.rotate_daily = rotate_daily
        self.log_fs_failures = log_fs_failures
        self.fs_failures_hd_max = fs_failures_hd_max
        self._current_path = self._compute_path()
        self._open_file()

    def _compute_path(self):
        if not self.rotate_daily:
            return self.path
        # Same rotation logic as LogFileOutput: insert date before extension
        base, ext = os.path.splitext(self.path)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        return f"{base}-{date_str}{ext}"

    def _open_file(self):
        new_file = not os.path.exists(self._current_path) or \
                   os.path.getsize(self._current_path) == 0
        self._fh = open(self._current_path, 'a', buffering=1, encoding='utf-8')
        if new_file:
            self._fh.write("# ALERT2 decoder diagnostic log\n")
            self._fh.write("# Format: timestamp | src | path | flags | "
                           "RF metrics | byte counts | hex payload\n")
            self._fh.write("#\n")
        # Session-start marker — written on every file open, including after
        # daily rotation. Format is greppable and parseable: # SESSION_START
        # at <iso8601> by <version> on <hostname>
        # This anchors the diag to a wall-clock window so later analysis
        # (e.g. comparing against 4601's encoder log) can know when this
        # decoder instance was running.
        try:
            import socket
            from . import __version__ as _ver
            host = socket.gethostname()
        except Exception:
            _ver = '?'
            host = '?'
        now_iso = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        self._fh.write(f"# SESSION_START at {now_iso} by alert2 {_ver} "
                       f"on {host}\n")

    def _maybe_rotate(self):
        if not self.rotate_daily:
            return
        new_path = self._compute_path()
        if new_path != self._current_path:
            try:
                self._fh.close()
            except Exception:
                pass
            self._current_path = new_path
            self._open_file()

    def emit(self, pkt: Alert2Packet) -> None:
        self._maybe_rotate()
        ts = datetime.datetime.fromtimestamp(pkt.rx_time).strftime(
            '%Y-%m-%d %H:%M:%S.%f')[:-3]
        src = pkt.mant.source_addr if pkt.mant else None
        path = (','.join(str(x) for x in pkt.mant.added_sas)
                if pkt.mant and pkt.mant.added_sas else 'direct')
        # Flags
        flags = []
        if pkt.truncated:
            flags.append('TRUNCATED')
        if pkt.rs_corrections > 0:
            _pb = getattr(pkt, 'rs_block_corr', None)
            if _pb and len(_pb) > 1:
                flags.append(
                    f"RS_corr={pkt.rs_corrections}"
                    f"[{','.join(str(x) for x in _pb)}]")
            else:
                flags.append(f'RS_corr={pkt.rs_corrections}')
        if pkt.framesync_hd is not None and pkt.framesync_hd > 0:
            flags.append(f'FS_hd={pkt.framesync_hd}')
        # Iterative-decode flag: only show when erasures were used.
        # 'standard' means iterative was on but no erasures needed —
        # BUT that is the Viterbi-diversity recovery (SOVA's alternate
        # Viterbi succeeded via unforced RS where soft-V failed), which
        # IS noteworthy for ladder attribution, so log it as sova-std.
        if pkt.iter_method == 'erasure':
            flags.append(f'iter=erasure(n={pkt.iter_n_erasures})')
        elif pkt.iter_method == 'standard':
            flags.append('iter=sova-std')
        # Decode-ladder attribution (rc16): which alternative rung
        # produced this packet. Absence of eq/iter flags = plain
        # standard soft decode.
        if getattr(pkt, 'eq_recovered', False):
            flags.append('eq-dd' if getattr(pkt, 'eq_stage', None) == 'dd'
                         else 'eq')
        if getattr(pkt, 'burst_scoped', False):
            flags.append('burst')
        if getattr(pkt, 'presync_found', False):
            _rhd = getattr(pkt, 'presync_raw_hd', None)
            flags.append(f'presync(raw_hd={_rhd})' if _rhd is not None
                         else 'presync')
        flags_str = ','.join(flags) if flags else 'clean'
        # RF
        snr_str = f'SNR={pkt.snr_db:.2f}dB' if pkt.snr_db is not None else 'SNR=?'
        # MER is reported alongside SNR, never instead of it: the two
        # measure different things (SNR is an FM-quieting level ratio, MER
        # is amplitude-normalised modulation quality) and every diag
        # collected before this change has SNR only.
        if pkt.mer_db is not None:
            _cov = pkt.mer_coverage or '?'
            snr_str += (f' MER={pkt.mer_db:.2f}dB'
                        f' EVM={pkt.mer_evm_pct:.1f}%'
                        f' mer_n={pkt.mer_symbols}({_cov})')
            # Demod-chain diagnostic, diag only: MER after an ideal 5-tap
            # linear eq, and the gain over raw. Separates ISI-limited
            # packets (--eq-retry can help) from noise-limited ones (it
            # cannot). Deliberately absent from the log line and the CLI
            # summary — it is for analysing the chain, not for operations.
            if pkt.mer_eq_db is not None:
                snr_str += (f' MER_eq={pkt.mer_eq_db:.2f}dB'
                            f' eq_gain={pkt.mer_eq_gain_db:+.2f}dB')
            # Achieved MER: what the real equalizer delivered, between the
            # raw channel (MER) and the ideal bound (MER_eq).
            if getattr(pkt, 'mer_act_db', None) is not None:
                snr_str += f' MER_act={pkt.mer_act_db:.2f}dB'
            # Symbol-rate error the rate-track rung corrected for (ppm of
            # 4800 baud). Only present on packets that came through it.
            if getattr(pkt, 'rate_ppm', None) is not None:
                snr_str += f' rate={pkt.rate_ppm:+.0f}ppm'
            # Capture LEVEL, not quality. alpha is the least-squares symbol
            # amplitude — the scale MER divides out — in raw soft-sample
            # units. Deliberately not normalised to dB against a reference
            # constant: the reference would be site-specific, and the
            # per-source average supplies the baseline empirically after a
            # day. A mixer level reset (e.g. a Pi reboot restoring default
            # input gain) shows up here as a step change on the very next
            # packet, long before MER degrades enough to cost packets.
            if pkt.mer_alpha is not None:
                snr_str += f' alpha={pkt.mer_alpha:.4f}'
        else:
            snr_str += ' MER=?'
        # Per-burst symbol-timing probe (--timing-probe). Observation only,
        # and reported independently of MER so it survives --mer-mode off.
        # t_off is the fractional SAMPLE offset that would have peaked the
        # known-preamble match at this burst; 9 samples = 1 symbol. Whole-
        # sample values mean the window's phase was simply the wrong one of
        # the nine. t_gain is the correlation left on the table.
        if pkt.timing_offset is not None:
            snr_str += f' t_off={pkt.timing_offset:+.3f}'
            if pkt.timing_gain is not None:
                snr_str += f' t_gain={pkt.timing_gain:.4f}'
        # Byte counts
        airlink_len = pkt.airlink.length if pkt.airlink else 0
        mant_hdr_len = pkt.mant.header_bytes if pkt.mant else 0
        mant_payload_len = pkt.mant.payload_length if pkt.mant else 0
        app_actual_len = len(pkt.application_payload)
        byte_str = (f'airlink_len={airlink_len} '
                    f'mant_hdr={mant_hdr_len} '
                    f'mant_payload={mant_payload_len} '
                    f'app_actual={app_actual_len} '
                    f'decode_path={pkt.decode_path}')
        # Hex
        mant_pdu_hex = pkt.raw_payload.hex() if pkt.raw_payload else ''
        app_hex = pkt.application_payload.hex() if pkt.application_payload else ''
        # Report summary if available
        srp_str = ''
        if pkt.srp_pdu:
            srp_str = f' srp={pkt.srp_pdu}'
        # Write the diag line. The matchable keys are already here:
        #   - cross-receiver (QTH<->shop): mant_pdu_hex continuation line is
        #     the exact raw key (both SDRs hear identical relayed bytes).
        #   - vs the 4601 relay log: 4601 sits at a different point in the
        #     relay chain, so no raw field matches it; the match is on the
        #     DECODED key (src, SRP id, SRP ts, path) — all present on this
        #     line — with a 12h-wrap tiebreaker on wall-clock time.
        line = (f'{ts} | src={src} path=[{path}] | {flags_str} | '
                f'{snr_str} | {byte_str}{srp_str}\n'
                f'         mant_pdu_hex={mant_pdu_hex}\n'
                f'         app_payload_hex={app_hex}\n')
        try:
            self._fh.write(line)
        except Exception:
            pass

    def note_framesync_event(self, fec_mode: int, fs_hd: int,
                              decoded_pkts: list, fs_end_bit: int) -> None:
        """Optionally log framesync failures as commented diag lines.

        Format: `# FS_FAIL <iso8601> HD=<n> mode=<n>`
        Only written when log_fs_failures=True and the framesync
        produced zero packets. The `#` prefix means existing diag
        parsers ignore these lines.
        """
        if not self.log_fs_failures:
            return
        if decoded_pkts:  # success — handled by emit()
            return
        if fs_hd > self.fs_failures_hd_max:
            return  # likely noise, skip
        try:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            self._fh.write(f'# FS_FAIL {ts} HD={fs_hd} mode={fec_mode}\n')
        except Exception:
            pass

    def note_stream_health(self, info: dict) -> None:
        """Write a live-pipeline health line into the diag (rc22).

        Audio arrives from the sound card at a fixed rate; if the
        decoder cannot keep up, samples are shed upstream and the
        bursts they carried vanish with no framesync and no truncated
        remnant. Comparing samples actually read against samples the
        wall clock says were produced makes that visible. A healthy
        line reads deficit=0.000s; a growing deficit is dropped audio,
        which is a plumbing problem, not an RF or decode one.
        """
        if self._fh is None:
            return
        try:
            self._fh.write(
                "# AUDIO_CLOCK elapsed={elapsed_s:.1f}s "
                "read={samples_read} expected={samples_expected} "
                "deficit={deficit_s:.3f}s ({deficit_pct:.3f}%) "
                "decode_rt={rt_factor:.3f}x src={source}\n".format(**info))
            self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        # Session-end marker — written on graceful shutdown. May be absent
        # if the decoder is killed forcefully; analysis should treat the
        # last reception timestamp as a fallback bound in that case.
        try:
            now_iso = datetime.datetime.now().astimezone().isoformat(
                timespec='seconds')
            self._fh.write(f"# SESSION_END at {now_iso}\n")
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


class CsvOutput(PacketOutput):
    """Write decoded packets to a CSV file.

    Header is written automatically when the file is created. If the file
    already exists and is non-empty, append without rewriting the header.

    Args:
        path: CSV file path
        rotate_daily: open a new file each calendar day
    """

    def __init__(self, path: str, rotate_daily: bool = False):
        self.path_template = path
        self.rotate_daily = rotate_daily
        self._fh: Optional[TextIO] = None
        self._writer: Optional[csv.DictWriter] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()
        self._open_file()

    def _today_str(self) -> str:
        return datetime.date.today().isoformat()

    def _resolve_path(self) -> str:
        if self.rotate_daily:
            today = self._today_str()
            if '{date}' in self.path_template:
                return self.path_template.replace('{date}', today)
            base, ext = os.path.splitext(self.path_template)
            return f"{base}-{today}{ext}"
        return self.path_template

    def _open_file(self) -> None:
        path = self._resolve_path()
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        # Check if we need to write a header (new or empty file)
        write_header = (not os.path.exists(path) or
                        os.path.getsize(path) == 0)
        self._fh = open(path, 'a', buffering=1, newline='', encoding='utf-8')
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS,
                                       extrasaction='ignore')
        if write_header:
            self._writer.writeheader()
        self._current_date = self._today_str()

    def _maybe_rotate(self) -> None:
        if not self.rotate_daily:
            return
        today = self._today_str()
        if today != self._current_date:
            if self._fh:
                self._fh.close()
            self._open_file()

    def emit(self, pkt: Alert2Packet) -> None:
        with self._lock:
            self._maybe_rotate()
            row = packet_to_dict(pkt)
            self._writer.writerow(row)

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


# =============================================================================
# PRIORITY 3: Decoder statistics
# =============================================================================

class StatsCollector(PacketOutput):
    """Collect decoder statistics during a session.

    Tracks:
      - Total packets decoded
      - Packets per FEC mode
      - Framesync Hamming distance distribution
      - RS corrections distribution
      - Source address frequency
      - Decode path attribution (which demod variant produced each packet)
      - Total framesync attempts vs successes
      - Burst detections

    Use print_summary() at end to display, or get_summary() for a dict.
    """

    def __init__(self):
        self.start_time = time.time()
        self.packet_count = 0
        self.packets_by_mode = Counter()
        self.packets_by_source = Counter()
        self.packets_by_path = Counter()
        self.truncated_count = 0
        self.fs_hd_distribution = Counter()
        self.rs_corrections_distribution = Counter()
        self.payload_lengths = []
        self.snr_values = []  # Per-packet SNR in dB
        self.mer_values = []  # Per-packet MER in dB (amplitude-normalised)
        self.mer_eq_values = []   # MER after an ideal linear equalizer
        self.eq_gain_values = []  # removable-ISI component, dB
        self.alpha_values = []    # capture level (raw soft-sample units)
        self.snr_by_source = defaultdict(list)
        # PDU ID rotation tracking: last seen pdu_id per source.
        # Spec says PDU IDs rotate 0->6->0; gaps in the sequence indicate
        # missed packets. Value 7 means "PDU ID disabled" — don't track.
        self.last_pdu_id_by_source = {}      # src -> last pdu_id seen
        self.pdu_id_gaps_by_source = defaultdict(int)  # src -> count of gaps
        self._last_pdu_id_logical = {}       # src -> {id, ts} of last logical pkt
        self.attempt_count = 0
        self.attempt_successes = 0
        self.attempts_by_fec_mode = Counter()
        self.burst_count = 0
        # 30-minute noise-floor trace: (bucket_start_unixtime) -> list of SNRs
        # Each packet contributes one SNR to the bucket containing its rx_time.
        # Median SNR per bucket lets us detect noise-floor changes over time.
        self._snr_buckets = defaultdict(list)
        self.SNR_BUCKET_SECONDS = 1800  # 30 minutes
        self._lock = threading.Lock()

    def emit(self, pkt: Alert2Packet) -> None:
        with self._lock:
            self.packet_count += 1
            self.packets_by_mode[pkt.fec_mode] += 1
            if pkt.mant:
                self.packets_by_source[pkt.mant.source_addr] += 1
                self.payload_lengths.append(pkt.mant.payload_length)
            self.packets_by_path[pkt.decode_path] += 1
            if pkt.truncated:
                self.truncated_count += 1
            if pkt.framesync_hd is not None:
                self.fs_hd_distribution[pkt.framesync_hd] += 1
            self.rs_corrections_distribution[pkt.rs_corrections] += 1
            if pkt.snr_db is not None:
                self.snr_values.append(pkt.snr_db)
                if pkt.mant:
                    self.snr_by_source[pkt.mant.source_addr].append(pkt.snr_db)
                # Bucket SNRs by 30-min window of rx_time for noise-floor trace
                bucket_start = int(pkt.rx_time // self.SNR_BUCKET_SECONDS) * self.SNR_BUCKET_SECONDS
                self._snr_buckets[bucket_start].append(pkt.snr_db)
            if getattr(pkt, 'mer_db', None) is not None:
                self.mer_values.append(pkt.mer_db)
            if getattr(pkt, 'mer_alpha', None) is not None:
                self.alpha_values.append(pkt.mer_alpha)
            if getattr(pkt, 'mer_eq_db', None) is not None:
                self.mer_eq_values.append(pkt.mer_eq_db)
                self.eq_gain_values.append(pkt.mer_eq_gain_db)
            # Track PDU ID rotation per source (skip pdu_id=7, "disabled").
            # Dedupe by (src, ts) so the same packet through multiple
            # repeaters doesn't count as a "new" PDU ID.
            if (pkt.mant and pkt.srp_pdu is not None
                    and pkt.srp_pdu.pdu_id != 7):
                src = pkt.mant.source_addr
                cur_id = pkt.srp_pdu.pdu_id
                cur_ts = pkt.srp_pdu.timestamp  # may be None
                # Only update if this is a new logical packet (different ts
                # or no ts and id has changed)
                last = self._last_pdu_id_logical.get(src)
                if last is None or last['ts'] != cur_ts or last['id'] != cur_id:
                    if last is not None:
                        last_id = last['id']
                        expected = (last_id + 1) % 7  # rotates 0..6
                        if cur_id != expected:
                            gap = (cur_id - last_id - 1) % 7
                            self.pdu_id_gaps_by_source[src] += gap
                    self._last_pdu_id_logical[src] = {'id': cur_id, 'ts': cur_ts}
                    self.last_pdu_id_by_source[src] = cur_id

    def note_attempt(self, fec_mode: int, fs_hd: int, succeeded: bool) -> None:
        with self._lock:
            self.attempt_count += 1
            self.attempts_by_fec_mode[fec_mode] += 1
            if succeeded:
                self.attempt_successes += 1

    def note_burst(self, start_sample: int, duration_samples: int) -> None:
        with self._lock:
            self.burst_count += 1

    def get_noise_floor_trace(self):
        """Return a list of dicts, one per 30-min bucket, with median SNR
        and packet count. Useful for spotting noise-floor changes over a
        long run. Buckets with zero packets are omitted; gaps in the
        timeline can be inferred from missing bucket_start times.
        """
        import datetime
        import statistics
        trace = []
        for bucket_start in sorted(self._snr_buckets):
            snrs = self._snr_buckets[bucket_start]
            if not snrs: continue
            trace.append({
                'bucket_start_unix': bucket_start,
                'bucket_start_iso': datetime.datetime.fromtimestamp(
                    bucket_start).isoformat(),
                'packet_count': len(snrs),
                'snr_median_db': round(statistics.median(snrs), 2),
                'snr_min_db': round(min(snrs), 2),
                'snr_max_db': round(max(snrs), 2),
            })
        return trace

    def get_summary(self) -> dict:
        elapsed = time.time() - self.start_time
        success_rate = (self.attempt_successes / self.attempt_count
                        if self.attempt_count else 0.0)
        # SNR statistics
        snr_summary = None
        if self.snr_values:
            snr_summary = {
                'min_db': round(min(self.snr_values), 2),
                'avg_db': round(sum(self.snr_values) / len(self.snr_values), 2),
                'max_db': round(max(self.snr_values), 2),
                'count': len(self.snr_values),
            }
        mer_summary = None
        if self.mer_values:
            mer_summary = {
                'min_db': round(min(self.mer_values), 2),
                'avg_db': round(sum(self.mer_values) / len(self.mer_values), 2),
                'max_db': round(max(self.mer_values), 2),
                'count': len(self.mer_values),
            }
        mer_eq_summary = None
        if self.mer_eq_values:
            g = sorted(self.eq_gain_values)
            mer_eq_summary = {
                'min_db': round(min(self.mer_eq_values), 2),
                'avg_db': round(sum(self.mer_eq_values)
                                / len(self.mer_eq_values), 2),
                'max_db': round(max(self.mer_eq_values), 2),
                'median_gain_db': round(g[len(g) // 2], 2),
                'count': len(self.mer_eq_values),
            }
        alpha_summary = None
        if self.alpha_values:
            a = sorted(self.alpha_values)
            alpha_summary = {
                'min': round(a[0], 4),
                'median': round(a[len(a) // 2], 4),
                'max': round(a[-1], 4),
                'count': len(a),
            }
        snr_per_source = {
            src: round(sum(vs) / len(vs), 2)
            for src, vs in self.snr_by_source.items() if vs
        }
        return {
            'session_duration_sec': round(elapsed, 1),
            'packets_decoded': self.packet_count,
            'packets_per_minute': round(self.packet_count / elapsed * 60, 2)
                                   if elapsed > 0 else 0,
            'packets_by_fec_mode': dict(self.packets_by_mode),
            'unique_source_addresses': len(self.packets_by_source),
            'top_sources': self.packets_by_source.most_common(10),
            'packets_by_decode_path': dict(self.packets_by_path),
            'framesync_hd_distribution': dict(sorted(self.fs_hd_distribution.items())),
            'rs_corrections_distribution': dict(sorted(self.rs_corrections_distribution.items())),
            'truncated_packets': self.truncated_count,
            'avg_payload_length': round(sum(self.payload_lengths) / len(self.payload_lengths), 1)
                                   if self.payload_lengths else 0,
            'snr_summary_db': snr_summary,
            'mer_summary_db': mer_summary,
            'mer_eq_summary_db': mer_eq_summary,
            'capture_level_alpha': alpha_summary,
            'avg_snr_per_source_db': snr_per_source,
            'pdu_id_gaps_by_source': dict(self.pdu_id_gaps_by_source),
            'last_pdu_id_by_source': dict(self.last_pdu_id_by_source),
            'framesync_attempts': self.attempt_count,
            'framesync_successes': self.attempt_successes,
            'framesync_success_rate': round(success_rate, 4),
            'bursts_detected': self.burst_count,
            'noise_floor_trace_30min': self.get_noise_floor_trace(),
        }

    def print_summary(self, file=sys.stderr) -> None:
        s = self.get_summary()
        print("\n" + "=" * 60, file=file)
        print("ALERT2 Decoder Session Statistics", file=file)
        print("=" * 60, file=file)
        print(f"Session duration:       {s['session_duration_sec']:.1f} s", file=file)
        print(f"Packets decoded:        {s['packets_decoded']}", file=file)
        if s['session_duration_sec'] > 0:
            print(f"Packets/minute:         {s['packets_per_minute']}", file=file)
        if s['packets_by_fec_mode']:
            modes_str = ", ".join(f"FEC{m}={c}" for m, c
                                    in sorted(s['packets_by_fec_mode'].items()))
            print(f"By FEC mode:            {modes_str}", file=file)
        print(f"Unique source addresses: {s['unique_source_addresses']}", file=file)
        if s['top_sources']:
            print("Top sources:", file=file)
            for src, count in s['top_sources']:
                print(f"  {src:6d}: {count} packets", file=file)
        if s['framesync_hd_distribution']:
            hd_str = ", ".join(f"hd={hd}: {c}" for hd, c
                                in sorted(s['framesync_hd_distribution'].items()))
            print(f"Framesync HD:           {hd_str}", file=file)
        if s['rs_corrections_distribution']:
            rs_str = ", ".join(f"{c}corr: {n}" for c, n
                                in sorted(s['rs_corrections_distribution'].items()))
            print(f"RS corrections:         {rs_str}", file=file)
        if s.get('truncated_packets', 0) > 0:
            print(f"Truncated packets:      {s['truncated_packets']}   "
                  f"(follow-on RS failed; partial data only)", file=file)
        if s['avg_payload_length']:
            print(f"Avg payload length:     {s['avg_payload_length']:.1f} bytes", file=file)
        if s.get('snr_summary_db'):
            sn = s['snr_summary_db']
            print(f"Signal quality (SNR):   "
                  f"min {sn['min_db']:.1f}dB, avg {sn['avg_db']:.1f}dB, "
                  f"max {sn['max_db']:.1f}dB ({sn['count']} pkts)", file=file)
        if s.get('mer_summary_db'):
            mr = s['mer_summary_db']
            print(f"Modulation qual (MER):  "
                  f"min {mr['min_db']:.1f}dB, avg {mr['avg_db']:.1f}dB, "
                  f"max {mr['max_db']:.1f}dB ({mr['count']} pkts)", file=file)
        if s.get('mer_eq_summary_db'):
            me = s['mer_eq_summary_db']
            print(f"  after ideal eq:       "
                  f"min {me['min_db']:.1f}dB, avg {me['avg_db']:.1f}dB, "
                  f"max {me['max_db']:.1f}dB   "
                  f"median removable ISI {me['median_gain_db']:+.1f}dB",
                  file=file)
        if s.get('capture_level_alpha'):
            al = s['capture_level_alpha']
            print(f"Capture level (alpha):  "
                  f"min {al['min']:.4f}, median {al['median']:.4f}, "
                  f"max {al['max']:.4f}  (raw units; watch for step "
                  f"changes after a reboot)", file=file)
        if s.get('avg_snr_per_source_db'):
            print(f"Avg SNR by source:", file=file)
            sorted_srcs = sorted(s['avg_snr_per_source_db'].items(),
                                  key=lambda kv: -kv[1])
            for src, snr in sorted_srcs[:10]:
                print(f"  {src:6d}: {snr:.1f} dB", file=file)
        if s.get('pdu_id_gaps_by_source'):
            gaps = {s: g for s, g in s['pdu_id_gaps_by_source'].items() if g > 0}
            if gaps:
                total = sum(gaps.values())
                print(f"Estimated missed packets (PDU ID gaps): {total} total",
                      file=file)
                for src, g in sorted(gaps.items(), key=lambda kv: -kv[1])[:10]:
                    print(f"  {src:6d}: {g} missed", file=file)
        if s['packets_by_decode_path']:
            paths_str = ", ".join(f"{p}={c}" for p, c
                                    in s['packets_by_decode_path'].items())
            print(f"By decode path:         {paths_str}", file=file)
        if s['framesync_attempts']:
            print(f"Framesync attempts:     {s['framesync_attempts']}, "
                  f"successes: {s['framesync_successes']} "
                  f"({s['framesync_success_rate']*100:.1f}%)", file=file)
        if s['bursts_detected']:
            print(f"Bursts detected:        {s['bursts_detected']}", file=file)

        # 30-min noise floor trace, if there are >1 buckets to compare
        trace = s.get('noise_floor_trace_30min', [])
        if len(trace) >= 2:
            print(f"", file=file)
            print(f"Noise floor trace (30-min buckets):", file=file)
            print(f"  {'time':<19} {'pkts':>5} {'med SNR':>9} "
                  f"{'min':>6} {'max':>6}  notes", file=file)
            # Compute overall median for baseline comparison
            import statistics
            all_medians = [b['snr_median_db'] for b in trace]
            baseline = statistics.median(all_medians)
            for b in trace:
                tstr = b['bucket_start_iso'][:16].replace('T', ' ')
                delta = b['snr_median_db'] - baseline
                # Flag buckets >= 0.5 dB below baseline
                flag = ' ★ noise event' if delta <= -0.5 else ''
                print(f"  {tstr:<19} {b['packet_count']:>5} "
                      f"{b['snr_median_db']:>7.1f}dB "
                      f"{b['snr_min_db']:>5.1f} {b['snr_max_db']:>5.1f}  "
                      f"({delta:+.1f} vs median){flag}", file=file)
        print("=" * 60, file=file)

    def write_summary_file(self, path: str) -> None:
        """Write summary as a JSON file."""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.get_summary(), f, indent=2, default=str)


# =============================================================================
# PRIORITY 4: JSON Lines output
# =============================================================================

def packet_to_json_dict(pkt: Alert2Packet) -> dict:
    """Convert a packet to a JSON-serializable dict.

    Same fields as CSV but as a nested structure.
    """
    rx_iso = datetime.datetime.fromtimestamp(pkt.rx_time).isoformat()
    d = {
        'rx_time': {
            'iso': rx_iso,
            'unix': pkt.rx_time,
        },
        'fec_mode': pkt.fec_mode,
        'framesync_hd': pkt.framesync_hd,
        'rs_corrections': pkt.rs_corrections,
        'truncated': pkt.truncated,
        'decode_path': pkt.decode_path,
        'signal': None,
        'airlink': None,
        'mant': None,
        'app_payload_hex': pkt.application_payload.hex() if pkt.application_payload else None,
        'raw_pdu_hex': pkt.raw_payload.hex() if pkt.raw_payload else None,
    }
    if pkt.snr_db is not None:
        d['signal'] = {
            'snr_db': pkt.snr_db,
            'noise_rms': pkt.noise_rms,
            'signal_rms': pkt.signal_rms,
        }
    if pkt.airlink:
        d['airlink'] = {
            'version': pkt.airlink.version,
            'length': pkt.airlink.length,
        }
    if pkt.mant:
        m = pkt.mant
        d['mant'] = {
            'version': m.version,
            'proto_id': m.proto_id,
            'tssr': m.tssr,
            'add_path': m.add_path,
            'da_present': m.da_present,
            'port': m.port,
            'encrypted': m.encrypted,
            'ack': m.ack,
            'added_header': m.added_header,
            'hop_limit': m.hop_limit,
            'payload_length': m.payload_length,
            'source_addr': m.source_addr,
            'dest_addr': m.dest_addr,
            'added_sas': m.added_sas,
            'header_bytes': m.header_bytes,
        }
    if pkt.srp_pdu is not None:
        try:
            from .srp import srp_pdu_to_dict
            d['srp'] = srp_pdu_to_dict(pkt.srp_pdu)
        except Exception:
            d['srp'] = None
    return d


class JsonlOutput(PacketOutput):
    """Write decoded packets to a JSON Lines file (one JSON object per line).

    JSON Lines (.jsonl) is widely supported by log analysis tools and
    is easier to stream/append than a JSON array.

    Args:
        path: JSONL file path (or '-' for stdout)
        rotate_daily: open a new file each calendar day
    """

    def __init__(self, path: str, rotate_daily: bool = False):
        self.path_template = path
        self.rotate_daily = rotate_daily
        self._fh: Optional[TextIO] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()
        self._stdout_mode = (path == '-')
        if self._stdout_mode:
            self._fh = sys.stdout
            self.rotate_daily = False
        else:
            self._open_file()

    def _today_str(self) -> str:
        return datetime.date.today().isoformat()

    def _resolve_path(self) -> str:
        if self.rotate_daily:
            today = self._today_str()
            if '{date}' in self.path_template:
                return self.path_template.replace('{date}', today)
            base, ext = os.path.splitext(self.path_template)
            return f"{base}-{today}{ext}"
        return self.path_template

    def _open_file(self) -> None:
        path = self._resolve_path()
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        self._fh = open(path, 'a', buffering=1, encoding='utf-8')
        self._current_date = self._today_str()

    def _maybe_rotate(self) -> None:
        if not self.rotate_daily or self._stdout_mode:
            return
        today = self._today_str()
        if today != self._current_date:
            if self._fh and not self._stdout_mode:
                self._fh.close()
            self._open_file()

    def emit(self, pkt: Alert2Packet) -> None:
        with self._lock:
            self._maybe_rotate()
            obj = packet_to_json_dict(pkt)
            self._fh.write(json.dumps(obj, default=str) + '\n')
            if self._stdout_mode:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh and not self._stdout_mode:
                self._fh.close()
                self._fh = None


# =============================================================================
# Multi-output dispatcher
# =============================================================================

def packet_to_forwarder_dict(pkt: Alert2Packet) -> dict:
    """Convert a packet to a flat dict suitable for the alert2_forwarder.

    Fields are flatter than packet_to_json_dict — designed for downstream
    formatting into Campbell ASCII or similar. Hex strings (lowercase, no
    spaces) for byte fields, plain numbers and lists otherwise.
    """
    d = {
        'rx_time_unix': pkt.rx_time,
        'fec_mode': pkt.fec_mode,
        'snr_db': pkt.snr_db,
        'noise_rms': pkt.noise_rms,
        'signal_rms': pkt.signal_rms,
        'rs_corrections': pkt.rs_corrections,
        'framesync_hd': pkt.framesync_hd,
        'truncated': pkt.truncated,
        'src': None,
        'dest': None,
        'added_sas': [],
        'port': None,
        'hop_limit': None,
        'mant_payload_length': None,
        'raw_payload_hex': pkt.raw_payload.hex() if pkt.raw_payload else '',
        'application_payload_hex': pkt.application_payload.hex()
            if pkt.application_payload else '',
        'airlink_length': pkt.airlink.length if pkt.airlink else None,
    }
    if pkt.mant:
        d['src'] = pkt.mant.source_addr
        d['dest'] = pkt.mant.dest_addr
        d['added_sas'] = list(pkt.mant.added_sas) if pkt.mant.added_sas else []
        d['port'] = pkt.mant.port
        d['hop_limit'] = pkt.mant.hop_limit
        d['mant_payload_length'] = pkt.mant.payload_length
    return d


class UdpEmitOutput(PacketOutput):
    """Emit each decoded packet as a JSON datagram over UDP.

    Designed for hand-off to a separate forwarder process. Fire-and-forget:
    the decoder doesn't know or care if anyone is listening, doesn't block
    on slow consumers, and uses no buffering — if the OS UDP buffer is full
    the datagram is silently dropped (acceptable for our packet rate).

    Args:
        host: destination hostname or IP
        port: destination UDP port
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Non-blocking, with a generous-enough send buffer
        self._sock.setblocking(False)
        # Log only once if sends fail
        self._error_logged = False

    def emit(self, pkt: Alert2Packet) -> None:
        try:
            d = packet_to_forwarder_dict(pkt)
            payload = json.dumps(d).encode('utf-8')
            self._sock.sendto(payload, (self.host, self.port))
        except Exception as e:
            if not self._error_logged:
                import logging
                logging.getLogger(__name__).warning(
                    f"UdpEmitOutput: send to {self.host}:{self.port} failed: {e}. "
                    f"Further failures will be silent.")
                self._error_logged = True

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class FramesyncDisplayOutput(PacketOutput):
    """Print framesync events to stderr in real-time for live monitoring.

    Output format (one line per framesync attempt):

        [HH:MM:SS.mmm] FS HD=0 → src=731,src=790 (2 PDUs)
        [HH:MM:SS.mmm] FS HD=2 → FAILED
        [HH:MM:SS.mmm] FS HD=4 → FAILED (likely noise)

    Use case: watching the terminal while listening to the radio audio,
    so you can correlate audible ALERT2 bursts with decoder activity.
    Each failed framesync that you also hear in the audio is a "missed
    packet by ear" — actionable diagnostic info.

    Honest caveats:
      - In WAV-file batch mode, all events fire in the few seconds
        the decoder is running, not real-time. Use for streaming
        (--stdin) for genuine live monitoring.
      - At HD≥3, many "failures" are noise that bit-matched the
        framesync pattern, not real packets. Use hd_max=2 (default)
        to suppress most of those.
      - Multiple PDUs from one framesync (multi-source repeater
        bundles) all appear on the success line — that's one
        framesync detection, not multiple.

    Args:
        hd_max: only display framesyncs with HD ≤ this value (default 2)
        show_failures: print FAILED lines too (default True)
        show_successes: print success lines too (default True)
        stream: file-like to print to (default sys.stderr)
    """

    def __init__(self, hd_max: int = 2, show_failures: bool = True,
                 show_successes: bool = True, stream=None):
        self.hd_max = hd_max
        self.show_failures = show_failures
        self.show_successes = show_successes
        self.stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()

    def note_framesync_event(self, fec_mode: int, fs_hd: int,
                              decoded_pkts: list, fs_end_bit: int) -> None:
        if fs_hd > self.hd_max:
            return
        succeeded = bool(decoded_pkts)
        if succeeded and not self.show_successes:
            return
        if not succeeded and not self.show_failures:
            return
        ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
        if succeeded:
            srcs = []
            for pkt in decoded_pkts:
                if pkt.mant is not None:
                    srcs.append(f'src={pkt.mant.source_addr}')
            srcs_str = ','.join(srcs) if srcs else '?'
            pdu_str = (f' ({len(decoded_pkts)} PDUs)'
                       if len(decoded_pkts) > 1 else '')
            line = f'[{ts}] FS HD={fs_hd} → {srcs_str}{pdu_str}\n'
        else:
            note = ''
            if fs_hd >= 3:
                note = ' (likely noise)'
            line = f'[{ts}] FS HD={fs_hd} → FAILED{note}\n'
        with self._lock:
            try:
                self.stream.write(line)
                self.stream.flush()
            except Exception:
                pass


class MultiOutput(PacketOutput):
    """Combine multiple outputs — emits each packet to all configured handlers.

    Usage:
        out = MultiOutput([
            StdoutOutput(),
            LogFileOutput('alert2.log'),
            CsvOutput('alert2.csv'),
            stats := StatsCollector(),
        ])
        ...
        out.close()
        stats.print_summary()
    """

    def __init__(self, outputs: List[PacketOutput]):
        self.outputs = list(outputs)

    def add(self, output: PacketOutput) -> None:
        self.outputs.append(output)

    def emit(self, pkt: Alert2Packet) -> None:
        for o in self.outputs:
            try:
                o.emit(pkt)
            except Exception as e:
                # Don't let one output failure break the others
                import logging
                logging.getLogger('alert2').error(
                    f"Output {type(o).__name__} failed: {e}")

    def note_attempt(self, fec_mode: int, fs_hd: int, succeeded: bool) -> None:
        for o in self.outputs:
            try:
                o.note_attempt(fec_mode, fs_hd, succeeded)
            except Exception:
                pass

    def note_framesync_event(self, fec_mode: int, fs_hd: int,
                              decoded_pkts: list, fs_end_bit: int) -> None:
        for o in self.outputs:
            try:
                o.note_framesync_event(fec_mode, fs_hd,
                                        decoded_pkts, fs_end_bit)
            except Exception:
                pass

    def note_burst(self, start_sample: int, duration_samples: int) -> None:
        for o in self.outputs:
            try:
                o.note_burst(start_sample, duration_samples)
            except Exception:
                pass

    def close(self) -> None:
        for o in self.outputs:
            try:
                o.close()
            except Exception:
                pass
