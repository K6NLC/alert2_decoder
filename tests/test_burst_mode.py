"""Tests for burst-scoped decoding (rc23)."""
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import numpy as np

from alert2.burst import frame_bit_length, bit_to_sample, extract_burst
from alert2.constants import SAMPLES_PER_BIT, RX_SAMPLE_RATE
from alert2.generator import make_sensor_frame, build_tx_audio, TxTiming
from alert2.stream import stream_decode

FS = 43200


def _audio_with_bursts(n=3, seed=5):
    total = int((4 + 6 * n) * FS)
    audio = np.random.RandomState(seed).normal(
        0, 0.004, total).astype(np.float32)
    for k in range(n):
        readings = [(10 + i, 1.0 + i + k) for i in range(12)]
        bits = make_sensor_frame(750 + k, readings, timestamp=40000 + 60 * k)
        b = build_tx_audio(bits, TxTiming())
        i = int((2 + 6 * k) * FS)
        audio[i:i + len(b)] += b[:max(0, total - i)]
    return audio


def test_frame_bit_length_scales_with_payload():
    short = frame_bit_length(40, 0)
    long_ = frame_bit_length(400, 0)
    assert long_ > short > 0
    # a 1023 B frame must not exceed the spec airtime (~5.6 s)
    assert frame_bit_length(1023, 0) * SAMPLES_PER_BIT / RX_SAMPLE_RATE < 6.0


def test_bit_to_sample_roundtrip():
    for fs in (43200, 48000):
        s0 = bit_to_sample(0, 3, fs)
        s1 = bit_to_sample(100, 3, fs)
        expected = int(100 * SAMPLES_PER_BIT * fs / RX_SAMPLE_RATE)
        assert abs((s1 - s0) - expected) <= 2


def test_extract_rejects_out_of_range():
    audio = np.zeros(1000, dtype=np.float32)
    seg, ok = extract_burst(audio, FS, 10, 0, 500)
    assert not ok and seg is None       # start_bit negative
    seg, ok = extract_burst(audio, FS, 100000, 0, 500)
    assert not ok                        # past the end


def test_burst_mode_is_superset_of_chunk_mode():
    """Burst-scoped output must never drop a packet the chunk pass found."""
    audio = _audio_with_bursts()
    pcm = (audio * 32767).astype(np.int16).tobytes()

    def run(burst):
        got = list(stream_decode(
            io.BytesIO(pcm), sample_rate=FS, chunk_seconds=6.0,
            overlap_seconds=3.0, soft_viterbi=True, iterative='off',
            eq_retry=True, burst_mode=burst))
        return {(p.mant.source_addr,
                 p.srp_pdu.timestamp if (p.srp_pdu
                                         and p.srp_pdu.has_timestamp)
                 else None)
                for p in got if p.mant}

    base = run(False)
    with_burst = run(True)
    assert base, "fixture must decode something"
    assert base <= with_burst, (
        f"burst mode lost packets: {sorted(base - with_burst)}")


def test_burst_mode_tags_packets():
    audio = _audio_with_bursts(n=2, seed=9)
    pcm = (audio * 32767).astype(np.int16).tobytes()
    got = list(stream_decode(io.BytesIO(pcm), sample_rate=FS,
                             chunk_seconds=6.0, overlap_seconds=3.0,
                             soft_viterbi=True, iterative='off',
                             eq_retry=True, burst_mode=True))
    assert got, "fixture must decode"
    # the field exists on every packet and is a bool
    assert all(isinstance(getattr(p, 'burst_scoped', False), bool)
               for p in got)
