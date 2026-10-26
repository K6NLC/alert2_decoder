# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 Protocol Decoder

A pure Python decoder for the National Hydrologic Warning Council
ALERT2 hydrologic radio protocol.

Supports:
  - AirLink Layer v1.2 (FSK demodulation, all 3 FEC modes)
  - MANT Layer v1.2 (network and transport layer parsing)
  - Source signal: WAV files or live SDR via rtl_fm

Basic usage:
    from alert2 import decode_wav
    packets = decode_wav('capture.wav')
    for p in packets:
        print(p)
"""

__version__ = '0.9.1rc38'  # rc14: off-first SOVA + validated 5/32 gate + honest-partial fallback; rc15: --eq-retry preamble-trained equalizer (unforced RS)

from .decoder import decode_wav, Alert2Decoder
from .types import Alert2Packet, MantHeader, AirLinkHeader
from .fec import (
    Viterbi, SoftViterbi, quantize_soft, estimate_amplitude,
    decode_first_block, decode_first_block_soft,
    decode_followon_stream, decode_followon_stream_soft,
)

__all__ = [
    'decode_wav',
    'Alert2Decoder',
    'Alert2Packet',
    'MantHeader',
    'AirLinkHeader',
    'Viterbi',
    'SoftViterbi',
    'quantize_soft',
    'estimate_amplitude',
    '__version__',
]
