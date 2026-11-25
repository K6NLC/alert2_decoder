# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.hub — merge the decoded streams of several receive sites.

Each decoder runs with `--site-name NAME --link HUB:PORT`. The hub
recognises the same air frame arriving from more than one site, picks the
best copy, and re-emits in the decoder's own UDP JSON format, so the
existing consumers (the Campbell forwarder, alert2-aprs, alert2-actions)
run once, on one stream.

    alert2-hub --config hub.ini --check
    alert2-hub --config hub.ini

Outputs
-------
Each [output NAME] is a UDP destination with a mode:

    merged    one copy per air frame, after a short hold so every site
              has a chance to deliver; complete beats truncated, then
              higher MER. Tagged with `site` (the winner) and `heard_by`.
    separate  every site's copy, immediately, tagged with its `site`.

Add Path (optional, per site, per output)
-----------------------------------------
A site may be given an ALERT2 source address. On an output with
`add_path = yes`, the hub then acts on the MANT header as a repeater
would under MANT spec 2.2.4: if and only if the frame's Add Path Service
Request bit is set, it increments the Number of Added Source Addresses
and appends the receiving site's address, so the base software can show
which receive point delivered the report. The hop limit is left alone
unless `decrement_hop = yes`. The datagram keeps the bytes as received in
`raw_payload_original_hex` and names the address in `add_path_applied`:
the modified header is never passed off as what was on the air.

Use only addresses assigned in the network's address plan.
"""
import argparse
import configparser
import json
import logging
import os
import selectors
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .link import decode_line
from .types import MantHeader

logger = logging.getLogger('alert2.hub')

MAX_ADDED_SAS = 6          # MANT header: 0-12 bytes of added addresses
MAX_LINE = 256 * 1024


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Site:
    name: str
    add_path: Optional[int] = None


@dataclass
class Output:
    name: str
    host: str
    port: int
    mode: str = 'merged'
    add_path: bool = False
    decrement_hop: bool = False


@dataclass
class Config:
    listen: str = '0.0.0.0:9990'
    hold: float = 2.0
    dedup_window: float = 30.0
    key: str = ''
    strict_sites: bool = False
    log_file: str = ''
    stats_file: str = ''
    stats_interval: float = 300.0
    sites: Dict[str, Site] = field(default_factory=dict)
    outputs: Dict[str, Output] = field(default_factory=dict)


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not cp.read(path):
        raise SystemExit(f"config not found: {path}")
    cfg = Config()
    if cp.has_section('hub'):
        h = cp['hub']
        cfg.listen = h.get('listen', cfg.listen)
        cfg.hold = h.getfloat('hold', cfg.hold)
        cfg.dedup_window = h.getfloat('dedup_window', cfg.dedup_window)
        cfg.key = h.get('key', '') or os.environ.get('ALERT2_LINK_KEY', '')
        cfg.strict_sites = h.getboolean('strict_sites', False)
        cfg.log_file = h.get('log_file', '')
        cfg.stats_file = h.get('stats_file', '')
        cfg.stats_interval = h.getfloat('stats_interval', cfg.stats_interval)
    seen_addr = {}
    for sec in cp.sections():
        if sec.startswith('site '):
            name = sec.split(None, 1)[1].strip()
            ap = cp[sec].get('add_path', '').strip()
            addr = None
            if ap:
                try:
                    addr = int(ap, 0)
                except ValueError:
                    raise SystemExit(f"[{sec}]: add_path must be a number")
                if not 1 <= addr <= 0xFFFE:
                    raise SystemExit(f"[{sec}]: add_path {addr} out of range")
                if addr in seen_addr:
                    raise SystemExit(f"[{sec}]: add_path {addr} already "
                                     f"used by site {seen_addr[addr]}")
                seen_addr[addr] = name
            cfg.sites[name] = Site(name, addr)
        elif sec.startswith('output '):
            name = sec.split(None, 1)[1].strip()
            o = cp[sec]
            to = o.get('to', '')
            try:
                host, port = to.rsplit(':', 1)
                port = int(port)
            except ValueError:
                raise SystemExit(f"[{sec}]: to = HOST:PORT is required")
            mode = o.get('mode', 'merged').lower()
            if mode not in ('merged', 'separate'):
                raise SystemExit(f"[{sec}]: mode must be merged or separate")
            cfg.outputs[name] = Output(
                name, host or '127.0.0.1', port, mode,
                o.getboolean('add_path', False),
                o.getboolean('decrement_hop', False))
    if not cfg.outputs:
        raise SystemExit("config has no [output ...] sections")
    if any(o.add_path for o in cfg.outputs.values()) and not any(
            s.add_path for s in cfg.sites.values()):
        print("note: an output has add_path = yes but no [site ...] has an "
              "add_path address; frames pass through unchanged until one "
              "is set", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# Add Path
# ---------------------------------------------------------------------------

def apply_add_path(pkt: dict, addr: int,
                   decrement_hop: bool = False) -> Tuple[dict, str]:
    """Return (datagram, note). The datagram is a modified copy when the
    address was appended, otherwise the one passed in. `note` says why
    not: '', 'no-apsr', 'full', 'echo', 'truncated', 'unparseable'."""
    if pkt.get('truncated'):
        return pkt, 'truncated'
    try:
        raw = bytes.fromhex(pkt.get('raw_payload_hex') or '')
        hdr = MantHeader.parse(raw)
    except Exception:
        return pkt, 'unparseable'
    if not hdr.add_path:
        return pkt, 'no-apsr'
    if addr in hdr.added_sas:
        return pkt, 'echo'
    if hdr.num_added_sas >= MAX_ADDED_SAS:
        return pkt, 'full'
    # count byte sits after the 6 fixed bytes, optional DA, optional PDU id
    cnt_off = 6 + (2 if hdr.da_present else 0) + (1 if hdr.proto_id == 1 else 0)
    end = hdr.header_bytes
    out = bytearray(raw[:end])
    out[cnt_off] = hdr.num_added_sas + 1
    out += addr.to_bytes(2, 'big')
    new_hop = hdr.hop_limit
    if decrement_hop and 1 <= hdr.hop_limit <= 6:
        new_hop = hdr.hop_limit - 1
        word = int.from_bytes(out[:6], 'big')
        word = (word & ~(0x7 << 28)) | (new_hop << 28)
        out[:6] = word.to_bytes(6, 'big')
    new_raw = bytes(out) + raw[end:]
    d = dict(pkt)
    d['raw_payload_original_hex'] = pkt['raw_payload_hex']
    d['raw_payload_hex'] = new_raw.hex()
    d['added_sas'] = list(hdr.added_sas) + [addr]
    d['hop_limit'] = new_hop
    d['add_path_applied'] = addr
    return d, ''


# ---------------------------------------------------------------------------
# Merge engine
# ---------------------------------------------------------------------------

def _quality(p: dict):
    mer = p.get('mer_db')
    return (0 if p.get('truncated') else 1,
            mer if isinstance(mer, (int, float)) else -99.0,
            -(p.get('rs_corrections') or 0))


class Hub:
    def __init__(self, cfg: Config, send=None, now=time.time):
        self.cfg = cfg
        self.now = now
        self._send = send or self._udp_send
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # key -> {'t0': arrival, 'copies': {site: pkt}}
        self.pending: Dict[tuple, dict] = {}
        # key -> (rx_time, arrival, set(sites)) for frames already merged
        self.recent: Dict[tuple, tuple] = {}
        self.stats = {'frames': 0, 'late': 0, 'rejected': 0,
                      'truncated_dropped': 0, 'add_path': {},
                      'sites': {}, 'combos': {}}
        self._log = open(cfg.log_file, 'a', encoding='utf-8') \
            if cfg.log_file else None

    # -- output ------------------------------------------------------------
    def _udp_send(self, out: Output, d: dict):
        try:
            self._sock.sendto(json.dumps(d).encode('utf-8'),
                              (out.host, out.port))
        except Exception as e:
            logger.warning(f"output {out.name}: {e}")

    def _emit(self, out: Output, pkt: dict, site: str):
        d = pkt
        s = self.cfg.sites.get(site)
        if out.add_path and s is not None and s.add_path is not None:
            d, note = apply_add_path(pkt, s.add_path, out.decrement_hop)
            k = note or 'applied'
            self.stats['add_path'][k] = self.stats['add_path'].get(k, 0) + 1
        self._send(out, d)

    def _site_stats(self, site: str) -> dict:
        return self.stats['sites'].setdefault(site, {
            'received': 0, 'complete': 0, 'truncated': 0, 'best': 0,
            'only': 0, 'mer_sum': 0.0, 'mer_n': 0, 'last': None})

    # -- input -------------------------------------------------------------
    def ingest(self, site: str, pkt: dict):
        if self.cfg.strict_sites and site not in self.cfg.sites:
            self.stats['rejected'] += 1
            logger.warning(f"packet from unconfigured site {site!r} dropped")
            return
        now = self.now()
        pkt = dict(pkt)
        pkt['site'] = site
        st = self._site_stats(site)
        st['received'] += 1
        st['last'] = now
        st['truncated' if pkt.get('truncated') else 'complete'] += 1
        mer = pkt.get('mer_db')
        if isinstance(mer, (int, float)):
            st['mer_sum'] += mer
            st['mer_n'] += 1
        for out in self.cfg.outputs.values():
            if out.mode == 'separate':
                self._emit(out, pkt, site)
        key = (pkt.get('fec_mode'), pkt.get('raw_payload_hex') or '')
        rx = float(pkt.get('rx_time_unix') or now)
        prev = self.recent.get(key)
        if prev is not None and abs(rx - prev[0]) <= self.cfg.dedup_window:
            # this frame was already merged and sent: a late copy
            if site not in prev[2]:
                self._credit_late(prev[2], site)
                self._log_line(f"LATE  {self._desc(pkt)} from {site} "
                               f"(+{now - prev[1]:.1f}s after merge)")
            return
        if pkt.get('truncated') and key[1]:
            # a truncated copy of a frame already merged complete
            for (fm, hx), v in self.recent.items():
                if (fm == key[0] and len(hx) > len(key[1])
                        and hx.startswith(key[1])
                        and abs(rx - v[0]) <= self.cfg.dedup_window):
                    if site not in v[2]:
                        self._credit_late(v[2], site)
                    return
        g = self.pending.get(key)
        if g is None:
            g = self.pending[key] = {'t0': now, 'copies': {}}
        cur = g['copies'].get(site)
        if cur is None or _quality(pkt) > _quality(cur):
            g['copies'][site] = pkt

    def _credit_late(self, sites: set, site: str):
        """A site delivered a frame after it was merged (slow link, spool
        replay): too late to compete, but it did hear it."""
        combos = self.stats['combos']
        old = '+'.join(sorted(sites))
        if combos.get(old, 0) > 0:
            combos[old] -= 1
            if not combos[old]:
                del combos[old]
            if len(sites) == 1:
                self._site_stats(next(iter(sites)))['only'] -= 1
        sites.add(site)
        new = '+'.join(sorted(sites))
        combos[new] = combos.get(new, 0) + 1
        self.stats['late'] += 1

    # -- merge -------------------------------------------------------------
    def flush(self, force: bool = False) -> int:
        now = self.now()
        due = [k for k, g in self.pending.items()
               if force or now - g['t0'] >= self.cfg.hold]
        if not due:
            return 0
        groups = [(k, self.pending.pop(k)) for k in due]
        # a truncated frame whose bytes are a prefix of a complete frame
        # flushed alongside it is the same air frame: fold it in
        complete = [(k, g) for k, g in groups
                    if not all(p.get('truncated')
                               for p in g['copies'].values())]
        n = 0
        for k, g in groups:
            if all(p.get('truncated') for p in g['copies'].values()):
                host = next((cg for ck, cg in complete
                             if ck[0] == k[0] and len(ck[1]) > len(k[1])
                             and ck[1].startswith(k[1])), None)
                if host is not None:
                    for site, p in g['copies'].items():
                        host['copies'].setdefault(site, p)
                    self.stats['truncated_dropped'] += 1
                    g['copies'] = {}
        for k, g in groups:
            if not g['copies']:
                continue
            self._merge_out(k, g, now)
            n += 1
        # forget old merged keys (spooled copies can arrive minutes late)
        for k in [k for k, v in self.recent.items() if now - v[1] > 900]:
            del self.recent[k]
        return n

    def _merge_out(self, key, g, now):
        copies = g['copies']
        best_site = max(copies, key=lambda s: _quality(copies[s]))
        best = dict(copies[best_site])
        best['heard_by'] = [
            {'site': s, 'mer_db': p.get('mer_db'),
             'truncated': bool(p.get('truncated')),
             'rs_corrections': p.get('rs_corrections'),
             'rx_time_unix': p.get('rx_time_unix')}
            for s, p in sorted(copies.items())]
        self.recent[(key[0], best.get('raw_payload_hex') or '')] = (
            float(best.get('rx_time_unix') or now), now, set(copies))
        self.recent[key] = self.recent[(key[0],
                                        best.get('raw_payload_hex') or '')]
        self.stats['frames'] += 1
        combo = '+'.join(sorted(copies))
        self.stats['combos'][combo] = self.stats['combos'].get(combo, 0) + 1
        self._site_stats(best_site)['best'] += 1
        if len(copies) == 1:
            self._site_stats(best_site)['only'] += 1
        for out in self.cfg.outputs.values():
            if out.mode == 'merged':
                self._emit(out, best, best_site)
        heard = ' '.join(
            f"{h['site']}={'T' if h['truncated'] else ''}"
            f"{h['mer_db']:.1f}" if isinstance(h['mer_db'], (int, float))
            else f"{h['site']}={'T' if h['truncated'] else ''}?"
            for h in best['heard_by'])
        self._log_line(f"FRAME {self._desc(best)} best={best_site} [{heard}]")

    @staticmethod
    def _desc(p: dict) -> str:
        path = ','.join(str(a) for a in (p.get('added_sas') or []))
        return (f"src={p.get('src')} path=[{path}]"
                f"{' TRUNC' if p.get('truncated') else ''}")

    def _log_line(self, text: str):
        line = time.strftime('%Y-%m-%d %H:%M:%S',
                             time.localtime(self.now())) + ' ' + text
        logger.info(text)
        if self._log:
            self._log.write(line + '\n')
            self._log.flush()

    # -- stats -------------------------------------------------------------
    def stats_text(self) -> str:
        s = self.stats
        n = max(1, s['frames'])
        lines = [f"hub: {s['frames']} frames merged, {s['late']} late "
                 f"copies, {s['truncated_dropped']} truncated folded, "
                 f"{s['rejected']} rejected"]
        for name, st in sorted(s['sites'].items()):
            mer = st['mer_sum'] / st['mer_n'] if st['mer_n'] else float('nan')
            heard = sum(c for combo, c in s['combos'].items()
                        if name in combo.split('+'))
            lines.append(
                f"  {name:<10} heard {heard}/{s['frames']} "
                f"({100.0 * heard / n:.2f}%)  best {st['best']}  "
                f"only-site {st['only']}  truncated {st['truncated']}  "
                f"MER avg {mer:.2f} dB")
        for combo, c in sorted(s['combos'].items(), key=lambda t: -t[1]):
            lines.append(f"  heard by {combo:<20} {c}")
        if s['add_path']:
            lines.append("  add-path: " + ', '.join(
                f"{k}={v}" for k, v in sorted(s['add_path'].items())))
        return '\n'.join(lines)

    def write_stats(self):
        if not self.cfg.stats_file:
            return
        try:
            tmp = self.cfg.stats_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=1)
            os.replace(tmp, self.cfg.stats_file)
        except Exception as e:
            logger.warning(f"stats file: {e}")

    def close(self):
        self.flush(force=True)
        if self._log:
            self._log.close()


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

def serve(cfg: Config, hub: Optional[Hub] = None, stop=None):
    hub = hub or Hub(cfg)
    host, _, port = cfg.listen.rpartition(':')
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host or '0.0.0.0', int(port)))
    srv.listen(16)
    srv.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(srv, selectors.EVENT_READ, None)
    logger.info(f"alert2-hub listening on {cfg.listen}: "
                f"{len(cfg.outputs)} output(s), hold {cfg.hold:g}s"
                f"{', signed links only' if cfg.key else ''}")
    conns: Dict[socket.socket, dict] = {}
    try:
        import signal

        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, ImportError):
        pass                      # not the main thread (tests)
    next_stats = time.time() + cfg.stats_interval
    try:
        while not (stop and stop()):
            for key, _ in sel.select(timeout=0.25):
                sock = key.fileobj
                if sock is srv:
                    c, addr = srv.accept()
                    c.setblocking(False)
                    conns[c] = {'buf': b'', 'addr': addr, 'site': None}
                    sel.register(c, selectors.EVENT_READ, 1)
                    logger.info(f"link from {addr[0]}:{addr[1]} connected")
                    continue
                st = conns[sock]
                try:
                    data = sock.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    data = b''
                if not data:
                    logger.info(f"link from {st['addr'][0]} "
                                f"(site {st['site'] or '?'}) closed")
                    sel.unregister(sock)
                    sock.close()
                    del conns[sock]
                    continue
                st['buf'] += data
                if len(st['buf']) > MAX_LINE and b'\n' not in st['buf']:
                    st['buf'] = b''
                    hub.stats['rejected'] += 1
                while b'\n' in st['buf']:
                    line, st['buf'] = st['buf'].split(b'\n', 1)
                    if not line.strip():
                        continue
                    try:
                        site, pkt = decode_line(line, cfg.key or None)
                    except ValueError as e:
                        hub.stats['rejected'] += 1
                        logger.warning(f"{st['addr'][0]}: {e}")
                        continue
                    if st['site'] != site:
                        st['site'] = site
                        logger.info(f"link from {st['addr'][0]} is site "
                                    f"{site!r}")
                    hub.ingest(site, pkt)
            hub.flush()
            if time.time() >= next_stats:
                next_stats = time.time() + cfg.stats_interval
                logger.info(hub.stats_text())
                hub.write_stats()
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
        logger.info(hub.stats_text())
        hub.write_stats()
        for c in list(conns):
            c.close()
        srv.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='alert2-hub',
        description='Merge decoded ALERT2 streams from several receive '
                    'sites (alert2-decode --site-name N --link HUB:PORT) '
                    'and re-emit one stream for the forwarder, alert2-aprs '
                    'and alert2-actions.')
    ap.add_argument('--config', required=True)
    ap.add_argument('--check', action='store_true',
                    help='validate the config, print sites/outputs, exit')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), 20),
                        format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(args.config)
    if args.check:
        print(f"listen {cfg.listen}  hold {cfg.hold:g}s  dedup_window "
              f"{cfg.dedup_window:g}s  key {'set' if cfg.key else 'none'}  "
              f"strict_sites {cfg.strict_sites}")
        for s in cfg.sites.values():
            print(f"[site {s.name}] add_path "
                  f"{s.add_path if s.add_path is not None else '-'}")
        for o in cfg.outputs.values():
            print(f"[output {o.name}] {o.host}:{o.port} {o.mode}"
                  f"{' add_path' if o.add_path else ''}"
                  f"{' decrement_hop' if o.decrement_hop else ''}")
        return 0
    serve(cfg)
    return 0


if __name__ == '__main__':
    sys.exit(main())
