# `alert2-aprs` — posting gauges to APRS-IS / aprs.fi

A separate small consumer. The decoder keeps doing what it does; this
process listens for its `--udp-emit` datagrams and posts each configured
site to APRS-IS under its own tactical callsign, with stage as telemetry
so aprs.fi graphs it.

```bash
# terminal 1 — the decoder, as today, plus the UDP emit
alert2-decode --stdin --sample-rate 48000 ... --udp-emit 127.0.0.1:9998

# terminal 2 — the emitter
export APRS_PASSCODE=NNNNN
alert2-aprs --config aprs.ini --dry-run      # print the lines first
alert2-aprs --config aprs.ini                # then for real
```

`--check` validates the config and prints the site table without
connecting. `--dry-run` prints every APRS line it would send; paste one
into aprs.fi's raw-packet view to confirm the parse before going live.

## Config

Start from `aprs.ini.example` in the package root (or `alert2-aprs --write-example aprs.ini`); every field is commented there.

```ini
[aprs]
server       = rotate.aprs2.net:14580
login        = N0CALL-10       ; your call-SSID, for the APRS-IS login only
; passcode   = NNNNN           ; or the APRS_PASSCODE environment variable
tocall       = APZA2T          ; experimental tocall, "ALERT2"
listen       = 127.0.0.1:9998  ; where the decoder's --udp-emit points
min_interval = 300             ; s, per site; never post more often
defs_every   = 10              ; telemetry definitions every N frames
call_prefix  = AL2             ; tactical call = prefix + site id

[site 735]
name        = Arroyo Grande Ck at Hwy 1
lat         = 35.1213
lon         = -120.5789
symbol      = /w               ; water station; /_ for a weather symbol
stage       = 7                ; sensor id carrying stage (default 7)
stage_units = ft
stage_range = 0,300            ; engineering range the 0-255 channel spans
battery     = 8                ; sensor id (default 8)
comment     =                  ; optional free text appended

[site 720]
call = AGCRK                   ; override the tactical call
lat  = 35.30
lon  = -120.66
stage_range = 0,20
```

Sites not listed are ignored, so bring them up one at a time. A
`[site N]` header gives the ALERT2 source id; `src = N` inside the
section works too.

## What gets sent, per report

```
AL2735>APZA2T,TCPIP*:/011640z3507.28N/12034.73Ww Stage 212.33 ft Batt 13.0V via 4604,4601
AL2735>APZA2T,TCPIP*:T#000,180,130,053,000,000,00000000
```

and, on the first frame and every `defs_every` after, the definitions
addressed to the site's own call:

```
AL2735>APZA2T,TCPIP*::AL2735   :PARM.Stage,Batt,MER,Rain1h,Rain24h
AL2735>APZA2T,TCPIP*::AL2735   :UNIT.ft,V,dB,in,in
AL2735>APZA2T,TCPIP*::AL2735   :EQNS.0,1.17647,0,0,0.1,0,0,0.1,0,0,0.01,0,0,0.1,0
AL2735>APZA2T,TCPIP*::AL2735   :BITS.00000000,Arroyo Grande Ck at Hwy 1
```

- The position packet's `DDHHMMz` is the **SRP report time**, resolved
  from the ALERT2 timestamp (seconds since 12:00 AM or PM UTC, whichever
  is more recent), not the receive time. Through rc48 this was resolved
  from noon only, which stamped every report received between 00:00 and
  12:00 UTC twelve hours early; fixed in rc49 and checked against
  117,584 timestamped reports from an AL205R (all within 5 minutes of
  the IND's receive time).
  A relayed copy of the same report via another repeater path is
  recognised by that time and not posted twice.
- Telemetry channels are fixed: 1 Stage, 2 Battery, 3 MER, 4 Rain1h,
  5 Rain24h. By default (`telemetry_format = raw`) the decoder's values
  go into the channels verbatim — `T#073,9.11,13.1,10.04,0,0,...` with an
  identity EQNS — so what aprs.fi graphs is exactly what was decoded, to
  the hundredth. The APRS spec defines channels as 0-255 integers;
  aprs.fi's parser accepts decimals, which is why this is the default
  here. `telemetry_format = counts` gives the spec-pure form for any
  consumer that rejects decimals; that mode needs the per-site
  `stage_range` and resolution notes below.

  **Resolution** is `(hi - lo) / telemetry_max`, so span only the band
  the level moves in, not the whole datum. A reservoir at 1180-1300 ft:

  | `stage_range` | `telemetry_max` | ft per count | error |
  |---|---|---|---|
  | 0,1300 | 255 | 5.10 | +/-2.5 ft |
  | 1180,1300 | 255 | 0.47 | +/-0.24 ft |
  | 0,1300 | 9999 | 0.13 | +/-0.07 ft |
  | 1180,1300 | 9999 | 0.012 | +/-0.006 ft |

  `telemetry_max` (in `[aprs]`, default 255) widens every channel; the
  APRS spec says 255 but aprs.fi's parser accepts larger values and many
  trackers send up to 9999. Confirm the graph at 255 first, then raise
  it. The exact reading is always in the position packet's comment text
  regardless; telemetry only drives the graph.
- Tactical from-calls: aprs.fi shows position and telemetry for any
  from-call the server accepts, and the server accepts packets whose
  from-call differs from the login (that is how igates work). The login
  call appears only in the `qAC` construct the server adds.

## Behaviour

One TCP connection, login `user CALL pass N vers alert2-aprs 0.1`,
reconnect with backoff (5 s doubling to 5 min). While disconnected,
reports are dropped, not queued — a gauge reading is only worth posting
now. Truncated frames and unknown sources are ignored. Every line sent
goes to the log; stats are printed at exit.

## Rain — not yet mapped, and why

The ALERT2 `Rain` reading is a cumulative tip counter since the gauge was
last reset, with wrap at the field width. APRS wants windowed totals:
`r` last hour, `p` last 24 h, `P` since midnight, in hundredths of an
inch. Turning one into the other means keeping a per-site history of
(time, accumulator), differencing over the windows, and treating a
decrease as a reset. That is a few hours of history before the 24 h
figure is honest, so it comes after stage is seen working on aprs.fi.
Telemetry channels 4 and 5 are already reserved for it so the
definitions will not move.
