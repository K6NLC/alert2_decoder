# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 Protocol Constants

All values are taken directly from:
  - ALERT2 AirLink Layer Specification v1.2 (Jan 2025)
  - ALERT2 MANT Specification v1.2 (Jan 2025)
"""
import numpy as np

# =============================================================================
# Air Interface Constants
# =============================================================================

# Bit rate (per AirLink spec §2.3)
BAUD_RATE = 4800  # bits per second, ±3%

# Recommended receiver sample rate (per AirLink spec §2.3 Figure 2-4)
# This gives exactly 9 samples per bit
RX_SAMPLE_RATE = 43200  # Hz
SAMPLES_PER_BIT = 9

# RRC pulse shape (per AirLink spec §2.3)
RRC_BETA = 0.96

# 71-tap RRC FIR filter coefficients in Q15 format
# Source: AirLink Spec v1.2, Figure 2-4
RRC_TAPS_Q15 = np.array([
    -18,  -9,   5,  18,  23,  17,   1, -17,
    -31, -33, -19,   5,  31,  45,  38,  11,
    -30, -65, -77, -54,   1,  70, 122, 124,
     55, -76,-228,-332,-302, -62, 431,1163,
   2062,3001,3827,4394,4595,4394,3827,3001,
   2062,1163, 431, -62,-302,-332,-228, -76,
     55, 124, 122,  70,   1, -54, -77, -65,
    -30,  11,  38,  45,  31,   5, -19, -33,
    -31, -17,   1,  17,  23,  18,   5
], dtype=np.float32)
RRC_TAPS = RRC_TAPS_Q15 / 32768.0
assert len(RRC_TAPS) == 71

# =============================================================================
# Preamble (per AirLink spec §2.2)
# =============================================================================

# AGC tone frequency (a tone in 2000-2500 Hz range, default ~2400 Hz)
AGC_TONE_FREQ = 2400  # Hz, nominal

# Bit Synchronization pattern (48 bits)
# Sent before frame sync; transmitted MSB-first byte 0xEB onwards
BITSYNC_HEX = 0xEB90B433AAAA
BITSYNC_BITS = 48

# Frame Synchronization patterns - one per FEC mode (32 bits each)
# Per AirLink spec §2.2.4
FRAMESYNC_M0 = 0x352EF853  # FEC mode 0
FRAMESYNC_M1 = 0x5898233E  # FEC mode 1
FRAMESYNC_M2 = 0xEE434E88  # FEC mode 2
FRAMESYNC_BITS = 32

FRAMESYNCS = {
    0: FRAMESYNC_M0,
    1: FRAMESYNC_M1,
    2: FRAMESYNC_M2,
}

# =============================================================================
# Forward Error Correction (per AirLink spec §3.5)
# =============================================================================

# Block sizes per FEC mode (data bytes — RS parity not included)
FIRST_BLOCK_DATA = {0: 24, 1: 44, 2: 54}      # First block payload
FOLLOWON_BLOCK_DATA = {0: 32, 1: 44, 2: 54}   # Each follow-on block payload

# RS parity bytes per block per FEC mode
RS_PARITY = {0: 16, 1: 20, 2: 20}

# Total RS-coded block sizes (data + parity)
FIRST_BLOCK_TOTAL = {m: FIRST_BLOCK_DATA[m] + RS_PARITY[m] for m in (0, 1, 2)}
FOLLOWON_BLOCK_TOTAL = {m: FOLLOWON_BLOCK_DATA[m] + RS_PARITY[m] for m in (0, 1, 2)}

# Reed-Solomon parameters (CCSDS Blue Book §3.2 conventional basis)
RS_SYMSIZE = 8
RS_GFPOLY = 0x187   # x^8 + x^7 + x^2 + x + 1
RS_FCR = 112        # First consecutive root
RS_PRIM = 11        # Primitive root

# Convolutional code parameters
# Per AirLink spec §3.5.2 Figure 3-7
CONV_PARAMS = {
    0: {'k': 7, 'poly_a': 0x6D, 'poly_b': 0x4F, 'rate': (1, 2),
        'puncture': None},
    1: {'k': 9, 'poly_a': 0x1AF, 'poly_b': 0x11D, 'rate': (2, 3),
        # Fig. 3-7 rows A=[1 0], B=[1 1], read column-down then across:
        # A1 B1 A2 B2 -> A2 dropped = "3rd bit of every four".
        # (An earlier draft had the rows swapped, dropping B2 = 4th bit.)
        'puncture': [(1, 0), (1, 1)]},
    2: {'k': 9, 'poly_a': 0x1AF, 'poly_b': 0x11D, 'rate': (3, 4),
        'puncture': [(1, 0, 1), (1, 1, 0)]},  # remove 3rd & 6th bit of every 6
}

# =============================================================================
# Scrambling (per AirLink spec §3.4)
# =============================================================================

# 17-bit Maximum Length Sequence (MLS) scrambler
# Polynomial: x^17 + x^3 + 1
# Preload: 0x00001 (only x^1 set)
MLS_TAPS = (17, 3)  # Tap positions
MLS_PRELOAD = 0x00001
MLS_LENGTH = 17

# =============================================================================
# AirLink Frame structure (per AirLink spec §3.2)
# =============================================================================

# AirLink Header is 2 bytes:
#   2 bits: Version (always 00)
#   4 bits: Reserved (always 0000)
#  10 bits: Length of AirLink PDU (including header)
AIRLINK_HEADER_BYTES = 2

# =============================================================================
# MANT Header structure (per MANT spec)
# =============================================================================

# Minimum MANT header is 48 bits = 6 bytes:
#   2 bits:  Version (0)
#   3 bits:  Protocol ID
#   1 bit:   Time Stamp Service Request (TSSR)
#   1 bit:   Add Path Service Request
#   1 bit:   Destination Address included
#   4 bits:  Port
#   1 bit:   Encrypted
#   2 bits:  Reserved
#   1 bit:   ACK
#   1 bit:   Added Header
#   3 bits:  Hop Limit
#  12 bits:  Payload Length
#  16 bits:  Source Address
MANT_HEADER_MIN_BYTES = 6

# Protocol IDs (per MANT spec)
MANT_PROTO_SRP = 0       # Self-Reporting Protocol
MANT_PROTO_CONC = 1      # Concentration Protocol (legacy ALERT)
MANT_PROTO_EERDS = 2     # End-to-End Reliable Data Service
MANT_PROTO_NETMGMT = 7   # Network Management
