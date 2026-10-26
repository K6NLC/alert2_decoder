# Output Formats

The decoder supports multiple simultaneous output formats. All can be combined
in a single command.

## Stdout (default)

Human-readable, one-line-per-packet, written to stdout. Suppress with `--quiet`.

```bash
python -m alert2 capture.wav
```

Output:
```
[FEC0] src=775 port=0 hop=0 len=26 FS_hd=0 payload=0111f924744d01150a11000834415cf7
```

Add `--timestamp-stdout` to prefix each line with a timestamp.
Add `--raw` to show the full raw MANT PDU bytes instead of just the
application payload.

## Log file (`--log-file`)

Same human-readable format as stdout, written to a file with timestamps.

```bash
python -m alert2 --stdin --log-file alert2.log
```

File content:
```
[2026-05-03 04:49:27.264] [FEC0] src=775 port=0 hop=0 len=26 FS_hd=0 payload=0111f924744d01150a11000834415cf7
```

The log file is opened in append mode, so restarting the decoder doesn't
clobber existing entries.

## CSV file (`--csv-file`)

Structured CSV with all packet fields broken out as columns. Includes a
header row when creating a new file; appends without re-writing the header
to existing files.

```bash
python -m alert2 --stdin --csv-file alert2.csv
```

Columns:
- `rx_time_iso`, `rx_time_unix` — timestamp in two formats
- `fec_mode` — 0, 1, or 2
- `framesync_hd` — Hamming distance of framesync match (lower is better)
- `rs_corrections` — number of bytes the Reed-Solomon decoder fixed
- `airlink_version`, `airlink_length` — AirLink header fields
- `mant_version`, `mant_proto_id`, `mant_source`, `mant_dest`, `mant_port`,
  `mant_hop_limit`, `mant_payload_length` — MANT header fields
- `mant_tssr`, `mant_add_path`, `mant_da_present`, `mant_encrypted`, `mant_ack`,
  `mant_added_header` — MANT flags (1 = set, 0 = clear)
- `mant_header_bytes` — total MANT header byte length
- `app_payload_hex` — application-layer payload bytes as hex
- `raw_pdu_hex` — full MANT PDU bytes as hex (header + payload)
- `decode_path` — which demod variant produced this packet

Open in any spreadsheet tool, or read with pandas:
```python
import pandas as pd
df = pd.read_csv('alert2.csv', parse_dates=['rx_time_iso'])
print(df.groupby('mant_source').size())
```

## JSON Lines file (`--jsonl-file`)

One JSON object per line. Use `-` for stdout instead of a file path.

```bash
# To file
python -m alert2 --stdin --jsonl-file alert2.jsonl

# To stdout (good for piping to other tools)
python -m alert2 --stdin --quiet --jsonl-file -
```

Each line:
```json
{
  "rx_time": {"iso": "2026-05-03T04:49:27.264740", "unix": 1777783767.264740},
  "fec_mode": 0,
  "framesync_hd": 0,
  "rs_corrections": 0,
  "decode_path": "matched-filter",
  "airlink": {"version": 0, "length": 35},
  "mant": {
    "version": 0, "proto_id": 0, "tssr": true, "add_path": true,
    "da_present": false, "port": 0, "encrypted": false, "ack": false,
    "added_header": false, "hop_limit": 0, "payload_length": 26,
    "source_addr": 775, "dest_addr": null, "header_bytes": 6
  },
  "app_payload_hex": "0111f924744d01150a11000834415cf7",
  "raw_pdu_hex": "0600001a03070111f924744d01150a11000834415cf7"
}
```

JSON Lines is widely supported by log-analysis tools (`jq`, ElasticSearch,
Splunk, etc.) and is friendlier to streaming than a single big JSON array.

Pipe to `jq` for filtering:
```bash
python -m alert2 --stdin --quiet --jsonl-file - | jq 'select(.mant.source_addr == 775)'
```

## Statistics (`--stats` and `--stats-file`)

Print a summary at end of session, write a JSON summary file, or both.

```bash
python -m alert2 capture.wav --stats
```

Terminal output (to stderr):
```
============================================================
ALERT2 Decoder Session Statistics
============================================================
Session duration:       0.4 s
Packets decoded:        1
Packets/minute:         152.31
By FEC mode:            FEC0=1
Unique source addresses: 1
Top sources:
     775: 1 packets
Framesync HD:           hd=0: 1
RS corrections:         0corr: 1
Avg payload length:     26.0 bytes
By decode path:         matched-filter=1
Framesync attempts:     2, successes: 1 (50.0%)
============================================================
```

```bash
python -m alert2 --stdin --stats-file stats.json
```

JSON file content:
```json
{
  "session_duration_sec": 1234.5,
  "packets_decoded": 250,
  "packets_per_minute": 12.15,
  "packets_by_fec_mode": {"0": 250},
  "unique_source_addresses": 35,
  "top_sources": [[775, 18], [797, 16], ...],
  "packets_by_decode_path": {"matched-filter": 240, "matched-filter-inverted": 10},
  "framesync_hd_distribution": {"0": 220, "1": 25, "2": 5},
  "rs_corrections_distribution": {"0": 245, "1": 4, "2": 1},
  "avg_payload_length": 32.5,
  "framesync_attempts": 312,
  "framesync_successes": 250,
  "framesync_success_rate": 0.801,
  "bursts_detected": 0
}
```

This is a great way to monitor decoder health over time:
- Low **framesync_success_rate** suggests a noisy or marginal signal
- High **rs_corrections** values suggest noisy bits getting through framesync
  but with errors that RS has to correct
- Most packets at **framesync_hd: 0** is the sign of a clean signal
- The **packets_per_minute** rate tells you how active your network is

## Daily rotation (`--rotate-daily`)

Add this flag to any file output (`--log-file`, `--csv-file`, `--jsonl-file`)
to automatically start a new file at local midnight. Filenames get a
`-YYYY-MM-DD` suffix inserted before the extension.

```bash
python -m alert2 --stdin \
    --log-file logs/alert2.log \
    --csv-file logs/alert2.csv \
    --rotate-daily
```

Creates files like:
- `logs/alert2-2026-05-03.log`
- `logs/alert2-2026-05-03.csv`

When the local time crosses midnight, the decoder automatically opens new
files like `logs/alert2-2026-05-04.log`.

You can also explicitly use `{date}` in the path template:
```bash
python -m alert2 --stdin --log-file 'logs/alert2-{date}.log' --rotate-daily
```

## Combining outputs

All outputs can be active at once. The decoder writes the same packet to
each enabled output independently:

```bash
python -m alert2 --stdin \
    --log-file logs/alert2.log \
    --csv-file logs/alert2.csv \
    --jsonl-file logs/alert2.jsonl \
    --rotate-daily \
    --stats \
    --stats-file logs/stats.json
```

This gives you:
- Real-time human-readable output on the terminal
- A timestamped log file
- A structured CSV for spreadsheet analysis
- A JSON Lines file for programmatic consumption
- A printed stats summary at exit
- A JSON stats file for record-keeping

If one output fails (e.g., disk full), the others continue to work.

## Programmatic use

The output classes can be used directly from Python:

```python
from alert2 import Alert2Decoder
from alert2.output import (
    StdoutOutput, LogFileOutput, CsvOutput, JsonlOutput,
    StatsCollector, MultiOutput,
)
from alert2.demod import demodulate
import scipy.io.wavfile as wav
import numpy as np

# Set up outputs
stats = StatsCollector()
output = MultiOutput([
    StdoutOutput(),
    LogFileOutput('alert2.log'),
    CsvOutput('alert2.csv'),
    stats,
])

# Decode
fs, data = wav.read('capture.wav')
audio = data.astype(np.float32) / 32768.0
bits, _ = demodulate(audio, fs)

decoder = Alert2Decoder(output=output)
for pkt in decoder.decode_bits(bits):
    output.emit(pkt)

output.close()

# Print stats
stats.print_summary()
print(stats.get_summary())
```
