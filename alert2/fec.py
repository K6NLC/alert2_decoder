# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Forward Error Correction stages for ALERT2.

Three stages, applied in this order on the receive side:
  1. Convolutional decode (Viterbi) - undo the rate 1/2 CC
  2. Reed-Solomon decode - correct burst errors
  3. MLS descramble - undo the scrambler (recursive; was self-synchronizing
     on TX so descrambler is non-recursive)
"""
import ctypes
import ctypes.util

try:                      # numpy powers the vectorized soft Viterbi.
    import numpy as np    # fec.py is otherwise numpy-free by design
except ImportError:       # (see demod.py) — so this is optional and the
    np = None             # decoder falls back to the pure-Python trellis.
from typing import Optional, Tuple
from .constants import (
    MLS_PRELOAD, MLS_TAPS, RS_SYMSIZE, RS_GFPOLY, RS_FCR, RS_PRIM,
    CONV_PARAMS, FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY,
)


# =============================================================================
# Scrambler / Descrambler (per AirLink spec §3.4)
# =============================================================================

class MLSScrambler:
    """17-bit MLS scrambler — tap polynomial x^17 + x^3 + 1.

    Recursive (self-synchronizing) scrambler:
        out_bit = in_bit XOR sr[17] XOR sr[3]
        sr shifts; new sr[1] = out_bit
    """

    def __init__(self):
        self.reset()

    def reset(self):
        # Use array of bits, indices 1..17 (index 0 unused)
        self.sr = [0] * 18
        self.sr[1] = 1  # MLS_PRELOAD = 0x00001

    def step(self, in_bit: int) -> int:
        out = in_bit ^ self.sr[17] ^ self.sr[3]
        for i in range(17, 1, -1):
            self.sr[i] = self.sr[i - 1]
        self.sr[1] = out
        return out


def scramble_block(data: bytes) -> bytes:
    """Scramble a block of bytes (used in encoding direction).

    Reset scrambler before each block per spec §3.4.
    """
    s = MLSScrambler()
    out = bytearray(len(data))
    for byte_idx, b in enumerate(data):
        ob = 0
        for bit_idx in range(7, -1, -1):
            in_bit = (b >> bit_idx) & 1
            out_bit = s.step(in_bit)
            ob |= (out_bit << bit_idx)
        out[byte_idx] = ob
    return bytes(out)


def descramble_block(data: bytes) -> bytes:
    """Descramble a block of bytes.

    For a recursive scrambler, the descrambler is non-recursive:
        out_bit = in_bit XOR sr[17] XOR sr[3]
        sr shifts; new sr[1] = in_bit (NOT out_bit)

    Reset descrambler before each block per spec §3.4.
    """
    sr = [0] * 18
    sr[1] = 1
    out = bytearray(len(data))
    for byte_idx, b in enumerate(data):
        ob = 0
        for bit_idx in range(7, -1, -1):
            in_bit = (b >> bit_idx) & 1
            out_bit = in_bit ^ sr[17] ^ sr[3]
            ob |= (out_bit << bit_idx)
            for i in range(17, 1, -1):
                sr[i] = sr[i - 1]
            sr[1] = in_bit
        out[byte_idx] = ob
    return bytes(out)


# =============================================================================
# Reed-Solomon (via libfec)
# =============================================================================

# Lazy load libfec
_libfec = None
_rs_handle_cache = {}


def _load_libfec():
    global _libfec
    if _libfec is not None:
        return _libfec
    try:
        _libfec = ctypes.CDLL("libfec.so.0")
    except OSError:
        try:
            _libfec = ctypes.CDLL("libfec.so")
        except OSError:
            raise RuntimeError(
                "libfec not found. Install with: "
                "sudo apt install libfec-dev (Linux) or build from "
                "http://www.ka9q.net/code/fec/"
            )
    _libfec.init_rs_int.argtypes = [ctypes.c_int] * 6
    _libfec.init_rs_int.restype = ctypes.c_void_p
    _libfec.decode_rs_int.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int),
                                       ctypes.POINTER(ctypes.c_int),
                                       ctypes.c_int]
    _libfec.decode_rs_int.restype = ctypes.c_int
    _libfec.encode_rs_int.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int),
                                       ctypes.POINTER(ctypes.c_int)]
    _libfec.encode_rs_int.restype = None
    _libfec.free_rs_int.argtypes = [ctypes.c_void_p]
    _libfec.free_rs_int.restype = None
    return _libfec


def _get_rs_handle(data_len: int, parity_len: int):
    """Get or create a libfec RS handle for the given block sizes."""
    key = (data_len, parity_len)
    if key in _rs_handle_cache:
        return _rs_handle_cache[key]
    libfec = _load_libfec()
    pad = 255 - data_len - parity_len
    if pad < 0:
        raise ValueError(f"Block too large: {data_len}+{parity_len} > 255")
    handle = libfec.init_rs_int(RS_SYMSIZE, RS_GFPOLY, RS_FCR, RS_PRIM,
                                 parity_len, pad)
    if not handle:
        raise RuntimeError("init_rs_int failed")
    _rs_handle_cache[key] = handle
    return handle


def rs_encode(data: bytes, parity_len: int) -> bytes:
    """Encode data with RS, returning data + parity bytes."""
    libfec = _load_libfec()
    rs = _get_rs_handle(len(data), parity_len)
    arr = (ctypes.c_int * (len(data) + parity_len))()
    for i, b in enumerate(data):
        arr[i] = b
    parity = (ctypes.c_int * parity_len)()
    libfec.encode_rs_int(rs, arr, parity)
    return bytes(data) + bytes(p & 0xFF for p in parity)


def rs_decode(codeword: bytes, data_len: int,
              parity_len: int) -> Tuple[Optional[bytes], int]:
    """Decode an RS codeword.

    Returns (data, num_corrections) on success.
    Returns (None, -1) on uncorrectable error.
    """
    libfec = _load_libfec()
    rs = _get_rs_handle(data_len, parity_len)
    arr = (ctypes.c_int * (data_len + parity_len))()
    for i, b in enumerate(codeword):
        arr[i] = b
    eras_pos = (ctypes.c_int * parity_len)()
    n = libfec.decode_rs_int(rs, arr, eras_pos, 0)
    if n < 0:
        return None, -1
    return bytes(arr[i] & 0xFF for i in range(data_len)), n


# =============================================================================
# Convolutional encoder
# =============================================================================

# =============================================================================
# Puncturing (AirLink spec §3.5.2, Figure 3-7)
# =============================================================================
#
# Modes 1 and 2 use the same k=9 mother code (0x1AF / 0x11D) as a rate-1/2
# encoder and then DELETE output bits in a fixed pattern to reach rate 2/3
# or 3/4. The pattern is a 2-row matrix (row A, row B), applied "column down
# then row across": for input step i the column is i mod P, and output bit
# A_i is sent iff row A[col] == 1, likewise B_i.
#
#   mode 1: [[1,0],[1,1]]      -> A1 B1 A2 B2 with A2 dropped  = 3 of 4
#   mode 2: [[1,0,1],[1,1,0]]  -> A1 B1 A2 B2 A3 B3, drop A2,B3 = 4 of 6
#
# The receiver re-inserts the deleted positions as ERASURES (soft value 0,
# equidistant from both branch hypotheses) and runs the ordinary rate-1/2
# Viterbi. No trellis change is needed; the decoder simply gets no
# information at punctured positions.

def _puncture_pattern(fec_mode: int):
    p = CONV_PARAMS[fec_mode].get('puncture')
    if not p:
        return None
    return p[0], p[1]


def cc_bits_for_input(n_input_bits: int, fec_mode: int = 0,
                      append_tail: bool = True) -> int:
    """Transmitted CC bits for n input bits (tail included if requested).

    Counts the kept positions of the puncture pattern exactly, so partial
    pattern groups at the end of a block are handled the way the encoder
    handles them rather than by a fractional rate estimate.
    """
    k = CONV_PARAMS[fec_mode]['k']
    n = n_input_bits + ((k - 1) if append_tail else 0)
    pat = _puncture_pattern(fec_mode)
    if pat is None:
        return 2 * n
    ra, rb = pat
    P = len(ra)
    full, rem = divmod(n, P)
    kept = sum(ra) + sum(rb)
    out = full * kept
    for i in range(rem):
        out += ra[i] + rb[i]
    return out


def _pad8(n: int) -> int:
    return (n + 7) // 8 * 8


def first_block_cc_bits(fec_mode: int = 0) -> int:
    """CC bits actually carrying the first block (what the Viterbi sees).

    mode 0: 40 B -> 320 + 6 tail = 326 in -> 652 out
    mode 1: 64 B -> 512 + 8 tail = 520 in -> 780 out
    mode 2: 74 B -> 592 + 8 tail = 600 in -> 800 out
    """
    total = FIRST_BLOCK_DATA[fec_mode] + RS_PARITY[fec_mode]
    return cc_bits_for_input(total * 8, fec_mode, append_tail=True)


def first_block_cc_bits_padded(fec_mode: int = 0) -> int:
    """On-air length of the first block including byte-alignment padding.

    mode 0: 656 (82 B), mode 1: 784 (98 B), mode 2: 800 (100 B) — matching
    the spec's Figure 3-8 totals. The follow-on stream starts here.
    """
    return _pad8(first_block_cc_bits(fec_mode))


def followon_cc_bits(total_rs_bytes: int, fec_mode: int = 0) -> int:
    """CC bits carrying the combined follow-on stream (single tail at end)."""
    return cc_bits_for_input(total_rs_bytes * 8, fec_mode, append_tail=True)


def followon_cc_bits_padded(total_rs_bytes: int, fec_mode: int = 0) -> int:
    return _pad8(followon_cc_bits(total_rs_bytes, fec_mode))


def puncture_bits(pairs_flat, fec_mode: int = 0):
    """Apply the mode's puncture pattern to a flat [A0,B0,A1,B1,...] list."""
    pat = _puncture_pattern(fec_mode)
    if pat is None:
        return list(pairs_flat)
    ra, rb = pat
    P = len(ra)
    out = []
    n = len(pairs_flat) // 2
    for i in range(n):
        c = i % P
        if ra[c]:
            out.append(pairs_flat[2 * i])
        if rb[c]:
            out.append(pairs_flat[2 * i + 1])
    return out


def depuncture_soft(received, fec_mode: int = 0, n_steps: int = None):
    """Re-insert punctured positions as erasures (0) -> 2*n_steps values.

    `received` is the soft (or +/-ideal hard-mapped) stream as transmitted.
    If n_steps is None it is inferred from the pattern and the length of
    `received`. Any trailing received values beyond the last full step are
    ignored (they are byte-alignment padding).
    """
    pat = _puncture_pattern(fec_mode)
    if pat is None:
        if n_steps is None:
            n_steps = len(received) // 2
        return list(received[:2 * n_steps])
    ra, rb = pat
    P = len(ra)
    kept_per_group = sum(ra) + sum(rb)
    if n_steps is None:
        full = len(received) // kept_per_group
        rem_bits = len(received) - full * kept_per_group
        extra = 0
        for i in range(P):
            need = ra[i] + rb[i]
            if rem_bits >= need and need > 0:
                rem_bits -= need
                extra += 1
            else:
                break
        n_steps = full * P + extra
    out = []
    j = 0
    L = len(received)
    for i in range(n_steps):
        c = i % P
        if ra[c]:
            out.append(received[j] if j < L else 0); j += 1
        else:
            out.append(0)
        if rb[c]:
            out.append(received[j] if j < L else 0); j += 1
        else:
            out.append(0)
    return out


def conv_encode(input_bits, fec_mode: int = 0, append_tail: bool = True):
    """Convolutionally encode bits per AirLink spec §3.5.2.

    Returns a list of output bits.

    For FEC mode 0: rate 1/2, k=7, polys 0x6D and 0x4F;
    output bits = 2 * input_bits + 12 (tail) if append_tail.
    Modes 1 and 2: k=9, polys 0x1AF / 0x11D, then punctured to rate 2/3
    or 3/4 (see puncture_bits). Use cc_bits_for_input() for the length.
    """
    params = CONV_PARAMS[fec_mode]
    poly_a = params['poly_a']
    poly_b = params['poly_b']
    k = params['k']
    tail_bits = k - 1
    sr_mask = (1 << k) - 1

    input_with_tail = list(input_bits) + ([0] * tail_bits if append_tail else [])
    out = []
    sr = 0
    for b in input_with_tail:
        sr = ((sr << 1) | b) & sr_mask
        c0 = bin(sr & poly_a).count('1') & 1
        c1 = bin(sr & poly_b).count('1') & 1
        out.append(c0)
        out.append(c1)
    return puncture_bits(out, fec_mode)


# =============================================================================
# Viterbi decoder
# =============================================================================

class Viterbi:
    """Hard-decision Viterbi decoder for k=7 r=1/2 convolutional code."""

    def __init__(self, fec_mode: int = 0):
        # k=7 (64 states) for mode 0; k=9 (256 states) for modes 1 and 2.
        # The trellis below is generic in k. Punctured modes must be
        # DEPUNCTURED (depuncture_soft) before calling decode_soft: the
        # decoder always consumes 2 code values per input bit.
        self.params = CONV_PARAMS[fec_mode]
        self.fec_mode = fec_mode
        self.k = self.params['k']
        self.poly_a = self.params['poly_a']
        self.poly_b = self.params['poly_b']
        self.tail = self.k - 1
        self.n_states = 1 << (self.k - 1)

        # Precompute output bits for each (state, input) transition
        self.next_outputs = [[None, None] for _ in range(self.n_states)]
        for state in range(self.n_states):
            for b in (0, 1):
                sr = (state << 1) | b
                sr &= (1 << self.k) - 1
                c0 = bin(sr & self.poly_a).count('1') & 1
                c1 = bin(sr & self.poly_b).count('1') & 1
                self.next_outputs[state][b] = (c0, c1)

    def decode(self, code_bits, expect_tail: bool = True):
        """Decode a list of code bits (must be even length).

        Returns (decoded_bits, path_metric).
        If expect_tail=True, decoded length = N - tail where N = len(code_bits)/2.
        """
        N = len(code_bits) // 2
        INF = 1 << 30
        pm = [INF] * self.n_states
        pm[0] = 0
        bp = [[0] * self.n_states for _ in range(N)]
        bit_of = [[0] * self.n_states for _ in range(N)]

        for step in range(N):
            c0_rx = code_bits[2 * step]
            c1_rx = code_bits[2 * step + 1]
            new_pm = [INF] * self.n_states
            for prev_state in range(self.n_states):
                if pm[prev_state] >= INF:
                    continue
                for b in (0, 1):
                    new_state = ((prev_state << 1) | b) & (self.n_states - 1)
                    c0, c1 = self.next_outputs[prev_state][b]
                    metric = pm[prev_state] + (c0 ^ c0_rx) + (c1 ^ c1_rx)
                    if metric < new_pm[new_state]:
                        new_pm[new_state] = metric
                        bp[step][new_state] = prev_state
                        bit_of[step][new_state] = b
            pm = new_pm

        if expect_tail:
            end_state = 0
        else:
            end_state = min(range(self.n_states), key=lambda s: pm[s])

        bits_out = []
        state = end_state
        for step in range(N - 1, -1, -1):
            bits_out.append(bit_of[step][state])
            state = bp[step][state]
        bits_out.reverse()

        if expect_tail:
            bits_out = bits_out[:-self.tail]
        return bits_out, pm[end_state]


# =============================================================================
# Soft-decision Viterbi (FEC mode 0 only)
#
# See docs/SOFT_VITERBI.md for the full design rationale. Summary:
#   - Branch metric: absolute-value (linear), NOT squared-Euclidean.
#     Linear is ~0.2 dB worse than squared in pure AWGN but far more
#     robust to the impulse interference that dominates the shop site.
#   - Quantization: 4-bit signed, range [-8, +7]. Captures essentially
#     all soft-decision gain (Heller & Jacobs 1971) and acts as soft
#     saturation against outliers.
#   - Sign convention: positive soft value -> bit 1, negative -> bit 0.
#     This matches slice_soft_samples (1 if v > threshold else 0) after
#     threshold normalization.
# =============================================================================

# Quantized "ideal" symbol value. We saturate the expected codeword symbol
# at +/-SOFT_IDEAL rather than the full +/-8 rail, leaving headroom so that
# constructive-interference samples that overshoot don't get a worse metric
# than a perfectly-placed sample.
SOFT_IDEAL = 6
SOFT_CLIP = 8  # quantizer clips received values to [-SOFT_CLIP, +SOFT_CLIP-1]


def quantize_soft(soft_samples, amplitude_estimate, n_bits: int = 4):
    """Quantize float soft samples to signed n-bit ints.

    Args:
        soft_samples: 1-D sequence of float soft values (already roughly
                      zero-centered; demod DC-removes upstream).
        amplitude_estimate: estimate of the nominal symbol amplitude |A|.
                            Used to scale so a clean symbol lands near
                            +/-SOFT_IDEAL.
        n_bits: quantizer bit depth (default 4 -> range [-8, +7]).

    Returns:
        list[int] of quantized values clipped to [-2^(n_bits-1),
        2^(n_bits-1) - 1].

    Defensive behavior:
        - amplitude_estimate <= 0 or non-finite -> returns all zeros
          (equivalent to "no confidence"; Viterbi still runs but every
          branch metric ties, so it degrades gracefully rather than
          crashing).
        - NaN/inf soft values are treated as 0.
    """
    import math
    hi = (1 << (n_bits - 1)) - 1   # +7 for 4-bit
    lo = -(1 << (n_bits - 1))      # -8 for 4-bit

    out = []
    if (amplitude_estimate is None
            or not math.isfinite(amplitude_estimate)
            or amplitude_estimate <= 0):
        return [0] * len(soft_samples)

    # Scale so that a sample at +amplitude_estimate maps to +SOFT_IDEAL.
    scale = SOFT_IDEAL / amplitude_estimate
    for v in soft_samples:
        if v is None or not math.isfinite(v):
            out.append(0)
            continue
        q = int(round(v * scale))
        if q > hi:
            q = hi
        elif q < lo:
            q = lo
        out.append(q)
    return out


def estimate_amplitude(soft_samples) -> float:
    """Estimate nominal symbol amplitude |A| from soft samples.

    Uses the median of absolute values, which is robust to the impulse
    outliers present at the shop site (a mean would be inflated by spikes).
    Operates on up to the first 512 samples — enough to get a stable
    estimate from the preamble + start of payload without being skewed by
    a late-burst interference event.

    Returns 0.0 if no usable samples (caller treats that as "no confidence").
    """
    import math
    vals = []
    for i, v in enumerate(soft_samples):
        if i >= 512:
            break
        if v is not None and math.isfinite(v):
            vals.append(abs(v))
    if not vals:
        return 0.0
    vals.sort()
    n = len(vals)
    if n % 2 == 1:
        return float(vals[n // 2])
    return float((vals[n // 2 - 1] + vals[n // 2]) / 2.0)


class SoftViterbi(Viterbi):
    """Soft-decision Viterbi for any FEC mode (k=7 r=1/2 in mode 0; k=9
    punctured r=2/3 / r=3/4 in modes 1 / 2 once the input is depunctured).

    Subclasses the hard Viterbi: identical trellis, transition table, and
    traceback. The ONLY difference is the branch metric, which uses the
    quantized soft values' magnitudes as confidence weights.

    Use decode_soft() with quantized inputs. The inherited decode() still
    works for hard inputs if ever needed.
    """

    def _soft_tables(self):
        """Per-new-state predecessor/branch tables for the vectorized
        trellis (built once per decoder instance).

        For new_state s with n_states = 2^k:
            s = ((prev << 1) | b) & (n_states - 1)
        so the branch bit is b = s & 1 (the same for both predecessors),
        and the two predecessors are p0 = s >> 1 and p1 = p0 + n/2.
        That also means bit_of[step][s] is always s & 1, so no separate
        bit table is needed for traceback.
        """
        if getattr(self, '_sv_tables', None) is not None:
            return self._sv_tables
        if np is None:
            return None
        ns = self.n_states
        s = np.arange(ns, dtype=np.int64)
        b = s & 1
        p0 = s >> 1
        p1 = p0 + (ns >> 1)
        exp = np.array([-SOFT_IDEAL, SOFT_IDEAL], dtype=np.int64)
        e0 = np.empty((2, ns), dtype=np.int64)
        e1 = np.empty((2, ns), dtype=np.int64)
        for i, preds in enumerate((p0, p1)):
            for j in range(ns):
                c0, c1 = self.next_outputs[int(preds[j])][int(b[j])]
                e0[i, j] = exp[c0]
                e1[i, j] = exp[c1]
        self._sv_tables = (p0, p1, e0, e1)
        return self._sv_tables

    def decode_soft(self, soft_code_values, expect_tail: bool = True):
        """Vectorized soft-decision Viterbi (rc24).

        Bit-exact replacement for the original triple-nested Python
        loop (kept as decode_soft_ref and asserted equal in the test
        suite). Same trellis, same metric, same tie-breaking — the only
        change is that each step's 2*n_states branch metrics are
        computed as numpy array operations instead of Python loops.

        Profiling on a live-sized chunk showed this function taking 68%
        of all decode time: 28 framesync candidates per chunk, each
        running ~510 trellis steps x 64 states x 2 branches in pure
        Python (~6.9M abs() calls per three chunks). That was the
        binding constraint on the Pi 4, which had to drop from 4
        multiphase candidates to 2.

        Tie-breaking note: the original visits predecessors in
        increasing prev_state order and writes only on strict <, so
        ties go to p0. `take1 = cand1 < cand0` reproduces that exactly.

        INF note: the original skips predecessors whose metric is INF
        and never writes them, so an unreachable state keeps exactly
        INF (and its backpointer keeps its initial 0). Clamping the
        step result at INF and forcing those backpointers to 0
        reproduces both.
        """
        if np is None:    # numpy-free environment: identical results,
            return self.decode_soft_ref(   # just slower
                soft_code_values, expect_tail=expect_tail)
        N = len(soft_code_values) // 2
        ns = self.n_states
        INF = 1 << 30
        if N == 0:
            return [], 0
        p0, p1, e0, e1 = self._soft_tables()
        r = np.asarray(soft_code_values[:2 * N],
                       dtype=np.int64).reshape(N, 2)
        # Branch metrics for every step and every new-state, computed in
        # one shot: |r0 - expected0| + |r1 - expected1|.
        bm0 = (np.abs(r[:, 0:1] - e0[0][None, :])
               + np.abs(r[:, 1:2] - e1[0][None, :]))
        bm1 = (np.abs(r[:, 0:1] - e0[1][None, :])
               + np.abs(r[:, 1:2] - e1[1][None, :]))
        pm = np.full(ns, INF, dtype=np.int64)
        pm[0] = 0
        bp = np.zeros((N, ns), dtype=np.bool_)
        half = ns >> 1
        for step in range(N):
            cand0 = pm[:half].repeat(2) + bm0[step]
            cand1 = pm[half:].repeat(2) + bm1[step]
            take1 = cand1 < cand0
            pm = np.where(take1, cand1, cand0)
            dead = pm >= INF
            if dead.any():
                pm[dead] = INF
                take1[dead] = False
            bp[step] = take1
        if expect_tail:
            end_state = 0
        else:
            end_state = int(np.argmin(pm))
        bits_out = np.empty(N, dtype=np.int8)
        state = end_state
        bp_l = bp.tolist()
        for step in range(N - 1, -1, -1):
            bits_out[step] = state & 1
            state = (state >> 1) + half if bp_l[step][state] else (state >> 1)
        out = [int(x) for x in bits_out]
        if expect_tail:
            out = out[:-self.tail] if self.tail else out
        return out, int(pm[end_state])

    def decode_soft_ref(self, soft_code_values, expect_tail: bool = True):
        """Decode quantized soft code values.

        Args:
            soft_code_values: sequence of quantized ints (from
                quantize_soft), length 2*N. Sign = bit decision
                (positive -> 1), magnitude = confidence.
            expect_tail: if True, the last `tail` decoded bits are the
                CC flush and are dropped; end state forced to 0.

        Returns:
            (decoded_bits, path_metric) — same shape as Viterbi.decode().

        Branch metric (per design doc 3b/4):
            For each of the two code symbols in a step, the expected
            transmitted value is +SOFT_IDEAL for code bit 1 and
            -SOFT_IDEAL for code bit 0. The metric contribution is the
            absolute difference between the received quantized value and
            that expected value. Lower is better; a confident-correct
            symbol contributes ~0, a confident-wrong symbol contributes
            ~2*SOFT_IDEAL, and a near-zero (uncertain) symbol contributes
            ~SOFT_IDEAL regardless of the hypothesized bit (so it doesn't
            bias the path either way).
        """
        N = len(soft_code_values) // 2
        INF = 1 << 30
        pm = [INF] * self.n_states
        pm[0] = 0
        bp = [[0] * self.n_states for _ in range(N)]
        bit_of = [[0] * self.n_states for _ in range(N)]

        # Expected quantized value for a code bit: 1 -> +IDEAL, 0 -> -IDEAL
        exp_for = (-SOFT_IDEAL, SOFT_IDEAL)  # index by code bit (0 or 1)

        for step in range(N):
            r0 = soft_code_values[2 * step]
            r1 = soft_code_values[2 * step + 1]
            new_pm = [INF] * self.n_states
            for prev_state in range(self.n_states):
                ppm = pm[prev_state]
                if ppm >= INF:
                    continue
                for b in (0, 1):
                    new_state = ((prev_state << 1) | b) & (self.n_states - 1)
                    c0, c1 = self.next_outputs[prev_state][b]
                    metric = (ppm
                              + abs(r0 - exp_for[c0])
                              + abs(r1 - exp_for[c1]))
                    if metric < new_pm[new_state]:
                        new_pm[new_state] = metric
                        bp[step][new_state] = prev_state
                        bit_of[step][new_state] = b
            pm = new_pm

        if expect_tail:
            end_state = 0
        else:
            end_state = min(range(self.n_states), key=lambda s: pm[s])

        bits_out = []
        state = end_state
        for step in range(N - 1, -1, -1):
            bits_out.append(bit_of[step][state])
            state = bp[step][state]
        bits_out.reverse()

        if expect_tail:
            bits_out = bits_out[:-self.tail]
        return bits_out, pm[end_state]


# =============================================================================
# Soft-input full-decode pipeline (mirrors the hard decode_* functions)
#
# These take quantized soft code values instead of hard bits. RS decode and
# descramble are byte-domain and identical to the hard path — only the CC
# (Viterbi) stage changes.
# =============================================================================

def decode_first_block_soft(soft_cc_values, fec_mode: int = 0):
    """Soft-decision counterpart of decode_first_block().

    Args:
        soft_cc_values: quantized soft code values for the first block
                        (length matches the hard path's cc_bits slice).
    Returns:
        (descrambled_data_bytes, num_rs_corrections) or (None, -1).
    """
    expected_total = FIRST_BLOCK_DATA[fec_mode] + RS_PARITY[fec_mode]
    v = SoftViterbi(fec_mode)
    # Punctured modes: re-insert erasures so the decoder sees 2 values per
    # input step (data bits + tail). Mode 0 passes straight through.
    soft_full = depuncture_soft(soft_cc_values, fec_mode,
                                expected_total * 8 + (v.k - 1))
    decoded_bits, pm = v.decode_soft(soft_full, expect_tail=True)
    rs_codeword = bits_to_bytes(decoded_bits)

    if len(rs_codeword) < expected_total:
        return None, -1
    rs_codeword = rs_codeword[:expected_total]

    rs_data, n_corr = rs_decode(rs_codeword, FIRST_BLOCK_DATA[fec_mode],
                                RS_PARITY[fec_mode])
    if rs_data is None:
        return None, -1

    return descramble_block(rs_data), n_corr


def decode_followon_stream_soft_blocks(soft_cc_values, fec_mode: int,
                                        total_data_bytes: int):
    """Block-resolving variant (rc21): decodes the follow-on stream and
    reports per-block RS corrections and how many blocks succeeded.

    Returns (data, per_block_corr, blocks_ok):
      - full success: data = all bytes, blocks_ok == n_blocks
      - partial:      data = bytes of the blocks that decoded before the
                      first RS failure, blocks_ok < n_blocks
      - nothing:      data = b'', blocks_ok = 0
    The Viterbi pass is unchanged (single continuous CC stream); only
    the RS stage stops at the first failing block instead of discarding
    the prefix. This powers per-block RS diag attribution and the
    progressive decision-directed equalizer.
    """
    if total_data_bytes <= 0:
        return b'', [], 0

    full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
    parity = RS_PARITY[fec_mode]
    n_full_blocks = total_data_bytes // full_block_data
    final_block_size = total_data_bytes % full_block_data
    if final_block_size == 0:
        block_sizes = [full_block_data] * n_full_blocks
    else:
        block_sizes = [full_block_data] * n_full_blocks + [final_block_size]
    total_rs_bytes = sum(b + parity for b in block_sizes)

    v = SoftViterbi(fec_mode)
    # Single CC stream over all follow-on blocks, one tail at the end;
    # punctured modes are depunctured over exactly that many steps.
    soft_full = depuncture_soft(soft_cc_values, fec_mode,
                                total_rs_bytes * 8 + (v.k - 1))
    decoded_bits, pm = v.decode_soft(soft_full, expect_tail=True)
    rs_stream = bits_to_bytes(decoded_bits)
    if len(rs_stream) < total_rs_bytes:
        return b'', [], 0
    rs_stream = rs_stream[:total_rs_bytes]

    out_data = bytearray()
    per_block = []
    offset = 0
    blocks_ok = 0
    for block_size in block_sizes:
        codeword_size = block_size + parity
        codeword = rs_stream[offset:offset + codeword_size]
        offset += codeword_size
        rs_data, n_corr = rs_decode(codeword, block_size, parity)
        if rs_data is None:
            break
        out_data.extend(descramble_block(rs_data))
        per_block.append(n_corr)
        blocks_ok += 1
    return bytes(out_data), per_block, blocks_ok


def decode_followon_stream_soft(soft_cc_values, fec_mode: int,
                                 total_data_bytes: int):
    """Soft-decision counterpart of decode_followon_stream().

    Identical block-splitting / RS / descramble logic as the hard path;
    only the Viterbi stage uses soft inputs.
    """
    if total_data_bytes <= 0:
        return b'', 0

    full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
    parity = RS_PARITY[fec_mode]

    n_full_blocks = total_data_bytes // full_block_data
    final_block_size = total_data_bytes % full_block_data
    if final_block_size == 0:
        block_sizes = [full_block_data] * n_full_blocks
    else:
        block_sizes = [full_block_data] * n_full_blocks + [final_block_size]

    total_rs_bytes = sum(b + parity for b in block_sizes)

    v = SoftViterbi(fec_mode)
    soft_full = depuncture_soft(soft_cc_values, fec_mode,
                                total_rs_bytes * 8 + (v.k - 1))
    decoded_bits, pm = v.decode_soft(soft_full, expect_tail=True)
    rs_stream = bits_to_bytes(decoded_bits)
    if len(rs_stream) < total_rs_bytes:
        return None, -1
    rs_stream = rs_stream[:total_rs_bytes]

    out_data = bytearray()
    total_corr = 0
    offset = 0
    for block_size in block_sizes:
        codeword_size = block_size + parity
        codeword = rs_stream[offset:offset + codeword_size]
        offset += codeword_size
        rs_data, n_corr = rs_decode(codeword, block_size, parity)
        if rs_data is None:
            return None, -1
        out_data.extend(descramble_block(rs_data))
        total_corr += n_corr

    return bytes(out_data), total_corr


# =============================================================================
# Helpers for bit/byte conversion
# =============================================================================

def bytes_to_bits(data: bytes) -> list:
    """Convert bytes to MSB-first bit list."""
    out = []
    for b in data:
        for i in range(7, -1, -1):
            out.append((b >> i) & 1)
    return out


def bits_to_bytes(bits) -> bytes:
    """Convert MSB-first bit list to bytes (truncates partial last byte)."""
    out = bytearray()
    n_full = len(bits) // 8
    for byte_idx in range(n_full):
        b = 0
        for bit_idx in range(8):
            b |= bits[byte_idx * 8 + bit_idx] << (7 - bit_idx)
        out.append(b)
    return bytes(out)


# =============================================================================
# Full decode pipeline for one block
# =============================================================================

def decode_first_block(cc_bits, fec_mode: int = 0):
    """Decode a first-block's worth of convolutional bits.

    Returns: (descrambled_data_bytes, num_rs_corrections) or (None, -1).
    """
    expected_total = FIRST_BLOCK_DATA[fec_mode] + RS_PARITY[fec_mode]
    if fec_mode != 0:
        # Hard bits for a punctured mode: map to +/-SOFT_IDEAL, depuncture
        # with erasures, and use the soft decoder (the hard trellis has no
        # erasure metric). Identical decisions to a hard decode elsewhere.
        return decode_first_block_soft(
            [SOFT_IDEAL if b else -SOFT_IDEAL for b in cc_bits], fec_mode)

    # Convolutional decode
    v = Viterbi(fec_mode)
    decoded_bits, pm = v.decode(cc_bits, expect_tail=True)
    rs_codeword = bits_to_bytes(decoded_bits)

    if len(rs_codeword) < expected_total:
        return None, -1

    rs_codeword = rs_codeword[:expected_total]

    # RS decode
    rs_data, n_corr = rs_decode(rs_codeword, FIRST_BLOCK_DATA[fec_mode],
                                  RS_PARITY[fec_mode])
    if rs_data is None:
        return None, -1

    # Descramble
    descrambled = descramble_block(rs_data)

    return descrambled, n_corr


def decode_followon_block(cc_bits, fec_mode: int = 0,
                            block_data_size: Optional[int] = None):
    """Decode a follow-on block's worth of convolutional bits.

    DEPRECATED for FEC mode 0 — per AirLink spec §3.5.2, all follow-on
    blocks must be CC-encoded together as a single stream. Use
    decode_followon_stream() instead.

    block_data_size: for the final follow-on block which may be shorter
                     than 32 bytes. None means use full follow-on size.
    """
    if fec_mode != 0:
        raise NotImplementedError("Only FEC mode 0 implemented")
    if block_data_size is None:
        block_data_size = FOLLOWON_BLOCK_DATA[fec_mode]

    v = Viterbi(fec_mode)
    decoded_bits, pm = v.decode(cc_bits, expect_tail=True)
    rs_codeword = bits_to_bytes(decoded_bits)
    expected_total = block_data_size + RS_PARITY[fec_mode]
    if len(rs_codeword) < expected_total:
        return None, -1
    rs_codeword = rs_codeword[:expected_total]

    rs_data, n_corr = rs_decode(rs_codeword, block_data_size,
                                  RS_PARITY[fec_mode])
    if rs_data is None:
        return None, -1

    return descramble_block(rs_data), n_corr


def decode_followon_stream(cc_bits, fec_mode: int, total_data_bytes: int):
    """Decode the full follow-on stream (all follow-on blocks combined).

    Per AirLink spec §3.5.2: all follow-on blocks are CC-encoded as a
    single stream, with one CC tail at the end. After CC decoding, the
    resulting RS bytes are split into per-block codewords (each of size
    DATA_BYTES + PARITY_BYTES, with the final block possibly shorter)
    and each RS codeword is decoded separately.

    Args:
        cc_bits: bit stream for ALL follow-on blocks combined, including
                 the trailing CC tail (16 bits = 6 tail + padding)
        fec_mode: FEC mode (only 0 supported)
        total_data_bytes: total number of data bytes across all follow-on
                          blocks (NOT including RS parity)

    Returns:
        (data_bytes, total_rs_corrections) on success
        (None, -1) on failure
    """
    if fec_mode != 0:
        return decode_followon_stream_soft(
            [SOFT_IDEAL if b else -SOFT_IDEAL for b in cc_bits],
            fec_mode, total_data_bytes)
    if total_data_bytes <= 0:
        return b'', 0

    full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
    parity = RS_PARITY[fec_mode]

    # Determine block sizes:
    # - All blocks except the last are full-size (32 bytes data for mode 0)
    # - The final block may be shorter
    n_full_blocks = total_data_bytes // full_block_data
    final_block_size = total_data_bytes % full_block_data
    if final_block_size == 0:
        # Exactly fills full blocks — no shortened final block
        block_sizes = [full_block_data] * n_full_blocks
    else:
        block_sizes = [full_block_data] * n_full_blocks + [final_block_size]

    # Total RS bytes after RS encoding (before CC encoding)
    total_rs_bytes = sum(b + parity for b in block_sizes)

    # CC decode the entire follow-on stream
    v = Viterbi(fec_mode)
    decoded_bits, pm = v.decode(cc_bits, expect_tail=True)
    rs_stream = bits_to_bytes(decoded_bits)
    if len(rs_stream) < total_rs_bytes:
        return None, -1
    rs_stream = rs_stream[:total_rs_bytes]

    # Split into per-block RS codewords and decode each separately
    out_data = bytearray()
    total_corr = 0
    offset = 0
    for block_size in block_sizes:
        codeword_size = block_size + parity
        codeword = rs_stream[offset:offset + codeword_size]
        offset += codeword_size

        rs_data, n_corr = rs_decode(codeword, block_size, parity)
        if rs_data is None:
            return None, -1
        # Descramble this block (each block is descrambled independently
        # per spec §3.4 — MLS register reset per block)
        out_data.extend(descramble_block(rs_data))
        total_corr += n_corr

    return bytes(out_data), total_corr
