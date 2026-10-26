# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 transmit CLI.

Examples
--------
# Sensor report: site 723, sensor 5 = 12.34, sensor 8 = 128, to WAV
python -m alert2.txcli sensor --sa 723 --reading 5=12.34 --reading 8=128 \
    --wav out.wav

# V2 query: Get TDMA Bytes Remaining (0x52) from site 4601, transmit
# with PTT on serial RTS
python -m alert2.txcli query --sa 999 --da 4601 --param 0x52 \
    --ptt serial:/dev/ttyUSB0:rts

# Raw MANT payload
python -m alert2.txcli raw --sa 4601 --payload 0b0152 --da 4601 --port 8 \
    --wav query.wav

# Calibration tones
python -m alert2.txcli tone --seconds 3 --ptt gpio:17          # full
python -m alert2.txcli tone --seconds 3 --half --wav half.wav  # half ampl.

PTT spec:  serial:<port>:<rts|dtr>[:invert]   or   gpio:<bcm-pin>[:low]
"""
import argparse
import sys

from .generator import (
    make_sensor_frame, make_v2_query_frame, make_raw_frame,
    build_tx_audio, test_tone, write_wav, transmit, TxTiming,
    NullPTT, SerialPTT, GpioPTT, PARAM_TDMA_BYTES_REMAINING,
)


def parse_ptt(spec):
    if not spec:
        return NullPTT()
    parts = spec.split(":")
    if parts[0] == "serial":
        port = parts[1]
        line = parts[2] if len(parts) > 2 else "rts"
        invert = len(parts) > 3 and parts[3] == "invert"
        return SerialPTT(port, line=line, invert=invert)
    if parts[0] == "gpio":
        pin = int(parts[1])
        active_high = not (len(parts) > 2 and parts[2] == "low")
        return GpioPTT(pin, active_high=active_high)
    raise ValueError(f"bad --ptt spec: {spec}")


def parse_reading(s):
    sid, val = s.split("=", 1)
    v = float(val) if ("." in val) else int(val, 0)
    return int(sid, 0), v


def add_common(p):
    p.add_argument("--wav", help="write to WAV instead of transmitting")
    p.add_argument("--ptt", help="serial:<port>:<rts|dtr>[:invert] "
                               "or gpio:<pin>[:low]")
    p.add_argument("--device", help="sounddevice output device")
    p.add_argument("--amplitude", type=float, default=0.8)
    p.add_argument("--fec-mode", type=int, default=0, choices=(0, 1, 2),
                   help="AirLink FEC mode: 0=k7 r1/2 (default), "
                        "1=k9 r2/3, 2=k9 r3/4")
    p.add_argument("--tx-rate", type=int, default=48000,
                   help="output sample rate for --wav and playback "
                        "(default 48000). Audio is synthesised at 43200 Hz "
                        "and resampled. Use 43200 only for a file you will "
                        "decode directly rather than play — most soundcards "
                        "cannot play 43200 natively, and doing so without "
                        "resampling will NOT decode.")
    p.add_argument("--warmup", type=float, default=750.0, help="PTT lead ms")
    p.add_argument("--co", type=float, default=10.0, help="carrier-only ms")
    p.add_argument("--agc", type=float, default=55.0, help="AGC tone ms")
    p.add_argument("--tail", type=float, default=5.0, help="RF tail ms")


def emit(audio, args, timing):
    rate = getattr(args, "tx_rate", 48000)
    dur_ms = len(audio) / 43200.0 * 1000.0
    if args.wav:
        write_wav(args.wav, audio, sample_rate=rate)
        note = "" if rate in (44100, 48000, 96000, 192000) else \
            "  [WARNING: most soundcards cannot play this rate natively]"
        print(f"wrote {args.wav} ({dur_ms:.0f} ms @ {rate} Hz){note}")
        return
    ptt = parse_ptt(args.ptt)
    try:
        transmit(audio, ptt=ptt, timing=timing,
                 device=args.device, sample_rate=rate)
        print(f"transmitted @ {rate} Hz")
    finally:
        ptt.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="alert2-tx",
                                 description="ALERT2 packet generator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sensor", help="send a General Sensor Report")
    s.add_argument("--sa", type=lambda x: int(x, 0), required=True)
    s.add_argument("--da", type=lambda x: int(x, 0))
    s.add_argument("--reading", action="append", required=True,
                   metavar="ID=VALUE", help="repeatable, e.g. 5=12.34")
    s.add_argument("--timestamp", type=int,
                   help="seconds since last 12:00 UTC")
    s.add_argument("--test-flag", action="store_true",
                   help="set the SRP test flag")
    add_common(s)

    q = sub.add_parser("query", help="IND API v2 Get-parameter query")
    q.add_argument("--sa", type=lambda x: int(x, 0), required=True)
    q.add_argument("--da", type=lambda x: int(x, 0), required=True)
    q.add_argument("--param", action="append", type=lambda x: int(x, 0),
                   help="parameter type(s), default 0x52")
    add_common(q)

    r = sub.add_parser("raw", help="raw MANT payload (hex)")
    r.add_argument("--sa", type=lambda x: int(x, 0), required=True)
    r.add_argument("--da", type=lambda x: int(x, 0))
    r.add_argument("--port", type=lambda x: int(x, 0), default=0)
    r.add_argument("--payload", required=True, help="hex bytes")
    add_common(r)

    t = sub.add_parser("tone", help="calibration test tone")
    t.add_argument("--seconds", type=float, default=2.0)
    t.add_argument("--freq", type=float, default=2400.0)
    t.add_argument("--half", action="store_true", help="half amplitude")
    add_common(t)

    args = ap.parse_args(argv)
    timing = TxTiming(warmup_ms=args.warmup, co_ms=args.co,
                      agc_ms=args.agc, tail_ms=args.tail)

    if args.cmd == "sensor":
        readings = [parse_reading(x) for x in args.reading]
        bits = make_sensor_frame(args.sa, readings, dest_addr=args.da,
                                 timestamp=args.timestamp,
                                 test=args.test_flag, fec_mode=args.fec_mode)
        audio = build_tx_audio(bits, timing, args.amplitude)
    elif args.cmd == "query":
        params = args.param or [PARAM_TDMA_BYTES_REMAINING]
        bits = make_v2_query_frame(args.sa, args.da, params=params,
                                   fec_mode=args.fec_mode)
        audio = build_tx_audio(bits, timing, args.amplitude)
    elif args.cmd == "raw":
        bits = make_raw_frame(args.sa, args.payload, dest_addr=args.da,
                              port=args.port, fec_mode=args.fec_mode)
        audio = build_tx_audio(bits, timing, args.amplitude)
    elif args.cmd == "tone":
        audio = test_tone(args.seconds * 1000.0, args.amplitude,
                          half=args.half, freq=args.freq)
    else:
        ap.error("unknown command")

    emit(audio, args, timing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
