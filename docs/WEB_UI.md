# Web UI Guide

Optional browser-based monitoring interface, added in v0.8.0. Designed
for unattended operation: you can have the decoder running headless on a
Raspberry Pi and check on it from your laptop, phone, or tablet without
needing to touch the Pi.

## Quick start

```bash
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
  python -m alert2 --stdin --log-file alert2.log --web-ui
```

Then open `http://localhost:8080` in any browser.

## CLI flags

| Flag | Default | What it does |
|------|---------|---------------|
| `--web-ui` | off | Enable the web UI |
| `--web-ui-port N` | `8080` | TCP port to listen on |
| `--web-ui-bind ADDR` | `127.0.0.1` | Bind address |
| `--web-ui-buffer N` | `500` | How many recent packets to keep |

By default the UI binds only to `127.0.0.1`, so it's only reachable from
the same machine running the decoder. To reach it from your laptop while
the decoder runs on a Pi, you can either SSH-tunnel or bind to your LAN:

```bash
# SSH tunnel (recommended): from your laptop, forward Pi's port to your laptop
ssh -L 8080:localhost:8080 nick@k6nlc-pi
# Then open http://localhost:8080 on your laptop

# OR bind to LAN directly (anyone on your network can see the data)
python -m alert2 --stdin --web-ui --web-ui-bind 0.0.0.0
# Then open http://k6nlc-pi.local:8080 from any device on your LAN
```

The data isn't sensitive (it's hydrology sensor readings), but binding
to `0.0.0.0` is something you should opt into intentionally.

## What you see

### Main page (`/`)

Three areas:

**Header strip** shows decoder uptime, total packets received, average
SNR, and unique source count.

**Packet list (left side, fixed-width)** — the most recent packets,
newest at top. Each row shows time, source ID, repeater path, SNR, and
the sensor readings summary. Click any row to see full details on the
right.

**Detail panel (right side)** — when you click a packet, you get a
breakdown of the MANT header (source, destination, path, hop limit,
port, payload length, App PDU ID, test flag), the RF / FEC info (SNR,
framesync HD, RS corrections, FEC mode, AirLink length, SRP timestamp),
a sensor readings table with sensor IDs, names, and values, and the raw
hex application payload.

**Footer strip** shows quality stats: how many packets had RS_corr=0,
how many had FS_hd=0, total truncated count, and SNR range.

### Per-source page (`/source/N`)

Click any source ID in the main page to drill into that source. You see:

- Summary cards: packet count, avg/min/max SNR, truncated count,
  RS-corrected count, last PDU ID
- An inline SVG chart of SNR over time. Dots are colored by status:
  blue=clean, orange=had RS corrections, red=truncated. Hover for
  exact times and values.
- A history table of every packet from this source still in the
  ring buffer, with sensor data and timestamps.

The per-source view is scoped to the current decoder session — it
doesn't read historical log files.

## Filtering

The filter bar on the main page accepts simple expressions, separated by
spaces (all conditions must match — implicit AND).

| Token | What it matches |
|-------|------------------|
| `src=769` | Only show packets from source 769 |
| `rs>0` | Packets that had any RS corrections |
| `hd>1` | Framesync HD greater than 1 |
| `snr<5` | Low-SNR packets |
| `pdu_id=3` | A specific PDU ID |
| `truncated` | Bare keyword — show only truncated packets |
| `len>30` | MANT payload length > 30 bytes |

Examples:

- `src=716 rs=0` — clean decodes from source 716 only
- `truncated hd>0` — truncated packets that also had marginal framesync
- `snr<8` — show only low-SNR packets to find marginal sources

Filters are applied client-side, so they don't affect the buffer or
data flow.

## Refresh rate

Default polling is every 5 seconds. The decoder keeps emitting packets
in the background; the page just polls to find out about new ones.

When new packets arrive while you're scrolled to the top, they appear
automatically. If you've scrolled down to read older entries, an
"auto-scroll paused" banner appears so you don't lose your place.
Click the banner to jump back to the top and resume auto-scrolling.

## CSV export

The "Download CSV" button at the top exports whatever is currently in
the ring buffer (the most recent N packets, where N is set by
`--web-ui-buffer`, default 500).

This is independent of the regular `--csv-file` output. The regular CSV
file logs every packet to disk continuously; the export button is for
"grab a snapshot of what's in front of me right now."

## Performance

The web UI runs in a background thread and doesn't block the decoder.
The HTTP server uses Python's stdlib `http.server` with threading mixin,
so multiple concurrent browsers (or tabs) work fine.

The ring buffer is capped — the default 500 packets is plenty for a
running session. Older packets are dropped automatically. If you want
to see more history, increase `--web-ui-buffer`. Memory usage is
approximately 4 KB per packet, so 500 packets is ~2 MB.

The polling interval (5 sec) is hardcoded server-side and embedded in
the served HTML at startup. To change it, you'd need to edit
`alert2/templates/index.html` and `source.html` (search for
`__POLL_INTERVAL__`).

## Limitations and design choices

**Read-only.** There's intentionally no way to control the decoder from
the browser — no pause/resume, no clear-buffer, no settings changes.
This keeps the security surface minimal. If you want to change
something, stop the decoder and restart with different flags.

**Session-scoped.** The web UI shows the current session only. If the
decoder restarts, the buffer resets. For historical analysis use the
regular `--log-file` and `--csv-file` outputs.

**No auth.** The UI has no login or access control. The default
`127.0.0.1` bind ensures only local processes can reach it; if you
choose to bind to your LAN with `0.0.0.0`, anyone on your network can
see the data.

**Light theme only.** A dark theme toggle could be added but isn't in
v1. If you'd like one, let me know.

**Single-file HTML.** The two HTML pages are self-contained — all CSS
and JS is inline. No build step, no dependencies, easy to modify.
