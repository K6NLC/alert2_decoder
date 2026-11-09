# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
I/Q front end: reading, survey, channelize/discriminate, per-burst carrier
refinement, CLI integration. Synthetic captures are made by FM-modulating
the generator's own baseband audio onto a complex carrier at 240 ksps.
"""
import os

import numpy as np
import pytest

from alert2.generator import make_sensor_frame, build_tx_audio, TxTiming
from alert2 import iq


def _libfec_available():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec_available(),
                            reason="libfec not installed")

FS = 240000


def _fm(audio43k, offset_hz, dev_hz=2500.0, fs=FS):
    """Baseband audio (43200 Hz) -> complex FM at `offset_hz`, fs sps."""
    from scipy.signal import resample_poly
    from fractions import Fraction
    fr = Fraction(fs, 43200).limit_denominator(1000)
    a = resample_poly(audio43k, fr.numerator, fr.denominator)
    phase = 2 * np.pi * np.cumsum(offset_hz + dev_hz * a) / fs
    return np.exp(1j * phase).astype(np.complex64)


def _capture(bursts, seconds=4.0, noise=0.05, seed=3):
    """bursts: [(t_start_s, offset_hz, frame_bits)] -> complex64 capture."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    x = (rng.normal(0, noise, n) + 1j * rng.normal(0, noise, n)
         ).astype(np.complex64)
    for t0, off, bits in bursts:
        a = build_tx_audio(bits, TxTiming(warmup_ms=0))
        s = _fm(a, off)
        i = int(t0 * FS)
        x[i:i + len(s)] += 0.3 * s
    return x


def _frame(src, ts):
    return make_sensor_frame(src, [(5, 12.5), (8, 128)], timestamp=ts)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_parse_gqrx_name():
    assert iq.parse_gqrx_name(
        "/x/gqrx_20261029_052740_171850000_240000_fc.raw") == (171850000,
                                                                 240000)
    assert iq.parse_gqrx_name("gqrx_20261029_053055_171850000_240000_fc_1.raw") \
        == (171850000, 240000)
    assert iq.parse_gqrx_name("capture.cu8") is None


def test_read_formats_agree(tmp_path):
    x = _capture([(0.5, 20000.0, _frame(700, 100))], seconds=1.5)
    p32 = tmp_path / "gqrx_20260101_000000_170000000_240000_fc.raw"
    x.tofile(str(p32))
    a, fs, c = iq.read_iq(str(p32))
    assert fs == 240000 and c == 170000000.0 and np.allclose(a, x)
    # cu8 (rtl_sdr): 8-bit unsigned interleaved
    u = np.empty(2 * len(x), np.uint8)
    u[0::2] = np.clip(np.round(x.real * 127.5 + 127.5), 0, 255)
    u[1::2] = np.clip(np.round(x.imag * 127.5 + 127.5), 0, 255)
    p8 = tmp_path / "cap.cu8"
    u.tofile(str(p8))
    b, fs8, c8 = iq.read_iq(str(p8), sample_rate=240000)
    assert fs8 == 240000 and c8 is None
    assert np.abs(b - x).max() < 0.01
    with pytest.raises(ValueError):
        iq.read_iq(str(p8))          # no rate anywhere


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------

def test_survey_finds_bursty_channel_not_noise():
    x = _capture([(0.5, 37000.0, _frame(700, 100)),
                  (2.0, 37000.0, _frame(701, 100))], seconds=4.0)
    chans = iq.survey(x, FS)
    assert len(chans) >= 1
    off, score = chans[0]
    assert abs(off - 37000.0) < 1500
    assert score > iq.SURVEY_MIN_RATIO_DB
    # pure noise: nothing bursty
    rng = np.random.default_rng(1)
    n = (rng.normal(0, .05, FS * 3) + 1j * rng.normal(0, .05, FS * 3)
         ).astype(np.complex64)
    assert iq.survey(n, FS) == [(0.0, 0.0)]


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

@libfec
def test_decode_iq_offset_channel(tmp_path):
    x = _capture([(0.5, 37000.0, _frame(700, 100)),
                  (2.0, 37000.0, _frame(701, 101))], seconds=4.0)
    p = tmp_path / "gqrx_20260101_000000_170000000_240000_fc.raw"
    x.tofile(str(p))
    pk, rep = iq.decode_iq(str(p), soft_viterbi=True, max_framesync_hd=3,
                           multiphase=True, phase_candidates=9,
                           try_simple_demod_too=False)
    srcs = sorted(q.mant.source_addr for q in pk)
    assert srcs == [700, 701]
    for q in pk:
        assert not q.truncated
        assert abs(q.carrier_hz - 170037000.0) < 300


@libfec
def test_burst_refine_handles_two_carriers_in_one_channel(tmp_path):
    """Two sites 2.4 kHz apart (a real shared-channel case): the survey
    centres between them; per-burst refinement recovers both and reports
    each burst's own carrier."""
    x = _capture([(0.5, 36000.0, _frame(700, 100)),
                  (2.0, 38400.0, _frame(701, 101))], seconds=4.0)
    p = tmp_path / "gqrx_20260101_000000_170000000_240000_fc.raw"
    x.tofile(str(p))
    pk, rep = iq.decode_iq(str(p), channel_bw=3500.0, soft_viterbi=True,
                           max_framesync_hd=3, multiphase=True,
                           phase_candidates=9, try_simple_demod_too=False)
    by = {q.mant.source_addr: q for q in pk}
    assert set(by) == {700, 701}
    assert abs(by[700].carrier_hz - 170036000.0) < 400
    assert abs(by[701].carrier_hz - 170038400.0) < 400


@libfec
def test_cli_iq_file_mode(tmp_path, capsys):
    from alert2.cli import main
    x = _capture([(0.5, -15000.0, _frame(702, 100))], seconds=2.5)
    p = tmp_path / "gqrx_20260101_000000_170000000_240000_fc.raw"
    x.tofile(str(p))
    log = tmp_path / "out.log"
    import sys
    argv = sys.argv
    sys.argv = ['alert2-decode', str(p), '--multiphase',
                '--phase-candidates', '9', '--max-fs-hd', '3',
                '--log-file', str(log), '--no-stats']
    try:
        main()
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    err = capsys.readouterr().err
    assert 'I/Q:' in err and 'candidate channel' in err
    text = log.read_text()
    assert 'src=702' in text


@libfec
def test_cli_freq_hint_skips_survey(tmp_path, capsys):
    from alert2.cli import main
    x = _capture([(0.5, -15000.0, _frame(703, 100))], seconds=2.5)
    p = tmp_path / "gqrx_20260101_000000_170000000_240000_fc.raw"
    x.tofile(str(p))
    log = tmp_path / "out.log"
    import sys
    argv = sys.argv
    sys.argv = ['alert2-decode', str(p), '--freq', '169.985M',
                '--multiphase', '--phase-candidates', '9', '--max-fs-hd', '3',
                '--log-file', str(log), '--no-stats']
    try:
        main()
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    assert '-15000 Hz' in capsys.readouterr().err
    assert 'src=703' in log.read_text()


def test_parse_freq():
    from alert2.cli import _parse_freq
    assert _parse_freq('170.2625M') == 170262500.0
    assert _parse_freq('171850k') == 171850000.0
    assert _parse_freq('170262500') == 170262500.0
