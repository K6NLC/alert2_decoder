# `alert2-actions` — run things when a gauge crosses a line

A third consumer of the decoder's `--udp-emit` datagrams (alongside the
Campbell forwarder and `alert2-aprs`). A text config declares **blocks**
— named lists of shell commands and HTTP requests — and **rules** that
fire them when a site's reading crosses a threshold, with hysteresis and
debounce. A keyboard console fires any block by hand.

```bash
alert2-decode --stdin ... --udp-emit 127.0.0.1:9998 --udp-emit 127.0.0.1:9997
alert2-actions --config actions.ini --check          # validate, list
alert2-actions --config actions.ini --dry-run        # log, run nothing
alert2-actions --config actions.ini --fire cam_high  # one block, by hand
alert2-actions --config actions.ini                  # live + console
alert2-actions --config actions.ini --no-console     # as a service
```

`--udp-emit` may now be repeated, one address per consumer.

Start from `actions.ini.example` (package root); every field is
commented there.

## Rules

Each rule is a two-state machine on one sensor of one site:

```ini
[rule lagoon_high]
site     = 769
sensor   = 7          ; stage (default)
above    = 12.0       ; NORMAL -> ACTIVE once value >= above
below    = 10.5       ; ACTIVE -> NORMAL once value <= below (hysteresis)
debounce = 2          ; consecutive qualifying reports before either change
on_enter = cam_high, notify
on_exit  = cam_normal, notify
```

- `below` defaults to `above` (no hysteresis). A reading in the band
  between them changes nothing.
- `debounce` counts *consecutive* reports past the line; one report back
  on the other side resets the count. With ALERT2's 15-minute report
  cycle, `debounce = 2` means roughly 30 minutes of confirmation.
- `direction = falling` with `under` / `over` watches a drop (reservoir
  drawdown).
- `stale = 3600` + `on_stale = ...` fires once when a site has not
  reported for that long, and re-arms on the next report.
- Any number of rules may watch one site (a warning line and a flood
  line, say); any number of rules may name one block.
- Rule state is saved to `state_file` after every change, so a restart
  while the river is up does not re-fire `on_enter`, and still fires
  `on_exit` when it drops.
- Truncated frames are ignored.

## Blocks

```ini
[block cam_high]
key  = h                                    ; console hotkey
http = GET http://10.0.0.5/rcp.xml?command=...&payload=2 auth=service:PASS digest
run  = logger -t alert2 "lagoon HIGH ${ALERT2_VALUE} ft"
stop_on_error = no                          ; default: log and continue
timeout = 60                                ; per step, seconds
```

Steps run in order (`run`, `http`, `mail`, `wait`). `run` is a shell command; `mail` is
`to@addr[,more] "subject" "body"` through the SMTP server in `[actions]`
(Gmail works with an App Password; a carrier's email-to-SMS address such
as `number@vtext.com` turns it into a text message); `http` is
`GET|POST url [auth=user:pass] [digest] [insecure] [body=...]` with basic
or digest auth. `insecure` accepts any TLS certificate and allows weak
keys, for that request only — IP cameras commonly present a self-signed
certificate whose key Python's TLS rejects outright ("EE certificate key
too weak"). Credentials are redacted in the log and in `--check`. Every step sees the event in its environment —
`ALERT2_RULE`, `ALERT2_SITE`, `ALERT2_SENSOR`, `ALERT2_VALUE`,
`ALERT2_EVENT` (`enter|exit|stale|manual`), `ALERT2_TIME` — and
`${ALERT2_...}` is substituted inside http URLs too. Each step's result
goes to the log.

### Bosch cameras

Bosch IP cameras accept RCP+ commands over HTTP at `/rcp.xml` with
digest authentication, which the `http` step supports directly. The
specific command and payload for switching a recording profile (or
raising a virtual alarm input that a profile is tied to) comes from the
camera's RCP+ reference for its firmware, or from watching what
Configuration Manager sends; the example config shows the shape only.
Test the URL with `--fire` before wiring it to a rule.

### Waits

`wait = 3` (or `30s`, `2m`, `1h`) pauses a block before its next step —
IR on, wait, snapshot, wait, IR off. Blocks run on a worker thread, one
at a time in the order they were triggered, so a wait holds only that
block; datagrams, rules and the console keep going.

## Timers

```ini
[timer ir_every_10min]
every        = 10m            ; seconds, or 30s / 10m / 1h
block        = ir_still
while_active = lagoon_high    ; only while that rule is ACTIVE
immediate    = yes            ; also fire the moment it becomes eligible
```

`while_normal = RULE` is the reverse gate; no gate means always. A timer
that is due but not eligible waits for its next tick, unless
`immediate = yes`, in which case it fires as soon as the rule flips. The
block runs with `ALERT2_EVENT=timer`, plus the gating rule's site and
last value.

## Console

With a terminal attached, each block's `key` fires it; `s` prints every
rule's state and last reading, `?` the key list, `q` quits. Manual
fires run with `ALERT2_EVENT=manual` and do not change rule state.

## Measured

End to end from the QTH capture through one decoder run fanning out to
`alert2-aprs` and `alert2-actions` at once: site 735's 212.33 ft stage
crossed a 200 ft line with `debounce = 2` on its second report, the
block ran with the value in its environment, and the state file recorded
ACTIVE. 12 tests cover the state machine (hysteresis, debounce, two
rules per site, falling, stale re-arm, persistence), block execution
(order, environment, failure handling, dry run), http spec parsing and
the CLI.
