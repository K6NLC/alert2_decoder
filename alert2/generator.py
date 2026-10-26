# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 Packet Generator — transmit-side counterpart to the decoder.

Builds spec-compliant ALERT2 AirLink frames (MANT PDU -> blocking ->
scramble -> RS -> CC -> bitsync/framesync -> RRC-shaped FSK audio) and
transmits them via a sound card with PTT keying (serial RTS/DTR or GPIO),
or writes them to a WAV file.

Shares constants and FEC code with the decoder (alert2.constants,
alert2.fec) so generator and decoder cannot drift apart. A frame built
here decodes byte-exact through alert2.decoder — see
tests/test_generator_roundtrip.py.

Capabilities
------------
- Self-Reporting Protocol payloads: General Sensor Reports with
  assignable sensor IDs and values (uint/int/FP2), optional timestamp.
- IND API v2 queries over MANT Config & Control (e.g. Get parameter
  0x52 "TDMA Bytes Remaining") addressed to a target site.
- Raw MANT payload injection (hex) for anything else.
- Preamble: carrier-only, AGC tone (2000-2500 Hz), configurable durations.
- Amplitude control incl. half-amplitude mode; standalone test tones.
- PTT via serial RTS/DTR (pyserial) or GPIO (Raspberry Pi), with warm-up
  lead time before audio.

FEC mode 0 only (matches the decoder; modes 1/2 need puncturing).
"""

from __future__ import annotations

import math
import time
import wave
import struct
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

from .constants import (
    BAUD_RATE, RX_SAMPLE_RATE, SAMPLES_PER_BIT, RRC_TAPS,
    AGC_TONE_FREQ, BITSYNC_HEX, BITSYNC_BITS, FRAMESYNCS, FRAMESYNC_BITS,
    FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY,
    AIRLINK_HEADER_BYTES,
)
from .fec import scramble_block, rs_encode, conv_encode, bytes_to_bits

logger = logging.getLogger(__name__)

TX_SAMPLE_RATE = RX_SAMPLE_RATE      # 43200 Hz, 9 samples/bit — the rate
                                     # audio is SYNTHESISED at (integer
                                     # samples per symbol makes shaping exact)

# Rate to WRITE and PLAY at by default. Synthesis happens at 43200 because
# that divides evenly by the 4800 baud rate, but almost no soundcard runs at
# 43200: playing a 43200 Hz file on a 48000 Hz device without resampling
# stretches every symbol by 1.111x, so bits slip progressively. The preamble
# then matches for ~15 bits and diverges, which looks like a strong signal
# (preamble correlation 0.83) that will not decode (framesync HD 4-7 instead
# of 0-1). That cost a field debugging session; hence 48000 as the default
# for anything leaving the process.
DEFAULT_OUTPUT_RATE = 48000

# Rates a soundcard is likely to accept natively.
COMMON_OUTPUT_RATES = (44100, 48000, 96000, 192000)


def resample_to(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Linear-interpolate audio between sample rates.

    Adequate here because the RRC shaping is already band-limited well below
    Nyquist at both rates; the decoder's own front end resamples the same way.
    """
    if from_rate == to_rate:
        return audio
    n_out = int(round(len(audio) * to_rate / float(from_rate)))
    if n_out <= 1 or len(audio) < 2:
        return audio
    src = np.arange(len(audio), dtype=np.float64)
    dst = np.linspace(0.0, len(audio) - 1, n_out)
    return np.interp(dst, src, audio)
PAD_BYTE = 0x55                       # spec §3.3 first-block pad


# =====================================================================
# Application layer (Self-Reporting Protocol) builders
# =====================================================================

def encode_fp2(value: float) -> bytes:
    """Encode a float as Campbell FP2 (2-byte). Inverse of srp.decode_fp2.

    FP2: sign(1) | exponent(2, base-10 negative decimal places) |
    mantissa(13). Range ±7999 with 0-3 decimal places.
    """
    if value != value:  # NaN
        return b"\x9f\xff"
    sign = 1 if value < 0 else 0
    mag = abs(value)
    # choose the exponent (decimal places 0-3) that fits mantissa <= 8191
    for exp in (3, 2, 1, 0):
        mant = round(mag * (10 ** exp))
        if mant <= 8191:
            break
    else:
        raise ValueError(f"FP2 overflow: {value}")
    word = (sign << 15) | (exp << 13) | (mant & 0x1FFF)
    return word.to_bytes(2, "big")


def encode_value(value: Union[int, float], fmt: Optional[int] = None,
                 length: Optional[int] = None) -> Tuple[int, bytes]:
    """Encode a sensor value. Returns (format_code, value_bytes).

    Default: ints -> uint (fmt 1) or int (fmt 2) in the fewest bytes;
    floats -> FP2 (fmt 3, len 2). Explicit fmt/length override.
    """
    if fmt is not None:
        if fmt == 1:
            n = length or max(1, (int(value).bit_length() + 7) // 8)
            return 1, int(value).to_bytes(n, "big", signed=False)
        if fmt == 2:
            n = length or max(1, ((int(value).bit_length() + 8) // 8))
            return 2, int(value).to_bytes(n, "big", signed=True)
        if fmt == 3:
            if length in (None, 2):
                return 3, encode_fp2(float(value))
            if length == 4:
                return 3, struct.pack(">f", float(value))
            if length == 8:
                return 3, struct.pack(">d", float(value))
        raise ValueError(f"unsupported fmt={fmt} length={length}")
    if isinstance(value, float) and not value.is_integer():
        return 3, encode_fp2(value)
    iv = int(value)
    if iv < 0:
        n = max(1, (iv.bit_length() + 8) // 8)
        return 2, iv.to_bytes(n, "big", signed=True)
    n = max(1, (iv.bit_length() + 7) // 8)
    return 1, iv.to_bytes(n, "big", signed=False)


def _report_length_field(n: int) -> bytes:
    """Extensible report length per SRP spec §2.1.4 (mirrors parse_length)."""
    if n < 0x80:
        return bytes([n])
    if n < 0x8000:
        return bytes([0x80 | (n >> 8), n & 0xFF])
    raise ValueError("report too long")


def build_gsr(readings: Sequence[Tuple[int, Union[int, float]]],
              fmts: Optional[Sequence[Optional[int]]] = None) -> bytes:
    """Build a Type 1 General Sensor Report (T + L + readings).

    readings: [(sensor_id, value), ...]  sensor_id 0-254 (255 reserved
    for embedded timestamps).
    """
    body = b""
    for i, (sid, val) in enumerate(readings):
        if not 0 <= sid <= 255:
            raise ValueError(f"sensor id {sid} out of range")
        fmt = fmts[i] if fmts else None
        f, vb = encode_value(val, fmt)
        if len(vb) > 15:
            raise ValueError("value too long for 4-bit length nibble")
        body += bytes([sid, (f << 4) | len(vb)]) + vb
    return bytes([0x01]) + _report_length_field(len(body)) + body


def build_srp_payload(reports: Sequence[bytes],
                      timestamp: Optional[int] = None,
                      test: bool = False,
                      pdu_id: int = 7) -> bytes:
    """Assemble an SRP application payload: control byte [+ TS] + reports.

    Control byte (mirrors srp.parse_srp_pdu): version(2)=0 | TS flag(1)
    | test(1) | pdu_id(3) | extensibility(1)=0.
    timestamp: seconds since most recent 12:00 UTC (0-65535).
    """
    cb = 0
    if timestamp is not None:
        cb |= 1 << 2
    if test:
        cb |= 1 << 3
    cb |= (pdu_id & 0x7) << 4
    out = bytes([cb])
    if timestamp is not None:
        out += int(timestamp).to_bytes(2, "big")
    for r in reports:
        out += r
    return out


# =====================================================================
# IND API v2 query builder (MANT Config & Control)
# =====================================================================

API_GET_PARAMETER = 0x0B
PARAM_TDMA_BYTES_REMAINING = 0x52

# MANT spec: "IND Configuration & Control Protocol (0x80)". The MANT
# header Port field is 4 bits and carries the LOW nibble of the port
# parameter; deployed C&C traffic is seen with port nibble 0x8. Kept
# configurable because 1.x firmware behavior varies.
MANT_PORT_SRP = 0x0
MANT_PORT_CC = 0x8


def _ext(value: int) -> bytes:
    """IND API extensible value (1 or 2 bytes)."""
    if value > 0x7F:
        return bytes([(value >> 8) | 0x80, value & 0xFF])
    return bytes([value & 0xFF])


def build_ind_get(params: Sequence[int] = (PARAM_TDMA_BYTES_REMAINING,)
                  ) -> bytes:
    """Build an IND API Get-parameter TLV payload (e.g. b'\\x0b\\x01\\x52').

    Sent as the MANT payload on the Config & Control port, addressed to
    the target site. Per MANT spec §3.4 the payload uses the API binary
    TLV encoding without the SOH byte.
    """
    body = b"".join(_ext(p) for p in params)
    return _ext(API_GET_PARAMETER) + _ext(len(body)) + body


# =====================================================================
# MANT PDU builder (mirror of types.MantHeader.parse)
# =====================================================================

def build_mant_pdu(payload: bytes,
                   source_addr: int,
                   dest_addr: Optional[int] = None,
                   port: int = MANT_PORT_SRP,
                   hop_limit: int = 3,
                   version: int = 2,
                   proto_id: int = 0,
                   tssr: bool = False,
                   add_path: bool = False) -> bytes:
    """Build a MANT PDU (header + payload) the decoder parses back exactly."""
    if not 0 <= source_addr <= 0xFFFF:
        raise ValueError("source_addr out of range")
    if len(payload) > 0xFFF:
        raise ValueError("MANT payload exceeds 4095 bytes")
    da_present = dest_addr is not None
    word = 0
    word |= (version & 0x3) << 46
    word |= (proto_id & 0x7) << 43
    word |= (1 if tssr else 0) << 42
    word |= (1 if add_path else 0) << 41
    word |= (1 if da_present else 0) << 40
    word |= (port & 0xF) << 36
    # encrypted=0, reserved=0, ack=0, added_header=0
    word |= (hop_limit & 0x7) << 28
    word |= (len(payload) & 0xFFF) << 16
    word |= source_addr & 0xFFFF
    hdr = word.to_bytes(6, "big")
    if da_present:
        hdr += int(dest_addr).to_bytes(2, "big")
    if add_path:
        hdr += b"\x00"  # Number of Added Source Addresses = 0 at origin
    return hdr + payload


# =====================================================================
# AirLink frame builder (multi-block, FEC 0)
# =====================================================================

def build_airlink_bits(mant_pdus: Sequence[bytes],
                       fec_mode: int = 0) -> List[int]:
    """AirLink Payload -> full frame bit stream (bitsync + framesync +
    CC(first block) + CC(follow-on stream)).

    Mirrors the decoder's layout exactly: first block CC'd separately
    (tail + pad to byte boundary), all follow-on blocks scrambled and
    RS-coded per block but CC'd as ONE stream with a single tail.
    """
    payload = b"".join(mant_pdus)
    if len(payload) > 1023:
        raise ValueError("AirLink payload exceeds 1023 bytes")

    # AirLink header: version(2)=0 reserved(4)=0 length(10)=payload len
    # (length EXCLUDES the 2 header bytes — see decoder.py §3.2 note).
    hdr = ((0 << 14) | (0 << 10) | len(payload)).to_bytes(2, "big")
    pdu = hdr + payload

    fb_data = FIRST_BLOCK_DATA[fec_mode]        # 24
    fo_data = FOLLOWON_BLOCK_DATA[fec_mode]     # 32
    parity = RS_PARITY[fec_mode]                # 16

    first = pdu[:fb_data]
    if len(first) < fb_data:
        first += bytes([PAD_BYTE]) * (fb_data - len(first))
    rest = pdu[fb_data:]

    def rs_block(block: bytes) -> bytes:
        return rs_encode(scramble_block(block), parity)

    # First block: own CC pass with tail, padded to a byte boundary
    # (spec 3.5.2: modes 1/2 pad "in the same manner" as mode 0). For the
    # first block this is 656 / 784 / 800 bits in modes 0 / 1 / 2.
    first_cc = conv_encode(bytes_to_bits(rs_block(first)),
                           fec_mode=fec_mode, append_tail=True)
    while len(first_cc) % 8:
        first_cc.append(0)

    bits: List[int] = []
    bits += [(BITSYNC_HEX >> i) & 1 for i in range(BITSYNC_BITS - 1, -1, -1)]
    fs = FRAMESYNCS[fec_mode]
    bits += [(fs >> i) & 1 for i in range(FRAMESYNC_BITS - 1, -1, -1)]
    bits += first_cc

    if rest:
        rs_stream = b""
        for off in range(0, len(rest), fo_data):
            rs_stream += rs_block(rest[off:off + fo_data])
        fo_cc = conv_encode(bytes_to_bits(rs_stream),
                            fec_mode=fec_mode, append_tail=True)
        while len(fo_cc) % 8:
            fo_cc.append(0)
        bits += fo_cc

    return bits


# =====================================================================
# Modulator — RRC-shaped FSK baseband audio
# =====================================================================

def shape_bits(bits: Sequence[int], amplitude: float = 0.8) -> np.ndarray:
    """NRZ bits -> RRC pulse-shaped waveform at 43.2 kHz (9 samples/bit).

    Applies the spec's 71-tap root-raised-cosine (the decoder applies the
    same taps as its matched filter, giving the full RC response end to
    end). Output normalized so |peak| == amplitude.
    """
    nrz = np.zeros(len(bits) * SAMPLES_PER_BIT, dtype=np.float32)
    lvl = np.where(np.asarray(bits, dtype=np.int8) > 0, 1.0, -1.0)
    nrz[::SAMPLES_PER_BIT] = lvl * SAMPLES_PER_BIT  # impulse train
    shaped = np.convolve(nrz, RRC_TAPS, mode="same")
    peak = np.max(np.abs(shaped))
    if peak > 0:
        shaped = shaped * (amplitude / peak)
    return shaped.astype(np.float32)


def agc_tone(duration_ms: float, amplitude: float = 0.8,
             freq: float = AGC_TONE_FREQ) -> np.ndarray:
    n = int(TX_SAMPLE_RATE * duration_ms / 1000.0)
    t = np.arange(n) / TX_SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(duration_ms: float) -> np.ndarray:
    """Carrier-only / tail: keyed carrier with no audio modulation."""
    return np.zeros(int(TX_SAMPLE_RATE * duration_ms / 1000.0),
                    dtype=np.float32)


@dataclass
class TxTiming:
    """Per-site radio timing (defaults = the field-measured AL205 values)."""
    warmup_ms: float = 750.0   # PTT lead before any audio
    co_ms: float = 10.0        # carrier-only
    agc_ms: float = 55.0       # AGC tone
    tail_ms: float = 5.0       # RF tail after data


def build_tx_audio(bits: Sequence[int],
                   timing: TxTiming = TxTiming(),
                   amplitude: float = 0.8) -> np.ndarray:
    """Full transmission audio: CO + AGC tone + shaped data + tail.

    NOTE: warm-up is PTT lead time, not audio — handled by transmit().
    """
    return np.concatenate([
        silence(timing.co_ms),
        agc_tone(timing.agc_ms, amplitude),
        shape_bits(bits, amplitude),
        silence(timing.tail_ms),
    ])


def test_tone(duration_ms: float = 2000.0, amplitude: float = 0.8,
              half: bool = False,
              freq: float = AGC_TONE_FREQ) -> np.ndarray:
    """Standalone calibration tone. half=True -> half amplitude (-6 dB)."""
    return agc_tone(duration_ms, amplitude * (0.5 if half else 1.0), freq)


def write_wav(path: str, audio: np.ndarray,
              sample_rate: int = DEFAULT_OUTPUT_RATE,
              source_rate: int = TX_SAMPLE_RATE) -> None:
    """Write audio as 16-bit mono WAV, RESAMPLING to `sample_rate`.

    `audio` is assumed to be at `source_rate` (43200 Hz, the synthesis rate).
    Earlier versions only stamped the header without resampling, so a file
    labelled 48000 still contained 43200 Hz samples and would not decode when
    played. The default is now 48000 so the output works on ordinary hardware
    without a separate `sox` step.

    Pass sample_rate=TX_SAMPLE_RATE to keep the native synthesis rate, e.g.
    for a file that will be decoded directly rather than played.
    """
    if sample_rate != source_rate:
        audio = resample_to(audio, source_rate, sample_rate)
    elif sample_rate not in COMMON_OUTPUT_RATES:
        logger.warning(
            "writing %s at %d Hz, which most soundcards cannot play "
            "natively; playing it without resampling will NOT decode. "
            "Use sample_rate=48000, or play via ALSA 'plughw:' which "
            "resamples for you.", path, sample_rate)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())


# =====================================================================
# PTT control
# =====================================================================

class NullPTT:
    """No keying (WAV export / bench use)."""
    def key(self): pass
    def unkey(self): pass
    def close(self): pass


class SerialPTT:
    """PTT via serial control line. line='rts' or 'dtr'."""
    def __init__(self, port: str, line: str = "rts", invert: bool = False):
        import serial  # pyserial
        self._ser = serial.Serial(port)
        self._line = line.lower()
        self._invert = invert
        self.unkey()

    def _set(self, state: bool):
        v = (not state) if self._invert else state
        if self._line == "rts":
            self._ser.rts = v
        elif self._line == "dtr":
            self._ser.dtr = v
        else:
            raise ValueError("line must be 'rts' or 'dtr'")

    def key(self):   self._set(True)
    def unkey(self): self._set(False)
    def close(self):
        try:
            self.unkey()
        finally:
            self._ser.close()


class GpioPTT:
    """PTT via Raspberry Pi GPIO (BCM numbering). active_high default."""
    def __init__(self, pin: int, active_high: bool = True):
        try:
            from gpiozero import DigitalOutputDevice
            self._dev = DigitalOutputDevice(pin, active_high=active_high,
                                            initial_value=False)
            self._impl = "gpiozero"
        except ImportError:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT,
                       initial=GPIO.LOW if active_high else GPIO.HIGH)
            self._gpio, self._pin, self._ah = GPIO, pin, active_high
            self._impl = "rpi"

    def key(self):
        if self._impl == "gpiozero":
            self._dev.on()
        else:
            self._gpio.output(self._pin,
                              self._gpio.HIGH if self._ah else self._gpio.LOW)

    def unkey(self):
        if self._impl == "gpiozero":
            self._dev.off()
        else:
            self._gpio.output(self._pin,
                              self._gpio.LOW if self._ah else self._gpio.HIGH)

    def close(self):
        self.unkey()
        if self._impl == "gpiozero":
            self._dev.close()


# =====================================================================
# Transmit
# =====================================================================

def transmit(audio: np.ndarray,
             ptt=None,
             timing: TxTiming = TxTiming(),
             device: Optional[Union[int, str]] = None,
             sample_rate: int = DEFAULT_OUTPUT_RATE) -> None:
    """Key PTT, wait the warm-up, play audio, unkey.

    Audio is synthesised at TX_SAMPLE_RATE (43200) and RESAMPLED to
    `sample_rate` before playback, because few soundcards accept 43200 Hz.
    Playing 43200 Hz samples on a 48000 Hz device stretches every symbol by
    1.111x and the frame will not decode, while still looking like a strong
    signal on the air.

    Requires the `sounddevice` package for playback.
    """
    import sounddevice as sd
    ptt = ptt or NullPTT()
    try:
        ptt.key()
        time.sleep(timing.warmup_ms / 1000.0)
        play_audio = resample_to(audio, TX_SAMPLE_RATE, sample_rate)
        sd.play(play_audio, samplerate=sample_rate, device=device,
                blocking=True)
    finally:
        # small settle so the tail clears the modulator before unkey
        time.sleep(0.01)
        ptt.unkey()


# =====================================================================
# High-level convenience
# =====================================================================

def make_sensor_frame(source_addr: int,
                      readings: Sequence[Tuple[int, Union[int, float]]],
                      dest_addr: Optional[int] = None,
                      timestamp: Optional[int] = None,
                      test: bool = False,
                      hop_limit: int = 3,
                 fec_mode: int = 0) -> List[int]:
    """Sensor report frame: [(sensor_id, value), ...] -> AirLink bits."""
    app = build_srp_payload([build_gsr(readings)], timestamp=timestamp,
                            test=test)
    pdu = build_mant_pdu(app, source_addr, dest_addr=dest_addr,
                         port=MANT_PORT_SRP, hop_limit=hop_limit)
    return build_airlink_bits([pdu], fec_mode=fec_mode)


def make_v2_query_frame(source_addr: int,
                        dest_addr: int,
                        params: Sequence[int] = (
                            PARAM_TDMA_BYTES_REMAINING,),
                        hop_limit: int = 3,
                        fec_mode: int = 0) -> List[int]:
    """IND API v2 Get-parameter query addressed to dest_addr."""
    payload = build_ind_get(params)
    pdu = build_mant_pdu(payload, source_addr, dest_addr=dest_addr,
                         port=MANT_PORT_CC, hop_limit=hop_limit)
    return build_airlink_bits([pdu], fec_mode=fec_mode)


def make_raw_frame(source_addr: int, payload_hex: str,
                   dest_addr: Optional[int] = None,
                   port: int = MANT_PORT_SRP,
                   hop_limit: int = 3,
                 fec_mode: int = 0) -> List[int]:
    """Arbitrary MANT payload from hex."""
    pdu = build_mant_pdu(bytes.fromhex(payload_hex), source_addr,
                         dest_addr=dest_addr, port=port,
                         hop_limit=hop_limit)
    return build_airlink_bits([pdu], fec_mode=fec_mode)


# =====================================================================
# Report-type builders: TBRG (Type 2) and MSR (Types 3/4/5)
# =====================================================================
# Mirror alert2.srp parsers exactly so generated reports decode back
# byte-identical. MSR field definitions are imported from srp so the two
# can never drift.

from .srp import MSR_TYPE3_FIELDS, MSR_TYPE4_FIELDS, MSR_TYPE5_FIELDS

_MSR_FIELDS = {3: MSR_TYPE3_FIELDS, 4: MSR_TYPE4_FIELDS, 5: MSR_TYPE5_FIELDS}


def build_tbrg(sensor_id: int, accumulator: int,
               tip_offsets: Sequence[int] = (),
               accum_fmt: int = 1, accum_len: int = 2) -> bytes:
    """Type 2 Tipping Bucket report (T + L + sid + fmt/len + accum + tips).

    accumulator: running tip count (unsigned). tip_offsets: 1-byte time
    offsets since report start (0-255), one per recent tip.
    """
    fl = ((accum_fmt & 0xF) << 4) | (accum_len & 0xF)
    accum_bytes = int(accumulator).to_bytes(accum_len, "big",
                                            signed=(accum_fmt == 2))
    body = bytes([sensor_id, fl]) + accum_bytes + bytes(
        b & 0xFF for b in tip_offsets)
    return bytes([0x02]) + _report_length_field(len(body)) + body


def build_msr(report_type: int, values: dict) -> bytes:
    """Multi-Sensor Report (Type 3 English, 4 Metric, 5 IND).

    values: {sensor_id: value} for the fields to include. The data-flags
    byte is derived from which fields are present (bit per field, in the
    spec's field order). Values are pre-scaling raw or human — human
    values are divided by the field scale to get the transmitted integer.
    """
    if report_type not in _MSR_FIELDS:
        raise ValueError("report_type must be 3, 4, or 5")
    fields = _MSR_FIELDS[report_type]

    flags = 0
    body = b""
    for bit_pos, (name, sid, sz, fmt_code, scale, unit) in enumerate(fields):
        if sid not in values:
            continue
        flags |= (1 << bit_pos)
        raw = values[sid]
        # invert the decoder's scaling: transmitted int = human / scale
        iv = int(round(raw / scale)) if scale != 1 else int(round(raw))
        signed = ((fmt_code >> 4) & 0xF) == 2
        body += iv.to_bytes(sz, "big", signed=signed)

    payload = bytes([flags]) + body
    return bytes([report_type]) + _report_length_field(len(payload)) + payload


def make_tbrg_frame(source_addr: int, sensor_id: int, accumulator: int,
                    tip_offsets: Sequence[int] = (),
                    timestamp: Optional[int] = None,
                    dest_addr: Optional[int] = None,
                    hop_limit: int = 3,
                    fec_mode: int = 0) -> List[int]:
    """'Pretend to be a rain gauge' — a TBRG report frame."""
    app = build_srp_payload([build_tbrg(sensor_id, accumulator, tip_offsets)],
                            timestamp=timestamp)
    pdu = build_mant_pdu(app, source_addr, dest_addr=dest_addr,
                         hop_limit=hop_limit)
    return build_airlink_bits([pdu], fec_mode=fec_mode)


def make_msr_frame(source_addr: int, report_type: int, values: dict,
                   timestamp: Optional[int] = None,
                   dest_addr: Optional[int] = None,
                   hop_limit: int = 3,
                 fec_mode: int = 0) -> List[int]:
    """'Pretend to be a multi-sensor station' — an MSR frame."""
    app = build_srp_payload([build_msr(report_type, values)],
                            timestamp=timestamp)
    pdu = build_mant_pdu(app, source_addr, dest_addr=dest_addr,
                         hop_limit=hop_limit)
    return build_airlink_bits([pdu], fec_mode=fec_mode)


def make_status_frame(source_addr: int, battery_v: float = 12.8,
                      clock_status: int = 0, ind_temp_c: float = 25.0,
                      msgs_rcvd: int = 0, msgs_sent: int = 0,
                      status_bits: int = 0,
                      timestamp: Optional[int] = None,
                      dest_addr: Optional[int] = None,
                      fec_mode: int = 0) -> List[int]:
    """'Pretend to be a site' status report — MSR Type 5 (IND status).

    The same report shape the real repeaters emit hourly (battery, clock,
    temp, msg counters, status bits). status_bits bit-7 (0x80) is the
    slot-overrun flag we investigated.
    """
    values = {
        201: clock_status,   # Clock Status
        8:   battery_v,      # Battery (0.1 V)
        202: ind_temp_c,     # IND Temp (0.1 C)
        203: msgs_rcvd,      # Messages Received
        204: msgs_sent,      # Messages Sent
        205: status_bits,    # Status Bits
    }
    return make_msr_frame(source_addr, 5, values, timestamp=timestamp,
                          dest_addr=dest_addr, fec_mode=fec_mode)


# =====================================================================
# IND API — bundled Get, and Set / Save
# =====================================================================

API_SET_PARAMETER = 0x0A
API_SAVE_CONFIG = 0x78

# Named parameters for the console picker: code -> (name, category, access)
IND_PARAMS = {
    0x52: ("TDMA Bytes Remaining", "Health", "RO"),
    0x7F: ("Clock Status", "Health", "RO"),
    0x75: ("API Version Number", "Health", "RO"),
    0x96: ("Version String", "Health", "RO"),
    0x8080: ("Last NV-Save Status", "Health", "RO"),
    0x66: ("TX Radio Warm-Up Time", "TX-Timing", "RW"),
    0x60: ("Carrier-Only Time", "TX-Timing", "RW"),
    0x61: ("AGC Time", "TX-Timing", "RW"),
    0x62: ("RF Tail Time", "TX-Timing", "RW"),
    0x68: ("TX Audio Modulation Voltage", "TX-Timing", "RW"),
    0x65: ("TX Radio Always On", "TX-Timing", "RW"),
    0x48: ("TDMA Frame Length", "TDMA", "RW"),
    0x4A: ("TDMA Slot Length", "TDMA", "RW"),
    0x4B: ("TDMA Slot Start Offset", "TDMA", "RW"),
    0x4E: ("TDMA Slot Padding", "TDMA", "RW"),
    0x4F: ("TDMA Center Transmission", "TDMA", "RW"),
    0x51: ("TDMA Slot Overrun Behavior", "TDMA", "RW"),
    0x9015: ("Clock Source", "Clock", "RW"),
    0x9016: ("NTP Servers", "Clock", "RW"),
    0x9000: ("Baud Rate", "Serial", "RW"),
    0x9001: ("Parity", "Serial", "RW"),
    0x9002: ("Stop Bits", "Serial", "RW"),
    0x9006: ("Timeout", "Serial", "RW"),
    0x18: ("IND (Source) Address", "Addressing", "RW"),
    0x19: ("Destination Address", "Addressing", "RW"),
    0x1A: ("Add Path Service", "Addressing", "RW"),
    0x40: ("Hop Limit", "Addressing", "RW"),
    0x77: ("Agency Identifier", "Addressing", "RW"),
    0x56: ("Status Report Interval (min)", "Reporting", "RW"),
    0x57: ("Status Report Offset (min)", "Reporting", "RW"),
    0x41: ("EERDS Enable", "EERDS", "RW"),
    0x8452: ("AirLink Noise Level", "Diagnostics", "RO"),
    0x8450: ("Symbol Errors Corrected", "Diagnostics", "RO"),
    0x8453: ("MANT PDUs Decoded", "Diagnostics", "RO"),
    0x844E: ("AirLink FEC Mode", "Diagnostics", "RO"),
}


def build_ind_set(param: int, value: bytes) -> bytes:
    """IND API Set-parameter TLV: 0x0A <len> <param-tlv>.

    NOTE: whether the AL205R accepts remote (over-the-air) Set commands
    is firmware-dependent — bench-verify before trusting on a live site.
    A Set is not persistent until an NV-Save (0x78) is also sent.
    """
    inner = _ext(param) + _ext(len(value)) + bytes(value)
    return _ext(API_SET_PARAMETER) + _ext(len(inner)) + inner


def build_ind_save() -> bytes:
    """IND API Save-Configuration command (0x78) — persist to NV."""
    return _ext(API_SAVE_CONFIG) + _ext(0)
