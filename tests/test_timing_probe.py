# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-burst symbol-timing probe — observation only.

Built to answer one question on live traffic: are low-MER bursts mistimed?
Offline on 15 bursts, MER correlated -0.978 with |timing offset| and the
offsets landed on WHOLE samples (-4.000, -3.000, -2.000, +1.000), i.e. the
window picked the wrong one of the nine phases rather than being slightly
off. Every burst reached ~0.94 preamble correlation at its own best phase,
including one reading MER -0.70 at its used phase.

These tests pin that the probe measures without touching the decode path.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.constants import SAMPLES_PER_BIT
from alert2.timing_probe import measure_burst_timing, PREAMBLE_BITS
from alert2.generator import (make_sensor_frame, build_tx_audio, TxTiming)


def _libfec():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec(), reason="libfec not installed")


def _decode(noise=0.05, seed=1, probe=True, shift=0.0):
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    rng = np.random.default_rng(seed)
    audio = build_tx_audio(
        make_sensor_frame(723, [(5, 12.34)], timestamp=39600),
        TxTiming()).astype(np.float64)
    if shift:
        g = np.arange(len(audio))
        audio = np.interp(g + shift, g, audio)
    audio = audio + rng.normal(0, noise, len(audio))
    bits, info = demodulate(audio.astype(np.float32), 43200)
    ss = info.get('soft_samples')
    d = Alert2Decoder(soft_viterbi=True, mer_mode='off', timing_probe=probe)
    pkts = d.decode_bits(
        bits, soft_samples=ss,
        amplitude_estimate=estimate_amplitude(ss) if ss else None,
        filtered_43k=info.get('filtered_43k') if probe else None,
        bit_phase=info.get('best_phase', 0))
    return pkts, info


def test_probe_returns_none_without_stream():
    assert measure_burst_timing(None, 1000, 0.0, 0) is None


def test_probe_returns_none_when_preamble_outside_stream():
    f = np.zeros(50)
    assert measure_burst_timing(f, 5, 0.0, 0) is None      # start_bit < 0
    assert measure_burst_timing(f, 10_000, 0.0, 0) is None  # past the end


def test_probe_reports_high_match_on_a_clean_signal():
    """A clean burst must reach a high preamble correlation at SOME phase.

    Note the offset is NOT asserted to be ~0: on a short generated frame
    find_best_phase() lands ~1.6 samples off the preamble optimum even with
    no noise, which is the same window-phase error the probe exists to
    quantify. What must hold is that a good phase EXISTS and the probe
    finds it.
    """
    from alert2.demod import demodulate
    audio = build_tx_audio(
        make_sensor_frame(723, [(5, 12.34)], timestamp=39600), TxTiming())
    bits, info = demodulate(audio.astype(np.float32), 43200)
    from alert2.sync import find_framesync
    cands = find_framesync(bits, max_hd=2)
    if not cands:
        pytest.skip("no clean framesync in the generated frame")
    fe, mode, _ = cands[0]
    r = measure_burst_timing(info['filtered_43k'], fe,
                             float(info['best_phase']), mode)
    assert r is not None
    assert abs(r['offset']) <= SAMPLES_PER_BIT / 2.0 + 1e-6, r
    assert r['corr'] > 0.8, r
    assert r['corr'] >= r['corr_at_0'] - 1e-9, r


def test_probe_search_spans_a_full_symbol():
    """+/-0.5 saturated on every degraded burst; the window must cover all
    nine integer phases so a wrong-phase burst is distinguishable from a
    flat correlation surface."""
    import inspect
    sig = inspect.signature(measure_burst_timing)
    assert sig.parameters['search'].default >= SAMPLES_PER_BIT / 2.0


@libfec
def test_probe_does_not_change_decoding():
    """The whole point: measurement must not perturb the ladder."""
    on, _ = _decode(probe=True)
    off, _ = _decode(probe=False)
    assert len(on) == len(off)
    for a, b in zip(on, off):
        assert bytes(a.raw_payload) == bytes(b.raw_payload)
        assert a.truncated == b.truncated
        assert a.rs_corrections == b.rs_corrections
        assert a.framesync_hd == b.framesync_hd
    assert all(p.timing_offset is None for p in off)


@libfec
def test_probe_populates_fields_when_enabled():
    pkts, _ = _decode(probe=True)
    assert pkts
    assert any(p.timing_offset is not None for p in pkts)
    for p in pkts:
        if p.timing_offset is not None:
            assert abs(p.timing_offset) <= SAMPLES_PER_BIT / 2.0 + 1e-6
            assert p.timing_gain is not None and p.timing_gain >= -1e-9


@libfec
def test_probe_absent_without_filtered_stream():
    """No pre-decimation stream means no measurement — never a guess."""
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    audio = build_tx_audio(
        make_sensor_frame(723, [(5, 12.34)], timestamp=39600), TxTiming())
    bits, info = demodulate(audio.astype(np.float32), 43200)
    ss = info.get('soft_samples')
    d = Alert2Decoder(soft_viterbi=True, mer_mode='off', timing_probe=True)
    pkts = d.decode_bits(bits, soft_samples=ss,
                         amplitude_estimate=estimate_amplitude(ss))
    assert all(p.timing_offset is None for p in pkts)


@libfec
def test_diag_emits_timing_when_present(tmp_path):
    from alert2.output import DiagOutput
    pkts, _ = _decode(probe=True)
    p = next((k for k in pkts if k.timing_offset is not None), None)
    if p is None:
        pytest.skip("no probed packet")
    path = tmp_path / "t.diag"
    o = DiagOutput(str(path))
    o.emit(p)
    o.close()
    assert 't_off=' in path.read_text()


# ---------------------------------------------------------------------------
# Polarity
#
# The first field run of this probe measured NOTHING: 2275 of 2275 bursts
# reported |t_off| = 4.500, the search limit, giving zero variance. Cause:
# the decoder's inverted-polarity pass flips the BITS but `filtered_43k` is
# always the un-inverted matched-filter output. Correlating a non-inverted
# reference against it made every correlation negative (corr@0 = -0.914 on a
# burst that reads +0.914 correctly) so the argmax ran to a boundary.
#
# It slipped through because the probe genuinely does not change decoding —
# the no-perturbation test passed while the output was garbage. These tests
# check the MEASUREMENT, not just the absence of side effects.
# ---------------------------------------------------------------------------

def _preamble_stream(fec_mode=0, invert=False, phase=3, noise=0.0, seed=0):
    """Synthesise a matched-filter-like stream carrying the known preamble.

    Each symbol is held for SAMPLES_PER_BIT samples (a rectangular pulse)
    rather than placed as an impulse, so that interpolating between sample
    instants behaves like a real filtered waveform.
    """
    from alert2.timing_probe import _preamble_symbols
    from alert2.constants import SAMPLES_PER_BIT
    rng = np.random.default_rng(seed)
    sym = _preamble_symbols(fec_mode)
    if invert:
        sym = -sym
    pad = 400
    # Impulse train smoothed by a raised-cosine-ish window, so the
    # correlation has a single peak. A rectangular hold would give a FLAT
    # top on which every offset inside the symbol scores identically, and
    # the argmax would be meaningless.
    imp = np.zeros(len(sym) * SAMPLES_PER_BIT)
    imp[::SAMPLES_PER_BIT] = sym
    win = np.hanning(2 * SAMPLES_PER_BIT + 1)
    body = np.convolve(imp, win / win.sum() * SAMPLES_PER_BIT, mode='same')
    f = np.zeros(pad + len(body) + pad)
    f[pad:pad + len(body)] = body
    if noise:
        f = f + rng.normal(0, noise, len(f))
    # Sample at the CENTRE of each symbol.
    phase = pad
    fs_end_bit = len(sym)
    return f, fs_end_bit, phase


def test_inverted_flag_recovers_the_correlation():
    from alert2.timing_probe import measure_burst_timing as mbt
    f, fe, ph = _preamble_stream(invert=True)
    wrong = mbt(f, fe, float(ph), 0, inverted=False)
    right = mbt(f, fe, float(ph), 0, inverted=True)
    assert wrong is not None and right is not None
    assert right['corr_at_0'] > 0.5, right
    assert wrong['corr_at_0'] < -0.5, wrong
    assert right['corr_at_0'] == pytest.approx(-wrong['corr_at_0'], abs=1e-9)


def test_polarity_error_is_self_announcing():
    """A sign error must be visible, not silently degrade to a boundary."""
    from alert2.timing_probe import measure_burst_timing as mbt
    f, fe, ph = _preamble_stream(invert=True)
    wrong = mbt(f, fe, float(ph), 0, inverted=False)
    assert wrong['polarity_suspect'] is True
    right = mbt(f, fe, float(ph), 0, inverted=True)
    assert right['polarity_suspect'] is False


def test_wrong_polarity_drives_the_offset_far_from_truth():
    """Reproduce the field failure so its signature stays recognisable.

    With the reference polarity wrong every correlation is negative and the
    argmax has nothing real to lock to, so it lands far from the true
    offset — in the field it pinned at the +/-4.500 boundary on 100% of
    bursts. The exact landing point depends on the pulse shape, so this
    asserts "far from truth", not "exactly at the limit".
    """
    from alert2.timing_probe import measure_burst_timing as mbt
    from alert2.constants import SAMPLES_PER_BIT
    f, fe, ph = _preamble_stream(invert=True)
    wrong = mbt(f, fe, float(ph), 0, inverted=False)
    right = mbt(f, fe, float(ph), 0, inverted=True)
    assert abs(right['offset']) < 1.0, right
    assert abs(wrong['offset']) > SAMPLES_PER_BIT / 3.0, wrong
    assert abs(wrong['offset']) > abs(right['offset']) + 2.0


@libfec
def test_offsets_vary_across_bursts_on_the_inverted_path():
    """Guards the exact field symptom: a constant t_off is a bug, not data.

    Runs the decoder over audio whose polarity is inverted relative to the
    stream the probe sees — i.e. the QTH's normal operating condition — and
    requires the reported offsets to actually differ from one another.
    """
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    rng = np.random.default_rng(3)
    audio = build_tx_audio(
        make_sensor_frame(723, [(5, 12.34)], timestamp=39600),
        TxTiming()).astype(np.float64)
    audio = audio + rng.normal(0, 0.10, len(audio))
    bits, info = demodulate(audio.astype(np.float32), 43200)
    ss = info.get('soft_samples')
    d = Alert2Decoder(soft_viterbi=True, mer_mode='off', timing_probe=True)
    got = d.decode_bits(
        [1 - b for b in bits],
        soft_samples=[-x for x in ss] if ss else None,
        amplitude_estimate=estimate_amplitude(ss) if ss else None,
        filtered_43k=info.get('filtered_43k'),
        bit_phase=info.get('best_phase', 0),
        soft_inverted=True)
    offs = [p.timing_offset for p in got if p.timing_offset is not None]
    if not offs:
        pytest.skip("no probed packet on the inverted path")
    from alert2.constants import SAMPLES_PER_BIT
    saturated = sum(1 for o in offs
                    if abs(o) >= SAMPLES_PER_BIT / 2.0 - 1e-6)
    assert saturated < len(offs), (
        f"all {len(offs)} offsets pinned at the search limit — this is the "
        f"polarity bug signature, not a measurement")
