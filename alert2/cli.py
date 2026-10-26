# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Command-line interface for the ALERT2 decoder.

Basic usage:
    python -m alert2 capture.wav
    python -m alert2 --stdin
    rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - | python -m alert2 --stdin

With logging and CSV output:
    python -m alert2 --stdin --log-file alert2.log --csv-file alert2.csv

With JSON Lines output to a file (or '-' for stdout):
    python -m alert2 --stdin --jsonl-file alert2.jsonl

Daily rotating logs:
    python -m alert2 --stdin --log-file alert2.log --rotate-daily

Show session statistics at end:
    python -m alert2 capture.wav --stats

Maximize yield from a recorded file (multi-phase, offline only):
    python -m alert2 capture.wav --multiphase
"""
import argparse
import logging
import sys
from . import __version__, Alert2Decoder
from .stream import stream_decode_stdin
from .demod import demodulate
from .output import (
    StdoutOutput, LogFileOutput, CsvOutput, JsonlOutput,
    StatsCollector, MultiOutput,
)


def _parse_fec_modes(text) -> list:
    """'0,1,2' -> [0, 1, 2]; validates against the modes the spec defines."""
    if isinstance(text, (list, tuple)):
        modes = [int(m) for m in text]
    else:
        modes = [int(m) for m in str(text).replace(' ', '').split(',') if m]
    bad = [m for m in modes if m not in (0, 1, 2)]
    if bad or not modes:
        raise SystemExit(f"--fec-modes: expected a comma-separated subset "
                         f"of 0,1,2 (got {text!r})")
    return sorted(set(modes))


def build_outputs(args):
    """Build a MultiOutput from the args.
    Returns (multi_output, stats_or_None, web_output_or_None)."""
    outputs = []
    stats = None
    web_output = None

    if not args.quiet:
        outputs.append(StdoutOutput(raw=args.raw,
                                     include_timestamp=args.timestamp_stdout))

    if args.log_file:
        outputs.append(LogFileOutput(args.log_file, raw=args.raw,
                                       rotate_daily=args.rotate_daily))

    if args.csv_file:
        outputs.append(CsvOutput(args.csv_file,
                                  rotate_daily=args.rotate_daily))

    if args.jsonl_file:
        outputs.append(JsonlOutput(args.jsonl_file,
                                    rotate_daily=args.rotate_daily))

    # Diagnostic mode — extra per-packet detail for offline analysis.
    # Either auto-generated path (--diag-mode) or explicit (--diag-file).
    if args.diag_mode or args.diag_file:
        from .output import DiagOutput
        if args.diag_file:
            diag_path = args.diag_file
        else:
            # Default to alert2.diag in the current directory, rotated daily
            diag_path = 'alert2.diag'
        outputs.append(DiagOutput(diag_path,
                                    rotate_daily=args.rotate_daily,
                                    log_fs_failures=args.log_framesync_failures,
                                    fs_failures_hd_max=args.show_framesync_hd_max))
        print(f"Diagnostic log: {diag_path}", file=sys.stderr)
        if args.log_framesync_failures:
            print(f"  ...with framesync failure markers (HD ≤ "
                  f"{args.show_framesync_hd_max})", file=sys.stderr)
    elif args.log_framesync_failures:
        # User asked for fs failure logging but didn't request a diag file
        print("WARNING: --log-framesync-failures requires --diag-file; "
              "no failures will be logged", file=sys.stderr)

    # Live framesync display to stderr (independent of file outputs)
    if args.show_framesync:
        from .output import FramesyncDisplayOutput
        outputs.append(FramesyncDisplayOutput(
            hd_max=args.show_framesync_hd_max))
        print(f"Framesync display enabled (HD ≤ "
              f"{args.show_framesync_hd_max})", file=sys.stderr)

    # UDP emit for hand-off to a forwarder process. Format: HOST:PORT.
    # Decoder emits each packet as one JSON UDP datagram, fire-and-forget.
    if args.udp_emit:
        from .output import UdpEmitOutput
        try:
            host, port_str = args.udp_emit.rsplit(':', 1)
            port = int(port_str)
        except (ValueError, AttributeError):
            print(f"ERROR: --udp-emit value '{args.udp_emit}' must be HOST:PORT",
                   file=sys.stderr)
            sys.exit(1)
        if not host:
            host = '127.0.0.1'
        outputs.append(UdpEmitOutput(host, port))
        print(f"UDP emit to {host}:{port}", file=sys.stderr)

    # Stats always collected (cheap). They print to stderr at session end
    # unless --no-stats is given. Optionally also write JSON file.
    if not args.no_stats or args.stats_file:
        stats = StatsCollector()
        outputs.append(stats)

    # Web UI is purely additive — completely independent of file logging.
    if args.web_ui:
        from .web import WebOutput
        web_output = WebOutput(buffer_size=args.web_ui_buffer, stats=stats,
                                max_tracked_sources=args.max_tracked_sources)
        outputs.append(web_output)

    return MultiOutput(outputs), stats, web_output


def main():
    parser = argparse.ArgumentParser(
        prog='alert2',
        description='ALERT2 hydrologic protocol decoder',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  Decode a WAV file:
      python -m alert2 capture.wav

  Decode live SDR audio:
      rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - | python -m alert2 --stdin

  Decode at lower framesync threshold (more permissive):
      python -m alert2 capture.wav --max-fs-hd 6

  Save to CSV and human-readable log, with daily rotation:
      python -m alert2 --stdin \\
          --log-file logs/alert2.log \\
          --csv-file logs/alert2.csv \\
          --rotate-daily \\
          --stats

  Stream JSON Lines to stdout (no human-readable output):
      python -m alert2 --stdin --jsonl-file - --quiet
''',
    )
    # Input
    parser.add_argument('wavfile', nargs='?',
                        help='Input WAV file (or use --stdin)')
    parser.add_argument('--stdin', action='store_true',
                        help='Read raw int16 mono PCM from stdin')
    parser.add_argument('-r', '--sample-rate', type=int, default=48000,
                        help='Sample rate for stdin input (default: 48000)')
    parser.add_argument('-d', '--list-devices', action='store_true',
                        help='List available ALSA capture devices and '
                             'RTL-SDR devices, print example commands, '
                             'then exit. Useful for figuring out the right '
                             '--device argument for arecord or -d for '
                             'rtl_fm.')

    # Decoder tuning
    parser.add_argument('--max-fs-hd', type=int, default=5,
                        help='Max Hamming distance for framesync match (default: 5)')
    parser.add_argument('--fec-modes', default='0', metavar='LIST',
                        help='Comma-separated AirLink FEC modes to search for '
                             '(0=k7 r1/2, 1=k9 r2/3, 2=k9 r3/4). Each mode '
                             'has its own framesync word; "0,1,2" searches '
                             'all three (default: 0)')

    # Output options
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress human-readable stdout output')
    parser.add_argument('--raw', action='store_true',
                        help='Show raw MANT PDU bytes instead of just app payload')
    parser.add_argument('--timestamp-stdout', action='store_true',
                        help='Prefix stdout output with timestamp')
    parser.add_argument('--log-file',
                        help='Append decoded packets to a log file '
                             '(human-readable, with timestamps)')
    parser.add_argument('--csv-file',
                        help='Append decoded packets to a CSV file '
                             '(structured, with full field detail)')
    parser.add_argument('--jsonl-file',
                        help='Append decoded packets to a JSON Lines file '
                             "(use '-' for stdout)")
    parser.add_argument('--diag-mode', action='store_true',
                        help='Enable diagnostic mode: writes a detailed '
                             'per-packet log (alert2.diag) with RS '
                             'corrections, framesync HD, byte counts, '
                             'and raw hex payloads. Useful for analyzing '
                             'truncated/marginal packets, especially when '
                             'paired with WAV captures.')
    parser.add_argument('--diag-file',
                        help='Path for the diagnostic log (implies --diag-mode). '
                             'Default if --diag-mode alone: alert2.diag')
    parser.add_argument('--udp-emit', metavar='HOST:PORT',
                        help='Send each decoded packet as a JSON datagram to '
                             'the specified UDP endpoint. Used to feed an '
                             'external forwarder process (see alert2_forwarder). '
                             'Fire-and-forget: decoder does not block on slow '
                             'or absent consumers. Example: 127.0.0.1:9999')
    parser.add_argument('--rotate-daily', action='store_true',
                        help='Open new log/csv/jsonl files at local midnight')

    # Framesync display (live monitoring of framesync attempts)
    parser.add_argument('--show-framesync', action='store_true',
                        help='Print framesync detection events to stderr as '
                             'they happen — useful for live monitoring while '
                             'listening to radio audio. Each line shows the '
                             'framesync Hamming distance and whether it '
                             'decoded successfully or failed. Failed '
                             'framesyncs are candidates for "packets missed '
                             'by ear" — bursts you can hear but the decoder '
                             "couldn't recover. In streaming mode (--stdin) "
                             "this is real-time; in WAV-file mode all events "
                             "fire in the few seconds the decoder runs.")
    parser.add_argument('--show-framesync-hd-max', type=int, default=2,
                        metavar='N',
                        help='Only show framesyncs with HD ≤ N (default 2). '
                             'Higher values include more noise-induced '
                             'false-positive framesyncs that clutter the '
                             'display. Set to 5 to see everything (matches '
                             '--max-fs-hd default).')
    parser.add_argument('--log-framesync-failures', action='store_true',
                        help='Add `# FS_FAIL` markers to the diag file for '
                             'framesync attempts that failed to decode any '
                             'packet. Subject to --show-framesync-hd-max. '
                             'Prefixed with `#` so existing diag parsers '
                             'ignore them. Requires --diag-file.')

    # Stats - always collected by default, printed to stderr at session end.
    # Pass --no-stats to suppress terminal output (file output unaffected).
    parser.add_argument('--stats', action='store_true',
                        help='(default behavior; deprecated) Print decoder '
                             'statistics at end of session')
    parser.add_argument('--no-stats', action='store_true',
                        help='Suppress the per-session statistics summary '
                             'printed to stderr at session end')
    parser.add_argument('--stats-file',
                        help='Write decoder statistics summary to a JSON file '
                             'at end of session (in addition to stderr)')
    parser.add_argument('--stats-in-log', action='store_true',
                        help='Also append the stats summary to the --log-file '
                             'at end of session')

    # Web UI
    parser.add_argument('--web-ui', action='store_true',
                        help='Enable browser-based monitoring UI '
                             '(default http://127.0.0.1:8080)')
    parser.add_argument('--web-ui-port', type=int, default=8080,
                        help='Port for the web UI (default 8080)')
    parser.add_argument('--web-ui-bind', default='127.0.0.1',
                        help='Bind address for the web UI '
                             '(default 127.0.0.1; use 0.0.0.0 for LAN access)')
    parser.add_argument('--web-ui-buffer', type=int, default=500,
                        help='Number of recent packets to keep in the web UI '
                             'ring buffer (default 500)')
    parser.add_argument('--max-tracked-sources', type=int, default=500,
                        help='Maximum number of unique source addresses to '
                             'track in the web UI SNR history. When exceeded, '
                             'the least-recently-seen source is evicted '
                             '(default 500)')

    # Diagnostic
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable verbose logging')
    parser.add_argument('--chunk-seconds', type=float, default=1.5,
                        help='Live-stream decode buffer size in seconds '
                             '(default 1.5). Only affects --stdin streaming, '
                             'not WAV-file decode (which processes the whole '
                             'file at once).')
    parser.add_argument('--overlap-seconds', type=float, default=0.6,
                        help='Live-stream chunk overlap in seconds '
                             '(default 0.6). MUST exceed the longest burst '
                             '(~452 ms for the largest frames observed on '
                             'the SLO network) or boundary-straddling '
                             'packets get truncated twice instead of '
                             'decoded once cleanly. Raise for sites with '
                             'unusually long frames; do not lower below '
                             '~0.5 s. Only affects --stdin streaming.')
    # --- Decoder default policy (single source of truth) ---------------
    # These constants make promoting a feature to default a ONE-LINE
    # change with a clear audit trail, per the staged-rollout discipline
    # in docs/SOFT_VITERBI.md and docs/SOFT_FRAMESYNC.md.
    #
    #   SOFT_VITERBI_DEFAULT: promoted to True in v0.9.0a3 on the
    #     strength of 5 real captures (+15% logical, zero regressions,
    #     zero payload mismatches). A 24h run is still wanted to put a
    #     confidence interval on the rate; until then `--no-soft-viterbi`
    #     reverts to hard with no code change.
    #   SOFT_FRAMESYNC_DEFAULT: stays False until its four-way A/B
    #     (hard/hard, softV-only, softFS-only, both) demonstrates
    #     additive gain with zero hard-only regressions, per
    #     docs/SOFT_FRAMESYNC.md §5d. Flip this one line when it does.
    _SOFT_VITERBI_DEFAULT = True
    _SOFT_FRAMESYNC_DEFAULT = False

    mer = parser.add_argument_group('signal-quality measurement (MER)')
    mer.add_argument('--mer-mode', choices=('off', 'basic', 'full'),
                     default='full',
                     help='MER reporting. "full" adds MER_eq (MER after an '
                          'ideal linear equalizer, i.e. how much of the '
                          'impairment is removable ISI). MEASURED COST: '
                          'basic +6%% CPU, full +37%%. On a host already near '
                          'decode_rt 1.0 use basic, off, or --mer-sample. '
                          '[default: full]')
    mer.add_argument('--phase-dedup', choices=('best', 'first'),
                     default='best',
                     help='When several symbol phases decode the SAME '
                          'payload, which copy to keep. "best" retains the '
                          'one whose sampling instant best matched the '
                          'known preamble; "first" reproduces the previous '
                          'behaviour, where the primary phase always won. '
                          'The packet SET is identical either way — only '
                          'the retained copy differs. Measured on real '
                          'captures, first-wins discarded a mean 3.55 dB '
                          '(max 7.48 dB) of MER. [default: best]')
    mer.add_argument('--burst-all-phases', action='store_true',
                     help='STAGE 2 (experimental). Feed the burst pass '
                          'framesync candidates from every symbol phase '
                          'instead of only the detection phase, then decode '
                          'each burst ONCE at its own best phase. Rationale: '
                          'the winning phase is uniformly random over all 9 '
                          '(measured 10.3-12.2%% each, n=1269) with no '
                          'memory, so partial coverage is structurally '
                          'lossy; and detection is only 1.3%% of CPU after '
                          'vectorisation, while decode is ~76%%. Requires '
                          '--burst-mode. Compare against a run without it '
                          'before trusting it.')
    mer.add_argument('--burst-phase-count', type=int, default=9, metavar='N',
                     help='Phases scanned for burst candidates when '
                          '--burst-all-phases is set. [default: 9 = all]')
    mer.add_argument('--candidate-gate', type=float, default=0.0,
                     metavar='CORR',
                     help='Reject framesync candidates whose 80-bit preamble '
                          'correlation falls below CORR, BEFORE running the '
                          'Viterbi. 0 = off (default). Suggested 0.40. '
                          'Measured across four captures (70 real bursts, '
                          '924 spurious): real bursts scored >= 0.610, '
                          'spurious <= 0.559, and a 0.40 gate rejected '
                          '~98%% of spurious candidates while losing none — '
                          'giving 2.2-2.5x less CPU with byte-identical '
                          'packet sets. Unlike a recovery rung this can only '
                          'REMOVE decodes, so it is opt-in; raise it above '
                          '~0.55 at your own risk.')
    mer.add_argument('--phase-select', action='store_true',
                     help='Detect bursts across all candidate phases first, '
                          'then decode each burst ONCE at the phase whose '
                          'sampling instant best matched its preamble, '
                          'instead of decoding every burst at every phase '
                          'and keeping the best copy. Requires --multiphase. '
                          'Measured on a 40s QTH capture at 9 phases: same '
                          '42 packets, 0 lost, 0 gained, 1.68x faster.')
    mer.add_argument('--phase-attempts', type=int, default=1, metavar='N',
                     help='With --phase-select, how many phases to try per '
                          'burst in descending preamble-correlation order. '
                          '1 is enough on measured data (the top phase '
                          'decoded 41/41); 2 adds a fallback in case the '
                          'best-correlating phase fails Reed-Solomon, at '
                          'about a third of the speedup. [default: 1]')
    mer.add_argument('--timing-probe', action='store_true',
                     help='Measure, per decoded burst, the symbol-timing '
                          'offset that would have best matched the known '
                          '80-bit preamble, and log it to the diag as '
                          't_off (samples; 9 = 1 symbol). OBSERVATION ONLY '
                          '— decoding is unchanged. Offline on 15 bursts, '
                          'MER correlated -0.978 with |t_off| and the '
                          'offsets were whole samples, i.e. the wrong one '
                          'of the 9 phases rather than a small error. This '
                          'flag exists to confirm that on live traffic at '
                          'n=thousands before any decode change is built.')
    mer.add_argument('--mer-sample', type=int, default=1, metavar='N',
                     help='Measure MER on 1 frame in N. Day-scale statistics '
                          'need only a sample: N=10 costs ~0.6%% (basic) or '
                          '~3.7%% (full) while still yielding hundreds of '
                          'readings/day. [default: 1 = every frame]')

    sv = parser.add_mutually_exclusive_group()
    sv.add_argument('--soft-viterbi', dest='soft_viterbi',
                    action='store_true', default=_SOFT_VITERBI_DEFAULT,
                    help='Use soft-decision Viterbi (all FEC modes). '
                         '~1-2 dB coding gain in AWGN-limited conditions. '
                         'See docs/SOFT_VITERBI.md.'
                         + (' [default]' if _SOFT_VITERBI_DEFAULT else ''))
    sv.add_argument('--no-soft-viterbi', dest='soft_viterbi',
                    action='store_false',
                    help='Force hard-decision Viterbi.'
                         + ('' if _SOFT_VITERBI_DEFAULT else ' [default]'))

    sf = parser.add_mutually_exclusive_group()
    sf.add_argument('--soft-framesync', dest='soft_framesync',
                    action='store_true', default=_SOFT_FRAMESYNC_DEFAULT,
                    help='Detect framesync by soft matched-filter '
                         'correlation instead of Hamming distance. '
                         'Independent of --soft-viterbi; they compose. '
                         'See docs/SOFT_FRAMESYNC.md.'
                         + (' [default]' if _SOFT_FRAMESYNC_DEFAULT else ''))
    sf.add_argument('--no-soft-framesync', dest='soft_framesync',
                    action='store_false',
                    help='Force hard-decision framesync.'
                         + ('' if _SOFT_FRAMESYNC_DEFAULT else ' [default]'))
    parser.add_argument('--rate-track', action='store_true',
                        help='Per-burst symbol-rate correction. Measures each '
                             'burst\'s clock drift from the eye-opening phase '
                             'slope over the preamble + first block and, when '
                             'it exceeds --rate-track-min-slope, re-samples the '
                             'burst on the corrected grid and decodes it too '
                             '(best-wins keeps one copy). Needed for SDR '
                             'recordings with ~0.1%% rate error; a no-op on a '
                             'crystal-locked chain. Requires --multiphase.')
    parser.add_argument('--rate-track-min-slope', type=float, default=0.002,
                        metavar='S',
                        help='Drift threshold in samples per symbol at 9 '
                             'samples/symbol (default 0.002 = 0.022%%; QTH '
                             'measures <= 0.012%%, SDR recordings ~0.1%%)')
    parser.add_argument('--soft-framesync-threshold', type=float,
                        default=0.62,
                        help='Min normalized correlation score for a soft '
                             'framesync match (default 0.62; calibration '
                             'target — sweep on real captures).')

    # Iterative decoding modes:
    #   off    — standard soft-V, no erasure-RS retry (current default)
    #   demod  — Phase 1: erasure-RS retry using demodulator-level
    #            reliability (|soft sample|). Verified +2.2% on 6 captures.
    #   sova   — Phase 1.5: canonical Hagenauer-Hoeher SOVA reliability
    #            (per-bit metric gap from sliding-window backward update).
    #            Theoretically more accurate; under evaluation.
    _ITERATIVE_DEFAULT = 'off'
    parser.add_argument('--max-erasures', type=int, default=None,
                        help="Cap on RS erasures per block for SOVA/demod "
                             "iterative modes (the analog of --max-fs-hd for "
                             "the erasure stage). Default None = full parity "
                             "budget (least safe). Lower values (e.g. 16-32 "
                             "for FEC mode 0) sharply cut fabrication rate at "
                             "the cost of fewer recoveries. No effect when "
                             "--iterative-mode is off.")
    parser.add_argument('--max-rs-corr', type=int, default=None,
                        help="General cap on total RS corrections for ANY "
                             "decode (standard or erasure). Coarse backstop "
                             "against pathological over-correction. Default "
                             "None = off. Field data shows real standard "
                             "decodes never exceeded ~19, so ~32+ rejects no "
                             "real standard traffic.")
    parser.add_argument('--sova-max-fs-hd', type=int, default=None,
                        help="Restrict SOVA erasure recovery to framesync "
                             "Hamming distance <= this value, independent of "
                             "--max-fs-hd (so framesync detection can run "
                             "wider for SOVA-off decodes). Part of the "
                             "validated combined SOVA gate; pair with "
                             "--sova-max-rs-corr. Recommended: 5.")
    parser.add_argument('--sova-max-rs-corr', type=int, default=None,
                        help="Restrict SOVA erasure recovery to TOTAL RS "
                             "corrections <= this value. The validated "
                             "discriminator: real ALERT2 packets need little "
                             "RS correction (mostly 0, max observed 63), "
                             "while fabrications cluster high (>=32). With "
                             "--sova-max-fs-hd 5 this gave zero fabrications "
                             "across two field days. Recommended: 32.")
    parser.add_argument('--sova-max-rs-corr-per-block', type=int, default=None,
                        help="Optional per-block RS correction cap for SOVA "
                             "erasure recovery (worst single block). Off "
                             "unless set. Size-invariant co-gate for "
                             "deployments with larger packets where a flat "
                             "total scales with block count.")
    parser.add_argument('--eq-retry', action='store_true', default=False,
                        help="Preamble-trained equalizer retry (rc15). On a "
                             "failed standard soft decode, fit a 5-tap FIR "
                             "per burst on the 80 known preamble bits and "
                             "retry the standard decode on equalized "
                             "samples. Acceptance is by plain unforced RS "
                             "(zero fabrication risk). Targets the TK790 "
                             "analog chain's length-dependent truncation; "
                             "recoveries stack with gated SOVA.")
    parser.add_argument('--presync-eq', action='store_true', default=False,
                        help="Detection-only pre-equalizer (rc16). Self-"
                             "calibrates a static FIR from decoded bursts' "
                             "preambles, then runs the framesync search on "
                             "the equalized bit stream as well, merging in "
                             "candidates the raw stream missed. Decode "
                             "always uses original samples. Lowers real "
                             "packets' framesync HD 2-3 points on distorted "
                             "chains, letting max-fs-hd run lower for the "
                             "same catch rate (large CPU saving).")
    parser.add_argument('--burst-mode', action='store_true', default=False,
                        help="Burst-scoped decoding (rc23). After the "
                             "chunk detection pass, cut each detected "
                             "burst out of the audio and demodulate the "
                             "cut, so the symbol-clock phase and "
                             "amplitude are fitted to that burst instead "
                             "of to the whole window. Each burst's "
                             "optimal phase is effectively random "
                             "(measured: 8 of 9 values), which is what "
                             "--multiphase currently brute-forces at 4x "
                             "the cost. Additive to the primary pass.")
    parser.add_argument('--phase-block-scan', action='store_true',
                        default=False,
                        help="Estimate the symbol-clock phase by scanning "
                             "the whole window in blocks and scoring each "
                             "phase by its strongest block, instead of "
                             "judging on the leading 0.185 s (which at "
                             "ALERT2 traffic density is usually noise). "
                             "Measured 5x more likely to pick a burst's "
                             "own phase — but NOT superset-safe alone "
                             "(it reshuffles which bursts a single "
                             "window phase serves), so use it with "
                             "--burst-mode or --multiphase.")
    parser.add_argument('--health-interval', type=float, default=300.0,
                        help="Seconds between live audio-clock health "
                             "reports written to the diag as AUDIO_CLOCK "
                             "lines (streaming only). Compares samples "
                             "actually read against samples the wall "
                             "clock says the sound card produced; a "
                             "growing deficit means audio is being "
                             "dropped upstream. Default 300.")
    parser.add_argument('--presync-max-fs-hd', type=int, default=None,
                        help="Framesync HD threshold for the presync-eq "
                             "second search (default: max-fs-hd minus 1). "
                             "Real packets sync 2-3 HD lower on the "
                             "equalized stream, so a lower threshold here "
                             "keeps presync's catch while cutting its "
                             "noise-candidate CPU cost several-fold.")
    parser.add_argument('--iterative-mode', dest='iterative',
                        choices=['off', 'demod', 'sova'],
                        default=_ITERATIVE_DEFAULT,
                        help='Iterative decoding mode. '
                             "'off' (default) = standard soft-V; "
                             "'demod' = erasure-RS using demod confidence "
                             "(Phase 1, +1.64%% measured, filtered for "
                             "false positives); "
                             "'sova' = erasure-RS using canonical SOVA "
                             "reliability (Phase 1.5; experimental, "
                             "does not exceed demod in measured tests).")
    # Back-compat aliases for the bool form
    it = parser.add_mutually_exclusive_group()
    it.add_argument('--iterative', dest='iterative_bool',
                    action='store_true', default=None,
                    help="Alias for --iterative-mode demod.")
    it.add_argument('--no-iterative', dest='iterative_bool',
                    action='store_false',
                    help="Alias for --iterative-mode off.")
    # Multi-phase matched-filter decode (file mode only). The demod picks
    # one symbol-sampling phase for the whole stream; phase wanders within
    # bursts, so a single fixed phase can miss recoverable packets. This
    # pass decodes at every phase and unions the results.
    parser.add_argument('--multiphase', dest='multiphase',
                        action='store_true', default=False,
                        help='Decode the top-N symbol-sampling phases '
                             '(ranked by eye-opening) and union the results. '
                             'Recovers packets a single fixed phase misses on '
                             'healthy-SNR captures (no gain on low-SNR/'
                             'under-driven signals). Works for file and live '
                             '--stdin decode. Number of phases set by '
                             '--phase-candidates (default 3, which captured '
                             'all complete-packet gain of the full 9-phase '
                             'search at ~1/3 the cost). Off by default.')
    parser.add_argument('--phase-candidates', dest='phase_candidates',
                        type=int, default=3, metavar='N',
                        help='With --multiphase, how many sampling phases to '
                             'try, ranked by eye-opening (default 3). N=9 '
                             'tries them all (exhaustive, most resources); '
                             'N=2-3 is the efficient sweet spot for Pi-class '
                             'hardware. Ignored without --multiphase.')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')

    args = parser.parse_args()

    # Device listing is a "print and exit" mode — handle it before any
    # other arg validation so users can run `alert2 -d` without also
    # needing to supply a wavfile or --stdin.
    if args.list_devices:
        from .devices import list_devices_and_exit
        list_devices_and_exit()
        sys.exit(0)

    # Resolve back-compat alias
    if args.iterative_bool is True:
        args.iterative = 'demod'
    elif args.iterative_bool is False:
        args.iterative = 'off'

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        stream=sys.stderr)

    if not args.wavfile and not args.stdin:
        parser.print_help()
        sys.exit(1)

    if getattr(args, 'multiphase', False) and args.stdin:
        print(f"Multi-phase live decode enabled (top {args.phase_candidates} "
              "phases by eye-opening). Affordable on x86 laptops; "
              "benchmark on a Pi4 before trusting it to keep up live.",
              file=sys.stderr)

    output, stats, web_output = build_outputs(args)

    web_server = None
    if web_output is not None:
        from .web import start_web_server
        try:
            web_server = start_web_server(
                web_output, stats,
                bind=args.web_ui_bind,
                port=args.web_ui_port,
            )
            print(f"Web UI: http://{args.web_ui_bind}:{args.web_ui_port}",
                  file=sys.stderr)
        except OSError as e:
            print(f"Could not start web UI on {args.web_ui_bind}:"
                  f"{args.web_ui_port}: {e}", file=sys.stderr)
            print("Continuing without web UI.", file=sys.stderr)
            web_server = None

    try:
        if args.wavfile:
            run_file_mode(args, output, stats)
        else:
            run_stdin_mode(args, output, stats)
    finally:
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()


def run_file_mode(args, output, stats):
    """Decode a single WAV file."""
    import scipy.io.wavfile as wavio
    import numpy as np

    fs, data = wavio.read(args.wavfile)
    if data.ndim > 1:
        data = data[:, 0]
    audio = data.astype(np.float32) / 32768.0

    # Demodulate: audio -> bits + info dict (matched filter, timing recovery).
    # (This call was missing, leaving `bits`/`info` undefined below.)
    from .demod import demodulate
    bits, info = demodulate(audio, fs)

    decoder = Alert2Decoder(max_framesync_hd=args.max_fs_hd, output=output,
                            fec_modes=_parse_fec_modes(args.fec_modes),
                            soft_viterbi=args.soft_viterbi,
                            mer_mode=args.mer_mode,
                            mer_sample=args.mer_sample,
                            timing_probe=args.timing_probe,
                            candidate_gate=args.candidate_gate,
                            soft_framesync=args.soft_framesync,
                            soft_framesync_threshold=args.soft_framesync_threshold,
                            iterative=args.iterative,
                            max_erasures=args.max_erasures,
                            max_rs_corr=args.max_rs_corr,
                            sova_max_fs_hd=args.sova_max_fs_hd,
                            sova_max_rs_corr=args.sova_max_rs_corr,
                            sova_max_rs_corr_per_block=args.sova_max_rs_corr_per_block,
                            eq_retry=args.eq_retry,
                            presync_eq=args.presync_eq,
                            presync_max_fs_hd=args.presync_max_fs_hd)
    audio_43k = info.get('audio_43k')
    phase = info.get('best_phase', 0)
    soft_samples = info.get('soft_samples')
    amp_est = info.get('amplitude_estimate')

    if not args.quiet:
        if args.soft_viterbi:
            print(f"Soft-decision Viterbi enabled (FEC modes {args.fec_modes}).",
                  file=sys.stderr)
        if args.soft_framesync:
            print(f"Soft-decision framesync enabled "
                  f"(threshold {args.soft_framesync_threshold}).",
                  file=sys.stderr)

    seen = set()
    _collected = []
    count = 0
    for label, b_use, inv in [
            ('matched-filter', bits, False),
            ('matched-filter-inverted', [1 - b for b in bits], True)]:
        for pkt in decoder.decode_bits(b_use,
                                         audio_43k=audio_43k,
                                         bit_phase=phase,
                                         soft_samples=soft_samples,
                                         amplitude_estimate=amp_est,
                                         filtered_43k=info.get('filtered_43k'),
                                         soft_inverted=inv):
            pkt.decode_path = label
            # Collected rather than emitted immediately so that a later
            # phase decoding the SAME payload can be compared against this
            # copy (see _select_best_copies). Emission happens once, after
            # every pass for this file has run.
            _collected.append(pkt)

    # Multi-phase pass (--multiphase): decode the top-N symbol phases
    # ranked by eye-opening (variance) and union. Recovers packets a
    # single fixed phase misses on healthy-SNR captures. See docs for
    # evidence; top-3 captured all complete-packet gain of the exhaustive
    # search at ~1/3 the cost.
    if getattr(args, 'multiphase', False):
        from .demod import (matched_filter, resample_to_rx_rate, slice_bits,
                            rank_phases)
        from .fec import estimate_amplitude
        from .constants import SAMPLES_PER_BIT
        n_cand = getattr(args, 'phase_candidates', 3)
        _bb = audio - audio.mean()
        _bb43 = resample_to_rx_rate(_bb, fs)
        _filt = matched_filter(_bb43)
        _candidates = rank_phases(_filt, n_cand)
        if not args.quiet:
            print(f"Multi-phase decode enabled (top {n_cand} of "
                  f"{SAMPLES_PER_BIT} phases by eye-opening: "
                  f"{sorted(_candidates)}).", file=sys.stderr)
        for _ph in _candidates:
            if _ph == phase:
                continue  # already decoded at the demod's best phase
            _soft = _filt[_ph::SAMPLES_PER_BIT]
            _bits = slice_bits(_soft)
            _soft_list = [float(x) for x in _soft]
            _amp = estimate_amplitude(_soft_list)
            for label, b_use, inv in [
                    ('mf-phase%d' % _ph, _bits, False),
                    ('mf-phase%d-inverted' % _ph,
                     [1 - b for b in _bits], True)]:
                for pkt in decoder.decode_bits(
                        b_use, audio_43k=_bb43, bit_phase=_ph,
                        soft_samples=(_soft_list if not inv
                                      else [-x for x in _soft_list]),
                        amplitude_estimate=_amp,
                        filtered_43k=_filt,
                        soft_inverted=inv):
                    pkt.decode_path = label
                    _collected.append(pkt)

        # Per-burst symbol-rate correction (--rate-track). See rate_track.py.
        if getattr(args, 'rate_track', False):
            try:
                from .burst_detect import detect_bursts
                from .rate_track import decode_rate_corrected
                _bursts = detect_bursts(
                    _filt, list(range(SAMPLES_PER_BIT)), args.max_fs_hd,
                    decoder.fec_modes,
                    min_corr=max(args.candidate_gate or 0.0, 0.40))
                _rt = decode_rate_corrected(
                    decoder, _filt, _bursts,
                    min_slope=args.rate_track_min_slope)
                if not args.quiet:
                    print(f"Rate-track: {len(_bursts)} bursts, "
                          f"{len(_rt)} packets on corrected grids.",
                          file=sys.stderr)
                _collected.extend(_rt)
            except Exception as e:
                logger.warning(f"rate-track failed: {e}")
    elif getattr(args, 'rate_track', False) and not args.quiet:
        print("--rate-track needs --multiphase; ignored.", file=sys.stderr)

    # Best-wins selection, then emit. mode='first' reproduces the
    # historical first-decode-wins behaviour exactly.
    from .stream import _select_best_copies
    for pkt in _select_best_copies(_collected,
                                   getattr(args, 'phase_dedup', 'best')):
        key = (pkt.fec_mode, pkt.raw_payload)
        if key in seen:
            continue
        seen.add(key)
        count += 1
        output.emit(pkt)

    output.close()

    if stats is not None:
        # Print to terminal unless explicitly suppressed
        if not args.no_stats:
            stats.print_summary()
        # Write JSON summary file if requested
        if args.stats_file:
            stats.write_summary_file(args.stats_file)
        # Append summary to log file if requested
        if args.stats_in_log and args.log_file:
            _append_stats_to_log(stats, args.log_file)


def _append_stats_to_log(stats, log_file_path):
    """Append the stats summary to the log file at session end."""
    import io
    buf = io.StringIO()
    stats.print_summary(file=buf)
    summary_text = buf.getvalue()
    # Resolve potential daily-rotation suffix
    import os, datetime
    path = log_file_path
    if not os.path.exists(path):
        # Try with date suffix (rotate-daily)
        base, ext = os.path.splitext(log_file_path)
        path = f"{base}-{datetime.date.today().isoformat()}{ext}"
        if not os.path.exists(path):
            path = log_file_path  # give up, write to original even if it doesn't exist
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n')
            f.write(summary_text)
    except Exception as e:
        print(f"Warning: could not append stats to {path}: {e}", file=sys.stderr)


def run_stdin_mode(args, output, stats):
    """Decode a streaming PCM source from stdin."""
    if not args.quiet:
        print(f"Decoding stream from stdin at {args.sample_rate} Hz... "
              "(Ctrl-C to stop)", file=sys.stderr)
        if args.soft_viterbi:
            print(f"Soft-decision Viterbi enabled (FEC modes {args.fec_modes}).",
                  file=sys.stderr)
        if args.soft_framesync:
            print(f"Soft-decision framesync enabled "
                  f"(threshold {args.soft_framesync_threshold}).",
                  file=sys.stderr)

    # SIGTERM handler so systemd / kill (default signal) shuts the
    # decoder down cleanly, allowing SESSION_END markers to be written
    # by output.close() in the `finally` below.
    import signal
    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, OSError):
        pass  # may fail in non-main thread or some environments

    def _health_cb(info):
        # route the live-pipeline health report to every output that
        # wants it (DiagOutput writes an AUDIO_CLOCK comment line)
        try:
            for _o in getattr(output, 'outputs', [output]):
                _o.note_stream_health(info)
        except Exception:
            pass

    count = 0
    try:
        for pkt in stream_decode_stdin(sample_rate=args.sample_rate,
                                         max_framesync_hd=args.max_fs_hd,
                                         fec_modes=_parse_fec_modes(args.fec_modes),
                                         soft_viterbi=args.soft_viterbi,
                            mer_mode=args.mer_mode,
                            mer_sample=args.mer_sample,
                            timing_probe=args.timing_probe,
                            candidate_gate=args.candidate_gate,
                            phase_select=args.phase_select,
                            rate_track=args.rate_track,
                            rate_track_min_slope=args.rate_track_min_slope,
                            phase_attempts=args.phase_attempts,
                            phase_dedup=args.phase_dedup,
                            burst_all_phases=args.burst_all_phases,
                            burst_phase_count=args.burst_phase_count,
                                         soft_framesync=args.soft_framesync,
                                         soft_framesync_threshold=args.soft_framesync_threshold,
                                         iterative=args.iterative,
                                         max_erasures=args.max_erasures,
                                         max_rs_corr=args.max_rs_corr,
                                         sova_max_fs_hd=args.sova_max_fs_hd,
                                         sova_max_rs_corr=args.sova_max_rs_corr,
                                         sova_max_rs_corr_per_block=args.sova_max_rs_corr_per_block,
                                         eq_retry=args.eq_retry,
                                         presync_eq=args.presync_eq,
                                         presync_max_fs_hd=args.presync_max_fs_hd,
                                         burst_mode=args.burst_mode,
                                         phase_block_scan=args.phase_block_scan,
                                         multiphase=args.multiphase,
                                         phase_candidates=args.phase_candidates,
                                         chunk_seconds=args.chunk_seconds,
                                         overlap_seconds=args.overlap_seconds,
                                         health_callback=_health_cb,
                                         health_interval_s=args.health_interval):
            count += 1
            output.emit(pkt)
    except KeyboardInterrupt:
        print(f"\nStopped. Decoded {count} packets.", file=sys.stderr)
    finally:
        output.close()
        if stats is not None:
            if not args.no_stats:
                stats.print_summary()
            if args.stats_file:
                stats.write_summary_file(args.stats_file)
            if args.stats_in_log and args.log_file:
                _append_stats_to_log(stats, args.log_file)


if __name__ == '__main__':
    main()
