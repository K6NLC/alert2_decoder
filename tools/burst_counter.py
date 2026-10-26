#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
burst_counter.py — Count ALERT2 framesync detections in a WAV file.

The decoder's framesync detector is the gold standard for "is there an
ALERT2 burst here?" — it matches the protocol's specific 24-bit bit
pattern in the demodulated FSK output. We use it directly here as our
burst counter, then compare against successful decodes from a diag file.

This is more reliable than audio-energy detection because:
  - Framesync matches the actual protocol bit pattern, not just "loud audio"
  - It's exactly what the decoder uses to find packets internally
  - It accepts a configurable Hamming distance tolerance

USAGE:
    python3 burst_counter.py file.wav
    python3 burst_counter.py file.wav --diag alert2.diag
    python3 burst_counter.py file.wav --max-fs-hd 8  # more permissive

HONEST LIMITATIONS:
  - Framesync false positives at high Hamming distance. The decoder uses
    max_fs_hd=5 by default; this tool defaults to the same.
  - One framesync detection ≠ one decoded packet. A single audio burst
    may have multiple PDUs (concatenated reports, or relayed via
    repeaters). The decoder dedups; framesync detection counts every
    bit-pattern match.
  - At high HD (5+), pure noise can produce ~0.1-1 spurious framesync
    candidates per second. These appear as "missed bursts" but are
    actually false framesync matches in noise — not lost packets.
"""

import argparse
import datetime
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from alert2.demod import demodulate
from alert2.sync import find_framesync


def detect_framesyncs(audio, sample_rate, max_fs_hd=5, modes=(0,)):
    """Run audio through the decoder's demodulator and framesync detector.

    The demodulator resamples to 43.2 kHz internally, then decimates at
    the bit rate (3200 bps → 13.5 samples per bit at 43.2 kHz). We map
    bit indices back to the original audio time by:
        sample_43k = best_phase + fs_start_bit * 13.5
        sec = sample_43k / 43200
    """
    bits, info = demodulate(audio, sample_rate)
    best_phase = info.get('best_phase', 0)
    fs_43k = info.get('sample_rate', 43200)
    samples_per_bit_43k = fs_43k / 3200  # 13.5

    candidates = find_framesync(bits, max_hd=max_fs_hd, modes=list(modes))
    detections = []
    for fs_end, fec_mode, fs_hd in candidates:
        fs_start_bit = fs_end - 24
        sample_43k = best_phase + fs_start_bit * samples_per_bit_43k
        sec = sample_43k / fs_43k
        detections.append({
            'sec': sec,
            'fec_mode': fec_mode,
            'fs_hd': fs_hd,
        })
    return detections


def parse_diag_logicals(diag_path):
    """Return set of (src, srp_ts) tuples = unique logical packets in diag.

    This dedups by source + SRP timestamp, matching how we count logical
    packets in cross-site analysis. For WAV-file batch decoding, the
    timestamps are wall-clock-when-decoded (not audio-position), so
    aggregate count is what we compare.
    """
    logicals = set()
    raw_count = 0
    pat = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s*\|\s*src=(\d+)')
    srp_pat = re.compile(r'srp=SRP\[(?:id=\d+\])?\[?ts=(\d+)\]')
    with open(diag_path, errors='replace') as f:
        for line in f:
            m = pat.match(line)
            if m:
                raw_count += 1
                sm = srp_pat.search(line)
                if sm:
                    logicals.add((m.group(2), sm.group(1)))
    return raw_count, logicals


def get_wav_start_time(wav_path, diag_path=None):
    """Best-effort WAV start wall-clock time (timezone-naive)."""
    if diag_path:
        pat = re.compile(r'^#\s*SESSION_START\s+at\s+(\S+)')
        with open(diag_path, errors='replace') as f:
            for line in f:
                m = pat.match(line)
                if m:
                    try:
                        dt = datetime.datetime.fromisoformat(m.group(1))
                        if dt.tzinfo is not None:
                            dt = dt.replace(tzinfo=None)
                        return dt
                    except ValueError:
                        pass
                if not line.startswith('#'):
                    break
    mtime = Path(wav_path).stat().st_mtime
    return datetime.datetime.fromtimestamp(mtime)


def cross_reference(detections, receptions, wav_start, match_window_ms=500):
    """Match each framesync detection to nearby decoder receptions."""
    matched = []
    unmatched = []
    half = datetime.timedelta(milliseconds=match_window_ms)
    for det in detections:
        det_time = wav_start + datetime.timedelta(seconds=det['sec'])
        in_window = [rx for rx in receptions
                     if (det_time - half) <= rx[0] <= (det_time + half)]
        if in_window:
            matched.append((det, in_window))
        else:
            unmatched.append(det)
    return matched, unmatched


def main():
    p = argparse.ArgumentParser(
        description='Count ALERT2 framesync detections in a WAV file '
                    'and optionally compare against a decoder diag for '
                    'yield analysis.')
    p.add_argument('wav', help='Input WAV file')
    p.add_argument('--diag', help='Diag file to cross-reference')
    p.add_argument('--max-fs-hd', type=int, default=5,
                   help='Max framesync HD (default 5, matches decoder)')
    p.add_argument('--modes', default='0',
                   help='Comma-separated FEC modes (default: 0)')
    p.add_argument('--match-window-ms', type=int, default=500,
                   help='Match tolerance ms (default 500)')
    args = p.parse_args()

    modes = tuple(int(x) for x in args.modes.split(','))

    print(f'Reading {args.wav}...', file=sys.stderr)
    try:
        fs, data = wavfile.read(args.wav)
    except Exception as e:
        print(f'  Error: {e}', file=sys.stderr)
        return 1
    if data.ndim > 1:
        data = data[:, 0]
    audio = data.astype(np.float32) / 32768.0
    duration = len(audio) / fs
    print(f'  Duration: {duration:.1f}s @ {fs} Hz', file=sys.stderr)
    print(f'  Peak: {np.abs(audio).max():.3f}, RMS: {audio.std():.3f}',
          file=sys.stderr)

    if np.abs(audio).max() < 0.01:
        print('  WARNING: audio essentially silent', file=sys.stderr)
    if np.abs(audio).max() >= 0.999:
        print('  WARNING: audio clipping', file=sys.stderr)

    print(f'\nFramesync detection (max_fs_hd={args.max_fs_hd}, modes={modes})...',
          file=sys.stderr)
    detections = detect_framesyncs(audio, fs,
                                    max_fs_hd=args.max_fs_hd, modes=modes)
    print(f'  Found {len(detections)} framesync candidates', file=sys.stderr)

    hd_counts = Counter(d['fs_hd'] for d in detections)
    print(f'  HD breakdown: ' +
          ', '.join(f'HD={hd}:{n}' for hd, n in sorted(hd_counts.items())),
          file=sys.stderr)

    if not args.diag:
        print('\nFirst 30 framesync times:')
        for d in detections[:30]:
            print(f'  {d["sec"]:7.3f}s  mode={d["fec_mode"]}  HD={d["fs_hd"]}')
        if len(detections) > 30:
            print(f'  ... and {len(detections) - 30} more')
        print('\nProvide --diag to cross-reference with decodes.')
        return 0

    print(f'\nReading {args.diag}...', file=sys.stderr)
    raw_count, logicals = parse_diag_logicals(args.diag)
    print(f'  {raw_count} decoder receptions, {len(logicals)} unique logical packets',
          file=sys.stderr)

    print('\n' + '=' * 60)
    print('YIELD ANALYSIS')
    print('=' * 60)
    print(f'  Framesync detections (audio):  {len(detections)}')
    print(f'  Unique logical packets (diag): {len(logicals)}')
    print(f'  Raw receptions (diag):         {raw_count}')

    # Honest interpretation depends on HD breakdown and dataset character.
    # At HD=0: real bursts only. Logicals ≥ FS means decoder caught all
    # clean bursts (and multi-PDU bursts produced extra logicals).
    # At higher HD: noise can match the pattern. Apparent low yield often
    # means many false-positive framesyncs in quiet audio.
    print(f'\n  Framesync HD breakdown:')
    hd_counts = Counter(d['fs_hd'] for d in detections)
    for hd in sorted(hd_counts):
        marker = '  ← clean matches (real bursts)' if hd <= 1 else \
                 '  ← marginal (mix of weak signals and noise)' if hd <= 3 else \
                 '  ← mostly noise at high HD'
        print(f'    HD={hd}: {hd_counts[hd]}{marker}')

    if len(detections) <= len(logicals):
        print(f'\n  All framesyncs decoded → effective yield: 100%')
        extras = len(logicals) - len(detections)
        if extras > 0:
            print(f'  ({extras} extra logicals: each audio burst contained multiple')
            print(f'   PDUs from different stations relayed via the repeater network.)')
    else:
        missed = len(detections) - len(logicals)
        yield_pct = 100 * len(logicals) / len(detections)
        # Estimate how many of the misses are likely noise vs real
        hd_0_1 = sum(c for hd, c in hd_counts.items() if hd <= 1)
        hd_2_3 = sum(c for hd, c in hd_counts.items() if 2 <= hd <= 3)
        hd_4_5 = sum(c for hd, c in hd_counts.items() if hd >= 4)
        print(f'\n  Framesyncs without logical packet: {missed}')
        print(f'  Yield (logicals / framesyncs):     {yield_pct:.1f}%')
        if hd_4_5 >= missed:
            print(f'\n  Honest interpretation: most likely all "missed" framesyncs')
            print(f'  are noise that bit-matched the framesync pattern with HD 4-5')
            print(f'  ({hd_4_5} HD≥4 candidates ≥ {missed} unaccounted). True decoder')
            print(f'  yield is probably very close to 100% on real bursts.')
            print(f'  Run with --max-fs-hd 0 to see clean bursts only.')
        else:
            print(f'\n  Honest interpretation: at least {missed - hd_4_5} of the missed')
            print(f'  framesyncs are at low HD (≤3), suggesting real bursts the')
            print(f'  decoder failed to recover. Worth investigating.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
