# Changelog

## v0.9.1rc9 (2026-06-13)

Multi-PDU bundling indicator in the web UI detail panel.

When one AirLink frame carries several stacked MANT PDUs (repeater
concentration — a repeater bundles multiple stations' reports into one
transmission), the detail panel now shows a "Bundled transmission"
note listing the other stations in the same frame. This explains why
the AirLink length (which covers the whole frame) can be much larger
than a single PDU's payload length — the extra bytes are the other
bundled stations, not missing data or FEC overhead.

Verified against real captures: e.g. a 3-PDU frame (airlink=101)
correctly identified as sources [731, 771, 733], matching the manual
arithmetic (16+40+42 ≈ 101 byte payloads).

TRUNCATED label is unchanged and retained — it is orthogonal to
bundling: TRUNCATED means a single PDU was partially recovered
(follow-on RS failed), while bundling means multiple complete PDUs
shared a frame. A frame can be bundled AND contain a truncated PDU.

Changes: types.py (frame_pdu_count / frame_index / frame_sources
fields), decoder.py (stamp these on PDUs sharing a frame in
_try_decode_at), web.py (expose in packet dict), templates/index.html
(bundled note + styling). 150 tests pass (+2 bundling tests).
No change to decode logic or yield.

## v0.9.1rc8 (2026-06-02)

Web UI fix — packet feed froze after running a while.

Symptom: the live packet table stopped updating after ~1-2 hours
(observed across Android and Firefox identically), while uptime/packet
stats and CSV export kept working.

Root cause: a packet with a non-finite float in any field (a sensor
value decoded from corrupted/erasure bytes encoding IEEE NaN/Infinity,
or an SNR edge case) entered the web ring buffer. Python's json.dumps
emits the bare tokens `NaN`/`Infinity`, which are valid Python json
output but INVALID JSON — the browser's JSON.parse rejects the entire
response. Because the offending packet stays in the 500-entry buffer,
every subsequent /api/packets response was poisoned too, until it aged
out. The client's `await resp.json()` threw and was swallowed by an
empty catch, so the feed silently froze. /api/stats and CSV were
unaffected (separate code paths; CSV via Python csv writer tolerates
NaN text).

Fix: `_json_safe()` recursively replaces non-finite floats with null
before serialization, applied at `_send_json()` so every JSON endpoint
is covered. Output is now always valid JSON. The frontend already
renders null snr_db as '—', so no client change needed.

148 tests pass (+3: _json_safe unit + nested + valid-JSON checks).
No decoder changes; web.py only.

## v0.9.1rc7 (2026-06-02)

Pattern F false-positive filter — unparseable sensor readings.

Iterative/SOVA modes were emitting packets with valid source IDs but
garbage sensor data (hex blobs, impossible values like Stage=140
billion) when erasure-RS filled a corrupted block with bytes that
happened to parse as a GSR/MSR report. Field-observed in the tkr_audio
TK790 capture.

Pattern F (decoder.py `_is_likely_false_positive`): for erasure
decodes, reject if any sensor reading has an unrecognized format code
(value_type contains 'fmt_0x', 'truncated', or 'utf8-error'). Caught
8/8 garbage decodes in both iterative and SOVA modes on the field
capture, while keeping every legitimate erasure recovery (confirmed
against independent clean-path decodes).

Only affects erasure decodes (iterative mode is opt-in); zero effect
on default soft-V operation. Baseline regression check unchanged
(64 recv / 53 logical on sdr_capture515_trimmed_QTH.wav). 145 tests
pass (+2).

Known limitation: does not catch garbage where the format code is
valid but the value is out of physical range (e.g. Battery
Voltage=1.5e9 with recognized format). Pattern E catches some via the
RS-ratio; per-sensor range tables were considered and rejected as
risking real sensor-glitch data.

## v0.9.0 (final — production successor to v0.8.5)

Promoted from v0.9.0rc1 unchanged in code. The only difference from
rc1 is this label: the promote-to-default gate is now met.

**What closed the gate (2026-05-17):** the gold-standard paired
same-input A/B. `qth_tonight_6hr_trimmed.wav` (12.7 min, ~7.9 dB
QTH) decoded twice through identical code — `--no-soft-viterbi` vs
`--soft-viterbi`. Result: **+15.8% logical capture (467→541), 74
soft-only recoveries, 0 hard-only regressions, 0 payload mismatches,
McNemar χ²=72.0 (p≪0.05)**. Truncation roughly halved (3.6→1.9%),
RS-correction load nearly halved (643→344).

This resolved the earlier 24h-run ambiguity honestly: that run's
unpaired comparison against a different-day baseline could not detect
the effect (a measurement limitation, flagged at the time, which is
why the claim was correctly withdrawn then). The paired test removes
every confound and proves the short-capture +15% was real, not a
sampling artifact. See AB_RESULTS_2026-05-15.md.

All 6d promote-to-default criteria met: paired same-input gain
(statistically significant, regression-free), zero payload
corruption, 24h field stability (demonstrated separately), CPU
1.33× hard (measured). Default policy unchanged from rc1:

| Feature | Default | Override |
|---------|---------|----------|
| Soft-Viterbi | **ON** | `--no-soft-viterbi` |
| Chunk-overlap (0.6 s) | **ON** | `--overlap-seconds` |
| Soft-framesync | **OFF** (3 negative A/Bs) | `--soft-framesync` |

No code change from rc1. Suite: 101 passed, 4 skipped. Shop site
remains RF-limited (confirmed co-channel interferer — out of decoder
scope). FEC modes 1/2 still deferred (network is mode-0 only).

## v0.9.0rc1 (release candidate — consolidated from the a0–a4 line)

Release candidate consolidating the v0.9.0 development line. This is
the intended production successor to v0.8.5. Labelled `rc1` (not final
v0.9.0) for one honest reason: the merge-behind-flag criteria for
soft-Viterbi are decisively met (7 real captures), but the design
doc's gate for promoting it to the *production default* was a ≥24-hour
field run to put a confidence interval on the measured gain. That run
has not happened yet. `rc1` ships the defaults; the label flips to
final v0.9.0 (a one-line change) once the 24h data confirms.

### What changed since v0.8.5, and why

**1. Soft-decision Viterbi — implemented, DEFAULT ON (FEC mode 0).**
Matched-confidence Viterbi using the demodulator's soft samples
instead of sliced hard bits. Evidence across 7 independent real
captures: +~15% logical capture rate at the QTH (e.g. dense capture
46→53), **zero hard-only regressions, zero payload mismatches** in
every single capture, marginal/truncated bursts converted to clean.
Mechanism: feeds Reed-Solomon a cleaner codeword so RS has correction
margin instead of failing at its ceiling. CPU cost ~1.33× hard
(measured) — comfortable real-time headroom on a Pi 4. Reverts to
hard with `--no-soft-viterbi` (no code change). Default policy is a
single audited constant `_SOFT_VITERBI_DEFAULT` in cli.py.

**2. Chunk-overlap fix — DEFAULT (live streaming).** Pure framing
change, not an algorithm change. Overlap default 0.30→0.60 s,
chunk 1.0→1.5 s, sized from measurement: the largest real ALERT2
burst on the SLO network is ~452 ms (airlink_len 130 B), so the old
300 ms overlap let long boundary-straddling frames decode
twice-truncated instead of once-clean. Measured effect: ~+17%
logical on the hard decoder, streaming path, on a real capture
(46→55) — additive to soft-Viterbi (different failure stage).
Tunable per-site via `--overlap-seconds` / `--chunk-seconds`.
Affects live `--stdin` only; WAV-file decode has no chunk
boundaries.

**3. Soft-decision framesync — implemented, DEFAULT OFF.** Complete,
10 tests, behind `--soft-framesync`. Four-way A/B (hard/hard,
soft-V-only, soft-FS-only, both) run on THREE independent real
captures (QTH dense, two shop): **zero additive logical gain in all
three** — soft-FS recovered no packet that soft-Viterbi didn't
already get, and never regressed anything either. Per its own
design-doc §5d kill criterion, a clean negative is a valid outcome:
the feature is kept dormant (not removed — it is correct, costs
nothing off, and removal would risk the shared framesync plumbing
soft-Viterbi depends on) with a documented re-test trigger if a
future capture ever exhibits framesync-limited (vs Viterbi/RS-
limited) losses. Honest status: **no benefit observed on any real
data tested.**

**4. Web UI UTF-8 fix + package-wide open() audit.** The web UI
showed mojibake (`Â·` for `·`, `â` for `—`) on the deployment Pi
because `_read_template()` and six other text-mode `open()` calls
omitted `encoding=`, defaulting to the Pi's ISO-8859-1 locale. This
was the same bug v0.8.5 needed a manual patch for, silently
reintroduced when v0.9.0 forked from v0.8.5 *source* (the manual
patch lived only on the Pi). ALL text-mode opens now explicitly
UTF-8, locale-independent, with AST regression tests that fail even
on a UTF-8 dev box so it cannot slip through a future fork again.

**5. Carried fixes & test hardening.** The `test_noise_floor_trace_
30min` self-inconsistency (from the original v0.8.5 fork) fixed.
Regression tests added and kept green throughout: chunk-overlap
sizing invariant, template encoding, soft-V correctness +
end-to-end + CC-gain, soft-FS correctness + composition. Suite:
**101 passed, 4 skipped** (skips need WAV fixtures absent from the
tree; pre-existing). No decoder algorithm changes beyond hard→soft
Viterbi (per user preference: no Viterbi/RS-core algorithm changes;
framesync detection is a separate stage).

### Default policy (single source of truth: cli.py)

| Feature | Default | Override | Evidence |
|---------|---------|----------|----------|
| Soft-Viterbi | **ON** | `--no-soft-viterbi` | 7 captures, +15% QTH, 0 regressions |
| Chunk-overlap (0.6 s) | **ON** | `--overlap-seconds` | +17% streaming, regression cleared |
| Soft-framesync | **OFF** | `--soft-framesync` | 3 negative four-way A/Bs |

### Not in scope / known boundaries

- The shop site is RF-limited by a confirmed co-channel interferer
  (periodic ~15 s burst, +35 dB over signal, dead-on 171.850 — see
  AB_RESULTS). No decoder change addresses this; it is an
  RF/operational matter. Documented honestly rather than chased in
  software.
- FEC modes 1/2 still not implemented (network uses mode 0
  exclusively; deferred).
- Final v0.9.0 promotion gated on the 24h field run.

---

## v0.9.0a4 (alpha — UTF-8 template fix + package-wide open() audit)

### Web UI mojibake fix (regression from the v0.9.0 fork)

**Bug:** the web UI showed `Â·` where a `·` separator belonged and
`â€"`/`â` where an em-dash belonged (e.g. `1000 Â· SNR 7.7 dB Â· hd=1`,
Destination field `â`). Root cause: `web.py::_read_template()` called
`open()` with no `encoding=`, so it used the platform default. On the
deployment Pi that default is ISO-8859-1 (system locale), which
misreads the templates' UTF-8 bytes — `C2 B7` (·) → `Â·`,
`E2 80 94` (—) → `â€"`.

**History / why it regressed:** this is the same bug the production
v0.8.5 needed a *manual* `_read_template()` UTF-8 patch for (per the
project primer, slated for an official v0.8.6). The v0.9.0 line was
forked from v0.8.5 *source*, which never carried that manual patch —
it existed only on the Pi's installed copy — so the fork silently
reintroduced it. Now fixed in source, locale-independent, verified
under a simulated `LC_ALL=C` (Latin-1) locale.

### Package-wide open() encoding audit (primer pending item)

Closed the primer's "audit other open() calls for locale-related
encoding issues" item. ALL text-mode `open()` calls in the package
now pass `encoding='utf-8'` explicitly:

- `web.py` `_read_template()` — the template-mojibake fix above.
- `output.py` log writer, CSV writer, JSONL/diag writers, stats
  writer (4 sites) — previously would mangle non-ASCII sensor
  descriptions in output files on the Pi.
- `cli.py` stats-append writer (1 site).

Binary opens (WAV I/O etc.) correctly unaffected.

### Regression tests (3 new, `test_template_encoding.py`)

- Templates decode with no mojibake (asserts the exact `Â·` / `â€"`
  signatures are absent, and proper U+00B7 / U+2014 present).
- Static AST guard: `_read_template`'s `open()` must carry
  `encoding=` (catches regressions even on a UTF-8 dev box, where
  runtime behaviour would otherwise mask the bug — which is exactly
  how it slipped through the fork).
- Package-wide AST audit: no text-mode `open()` anywhere omits
  `encoding=`. Locks the primer item against future regression.

Suite: **101 passed, 4 skipped** (was 98).

No functional decoder changes in a4. Default policy unchanged from a3
(soft-Viterbi + chunk-overlap default-on; soft-framesync flag-off
pending an additive A/B result).

## v0.9.0a3 (alpha — soft-FS implemented; soft-V + chunk-overlap default-on)

### Default policy changes

Two features promoted to default-on, one deliberately NOT:

- **Chunk-overlap fix → default** (was already the a2 default; now
  formally policy). Evidence: ~+17% logical on streaming, regression
  investigated and cleared. Framing change, not algorithm.
- **Soft-decision Viterbi → default-ON.** Evidence: +15% logical,
  zero hard-only regressions, zero payload mismatches, consistent
  across six real captures. `--no-soft-viterbi` reverts to hard with
  no code change. A 24h run is still wanted to put a confidence
  interval on the rate; the revert flag exists precisely so this
  default can be backed out instantly if the long run disagrees.
- **Soft-decision framesync → implemented but default-OFF.** The
  four-way A/B (below) did not show additive gain. Kept behind
  `--soft-framesync` per its own design-doc kill criterion.

Default policy lives in two clearly-commented constants in `cli.py`
(`_SOFT_VITERBI_DEFAULT`, `_SOFT_FRAMESYNC_DEFAULT`) — promoting a
feature is a one-line, audited change. `--no-soft-viterbi` /
`--no-soft-framesync` overrides added.

### Soft-decision framesync — implemented

`sync.py`: `find_framesync_soft` (matched-filter correlation on the
calibrated soft samples instead of Hamming distance on sliced bits),
`find_framesync_soft_with_bitsync_gate`, `soft_score_to_synthetic_hd`
(diag/JSON continuity). Reuses the soft_samples + amplitude_estimate
plumbing from soft-Viterbi; orthogonal to it (the two compose).
Threshold default **0.62**, set from a false-positive characterization
sweep: zero false candidates in the realistic >7 dB regime, ~1/1000
in the pathological noise-as-signal case (and RS parity is the final
gate against phantom packets regardless). Wired through decoder,
decode_wav, CLI (file + stdin), stream.

10 new tests (`test_soft_framesync.py`): clean-detection,
pure-noise-bound, inversion, equivalence floor, weak-corruption
recovery, threshold monotonicity, synthetic-HD mapping, defensive
amplitude, composition with soft-V, no-phantom-from-noise. Suite:
**98 passed, 4 skipped** (was 88).

### Four-way A/B — soft-FS negative result (honest)

sdr_capture515_trimmed_QTH, four variants (HH / SV-only / SF-only /
both):

- SV-only: 46→53 logical (+7), zero regressions — confirms
  soft-Viterbi yet again (6th capture).
- SF-only: 46→46 (**+0**). BOTH == SV-alone (no additivity).
- Zero hard-only regressions, zero payload mismatches throughout.

Soft-FS provided no benefit on this capture: framesync was not the
binding constraint (the recoverable packets' framesyncs were already
found by hard-decision; the loss was at Viterbi/RS, which soft-V
fixes). Per the soft-FS design-doc §5d, a clean negative result is a
valid outcome → soft-FS is NOT defaulted. It remains complete,
correct, tested, and behind its flag for re-test on noisier / shop /
24h captures. See docs/SOFT_FRAMESYNC.md §9 and
AB_RESULTS_2026-05-15.md.

## v0.9.0a2 (alpha — chunk-overlap fix + soft-framesync design)

### Chunk-overlap sizing fix (live streaming)

**Pure framing change, no algorithm change.** The live-stream
decoder buffers audio in overlapping chunks so a burst landing on a
chunk boundary still appears intact in the carried-over tail. The
previous overlap default (0.30 s) was measured against 151 real
packets from QTH/shop captures to be SHORTER than the largest real
ALERT2 burst:

- Max observed frame: airlink_len=130 B ≈ **452 ms** burst.
- Old overlap: 300 ms — shorter than that.

Consequence: a long frame straddling a chunk boundary was truncated
in BOTH the first chunk and the overlap tail — decoded twice-
truncated instead of once-clean, defeating the chunk-overlap re-
decode safety net the user relies on for boundary packets. This was
a silent capture-rate loss specific to live streaming.

Fix: `overlap_seconds` default 0.30 → **0.60** (covers the 452 ms
observed max + ~30% margin), `chunk_seconds` 1.0 → **1.5** (keeps
the decode-overhead ratio similar). Both now tunable via new CLI
flags `--overlap-seconds` and `--chunk-seconds` for sites with
unusually long frames — no code edit needed to retune.

Scope note: this affects **live `--stdin` streaming only**. WAV-file
decode processes the whole file at once (no chunk boundaries), so
the diag-based A/B comparisons are unaffected and will not show this
gain — it shows up in real Pi field operation.

New regression tests (`test_chunk_overlap.py`, 3) pin the invariant
in terms of the overlap/burst-length relationship, so the default
can be retuned upward but cannot silently regress below burst
length. Suite: **88 passed, 4 skipped** (was 85).

### Soft-decision framesync — design phase

`docs/SOFT_FRAMESYNC.md` added: full design for soft-decision
framesync correlation (matched-filter score on the soft samples
instead of Hamming distance on sliced bits). Motivated by the
observation that soft-Viterbi only helps bursts that first pass
framesync; bursts with framesync too corrupted to match are dropped
before the improved decoder sees them. Soft-FS attacks that earlier
failure stage and should be additive to soft-Viterbi.

No soft-framesync code in this release — design only, pending review.
Implementation targets v0.9.0a3 behind a `--soft-framesync` flag,
with the same disciplined four-way A/B (hard/hard, softV-only,
softFS-only, both) and the same kill-criterion discipline as
soft-Viterbi (a clean negative result is an acceptable outcome).

### Field A/B status (soft-Viterbi)

Fifth real capture (`sdr_capture515_trimmed_QTH`, dense, 56/64
receptions, ~7.9 dB) analyzed: **+15% logical capture rate**
(46→53), zero hard-only regressions, zero payload mismatches, one
truncation eliminated. Consistent with the four prior captures.
Soft-Viterbi now decisively clears the merge-behind-flag gate;
24h-run volume still wanted before promoting to default. See
`AB_RESULTS_2026-05-15.md`.

## v0.9.0a1 (alpha — soft-Viterbi implemented)

### Soft-decision Viterbi: implementation

The `--soft-viterbi` flag is now real. Soft-decision Viterbi is
implemented for FEC mode 0, behind the flag, defaulting OFF. Hard
Viterbi remains the production path until field A/B measurement
justifies a switch (design-doc section 6c/6d).

**New code:**

- `fec.py`:
  - `quantize_soft(soft, amplitude_estimate, n_bits=4)` — 4-bit
    signed quantizer. Defensive: zero/negative/NaN amplitude returns
    all-zeros (graceful degradation, not a crash); NaN/inf samples
    become 0; outliers clip at the rail rather than dominating the
    path metric.
  - `estimate_amplitude(soft)` — median-of-absolute-value symbol
    amplitude estimate over the first ≤512 samples. Median (not mean)
    so impulse spikes at the shop don't inflate it.
  - `SoftViterbi(Viterbi)` — subclass; shares the trellis, transition
    table and traceback. Only the branch metric differs: absolute
    difference between received quantized value and expected ±IDEAL
    (per design-doc 3b — linear, not squared-Euclidean, chosen for
    impulse robustness at ~0.2 dB AWGN cost).
  - `decode_first_block_soft()` / `decode_followon_stream_soft()` —
    soft counterparts of the existing decode functions. RS and
    descramble stages are byte-domain and unchanged; only the CC
    stage differs.

- `demod.py`: `demodulate()` info dict now also carries
  `soft_samples` (per-bit floats, index-aligned with the hard
  `bits`) and `amplitude_estimate`. The hard path never reads these;
  return signature is unchanged (optional dict keys).

- `decoder.py`: `Alert2Decoder(soft_viterbi=False)` kwarg;
  `decode_bits(... soft_samples, amplitude_estimate, soft_inverted)`;
  `_try_decode_at(... quantized_soft)`. Quantization happens once per
  stream (it only depends on the per-burst amplitude estimate, not
  framesync position). The inverted-polarity path negates soft values
  to match `[1-b for b in bits]` — verified by
  `test_soft_inversion_symmetry`.

- `types.py`: new `Alert2Packet.viterbi_mode` field ('hard'/'soft')
  so downstream/diag can see which decoder produced each packet.

- `cli.py`, `stream.py`: `--soft-viterbi` flag wired through both
  file mode and live/stdin mode, with a one-line stderr notice when
  active.

**Tests added (24 across 2 files), all passing:**

- `test_soft_viterbi.py` (14): quantizer edge cases, amplitude
  robustness, clean identity (soft == hard == original at zero
  noise), recovery under light noise, inversion symmetry, mode-1/2
  rejection, and `test_soft_viterbi_beats_hard_at_threshold` — soft
  produces strictly fewer bit errors than hard from the SAME noisy
  waveform.
- `test_soft_viterbi_e2e.py` (4, libfec-gated): full-pipeline clean
  equality (soft byte-identical to hard, correct tagging),
  soft/hard agreement across light noise (regression guard — soft
  never corrupts what hard got right), `test_soft_cc_gain_through_
  rs_pipeline` (soft yields fewer byte-errors feeding RS at
  threshold, measured apples-to-apples), and no-phantom-packets from
  pure noise.

Full suite: **85 passed, 4 skipped** (skips need WAV fixtures not in
the build tree; pre-existing).

**Honest note on the test plan:** the first end-to-end attempt at a
"soft recovers a frame hard loses" test failed. Investigation showed
this was a flawed test premise — a hand-rolled FSK modulator too
clean to exhibit the soft/hard gap at the packet level, plus a
payload-comparison bug — NOT a decoder defect (hard and soft track
byte-for-byte on clean signals). It was replaced with rigorous
assertions that measure the CC-level gain where the hard-vs-soft
comparison is genuinely apples-to-apples (same received samples into
both decoders). The real packet-level gain figure still comes from
the field A/B on actual captures (design-doc 6c) — that has not been
run yet and no gain is being claimed until it is.

**Not done / next:**

- Field A/B: run a real captured WAV through `python -m alert2 X.wav`
  and `python -m alert2 X.wav --soft-viterbi`, compare truncation
  rates per design-doc 6c/6d.
- Synthetic SNR sweep (design-doc 6b) as a slow marked test —
  optional, lower priority than real-capture A/B.
- Per-packet soft-margin logging in diag-mode (design-doc open
  question 8.1) — deferred.
- Still alpha. Do not field-deploy as the default decoder.

## v0.9.0a0 (fork — pre-implementation)

### Soft-decision Viterbi: design phase

Forked from v0.8.5 as a working branch for soft-decision Viterbi
exploration. **No algorithmic changes yet** — this release is a
starting point for the work and is not field-deployable.

What's in this fork:

- `docs/SOFT_VITERBI.md` — design doc covering the motivation
  (shop-vs-QTH analysis, May 14–15 2026), branch-metric choice
  (absolute-value over squared-Euclidean for impulse robustness),
  4-bit signed quantization plan, API seams, test plan with
  measurable pass/fail criteria, and the explicit decision to
  default to hard Viterbi until measurements justify otherwise.

What's NOT in this fork:

- Any code change. Decoder behavior is identical to v0.8.5.
- The soft-Viterbi implementation itself. That comes in v0.9.0a1.
- Any field-deploy artifacts. Don't install this on a Pi.

### Test fix (carry-over from v0.8.5)

Fixed `test_noise_floor_trace_30min`, which had a self-inconsistency:
the test name and comment described 30-minute buckets (matching the
production `SNR_BUCKET_SECONDS = 1800`), but the rx_time values placed
packets at t=1500 and t=2700 expecting them to share a bucket — which
only works for 3600-second (hourly) buckets. With actual 30-minute
buckets, those timestamps land in different buckets, giving 3 buckets
total when the test expected 2.

Production code was correct (30-min buckets, as named). Test data has
been rewritten to use timestamps that fall cleanly inside two 30-min
buckets (t = 100/900/1700 in bucket 0; t = 3700 in bucket 3600), and
a new assertion was added on `bucket_start_unix` to catch any future
bucket-size drift.

Result: 59/59 non-libfec tests pass (previously 58/59).

Why a separate alpha version: the soft-Viterbi work is non-trivial
and we want a clean line between hard-Viterbi production (v0.8.x)
and the experimental branch. If A/B measurement against real
captures shows the gain is real, this branch merges as v0.9.0. If
not, we kill it and bump v0.8.6 with the existing pending items.

## v0.8.5

### Bug fix: Web UI character encoding (the "â" glitch)

The web UI was rendering `→` (the path separator arrow) and other
non-ASCII characters as garbled mojibake like `Â,` on browsers. Root
cause: the `/api/packets` JSON endpoint sent
`Content-Type: application/json` without a charset, so browsers
defaulted to Latin-1 and misinterpreted UTF-8-encoded characters.

Fixed by adding `; charset=utf-8` to the JSON and CSV API responses.

The HTML page already had `<meta charset="UTF-8">` and was fine — only
the JSON fetched-after-page-load was affected, which is why the issue
was visible on Linux, Windows, and phones equally.

This is a server-side fix; no client/browser changes needed.

### Tests

72 tests still pass (no regressions).

## v0.8.4

### Bug fix: per-PDU truncation flag

Fixed a misleading TRUNCATED flag on the first PDU of a multi-PDU
AirLink frame. Previously, when follow-on RS failed and the FIRST
PDU's bytes were entirely in the first FEC block (so the PDU was
byte-complete), the decoder still flagged it TRUNCATED because the
AirLink frame had additional content after it.

The flag now correctly indicates only "this PDU's own bytes are
incomplete." From recent diag-mode samples this fix clears ~4-5% of
truncation flags that were misleading.

### New: --udp-emit HOST:PORT flag

Sends each decoded packet as a JSON UDP datagram to the specified
endpoint. Used to hand packets off to the new `alert2_forwarder`
companion program (or any other consumer). Fire-and-forget — the
decoder never blocks on a slow or absent consumer.

JSON format is flat (no nested objects) for easy parsing:

```json
{
  "rx_time_unix": 1747068213.594640,
  "fec_mode": 0,
  "snr_db": 14.6,
  "noise_rms": 0.012,
  "rs_corrections": 0,
  "framesync_hd": 0,
  "truncated": false,
  "src": 769,
  "dest": null,
  "added_sas": [4604, 4601],
  "port": 0,
  "hop_limit": 0,
  "mant_payload_length": 47,
  "raw_payload_hex": "0200002f...",
  "application_payload_hex": "04143d...",
  "airlink_length": 89
}
```

Example usage with the forwarder:

```bash
# Terminal 1: forwarder
python3 -m alert2_forwarder --dest 192.0.2.10:60015

# Terminal 2: decoder with UDP emit
sudo rtl_fm ... | python3 -m alert2 --stdin --log-file alert2.log \
  --udp-emit 127.0.0.1:9999
```

### Bundle: include libfec-dev

The bundle now ships `libfec-dev` along with the other system .deb
packages. The Reed-Solomon decoder uses `libfec.so.0` via ctypes; on
fresh systems it isn't always present and the decoder fails at runtime.
The new bundle includes it so installs don't break on missing libfec.

### Installer: clean up stale pip-installed numpy/scipy

The installer now detects pip-installed numpy/scipy in either user-site
(`~/.local/...`) or system-wide (`/usr/local/...`) locations, and offers
to remove them before installing the apt versions. This fixes the issue
where a broken piwheels scipy left over from a previous install attempt
shadowed the apt scipy and caused import errors.

### Installer: always install Python/decoder deps

Previously, answering "no" to the "rtl-sdr already installed, replace?"
prompt also skipped installing python3-numpy, python3-scipy, etc. Now
the rtl-sdr replacement prompt only affects the rtl-sdr family; Python
and decoder deps are always installed.

### Test count

72 tests total (added 3 tests: UdpEmitOutput, forwarder dict format,
truncation flag fix verification).

## v0.8.3

### Fixed: offline bundle builder (v0.8.2 install attempts on Pi 2)

Three issues identified during real install attempts:

1. **Version detection no longer imports the package.** The previous
   builder did `from alert2 import __version__` which fails if scipy
   isn't already installed (which it isn't, since we're about to install
   it). Now reads the version string directly from `alert2/__init__.py`.

2. **numpy/scipy are now installed via apt** (`python3-numpy`,
   `python3-scipy`) instead of pip wheels from piwheels. The piwheels
   armv7l/Py3.11 scipy 1.17.1 wheel had a packaging bug that left
   `scipy/__config__.py` missing, producing the misleading "you cannot
   import SciPy while being in scipy source directory" error.
   The apt versions are built by Debian maintainers and bring their
   runtime deps automatically.

3. **System libs explicitly included.** numpy/scipy need
   `libopenblas0` and `libgfortran5` at runtime. These are now in the
   bundle's `.deb` set so they install automatically.

The `python_wheels/` directory remains in the bundle (empty) for
backward compatibility with older install scripts, but isn't used
in v0.8.3+.

### New: --diag-mode flag for analyzing marginal/truncated packets

Adds `--diag-mode` and `--diag-file PATH` CLI flags. When enabled, the
decoder writes a parallel diagnostic log (default: `alert2.diag`) with
extra detail useful for understanding which packets decoded cleanly,
which had RS corrections applied, and where truncations actually
occurred byte-for-byte.

Each diagnostic entry includes:
- Timestamp, source, repeater path
- Flags (`TRUNCATED`, `RS_corr=N`, `FS_hd=N`, or `clean`)
- SNR estimate
- Byte counts: AirLink length, MANT header bytes, MANT payload claimed
  length, application payload bytes actually recovered
- Raw hex of the MANT PDU and application payload
- SRP report summary when applicable

Example for a truncated packet:
```
2026-05-11 10:23:45.123 | src=4615 path=[4604,4601] | TRUNCATED,RS_corr=2,FS_hd=2 | SNR=4.67dB |
                          airlink_len=43 mant_hdr=11 mant_payload=32 app_actual=11
                          mant_pdu_hex=0200002012070211fc11f9541397011b0a1100083441
                          app_payload_hex=541397011b0a1100083441
```

vs the corresponding clean copy:
```
2026-05-11 10:23:45.211 | src=4615 path=[4602,4601] | FS_hd=1 | SNR=4.53dB |
                          airlink_len=43 mant_hdr=11 mant_payload=32 app_actual=32
                          mant_pdu_hex=02000020...   (full 43 bytes)
                          app_payload_hex=...        (full 32 bytes)
```

The `app_actual` byte count vs the claimed `mant_payload` tells you
how much of the packet was recoverable.

Diagnostic output is paired well with WAV captures: take the diag file
and the WAV, find the truncated entries, and you can replay specific
problem bursts through a verbose decoder to study them.

Off by default — adds an output handler when enabled.

### Test count

69 tests total (added 1 DiagOutput test).

## v0.8.2

### Web UI: "Hide truncated when full version exists" checkbox

A new checkbox in the filter bar of the main page that hides truncated
packets if a non-truncated decode of the same `(src, srp_ts)` pair also
exists in the visible buffer.

This is useful because the decoder sometimes finds the same burst at two
adjacent bit positions/phases — succeeding at one and producing a
truncated result at the other. Both decode events are valid information,
but the cleaner one is what you want for most monitoring purposes.

- Client-side only — no server-side filtering
- Off by default (preserves the v0.8.1 behavior of showing all packets)
- Per-tab, not persisted across browser sessions

### Safety: cap on tracked sources for SNR history

Added `--max-tracked-sources N` CLI flag (default 500). The web UI's
`_snr_history` dict now has an upper bound on the number of unique
source addresses it tracks. When the cap is reached, the least-recently-
seen source is evicted (LRU eviction).

This is a defensive change. Your network has 53 unique sources observed
at peak; the default cap of 500 gives you ~10× headroom for network
growth. The cap exists as a circuit breaker for the unlikely case of
bit errors creating phantom `src_addr` values that would otherwise grow
the dict indefinitely.

Worst-case memory bounded:
- 500 sources × 200 SNR-history entries × ~200 bytes = ~20 MB
- Default operation with your network: stable around 4-5 MB

A WARNING is logged the first time eviction occurs, so if it ever fires
you'll know to investigate.

### Test count

68 tests total (added 2 LRU eviction tests + 1 noise floor trace test).

### 30-minute noise floor trace in session stats

The decoder now buckets per-packet SNR into 30-minute windows. At
session end, the stats summary prints a new section showing the median
SNR for each bucket. Buckets that fall ≥0.5 dB below the run's median
are flagged with a `★ noise event` marker.

This helps spot intermittent interference: when many sources show a
simultaneous SNR drop in the same time window, that's a signature of
local noise floor rising rather than any single station's signal
dropping. Useful for diagnostic correlation against time-of-day
interference patterns.

Exposed via:
- Session stats output (stderr at end of run)
- `get_summary()` dict (`noise_floor_trace_30min` key)
- Available to be added to the web UI later

### Offline bundle support — install without internet

New `tools/build_offline_bundle.sh` script creates a portable bundle
containing the decoder, its Python dependencies (numpy, scipy), system
packages (rtl-sdr, libusb), and a defensive installer.

The bundle can be copied to a USB drive and used to install on a Pi
with no internet access.

- Architecture-specific (arm64 for Pi 4/5, armv7l for Pi 2/3)
- Includes wheels for both Python 3.9 (Bullseye) and 3.11 (Bookworm)
  if both Python versions are present at build time
- Defensive installer prompts before changing existing rtl-sdr or
  alert2 installations
- Optionally sets up udev rules, plugdev group, kernel driver blacklist
- Self-test against bundled WAV after install
- See `docs/OFFLINE_INSTALL.md` for full usage

## v0.8.1

### Fixed — stacked MANT PDUs in single AirLink frame are now decoded

This release fixes a real bug that explains the "7 sources never decoded"
mystery from the long-run analyses. The AirLink layer can carry multiple
concatenated MANT PDUs in a single transmission frame, and the decoder
was only parsing the FIRST MANT PDU and silently dropping any after it.

**The bug**

When 4601 received packets from low-priority sources (sources scheduled
right after a higher-priority source), it bundled them into a single
AirLink frame for retransmission. The frame structure looked like:

```
[AirLink hdr 2B] [MANT PDU #1: src=771, payload] [MANT PDU #2: src=733, payload]
```

The decoder was reading only the bytes inside the first MANT PDU's
declared payload length, then stopping. The second MANT PDU's bytes
were ignored.

This is why sources `733`, `737`, `757`, and likely others showed 0%
capture rate across two long-run cross-validations even at high SNR —
they were physically on the air, the decoder could see the bytes, but
the decoder was throwing them away.

**The fix**

`_try_decode_at()` now returns a list of packets and iterates through
the AirLink Payload, parsing one MANT PDU at a time until all
`airlink.length` bytes are consumed (or until parse fails on bytes
that aren't valid MANT — typically 0x55 padding).

The truncation flag now applies only to the FIRST PDU when follow-on
RS fails, since later PDUs only exist if the bytes are actually present.

**How this was found**

You uploaded 4601's transmission scheduler logs (`current` and the
`.s` file). These showed 4601 transmitting "1 MANT PDUs" most of the
time but "2 MANT PDUs" or "3 MANT PDUs" for some transmissions. By
correlating the receive timestamps in `SLO-Tassajera_*` with the
transmission events in 4601's log, the never-decoded sources turned
out to ALWAYS be received within ~1 second of a "partner" source —
and the partner was always one of the well-decoded sources. That
was the smoking gun.

Confirmed by inspecting raw decoded bytes from a real WAV: an
AirLink frame containing src=771 as PDU #1 and src=733 as PDU #2,
both with valid MANT headers and parseable payloads.

**Sources expected to decode after this fix**

The previously-invisible bundled sources should now decode at roughly
the same rate as their bundle partners:

| Was 0% | Bundles with | Should now decode at |
|--------|--------------|---------------------|
| 733 | 771 (~65%) | ~65% |
| 737 | 769 (~97%) | ~97% |
| 757 | 797 (~64%) | ~64% |

### Test count

65 tests (added 1 ground-truth test for stacked-PDU decoding).

## v0.8.0

### Added — browser-based monitoring web UI

A new optional web UI provides live monitoring in any browser. Activated
with `--web-ui`. Runs alongside the regular text logging — completely
additive, doesn't change any existing behavior.

```bash
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
  python -m alert2 --stdin --log-file alert2.log --web-ui
```

Then open `http://localhost:8080` in any browser.

**Features:**
- Live-updating packet list (5-second polling)
- Click any packet to see full MANT/RF/sensor breakdown
- Per-source pages with SNR-over-time mini-chart and packet history
- Filter bar: `src=769`, `rs>0`, `hd>1`, `truncated` etc.
- "Download CSV" button to export the current buffer
- Auto-pauses scrolling when you scroll away from the top
- Mobile-responsive layout — works on phones/tablets
- Light theme, clean professional look
- Read-only — no controls that affect the running decoder

**Architecture:**
- Stdlib only (`http.server`) — no Flask, no extra dependencies
- Vanilla HTML/CSS/JS — no frameworks, no build step
- Runs in a background thread, doesn't slow the decoder
- Default binds to `127.0.0.1` only (not exposed to LAN)

**New CLI flags:**
- `--web-ui` enable the UI on default `127.0.0.1:8080`
- `--web-ui-port N` choose a different port (default 8080)
- `--web-ui-bind ADDR` bind to a different address; pass `0.0.0.0`
  to make the UI visible on your LAN
- `--web-ui-buffer N` how many recent packets to keep (default 500)

**Endpoints:**
- `GET /` — main page
- `GET /source/N` — per-source detail page
- `GET /api/packets?since=N` — JSON paginated by sequence number
- `GET /api/stats` — current stats summary
- `GET /api/sources` — per-source aggregates
- `GET /api/source/N` — one source's full data including SNR history
- `GET /export/csv` — download CSV of the current buffer

See `docs/WEB_UI.md` for details.

### Test count

64 tests total (added 22 web UI tests):
- 8 FEC tests
- 11 output tests
- 13 SRP tests
- 7 quality tests
- 3 ground-truth tests
- 22 web UI tests

## v0.7.0

### Changed — stats summary always prints to terminal

Previously, the per-session stats summary only printed if you passed
`--stats`. Without that flag, stats were collected but silently discarded
at session end. This was a regression introduced in v0.5.0 when the
example commands switched to `--stats-file`.

The stats summary now ALWAYS prints to stderr at session end, unless
`--no-stats` is given. Stats collection is essentially free, so there's
no reason to make it opt-in.

### Added — `--stats-in-log` option

Append the stats summary to the `--log-file` at session end. Useful when
running headless / as a service where you can't see the terminal output.

```
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 40 - 2>/dev/null | \
  python -m alert2 --stdin --log-file alert2.log --stats-in-log
```

When you stop the decoder, the log file gets a stats summary appended at
the bottom.

### Added — `[TRUNCATED]` indicator for partial decodes

When the AirLink follow-on RS block fails but the first block decoded
successfully, the decoder now emits the partial packet flagged as
`[TRUNCATED]`. Previously this case silently produced an incomplete
packet that looked normal:

```
Before: [FEC0] src=4615 ... GSR(sensor_10=0)
After:  [FEC0] src=4615 ... [TRUNCATED] SRP[id=5][ts=5015]: GSR(sensor_10=0)
```

Truncated packets are still reported (the data they DO contain is valid)
but you can now distinguish them from clean full decodes.

CSV gets a new `truncated` column (1=truncated, 0=full) and JSON gets a
top-level `truncated` boolean.

### Added — `truncated_packets` count in stats

```
Truncated packets:      1   (follow-on RS failed; partial data only)
```

### Deprecated — `--stats` flag (still works)

Now a no-op since stats are always on. Pass `--no-stats` to suppress.

## v0.6.0

### Fixed — three FEC pipeline bugs that caused truncated/corrupt sensor data

This release fixes a chain of bugs in the AirLink follow-on block decoder
that caused most multi-block packets to be truncated. The cross-validation
report we did earlier was correct that the data was wrong; the cause turned
out to be in the FEC pipeline, not the SRP parser.

**Bug 1 — Off-by-2 in follow-on bytes calculation**

`airlink.length` per spec §3.2 is the AirLink Payload size, which
**excludes** the 2-byte AirLink header. The decoder was treating it as
including the header, causing follow-on data size to be undercounted by 2
bytes. This broke RS decoding for any packet larger than the first block.

**Bug 2 — Follow-on blocks decoded as separate CC streams**

Per spec §3.5.2, all follow-on blocks must be CC-encoded as a **single
stream** with one CC tail at the very end. The decoder was treating each
follow-on block as its own independent CC stream with its own tail, causing
bit-stream misalignment for any packet with more than one follow-on block.

Fixed by adding `decode_followon_stream()` in `fec.py` that handles all
follow-on blocks as a single CC stream, splitting into per-block RS
codewords only after CC decoding.

**Bug 3 — `application_payload` not truncated to `mant.payload_length`**

The decoder kept all bytes after the MANT header, including any padding or
bytes from subsequent stacked MANT PDUs. The SRP parser would either bail
out on garbage or interpret next-PDU bytes as additional reports, producing
phantom `Type0(...)` / `Type6(...)` entries.

Fixed by always truncating to exactly `mant.payload_length` bytes.

### Validation

Re-ran cross-validation against the encoder ground truth log:
**6 of 6 decoded packets now match the encoder byte-exactly**
(previously 0 of 27 matched).

Sensor values that previously appeared to be "systematically offset"
(Battery Voltage 0.75V too high, etc) were actually byte-correct decodes
that were misinterpreted because of the truncation/padding bugs above.

### Added — ground-truth regression test

`tests/test_ground_truth.py` decodes a sample SDR WAV and validates each
packet's `application_payload` byte-exactly against the encoder log. This
will catch regressions in the FEC pipeline going forward.

### Test count

42 tests total (added 3 ground-truth tests):
- 8 FEC tests
- 11 output tests
- 13 SRP tests
- 7 quality tests
- 3 ground-truth tests

## v0.5.0

### Added — SRP PDU ID (App PDU ID) display and gap tracking

The SRP control byte includes a 3-bit "PDU ID" field that source stations
rotate from 0 through 6 (with 7 = "disabled") on each new packet. This is
the same field A2Control labels as "App PDU ID."

This was being parsed in v0.3.0+ but not displayed. Now:

- **Stdout / log file**: shown as `SRP[id=N][ts=T]` after framesync info,
  e.g. `SRP[id=2][ts=15081]: GSR(...)`. PDU ID 7 is omitted (means "disabled")
- **CSV**: new `app_pdu_id` and `app_test_flag` columns
- **JSON**: already included in the `srp` block as `pdu_id`
- **Stats**: when packets from the same source come in out of sequence,
  the StatsCollector estimates how many packets were missed by counting
  gaps in the rotating PDU ID sequence (deduplicated by `(src, ts)` so
  the same packet through multiple repeaters doesn't count as a gap)

Example new stats output:
```
Estimated missed packets (PDU ID gaps): 12 total
   790: 12 missed
```

### Why this matters

PDU ID rotation is the canonical way to detect lost packets without
needing access to the encoder's transmission log. If you see source
720 send `id=3`, `id=4`, then `id=6`, you know packet `id=5` was missed.
Aggregated across a session, this gives a per-source loss-rate estimate
that's independent of any external reference.

## v0.4.0

### Added — Signal quality (SNR) measurement

- New `alert2.quality` module measures per-packet signal-to-noise ratio
  using the FM-quieting effect of the SDR's audio output
- A pre-burst noise window and an in-burst signal window are compared;
  the dB ratio gives a useful relative SNR (not calibrated dBm)
- Each decoded packet now has `snr_db`, `noise_rms`, and `signal_rms` fields
- SNR shown in stdout/log: `SNR=4.0dB` after the framesync HD field
- New CSV columns: `snr_db`, `noise_rms`, `signal_rms`
- New JSON `signal` block: `{"snr_db": 4.0, "noise_rms": 0.044, "signal_rms": 0.028}`
- StatsCollector tracks min/avg/max SNR and per-source averages

### Stats output additions

```
Signal quality (SNR):   min 3.5dB, avg 4.0dB, max 4.1dB (6 pkts)
Avg SNR by source:
     773: 4.1 dB
     719: 4.1 dB
     781: 4.1 dB
     769: 3.9 dB
```

### What this gives you

- **Per-source comparison** — which sites have weak signal at your antenna?
- **Trend monitoring** — is reception getting worse over time?
- **Error correlation** — RS_corr events should correlate with low SNR
- **Antenna tuning** — A/B test antenna positions and see real numbers

### Caveats

- Numbers are NOT calibrated dBm. They're relative-dB based on FM quieting.
- Useful for trend and comparison; not for absolute-dBm reporting.
- Calibrated dBm would require a reference signal source (or `rtl_power`
  taps on the IQ stream pre-discriminator — a future enhancement).

### Tests

- 7 new tests in `tests/test_quality.py`, all passing
- Total: 39 tests across 4 test files

## v0.3.0

### Added — Application-layer (SRP) decoding

- New `alert2.srp` module decodes Self-Reporting Protocol PDUs (Port 0)
- Supports Type 1 (General Sensor Report), Type 2 (Tipping Bucket Rain Gage),
  Types 3/4/5 (Multi-Sensor Reports — English/Metric/IND), Type 7 (Time Series Data)
- All Format/Length combinations from Application Spec Figure 2-6
- FP2 floating-point format (Appendix 3)
- Recommended sensor names from Figure 2-5 (Rain, Stage, Battery, etc.)
- Unknown sensor IDs (administrator-assigned) decoded with raw IDs and full values
- Decoded sensor data appears in stdout, log file, CSV, and JSON Lines output

### Added — MANT layer improvements

- Now correctly handles **Add Path Service** (repeater path tracking)
- Parses **MANT PDU ID** field for End-to-End Reliable Datagram Service
- Accepts MANT version 0, 1, or 2 (per spec, current version is 2)
- New `mant_added_sas` field in CSV showing repeater chain

### Added — output enhancements

- Stdout and log file now show decoded sensor readings inline
- New `srp_summary` column in CSV
- Full nested `srp` object in JSON Lines output
- Repeater path shown as `path=[N1,N2]` in human output

### Added — tests

- 13 new tests in `tests/test_srp.py` covering FP2, all report types, value formats, padding handling, and admin-defined sensor IDs

### Documentation

- New `docs/SRP.md` covering application-layer decoding
- README updated with sensor data examples

## v0.2.0

### Added — output formats

- **`--log-file <path>`** — Append decoded packets to a timestamped log file
- **`--csv-file <path>`** — Append decoded packets to a structured CSV file with full header
- **`--jsonl-file <path>`** — Append decoded packets as JSON Lines (use `-` for stdout)
- **`--stats`** — Print decoder session statistics at end
- **`--stats-file <path>`** — Write statistics summary as JSON
- **`--rotate-daily`** — Automatically start new files at local midnight
- **`--quiet`** — Suppress stdout output (useful with `--jsonl-file -`)
- **`--timestamp-stdout`** — Add timestamp prefix to stdout output

### Added — internal

- New `alert2.output` module with pluggable `PacketOutput` interface
- `StatsCollector` tracks packet counts, source distribution, framesync HD,
  RS corrections, and decode-path attribution
- `MultiOutput` composes multiple handlers; one failure doesn't break others
- 11 new tests in `tests/test_output.py`

### Documentation

- New `docs/OUTPUTS.md` covering all output formats
- README updated with output format quick reference

## v0.1.0

Initial release.

- AirLink Layer FEC mode 0 demodulation and decoding
- MANT Layer header parsing
- WAV file decoding
- Live SDR streaming via stdin
- Pure Python with libfec for Reed-Solomon
- 8 round-trip tests for the FEC pipeline
