# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 demodulation: audio (or rtl_fm output) -> bit stream.

Pipeline per AirLink Spec Figure 1-2:
    audio -> bandpass -> FM discriminate -> resample to 43.2 kHz
          -> RRC matched filter -> bit-clock recovery -> bit decisions
"""
import numpy as np
import scipy.signal as sig
from typing import List, Tuple, Optional
from .constants import (
    BAUD_RATE, RX_SAMPLE_RATE, SAMPLES_PER_BIT, RRC_TAPS,
)


def fm_discriminate(audio: np.ndarray, fs: int) -> np.ndarray:
    """FM-discriminate an audio signal using Hilbert transform.

    Input: real-valued audio samples (typically from a soundcard, WAV file,
           or rtl_fm output).
    Output: instantaneous frequency in Hz (real-valued, baseband).

    For a clean signal already at baseband (e.g., from rtl_fm in FM mode),
    this is mostly identity but removes any residual DC.
    """
    # Bandpass to FSK band (preserves all FSK content)
    bp_b, bp_a = sig.butter(6, [400, 4500], btype='band', fs=fs)
    audio_bp = sig.filtfilt(bp_b, bp_a, audio)

    # FM discriminate via Hilbert transform
    # inst_freq[n] = (1/(2*pi)) * d/dt arg(z(t))
    z = sig.hilbert(audio_bp)
    inst_freq = np.diff(np.unwrap(np.angle(z))) * fs / (2 * np.pi)
    # Pad to original length
    inst_freq = np.concatenate([[inst_freq[0]], inst_freq])

    # Remove slow DC component
    lp_b, lp_a = sig.butter(2, 30, btype='low', fs=fs)
    dc_drift = sig.filtfilt(lp_b, lp_a, inst_freq)
    return inst_freq - dc_drift


def resample_to_rx_rate(signal: np.ndarray, fs_in: int) -> np.ndarray:
    """Resample to the spec's recommended 43.2 kHz (9 sps at 4800 baud).

    Uses polyphase filtering for clean, fast resampling.
    """
    if fs_in == RX_SAMPLE_RATE:
        return signal
    # Find rational ratio
    from math import gcd
    g = gcd(int(RX_SAMPLE_RATE), int(fs_in))
    up = RX_SAMPLE_RATE // g
    down = int(fs_in) // g
    return sig.resample_poly(signal, up=up, down=down)


def matched_filter(baseband: np.ndarray) -> np.ndarray:
    """Apply the spec's RRC matched filter.

    Input must be sampled at RX_SAMPLE_RATE (43.2 kHz).
    """
    return np.convolve(baseband, RRC_TAPS, mode='same')


def find_best_phase(filtered: np.ndarray, n_test: int = 8000,
                    block_scan: bool = False) -> int:
    """Find the best symbol-clock phase by maximizing variance.

    At the correct sampling phase, samples sit at the eye-diagram peaks
    where the values are most extreme (highest variance from zero).

    block_scan (rc23, OPT-IN — see the superset note below): scan
    the WHOLE window in blocks and
    score each phase by its strongest block, instead of judging on the
    first `n_test` samples alone.

    The old behaviour looked only at the leading 8000 samples — 0.185 s
    at 43.2 kHz, which for a 6-second streaming chunk is the first 3%
    and at ALERT2 traffic density is almost always pure noise. The
    phase for every burst in the window was therefore fitted to noise,
    which is what `--multiphase` has been brute-forcing around.

    Scanning by block works because the phase-dependence of variance
    comes only from signal: white noise contributes equally at every
    phase, so it cancels out of the comparison, while a burst produces
    a strong peak at its own correct phase. Taking the max over blocks
    (rather than the mean over the window) means the estimate is drawn
    from wherever the transmitted audio actually is, and several bursts
    at different phases no longer average each other away.

    NOT superset-safe on its own. Measured on real captures: SDR 515
    gained 10 logical packets and lost 5 (net +5); QTH 6-13 lost 1.
    Changing which single phase a window uses reshuffles which bursts
    it happens to serve. No single per-window phase can be a superset
    of another — that is the argument for per-burst phase estimation
    (--burst-mode), which is additive by construction. Use block_scan
    only alongside burst-mode or multiphase, which cover the residual.

    Set block_scan=False (the default) for legacy leading-window
    behaviour.
    """
    n = len(filtered)
    if not block_scan or n <= n_test:
        test = filtered[:min(n_test, n)]
        best_phase = 0
        best_var = -1.0
        for phase in range(SAMPLES_PER_BIT):
            v = np.var(test[phase::SAMPLES_PER_BIT])
            if v > best_var:
                best_var = v
                best_phase = phase
        return best_phase

    # Block length in decimated samples (~n_test input samples each).
    blk = max(32, n_test // SAMPLES_PER_BIT)
    best_phase = 0
    best_score = -1.0
    for phase in range(SAMPLES_PER_BIT):
        dec = filtered[phase::SAMPLES_PER_BIT]
        m = len(dec)
        if m == 0:
            continue
        nb = max(1, m // blk)
        score = -1.0
        for i in range(nb):
            seg = dec[i * blk:(i + 1) * blk] if i < nb - 1 else dec[i * blk:]
            if len(seg) < 32:
                continue
            v = float(np.var(seg))
            if v > score:
                score = v
        if score > best_score:
            best_score = score
            best_phase = phase
    return best_phase


def rank_phases(filtered: np.ndarray, n: int = 3,
                n_test: int = 8000) -> List[int]:
    """Return the top-`n` symbol-clock phases ranked by eye-opening.

    Variance of the decimated stream is a cheap proxy for how open the
    eye is at a given sampling phase (higher = sampling nearer the symbol
    peak). Instead of brute-forcing all SAMPLES_PER_BIT phases, rank them
    by this proxy and decode only the most promising `n`.

    Measured on a healthy-SNR capture: the top 3 phases recovered every
    complete packet that all 9 did, at ~1/3 the decode cost. The variance
    ranking is the principled selector — it estimates which phases are
    likely correct rather than trying them blindly.

    n is clamped to [1, SAMPLES_PER_BIT]. n>=SAMPLES_PER_BIT returns all
    phases (equivalent to exhaustive multiphase).
    """
    n = max(1, min(int(n), SAMPLES_PER_BIT))
    test = filtered[:min(n_test, len(filtered))]
    scored = [(float(np.var(test[ph::SAMPLES_PER_BIT])), ph)
              for ph in range(SAMPLES_PER_BIT)]
    scored.sort(reverse=True)
    return [ph for _, ph in scored[:n]]


def gardner_timing_recovery(filtered: np.ndarray,
                             initial_phase: int) -> Tuple[np.ndarray, np.ndarray]:
    """Gardner-style timing recovery for symbol-rate sampling.

    Tracks bit clock drift over the burst by adjusting symbol phase
    based on observed bit transitions.

    Returns (soft_samples, sample_indices).

    NOTE: This is implemented as a pure-Python loop. For very long signals
    it can be slow. For typical ALERT2 captures (tens of seconds at most)
    it runs in well under a second.
    """
    sps = SAMPLES_PER_BIT
    n = len(filtered)
    # Estimate output size for preallocation
    estimated_n = int(n / sps) + 10
    soft_samples = np.zeros(estimated_n, dtype=np.float32)
    sample_indices = np.zeros(estimated_n, dtype=np.float32)
    out_count = 0

    # Use fractional-sample tracking
    phase = float(initial_phase) + sps / 2.0
    alpha = 0.01  # Loop bandwidth

    prev = filtered[max(0, int(phase) - sps)]
    while phase < n - sps - 1:
        idx = int(phase)
        frac = phase - idx
        curr = filtered[idx] * (1 - frac) + filtered[idx + 1] * frac

        mid_phase = phase + sps / 2.0
        mid_idx = int(mid_phase)
        if mid_idx + 1 < n:
            mid_frac = mid_phase - mid_idx
            mid = filtered[mid_idx] * (1 - mid_frac) + filtered[mid_idx + 1] * mid_frac
        else:
            mid = 0

        soft_samples[out_count] = curr
        sample_indices[out_count] = phase
        out_count += 1
        if out_count >= estimated_n:
            # Reallocate
            soft_samples = np.concatenate([soft_samples,
                                            np.zeros(estimated_n, dtype=np.float32)])
            sample_indices = np.concatenate([sample_indices,
                                               np.zeros(estimated_n, dtype=np.float32)])
            estimated_n *= 2

        # Gardner timing error
        sign_curr = 1.0 if curr > 0 else -1.0
        sign_prev = 1.0 if prev > 0 else -1.0
        error = mid * (sign_curr - sign_prev)

        phase += sps + alpha * error
        prev = curr

    return soft_samples[:out_count], sample_indices[:out_count]


def slice_soft_samples(soft: np.ndarray, alpha: float = 0.005) -> List[int]:
    """Convert soft samples to hard bit decisions with adaptive threshold.

    Uses a slow IIR to track DC offset of the soft samples.
    """
    if len(soft) == 0:
        return []
    threshold = float(soft[:min(16, len(soft))].mean())
    bits = []
    for v in soft:
        bits.append(1 if v > threshold else 0)
        threshold = alpha * v + (1 - alpha) * threshold
    return bits


def _hum_highpass(audio: np.ndarray, fs: int, cutoff: float = 300.0) -> np.ndarray:
    """Remove mains-hum harmonics (120/180/240/300 Hz and below) ahead of
    demod.

    A 4th-order Butterworth high-pass at `cutoff` (default 300 Hz). ALERT2
    discriminator-audio FSK content sits at ~500 Hz and above, so a 300 Hz
    high-pass removes rectifier/ground-loop hum (>20 dB on measured TK790
    captures) while retaining >99% of the FSK band. Portable across ALERT2
    systems — it conditions the signal only, with no protocol assumptions.

    Applied to raw audio before resampling, so it benefits both the
    fixed-phase (soundcard/discriminator) and FM-discriminator paths.
    """
    if cutoff <= 0 or cutoff >= fs / 2:
        return audio
    b, a = sig.butter(4, cutoff, btype='high', fs=fs)
    return sig.filtfilt(b, a, audio)


def demodulate(audio: np.ndarray, fs: int,
               use_gardner: bool = False,
               phase_block_scan: bool = False,
               apply_fm_discrim: bool = False,
               hum_filter: bool = False,
               hum_cutoff: float = 300.0) -> Tuple[List[int], dict]:
    """Full demod pipeline.

    Returns (bits, info_dict).

    Args:
        audio: input samples (mono)
        fs: input sample rate
        use_gardner: if True, use Gardner timing recovery (slower but tracks
                     clock drift). If False, use fixed-phase decimation.
        apply_fm_discrim: if True, FM-discriminate the input first (use this
                          when input is raw RF audio that hasn't been
                          FM-demodulated). For input from rtl_fm or a radio's
                          discriminator output, leave this False — the input
                          is already baseband.

    info_dict contains diagnostic info.
    """
    # Optional mains-hum high-pass on the raw audio (pre-discrim, pre-resample).
    # Off by default; opt in via hum_filter=True (CLI: --hum-filter).
    if hum_filter:
        audio = _hum_highpass(audio, fs, hum_cutoff)

    # Optional FM discrimination (rarely needed — input is usually baseband)
    if apply_fm_discrim:
        bb = fm_discriminate(audio, fs)
    else:
        bb = audio - audio.mean()  # Just DC remove

    # Resample to 43.2 kHz (9 sps at 4800 baud)
    bb_43k = resample_to_rx_rate(bb, fs)

    # RRC matched filter
    filtered = matched_filter(bb_43k)

    # Find best symbol-clock phase
    phase = find_best_phase(filtered, block_scan=phase_block_scan)

    # Symbol-rate sampling
    if use_gardner:
        soft, _ = gardner_timing_recovery(filtered, phase)
    else:
        soft = filtered[phase::SAMPLES_PER_BIT]

    # Slice
    bits = slice_bits(soft)

    # Soft samples are index-aligned with `bits` (one soft value per bit,
    # same order). Downstream soft-Viterbi slices these in lockstep with
    # the hard bits at framesync offsets. We keep them as a plain list of
    # Python floats so the rest of the pipeline stays numpy-optional.
    soft_list = [float(x) for x in soft]
    from .fec import estimate_amplitude
    amp_est = estimate_amplitude(soft_list)

    info = {
        'sample_rate': RX_SAMPLE_RATE,
        'best_phase': phase,
        'baseband_std': float(np.std(bb_43k)),
        'filtered_std': float(np.std(filtered)),
        'n_soft_samples': len(soft),
        'apply_fm_discrim': apply_fm_discrim,
        # The resampled audio is exposed so downstream code can measure
        # signal quality (SNR) per packet. It is NOT serialized; this is an
        # in-memory reference only.
        'audio_43k': bb_43k,
        # Matched-filter output at 9 samples/symbol, BEFORE decimation.
        # Needed by the per-burst timing probe, which must see sub-symbol
        # detail that the decimated soft samples have already discarded.
        'filtered_43k': filtered,
        # Soft-decision support (see docs/SOFT_VITERBI.md). Optional and
        # ignorable: the hard path never touches these.
        'soft_samples': soft_list,
        'amplitude_estimate': amp_est,
    }
    return bits, info


def slice_bits(soft: np.ndarray) -> List[int]:
    """Public alias for slice_soft_samples."""
    return slice_soft_samples(soft)


def demodulate_simple(audio: np.ndarray, fs: int) -> Tuple[List[int], dict]:
    """Simpler demod path that doesn't use the matched filter.

    Useful for cross-checking or signals where the matched filter
    response is wrong (e.g., heavily filtered audio path).

    Pipeline: bandpass -> FM discriminate -> resample to 4800 Hz
              -> adaptive slice
    """
    # FM discriminate at original rate
    baseband = fm_discriminate(audio, fs)

    # Decimate to bit rate
    spb_in = fs / BAUD_RATE
    n_bits = int(len(baseband) / spb_in)
    soft = np.zeros(n_bits)
    for k in range(n_bits):
        i_lo = int(k * spb_in)
        i_hi = int((k + 1) * spb_in)
        soft[k] = baseband[i_lo:i_hi].mean()

    bits = slice_bits(soft)
    info = {
        'sample_rate': BAUD_RATE,
        'method': 'simple',
        'n_soft_samples': n_bits,
    }
    return bits, info
