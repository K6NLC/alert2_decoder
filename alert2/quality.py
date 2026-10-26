# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Signal quality measurement for ALERT2 SDR captures.

Provides Received Signal Level (RSL) / SNR estimates by exploiting the
"FM quieting" effect of the SDR's FM-demodulated audio output:

  - When NO signal is present, the FM discriminator outputs high-amplitude
    noise (envelope ~0.05-0.10 typical)
  - When a strong signal IS present, the FM discriminator locks and the
    output envelope drops substantially (envelope ~0.02-0.04 typical)

The dB difference between these levels gives a useful relative SNR
measurement. It is NOT calibrated dBm — for that, you'd need a reference
signal source. But it correlates strongly with link quality and is very
useful for:

  - Per-packet quality reporting
  - Per-source comparison (which sites have weak signal?)
  - Trend monitoring (is reception getting worse?)
  - Correlation with RS error corrections

The measurement is deliberately simple: RMS amplitude of the audio in two
windows (before and during the packet's framesync detection point), with
the ratio reported in dB.
"""
import numpy as np
from typing import Optional, Tuple
from .constants import RX_SAMPLE_RATE, SAMPLES_PER_BIT


# Default measurement windows (in milliseconds)
NOISE_WINDOW_MS = 80     # How much pre-burst audio to use for noise floor
SIGNAL_WINDOW_MS = 80    # How much in-burst audio to use for signal level
NOISE_GAP_MS = 50        # Minimum gap before framesync we want to be in "noise"


def measure_packet_snr(
    audio_43k: np.ndarray,
    bit_phase: int,
    framesync_end_bit: int,
    sample_rate: int = RX_SAMPLE_RATE,
    samples_per_bit: int = SAMPLES_PER_BIT,
    noise_window_ms: int = NOISE_WINDOW_MS,
    signal_window_ms: int = SIGNAL_WINDOW_MS,
    noise_gap_ms: int = NOISE_GAP_MS,
) -> Optional[dict]:
    """Estimate signal quality for a single packet.

    Args:
        audio_43k: the resampled audio at 43.2 kHz (post-resample, pre-MF)
        bit_phase: starting sample offset within audio_43k for bit clock
        framesync_end_bit: bit index where framesync ended (start of payload)
        sample_rate: receiver sample rate (43.2 kHz)
        samples_per_bit: 9 at 43.2 kHz / 4800 baud
        noise_window_ms: duration of pre-burst noise measurement window
        signal_window_ms: duration of in-burst signal measurement window
        noise_gap_ms: gap between noise window end and burst start

    Returns dict with:
        'noise_rms': RMS of pre-burst audio
        'signal_rms': RMS of in-burst audio
        'snr_db': SNR in dB (positive = signal cleaner than noise floor,
                  i.e. FM quieting active)
        'measurement_valid': True if both windows were inside the audio buffer
    Returns None if the windows can't be placed.
    """
    n_samples = len(audio_43k)

    # Convert bit positions to sample indices in the 43.2 kHz audio
    # bit_idx 0 corresponds to sample bit_phase
    # bit_idx N corresponds to sample (bit_phase + N * samples_per_bit)
    framesync_end_sample = bit_phase + framesync_end_bit * samples_per_bit

    # Convert windows to sample counts
    noise_win_samples = int(noise_window_ms * sample_rate / 1000)
    signal_win_samples = int(signal_window_ms * sample_rate / 1000)
    gap_samples = int(noise_gap_ms * sample_rate / 1000)

    # Framesync end is in the middle of a burst; payload starts right after
    # Signal window: just after framesync, in payload data
    # Note: framesync is preceded by ~30-50 ms of preamble (AGC + bitsync)
    # so we back up a bit from framesync_end to find the burst start
    bitsync_bits = 48
    framesync_bits = 32
    preamble_offset = (bitsync_bits + framesync_bits) * samples_per_bit
    burst_start_sample = framesync_end_sample - preamble_offset
    # Add ~30 ms more for AGC tone preamble before bitsync
    agc_samples = int(30 * sample_rate / 1000)
    burst_start_sample -= agc_samples

    # Noise window: ends `gap_samples` before burst start
    noise_end = burst_start_sample - gap_samples
    noise_start = noise_end - noise_win_samples

    # Signal window: starts at framesync_end (well into the burst)
    signal_start = framesync_end_sample
    signal_end = signal_start + signal_win_samples

    # Bounds check
    if noise_start < 0:
        # Not enough pre-burst audio — fall back to whatever we have
        noise_start = 0
    if signal_end > n_samples:
        signal_end = n_samples
    if noise_end <= noise_start or signal_end <= signal_start:
        return None

    noise_seg = audio_43k[noise_start:noise_end]
    signal_seg = audio_43k[signal_start:signal_end]

    if len(noise_seg) < 100 or len(signal_seg) < 100:
        return None

    noise_rms = float(np.sqrt(np.mean(noise_seg.astype(np.float64) ** 2)))
    signal_rms = float(np.sqrt(np.mean(signal_seg.astype(np.float64) ** 2)))

    # FM quieting: low signal_rms during burst is good
    # SNR_dB = 20 * log10(noise / signal)
    if signal_rms <= 0 or noise_rms <= 0:
        return None
    snr_db = 20.0 * np.log10(noise_rms / signal_rms)

    return {
        'noise_rms': noise_rms,
        'signal_rms': signal_rms,
        'snr_db': float(snr_db),
        'measurement_valid': (noise_start > 0
                              and signal_end < n_samples),
        'noise_window_samples': len(noise_seg),
        'signal_window_samples': len(signal_seg),
    }


def estimate_global_noise_floor(audio_43k: np.ndarray,
                                  sample_rate: int = RX_SAMPLE_RATE) -> float:
    """Estimate the global noise floor RMS in the audio buffer.

    Uses the highest-amplitude regions (which represent FM noise without
    signal — the OPPOSITE of typical "noise floor" calculations because
    of FM quieting).

    Returns RMS of the upper quartile of windowed envelopes.
    """
    if len(audio_43k) < sample_rate // 10:
        return 0.0
    # Compute envelope in 50ms windows
    win = sample_rate // 20  # 50ms
    n_wins = len(audio_43k) // win
    envs = []
    for i in range(n_wins):
        seg = audio_43k[i * win : (i + 1) * win]
        envs.append(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
    if not envs:
        return 0.0
    envs = np.array(envs)
    # Upper quartile = noise (since signal quiets the audio)
    return float(np.percentile(envs, 75))


# ---------------------------------------------------------------------------
# Modulation Error Ratio (MER) — the SINAD analogue for a data burst
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS, alongside measure_packet_snr():
#
# measure_packet_snr() is an FM-quieting LEVEL ratio: pre-burst RMS over
# in-burst RMS. Three limitations show up in the field:
#
#   1. It scales with capture gain. The same antenna feeding two machines
#      reported median -0.12 dB and +7.13 dB on 2026-07-26 — a ~7 dB scale
#      offset, not a real difference in link quality. The two numbers are
#      not comparable across sites.
#   2. It needs ~130 ms of pre-burst audio (80 ms window + 50 ms gap).
#      That is unavailable at chunk boundaries and structurally absent in
#      burst mode, so ~10% of packets report SNR=?.
#   3. It measures LEVEL, not distortion. It is blind to ISI, which is the
#      impairment the equalizer ladder actually fights.
#
# Classical SINAD (1 kHz tone through a notch filter) does not port: there
# is no tone, only 4800 baud data. The correct analogue for a demodulated
# data waveform is MER, computed against the KNOWN transmitted symbols.
# Because a decoded frame can be re-encoded through the exact transmit path
# (scramble -> RS -> CC), the reference sequence is exact, not estimated.
#
#   alpha = sum(y*a) / sum(a*a)          least-squares scale
#   e     = y - (alpha*a + beta)
#   MER   = 10*log10( sum((alpha*a)^2) / sum(e^2) )
#
# Properties that fix the three limitations above:
#   - amplitude-normalised (alpha absorbs capture gain) -> cross-site
#     comparable
#   - measured entirely INSIDE the burst -> works in burst mode, no
#     pre-burst audio needed
#   - residual contains noise + distortion + ISI + timing error, which is
#     what SINAD is meant to capture
#
# HONESTY CONSTRAINTS (both enforced below):
#   - The reference MUST be re-encoded decoded bits. Hard decisions
#     (sign(y)) make the residual collapse toward zero at low SNR and would
#     overstate quality on exactly the marginal packets that matter. This
#     function will not accept a hard-decision reference.
#   - When no valid reference exists (failed or partial decode), return
#     None. Callers emit nothing rather than a fallback estimate.
#
# This is a pure observation. It is computed after acceptance and feeds no
# decode, gating, or filtering decision.

MER_MIN_SYMBOLS = 64      # below this the estimate is too noisy to report
MER_MAX_DB = 60.0         # clamp: a perfect fit would divide by ~0


def measure_symbol_mer(raw_soft,
                       segments,
                       remove_dc: bool = True) -> Optional[dict]:
    """Modulation Error Ratio of soft symbols against known transmitted bits.

    Args:
        raw_soft: per-symbol soft values (matched-filter output), the same
                  array the equalizer trains on. Index-aligned with the
                  transmitted symbol stream.
        segments: list of (start, known_bits) pairs. `start` is the index in
                  raw_soft of the first symbol of that run. Multiple segments
                  exist because the first block and the follow-on stream are
                  separately CC-encoded and are divided by a few pad symbols
                  whose transmitted value is NOT known — those are excluded
                  rather than guessed, since a guess would be charged to the
                  error term and understate quality.
                  known_bits must be TRANSMITTED bits, re-encoded from
                  decoded data through scramble -> RS -> CC. They must NOT
                  be hard decisions taken from raw_soft itself.
        remove_dc: fit and remove a DC offset as well as a scale. The FM
                   discriminator can carry a small bias; leaving it in
                   would be charged to the error term.

    One scale is fitted jointly across all segments, which is correct
    because capture gain is constant over a burst, and gives the pad region
    no influence at all.

    Returns dict with:
        'mer_db':       10*log10(signal power / residual power)
        'alpha':        fitted symbol amplitude (capture-gain dependent)
        'dc':           fitted DC offset (0.0 if remove_dc is False)
        'n_symbols':    symbols the fit ran over
        'evm_pct':      100 * sqrt(residual power / signal power)
    Returns None if no segment can be placed against raw_soft, if fewer
    than MER_MIN_SYMBOLS symbols are usable, or if the fit degenerates.
    """
    if raw_soft is None or not segments:
        return None
    total = len(raw_soft)
    ys, as_ = [], []
    for start, known_bits in segments:
        if known_bits is None:
            continue
        n = len(known_bits)
        if n == 0 or start < 0 or start + n > total:
            continue
        seg = np.asarray(raw_soft[start:start + n], dtype=np.float64)
        if not np.all(np.isfinite(seg)):
            continue
        ys.append(seg)
        as_.append(np.where(np.asarray(known_bits, dtype=np.int8) != 0,
                            1.0, -1.0))
    if not ys:
        return None
    y = np.concatenate(ys)
    a = np.concatenate(as_)
    if len(y) < MER_MIN_SYMBOLS or not np.any(y):
        return None

    if remove_dc:
        # Joint LS fit of y ~ alpha*a + dc. Because a is +/-1 and roughly
        # balanced, this is a 2x2 solve; done explicitly to stay cheap.
        ma, my = a.mean(), y.mean()
        var_a = np.mean(a * a) - ma * ma
        if var_a <= 1e-12:
            return None                      # reference is all one symbol
        alpha = (np.mean(a * y) - ma * my) / var_a
        dc = my - alpha * ma
    else:
        denom = float(np.dot(a, a))
        if denom <= 0:
            return None
        alpha = float(np.dot(a, y)) / denom
        dc = 0.0

    if not np.isfinite(alpha) or alpha == 0.0:
        return None

    err = y - (alpha * a + dc)
    sig_pow = float(np.dot(alpha * a, alpha * a))
    err_pow = float(np.dot(err, err))
    if sig_pow <= 0:
        return None
    if err_pow <= 0:
        mer_db, evm = MER_MAX_DB, 0.0
    else:
        mer_db = float(min(MER_MAX_DB, 10.0 * np.log10(sig_pow / err_pow)))
        evm = float(100.0 * np.sqrt(err_pow / sig_pow))

    return {
        'mer_db': mer_db,
        'alpha': float(abs(alpha)),
        'dc': float(dc),
        'n_symbols': int(len(y)),
        'evm_pct': evm,
    }
