"""Detect and rank bursts across every symbol phase, without decoding.
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later


Step 1-2 of per-burst phase selection. Finds candidates, scores each by
|preamble correlation|, and merges the per-phase sightings of one burst into
a single candidate naming its best phase.

WHY
---
The winning phase is uniformly random per burst (10.3-12.2% across all nine,
n=1269) with no memory between bursts (consecutive winners agree 9.9%,
chance 11.1%). So full phase coverage is required and nothing can be
predicted. But covering all nine currently costs a FULL DECODE at each:
bursts decode at a mean of 8.0 of 9 phases and best-wins keeps one.
Profiling: Viterbi+RS 53% of CPU, quantize_soft 23%, framesync scan 1.3%.

The winner is identifiable before any of that runs. Measured over 41 bursts,
the highest-|correlation| phase decoded 41/41 (rank 1 every time), giving up
a mean 0.031 dB of MER against an oracle.

A previous attempt did this selection at CHUNK level and saved nothing: a
chunk holds many bursts wanting different phases, so their union is all nine.
Selection must be per BURST.

POSITION KEYS ARE ABSOLUTE. fs_end is a bit index relative to its own phase,
so one burst has different fs_end at different phases. Everything keys on
abs_sample = phase + fs_end * SAMPLES_PER_BIT, clustered with a tolerance
rather than binned — fixed bins split a burst that straddles a boundary,
which would re-decode it per phase and undo the saving.
"""
from typing import Dict, List, Sequence

try:
    import numpy as np
except ImportError:
    np = None

from .candidate_gate import preamble_correlation
from .constants import SAMPLES_PER_BIT
from .demod import slice_bits
from .sync import find_framesync

# Same burst seen from different phases differs by < 1 symbol; a few symbols
# of slack is ample and far below the gap between distinct transmissions.
# Measured insensitive from 72 to 1200 samples (14 bursts either way).
MERGE_TOLERANCE_SAMPLES = SAMPLES_PER_BIT * 8


class BurstCandidate:
    __slots__ = ('abs_sample', 'phase', 'fs_end', 'fec_mode', 'fs_hd',
                 'inverted', 'corr', 'alternates')

    def __init__(self, abs_sample, phase, fs_end, fec_mode, fs_hd,
                 inverted, corr):
        self.abs_sample = abs_sample
        self.phase = phase
        self.fs_end = fs_end
        self.fec_mode = fec_mode
        self.fs_hd = fs_hd
        self.inverted = inverted
        self.corr = corr
        self.alternates = []

    def ranked_attempts(self, max_attempts=2):
        """(phase, fs_end, fs_hd, inverted), best first.

        Today a burst gets up to 9 independent shots at Reed-Solomon; this
        reduces that to `max_attempts`, so the fallback matters even though
        rank 1 succeeded 41/41 in measurement.
        """
        out = [(self.phase, self.fs_end, self.fs_hd, self.inverted)]
        out += [(ph, fe, hd, inv)
                for _c, ph, fe, hd, inv in self.alternates]
        return out[:max_attempts] if max_attempts and max_attempts > 0 else out

    def __repr__(self):
        return (f'<Burst @{self.abs_sample} ph={self.phase} '
                f'fec={self.fec_mode} hd={self.fs_hd} inv={self.inverted} '
                f'corr={self.corr:.3f} alts={len(self.alternates)}>')


def scan_phase(filtered, phase, max_fs_hd, fec_modes,
               polarities=(False, True)):
    """Framesync candidates at one phase, each scored against the preamble.

    Correlation uses MAGNITUDE: a signed score silently rejected every real
    multiphase candidate in an earlier build, because callers disagree about
    who applies the polarity inversion.
    """
    if np is None or filtered is None:
        return []
    soft = filtered[phase::SAMPLES_PER_BIT]
    if len(soft) < 128:
        return []
    bits = slice_bits(soft)
    out = []
    for inverted in polarities:
        b = [1 - v for v in bits] if inverted else bits
        for fs_end, fec_mode, fs_hd in find_framesync(
                b, max_hd=max_fs_hd, modes=list(fec_modes)):
            c = preamble_correlation(soft, fs_end, fec_mode)
            if c is None:
                continue
            out.append(BurstCandidate(
                phase + fs_end * SAMPLES_PER_BIT, phase, fs_end,
                fec_mode, fs_hd, inverted, abs(float(c))))
    return out


def _collapse(run):
    run = sorted(run, key=lambda c: -c.corr)
    best = run[0]
    best.alternates = [(c.corr, c.phase, c.fs_end, c.fs_hd, c.inverted)
                       for c in run[1:]]
    return best


def merge_candidates(cands, tolerance=MERGE_TOLERANCE_SAMPLES):
    """Collapse per-phase sightings of one burst; best correlation wins."""
    if not cands:
        return []
    groups: Dict[tuple, List[BurstCandidate]] = {}
    for c in cands:
        groups.setdefault((c.fec_mode, c.inverted), []).append(c)
    merged = []
    for items in groups.values():
        items.sort(key=lambda c: c.abs_sample)
        run = [items[0]]
        for c in items[1:]:
            if c.abs_sample - run[-1].abs_sample <= tolerance:
                run.append(c)
            else:
                merged.append(_collapse(run))
                run = [c]
        merged.append(_collapse(run))
    merged.sort(key=lambda c: c.abs_sample)
    return merged


def detect_bursts(filtered, phases, max_fs_hd, fec_modes,
                  tolerance=MERGE_TOLERANCE_SAMPLES, min_corr=0.0):
    """Scan every phase, merge, rank. Decodes nothing.

    `min_corr` drops candidates whose |preamble correlation| falls below the
    threshold BEFORE merging. This is the same test the candidate gate
    applies inside the decode loop, moved earlier: the correlation is
    already computed here, and a candidate rejected now never becomes a
    decode job at all.

    It matters more than it looks. Without it a 40 s capture yields 550
    "bursts" of which only ~42 are real; the other ~508 are isolated noise
    detections that merge with nothing, so each becomes its own decode
    attempt and the per-burst saving is spent on noise. Measured across
    four captures, real bursts scored >= 0.610 and spurious <= 0.559, so
    0.40 discards them with margin.
    """
    all_c = []
    for ph in phases:
        for c in scan_phase(filtered, ph, max_fs_hd, fec_modes):
            if c.corr >= min_corr:
                all_c.append(c)
    return merge_candidates(all_c, tolerance)
