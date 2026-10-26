# Soft-Decision Framesync — Design Doc

**Status:** Draft / pre-implementation
**Branch target:** `v0.9.0a3-soft-framesync` (builds on a2 soft-Viterbi)
**Owner:** K6NLC
**Last updated:** 2026-05-16

---

## 1. Motivation

Soft-decision Viterbi (v0.9.0a1) gave a consistent ~15% logical
capture-rate improvement at the QTH with zero regressions across five
real captures. Mechanism: soft Viterbi feeds RS a cleaner codeword,
recovering damaged bursts hard-decision discarded.

But Viterbi only runs on bursts that **first pass framesync detection**.
A burst whose 32-bit framesync is too corrupted to match (within
`max_fs_hd`) is dropped *before* the decoder we just improved ever sees
it. Those packets are invisible to soft-Viterbi — it can't help a burst
it never receives.

Evidence this is a live loss channel, not theoretical:

- In the `sdr_capture515_trimmed_QTH` A/B, the 8 soft-only receptions
  had FS_hd of 1–3. Several were `FS_hd=3` — right at the detection
  edge. Shared (easily-decoded) receptions averaged FS_hd 0.64.
- That tells us framesync HD and decode difficulty are strongly
  correlated, and the packets we're recovering are the ones where
  framesync *barely* matched. Logically there is a population just past
  that edge where framesync did NOT match and the burst was dropped
  silently — never logged, so invisible in current diags.

Hard-decision framesync slices each received soft sample to a hard bit,
then counts bit mismatches (Hamming distance) against the 32-bit
pattern. This throws away exactly the confidence information that let
soft-Viterbi win. A framesync where 4 bits are "wrong" but every one of
those 4 was a near-threshold coin-flip is far more likely a real
framesync than one where 4 bits are confidently wrong — hard-decision
cannot tell those apart. Soft correlation can.

**Expected benefit:** additive to soft-Viterbi, because it attacks a
different, earlier failure stage. Recovers bursts currently lost at
detection, which then also get the soft-Viterbi treatment downstream.
Magnitude unknown until measured — could be modest (a few %) or
significant; the A/B will tell us. No claim until measured.

**This is a real algorithm change** (unlike chunk-overlap, which was
pure framing). It is in scope because the user specifically identified
it as a likely payoff and approved pursuing it. It is NOT a Viterbi
algorithm change — the user's "no decoder algorithm changes" preference
was about the Viterbi/RS core; framesync detection is a separate stage
and improving it does not touch the decoder math.

---

## 2. Non-goals

- No change to hard framesync as the default. Soft framesync ships
  behind a flag (`--soft-framesync`), like soft-Viterbi did.
- No change to the bitsync gate logic beyond optionally feeding it the
  soft-correlation candidate list.
- No joint soft framesync + soft Viterbi "super-decoder." They are
  independent flags that compose. Keep them orthogonal so the A/B can
  attribute gains correctly (hard/hard, soft-V/hard-FS, hard-V/soft-FS,
  soft-V/soft-FS).
- No change to the framesync patterns or spec interpretation.
- No FEC mode 1/2 work.

---

## 3. Approach

### 3a. The metric

Hard-decision today:
```
hd = popcount(sliced_bits_window XOR FRAMESYNC_PATTERN)
match if hd <= max_fs_hd
```

Soft-decision: treat the 32 received soft samples in the framesync
window as a vector, and correlate against the bipolar framesync
pattern. For framesync pattern bit `p_k ∈ {0,1}` mapped to `±1`, and
received soft sample `r_k` (same calibrated soft values soft-Viterbi
already uses, before quantization):

```
score = Σ_k  r_k * (2*p_k - 1)
```

This is a matched filter for the framesync sequence. A clean framesync
maximizes it; noise averages toward zero; an inverted framesync
minimizes it (useful — see polarity note). Normalize by the per-burst
amplitude estimate (already computed for soft-Viterbi) so the
threshold is SNR-independent:

```
norm_score = score / (amplitude_estimate * FRAMESYNC_BITS)
```

`norm_score` ranges roughly [-1, +1]; +1 is a perfect noiseless match.
A real framesync at the detection edge that hard-decision would reject
(HD 4-5) will still show a clearly positive `norm_score` if the wrong
bits were low-confidence — which is precisely the population we want.

### 3b. Threshold

Hard `max_fs_hd` becomes a soft `min_norm_score`. Mapping intuition:
HD=h on 32 bits, if the h wrong bits were maximally confident, costs
`2h/32` off a perfect score of 1.0. So a hard `max_fs_hd=5` is roughly
`norm_score >= 1 - 2*5/32 ≈ 0.69` in the worst case, but a soft match
will accept HD>5 cases when the wrong bits were weak. Default threshold
will be **calibrated empirically on real captures**, not picked from
this back-of-envelope. Start near 0.55 and sweep.

### 3c. Where it plugs in

`sync.py` gets a parallel function:

```python
def find_framesync_soft(soft_window_iter, amplitude_estimate,
                         min_norm_score, modes=None)
    -> List[Tuple[int, int, float]]   # (pos, fec_mode, norm_score)
```

It needs the soft samples index-aligned with bits — which `demod.py`
already exposes (`info['soft_samples']`) for soft-Viterbi. Same array,
no new demod work. The decoder, when `soft_framesync=True`, calls
`find_framesync_soft` instead of `find_framesync`, and converts
`norm_score` back to an approximate integer `fs_hd` for logging/diag
continuity (so existing log parsing and the A/B tooling keep working).

The bitsync gate (`find_framesync_with_bitsync_gate`) can optionally
gate the soft candidates the same way it gates hard ones — bitsync
itself stays hard for now (it's 48 bits and robust; lower priority).

### 3d. Polarity

The decoder already tries both bit polarities (normal and inverted).
Soft correlation handles this naturally: a strongly *negative*
`norm_score` is an inverted framesync. We can detect both in one pass
(`abs(norm_score) >= threshold`, sign = polarity) instead of two
separate slicing passes — a small efficiency win and conceptually
cleaner. Implementation will keep the explicit two-polarity path
initially for minimal diff / easy A/B attribution, and note the
single-pass optimization as a follow-up.

---

## 4. Risks

| Risk | Mitigation |
|------|-----------|
| Soft framesync admits more false candidates → wasted Viterbi work or false packets | RS parity is still the final gate; a false framesync that passes RS is ~2⁻¹²⁸. Extra Viterbi attempts cost CPU, not correctness. Measure CPU; the bitsync gate also prunes. |
| Threshold too loose → CPU blowup from many candidates per stream | Calibrate threshold on real captures; cap candidates per window; keep bitsync gate available. Profile on Pi. |
| Threshold too tight → no gain over hard | The whole point of the A/B is to find a threshold with gain and no regression. If none exists, the feature is killed — same kill criterion discipline as soft-Viterbi. |
| Amplitude estimate wrong on a burst → bad normalization → missed framesync | Same estimator soft-Viterbi already field-validated across 5 captures. Reuse it; don't reinvent. |
| Interaction with chunk-overlap re-decode produces duplicate-but-different fs_hd entries | Dedup is on decoded payload, not fs_hd. Already handled; soft-Viterbi exercised the same path. |
| Hard-only regression (a burst hard-FS found that soft-FS misses) | Explicit A/B check, exactly as soft-Viterbi: zero hard-only is a merge gate. |

---

## 5. Test plan

### 5a. Correctness (unit, no libfec needed)

- Clean framesync at high SNR: `norm_score ≈ 1.0`; detected at the
  exact same position as hard-decision.
- Pure noise: `norm_score` stays near 0; no detection at sane
  threshold.
- Inverted framesync: `norm_score ≈ -1.0`; detected as inverted.
- Equivalence floor: every framesync hard-decision finds at a given
  HD, soft-decision must also find (soft is strictly more permissive
  when the wrong bits are weak; it must never *miss* what hard catches
  at equal-or-better confidence). This is the unit-level analogue of
  the "zero hard-only" field gate.
- Threshold monotonicity: lowering `min_norm_score` only ever adds
  candidates, never removes.

### 5b. End-to-end (libfec-gated)

- Synthesize a frame whose framesync has N bits corrupted with
  *low-confidence* errors (near-threshold soft values). Hard-decision
  with a tight `max_fs_hd` misses it; soft-framesync recovers it; the
  recovered packet's payload is byte-correct.
- No phantom packets from pure noise with soft framesync on.
- Compose with soft-Viterbi: soft-FS + soft-V together must not
  regress vs soft-V alone (no hard-only, no payload corruption).
- **Chunk-overlap interaction (streamed, NOT file mode):** replay a
  packet-rich capture as raw PCM through the `--stdin` streaming
  path (not `decode_wav`, which has no chunk boundaries) with
  soft-FS on. Assert: (a) every packet decoded in file mode is also
  decoded via streaming (no boundary loss), (b) no duplicate-with-
  different-payload entries (dedup is payload-based and must stay
  robust under soft-FS), (c) packet count via streaming ≥ file mode.
  A file-mode test CANNOT substitute here — it structurally does not
  execute the overlap code path.

### 5c. Real-capture A/B (the decision gate)

Four-way replay of the same WAV (orthogonal flags):

```
python -m alert2 X.wav                                  # H/H baseline
python -m alert2 X.wav --soft-viterbi                   # softV only
python -m alert2 X.wav --soft-framesync                 # softFS only
python -m alert2 X.wav --soft-viterbi --soft-framesync  # both
```

Compare per the AB_RESULTS methodology (key by src+srp_ts+path):
total receptions, unique logical packets, soft-only, **hard-only
(regression — must be ~0)**, payload mismatches (must be 0), CPU.

Attribution question the four-way answers: does soft-FS add packets
*on top of* soft-V, or do they overlap (both just recovering the same
marginal bursts by different mechanisms)? Additive → strong case for
both. Overlapping → soft-FS may not be worth the complexity; report
honestly and possibly kill it.

### 5d. Merge criteria (mirrors soft-Viterbi 6d)

1. 5a + 5b green.
2. Real-capture A/B shows soft-FS recovers logical packets soft-V
   alone does not (additivity), with zero hard-only regressions and
   zero payload mismatches.
3. CPU on Pi 4 stays comfortable with both flags on.
4. If no threshold gives additive gain without regression → do not
   merge; document the negative result in AB_RESULTS and move on.
   (A clean negative result is a valid, valuable outcome.)

---

## 6. Rollout

- a3-alpha: `--soft-framesync` flag, default off, 5a+5b green, no
  field deploy.
- a3-beta: four-way A/B on real captures incl. a long (≥ several hr)
  run. Both QTH and shop.
- Merge to a3 only if criteria in 5d met. Default stays hard
  framesync regardless until long-run confidence, same conservative
  posture as soft-Viterbi.

---

## 7. Open questions — RESOLVED (2026-05-16)

1. **Single-pass dual-polarity vs two passes** — RESOLVED: keep
   explicit two-pass for a3. Rationale: minimal diff, and clean
   four-way A/B attribution (each polarity path independently
   traceable). Single-pass `sign(norm_score)` is a proven-it-first
   follow-up, not a3 scope. If soft-FS shows no gain we unwind one
   change, not two coupled ones.

2. **Soft bitsync too** — RESOLVED: conditional, not in a3. Pursue
   ONLY if (a) soft-FS proves additive AND (b) the four-way A/B
   shows residual losses specifically attributable to bitsync-gate
   rejection. Bitsync is 48 bits and robust; unlikely the
   bottleneck. Let data decide; do not assume.

3. **Diag format / one-rain-server compatibility** — RESOLVED by
   inspection of the forwarder v1.0.0 source:
   - The rain-server path is the decoder's **UDP JSON →
     alert2_forwarder**. `forwarder.py` does `json.loads`;
     `format.py::packet_to_campbell_ascii` reads ONLY:
     `rx_time_unix, fec_mode, snr_db, src, added_sas,
     mant_payload_length, raw_payload_hex,
     application_payload_hex` (all via `.get()` with defaults).
   - The forwarder **never reads** `FS_hd` / `framesync_hd` /
     `RS_corr` in any form.
   - Therefore: the diag/log TEXT format (`FS_hd=N`) is
     analysis-facing only and can be safely enriched to
     `FS_hd≈N (soft_score=0.xx)` with zero rain-server risk. The
     JSON contract is untouched — soft-FS adds/renames none of the
     8 consumed fields. The existing `framesync_hd` JSON field
     keeps populating via the synthetic integer mapping so any
     other JSON consumer stays stable.
   - **Decision:** log both in diag text (rich score + synthetic
     HD), preserve JSON field names/types exactly. No compatibility
     shim needed.

4. **Soft-FS × chunk-overlap interaction** — RESOLVED (scope of
   verification clarified):
   - Payload integrity and no-hard-only-regression for soft-FS CAN
     be verified on the packet-rich `sdr_capture515_trimmed_QTH.wav`
     in file mode — valid correctness check.
   - The chunk-overlap dedup interaction CANNOT be verified in file
     mode: WAV-file decode processes the whole file at once, no
     chunk boundaries — the overlap path never executes. It only
     runs in live `--stdin` streaming.
   - **Empirical baseline measured (2026-05-16, before soft-FS
     exists), hard decoder, sdr_capture515_trimmed_QTH:**
     file-mode 46 logical; stream old-0.3s-overlap 47 logical;
     stream new-0.6s-overlap **55 logical**. The chunk-overlap fix
     alone recovers ~12 logical packets (~+17%) on the hard
     decoder, streaming — independent of and additive to
     soft-Viterbi's ~+15% (different failure stage). Strong
     validation of item (1).
   - 4 "old-overlap-only" packets investigated at payload level:
     NOT a regression. They are distinct SRP-timestamp
     transmissions from stations (720,731,733,773) that the new
     run captures at *other* timestamps near the file edges; the
     differing chunk geometry shifts the capture window relative
     to the sensor transmit schedule. On a continuous live stream
     there are no file edges and the effect vanishes. Verified by
     payload+ts inspection, not assumed.

---

## 9. Four-way A/B result — 2026-05-16 (sdr_capture515_trimmed_QTH)

**Outcome: NEGATIVE for soft-FS. Per §5d, soft-FS is NOT promoted to
default. Implemented, tested, flag-default-OFF.**

| Variant | Receptions | Logical |
|---------|-----------|---------|
| Hard / Hard | 112 | 46 |
| Soft-Viterbi only | 128 | 53 (+7) |
| Soft-framesync only | 112 | **46 (+0)** |
| Both | 128 | 53 (+7) |

- SF-only == hard baseline exactly. SF contributed **zero** new
  logical packets.
- BOTH == SV-alone exactly (no packet BOTH recovered that SV-alone
  did not). **No additivity.**
- Zero hard-only regressions, zero payload mismatches (the
  correctness gates pass — SF is not harmful, just not helpful here).

**Interpretation (honest):** on this capture, framesync was not the
binding constraint. Soft-Viterbi's 7 recovered packets had framesync
that hard-decision *already found* (FS_hd 1–3, within max_fs_hd); the
loss was at the Viterbi/RS stage, which soft-V fixes. The population
soft-FS targets — bursts lost *at framesync detection* — was
effectively empty in this recording. This may differ on noisier
captures or the interference-limited shop site; the four-way harness
and the implementation remain ready to re-test. But on the evidence
in hand, soft-FS does not earn promotion.

This is exactly the §5d "clean negative result is a valid, valuable
outcome" path. Soft-FS stays behind `--soft-framesync` (default off)
until a capture demonstrates additive gain. No code is removed — the
feature is complete and correct, just not on by default.

**Re-test trigger:** run the four-way again on (a) a noisier/lower-SNR
capture, or (b) any shop capture, or (c) a long 24h run. If SF shows
additive logical gain with zero hard-only there, revisit
`_SOFT_FRAMESYNC_DEFAULT` in cli.py.
