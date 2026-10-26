# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Data type definitions for ALERT2 packets.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Any


@dataclass
class AirLinkHeader:
    """AirLink Layer header (2 bytes per spec §3.2)."""
    version: int = 0      # 2 bits, always 0
    reserved: int = 0     # 4 bits, always 0
    length: int = 0       # 10 bits — total PDU length

    @classmethod
    def parse(cls, data: bytes) -> 'AirLinkHeader':
        """Parse 2 bytes into AirLink header."""
        if len(data) < 2:
            raise ValueError("AirLink header requires 2 bytes")
        word = (data[0] << 8) | data[1]
        return cls(
            version=(word >> 14) & 0x3,
            reserved=(word >> 10) & 0xF,
            length=word & 0x3FF,
        )


@dataclass
class MantHeader:
    """MANT (Network/Transport) Layer header.

    Per MANT spec v1.2 §2.2, the header is minimum 6 bytes (48 bits) and
    may be extended by:
      - 2 bytes: Destination Address (when da_present=1)
      - 1 byte: Number of Added Source Addresses (when add_path=1)
      - 0-12 bytes: list of added Source Addresses (each 2 bytes)
      - 1 byte: MANT PDU ID (when proto_id=1, end-to-end reliable)
    """
    version: int = 2           # 2 bits — current version is 2 per MANT spec
    proto_id: int = 0          # 3 bits
    tssr: bool = False         # Time Stamp Service Request
    add_path: bool = False     # Add Path Service Request
    da_present: bool = False   # Destination Address included
    port: int = 0              # 4 bits
    encrypted: bool = False
    ack: bool = False
    added_header: bool = False
    hop_limit: int = 0         # 3 bits
    payload_length: int = 0    # 12 bits — bytes in MANT payload (after MANT header)
    source_addr: int = 0       # 16 bits
    dest_addr: Optional[int] = None  # 16 bits, only if da_present
    num_added_sas: int = 0     # 1 byte, only if add_path
    added_sas: List[int] = field(default_factory=list)  # 0-6 entries of 16 bits
    mant_pdu_id: Optional[int] = None  # 1 byte, only if proto_id=1

    # Header byte length once parsed (varies with optional fields)
    header_bytes: int = 6

    @classmethod
    def parse(cls, data: bytes) -> 'MantHeader':
        """Parse a MANT header from a byte array starting at the MANT header.

        Returns the parsed header. The header_bytes field indicates how many
        bytes were consumed.
        """
        if len(data) < 6:
            raise ValueError("MANT header requires at least 6 bytes")

        # First 48 bits as a single big-endian integer
        word = int.from_bytes(data[:6], 'big')
        hdr = cls(
            version=(word >> 46) & 0x3,
            proto_id=(word >> 43) & 0x7,
            tssr=bool((word >> 42) & 0x1),
            add_path=bool((word >> 41) & 0x1),
            da_present=bool((word >> 40) & 0x1),
            port=(word >> 36) & 0xF,
            encrypted=bool((word >> 35) & 0x1),
            # Reserved bits 33-34
            ack=bool((word >> 32) & 0x1),
            added_header=bool((word >> 31) & 0x1),
            hop_limit=(word >> 28) & 0x7,
            payload_length=(word >> 16) & 0xFFF,
            source_addr=word & 0xFFFF,
        )
        offset = 6

        # Optional Destination Address (16 bits)
        if hdr.da_present:
            if len(data) < offset + 2:
                raise ValueError("MANT header DA flag set but no DA bytes")
            hdr.dest_addr = (data[offset] << 8) | data[offset + 1]
            offset += 2

        # Optional MANT PDU ID (8 bits) — when EERDS protocol
        if hdr.proto_id == 1:
            if len(data) < offset + 1:
                raise ValueError("MANT EERDS but no PDU ID byte")
            hdr.mant_pdu_id = data[offset]
            offset += 1

        # Optional Add Path: 1 byte count + (count × 2 bytes) of added SAs
        if hdr.add_path:
            if len(data) < offset + 1:
                raise ValueError("MANT AddPath flag set but no count byte")
            hdr.num_added_sas = data[offset]
            offset += 1
            for i in range(hdr.num_added_sas):
                if len(data) < offset + 2:
                    raise ValueError(
                        f"MANT AddPath count={hdr.num_added_sas} but ran out at SA {i}")
                sa = (data[offset] << 8) | data[offset + 1]
                hdr.added_sas.append(sa)
                offset += 2

        hdr.header_bytes = offset
        return hdr

    def __str__(self) -> str:
        s = (f"MANT v{self.version} proto={self.proto_id} "
             f"src={self.source_addr}")
        if self.dest_addr is not None:
            s += f" dst={self.dest_addr}"
        s += (f" port={self.port} hop={self.hop_limit} "
              f"len={self.payload_length}")
        if self.added_sas:
            s += f" path={self.added_sas}"
        if self.encrypted:
            s += " ENC"
        if self.ack:
            s += " ACK"
        return s


@dataclass
class Alert2Packet:
    """A fully decoded ALERT2 packet."""
    fec_mode: int                              # 0, 1, or 2
    rx_time: float = 0.0                       # Unix timestamp
    rs_corrections: int = 0                    # Total RS error corrections
    airlink: Optional[AirLinkHeader] = None
    mant: Optional[MantHeader] = None
    raw_payload: bytes = b''                   # MANT PDU bytes (header + payload)
    application_payload: bytes = b''           # Bytes after MANT header
    srp_pdu: Optional[Any] = None              # Decoded SRP PDU (if Port=0 and parses)

    # Diagnostic info
    bitsync_hd: Optional[int] = None           # Hamming distance of bitsync match
    framesync_hd: Optional[int] = None         # Hamming distance of framesync match
    sample_offset: int = 0                     # Sample where framesync ended
    decode_path: str = 'unknown'               # 'matched_filter' / 'simple'

    # Signal quality (set when audio is available for measurement)
    snr_db: Optional[float] = None             # Signal-to-noise ratio (dB)
    noise_rms: Optional[float] = None          # Pre-burst RMS (FM noise level)
    signal_rms: Optional[float] = None         # In-burst RMS (smaller = better FM lock)
    # MER: the SINAD analogue, measured on soft symbols against the
    # re-encoded transmitted sequence. Amplitude-normalised, so unlike
    # snr_db it is comparable ACROSS sites and capture chains, and it is
    # measured entirely inside the burst so it survives burst mode and
    # chunk boundaries. None when no honest reference existed.
    mer_db: Optional[float] = None             # Modulation error ratio (dB)
    mer_evm_pct: Optional[float] = None        # Error vector magnitude (%)
    mer_symbols: Optional[int] = None          # Symbols the fit ran over
    mer_coverage: Optional[str] = None         # 'full' | 'first-block'
    mer_alpha: Optional[float] = None          # Fitted symbol amplitude
    # Diagnostic-only (diag file / packet detail panel, NOT the log line or
    # summaries): MER after an ideal 5-tap linear equalizer, and the gain
    # over raw. Large gain = ISI-limited (--eq-retry helps); near-zero gain
    # = noise-limited (it cannot).
    mer_eq_db: Optional[float] = None          # MER after ideal linear eq
    mer_eq_gain_db: Optional[float] = None     # mer_eq_db - mer_db
    # MER of the stream the decode ACTUALLY used (real equalizer output),
    # as opposed to mer_db (raw channel) and mer_eq_db (ideal FIR bound).
    # None when the equalizer never trained for this frame.
    mer_act_db: Optional[float] = None         # achieved MER after real eq
    rate_ppm: Optional[float] = None           # symbol-rate error applied by --rate-track (ppm)
    # Per-burst symbol-timing probe (observation only; see timing_probe.py).
    # timing_offset is the fractional sample offset that would have peaked
    # the known-preamble match; timing_gain is how much correlation the
    # actually-used sampling instant left on the table.
    timing_offset: Optional[float] = None      # samples (9 = 1 symbol)
    timing_gain: Optional[float] = None        # preamble corr improvement
    truncated: bool = False                    # True if follow-on RS failed —
                                                # decoded data is incomplete
                                                # (partial first-block only)
    viterbi_mode: str = 'hard'                  # 'hard' or 'soft' — which
                                                # Viterbi decoded this packet
    iter_method: Optional[str] = None           # 'standard' | 'erasure' | None
                                                # (None = iterative decode not
                                                # used; 'standard' = used but
                                                # only the standard RS path
                                                # was needed; 'erasure' = at
                                                # least one block went through
                                                # the erasure-RS fallback)
    iter_n_erasures: int = 0                    # Total erasure positions used
                                                # across all blocks (0 if
                                                # method != 'erasure')
    iter_max_block_corr: int = 0                # Max per-block RS correction
                                                # load (errors + erasures) over
                                                # all blocks. Per-block metric
                                                # for --sova-max-rs-corr, robust
                                                # to packet size (unlike a flat
                                                # total).
    eq_recovered: bool = False                  # True if any block of this
                                                # packet was decoded via the
                                                # preamble-trained equalizer
                                                # retry (--eq-retry). Unforced
                                                # RS validated — as trustworthy
                                                # as a standard decode.
    eq_stage: Optional[str] = None              # Which equalizer stage
                                                # produced the recovery:
                                                # 'preamble' (80 known
                                                # preamble bits) or 'dd'
                                                # (decision-directed: FIR
                                                # retrained on the re-encoded
                                                # first block). None if
                                                # eq_recovered is False.
    burst_scoped: bool = False                  # True if this packet came
                                                # from the burst-scoped pass
                                                # (audio cut to this burst,
                                                # then demodulated — so the
                                                # symbol phase and amplitude
                                                # are fitted to the burst
                                                # rather than to a 6 s window
                                                # whose phase was estimated
                                                # from its first 0.185 s).
    presync_found: bool = False
    presync_raw_hd: Optional[int] = None        # For presync-found packets:
                                                # the RAW-stream framesync HD
                                                # at the same offset — i.e.
                                                # how far above the raw
                                                # threshold this packet sat
                                                # before equalization made it
                                                # findable. None otherwise.
    rs_block_corr: Optional[list] = None        # Per-block RS corrections
                                                # [first_block, f1, f2, ...]
                                                # when soft block-resolved
                                                # decode was used. Total ==
                                                # rs_corrections. Reveals
                                                # WHERE in the frame the
                                                # errors landed (drift ramps
                                                # toward the tail; flat noise
                                                # spreads evenly).                 # True if this packet's
                                                # framesync was found only on
                                                # the presync-equalized bit
                                                # stream (--presync-eq), i.e.
                                                # detection the raw stream
                                                # missed at the same HD limit.
                                                # Decode always uses original
                                                # samples regardless.

    # Frame bundling: when one AirLink frame carries multiple stacked MANT
    # PDUs (repeater concentration), all PDUs from that frame share these.
    # frame_pdu_count is the number of PDUs in the transmission; frame_index
    # is this PDU's position (0-based); frame_sources lists the source
    # addresses of all PDUs in the frame (including this one).
    frame_pdu_count: int = 1
    frame_index: int = 0
    frame_sources: Optional[List[int]] = None

    def __str__(self) -> str:
        parts = [f"[FEC{self.fec_mode}]"]
        if self.mant:
            parts.append(str(self.mant))
        if self.srp_pdu and self.srp_pdu.reports:
            parts.append(str(self.srp_pdu))
        elif self.application_payload:
            parts.append(f"payload={self.application_payload.hex()}")
        if self.rs_corrections:
            parts.append(f"RS_corr={self.rs_corrections}")
        return " ".join(parts)
