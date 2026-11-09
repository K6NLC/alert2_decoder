# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.aprs — APRS-IS emitter for decoded ALERT2 reports.

A separate consumer. It listens for the decoder's --udp-emit JSON
datagrams, looks each source address up in a text config that supplies
what ALERT2 never carries (position, name, which sensor is stage), and
posts one position packet plus one telemetry frame per report to APRS-IS
under a tactical callsign per site, so aprs.fi shows every gauge on the
map and graphs its stage.

    alert2-decode --stdin ... --udp-emit 127.0.0.1:9998
    python3 -m alert2.aprs --config aprs.ini            # or alert2-aprs

Design points
-------------
* Tactical from-calls (AL2735, ...), one per site, originated through the
  operator's own login. Telemetry is attributed to the from-call, which is
  what makes aprs.fi graph each site separately. The login call never
  appears in the packets except in the q-construct the server adds.
* Position packet `/DDHHMMz...` carries the SRP report time, not receive
  time, so a relayed copy a few seconds later is the same report.
* Telemetry `T#sss,a1..a5,bits` with PARM/UNIT/EQNS/BITS definition
  messages sent at start and every `defs_every` frames. Analog channels
  are 0-255 by spec; the EQNS line maps them back to engineering units,
  so each site's config gives the stage range the 0-255 covers.
* Rain is deliberately not yet mapped: the ALERT2 reading is a cumulative
  tip counter, and APRS wants windowed totals, which needs history. The
  channels are reserved (Rain1h, Rain24h) so the definitions do not
  change when it lands.
* Per-site floor on posting rate (`min_interval`), dropped, not queued,
  while APRS-IS is unreachable; every line sent is logged; `--dry-run`
  prints the lines and connects to nothing.
"""
import argparse
import configparser
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('alert2.aprs')

DEFAULT_SERVER = 'rotate.aprs2.net:14580'
DEFAULT_TOCALL = 'APZA2T'          # experimental tocall: ALERT2
DEFAULT_LISTEN = '127.0.0.1:9998'
VERSION = '0.1'

# telemetry channel plan (fixed, so definitions never move)
CHANNELS = ('Stage', 'Batt', 'MER', 'Rain1h', 'Rain24h')


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Site:
    src: int
    call: str
    name: str
    lat: float
    lon: float
    symbol: str = '/w'                 # water/flood station
    stage: Optional[int] = 7           # sensor id
    stage_units: str = 'ft'
    stage_range: Tuple[float, float] = (0.0, 25.5)
    battery: Optional[int] = 8
    comment: str = ''


@dataclass
class Config:
    server: str = DEFAULT_SERVER
    login: str = ''
    passcode: str = ''
    tocall: str = DEFAULT_TOCALL
    listen: str = DEFAULT_LISTEN
    min_interval: float = 300.0
    defs_every: int = 10
    telemetry_max: int = 255       # counts mode only
    telemetry_format: str = 'raw'  # raw: decoder values verbatim; counts: 0..max + EQNS
    sites: Dict[int, Site] = field(default_factory=dict)


def _valid_call(c: str) -> bool:
    base, _, ssid = c.partition('-')
    if not (1 <= len(base) <= 6) or not base.isalnum() or not base.isupper():
        return False
    return (not ssid) or (ssid.isdigit() and 0 <= int(ssid) <= 15)


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not cp.read(path):
        raise SystemExit(f"config not found: {path}")
    cfg = Config()
    if cp.has_section('aprs'):
        a = cp['aprs']
        cfg.server = a.get('server', cfg.server)
        cfg.login = a.get('login', '').upper()
        cfg.passcode = a.get('passcode', '') or os.environ.get('APRS_PASSCODE', '')
        cfg.tocall = a.get('tocall', cfg.tocall).upper()
        cfg.listen = a.get('listen', cfg.listen)
        cfg.min_interval = a.getfloat('min_interval', cfg.min_interval)
        cfg.defs_every = a.getint('defs_every', cfg.defs_every)
        cfg.telemetry_max = a.getint('telemetry_max', cfg.telemetry_max)
        cfg.telemetry_format = a.get('telemetry_format', cfg.telemetry_format).lower()
        if cfg.telemetry_format not in ('raw', 'counts'):
            raise SystemExit("telemetry_format must be raw or counts")
        prefix = a.get('call_prefix', 'AL2').upper()
    else:
        prefix = 'AL2'
    for sec in cp.sections():
        if not sec.startswith('site'):
            continue
        s = cp[sec]
        src = s.getint('src', None)
        if src is None:
            try:
                src = int(sec.split()[1])
            except (IndexError, ValueError):
                raise SystemExit(f"[{sec}]: needs 'src = <id>' or a "
                                 f"'[site <id>]' header")
        call = s.get('call', f'{prefix}{src}').upper()
        if not _valid_call(call):
            raise SystemExit(f"[{sec}]: tactical call {call!r} is not a "
                             f"valid APRS callsign (<=6 alnum, optional "
                             f"-0..15); set call = ... explicitly")
        try:
            lat = s.getfloat('lat'); lon = s.getfloat('lon')
        except (TypeError, ValueError):
            raise SystemExit(f"[{sec}]: lat and lon are required")
        rng = s.get('stage_range', '0,25.5').split(',')
        site = Site(
            src=src, call=call, name=s.get('name', str(src)),
            lat=lat, lon=lon, symbol=s.get('symbol', '/w'),
            stage=s.getint('stage', 7) if s.get('stage', '7') != 'none' else None,
            stage_units=s.get('stage_units', 'ft'),
            stage_range=(float(rng[0]), float(rng[1])),
            battery=s.getint('battery', 8),
            comment=s.get('comment', ''),
        )
        if len(site.symbol) != 2:
            raise SystemExit(f"[{sec}]: symbol must be 2 chars (table+code)")
        cfg.sites[src] = site
    if not cfg.sites:
        raise SystemExit("config lists no [site ...] sections")
    return cfg


# ---------------------------------------------------------------------------
# Packet building
# ---------------------------------------------------------------------------

def aprs_lat(lat: float) -> str:
    h = 'N' if lat >= 0 else 'S'
    lat = abs(lat)
    d = int(lat)
    m = (lat - d) * 60
    return f'{d:02d}{m:05.2f}{h}'


def aprs_lon(lon: float) -> str:
    h = 'E' if lon >= 0 else 'W'
    lon = abs(lon)
    d = int(lon)
    m = (lon - d) * 60
    return f'{d:03d}{m:05.2f}{h}'


def report_time(rx_time: float, srp_ts: Optional[int]) -> float:
    """Absolute UTC time of an SRP report. The SRP timestamp is seconds
    since the most recent 12:00 UTC; resolve it against the receive time,
    allowing a report to be a little 'ahead' of a slow clock."""
    if srp_ts is None:
        return rx_time
    noon = (int(rx_time) // 86400) * 86400 + 43200
    if noon > rx_time:
        noon -= 86400
    t = noon + srp_ts
    if t > rx_time + 900:
        t -= 86400
    return t


def _dhm(t: float) -> str:
    return time.strftime('%d%H%Mz', time.gmtime(t))


def position_packet(cfg: Config, site: Site, t: float, comment: str) -> str:
    return (f'{site.call}>{cfg.tocall},TCPIP*:/{_dhm(t)}'
            f'{aprs_lat(site.lat)}{site.symbol[0]}{aprs_lon(site.lon)}'
            f'{site.symbol[1]} {comment}')


def _scale(v: Optional[float], lo: float, hi: float, mx: int = 255) -> int:
    """Engineering value -> integer channel count 0..mx (clipped)."""
    if v is None:
        return 0
    if hi <= lo:
        return 0
    x = (v - lo) / (hi - lo) * mx
    return int(round(min(mx, max(0, x))))


def _raw(v: Optional[float]) -> str:
    """Decoder value verbatim (2 dp, trailing zeros trimmed); 0 if absent."""
    if v is None:
        return '0'
    t = f'{v:.2f}'.rstrip('0').rstrip('.')
    return t if t not in ('', '-0') else '0'


def telemetry_packet(cfg: Config, site: Site, seq: int, stage, batt, mer,
                     rain1h=None, rain24h=None) -> str:
    """T# frame. 'raw' sends the engineering values as decimals (what the
    decoder produced, no scaling; aprs.fi parses these directly). 'counts'
    is the spec-pure 0..telemetry_max integer form with EQNS scaling."""
    head = f'{site.call}>{cfg.tocall},TCPIP*:T#{seq % 1000:03d},'
    if cfg.telemetry_format == 'raw':
        vals = [_raw(stage), _raw(batt), _raw(mer), _raw(rain1h),
                _raw(rain24h)]
        return head + ','.join(vals) + ',00000000'
    mx = cfg.telemetry_max
    a = [
        _scale(stage, *site.stage_range, mx),
        _scale(batt, 0.0, 25.5, mx),
        _scale(mer, 0.0, 25.5, mx),
        _scale(rain1h, 0.0, 2.55, mx),
        _scale(rain24h, 0.0, 25.5, mx),
    ]
    return head + ','.join(f'{x:03d}' for x in a) + ',00000000'


def definition_packets(cfg: Config, site: Site) -> List[str]:
    """PARM/UNIT/EQNS/BITS messages addressed to the site's own call."""
    to = f'{site.call:<9}'
    if cfg.telemetry_format == 'raw':
        eqns = [0, 1, 0] * 5          # identity: values are already units
    else:
        lo, hi = site.stage_range
        mx = cfg.telemetry_max
        k = 255.0 / mx                # slope correction for a wider channel
        eqns = [0, (hi - lo) / mx, lo,   # Stage
                0, 0.1 * k, 0,           # Batt    (0-25.5 V)
                0, 0.1 * k, 0,           # MER     (0-25.5 dB)
                0, 0.01 * k, 0,          # Rain1h  (0-2.55 in)
                0, 0.1 * k, 0]           # Rain24h (0-25.5 in)
    def fmt(x):
        return f'{x:.6g}'
    head = f'{site.call}>{cfg.tocall},TCPIP*::{to}:'
    return [
        head + 'PARM.' + ','.join(CHANNELS),
        head + f'UNIT.{site.stage_units},V,dB,in,in',
        head + 'EQNS.' + ','.join(fmt(x) for x in eqns),
        head + f'BITS.00000000,{site.name[:23]}',
    ]


def _find_reading(srp: dict, sensor_id: Optional[int]):
    """First numeric reading with this sensor id anywhere in the PDU."""
    if not srp or sensor_id is None:
        return None
    for rpt in srp.get('reports', []):
        for r in rpt.get('readings', []) or []:
            if r.get('sensor_id') == sensor_id:
                v = r.get('value')
                if isinstance(v, (int, float)):
                    return float(v)
        if rpt.get('type') == 'TBRG' and rpt.get('sensor_id') == sensor_id:
            return float(rpt.get('accumulator', 0))
    return None


def build_packets(cfg: Config, site: Site, d: dict, seq: int,
                  with_defs: bool) -> Tuple[List[str], dict]:
    """APRS lines for one decoder datagram, plus the readings used."""
    srp = d.get('srp') or {}
    stage = _find_reading(srp, site.stage)
    batt = _find_reading(srp, site.battery)
    mer = d.get('mer_db')
    t = report_time(float(d.get('rx_time_unix') or time.time()),
                    srp.get('timestamp') if srp.get('has_timestamp') else None)
    parts = []
    if stage is not None:
        parts.append(f'Stage {stage:.2f} {site.stage_units}')
    if batt is not None:
        parts.append(f'Batt {batt:.1f}V')
    path = d.get('added_sas') or []
    if path:
        parts.append('via ' + ','.join(str(p) for p in path))
    if site.comment:
        parts.append(site.comment)
    comment = ' '.join(parts) or site.name
    lines = [position_packet(cfg, site, t, comment)]
    if stage is not None or batt is not None:
        if with_defs:
            lines += definition_packets(cfg, site)
        lines.append(telemetry_packet(cfg, site, seq, stage, batt, mer))
    return lines, {'stage': stage, 'batt': batt, 'mer': mer, 'time': t}


# ---------------------------------------------------------------------------
# APRS-IS client
# ---------------------------------------------------------------------------

class AprsIs:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry = dry_run
        self.sock: Optional[socket.socket] = None
        self._next_try = 0.0
        self._backoff = 5.0

    def _connect(self):
        host, _, port = self.cfg.server.partition(':')
        s = socket.create_connection((host, int(port or 14580)), timeout=15)
        s.settimeout(5)
        login = (f'user {self.cfg.login} pass {self.cfg.passcode} '
                 f'vers alert2-aprs {VERSION}\r\n')
        s.sendall(login.encode('ascii'))
        banner = b''
        try:
            for _ in range(4):
                chunk = s.recv(1024)
                if not chunk:
                    break
                banner += chunk
                if b'logresp' in banner:
                    break
        except socket.timeout:
            pass
        text = banner.decode('ascii', 'replace').strip()
        logger.info(f"APRS-IS {self.cfg.server}: {text.replace(chr(10), ' | ')}")
        if 'unverified' in text.lower():
            logger.error("APRS-IS login UNVERIFIED: passcode wrong; packets "
                         "will be dropped by the server")
        s.setblocking(False)
        self.sock = s
        self._backoff = 5.0

    def send(self, line: str) -> bool:
        if self.dry:
            print(line)
            return True
        now = time.time()
        if self.sock is None:
            if now < self._next_try:
                return False
            try:
                self._connect()
            except Exception as e:
                logger.warning(f"APRS-IS connect failed: {e}; retry in "
                               f"{self._backoff:.0f}s")
                self._next_try = now + self._backoff
                self._backoff = min(self._backoff * 2, 300)
                return False
        try:
            # drain anything the server sent (keepalive comments)
            try:
                while self.sock.recv(4096):
                    pass
            except (BlockingIOError, socket.timeout):
                pass
            self.sock.sendall((line + '\r\n').encode('ascii', 'replace'))
            logger.info(f"TX {line}")
            return True
        except Exception as e:
            logger.warning(f"APRS-IS send failed: {e}; reconnecting")
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self._next_try = now + self._backoff
            return False

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
# Emitter loop
# ---------------------------------------------------------------------------

class Emitter:
    def __init__(self, cfg: Config, client: AprsIs):
        self.cfg = cfg
        self.client = client
        self.seq: Dict[int, int] = {}
        self.last_sent: Dict[int, float] = {}
        self.last_report: Dict[int, float] = {}
        self.stats = {'datagrams': 0, 'unknown_src': 0, 'rate_limited': 0,
                      'duplicate': 0, 'sent': 0, 'failed': 0}

    def handle(self, d: dict) -> List[str]:
        self.stats['datagrams'] += 1
        src = d.get('src')
        site = self.cfg.sites.get(src) if src is not None else None
        if site is None:
            self.stats['unknown_src'] += 1
            return []
        if d.get('truncated'):
            return []
        srp = d.get('srp') or {}
        t_rep = report_time(float(d.get('rx_time_unix') or time.time()),
                            srp.get('timestamp') if srp.get('has_timestamp')
                            else None)
        if self.last_report.get(src) == t_rep:
            self.stats['duplicate'] += 1          # relayed copy
            return []
        now = time.time()
        if now - self.last_sent.get(src, 0) < self.cfg.min_interval:
            self.stats['rate_limited'] += 1
            return []
        seq = self.seq.get(src, 0)
        with_defs = (seq % max(1, self.cfg.defs_every) == 0)
        lines, used = build_packets(self.cfg, site, d, seq, with_defs)
        if used['stage'] is None and used['batt'] is None \
                and site.stage is not None:
            return []           # a report without the data we map
        ok = all(self.client.send(ln) for ln in lines)
        if ok:
            self.seq[src] = seq + 1
            self.last_sent[src] = now
            self.last_report[src] = t_rep
            self.stats['sent'] += len(lines)
        else:
            self.stats['failed'] += len(lines)
        return lines


def serve(cfg: Config, dry_run: bool = False):
    client = AprsIs(cfg, dry_run)
    em = Emitter(cfg, client)
    host, _, port = cfg.listen.partition(':')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    logger.info(f"alert2-aprs listening on {cfg.listen}, "
                f"{len(cfg.sites)} site(s), login {cfg.login}, "
                f"{'DRY RUN' if dry_run else cfg.server}")
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            try:
                d = json.loads(data.decode('utf-8'))
            except Exception:
                continue
            em.handle(d)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        logger.info(f"alert2-aprs stats: {em.stats}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='alert2-aprs',
        description='Post decoded ALERT2 reports to APRS-IS (aprs.fi) as '
                    'tactical-call stations with stage telemetry. Feed it '
                    'with alert2-decode --udp-emit HOST:PORT.')
    ap.add_argument('--config', help='INI file (see aprs.ini.example, docs/APRS.md)')
    ap.add_argument('--dry-run', action='store_true',
                    help='print APRS lines instead of connecting')
    ap.add_argument('--check', action='store_true',
                    help='validate the config, print the sites, exit')
    ap.add_argument('--log-level', default='INFO')
    ap.add_argument('--write-example', metavar='PATH',
                    help='write a commented example config to PATH and exit')
    args = ap.parse_args(argv)
    if args.write_example:
        import importlib.resources as ir
        text = ir.files('alert2').joinpath('aprs.ini.example').read_text()
        with open(args.write_example, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"wrote {args.write_example}")
        return 0
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), 20),
                        format='%(asctime)s %(levelname)s %(message)s')
    if not args.config:
        ap.error("--config is required (or --write-example PATH to start one)")
    cfg = load_config(args.config)
    if args.check:
        print(f"server {cfg.server}  login {cfg.login}  tocall {cfg.tocall}"
              f"  listen {cfg.listen}  min_interval {cfg.min_interval:.0f}s")
        for src, s in sorted(cfg.sites.items()):
            print(f"  {src:5d} -> {s.call:<9} {s.name:<24} "
                  f"{aprs_lat(s.lat)} {aprs_lon(s.lon)} {s.symbol} "
                  f"stage=sensor {s.stage} range {s.stage_range} "
                  f"{s.stage_units}")
        return 0
    if not cfg.login or (not cfg.passcode and not args.dry_run):
        raise SystemExit("login and passcode are required (passcode may "
                         "come from the APRS_PASSCODE environment variable)")
    serve(cfg, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
