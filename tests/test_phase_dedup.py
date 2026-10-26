# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Best-wins phase dedup.

When the primary phase, a multiphase pass and the burst pass all decode the
same burst, the historical rule kept whichever arrived FIRST — which was
always the primary phase, since it runs first. Measured across 23 payloads
that decoded at 2+ phases (three real captures), that discarded a mean of
3.55 dB of MER and up to 7.48 dB: the primary phase decoded one burst at
MER -0.70 while another phase decoded the identical payload at 6.78.

Selecting by preamble correlation instead lands 0.013 dB short of an oracle
that always picks the best-MER phase; RS_corr or FS_hd alone leave 0.715 dB.

The invariant these tests defend: the PACKET SET must be identical either
way. Only the retained copy changes.
"""
import pytest

np = pytest.importorskip("numpy")

from alert2.stream import _select_best_copies, _phase_quality
from alert2.types import Alert2Packet


def _pkt(payload=b'\x01\x02', fec=0, rs=0, fs=0, tgain=None,
         truncated=False, path='x'):
    p = Alert2Packet(fec_mode=fec, raw_payload=payload)
    p.rs_corrections = rs
    p.framesync_hd = fs
    p.timing_gain = tgain
    p.truncated = truncated
    p.decode_path = path
    return p


def test_first_mode_is_exactly_the_old_behaviour():
    pkts = [_pkt(tgain=0.9, path='primary'), _pkt(tgain=0.0, path='mf3')]
    got = _select_best_copies(pkts, 'first')
    assert got == pkts, "first mode must not reorder or drop anything"


def test_best_mode_keeps_the_better_timed_copy():
    primary = _pkt(tgain=0.90, path='primary')
    better = _pkt(tgain=0.01, path='mf3')
    got = _select_best_copies([primary, better], 'best')
    assert len(got) == 1
    assert got[0] is better, "the copy nearer its preamble optimum must win"


def test_packet_set_is_identical_in_both_modes():
    """The invariant: selection may change WHICH copy, never WHICH payloads.

    'first' mode leaves duplicates in place for the caller's own `seen`
    set to collapse (that is what the historical path did), so both modes
    are compared after that same downstream dedup.
    """
    pkts = [
        _pkt(b'\xaa', rs=2, tgain=0.8), _pkt(b'\xaa', rs=0, tgain=0.1),
        _pkt(b'\xbb', rs=1, tgain=0.4),
        _pkt(b'\xcc', rs=0, tgain=0.0), _pkt(b'\xcc', rs=3, tgain=0.7),
    ]

    def emitted(mode):
        seen, out = set(), []
        for p in _select_best_copies(pkts, mode):
            k = (p.fec_mode, bytes(p.raw_payload), p.truncated)
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
        return out

    assert emitted('first') == emitted('best'), (
        "best-wins must emit the same payloads in the same order as "
        "first-wins; only the retained copy may differ")


def test_truncated_and_complete_are_kept_separately():
    """A badly-timed phase can truncate where a better one completes.

    Those are different payloads and both are emitted today; selection must
    not collapse them, or the complete copy could be lost.
    """
    trunc = _pkt(b'\x01', truncated=True, tgain=0.0)
    full = _pkt(b'\x01\x02\x03', truncated=False, tgain=0.5)
    got = _select_best_copies([trunc, full], 'best')
    assert len(got) == 2


def test_order_of_first_appearance_is_preserved():
    pkts = [_pkt(b'\x01', tgain=0.5), _pkt(b'\x02', tgain=0.5),
            _pkt(b'\x01', tgain=0.0), _pkt(b'\x03', tgain=0.5)]
    got = _select_best_copies(pkts, 'best')
    assert [bytes(p.raw_payload) for p in got] == [b'\x01', b'\x02', b'\x03']


def test_probe_absent_falls_back_to_rs_then_fs():
    """Without the timing probe, rank on RS corrections then framesync HD."""
    worse = _pkt(rs=4, fs=2)
    better = _pkt(rs=0, fs=1)
    got = _select_best_copies([worse, better], 'best')
    assert got[0] is better


def test_probe_copy_beats_a_probeless_copy():
    """A measured copy is preferred over an unmeasured one at equal RS."""
    unmeasured = _pkt(rs=0, fs=0, tgain=None)
    measured = _pkt(rs=0, fs=0, tgain=0.2)
    assert _phase_quality(measured) > _phase_quality(unmeasured)


def test_empty_and_single_inputs():
    assert _select_best_copies([], 'best') == []
    one = [_pkt()]
    assert _select_best_copies(one, 'best') == one
    assert _select_best_copies(one, 'first') == one


# ---------------------------------------------------------------------------
# Pipeline ordering
#
# rc28 shipped best-wins as a no-op on the LIVE path. _select_best_copies was
# correct and unit-tested, but stream.py applied its seen_payloads gate to
# each pass as it ran — so a better-timed duplicate from a later phase was
# discarded before selection could compare it. The file path (cli.py) had the
# gate moved; the streaming path did not, and that is the path both field
# sites run. A night of QTH data was collected against a no-op.
#
# The unit tests all passed. What was missing was a test of the ORDER.
# ---------------------------------------------------------------------------

def test_selection_must_precede_the_payload_gate():
    """Gate-then-select silently reduces best-wins to first-wins."""
    import inspect
    from alert2 import stream
    src = inspect.getsource(stream.stream_decode)
    sel = src.index('_select_best_copies(chunk_emit')
    gate = src.index('if key in seen_payloads')
    assert sel < gate, (
        "seen_payloads is consulted before _select_best_copies — duplicate "
        "copies are discarded before they can be compared, making "
        "--phase-dedup best a no-op on the live path")


def test_no_pass_gates_payloads_before_collection():
    """Each decode pass must append every copy to the chunk buffer."""
    import inspect, re
    from alert2 import stream
    src = inspect.getsource(stream.stream_decode)
    for m in re.finditer(r'chunk_emit\.append\(pkt\)', src):
        window = src[max(0, m.start() - 700):m.start()]
        last_gate = window.rfind('if key in seen_payloads')
        last_sel = window.rfind('_select_best_copies')
        assert last_gate == -1 or last_gate < last_sel, (
            "a decode pass filters on seen_payloads before appending to "
            "chunk_emit; alternatives are lost before selection")


def test_gate_still_runs_after_selection():
    """Cross-chunk suppression must survive the reordering."""
    import inspect
    from alert2 import stream
    src = inspect.getsource(stream.stream_decode)
    assert 'seen_payloads.add(key)' in src
    assert 'seen_payloads.clear()' in src, "the 1000-entry trim was dropped"
