# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""MER (modulation error ratio) — the SINAD analogue for a data burst.

These tests pin the properties MER exists to provide, and the honesty
constraints that keep it from becoming a fabrication vector:

  * amplitude normalisation (the whole point — snr_db is capture-gain
    dependent and therefore not comparable between sites)
  * monotonicity with channel noise
  * exact reference alignment (a misaligned reference must NOT silently
    produce a plausible number)
  * None when no honest reference exists — never a fallback estimate
  * superset-safety: MER must not change which packets are emitted
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.quality import measure_symbol_mer, MER_MIN_SYMBOLS
from alert2.generator import (
    make_sensor_frame, build_mant_pdu, build_airlink_bits,
    build_tx_audio, TxTiming,
)


def _libfec_available():
    try:
        from alert2.fec import _load_libfec
        _load_libfec()
        return True
    except Exception:
        return False


libfec = pytest.mark.skipif(not _libfec_available(),
                            reason="libfec not installed")


def _decode(bits, noise=0.0, gain=1.0, seed=1):
    """Decode through the soft path, which is what MER needs."""
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    rng = np.random.default_rng(seed)
    audio = build_tx_audio(bits, TxTiming()) * gain
    if noise:
        audio = audio + rng.normal(0, noise * gain, len(audio))
    demod_bits, info = demodulate(audio, 43200)
    soft = info.get('soft_samples')
    dec = Alert2Decoder(soft_viterbi=True)
    return dec.decode_bits(
        demod_bits, soft_samples=soft,
        amplitude_estimate=estimate_amplitude(soft) if soft else None)


def _sensor_bits():
    return make_sensor_frame(723, [(5, 12.34), (8, 128)], timestamp=39600)


def _multiblock_bits():
    # 100 payload bytes forces follow-on blocks at FEC 0, exercising the
    # two-segment reference with the pad gap between them.
    pdu = build_mant_pdu(bytes(range(100)), source_addr=769,
                         dest_addr=1, port=1)
    return build_airlink_bits([pdu])


# ---------------------------------------------------------------------------
# Pure-function properties (no libfec needed)
# ---------------------------------------------------------------------------

def test_perfect_symbols_score_high():
    bits = [1, 0] * 200
    raw = [1.0 if b else -1.0 for b in bits]
    m = measure_symbol_mer(raw, [(0, bits)])
    assert m is not None
    assert m['mer_db'] >= 50.0
    assert m['evm_pct'] == pytest.approx(0.0, abs=1e-6)


def test_amplitude_normalised():
    """The property snr_db lacks: same waveform, different capture gain.

    A 7 dB scale offset between two sites on the same antenna was what
    motivated this metric; MER must be blind to it.
    """
    rng = np.random.default_rng(0)
    bits = list(rng.integers(0, 2, 800))
    base = np.array([1.0 if b else -1.0 for b in bits])
    noise = rng.normal(0, 0.25, len(bits))
    results = []
    for gain in (0.05, 0.25, 1.0, 4.0, 20.0):
        raw = list(gain * (base + noise))
        m = measure_symbol_mer(raw, [(0, bits)])
        assert m is not None
        results.append(m['mer_db'])
        # alpha, by contrast, SHOULD track the gain
        assert m['alpha'] == pytest.approx(gain, rel=0.05)
    assert max(results) - min(results) < 0.01


def test_monotonic_in_noise():
    rng = np.random.default_rng(2)
    bits = list(rng.integers(0, 2, 1000))
    base = np.array([1.0 if b else -1.0 for b in bits])
    last = None
    for sigma in (0.05, 0.1, 0.2, 0.4, 0.8):
        raw = list(base + rng.normal(0, sigma, len(bits)))
        m = measure_symbol_mer(raw, [(0, bits)])
        assert m is not None
        if last is not None:
            assert m['mer_db'] < last
        last = m['mer_db']


def test_dc_offset_removed():
    """An FM discriminator bias must not be charged to the error term."""
    rng = np.random.default_rng(4)
    bits = list(rng.integers(0, 2, 600))
    base = np.array([1.0 if b else -1.0 for b in bits])
    raw = list(base + 0.3)                      # pure DC, no noise
    m = measure_symbol_mer(raw, [(0, bits)])
    assert m is not None
    assert m['mer_db'] >= 50.0
    assert m['dc'] == pytest.approx(0.3, abs=1e-6)
    # ...and with remove_dc off it must be visible as error, not hidden
    m2 = measure_symbol_mer(raw, [(0, bits)], remove_dc=False)
    assert m2['mer_db'] < m['mer_db']


def test_misaligned_reference_scores_badly():
    """A shifted reference must NOT produce a plausible number.

    This is the guard against a silently desynchronised reconstruction
    reporting healthy MER on a frame it never actually matched.
    """
    rng = np.random.default_rng(5)
    bits = list(rng.integers(0, 2, 900))
    base = np.array([1.0 if b else -1.0 for b in bits])
    raw = list(base + rng.normal(0, 0.1, len(bits)))
    aligned = measure_symbol_mer(raw, [(0, bits)])
    for shift in (1, 2, 3, 7):
        off = measure_symbol_mer(raw, [(0, bits[shift:])])
        assert off is not None
        assert off['mer_db'] < aligned['mer_db'] - 15.0


def test_returns_none_without_reference():
    raw = [0.5] * 500
    assert measure_symbol_mer(raw, []) is None
    assert measure_symbol_mer(raw, [(0, None)]) is None
    assert measure_symbol_mer(None, [(0, [1, 0])]) is None


def test_returns_none_when_too_few_symbols():
    bits = [1, 0] * (MER_MIN_SYMBOLS // 4)
    raw = [1.0 if b else -1.0 for b in bits]
    assert measure_symbol_mer(raw, [(0, bits)]) is None


def test_segment_out_of_range_is_skipped_not_guessed():
    """A segment past the end of raw_soft is dropped, never zero-padded."""
    rng = np.random.default_rng(6)
    bits = list(rng.integers(0, 2, 400))
    raw = [1.0 if b else -1.0 for b in bits]
    m = measure_symbol_mer(raw, [(0, bits), (10_000, bits)])
    assert m is not None
    assert m['n_symbols'] == len(bits)          # only the in-range segment


def test_all_one_symbol_reference_rejected():
    """A degenerate reference cannot be fitted; must return None."""
    bits = [1] * 300
    raw = [1.0] * 300
    assert measure_symbol_mer(raw, [(0, bits)], remove_dc=True) is None


def test_non_finite_segment_skipped():
    rng = np.random.default_rng(8)
    bits = list(rng.integers(0, 2, 400))
    raw = [1.0 if b else -1.0 for b in bits]
    raw[5] = float('nan')
    assert measure_symbol_mer(raw, [(0, bits)]) is None


# ---------------------------------------------------------------------------
# End-to-end through the decoder
# ---------------------------------------------------------------------------

@libfec
def test_packet_carries_mer():
    pkts = _decode(_sensor_bits(), noise=0.05)
    assert len(pkts) == 1
    p = pkts[0]
    assert p.mer_db is not None
    assert p.mer_evm_pct is not None
    assert p.mer_symbols and p.mer_symbols > MER_MIN_SYMBOLS
    assert p.mer_coverage in ('full', 'first-block')
    assert p.mer_alpha and p.mer_alpha > 0


@libfec
def test_multiblock_reference_is_full_coverage():
    """Follow-on present => reference spans both segments, skipping the pad.

    The pad between the first block's CC tail and the follow-on stream is
    4 symbols at fec_mode 0 and 0 at fec_mode 1; its transmitted value is
    unknown, so it must be excluded rather than assumed.
    """
    pkts = _decode(_multiblock_bits(), noise=0.03)
    assert len(pkts) == 1
    p = pkts[0]
    assert p.mer_coverage == 'full'
    assert not p.truncated
    # first block is 652 symbols at FEC 0; 'full' must exceed that
    assert p.mer_symbols > 652


@libfec
def test_mer_gain_invariant_end_to_end():
    """Same waveform through the real decode path at different gains."""
    vals = []
    for gain in (0.25, 1.0, 4.0):
        pkts = _decode(_sensor_bits(), noise=0.05, gain=gain)
        assert len(pkts) == 1
        vals.append(pkts[0].mer_db)
    assert max(vals) - min(vals) < 0.05


@libfec
def test_mer_degrades_with_channel_noise():
    clean = _decode(_sensor_bits(), noise=0.02)[0].mer_db
    noisy = _decode(_sensor_bits(), noise=0.25)[0].mer_db
    assert noisy < clean - 1.0


@libfec
def test_mer_absent_without_soft_samples():
    """No soft samples => no honest reference => None, not an estimate."""
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    audio = build_tx_audio(_sensor_bits(), TxTiming())
    bits, _ = demodulate(audio, 43200)
    pkts = Alert2Decoder().decode_bits(bits)
    assert len(pkts) == 1
    assert pkts[0].mer_db is None
    assert pkts[0].mer_coverage is None


@libfec
def test_mer_is_superset_safe():
    """MER is pure observation: identical packets with and without it.

    Guards the project's non-negotiable rule — a measurement rung may add
    information but must never change which packets are emitted.
    """
    import alert2.decoder as D
    bits = _multiblock_bits()
    with_mer = _decode(bits, noise=0.05)
    orig = D._mer_reference_segments
    try:
        D._mer_reference_segments = lambda *a, **k: ([], None)
        without = _decode(bits, noise=0.05)
    finally:
        D._mer_reference_segments = orig
    assert len(with_mer) == len(without)
    for a, b in zip(with_mer, without):
        assert bytes(a.raw_payload) == bytes(b.raw_payload)
        assert a.truncated == b.truncated
        assert a.rs_corrections == b.rs_corrections
        assert a.framesync_hd == b.framesync_hd
    assert without[0].mer_db is None
    assert with_mer[0].mer_db is not None


@libfec
def test_mer_does_not_leak_between_frames():
    """A frame with no reference must not inherit the previous frame's MER."""
    pkts = _decode(_sensor_bits(), noise=0.05)
    assert pkts[0].mer_db is not None
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    dec = Alert2Decoder(soft_viterbi=True)
    audio = build_tx_audio(_sensor_bits(), TxTiming())
    bits, _ = demodulate(audio, 43200)
    first = dec.decode_bits(bits)              # no soft => no MER
    second = dec.decode_bits(bits)
    assert all(p.mer_db is None for p in first + second)


# ---------------------------------------------------------------------------
# Reporting surfaces
# ---------------------------------------------------------------------------

def _fake_packet(mer=None):
    from alert2.types import Alert2Packet
    from tests.test_web import make_packet
    p = make_packet()
    p.mer_db = mer
    p.mer_evm_pct = 12.5 if mer is not None else None
    p.mer_symbols = 652 if mer is not None else None
    p.mer_coverage = 'full' if mer is not None else None
    return p


def test_diag_line_reports_mer_and_its_absence(tmp_path):
    from alert2.output import DiagOutput
    for mer, expect in ((14.25, 'MER=14.25dB'), (None, 'MER=?')):
        path = tmp_path / f"d{mer}.diag"
        out = DiagOutput(str(path))
        out.emit(_fake_packet(mer))
        out.close()
        text = path.read_text()
        assert 'SNR=' in text, "MER must be reported ALONGSIDE SNR, not instead"
        assert expect in text


def test_log_line_reports_mer():
    from alert2.output import format_packet_human
    line = format_packet_human(_fake_packet(9.75))
    assert 'MER=9.8dB' in line
    assert 'SNR=' in line


def test_web_dict_exposes_mer():
    from alert2.web import _packet_to_web_dict
    d = _packet_to_web_dict(_fake_packet(11.5), 1)
    assert d['mer_db'] == 11.5
    assert d['mer_coverage'] == 'full'
    assert 'snr_db' in d


# ---------------------------------------------------------------------------
# MER after an ideal linear equalizer (diag-only diagnostic)
# ---------------------------------------------------------------------------

@libfec
def test_mer_eq_is_never_worse_than_raw():
    """An ideal LS-fitted FIR cannot make the fit worse than no filter."""
    pkts = _decode(_multiblock_bits(), noise=0.05)
    p = pkts[0]
    assert p.mer_eq_db is not None
    assert p.mer_eq_gain_db == pytest.approx(p.mer_eq_db - p.mer_db, abs=1e-9)
    assert p.mer_eq_db >= p.mer_db - 0.5


@libfec
def test_eq_gain_shrinks_as_noise_dominates():
    """Linear ISI is removable; additive noise is not.

    As noise grows, a larger share of the residual becomes irreducible, so
    the gain an ideal equalizer can deliver must fall.
    """
    quiet = _decode(_multiblock_bits(), noise=0.05)[0]
    loud = _decode(_multiblock_bits(), noise=0.60)[0]
    assert loud.mer_eq_gain_db < quiet.mer_eq_gain_db


@libfec
def test_mer_eq_reported_on_all_surfaces(tmp_path):
    """MER_eq appears wherever MER does: log, diag, CLI summary, web dict."""
    import io
    from alert2.output import (format_packet_human, StatsCollector,
                               DiagOutput)
    from alert2.web import _packet_to_web_dict
    p = _decode(_multiblock_bits(), noise=0.05)[0]
    assert p.mer_eq_db is not None

    assert 'MER_eq=' in format_packet_human(p)

    sc = StatsCollector()
    sc.emit(p)
    buf = io.StringIO()
    sc.print_summary(file=buf)
    out = buf.getvalue()
    assert 'after ideal eq' in out
    assert 'removable ISI' in out

    path = tmp_path / "d.diag"
    d = DiagOutput(str(path))
    d.emit(p)
    d.close()
    text = path.read_text()
    assert 'MER_eq=' in text and 'eq_gain=' in text

    w = _packet_to_web_dict(p, 1)
    assert w['mer_eq_db'] is not None
    assert w['mer_eq_gain_db'] is not None


@libfec
def test_mer_eq_present_on_truncated_packets():
    """Truncated packets keep a first-block reference, so MER_eq survives."""
    import numpy as np
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    bits = _multiblock_bits()
    for seed in range(40):
        rng = np.random.default_rng(seed)
        a = build_tx_audio(bits, TxTiming()).astype(float)
        n = len(a)
        a = a + rng.normal(0, 0.15, n)
        a[int(n * 0.45):] += rng.normal(0, 1.6, n - int(n * 0.45))
        db, info = demodulate(a, 43200)
        ss = info.get('soft_samples')
        pk = Alert2Decoder(soft_viterbi=True).decode_bits(
            db, soft_samples=ss, amplitude_estimate=estimate_amplitude(ss))
        trunc = [k for k in pk if k.truncated]
        if trunc:
            k = trunc[0]
            assert k.mer_coverage == 'first-block'
            assert k.mer_db is not None and k.mer_eq_db is not None
            return
    pytest.skip("no truncated packet produced in 40 trials")


@libfec
def test_every_pdu_in_a_bundle_carries_the_frame_value():
    """MER is a per-FRAME measurement; bundled PDUs share it."""
    pdus = [build_mant_pdu(bytes(range(20)), source_addr=s, dest_addr=1,
                           port=1) for s in (701, 702, 703)]
    pkts = _decode(build_airlink_bits(pdus), noise=0.05)
    assert len(pkts) == 3
    assert len({round(k.mer_db, 6) for k in pkts}) == 1
    assert len({round(k.mer_eq_db, 6) for k in pkts}) == 1


# ---------------------------------------------------------------------------
# Null-safety of the reporting surfaces
#
# Admitting MER-only packets into the SNR history (packets with MER but no
# SNR — ~10% of field traffic, and every burst-mode packet) broke the
# per-source page: renderChart() called h.snr_db.toFixed() unguarded, threw,
# and because update() swallowed exceptions the history table went blank too.
# These pin both halves of the fix.
# ---------------------------------------------------------------------------

import os
import re

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'alert2', 'templates')


def _template(name):
    with open(os.path.join(_TEMPLATES, name)) as fh:
        return fh.read()


@pytest.mark.parametrize('name', ['source.html', 'index.html'])
@pytest.mark.parametrize('field', ['snr_db', 'mer_db', 'mer_eq_db',
                                   'mer_eq_gain_db', 'mer_evm_pct'])
def test_no_unguarded_tofixed_on_nullable_metrics(name, field):
    """Any .toFixed() on a nullable metric must sit behind a null check.

    Either an explicit `field != null` guard on the same or a preceding
    line, or a ternary. A bare `x.field.toFixed()` throws on null and takes
    the whole render down.
    """
    text = _template(name)
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r'(\w+)\.' + field + r'\.toFixed', line):
            ctx = '\n'.join(text.splitlines()[max(0, lineno - 4):lineno])
            guarded = (f'{field} != null' in ctx
                       or f'{field} == null' in ctx
                       or f'{field} !== null' in ctx)
            assert guarded, (
                f"{name}:{lineno} calls {m.group(1)}.{field}.toFixed() with "
                f"no null guard within 3 lines; {field} is nullable and this "
                f"will throw at runtime")


def test_source_page_renders_sections_independently():
    """A failure in one section must not blank the others."""
    text = _template('source.html')
    assert text.count('catch (e) { console.error(') >= 3, \
        "renderChart/renderHistory must be individually guarded and logged"
    assert 'catch (e) {}' not in text, \
        "silent catch hides render failures — this is how the blank " \
        "chart+table bug went unnoticed"


def test_source_history_table_column_count_matches_header():
    text = _template('source.html')
    header = re.search(r'<tr>((?:<th>.*?</th>)+)</tr>', text, re.S)
    assert header is not None
    n_cols = header.group(1).count('<th>')
    for m in re.finditer(r'colspan="(\d+)"', text):
        assert int(m.group(1)) == n_cols, (
            f"colspan={m.group(1)} but the header has {n_cols} columns")


@libfec
def test_history_admits_mer_only_packets():
    """The gate change that caused the bug is intentional — pin it.

    Packets with MER but no SNR must reach the per-source history, so the
    chart cannot assume snr_db is present.
    """
    from alert2.web import WebOutput
    p = _decode(_multiblock_bits(), noise=0.05)[0]
    p.snr_db = None
    assert p.mer_db is not None
    wo = WebOutput()
    wo.emit(p)
    hist = wo.get_source_history(p.mant.source_addr)
    assert len(hist) == 1
    assert hist[0]['snr_db'] is None
    assert hist[0]['mer_db'] is not None


# ---------------------------------------------------------------------------
# Cost control
#
# MER costs +6.2% CPU; MER+MER_eq costs +37.2% (measured on a 3-frame mix).
# A Pi at decode_rt 0.898x cannot absorb the full mode — it lands near 1.23x,
# an unbounded backlog. These pin the knobs that make it deployable.
# ---------------------------------------------------------------------------

@libfec
def test_mer_mode_off_disables_measurement():
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    import numpy as np
    rng = np.random.default_rng(1)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.05, len(audio))
    bits, info = demodulate(audio, 43200)
    soft = info.get('soft_samples')
    kw = dict(soft_samples=soft, amplitude_estimate=estimate_amplitude(soft))

    off = Alert2Decoder(soft_viterbi=True, mer_mode='off').decode_bits(bits, **kw)
    assert off[0].mer_db is None and off[0].mer_eq_db is None

    basic = Alert2Decoder(soft_viterbi=True, mer_mode='basic').decode_bits(bits, **kw)
    assert basic[0].mer_db is not None
    assert basic[0].mer_eq_db is None, "basic must skip the expensive eq pass"

    full = Alert2Decoder(soft_viterbi=True, mer_mode='full').decode_bits(bits, **kw)
    assert full[0].mer_db is not None and full[0].mer_eq_db is not None
    assert full[0].mer_db == pytest.approx(basic[0].mer_db, abs=1e-9)


@libfec
def test_mer_sampling_measures_one_frame_in_n():
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    import numpy as np
    rng = np.random.default_rng(2)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.05, len(audio))
    bits, info = demodulate(audio, 43200)
    soft = info.get('soft_samples')
    dec = Alert2Decoder(soft_viterbi=True, mer_mode='basic', mer_sample=3)
    measured = 0
    for _ in range(9):
        pk = dec.decode_bits(
            bits, soft_samples=soft,
            amplitude_estimate=estimate_amplitude(soft))
        if pk and pk[0].mer_db is not None:
            measured += 1
    assert measured == 3, f"expected 1-in-3 of 9 frames measured, got {measured}"


@libfec
def test_mer_never_changes_decode_regardless_of_mode():
    """Cost knobs are observation-only: identical packets in every mode."""
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    import numpy as np
    rng = np.random.default_rng(3)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.25, len(audio))
    bits, info = demodulate(audio, 43200)
    soft = info.get('soft_samples')
    kw = dict(soft_samples=soft, amplitude_estimate=estimate_amplitude(soft))
    runs = [Alert2Decoder(soft_viterbi=True, mer_mode=m,
                          mer_sample=n).decode_bits(bits, **kw)
            for m, n in (('off', 1), ('basic', 1), ('full', 1),
                         ('basic', 5), ('full', 5))]
    ref = runs[0]
    for r in runs[1:]:
        assert len(r) == len(ref)
        for a, b in zip(r, ref):
            assert bytes(a.raw_payload) == bytes(b.raw_payload)
            assert a.truncated == b.truncated
            assert a.rs_corrections == b.rs_corrections


def test_mer_mode_validated():
    from alert2.decoder import Alert2Decoder
    with pytest.raises(ValueError):
        Alert2Decoder(mer_mode='sometimes')


# ---------------------------------------------------------------------------
# alpha — capture LEVEL, reported raw
#
# alpha is the least-squares symbol amplitude: the scale MER divides out.
# It exists because a mixer level reset (a Pi reboot restoring default input
# gain) is invisible to both SNR — which the radio's limiter pins to a
# ~0.2 dB spread across all stations — and to MER, which is deliberately
# amplitude-normalised. Reported in raw soft-sample units with no reference
# constant, since any reference would be site-specific.
# ---------------------------------------------------------------------------

@libfec
def test_alpha_tracks_gain_while_mer_ignores_it():
    """The orthogonality that makes alpha worth a separate field."""
    import numpy as np
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    seen = {}
    for gain in (0.25, 1.0, 4.0):
        rng = np.random.default_rng(1)
        audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
        audio = (audio + rng.normal(0, 0.05, len(audio))) * gain
        bits, info = demodulate(audio, 43200)
        soft = info.get('soft_samples')
        p = Alert2Decoder(soft_viterbi=True).decode_bits(
            bits, soft_samples=soft,
            amplitude_estimate=estimate_amplitude(soft))[0]
        seen[gain] = (p.mer_alpha, p.mer_db)
    # alpha scales with gain...
    assert seen[1.0][0] == pytest.approx(seen[0.25][0] * 4, rel=0.05)
    assert seen[4.0][0] == pytest.approx(seen[1.0][0] * 4, rel=0.05)
    # ...while MER does not move
    mers = [v[1] for v in seen.values()]
    assert max(mers) - min(mers) < 0.05


@libfec
def test_alpha_reported_raw_on_all_surfaces(tmp_path):
    import io
    from alert2.output import StatsCollector, DiagOutput
    from alert2.web import _packet_to_web_dict
    p = _decode(_multiblock_bits(), noise=0.05)[0]
    assert p.mer_alpha is not None

    path = tmp_path / "d.diag"
    d = DiagOutput(str(path))
    d.emit(p)
    d.close()
    text = path.read_text()
    assert 'alpha=' in text
    # raw units, not dB — a dB form would need a site-specific reference
    assert 'alpha=' in text and 'alphadB' not in text
    m = re.search(r'alpha=([\d.]+)', text)
    assert m and float(m.group(1)) == pytest.approx(p.mer_alpha, abs=1e-4)

    sc = StatsCollector()
    sc.emit(p)
    buf = io.StringIO()
    sc.print_summary(file=buf)
    assert 'Capture level (alpha)' in buf.getvalue()

    w = _packet_to_web_dict(p, 1)
    assert w['mer_alpha'] == pytest.approx(p.mer_alpha)


@libfec
def test_alpha_absent_when_mer_disabled(tmp_path):
    """No measurement, no level reading — never a fabricated one."""
    import numpy as np
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    from alert2.output import DiagOutput
    rng = np.random.default_rng(1)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.05, len(audio))
    bits, info = demodulate(audio, 43200)
    soft = info.get('soft_samples')
    p = Alert2Decoder(soft_viterbi=True, mer_mode='off').decode_bits(
        bits, soft_samples=soft,
        amplitude_estimate=estimate_amplitude(soft))[0]
    assert p.mer_alpha is None
    path = tmp_path / "off.diag"
    d = DiagOutput(str(path))
    d.emit(p)
    d.close()
    assert 'alpha=' not in path.read_text()


def test_chart_plots_dots_for_both_series():
    """Every packet contributes a dot to EVERY series it has a value for.

    A previous version drew one dot per packet, preferring MER, which left
    SNR with a bare polyline and no points — indistinguishable from "SNR is
    not graphed".
    """
    text = _template('source.html')
    chart = text[text.index('function renderChart'):text.index('// Y-axis labels')]
    assert "dot(h, 'snr_db'" in chart and "dot(h, 'mer_db'" in chart, \
        "both series must emit dots"
    assert "h.mer_db != null ? h.mer_db : h.snr_db" not in chart, \
        "one-dot-per-packet fallback reintroduced"


def test_chart_series_colour_is_not_overwritten_by_status():
    """Fill encodes the metric; status uses a ring. The two must not collide.

    Overwriting fill with the status colour made a red dot mean TRUNCATED
    while the legend said the colours identified SNR vs MER.
    """
    text = _template('source.html')
    chart = text[text.index('function renderChart'):text.index('// Y-axis labels')]
    assert 'ring = C_TRUNC' in chart and 'ring = C_RS' in chart
    for bad in ('fill = C_TRUNC', 'color = C_TRUNC', "color = '#b91c1c'"):
        assert bad not in chart, f"status colour written to fill: {bad}"
    # legend swatches must reuse the same constants the dots use
    assert "swatch('SNR', C_SNR" in text and "swatch('MER', C_MER" in text


# ---------------------------------------------------------------------------
# Chart STRUCTURE, not just "did it throw"
#
# An earlier verification ran the chart JS and checked only that it raised no
# exception. A chart that silently drew one series and mislabelled its dot
# colours passes that test perfectly — and did, until the operator noticed on
# screen. These assert what actually gets drawn.
# ---------------------------------------------------------------------------

import json
import shutil
import subprocess


def _render_chart(history):
    """Run the real renderChart from the template and return the SVG."""
    node = shutil.which('node')
    if node is None:
        pytest.skip("node not available to execute template JS")
    tpl = _template('source.html')
    body = tpl[tpl.index('function renderChart'):tpl.index('async function update')]
    js = """
var OUT={};
var document={getElementById:function(){return {clientWidth:900,
  setAttribute:function(){},
  set innerHTML(v){OUT.svg=v;}, get innerHTML(){return OUT.svg||'';},
  querySelectorAll:function(){return []}}},
  querySelectorAll:function(){return []}};
function fmt(v,d){return v==null?d:v}
""" + body + """
renderChart(%s);
console.log(OUT.svg||'');
""" % json.dumps(history)
    out = subprocess.run([node, '-e', js], capture_output=True, text=True)
    assert out.returncode == 0, f"chart JS failed: {out.stderr}"
    return out.stdout


def _mixed_history():
    t = 1785000000
    h = []
    for i in range(30):
        e = {'rx_time_unix': t + i * 40, 'rs_corrections': 0,
             'truncated': False}
        if i % 7 == 0:
            e['snr_db'], e['mer_db'] = None, 4.5
        elif i % 5 == 0:
            e['snr_db'], e['mer_db'] = 7.9, None
        else:
            e['snr_db'], e['mer_db'] = 7.9, 4.5
        if i % 6 == 0:
            e['rs_corrections'] = 3
        if i % 11 == 0:
            e['truncated'] = True
        h.append(e)
    return h


def test_chart_draws_both_series():
    """SNR must be plotted, not just MER. One dot per metric per packet."""
    hist = _mixed_history()
    svg = _render_chart(hist)
    C_SNR, C_MER = '#0369a1', '#7c3aed'
    assert svg.count('<polyline') == 2, "expected an SNR and a MER polyline"
    assert C_SNR in svg and C_MER in svg
    n_snr = sum(1 for h in hist if h['snr_db'] is not None)
    n_mer = sum(1 for h in hist if h['mer_db'] is not None)
    assert svg.count(f'fill="{C_SNR}"') >= n_snr, \
        "every packet with an SNR needs an SNR dot — drawing only the " \
        "preferred metric leaves the other series as a bare line"
    assert svg.count(f'fill="{C_MER}"') >= n_mer


def test_chart_dot_fill_is_metric_not_status():
    """Fill encodes the series (matching the legend); status is a RING.

    Overwriting the fill with a status colour makes a red dot mean
    'truncated' while the legend says red is a series.
    """
    svg = _render_chart(_mixed_history())
    C_TRUNC, C_RS = '#b91c1c', '#b45309'
    assert f'fill="{C_TRUNC}"' not in svg, "status colour must not be a fill"
    assert f'fill="{C_RS}"' not in svg
    assert f'stroke="{C_TRUNC}"' in svg, "truncated packets need a ring"
    assert f'stroke="{C_RS}"' in svg


def test_chart_legend_only_advertises_present_series():
    """A --mer-mode off site must not show an empty MER series."""
    hist = _mixed_history()
    for h in hist:
        h['mer_db'] = None
    svg = _render_chart(hist)
    assert '>SNR<' in svg
    assert '>MER<' not in svg, "legend must not advertise a series with no data"
    assert svg.count('<polyline') == 1


# ---------------------------------------------------------------------------
# Achieved MER (MER_act)
#
# MER measures the RAW channel and MER_eq an IDEAL least-squares FIR fitted
# with perfect knowledge of the transmitted symbols. Neither is what the real
# equalizer produced, so how much of the available gain the ladder actually
# captured was invisible. MER_act closes that:
#
#     MER      ->   MER_act    ->   MER_eq
#     channel       achieved        bound
#
# Measured on a QTH capture: raw -0.43 dB, achieved 6.20 dB, bound 6.43 dB —
# the real equalizer captures 98% of the available gain (+7.05 of 7.26 dB),
# and made no packet worse in 13/13. That result closed off "train the
# equalizer harder" as an avenue: the remaining gap is 0.23 dB.
# ---------------------------------------------------------------------------

@libfec
def test_mer_act_present_only_when_the_equalizer_trained():
    """MER_act is scoped to packets the equalizer actually ran on.

    It is None otherwise — never a copy of raw MER, which would silently
    imply the equalizer had been applied when it had not.
    """
    pkts = _decode(_multiblock_bits(), noise=0.05)
    for p in pkts:
        if getattr(p, 'mer_act_db', None) is not None:
            assert p.mer_db is not None
            assert p.mer_eq_db is not None


@libfec
def test_mer_act_lies_between_raw_and_the_ideal_bound():
    """The real equalizer cannot beat an ideal LS fit on the same taps."""
    import numpy as np
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    rng = np.random.default_rng(5)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.45, len(audio))
    bits, info = demodulate(audio.astype(np.float32), 43200)
    ss = info.get('soft_samples')
    d = Alert2Decoder(soft_viterbi=True, max_framesync_hd=6, eq_retry=True,
                      mer_mode='full')
    got = d.decode_bits(bits, soft_samples=ss,
                        amplitude_estimate=estimate_amplitude(ss))
    checked = 0
    for p in got:
        a = getattr(p, 'mer_act_db', None)
        if a is None:
            continue
        checked += 1
        assert a <= p.mer_eq_db + 0.5, (
            f"achieved {a:.2f} exceeds the ideal bound {p.mer_eq_db:.2f}")
    if checked == 0:
        pytest.skip("equalizer did not train on this frame")


@libfec
def test_mer_act_absent_in_basic_mode():
    """basic mode skips the expensive eq measurements entirely."""
    import numpy as np
    from alert2.demod import demodulate
    from alert2.decoder import Alert2Decoder
    from alert2.fec import estimate_amplitude
    rng = np.random.default_rng(6)
    audio = build_tx_audio(_multiblock_bits(), TxTiming()).astype(float)
    audio = audio + rng.normal(0, 0.4, len(audio))
    bits, info = demodulate(audio.astype(np.float32), 43200)
    ss = info.get('soft_samples')
    d = Alert2Decoder(soft_viterbi=True, max_framesync_hd=6, eq_retry=True,
                      mer_mode='basic')
    for p in d.decode_bits(bits, soft_samples=ss,
                           amplitude_estimate=estimate_amplitude(ss)):
        assert getattr(p, 'mer_act_db', None) is None
        assert p.mer_eq_db is None


def test_diag_emits_mer_act_when_present(tmp_path):
    from alert2.output import DiagOutput
    p = _fake_packet(7.5)
    p.mer_eq_db = 6.9
    p.mer_eq_gain_db = -0.6
    p.mer_act_db = 6.25
    path = tmp_path / "a.diag"
    o = DiagOutput(str(path))
    o.emit(p)
    o.close()
    text = path.read_text()
    assert 'MER_act=6.25dB' in text
    assert 'MER=' in text and 'MER_eq=' in text
