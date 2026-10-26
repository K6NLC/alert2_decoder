# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
sova.py — Canonical Soft-Output Viterbi Algorithm.

Correctly accounts for the binary CC trellis structure: each state
is reached by TWO predecessors with the SAME most-recent-bit (the
LSB of the state). The "competing path" at each state is the loser
of the two predecessors. The metric difference between them tells us
how confident the survivor's *recent history* is — not the current
bit alone, but specifically the bits that differ between the two
predecessors' paths.

Reference: Hagenauer & Hoeher, GLOBECOM 1989. The implementation
follows the standard sliding-window backward update.

Per-step:
  - For each state s at step t:
    - Find the two predecessors that can reach s (they both contribute
      bit = s&1, the LSB of s).
    - Survivor = lower-metric predecessor.
    - Delta(t, s) = pm(loser) - pm(winner): how much worse the
      losing-path is. Large delta = recent history is confident.
  - Update: walk backward from current step along survivor's traceback,
    and for each bit that the *losing* path's history would have decided
    differently, update the bit's reliability via min(current_rel, Delta).

The "would have decided differently" set is determined by the symmetric
difference of the bit-histories of the two predecessors. For k=7,
the two predecessors of state s have states differing only in the
MSB (state bit k-2), since:
   s reached from (prev_a, bit) → prev_a = (s >> 1)
   s reached from (prev_b, bit) → prev_b = (s >> 1) | (1 << (k-1))
The two prev states differ in bit k-2 of their state. Their histories
diverged at some earlier step. Practically, the standard approximation
is: update the last ~5K survivor positions, applying Delta as a
min-update to each.

This is a known acceptable simplification — it over-applies Delta in
some cases (slightly pessimistic reliabilities) but it's what's used
in the textbook implementations and the deck-of-papers comparisons.
"""

from collections import deque
from typing import List
from .fec import SoftViterbi, SOFT_IDEAL, depuncture_soft


# Update-window length. Textbook value: ~5*K = 30-35 for k=7.
SOVA_WINDOW = 30


class SOVAResult:
    """Soft-output Viterbi decode result."""
    __slots__ = ('bits', 'path_metric', 'bit_reliability')

    def __init__(self, bits, path_metric, bit_reliability):
        self.bits = bits
        self.path_metric = path_metric
        self.bit_reliability = bit_reliability


def canonical_sova(soft_code_values: List[int], fec_mode: int = 0,
                   expect_tail: bool = True,
                   window: int = SOVA_WINDOW) -> SOVAResult:
    """Canonical Hagenauer-Hoeher SOVA (any FEC mode; punctured input
    is depunctured with neutral erasures).

    Returns SOVAResult with bits, final path metric, and per-bit
    reliability (sliding-window backward-propagated metric differences).
    """
    v = SoftViterbi(fec_mode)
    # Punctured modes: insert 0-valued erasures so every trellis step sees
    # two code values. With the abs() metric an erasure costs both
    # branches SOFT_IDEAL, i.e. it is neutral, which is the intent.
    soft_code_values = depuncture_soft(soft_code_values, fec_mode)
    N = len(soft_code_values) // 2
    n_states = v.n_states
    INF = 1 << 30
    k_mask = n_states - 1
    half_states = n_states >> 1  # high-bit set means alt predecessor

    pm = [INF] * n_states
    pm[0] = 0

    bp = [[0] * n_states for _ in range(N)]
    bit_of = [[0] * n_states for _ in range(N)]
    # delta[t][s] = path-metric gap between survivor and the loser
    # predecessor (both arrive via same bit). Captures "how confident
    # is the survivor's recent history".
    delta = [[INF] * n_states for _ in range(N)]

    exp_for = (-SOFT_IDEAL, SOFT_IDEAL)

    for step in range(N):
        r0 = soft_code_values[2 * step]
        r1 = soft_code_values[2 * step + 1]
        new_pm = [INF] * n_states

        for new_state in range(n_states):
            # Exactly two predecessors of new_state in a k-bit shift-register
            # trellis. Both contribute the same bit (= new_state & 1).
            b = new_state & 1
            prev_a = new_state >> 1
            prev_b = (new_state >> 1) | half_states  # MSB set
            # Both predecessors are valid trellis states (0..n_states-1)
            # since prev_b < n_states by construction.
            # The output symbols emitted depend on (prev, bit):
            c0a, c1a = v.next_outputs[prev_a][b]
            c0b, c1b = v.next_outputs[prev_b][b]

            ma = pm[prev_a]
            mb = pm[prev_b]
            if ma < INF:
                ma = ma + abs(r0 - exp_for[c0a]) + abs(r1 - exp_for[c1a])
            if mb < INF:
                mb = mb + abs(r0 - exp_for[c0b]) + abs(r1 - exp_for[c1b])

            if ma < mb:
                new_pm[new_state] = ma
                bp[step][new_state] = prev_a
                bit_of[step][new_state] = b
                delta[step][new_state] = (mb - ma) if mb < INF else INF
            else:
                new_pm[new_state] = mb
                bp[step][new_state] = prev_b
                bit_of[step][new_state] = b
                delta[step][new_state] = (ma - mb) if ma < INF else INF

        pm = new_pm

    # End state
    if expect_tail:
        end_state = 0
    else:
        end_state = min(range(n_states), key=lambda s: pm[s])

    # Backward traceback collecting survivor bits and local deltas
    survivor_bits = []
    survivor_deltas = []
    state = end_state
    for step in range(N - 1, -1, -1):
        survivor_bits.append(bit_of[step][state])
        survivor_deltas.append(delta[step][state])
        state = bp[step][state]
    # survivor_bits[0] is the LATEST step in time (N-1); we want
    # bits in chronological order, so reverse later.

    # Sliding-window minimum: for each survivor position, the
    # reliability is min(delta) over the window ending at this position
    # (looking forward in time, since a competing-path divergence
    # at time t affects bit decisions at times ≤ t).
    #
    # We're walking from latest-time (index 0) to earliest-time
    # (index N-1). The window of `window` steps "after this position
    # in time" corresponds to "before in our list".
    #
    # Position i in survivor_deltas corresponds to time N-1-i.
    # A divergence at time t affects bits at times [t-W+1, t]
    # = indices [N-1-t, N-1-t+W-1].
    # So as we scan i from 0 upward (latest to earliest time), the
    # divergence delta at position i (time N-1-i) affects positions
    # [i, i+W-1] in the list.
    # Equivalently: reliability[j] = min over i in [j-W+1, j] of delta[i]
    # where j > i means earlier time.
    # That's a sliding minimum over the previous W positions in our scan.

    reliabilities = [INF] * len(survivor_deltas)
    dq = deque()  # (index, delta) — monotonic increasing delta
    for i, d in enumerate(survivor_deltas):
        # Remove from back any entries with delta >= current (not min candidates)
        while dq and dq[-1][1] >= d:
            dq.pop()
        dq.append((i, d))
        # Remove front entries outside the window [i-window+1, i]
        while dq and dq[0][0] < i - window + 1:
            dq.popleft()
        reliabilities[i] = dq[0][1]

    # Reverse to chronological order
    bits_out = list(reversed(survivor_bits))
    rels_out = list(reversed(reliabilities))

    if expect_tail:
        bits_out = bits_out[:-v.tail]
        rels_out = rels_out[:-v.tail]

    return SOVAResult(bits_out, pm[end_state], rels_out)
