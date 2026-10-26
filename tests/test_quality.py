# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the signal-quality (SNR) module."""
import sys
import os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from alert2.quality import (
    measure_packet_snr, estimate_global_noise_floor,
    NOISE_WINDOW_MS, SIGNAL_WINDOW_MS,
)
from alert2.constants import RX_SAMPLE_RATE, SAMPLES_PER_BIT


def make_synthetic_audio(duration_sec, noise_rms, signal_rms,
                          burst_start_sec, burst_duration_sec,
                          fs=RX_SAMPLE_RATE):
    """Build a synthetic 43.2 kHz audio buffer with a quieting region.

    Outside the burst: white noise at `noise_rms` amplitude (FM noise).
    Inside the burst: lower-amplitude noise at `signal_rms` (FM-quieted).

    Returns audio array.
    """
    n = int(duration_sec * fs)
    rng = np.random.default_rng(42)
    audio = rng.standard_normal(n).astype(np.float32) * noise_rms
    burst_start = int(burst_start_sec * fs)
    burst_end = burst_start + int(burst_duration_sec * fs)
    # Replace burst region with lower-amplitude noise
    burst_n = burst_end - burst_start
    audio[burst_start:burst_end] = (
        rng.standard_normal(burst_n).astype(np.float32) * signal_rms)
    return audio


def test_snr_measurement_basic():
    """Synthetic audio with known noise/signal ratio should yield expected dB."""
    # Make 3 sec of audio. Burst from 1.0 to 1.5 sec.
    # Noise RMS = 0.06, in-burst RMS = 0.02 -> ratio 3 -> ~9.5 dB
    audio = make_synthetic_audio(
        duration_sec=3.0,
        noise_rms=0.06, signal_rms=0.02,
        burst_start_sec=1.0, burst_duration_sec=0.5,
    )
    # Place framesync_end inside the burst, around t=1.1s
    framesync_end_time = 1.1
    framesync_end_bit = int(framesync_end_time * RX_SAMPLE_RATE
                              / SAMPLES_PER_BIT)
    result = measure_packet_snr(audio, bit_phase=0,
                                 framesync_end_bit=framesync_end_bit)
    assert result is not None
    assert result['noise_rms'] > result['signal_rms']
    # SNR should be in the range 8-11 dB
    assert 8.0 < result['snr_db'] < 11.0, \
        f"Expected ~9.5dB, got {result['snr_db']}"


def test_snr_measurement_strong_signal():
    """A very strong (well-quieted) signal should yield high SNR."""
    audio = make_synthetic_audio(
        duration_sec=3.0,
        noise_rms=0.10, signal_rms=0.005,  # 20:1 ratio -> ~26 dB
        burst_start_sec=1.0, burst_duration_sec=0.5,
    )
    framesync_end_bit = int(1.1 * RX_SAMPLE_RATE / SAMPLES_PER_BIT)
    result = measure_packet_snr(audio, 0, framesync_end_bit)
    assert result is not None
    assert result['snr_db'] > 20.0


def test_snr_measurement_no_quieting():
    """If the 'burst' is the same level as noise, SNR should be near 0 dB."""
    audio = make_synthetic_audio(
        duration_sec=3.0,
        noise_rms=0.05, signal_rms=0.05,  # No FM quieting
        burst_start_sec=1.0, burst_duration_sec=0.5,
    )
    framesync_end_bit = int(1.1 * RX_SAMPLE_RATE / SAMPLES_PER_BIT)
    result = measure_packet_snr(audio, 0, framesync_end_bit)
    assert result is not None
    # Should be close to 0 dB (within 2 dB statistical wobble)
    assert abs(result['snr_db']) < 2.0


def test_snr_measurement_returns_none_when_too_short():
    """Audio buffer shorter than measurement windows should return None."""
    short_audio = np.zeros(1000, dtype=np.float32)  # <25 ms at 43.2 kHz
    result = measure_packet_snr(short_audio, 0, 100)
    assert result is None


def test_snr_measurement_handles_burst_at_start():
    """Burst very near the start (no room for noise window) shouldn't crash."""
    audio = make_synthetic_audio(
        duration_sec=2.0,
        noise_rms=0.06, signal_rms=0.02,
        burst_start_sec=0.05, burst_duration_sec=0.3,
    )
    framesync_end_bit = int(0.1 * RX_SAMPLE_RATE / SAMPLES_PER_BIT)
    result = measure_packet_snr(audio, 0, framesync_end_bit)
    # Either returns None or marks measurement as not fully valid
    if result is not None:
        # The function should still produce something, possibly with
        # measurement_valid=False
        assert 'snr_db' in result


def test_global_noise_floor_estimation():
    """Global noise floor should approximate the no-signal RMS."""
    audio = make_synthetic_audio(
        duration_sec=5.0,
        noise_rms=0.06, signal_rms=0.02,
        burst_start_sec=2.0, burst_duration_sec=0.3,
    )
    floor = estimate_global_noise_floor(audio)
    # Most of the audio is at ~0.06 RMS (noise), bursts are ~5% of total
    # The 75th percentile of envelopes should be close to 0.06
    assert 0.04 < floor < 0.08, f"Expected ~0.06, got {floor}"


def test_snr_decreases_with_weaker_burst():
    """Sweep signal_rms and verify SNR decreases monotonically as it rises."""
    snr_results = []
    for sig_rms in [0.005, 0.015, 0.030, 0.045, 0.060]:
        audio = make_synthetic_audio(
            duration_sec=2.0,
            noise_rms=0.06, signal_rms=sig_rms,
            burst_start_sec=1.0, burst_duration_sec=0.4,
        )
        framesync_end_bit = int(1.1 * RX_SAMPLE_RATE / SAMPLES_PER_BIT)
        r = measure_packet_snr(audio, 0, framesync_end_bit)
        assert r is not None
        snr_results.append(r['snr_db'])
    # Should be monotonically decreasing
    for i in range(len(snr_results) - 1):
        assert snr_results[i] >= snr_results[i + 1] - 0.5, \
            f"SNR should decrease with weaker quieting: {snr_results}"


if __name__ == '__main__':
    print("Running signal-quality tests...")
    test_snr_measurement_basic(); print("  ✓ basic SNR measurement (~9.5 dB)")
    test_snr_measurement_strong_signal(); print("  ✓ strong-signal high SNR")
    test_snr_measurement_no_quieting(); print("  ✓ no-quieting near 0 dB")
    test_snr_measurement_returns_none_when_too_short(); print("  ✓ returns None on short buffer")
    test_snr_measurement_handles_burst_at_start(); print("  ✓ handles burst at start gracefully")
    test_global_noise_floor_estimation(); print("  ✓ global noise floor estimate")
    test_snr_decreases_with_weaker_burst(); print("  ✓ SNR monotonic with quieting depth")
    print("All quality tests passed!")
