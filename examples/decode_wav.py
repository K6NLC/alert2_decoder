# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Simple example: decode all packets from a WAV file and print them.

Usage:
    python decode_wav.py input.wav
"""
import sys
from alert2 import decode_wav


def main():
    if len(sys.argv) != 2:
        print("Usage: python decode_wav.py <wav_file>")
        sys.exit(1)

    wav_path = sys.argv[1]
    print(f"Decoding {wav_path}...")
    packets = decode_wav(wav_path)

    print(f"\nFound {len(packets)} unique packets:\n")
    for i, p in enumerate(packets, 1):
        print(f"--- Packet {i} ---")
        print(f"  FEC mode: {p.fec_mode}")
        print(f"  Framesync HD: {p.framesync_hd}")
        print(f"  RS corrections: {p.rs_corrections}")
        if p.airlink:
            print(f"  AirLink length: {p.airlink.length}")
        if p.mant:
            print(f"  MANT version: {p.mant.version}")
            print(f"  MANT proto:   {p.mant.proto_id}")
            print(f"  Source addr:  {p.mant.source_addr}")
            if p.mant.dest_addr is not None:
                print(f"  Dest addr:    {p.mant.dest_addr}")
            print(f"  Port:         {p.mant.port}")
            print(f"  Hop limit:    {p.mant.hop_limit}")
            print(f"  Payload len:  {p.mant.payload_length}")
        print(f"  Application payload ({len(p.application_payload)} bytes):")
        print(f"    {p.application_payload.hex()}")
        print()


if __name__ == '__main__':
    main()
