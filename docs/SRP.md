# Self-Reporting Protocol (SRP) Decoding

The decoder includes an Application Layer parser for the **Self-Reporting Protocol** (SRP), which is the application protocol used by ALERT2 sensor stations to report measurements. It runs on MANT Port 0.

When a packet's MANT Port = 0, the decoder automatically parses the application payload as an SRP PDU and includes the decoded sensor readings in all output formats.

## Supported report types

| Type | Name | Status |
|------|------|--------|
| 1 | General Sensor Report (GSR) | ✓ Fully supported |
| 2 | Tipping Bucket Rain Gage (TBRG) | ✓ Fully supported |
| 3 | Multi-Sensor Report (English units) | ✓ Fully supported |
| 4 | Multi-Sensor Report (Metric units) | ✓ Fully supported |
| 5 | Multi-Sensor Report (IND status) | ✓ Fully supported |
| 7 | Time Series Data (TSD) | ✓ Fully supported |
| 250 | SET Command | Raw bytes only |
| 251 | GET Command | Raw bytes only |

## Value formats

Per Application Spec Figure 2-6, the decoder handles all defined Format/Length combinations:

- **0x1 (Unsigned int)**: 1, 2, 3, 4, or 8 bytes
- **0x2 (Signed int, two's complement)**: 1, 2, 3, 4, or 8 bytes
- **0x3 (Floating point)**:
  - 2 bytes: FP2 format (Appendix 3) — 1-bit sign, 2-bit negative exponent, 13-bit mantissa
  - 4 bytes: IEEE 754 single-precision
  - 8 bytes: IEEE 754 double-precision
- **0x4 (UTF-8 string)**: 1-15 bytes
- **0xD (1-byte unsigned int)**: seconds before transmission
- **0xE (2-byte unsigned int)**: seconds since most recent 12:00 AM/PM UTC
- **0xF (4-byte unsigned int)**: POSIX time

Unknown formats are preserved as raw hex bytes.

## Sensor names

The decoder uses the recommended sensor IDs from Application Spec Figure 2-5:

| ID | Name |
|----|------|
| 0 | Rain |
| 1 | Air Temperature |
| 2 | Relative Humidity |
| 3 | Barometric Pressure |
| 4 | Wind Speed |
| 5 | Wind Direction |
| 6 | Peak Wind Speed |
| 7 | Stage |
| 8 | Battery Voltage |
| 201 | Clock Status |
| 202 | IND Temp |
| 203 | Messages Received |
| 204 | Messages Sent |
| 205 | Status Bits |

For administrator-assigned sensor IDs (e.g., sensor 77), the decoder shows `sensor_77=<value>` with the value still fully decoded according to its Format/Length field. No hard-coded sensor names are required.

## Output examples

### Stdout / log file (human-readable)

```
[FEC0] src=719 path=[4601] port=0 hop=0 len=32 FS_hd=0 SRP[ts=15020]: TBRG(sensor=0 accum=2426)
[FEC0] src=769 path=[4602, 4601] port=0 hop=0 len=47 FS_hd=1 SRP[ts=15081]: GSR(sensor_10=0, sensor_11=62.2433, Stage=7.2, Battery Voltage=12.67, sensor_71=0, sensor_77=7.38, sensor_81=12.66)
```

The format:
- `src=N` — original sensor station's source address
- `path=[A,B]` — repeater chain (Add Path service)
- `SRP[ts=X]` — SRP control byte info, with timestamp if present
- `[TEST]` flag included if the test bit is set
- Reports follow as `GSR(...)`, `TBRG(...)`, `MSR-english(...)`, `TSD(...)`, etc.

### CSV

CSV output adds these columns relevant to SRP:

- `mant_added_sas` — repeater path (semicolon-separated, e.g., `4602;4601`)
- `srp_summary` — compact text summary of decoded SRP

The full hex bytes are still in `app_payload_hex` for downstream tools.

### JSON Lines

JSON output includes a full `srp` object with parsed reports:

```json
{
  "srp": {
    "version": 0,
    "has_timestamp": true,
    "test_flag": false,
    "pdu_id": 1,
    "timestamp": 15020,
    "reports": [
      {
        "type": "TBRG",
        "sensor_id": 0,
        "accumulator": 2426,
        "tip_offsets_seconds": []
      },
      {
        "type": "GSR",
        "readings": [
          {
            "sensor_id": 7,
            "sensor_name": "Stage",
            "value": 7.2,
            "value_type": "fp2",
            "raw_hex": "00C8"
          },
          {
            "sensor_id": 77,
            "sensor_name": null,
            "value": 7.38,
            "value_type": "fp2",
            "raw_hex": "02E2"
          }
        ]
      }
    ]
  }
}
```

## Programmatic access

```python
from alert2 import decode_wav

packets = decode_wav('capture.wav')
for pkt in packets:
    if pkt.srp_pdu and pkt.srp_pdu.reports:
        print(f"Source {pkt.mant.source_addr}:")
        for rpt in pkt.srp_pdu.reports:
            print(f"  {rpt}")
            # Each report has structured attributes:
            if hasattr(rpt, 'readings'):
                for reading in rpt.readings:
                    print(f"    Sensor {reading.sensor_id}: {reading.value}")
```

## What's not yet implemented

- **SET/GET Command parsing** (Types 250, 251) — currently shown as raw bytes
- **Embedded timestamp markers** (Sensor ID 255) — recognized but not given special handling for following historical data
- **Concentration Protocol (Port 1)** — legacy ALERT message format, not parsed
