# Protocol Notes

Implementation notes and lessons learned while building this decoder.

## Spec sources

- **AirLink Layer Specification v1.2** (January 2025) — defines the
  physical layer: FSK modulation, RRC pulse shaping, FEC, scrambling,
  framing.
- **MANT Specification v1.2** (January 2025) — defines the network
  and transport layer: addressing, routing, ACKs.
- **Application Layer Specification v1.3** (November 2019) — defines the
  payload formats: SRP (Self-Reporting Protocol) for sensor data, and
  the Concentration Protocol for legacy ALERT messages.

## Frame structure

A complete ALERT2 transmission consists of:

```
[Preamble: AGC tone (~30ms)] [Bitsync (48 bits)] [Framesync (32 bits)]
[First Block (CC encoded)] [Follow-on blocks (CC encoded)]
```

For FEC mode 0 with a 26-byte AirLink PDU:
- AGC tone: ~30 ms (varies)
- Bitsync `0xEB90B433AAAA`: 48 bits = 10 ms
- Framesync M0 `0x352EF853`: 32 bits = 6.7 ms
- First block: 24 data bytes + 16 RS parity = 40 bytes → 656 conv-coded bits = 137 ms
- Follow-on block (for the 2 bytes beyond first 24): 17 bytes RS-coded → ~70 ms
- Total burst: ~250 ms

## Bit ordering

**Critical detail:** all data is transmitted MSB-first within each byte, and
the most significant byte first. The bitsync starts with byte 0xEB whose top
bit (`1`) is sent first.

## RS encoding parameters

The spec says "CCSDS Blue Book §3.2" parameters. For Phil Karn's libfec
this means `init_rs_char(8, 0x187, 112, 11, 16, pad)` for FEC mode 0 and
`init_rs_char(8, 0x187, 112, 11, 20, pad)` for modes 1 and 2 (same field,
same first root and primitive element, 20 parity symbols instead of 16).
The 20-root parameters were confirmed on a real mode-2 recording: three of
four frames decoded with zero RS corrections.

Note the `prim=11` parameter — this is the primitive root of the field. Some
implementations get this wrong (using `prim=1`) and produce a valid but
incompatible RS code.

## Scrambler

A 17-bit MLS with polynomial x^17 + x^3 + 1, preloaded to 0x00001.

The scrambler is **recursive** (self-synchronizing) on the transmit side:
```
out = in XOR sr[17] XOR sr[3]
sr[1..17] shifts up; new sr[1] = out
```

The descrambler is **non-recursive**:
```
out = in XOR sr[17] XOR sr[3]
sr[1..17] shifts up; new sr[1] = in   (input, not output!)
```

The scrambler is reset to its preload state at the start of each block
(not each packet). For a single-block packet this distinction doesn't matter,
but for multi-block packets it does.

**Scrambling boundaries:** the first block is `[AirLink Header (2 bytes)] +
[First MANT data (22 bytes)]` = 24 bytes total. The entire 24 bytes are
scrambled together.

## Convolutional encoding

For FEC mode 0: k=7 rate-1/2, polynomials `0x6D` and `0x4F`.

For FEC modes 1 and 2: k=9 (256-state trellis), polynomials `0x1AF` and
`0x11D`, used in the same left-shifting-register convention as mode 0 (the
spec's figure draws them reversed; the table values are what the encoder
takes). The rate-1/2 output is then punctured, pattern read column-down
then row-across from spec Fig. 3-7:

| mode | matrix (rows A, B) | effect | rate |
|---|---|---|---|
| 1 | `[1 0] / [1 1]` | drop A of every 2nd step = 3rd bit of every 4 | 2/3 |
| 2 | `[1 0 1] / [1 1 0]` | drop A of 2nd step and B of 3rd = 3rd and 6th of every 6 | 3/4 |

The puncture phase runs continuously from the first code bit of a block
through the tail; partial groups at the end are kept bit-for-bit, not
rounded. Decoding re-inserts a 0 ("erasure") at every punctured position
and runs the ordinary soft Viterbi: with the |r - expected| branch metric
an erasure costs both branches the same, so it is neutral. Hard-decision
input is mapped to +/-ideal and decoded the same way (a hard trellis has
no erasure metric).

Per-block on-air lengths that follow (first block, bits incl. tail, then
padded to a byte): mode 0 652 -> 656 (82 B), mode 1 780 -> 784 (98 B),
mode 2 800 (100 B). These match spec Fig. 3-8. For follow-on streams the
spec table is a nominal `bytes + ceil(bytes/n) + 2` formula and can read
one byte longer than the exact punctured count (mode 1, 21 RS bytes: 264
bits = 33 B vs the table's 34 B); the difference is trailing pad after the
tail, which the decoder never reads. Only a real odd-length mode-1 frame
can settle whether encoders pad there, and none has been captured yet.

Spec note: "The only variation from the exact NASA specification is that the
second output bit must not be inverted." This means standard NASA code that
inverts the G2 output (common in CCSDS) is wrong here.

The tail occupies 2 bytes of the *output*: k-1 zero input bits (6 in mode
0, 8 in modes 1/2) are run through the encoder and the result is padded to
a byte boundary. In mode 0 that is 12 code bits + 4 pad.

## What can go wrong

Common bugs we encountered while building this:

1. **Wrong RS prim parameter** (`prim=1` vs `prim=11`) — produces a valid
   but incompatible RS code.
2. **Treating speaker audio as baseband** — the audio path filtering of a
   normal voice radio destroys the FSK signal, particularly the AGC tone.
   Use SDR or a discriminator output.
3. **Double FM-discrimination** — if the input is already baseband (from
   rtl_fm or a discriminator output), running it through Hilbert-transform
   FM discrimination AGAIN destroys the signal.
4. **Treating "noisy" portions as signal** — `rtl_fm` outputs noise at full
   amplitude when no signal is present, and the FM discriminator goes "quiet"
   when a signal appears. Bursts are the QUIET parts.
5. **Failing the whole frame on follow-on RS failure** — if the first block
   decodes successfully but the follow-on doesn't, you can still emit the
   first block's data (which contains the AirLink header and first MANT bytes).

## What's not yet implemented

- **Application layer SRP decoding:** the raw payload bytes can be parsed
  as Self-Reporting Protocol messages with sensor readings, but we don't
  do that yet.
- **Soft-decision Viterbi:** would gain ~2 dB of coding gain on marginal
  signals, but for strong signals isn't needed.
- **EERDS (encrypted) packets:** the spec includes optional encryption that
  requires keys we don't have.
