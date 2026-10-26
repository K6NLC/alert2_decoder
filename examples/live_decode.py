# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Example: decode live SDR stream and print packets.

Run with rtl_fm:
    rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - | python live_decode.py

Or you can pipe any other source of raw int16 mono PCM at the chosen sample rate.
"""
import sys
import time
from alert2.stream import stream_decode_stdin


def main():
    print("Listening for ALERT2 packets... (Ctrl-C to stop)", file=sys.stderr)

    count = 0
    start_time = time.time()
    try:
        for pkt in stream_decode_stdin(sample_rate=48000):
            count += 1
            elapsed = time.time() - start_time
            print(f"[{elapsed:8.1f}s] {pkt}")
            sys.stdout.flush()
    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        print(f"\nStopped after {elapsed:.1f}s. Decoded {count} packets.",
              file=sys.stderr)


if __name__ == '__main__':
    main()
