# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""I/Q (raw SDR) input: channel survey, FM discrimination, per-burst
carrier correction, then the ordinary decode chain.

WHY
---
The audio path depends on the SDR software having centred the carrier
before it demodulated. When it had not (QTH 2026-10-28, three recordings
with the carrier at the edge of the channel filter), the discriminator
output carried a -0.4 FS DC step and no FM quieting, the signal sat
~10 dB under the noise, and nothing could be recovered afterwards. With
I/Q the demodulation happens here, so the carrier offset is still in the
data to be measured and removed, per burst.

HOW
---
1. read_iq       -- GQRX `_fc.raw` (complex float32), rtl_sdr `.cu8`,
                    `.cs16`/`.cs8`, or a stereo WAV with I in L and Q in R.
                    Rate and centre frequency come from the GQRX filename
                    when present (`gqrx_YYYYMMDD_HHMMSS_<Hz>_<rate>_fc.raw`).
2. survey        -- short-time spectrum; a channel is a run of bins whose
                    99.5th-percentile power stands well above its median
                    (bursty), merged within one channel width. Returns
                    offsets from the tuned centre, strongest first.
3. channelize    -- mix the offset to 0 Hz, low-pass, decimate to 48 kHz.
                    Channel filter default +/-4.5 kHz: measured on a weak
                    QTH capture, 3.5-5.5 kHz decoded 4/4 bursts, 7 kHz 2/4,
                    9 kHz 0/4 (the wider filter admits noise that costs
                    quieting at threshold).
4. discriminate  -- arg(y[n] * conj(y[n-1])) / pi, i.e. the same baseband
                    a radio's discriminator output gives, 1.0 = fs/2.
5. decode        -- the resulting 48 kHz audio goes through decode_wav's
                    chain unchanged (multiphase, phase-select, rate-track).
6. burst refine  -- each burst the chain detects is re-channelized on its
                    own carrier (mean discriminator value over its first
                    block = its frequency offset) with the narrow filter and
                    decoded again; best-wins keeps the better copy. Handles
                    a shared channel whose sites have different crystals.

Each packet carries `carrier_hz`: the absolute carrier of the burst it
came from, when the centre frequency is known.
"""
import os
import re
from fractions import Fraction
from typing import List, Optional, Tuple

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

AUDIO_RATE = 48000
DEFAULT_CHANNEL_BW = 4500.0      # Hz, one-sided
SURVEY_BW = 10000.0              # first-pass channel half-width
SURVEY_BIN_HZ = 200.0
SURVEY_MIN_RATIO_DB = 11.0       # p99.5 / median per bin; noise reaches ~9.4 dB

_GQRX = re.compile(r'gqrx_\d{8}_\d{6}_(\d+)_(\d+)_fc')


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def parse_gqrx_name(path: str) -> Optional[Tuple[int, int]]:
    """(center_hz, sample_rate) from a GQRX I/Q recorder filename."""
    m = _GQRX.search(os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else None


def read_iq(path: str, fmt: Optional[str] = None,
            sample_rate: Optional[int] = None,
            center_hz: Optional[float] = None
            ) -> Tuple[np.ndarray, int, Optional[float]]:
    """Load I/Q as complex64. Returns (samples, sample_rate, center_hz).

    fmt: 'fc32' (GQRX), 'cu8' (rtl_sdr), 'cs8', 'cs16', 'wav' (stereo I/Q)
         or None to infer from the filename.
    """
    g = parse_gqrx_name(path)
    if g:
        center_hz = center_hz if center_hz is not None else float(g[0])
        sample_rate = sample_rate or g[1]
        fmt = fmt or 'fc32'
    ext = os.path.splitext(path)[1].lower()
    if fmt is None:
        fmt = {'.cu8': 'cu8', '.cs8': 'cs8', '.cs16': 'cs16', '.wav': 'wav',
               '.cf32': 'fc32', '.fc32': 'fc32', '.raw': 'fc32',
               '.iq': 'fc32'}.get(ext, 'fc32')
    if fmt == 'wav':
        import scipy.io.wavfile as wavio
        fs, d = wavio.read(path)
        if d.ndim != 2 or d.shape[1] < 2:
            raise ValueError("I/Q WAV must be stereo (I=left, Q=right)")
        if d.dtype.kind == 'i':
            d = d.astype(np.float32) / float(np.iinfo(d.dtype).max)
        x = (d[:, 0] + 1j * d[:, 1]).astype(np.complex64)
        sample_rate = sample_rate or int(fs)
    elif fmt == 'cu8':
        r = np.fromfile(path, dtype=np.uint8).astype(np.float32)
        r = (r - 127.5) / 127.5
        x = (r[0::2] + 1j * r[1::2]).astype(np.complex64)
    elif fmt == 'cs8':
        r = np.fromfile(path, dtype=np.int8).astype(np.float32) / 128.0
        x = (r[0::2] + 1j * r[1::2]).astype(np.complex64)
    elif fmt == 'cs16':
        r = np.fromfile(path, dtype='<i2').astype(np.float32) / 32768.0
        x = (r[0::2] + 1j * r[1::2]).astype(np.complex64)
    elif fmt == 'fc32':
        x = np.fromfile(path, dtype=np.complex64)
    else:
        raise ValueError(f"unknown I/Q format {fmt!r}")
    if not sample_rate:
        raise ValueError("I/Q sample rate unknown: pass sample_rate "
                         "(--iq-rate) or use the GQRX filename")
    return x, int(sample_rate), center_hz


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------

def survey(x: np.ndarray, fs: int, min_ratio_db: float = SURVEY_MIN_RATIO_DB,
           channel_bw: float = SURVEY_BW, max_channels: int = 6
           ) -> List[Tuple[float, float]]:
    """Find bursty channels. Returns [(offset_hz, score_db), ...], best
    first. Offsets are relative to the tuned centre (0 Hz)."""
    N = int(round(fs / SURVEY_BIN_HZ))
    N = max(64, N - N % 2)
    nf = len(x) // N
    if nf < 20:
        return [(0.0, 0.0)]
    frames = x[:nf * N].reshape(nf, N)
    w = np.hanning(N).astype(np.float32)
    S = np.abs(np.fft.fft(frames * w, axis=1)) ** 2
    S = np.fft.fftshift(S, axes=1)
    fr = np.fft.fftshift(np.fft.fftfreq(N, 1.0 / fs))
    med = np.median(S, axis=0) + 1e-20
    hi = np.percentile(S, 99.5, axis=0)
    ratio = 10 * np.log10(hi / med)
    # ignore the outer 10% (anti-alias roll-off) and the DC bin
    guard = np.abs(fr) > 0.45 * fs
    ratio[guard] = 0
    ratio[np.abs(fr) < SURVEY_BIN_HZ] = 0
    cands = []
    r = ratio.copy()
    for _ in range(max_channels):
        i = int(np.argmax(r))
        if r[i] < min_ratio_db:
            break
        m = np.abs(fr - fr[i]) <= channel_bw
        # centroid of the bursty bins in the window weights the carrier
        wgt = np.clip(r[m] - min_ratio_db / 2, 0, None)
        off = float(np.sum(fr[m] * wgt) / max(np.sum(wgt), 1e-9))
        cands.append((off, float(r[i])))
        r[m] = 0
    return cands or [(0.0, 0.0)]


# ---------------------------------------------------------------------------
# Channelize + discriminate
# ---------------------------------------------------------------------------

def _resample(y: np.ndarray, fs_in: int, fs_out: int) -> np.ndarray:
    fr = Fraction(fs_out, fs_in).limit_denominator(1000)
    return resample_poly(y, fr.numerator, fr.denominator)


def channelize(x: np.ndarray, fs: int, offset_hz: float,
               bw: float = DEFAULT_CHANNEL_BW, fs_out: int = AUDIO_RATE,
               start: int = 0, stop: Optional[int] = None) -> np.ndarray:
    """Mix `offset_hz` to 0, low-pass to +/-bw, decimate to fs_out.
    Operates on x[start:stop] (phase-continuous mixing)."""
    seg = x[start:stop]
    n = np.arange(start, start + len(seg), dtype=np.float64)
    y = seg * np.exp(-2j * np.pi * offset_hz * n / fs).astype(np.complex64)
    # anti-alias at the intermediate rate, then decimate in one step
    taps = int(min(801, max(101, 4 * fs / bw)))
    taps |= 1
    h = firwin(taps, bw / (fs / 2.0))
    y = lfilter(h, 1.0, y)
    return _resample(y, fs, fs_out)


def discriminate(y: np.ndarray) -> np.ndarray:
    """FM discriminator. 1.0 = half the sample rate of `y`."""
    d = np.angle(y[1:] * np.conj(y[:-1]))
    return (d / np.pi).astype(np.float32)


def iq_to_audio(x: np.ndarray, fs: int, offset_hz: float,
                bw: float = DEFAULT_CHANNEL_BW) -> np.ndarray:
    """Baseband audio at AUDIO_RATE for the channel at `offset_hz`."""
    return discriminate(channelize(x, fs, offset_hz, bw))


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def burst_carrier_offset(audio: np.ndarray, fs_audio: int, t_start_s: float,
                         n_symbols: int = 736) -> float:
    """Frequency offset (Hz) of a burst from the mean discriminator value
    over its preamble + first block. 1.0 in `audio` = fs_audio/2."""
    a = int(t_start_s * fs_audio)
    b = a + int(n_symbols * fs_audio / 4800.0)
    seg = audio[max(0, a):b]
    if len(seg) < 100:
        return 0.0
    return float(np.mean(seg)) * fs_audio / 2.0


def decode_iq(path: str, channel_bw: float = DEFAULT_CHANNEL_BW,
              offset_hz: Optional[float] = None,
              fmt: Optional[str] = None, sample_rate: Optional[int] = None,
              center_hz: Optional[float] = None, max_channels: int = 6,
              burst_refine: bool = True, verbose=None, **decode_kw):
    """Decode every ALERT2 channel found in an I/Q file.

    Returns (packets, report) where report lists the channels tried with
    their offsets and packet counts. `decode_kw` goes to decode_wav's
    underlying decoder (soft_viterbi, multiphase, rate_track, ...).
    """
    from .decoder import decode_wav_array
    x, fs, center = read_iq(path, fmt, sample_rate, center_hz)
    x = x - np.mean(x)                       # SDR DC spike
    if offset_hz is not None:
        chans = [(float(offset_hz), 0.0)]
    else:
        chans = survey(x, fs, max_channels=max_channels)
    report = {'sample_rate': fs, 'center_hz': center, 'duration_s':
              len(x) / fs, 'channels': []}
    packets = []
    seen = {}
    for off, score in chans:
        audio = iq_to_audio(x, fs, off, channel_bw)
        pk = decode_wav_array(audio, AUDIO_RATE, **decode_kw)
        if burst_refine:
            pk = _refine_bursts(x, fs, off, channel_bw, audio, pk,
                                decode_kw)
        for p in pk:
            if not hasattr(p, '_burst_off'):
                # first-pass packet: measure its own burst's offset from
                # the discriminator mean at the frame position
                t0 = (p.sample_offset - 80) / 4800.0
                p._burst_off = burst_carrier_offset(audio, AUDIO_RATE, t0)
            p.carrier_hz = (center + off + p._burst_off
                            if center is not None else off + p._burst_off)
            key = (p.fec_mode, bytes(p.raw_payload))
            cur = seen.get(key)
            if cur is None or (cur.truncated and not p.truncated) or (
                    (p.mer_db or -99) > (cur.mer_db or -99)):
                seen[key] = p
        n_here = len([p for p in pk])
        report['channels'].append({'offset_hz': off, 'score_db': score,
                                   'packets': n_here})
        if verbose:
            verbose(f"I/Q channel {off:+.0f} Hz (score {score:.1f} dB): "
                    f"{n_here} packets")
    packets = list(seen.values())
    packets.sort(key=lambda p: p.rx_time)
    return packets, report


def _refine_bursts(x, fs, off, bw, audio, first_pass, decode_kw):
    """Second pass: re-channelize each detected burst on its own carrier."""
    from .burst_detect import detect_bursts
    from .constants import SAMPLES_PER_BIT as SPB
    from .decoder import decode_wav_array
    from .demod import demodulate
    try:
        _bits, info = demodulate(audio, AUDIO_RATE)
        filt = np.asarray(info['filtered_43k'], dtype=float)
        bursts = detect_bursts(filt, list(range(SPB)), 4, [0, 1, 2],
                               min_corr=0.40)
    except Exception:
        return first_pass
    out = list(first_pass)
    for bc in bursts:
        t0 = (bc.abs_sample - 80 * SPB) / 43200.0
        d_off = burst_carrier_offset(audio, AUDIO_RATE, t0)
        if abs(d_off) < 50:
            continue                       # already centred within noise
        a = int(max(0, (t0 - 0.05) * fs))
        b = int(min(len(x), (t0 + 6.0) * fs))   # spec max frame 5.2 s
        if b - a < int(0.2 * fs):
            continue
        seg_audio = discriminate(channelize(x, fs, off + d_off, bw,
                                            start=a, stop=b))
        pk = decode_wav_array(seg_audio, AUDIO_RATE, **decode_kw)
        for p in pk:
            p.decode_path = 'iq-refined'
            p._burst_off = d_off
            out.append(p)
    return out
