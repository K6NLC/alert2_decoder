# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
--rate-track: per-burst open-loop symbol-rate correction.

Synthetic drift is injected by stretching the generator's 43200 Hz audio
in time (a transmitter running slow by `drift`), then the real pipeline
runs: demodulate -> matched filter -> detect_bursts -> rate_track.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.burst_detect import detect_bursts
from alert2.constants import SAMPLES_PER_BIT as SPB
from alert2.demod import demodulate, slice_bits
from alert2.fec import estimate_amplitude
from alert2.generator import build_mant_pdu, build_airlink_bits, \
    build_tx_audio, TxTiming
from alert2.rate_track import (estimate_slope, corrected_stream,
                               decode_rate_corrected, PREAMBLE_BITS,
                               DEFAULT_MIN_SLOPE)


def _libfec_available():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec_available(),
                            reason="libfec not installed")


def _audio(n_payload, drift, seed=1):
    app = bytes((i * 29 + seed) & 0xFF for i in range(n_payload))
    pdu = build_mant_pdu(app, 4321, port=1)
    a = build_tx_audio(build_airlink_bits([pdu]), TxTiming())
    if drift:
        # transmitter slow by `drift`: every symbol lasts (1+drift) longer
        n = int(len(a) * (1 + drift))
        a = np.interp(np.arange(n) / (1 + drift), np.arange(len(a)), a)
    rng = np.random.default_rng(seed)
    a = a + rng.normal(0, 0.02, len(a))
    return a.astype(np.float32), app


def _filt(audio):
    _bits, info = demodulate(audio, 43200)
    return np.asarray(info['filtered_43k'], dtype=float)


def _fixed_grid_decode(decoder, filt, bursts):
    out = []
    for bc in bursts:
        ph = bc.phase
        soft = filt[ph::SPB]
        bits = slice_bits(soft)
        sl = [float(v) for v in soft]
        if bc.inverted:
            bits = [1 - b for b in bits]
            sl = [-v for v in sl]
        out += decoder.decode_bits(
            bits, bit_phase=ph, soft_samples=sl,
            amplitude_estimate=estimate_amplitude(sl),
            soft_inverted=bc.inverted, presync_search=False,
            candidates=[(bc.fs_end, bc.fec_mode, bc.fs_hd)])
    return out


@pytest.mark.parametrize("drift", [0.0008, 0.0010, 0.0015, -0.0010])
def test_slope_estimate_tracks_injected_drift(drift):
    filt = _filt(_audio(120, drift)[0])
    bursts = detect_bursts(filt, list(range(SPB)), 3, [0], min_corr=0.40)
    assert len(bursts) == 1
    slope, _ph0, n = estimate_slope(
        filt, bursts[0].abs_sample - PREAMBLE_BITS * SPB)
    assert n == 12
    # slope is samples per symbol; injected drift stretches SPB samples
    assert slope == pytest.approx(drift * SPB, rel=0.15)


def test_clean_burst_is_below_threshold():
    filt = _filt(_audio(120, 0.0)[0])
    bursts = detect_bursts(filt, list(range(SPB)), 3, [0], min_corr=0.40)
    assert len(bursts) == 1
    slope, _, _ = estimate_slope(
        filt, bursts[0].abs_sample - PREAMBLE_BITS * SPB)
    assert abs(slope) < DEFAULT_MIN_SLOPE / 4
    assert corrected_stream(filt, bursts[0].abs_sample) is None


@libfec
def test_drift_breaks_fixed_grid_and_rate_track_recovers():
    from alert2.decoder import Alert2Decoder
    audio, app = _audio(150, 0.0010)          # the 0.1% seen on SDR captures
    filt = _filt(audio)
    dec = Alert2Decoder(soft_viterbi=True, max_framesync_hd=3,
                        mer_mode='basic')
    bursts = detect_bursts(filt, list(range(SPB)), 3, [0], min_corr=0.40)
    assert len(bursts) == 1
    fixed = _fixed_grid_decode(dec, filt, bursts)
    assert all(p.truncated or p.application_payload != app for p in fixed)
    got = decode_rate_corrected(dec, filt, bursts)
    assert len(got) == 1
    p = got[0]
    assert not p.truncated
    assert p.application_payload == app
    assert p.decode_path == 'rate-track'
    assert p.rate_ppm == pytest.approx(1000, rel=0.15)


@libfec
def test_rate_track_is_a_noop_without_drift():
    from alert2.decoder import Alert2Decoder
    audio, app = _audio(150, 0.0)
    filt = _filt(audio)
    dec = Alert2Decoder(soft_viterbi=True, max_framesync_hd=3,
                        mer_mode='basic')
    bursts = detect_bursts(filt, list(range(SPB)), 3, [0], min_corr=0.40)
    fixed = _fixed_grid_decode(dec, filt, bursts)
    assert [p.application_payload for p in fixed] == [app]
    assert decode_rate_corrected(dec, filt, bursts) == []


@libfec
def test_decode_wav_rate_track_flag(tmp_path):
    import scipy.io.wavfile as wavio
    from alert2.decoder import decode_wav
    audio, app = _audio(150, 0.0010)
    path = tmp_path / "drift.wav"
    wavio.write(str(path), 43200, (audio * 32767).astype(np.int16))
    base = decode_wav(str(path), soft_viterbi=True, max_framesync_hd=3,
                      multiphase=True, phase_candidates=9,
                      try_simple_demod_too=False)
    assert not any(p.application_payload == app and not p.truncated
                   for p in base)
    rt = decode_wav(str(path), soft_viterbi=True, max_framesync_hd=3,
                    multiphase=True, phase_candidates=9,
                    try_simple_demod_too=False, rate_track=True)
    full = [p for p in rt if p.application_payload == app
            and not p.truncated]
    assert len(full) == 1 and full[0].decode_path == 'rate-track'
