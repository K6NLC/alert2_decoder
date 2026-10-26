# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for presync-eq polarity gating and lowered HD threshold (rc20).

The gate must: run on all polarities before any packet is seen; skip the
presync second-search on polarities that have never produced a packet;
reactivate the moment a polarity produces one; and run the eq-stream
search at presync_max_fs_hd (default max_fs_hd - 1), never the raw
threshold. The primary (raw) search must run unconditionally regardless
of gating.
"""
import numpy as np
import pytest

import alert2.decoder as dec
from alert2.decoder import Alert2Decoder
from alert2.demod import demodulate
from alert2.generator import make_sensor_frame, build_tx_audio, TxTiming

FS = 43200


def _burst_audio(seed: int) -> np.ndarray:
    """Real generated burst (normal polarity) in light noise."""
    readings = [(10, 0), (20, 13.0 + seed), (0, 5.5 + seed)]
    bits = make_sensor_frame(750 + seed, readings, timestamp=20000 + seed)
    burst = build_tx_audio(bits, TxTiming())
    audio = np.random.RandomState(seed).normal(
        0, 0.004, len(burst) + 2 * FS).astype(np.float32)
    audio[FS:FS + len(burst)] += burst
    return audio


def _demod(audio):
    bits, info = demodulate(audio, FS)
    return bits, info.get('soft_samples'), info.get('amplitude_estimate')


class _SearchSpy:
    """Counts framesync-search invocations and records thresholds.

    The decoder uses find_framesync (require_bitsync=False, default) or
    find_framesync_with_bitsync_gate; wrap both. Per decode_bits call,
    1 invocation = primary only (presync skipped), 2 = primary +
    presync second-search.
    """

    def __init__(self):
        self.orig_gate = dec.find_framesync_with_bitsync_gate
        self.orig_plain = dec.find_framesync
        self.calls = []

    def __enter__(self):
        def spy_gate(bits, max_fs_hd=4, max_bs_hd=8, **kw):
            self.calls.append(max_fs_hd)
            return self.orig_gate(bits, max_fs_hd=max_fs_hd,
                                  max_bs_hd=max_bs_hd, **kw)

        def spy_plain(bits, max_hd=4, **kw):
            self.calls.append(max_hd)
            return self.orig_plain(bits, max_hd=max_hd, **kw)
        dec.find_framesync_with_bitsync_gate = spy_gate
        dec.find_framesync = spy_plain
        return self

    def __exit__(self, *a):
        dec.find_framesync_with_bitsync_gate = self.orig_gate
        dec.find_framesync = self.orig_plain


def _mk_decoder(**kw):
    d = Alert2Decoder(max_framesync_hd=5, soft_viterbi=True,
                      iterative='off', presync_eq=True,
                      presync_min_bursts=0, **kw)
    # pre-seed calibration so the presync search is armed immediately
    d._presync_w_sum = np.array([0.0, 0.0, 1.0, 0.0, 0.0]) * 1
    d._presync_w_n = 1
    return d


def test_presync_runs_on_both_polarities_before_first_packet():
    d = _mk_decoder()
    noise = np.random.RandomState(7).normal(0, 0.004, 3 * FS).astype(
        np.float32)
    bits, soft, amp = _demod(noise)
    with _SearchSpy() as spy:
        d.decode_bits(bits, soft_samples=soft, amplitude_estimate=amp,
                      soft_inverted=False)
        n_norm = len(spy.calls)
        d.decode_bits([1 - b for b in bits], soft_samples=soft,
                      amplitude_estimate=amp, soft_inverted=True)
        n_inv = len(spy.calls) - n_norm
    # no packets seen yet -> presync active on BOTH polarities
    assert n_norm == 2, "presync search should run pre-first-packet (norm)"
    assert n_inv == 2, "presync search should run pre-first-packet (inv)"


def test_presync_skips_dead_polarity_and_reactivates():
    d = _mk_decoder()
    audio = _burst_audio(1)
    bits, soft, amp = _demod(audio)

    # 1) decode a NORMAL-polarity packet (generator output is normal)
    pkts = d.decode_bits(bits, soft_samples=soft, amplitude_estimate=amp,
                         soft_inverted=False)
    assert pkts, "generated burst must decode on normal polarity"
    assert d._pol_pkts[False] > 0 and d._pol_pkts[True] == 0

    # 2) inverted pass on noise: presync must be SKIPPED (1 search call)
    noise = np.random.RandomState(9).normal(0, 0.004, 3 * FS).astype(
        np.float32)
    nbits, nsoft, namp = _demod(noise)
    with _SearchSpy() as spy:
        d.decode_bits([1 - b for b in nbits], soft_samples=nsoft,
                      amplitude_estimate=namp, soft_inverted=True)
        assert len(spy.calls) == 1, \
            "presync must skip a polarity that has produced no packets"
        # normal pass keeps presync (2 calls)
        d.decode_bits(nbits, soft_samples=nsoft, amplitude_estimate=namp,
                      soft_inverted=False)
        assert len(spy.calls) == 3, \
            "presync must stay active on the productive polarity"

    # 3) an INVERTED packet arrives -> gate reopens for inverted.
    # A genuinely inverted-chain burst is negated AUDIO (discriminator
    # sign flip); the inverted pass then flips demod bits back and
    # decode_bits negates soft internally, mirroring stream.py.
    audio2 = -_burst_audio(2)
    b2, s2, a2 = _demod(audio2)
    pkts2 = d.decode_bits([1 - b for b in b2],
                          soft_samples=s2, amplitude_estimate=a2,
                          soft_inverted=True)
    assert pkts2, "inverted burst must decode on inverted pass"
    assert d._pol_pkts[True] > 0
    with _SearchSpy() as spy:
        d.decode_bits([1 - b for b in nbits], soft_samples=nsoft,
                      amplitude_estimate=namp, soft_inverted=True)
        assert len(spy.calls) == 2, \
            "presync must reactivate once the polarity produces a packet"


def test_presync_search_uses_lowered_hd_threshold():
    d = _mk_decoder()  # default presync_max_fs_hd -> max_fs_hd - 1 = 4
    noise = np.random.RandomState(11).normal(0, 0.004, 3 * FS).astype(
        np.float32)
    bits, soft, amp = _demod(noise)
    with _SearchSpy() as spy:
        d.decode_bits(bits, soft_samples=soft, amplitude_estimate=amp,
                      soft_inverted=False)
    assert spy.calls == [5, 4], \
        f"expected primary at fs=5 then presync at fs=4, got {spy.calls}"

    d2 = _mk_decoder(presync_max_fs_hd=2)
    with _SearchSpy() as spy:
        d2.decode_bits(bits, soft_samples=soft, amplitude_estimate=amp,
                       soft_inverted=False)
    assert spy.calls == [5, 2], \
        f"explicit presync_max_fs_hd must be honored, got {spy.calls}"


def test_primary_search_never_gated():
    """Even with presync fully suppressed, the raw search must run."""
    d = _mk_decoder()
    d._pol_pkts = {False: 5, True: 0}  # inverted is 'dead'
    audio = -_burst_audio(3)  # inverted-chain burst (negated audio)
    bits, soft, amp = _demod(audio)
    # inverted pass on a genuinely inverted burst, presync gated off:
    pkts = d.decode_bits([1 - b for b in bits],
                         soft_samples=soft, amplitude_estimate=amp,
                         soft_inverted=True)
    assert pkts, ("primary raw search must still decode packets on a "
                  "presync-gated polarity")
