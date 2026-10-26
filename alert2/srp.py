# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 Self-Reporting Protocol (SRP) application-layer parser.

Per Application Layer Protocol Specification v1.3.

Decodes the application payload (after MANT header) into:
- Control byte
- Optional timestamp
- One or more reports (TLV format):
  - Type 1: General Sensor Report (GSR)
  - Type 2: Tipping Bucket Rain Gage Report (TBRG)
  - Type 3: Multi-Sensor Report (English)
  - Type 4: Multi-Sensor Report (Metric)
  - Type 5: Multi-Sensor Report (IND status)
  - Type 7: Time Series Data Report (TSD)
  - Type 250: SET Command
  - Type 251: GET Command

For unknown report types, raw bytes are kept and noted.
"""
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Union, Any


# Recommended sensor IDs from Application Spec Figure 2-5
RECOMMENDED_SENSOR_NAMES = {
    0: 'Rain',
    1: 'Air Temperature',
    2: 'Relative Humidity',
    3: 'Barometric Pressure',
    4: 'Wind Speed',
    5: 'Wind Direction',
    6: 'Peak Wind Speed',
    7: 'Stage',
    8: 'Battery Voltage',
    201: 'Clock Status',
    202: 'IND Temp',
    203: 'Messages Received',
    204: 'Messages Sent',
    205: 'Status Bits',
}


# =============================================================================
# Format/Length value decoder (Appendix Figure 2-6)
# =============================================================================

def decode_value(fmt: int, length: int, data: bytes) -> tuple:
    """Decode a Report Value given Format and Length fields.

    Returns (decoded_value, type_string).
    """
    if len(data) < length:
        return None, 'truncated'

    val_bytes = data[:length]

    # Format 0x1: Unsigned integer
    if fmt == 0x1:
        return int.from_bytes(val_bytes, 'big', signed=False), 'uint'

    # Format 0x2: Signed integer (two's complement)
    if fmt == 0x2:
        return int.from_bytes(val_bytes, 'big', signed=True), 'int'

    # Format 0x3: Floating point
    if fmt == 0x3:
        if length == 2:  # FP2 format (Appendix 3)
            return decode_fp2(val_bytes), 'fp2'
        elif length == 4:  # IEEE 754 single
            return struct.unpack('>f', val_bytes)[0], 'float32'
        elif length == 8:  # IEEE 754 double
            return struct.unpack('>d', val_bytes)[0], 'float64'

    # Format 0x4: UTF-8 string
    if fmt == 0x4:
        try:
            return val_bytes.decode('utf-8', errors='replace'), 'utf8'
        except Exception:
            return val_bytes.hex(), 'utf8-error'

    # Format 0xD: 1-byte unsigned int, "seconds before transmission"
    if fmt == 0xD and length == 1:
        return val_bytes[0], 'sec_before_tx'

    # Format 0xE: 2-byte unsigned int, "seconds since 12:00 AM/PM UTC"
    if fmt == 0xE and length == 2:
        return int.from_bytes(val_bytes, 'big'), 'sec_since_12'

    # Format 0xF: 4-byte unsigned int, POSIX time
    if fmt == 0xF and length == 4:
        return int.from_bytes(val_bytes, 'big'), 'posix_time'

    # Unknown / reserved
    return val_bytes.hex(), f'fmt_0x{fmt:x}_len_{length}'


def decode_fp2(val_bytes: bytes) -> Optional[float]:
    """Decode FP2 floating point format (Appendix 3).

    1-bit sign, 2-bit negative-decimal exponent, 13-bit mantissa.
    """
    if len(val_bytes) != 2:
        return None
    raw = int.from_bytes(val_bytes, 'big')
    sign = (raw >> 15) & 0x1
    exp = (raw >> 13) & 0x3
    mant = raw & 0x1FFF

    # Special cases
    if exp == 0 and mant == 8191:
        return float('inf') if sign == 0 else float('-inf')
    if exp == 0 and mant == 8190 and sign == 1:
        return float('nan')

    # Normal case: value = (-1)^S * 10^-E * M
    value = ((-1) ** sign) * (10 ** -exp) * mant
    return value


def format_value_for_display(value: Any, type_str: str, sensor_id: int) -> str:
    """Format a decoded value for human-readable display."""
    if value is None:
        return '?'
    if isinstance(value, float):
        if value != value:  # NaN
            return 'NaN'
        return f'{value:g}'
    return str(value)


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class SensorReading:
    """A single sensor reading within a General Sensor Report."""
    sensor_id: int
    format_code: int
    length: int
    raw_bytes: bytes
    value: Any = None
    value_type: str = ''
    sensor_name: Optional[str] = None  # From recommended IDs
    is_timestamp: bool = False  # If sensor_id == 255

    def __str__(self) -> str:
        name = self.sensor_name or f'sensor_{self.sensor_id}'
        return f'{name}={format_value_for_display(self.value, self.value_type, self.sensor_id)}'


@dataclass
class GeneralSensorReport:
    """Type 1 — General Sensor Report (one or more readings)."""
    readings: List[SensorReading] = field(default_factory=list)

    def __str__(self) -> str:
        return 'GSR(' + ', '.join(str(r) for r in self.readings) + ')'


@dataclass
class TippingBucketReport:
    """Type 2 — Tipping Bucket Rain Gage Report."""
    sensor_id: int
    format_code: int
    accumulator: int
    accumulator_raw: bytes
    tip_offsets: List[int] = field(default_factory=list)

    def __str__(self) -> str:
        s = f'TBRG(sensor={self.sensor_id} accum={self.accumulator}'
        if self.tip_offsets:
            s += f' tips={self.tip_offsets}'
        s += ')'
        return s


@dataclass
class MultiSensorReport:
    """Type 3, 4, 5 — Multi-Sensor Report (fixed format)."""
    report_type: int  # 3, 4, or 5
    units: str  # 'english', 'metric', or 'ind'
    data_flags: int
    readings: List[SensorReading] = field(default_factory=list)

    def __str__(self) -> str:
        return f'MSR-{self.units}(' + ', '.join(str(r) for r in self.readings) + ')'


@dataclass
class TimeSeriesReport:
    """Type 7 — Time Series Data Report."""
    sensor_id: int
    interval_byte: int
    format_code: int
    length: int
    raw_data: bytes
    values: List[Any] = field(default_factory=list)
    sensor_name: Optional[str] = None

    @property
    def interval_value(self) -> int:
        return self.interval_byte & 0x3F

    @property
    def interval_units(self) -> str:
        units_bits = (self.interval_byte >> 6) & 0x3
        return {0: 'sec', 1: 'min', 2: 'hour', 3: 'day'}.get(units_bits, '?')

    def __str__(self) -> str:
        name = self.sensor_name or f'sensor_{self.sensor_id}'
        return (f'TSD({name}, every {self.interval_value}{self.interval_units}, '
                f'n={len(self.values)})')


@dataclass
class UnknownReport:
    """Any report type we don't decode in detail."""
    report_type: int
    raw_bytes: bytes

    def __str__(self) -> str:
        type_names = {250: 'SET', 251: 'GET'}
        name = type_names.get(self.report_type, f'Type{self.report_type}')
        return f'{name}({self.raw_bytes.hex()})'


@dataclass
class SrpPdu:
    """A complete Self-Reporting Protocol PDU."""
    version: int = 0
    has_timestamp: bool = False
    test_flag: bool = False
    pdu_id: int = 7  # 7 = disabled/default
    extensibility: bool = False
    timestamp: Optional[int] = None  # Seconds since most recent 12:00 UTC
    reports: List[Any] = field(default_factory=list)
    parse_errors: List[str] = field(default_factory=list)
    raw_bytes: bytes = b''

    def __str__(self) -> str:
        if not self.reports:
            return f'SRP(empty){"  TS=" + str(self.timestamp) if self.has_timestamp else ""}'
        parts = [str(r) for r in self.reports]
        prefix = 'SRP'
        if self.test_flag:
            prefix += '[TEST]'
        # PDU ID 7 means "disabled" per spec; only show 0-6
        if self.pdu_id != 7:
            prefix += f'[id={self.pdu_id}]'
        if self.has_timestamp:
            prefix += f'[ts={self.timestamp}]'
        return prefix + ': ' + '; '.join(parts)


# =============================================================================
# Length field parsing (extensible, per spec §2.1.4)
# =============================================================================

def parse_length(data: bytes, offset: int) -> tuple:
    """Parse the Report Length field. May be 1 or 2 bytes.

    Per spec §2.1.4 / Figure 2-3: if MSB of first byte is set, length is
    encoded in 2 bytes (low 7 bits of first + 8 bits of next).

    Returns (length, bytes_consumed).
    """
    if offset >= len(data):
        return None, 0
    first = data[offset]
    if first & 0x80:
        # Two-byte length
        if offset + 1 >= len(data):
            return None, 0
        length = ((first & 0x7F) << 8) | data[offset + 1]
        return length, 2
    else:
        return first, 1


# =============================================================================
# Report parsers
# =============================================================================

def parse_gsr(data: bytes, offset: int, length: int) -> tuple:
    """Parse a Type 1 General Sensor Report.

    Args:
        data: full SRP payload bytes
        offset: byte offset to start of report value (after T, L)
        length: report length in bytes

    Returns (GeneralSensorReport, bytes_consumed_in_this_report).
    """
    end = offset + length
    if end > len(data):
        end = len(data)

    rpt = GeneralSensorReport()
    cur = offset

    while cur < end:
        if cur + 2 > len(data):
            break
        sid = data[cur]
        fl = data[cur + 1]
        fmt = (fl >> 4) & 0xF
        sublen = fl & 0xF
        cur += 2

        if cur + sublen > len(data):
            break

        val_bytes = data[cur:cur + sublen]
        cur += sublen

        # Special: sensor_id 255 = embedded timestamp follows
        is_ts = (sid == 255)
        value, vtype = decode_value(fmt, sublen, val_bytes)

        reading = SensorReading(
            sensor_id=sid,
            format_code=fmt,
            length=sublen,
            raw_bytes=val_bytes,
            value=value,
            value_type=vtype,
            sensor_name=RECOMMENDED_SENSOR_NAMES.get(sid),
            is_timestamp=is_ts,
        )
        rpt.readings.append(reading)

    return rpt, end - offset


def parse_tbrg(data: bytes, offset: int, length: int) -> tuple:
    """Parse a Type 2 Tipping Bucket Rain Gage Report."""
    end = offset + length
    if end > len(data):
        end = len(data)

    if offset + 2 > len(data):
        return None, 0

    sid = data[offset]
    fl = data[offset + 1]
    fmt = (fl >> 4) & 0xF
    sublen = fl & 0xF
    cur = offset + 2

    if cur + sublen > len(data):
        return None, 0

    accum_bytes = data[cur:cur + sublen]
    accum, _ = decode_value(fmt, sublen, accum_bytes)
    cur += sublen

    # Remaining bytes in the report are 1-byte time offsets
    tips = list(data[cur:end])

    rpt = TippingBucketReport(
        sensor_id=sid,
        format_code=fl,
        accumulator=accum if isinstance(accum, int) else 0,
        accumulator_raw=accum_bytes,
        tip_offsets=tips,
    )
    return rpt, end - offset


# Multi-Sensor Report Type 3 (English units)
MSR_TYPE3_FIELDS = [
    # (name, sensor_id, bytes, format_code, scale, units)
    ('Air Temperature', 1, 2, 0x22, 0.1, 'F'),
    ('Relative Humidity', 2, 1, 0x11, 1, '%'),
    ('Barometric Pressure', 3, 2, 0x12, 0.1, 'hPa'),
    ('Wind Speed', 4, 1, 0x11, 1, 'mph'),
    ('Wind Direction', 5, 2, 0x12, 1, 'deg'),
    ('Peak Wind Speed', 6, 1, 0x11, 1, 'mph'),
    ('Stage', 7, 2, 0x22, 0.01, 'ft'),
    ('Battery Voltage', 8, 1, 0x11, 0.1, 'V'),
]

# Multi-Sensor Report Type 4 (Metric units)
MSR_TYPE4_FIELDS = [
    ('Air Temperature', 1, 2, 0x22, 0.1, 'C'),
    ('Relative Humidity', 2, 1, 0x11, 1, '%'),
    ('Barometric Pressure', 3, 2, 0x12, 0.1, 'hPa'),
    ('Wind Speed', 4, 2, 0x12, 1, 'km/h'),
    ('Wind Direction', 5, 2, 0x12, 1, 'deg'),
    ('Peak Wind Speed', 6, 2, 0x12, 1, 'km/h'),
    ('Stage', 7, 3, 0x23, 0.001, 'm'),
    ('Battery Voltage', 8, 1, 0x11, 0.1, 'V'),
]

# Multi-Sensor Report Type 5 (IND status)
# Per Application v1.3 spec §2.4.7 / Figure 2-12, MSR-IND measurement
# suite has the following field widths and formats:
MSR_TYPE5_FIELDS = [
    ('Clock Status', 201, 1, 0x11, 1, ''),
    ('Battery', 8, 1, 0x11, 0.1, 'V'),
    # IND Temp: 2 bytes UNSIGNED integer at 0.1°C resolution.
    # (Earlier versions of this decoder mistakenly used 1 byte signed,
    # which silently shifted all subsequent fields by 1 byte and made
    # Messages Received / Messages Sent counters appear incorrect.
    # Fixed 2026-05-27.)
    ('IND Temp', 202, 2, 0x12, 0.1, 'C'),
    ('Messages Received', 203, 2, 0x12, 1, ''),
    ('Messages Sent', 204, 2, 0x12, 1, ''),
    ('Status Bits', 205, 1, 0x11, 1, ''),
    ('Reserved6', 206, 1, 0x11, 1, ''),
    ('Reserved7', 207, 1, 0x11, 1, ''),
]


def parse_msr(data: bytes, offset: int, length: int,
              report_type: int) -> tuple:
    """Parse a Multi-Sensor Report (Type 3, 4, or 5)."""
    end = offset + length
    if end > len(data):
        end = len(data)
    if offset >= len(data):
        return None, 0

    fields_def = {3: MSR_TYPE3_FIELDS, 4: MSR_TYPE4_FIELDS,
                   5: MSR_TYPE5_FIELDS}.get(report_type)
    if fields_def is None:
        return None, 0

    units = {3: 'english', 4: 'metric', 5: 'ind'}[report_type]

    flags = data[offset]
    cur = offset + 1
    rpt = MultiSensorReport(report_type=report_type, units=units,
                              data_flags=flags)

    for bit_pos, (name, sid, sz, fmt_code, scale, unit) in enumerate(fields_def):
        if not ((flags >> bit_pos) & 1):
            continue
        if cur + sz > len(data):
            break
        val_bytes = data[cur:cur + sz]
        fmt = (fmt_code >> 4) & 0xF
        sublen = fmt_code & 0xF
        raw_value, vtype = decode_value(fmt, sublen, val_bytes)
        # Apply scaling
        scaled = raw_value
        if isinstance(raw_value, (int, float)) and scale != 1:
            scaled = raw_value * scale
        rpt.readings.append(SensorReading(
            sensor_id=sid,
            format_code=fmt_code,
            length=sz,
            raw_bytes=val_bytes,
            value=scaled,
            value_type=f'{vtype}_{unit}' if unit else vtype,
            sensor_name=name,
        ))
        cur += sz

    return rpt, end - offset


def parse_tsd(data: bytes, offset: int, length: int) -> tuple:
    """Parse a Type 7 Time Series Data Report."""
    end = offset + length
    if end > len(data):
        end = len(data)
    if offset + 3 > len(data):
        return None, 0

    sid = data[offset]
    interval = data[offset + 1]
    fl = data[offset + 2]
    fmt = (fl >> 4) & 0xF
    sublen = fl & 0xF
    cur = offset + 3

    rpt = TimeSeriesReport(
        sensor_id=sid,
        interval_byte=interval,
        format_code=fl,
        length=sublen,
        raw_data=data[cur:end],
        sensor_name=RECOMMENDED_SENSOR_NAMES.get(sid),
    )

    while cur + sublen <= end:
        val_bytes = data[cur:cur + sublen]
        value, _ = decode_value(fmt, sublen, val_bytes)
        rpt.values.append(value)
        cur += sublen

    return rpt, end - offset


# =============================================================================
# Top-level SRP PDU parser
# =============================================================================

def parse_srp_pdu(data: bytes) -> SrpPdu:
    """Parse a complete Self-Reporting Protocol PDU.

    Args:
        data: the application payload bytes (after MANT header).

    Returns SrpPdu with parsed reports.
    """
    pdu = SrpPdu(raw_bytes=data)

    if len(data) == 0:
        return pdu

    # Control byte
    cb = data[0]
    pdu.version = cb & 0x3
    pdu.has_timestamp = bool((cb >> 2) & 1)
    pdu.test_flag = bool((cb >> 3) & 1)
    pdu.pdu_id = (cb >> 4) & 0x7
    pdu.extensibility = bool((cb >> 7) & 1)
    offset = 1

    # Optional 16-bit timestamp
    if pdu.has_timestamp:
        if offset + 2 > len(data):
            pdu.parse_errors.append('Timestamp flag set but not enough bytes')
            return pdu
        pdu.timestamp = (data[offset] << 8) | data[offset + 1]
        offset += 2

    # Reports (TLV chain)
    while offset < len(data):
        # Skip 0x55 padding bytes (per AirLink spec, "pad bytes" of 0x55
        # are added to fill the first block to its fixed length)
        # If the rest of the data is all 0x55, treat as padding
        rest = data[offset:]
        if len(rest) > 0 and all(b == 0x55 for b in rest):
            break

        # Type byte
        if offset >= len(data):
            break
        report_type = data[offset]
        offset += 1

        # Length field (extensible)
        report_len, len_consumed = parse_length(data, offset)
        if report_len is None:
            pdu.parse_errors.append(f'Failed to parse length at offset {offset}')
            break
        offset += len_consumed

        # Bound check — if length exceeds remaining data, parse what we can
        # (this happens with truncated packets where follow-on blocks failed)
        truncated = False
        if offset + report_len > len(data):
            available = len(data) - offset
            pdu.parse_errors.append(
                f'Report type {report_type} length {report_len} exceeds remaining {available} bytes — parsing truncated')
            report_len = available
            truncated = True

        # Dispatch by type
        if report_type == 1:
            rpt, consumed = parse_gsr(data, offset, report_len)
        elif report_type == 2:
            rpt, consumed = parse_tbrg(data, offset, report_len)
        elif report_type in (3, 4, 5):
            rpt, consumed = parse_msr(data, offset, report_len, report_type)
        elif report_type == 7:
            rpt, consumed = parse_tsd(data, offset, report_len)
        else:
            # Unknown type — keep raw bytes
            rpt = UnknownReport(report_type=report_type,
                                  raw_bytes=data[offset:offset + report_len])
            consumed = report_len

        if rpt is not None:
            pdu.reports.append(rpt)
        offset += report_len
        if truncated:
            break

    return pdu


def srp_pdu_to_dict(pdu: SrpPdu) -> dict:
    """Convert SrpPdu to a JSON-serializable dict."""
    d = {
        'version': pdu.version,
        'has_timestamp': pdu.has_timestamp,
        'test_flag': pdu.test_flag,
        'pdu_id': pdu.pdu_id,
        'timestamp': pdu.timestamp,
        'reports': [],
    }
    if pdu.parse_errors:
        d['parse_errors'] = pdu.parse_errors

    for rpt in pdu.reports:
        if isinstance(rpt, GeneralSensorReport):
            d['reports'].append({
                'type': 'GSR',
                'readings': [
                    {'sensor_id': r.sensor_id,
                     'sensor_name': r.sensor_name,
                     'value': r.value,
                     'value_type': r.value_type,
                     'raw_hex': r.raw_bytes.hex()}
                    for r in rpt.readings
                ],
            })
        elif isinstance(rpt, TippingBucketReport):
            d['reports'].append({
                'type': 'TBRG',
                'sensor_id': rpt.sensor_id,
                'accumulator': rpt.accumulator,
                'tip_offsets_seconds': rpt.tip_offsets,
            })
        elif isinstance(rpt, MultiSensorReport):
            d['reports'].append({
                'type': f'MSR-{rpt.units}',
                'data_flags': rpt.data_flags,
                'readings': [
                    {'sensor_id': r.sensor_id, 'sensor_name': r.sensor_name,
                     'value': r.value, 'value_type': r.value_type}
                    for r in rpt.readings
                ],
            })
        elif isinstance(rpt, TimeSeriesReport):
            d['reports'].append({
                'type': 'TSD',
                'sensor_id': rpt.sensor_id,
                'sensor_name': rpt.sensor_name,
                'interval': rpt.interval_value,
                'interval_units': rpt.interval_units,
                'values': rpt.values,
            })
        elif isinstance(rpt, UnknownReport):
            d['reports'].append({
                'type': f'Type{rpt.report_type}',
                'raw_hex': rpt.raw_bytes.hex(),
            })
    return d
