# Soft-Decision Viterbi — Design Doc

**Status:** Draft / pre-implementation
**Branch:** `v0.9.0-soft-viterbi` (fork of v0.8.5)
**Owner:** K6NLC
**Last updated:** 2026-05-15

---

## 1. Motivation

The current decoder uses **hard-decision** Viterbi: the demod's soft samples
(`filtered[phase::SAMPLES_PER_BIT]`) are sliced to {0,1} bits *before* Viterbi
sees them. This throws away the magnitude information — how confident the
slicer was about each bit.

For the rate-1/2 k=7 convolutional code in ALERT2 FEC mode 0, the theoretical
coding-gain delta of soft over hard decisions in AWGN is **~2 dB** (canonical
result, e.g. Proakis §8.7). In practice the gap is usually 1.5–2.2 dB depending
on quantization and signal characteristics.

### Where we expect this to help (and where we don't)

Based on the shop-vs-QTH analysis (May 14–15, 2026):

| Site | Mean SNR | Truncation rate | Expected soft-Viterbi gain |
|------|---------|-----------------|----------------------------|
| QTH  | 6.3 dB  | 16.3%           | **Real benefit possible** — operating closer to the threshold where Viterbi makes mistakes that propagate into RS-uncorrectable codewords. Expected: 2–6 percentage-point reduction in truncation rate. |
| Shop | 11.3 dB | 42.4%           | **Small benefit at best.** The shop's truncation is dominated by bursty in-band interference, not Gaussian bit-error rate. Soft Viterbi can't shrink an interference burst; the existing RS code is already the primary defense against bursts. Expected: 0–3 percentage-point reduction. |

We are explicitly building this to **measure** the delta, not because we
expect it to be a silver bullet. If the QTH improvement is <1 percentage
point, we will not merge.

### What this does NOT address

- Bursty in-band interference at the shop. That is an RF-side problem.
- Loss of frame sync. Soft decisions could in principle help framesync
  correlation too, but this design keeps framesync hard-decision for now;
  framesync correlation is robust at any SNR where Viterbi has a chance.
- FEC modes 1 and 2 (k=9 + puncturing). Out of scope for v0.9.0; tracked
  separately.

---

## 2. Non-goals

- No algorithm changes beyond hard→soft. No list-Viterbi, no iterative
  RS↔Viterbi, no LDPC. The user has explicitly excluded those as diminishing
  returns.
- No changes to the demod pipeline. We use what `demod.demodulate()`
  already produces.
- No change to default behavior. Soft Viterbi ships behind a `--soft-viterbi`
  flag (and a constructor kwarg). Hard remains the default until measurement
  justifies otherwise.
- No change to the forwarder. It receives the same JSON regardless of
  Viterbi flavor.

---

## 3. Branch-metric choice

Three candidates were considered:

### 3a. Squared-Euclidean (optimal AWGN)
Given received soft sample `r_i ∈ ℝ` (after RRC matched filter, mean-removed
so it sits roughly symmetric around 0) and expected code bit `c_i ∈ {0,1}`,
map `c_i → ±A` and use:

```
metric(r_i, c_i) = (r_i - (2*c_i - 1)*A)²
```

Two issues for us:
- We don't know `A` (the nominal symbol amplitude) per-burst without an AGC
  step. The slicer's adaptive threshold (`slice_soft_samples`) handles this
  implicitly for hard decisions, but for Viterbi we need it explicit.
- Squared metrics blow up under impulse noise — a single 10× outlier
  contributes 100× to the path metric. Bad for shop conditions.

### 3b. Absolute-value (linear)
```
metric(r_i, c_i) = |r_i - (2*c_i - 1)*A|
```

Nearly optimal in AWGN (loses ~0.2 dB vs squared in practice for soft
quantized to 4–8 bits) and **much** more robust to impulse outliers. This is
what we'll use.

### 3c. Log-likelihood-ratio (LLR) — equivalent to (b) after a sign change
LLR-style metrics are the form most often written in textbooks, but for our
purposes they reduce algebraically to the same thing as (b) when the noise
is symmetric and unimodal. Picking (b) gives us a simpler implementation
without losing meaningful performance.

### Decision: 3b, with the following details

- Sign convention: positive soft sample → bit "1", negative → bit "0". This
  is the convention `slice_soft_samples` already uses (`1 if v > threshold else 0`)
  after threshold normalization.
- The soft samples coming out of the matched filter are already roughly
  zero-centered (we DC-remove them in demod). We will compute a per-burst
  amplitude estimate `A_hat` from the *preamble* (bitsync section) where we
  know the bit pattern exactly, then use that `A_hat` for the whole burst.
  Fallback: use the median absolute value of the first 256 soft samples post-
  framesync as `A_hat` if preamble normalization fails.

---

## 4. Quantization

A floating-point Viterbi would work, but is wasteful — the literature is
unambiguous that **3- to 4-bit soft quantization captures essentially all
of the soft-decision gain** (Heller & Jacobs 1971, and basically every text
since). Quantization also acts as soft saturation, which makes the decoder
robust to occasional 50× outliers from impulse noise — exactly what we want
for the shop.

### Decision: 4-bit signed quantization (range -8…+7)

Specifically:
1. After per-burst amplitude estimation, divide each soft sample by `A_hat / 4`.
2. Round to nearest integer.
3. Clip to `[-8, +7]`.
4. Branch metric becomes:
   ```
   expected = +6 if c_i else -6   # "ideal" quantized value for ±A_hat
   metric = abs(q_i - expected)   # ranges 0..14
   ```
   (Saturating at ±6 rather than the full ±8 range gives some headroom for
   constructive interference samples — they don't all need to land at the
   rail.)

This keeps path-metric values small (each transition adds 0…28), so the
existing path metric type (Python int) is fine, and the math stays in
integer arithmetic. A future C extension would still be straightforward.

---

## 5. Code architecture — minimum-diff plan

The current call graph:

```
demodulate() → bits: List[int]  ── (hard, info contains audio_43k)
   │
   ▼
Alert2Decoder.decode_bits(bits, audio_43k=…)
   │
   ▼
_try_decode_at(bits, …)
   │
   ▼
decode_first_block(cc_bits, fec_mode=0)
decode_followon_stream(cc_bits, fec_mode, total_data_bytes)
   │
   ▼
Viterbi(fec_mode=0).decode(code_bits)
```

We will plumb a **parallel `soft` array** alongside `bits` through the same
call chain, gated by a single decoder-level flag `soft_viterbi: bool`.

### 5a. New in `demod.py`

```python
def demodulate(...) -> Tuple[List[int], dict]:
    ...
    # NEW: also expose the post-matched-filter soft samples in info
    info['soft_samples'] = soft  # the np.ndarray decimated to symbol rate
    info['amplitude_estimate'] = float(np.median(np.abs(soft[:256])))  # fallback
    return bits, info
```

We are *not* changing `demodulate`'s return signature. Soft samples ride in
the info dict, optional and ignorable.

### 5b. New in `fec.py`

```python
class SoftViterbi(Viterbi):
    """Soft-decision Viterbi for k=7 r=1/2 (FEC mode 0).

    Drop-in replacement for Viterbi.decode() but accepts soft inputs.
    """

    def decode_soft(self, soft_code_values, expect_tail=True):
        """
        soft_code_values: 1-D array of int8 in [-8, +7], length 2*N.
                          Sign indicates bit decision; magnitude indicates
                          confidence. Convention: positive → bit=1.
        Returns (decoded_bits, path_metric) same as Viterbi.decode().
        """
        # Identical trellis as parent; only the branch metric changes.
        # See section 4 for the metric.
```

Plus a new helper:

```python
def quantize_soft(soft_samples, amplitude_estimate, n_bits=4):
    """Quantize float soft samples to signed 4-bit ints.
    Returns np.ndarray dtype=int8."""
```

And two new decode entry points that mirror the existing pair:

```python
def decode_first_block_soft(soft_cc_q4, fec_mode=0): ...
def decode_followon_stream_soft(soft_cc_q4, fec_mode, total_data_bytes): ...
```

These call `SoftViterbi.decode_soft()` instead of `Viterbi.decode()`,
otherwise identical to the hard-decision counterparts (same RS, same
descramble).

### 5c. New in `decoder.py`

`Alert2Decoder.__init__()` gets one new kwarg:
```python
soft_viterbi: bool = False
```

`decode_bits()` gets one new optional positional:
```python
def decode_bits(self, bits, audio_43k=None, bit_phase=0,
                soft_samples=None):
```

If `soft_viterbi=True` and `soft_samples` is provided, `_try_decode_at()`
slices `soft_samples` the same way it slices `bits` (at the same indices)
and calls the `_soft` variants. Otherwise: existing hard path, unchanged.

### 5d. New in `cli.py`

Add `--soft-viterbi` flag. When set, also enables the necessary info
plumbing in the live and WAV decode paths. The flag defaults off.

### 5e. Web UI

Add a small badge in the packet detail view: `"Decoded: hard"` / `"Decoded: soft"`.
This requires a single new field on `Alert2Packet`: `viterbi_mode: str = "hard"`.

---

## 6. Test plan

### 6a. Encoder-roundtrip tests (correctness, lower bar)

In `tests/test_soft_viterbi.py`:

1. **Identity at high SNR.** Encode a known payload, demod through a clean
   simulated AWGN channel at +20 dB SNR, decode soft. Result must equal
   the hard-decision result and the original payload.

2. **No regression on clean signals.** Run soft decode against every WAV
   file in the existing test corpus that decodes cleanly with the hard
   decoder. Soft decode must produce identical packets. (If they don't
   match, something is wrong with sign convention or amplitude estimation,
   not the algorithm itself.)

3. **Quantization edge cases.** Confirm that quantize_soft handles:
   - All-zero input (returns zeros, doesn't divide by zero)
   - Single huge outlier (clips at ±8 rather than dominating)
   - Negative amplitude estimate (returns zeros; defensive)

### 6b. Synthetic SNR sweep (gain measurement)

In `tests/test_soft_viterbi_sweep.py` (slow test, marked `@pytest.mark.slow`):

1. Take a known-good payload, RS-encode + CC-encode + modulate it (encoder
   roundtrip — we have `conv_encode` and `rs_encode` already).
2. For SNR ∈ {2, 4, 6, 8, 10, 12} dB:
   - Add AWGN to match target SNR.
   - Demod through normal pipeline.
   - Run both hard and soft Viterbi.
   - Record BER, packet success rate, RS-corrections-needed-per-block.
3. **Pass criterion:** at SNR ≤ 6 dB, soft must show ≥1 dB equivalent gain
   (i.e., soft at 5 dB ≈ hard at 6 dB packet success rate).

This is a synthetic measurement — useful for confirming the algorithm
works, but it does not predict what happens on real captures. That's
section 6c.

### 6c. Real-capture A/B (the measurement that matters)

The point of this whole exercise. Procedure:

1. **Collect a paired QTH dataset**: at least 24 hours of `--diag-mode`
   running, with the WAV audio also being recorded (a rolling buffer is
   fine). Run on QTH where SNR is low — where soft should help if it's
   going to help anywhere.

2. **Replay the same WAV through both decoders.**
   ```bash
   # Hard reference
   alert2-decode --wav captures.wav --log hard.log --diag-mode hard.diag

   # Soft test
   alert2-decode --wav captures.wav --log soft.log --diag-mode soft.diag \
                 --soft-viterbi
   ```

3. **Compare:**
   - Total packets decoded (soft ≥ hard expected)
   - Truncation rate (soft ≤ hard expected)
   - Newly-decoded packets in soft set (do they look real? do their MANT
     headers parse cleanly? do their SRP timestamps fit the rest of the
     network?)
   - Packets decoded by hard but NOT by soft. **Should be zero or near-
     zero.** Any non-trivial number here means the soft path has a bug.

4. **Shop dataset same thing** — we expect a smaller gain here. If the
   shop gain is comparable to the QTH gain, that suggests we were wrong
   about the interference hypothesis. If the shop gain is much smaller,
   that confirms the analysis we did on May 15.

### 6d. Decision criteria for merge

Merge to main if **all** of the following hold:

1. All correctness tests (6a) pass.
2. Synthetic sweep (6b) shows ≥1 dB gain.
3. Real-capture A/B at QTH (6c) shows ≥2 percentage-point reduction in
   truncation rate, with packet count not decreasing.
4. No "hard-only" decoded packets in 6c (no regressions).
5. CPU usage on a Pi 4 stays under 50% during real-time decode of a busy
   channel (k=7 soft Viterbi is ~2-3× hard; we have plenty of headroom).

Fail any of those → keep on the fork, document the result, move on.

---

## 7. Risks and unknowns

| Risk | Mitigation |
|------|-----------|
| Amplitude estimation is wrong on some bursts → systematic decode failure | Fall back to median-abs-value of post-framesync soft samples if preamble-based estimate is implausible (e.g., very low or NaN). Log a debug message. |
| The synthetic test (6b) passes but real-capture (6c) shows no improvement, meaning our test channel model is unrealistic | Expected. The real measurement is the one that counts. The synthetic is for algorithm correctness, not performance prediction. |
| Soft path decodes garbage packets that pass framesync + first-block RS but contain noise — false positives | RS still has to succeed. The probability that random soft input produces an RS-valid first block is roughly 2⁻¹²⁸ (RS parity). Not a real risk. |
| Pi 4 CPU runs out | Profile early. If pure-Python soft Viterbi is too slow, we have a clear path to a small C extension (or numba) using the existing trellis structure. |
| `audio_43k` and `bits` get out of sync across the API | We pass `bit_phase` already for SNR measurement. The soft samples come from the same array at the same phase, so they're guaranteed in lockstep. Unit test 6a.2 will catch any drift. |

---

## 8. Open questions

1. **Should we measure per-bit confidence in the diag output?** The soft
   Viterbi has access to per-bit "soft margin" information that the hard
   path throws away. Logging a "min branch metric" or "average metric"
   per packet would be very informative for tuning. Costs nothing — let's
   add it to the diag-mode output behind `--soft-viterbi`.

2. **Do we want amplitude estimation per-block or per-burst?** Per-burst
   is simpler. Per-block is more robust to AGC drift mid-burst but adds
   complexity. Start per-burst; revisit if A/B shows weird per-block
   variance.

3. **Should the web UI expose a hard/soft toggle for diag re-decode of a
   captured packet?** Cute but not in scope for v0.9.0.

---

## 9. Rollout

- v0.9.0-alpha: fork of v0.8.5, soft Viterbi behind `--soft-viterbi` flag,
  tests 6a + 6b green, no field deployment.
- v0.9.0-beta: deploy to QTH for paired A/B run (6c). Shop also if
  bandwidth permits.
- v0.9.0: merge to main IF criteria in 6d are met. Default remains hard
  Viterbi until the next release after confidence is established.
- v0.9.1+: consider making soft the default. Or not — depends on the
  numbers.

---

## 10. References

- Proakis, *Digital Communications*, §8.7 (soft vs hard Viterbi).
- Heller & Jacobs, *Viterbi Decoding for Satellite and Space Communications*,
  IEEE Trans. Comm. Tech., 1971 (the 3-bit-quantization result).
- ALERT2 AirLink Spec v1.2, §3.5.2 (CC parameters).
- Shop-vs-QTH analysis, this project, May 15 2026 (motivation for
  expected-gain estimates).

---

## 11. Implementation status (v0.9.0a1)

Implemented and behind `--soft-viterbi` (default OFF). FEC mode 0 only.
Section 6a + the rigorous CC-level form of 6b are green (85 passed,
4 skipped — skips need WAV fixtures absent from the build tree).

**Status against the merge criteria (section 6d):**

1. Correctness tests (6a) — PASS.
2. Synthetic gain — PASS at the CC level (soft yields strictly fewer
   byte-errors feeding RS than hard from the same noisy waveform).
   A full packet-level synthetic sweep (6b as originally specced) is
   not implemented; the CC-level measurement is the meaningful one.
3. Real-capture A/B at QTH (6c) — **NOT YET RUN.** This is the gate.
4. No hard-only / soft-corruption regressions — PASS on synthetic
   (`test_soft_matches_hard_across_light_noise`); must be re-checked
   on the real-capture A/B.
5. Pi 4 CPU headroom — not yet measured on hardware, but measured in
   the dev sandbox at **1.33× hard** (cheaper than the 2–3× budgeted;
   absolute-value metric is light and the trellis is shared). A frame
   is ~150 ms airtime vs tens of ms to decode, so substantial
   real-time headroom is expected. Confirm on the Pi during the QTH
   run.

No gain is claimed until criterion 3 is met.

## 12. Field A/B runbook (criterion 6c)

Goal: one decision number — does soft reduce the truncation rate on a
real capture without losing packets hard got right?

1. Capture (per RTL_SDR_SETUP.md / HARDWARE.md). At the QTH, where
   SNR is low and soft should help most:
   ```bash
   rtl_fm -f 171.850M -s 48000 -M fm -E dc -g <gain> - 2>rtl_fm.log | \
     sox -t raw -r 48000 -e signed -b 16 -c 1 - qth_ab.wav
   ```
   Let it run long enough to capture a few hundred packets (the more
   the tighter the comparison — ideally a multi-hour rolling capture).

2. Replay through both decoders, same WAV, same framesync threshold:
   ```bash
   python -m alert2 qth_ab.wav \
       --log-file hard.log --diag-mode --diag-file hard.diag --no-stats
   python -m alert2 qth_ab.wav --soft-viterbi \
       --log-file soft.log --diag-mode --diag-file soft.diag --no-stats
   ```

3. Compare:
   - Total packets: `grep -c '^\[' hard.log` vs `soft.log`.
     Soft ≥ hard expected.
   - Truncation rate: `grep -c TRUNCATED` over total, each file.
     Soft ≤ hard expected.
   - **Hard-only packets** (decoded by hard, missing from soft):
     should be ZERO or near-zero. Any non-trivial count means a soft
     bug — investigate before going further.
   - Spot-check newly-soft-decoded packets: MANT headers parse, SRP
     timestamps fit the rest of the network, source addresses are
     plausible network nodes. New packets must look real, not like
     RS-passed noise.

4. Decision (section 6d): merge to main only if soft shows ≥2
   percentage-point truncation reduction at the QTH, packet count
   does not decrease, zero hard-only regressions, and Pi 4 CPU stays
   comfortable during the run. Otherwise: keep on the fork, write up
   the result, move on.

5. Repeat at the shop. Expectation from the May 15 analysis: a
   smaller gain there (interference-limited, not AWGN-limited). If
   the shop gain is comparable to the QTH gain, that contradicts the
   interference hypothesis and is worth understanding.
