# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.indapi — read an ALERT2 IND's ASCII output (Campbell AL205R,
Blue Water A2X) and re-emit it as this project's packet datagrams.

This lets a commercial receiver feed alert2-actions, alert2-aprs and
alert2-hub exactly as the software decoder does. In the hub it is simply
another site, so the scorecard compares the two receivers frame by frame.

    alert2-indapi --listen 0.0.0.0:9950 --site-name al205 --link HUB:9990
    alert2-indapi --connect 192.0.2.20:9950 --udp-emit 127.0.0.1:9997
    alert2-indapi --file SLO-Courthouse_20260830 --verify

The format (learned from 35 days of AL205R output, 118,424 N records)
----------------------------------------------------------------------
Comma-separated lines; a line holding only "." is a separator on the TCP
stream and is ignored. One received AirLink frame is:

    M,P,<Y,M,D,h,m,s.frac>,FEC=<mode>,NLVL=<noise>
    P,0,<time>,<n>,<size>,<size hex bytes>,<rs_corr>[,<size>,<bytes>,<rs_corr>...],<rx1>[,<rx2>...]
    M,N,<time>,ENC=<0|1>              } once per MANT PDU in the frame
    N,<time>,<10 header fields>,<len>,<src>,<n_added>,<added...>,<payload hex bytes>

P is the AirLink PDU as received: <n> is its payload length, then each
FEC block as a decimal size, that many hex bytes (the first block begins
with the 2-byte AirLink header) and the block's RS correction count.
N is one MANT PDU with its header already split into fields:

    version, protocol id, TSSR, APSR, DAI, port, encrypted, ack,
    added header, hop limit

The MANT header has an eleventh field (2 reserved bits) which the IND
does not print. Which of the two always-zero columns is "ack" and which
"added header" cannot be told from the data; both were 0 in every record.

Verified: rebuilding the MANT header bytes from the N fields and
concatenating a frame's PDUs reproduces the P record's bytes exactly
(see `--verify`). The reference logs cut P records off near 512
characters; for those frames every byte that is present matches, and the
N records, which this tool reads, are complete. NOT seen in the reference logs, so unverified: a
Destination Address (DAI=1), protocol id 1 (PDU id byte), encrypted
payloads, and the binary output format.

A block the IND could not correct shows -1 as its correction count; the
frame then has a P record and no N records (3 in 90,263 frames).

An N record with no P record and its own timestamp is the IND's own
report (its hourly status, under its own source address); it is passed
on marked `local_origin`. `S` records are the IND's periodic status line
and are counted, not used.
"""
import argparse
import calendar
import json
import logging
import socket
import sys
import time
from typing import Iterator, List, Optional

logger = logging.getLogger('alert2.indapi')


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _time(tok: List[str], utc_offset: float = 0.0) -> float:
    y, mo, d, h, mi = (int(x) for x in tok[:5])
    s = float(tok[5])
    return calendar.timegm((y, mo, d, h, mi, 0)) + s - utc_offset * 3600.0


def parse_p(tok: List[str]) -> dict:
    """P,<idx>,<6 time>,<n>,<size>,<bytes..>,<corr>,...,<trailer...>

    Long frames can arrive cut short: the reference logs cap this record
    near 512 characters. `complete` is False then, and `airlink_pdu`
    holds the bytes that were present."""
    n = int(tok[8])
    i = 9
    data = bytearray()
    corr = []
    got = 0
    complete = True
    # the first block is always 24 bytes, padded with 0x55 when the PDU
    # is shorter (AirLink spec 3.3)
    target = max(n + 2, 24)
    while got < target:
        if i >= len(tok):
            complete = False
            break
        try:
            size = int(tok[i])
        except ValueError:
            complete = False
            break
        i += 1
        raw = tok[i:i + size]
        try:
            blk = bytes(int(x, 16) for x in raw if len(x) == 2)
        except ValueError:
            blk = b''
        if len(blk) != size or i + size >= len(tok):
            data += blk[:len(raw)]
            complete = False
            break
        i += size
        data += blk
        got += size
        corr.append(int(tok[i])); i += 1
    if complete:
        data = data[:n + 2]
    return {'airlink_length': n, 'airlink_pdu': bytes(data),
            'rs_corrections': corr, 'trailer': tok[i:] if complete else [],
            'complete': complete}


def build_mant_header(f: dict) -> bytes:
    word = 0
    word |= (f['version'] & 0x3) << 46
    word |= (f['proto_id'] & 0x7) << 43
    word |= (f['tssr'] & 1) << 42
    word |= (f['apsr'] & 1) << 41
    word |= (f['dai'] & 1) << 40
    word |= (f['port'] & 0xF) << 36
    word |= (f['encrypted'] & 1) << 35
    word |= (f['ack'] & 1) << 32
    word |= (f['added_header'] & 1) << 31
    word |= (f['hop_limit'] & 0x7) << 28
    word |= (f['payload_length'] & 0xFFF) << 16
    word |= f['src'] & 0xFFFF
    hdr = word.to_bytes(6, 'big')
    if f['dai'] and f.get('dest') is not None:
        hdr += int(f['dest']).to_bytes(2, 'big')
    if f['proto_id'] == 1 and f.get('pdu_id') is not None:
        hdr += bytes([f['pdu_id']])
    if f['apsr']:
        hdr += bytes([len(f['added_sas'])])
        for a in f['added_sas']:
            hdr += a.to_bytes(2, 'big')
    return hdr


def parse_n(tok: List[str]) -> dict:
    """N,<6 time>,<10 fields>,<len>,<src>,<n_added>,<added..>,<payload..>"""
    v = [int(x) for x in tok[7:17]]
    f = dict(zip(('version', 'proto_id', 'tssr', 'apsr', 'dai', 'port',
                  'encrypted', 'ack', 'added_header', 'hop_limit'), v))
    i = 17
    f['payload_length'] = int(tok[i]); i += 1
    f['src'] = int(tok[i]); i += 1
    f['dest'] = None
    if f['dai']:
        # UNVERIFIED position: no DAI=1 record in the reference logs
        f['dest'] = int(tok[i]); i += 1
    f['added_sas'] = []
    if f['apsr']:
        n_added = int(tok[i]); i += 1
        f['added_sas'] = [int(x) for x in tok[i:i + n_added]]
        i += n_added
    payload = bytes(int(x, 16) for x in tok[i:])
    if len(payload) != f['payload_length']:
        raise ValueError(f"N record: {len(payload)} payload bytes, "
                         f"header says {f['payload_length']}")
    f['payload'] = payload
    f['raw'] = build_mant_header(f) + payload
    return f


class Parser:
    """Feed lines; get packet dicts in the decoder's --udp-emit format."""

    def __init__(self, utc_offset: float = 0.0, verify: bool = False):
        self.utc_offset = utc_offset
        self.verify = verify
        self.frame = None           # current M,P/P context
        self.enc = 0
        self.stats = {'frames': 0, 'pdus': 0, 'status': 0, 'bad_lines': 0,
                      'local': 0, 'verified': 0, 'verified_partial': 0,
                      'mismatch': 0, 'undecoded': 0,
                      'srp_parsed': 0}
        self._acc = b''             # PDUs of the current frame, for verify

    def _close_frame(self):
        fr = self.frame
        if fr and self.verify and fr.get('p'):
            p = fr['p']
            want = p['airlink_pdu'][2:]
            if any(c < 0 for c in p['rs_corrections']):
                # a block the IND could not correct (-1): it prints the P
                # record and no N records
                self.stats['undecoded'] += 1
            elif p['complete'] and self._acc == want:
                self.stats['verified'] += 1
            elif not p['complete'] and len(want) >= 8 \
                    and self._acc[:len(want)] == want:
                # P record cut short; everything it did carry matches
                self.stats['verified_partial'] += 1
            else:
                self.stats['mismatch'] += 1
                logger.debug(f"frame at {fr['time']}: N records do not "
                             f"reproduce the P record")
        self._acc = b''

    def feed(self, line: str) -> List[dict]:
        line = line.strip()
        if not line or line == '.':
            return []
        tok = line.split(',')
        try:
            if tok[0] == 'M' and tok[1] == 'P':
                self._close_frame()
                meta = dict(x.split('=', 1) for x in tok[8:] if '=' in x)
                self.frame = {'time': _time(tok[2:8], self.utc_offset),
                              'fec': int(meta.get('FEC', 0)),
                              'nlvl': float(meta['NLVL'])
                              if 'NLVL' in meta else None, 'p': None}
                self.stats['frames'] += 1
            elif tok[0] == 'P':
                if self.frame is not None:
                    self.frame['p'] = parse_p(tok)
            elif tok[0] == 'M' and tok[1] == 'N':
                meta = dict(x.split('=', 1) for x in tok[8:] if '=' in x)
                self.enc = int(meta.get('ENC', 0))
            elif tok[0] == 'N':
                f = parse_n(tok)
                t = _time(tok[1:7], self.utc_offset)
                # An N record whose time is not its frame's was not
                # received over the air: it is the IND's own report
                # (hourly status under its own source address).
                local = (self.frame is None
                         or abs(t - self.frame['time']) > 1e-4)
                d = self._packet(f, t, local)
                self.enc = 0
                if local:
                    self.stats['local'] += 1
                else:
                    self._acc += f['raw']
                self.stats['pdus'] += 1
                return [d]
            elif tok[0] == 'S':
                self.stats['status'] += 1
            else:
                self.stats['bad_lines'] += 1
        except Exception as e:
            self.stats['bad_lines'] += 1
            logger.debug(f"unparsed line ({e}): {line[:100]}")
        return []

    def finish(self):
        self._close_frame()

    def _packet(self, f: dict, t: float, local: bool = False) -> dict:
        fr = {} if local else (self.frame or {})
        p = fr.get('p') or {}
        d = {
            'rx_time_unix': t,
            'fec_mode': fr.get('fec', 0),
            'snr_db': None, 'noise_rms': None, 'signal_rms': None,
            'noise_level': fr.get('nlvl'),
            'rs_corrections': sum(p.get('rs_corrections', [])) if p else None,
            'framesync_hd': None,
            'truncated': False,
            'src': f['src'], 'dest': f['dest'],
            'added_sas': list(f['added_sas']),
            'port': f['port'], 'hop_limit': f['hop_limit'],
            'mant_payload_length': f['payload_length'],
            'raw_payload_hex': f['raw'].hex(),
            'application_payload_hex': f['payload'].hex(),
            'airlink_length': p.get('airlink_length'),
            'mer_db': None, 'carrier_hz': None,
            'encrypted': bool(self.enc or f['encrypted']),
            'receiver': 'ind-ascii',
            'local_origin': local,
            'srp': None,
        }
        if f['port'] == 0 and not d['encrypted']:
            try:
                from .srp import parse_srp_pdu, srp_pdu_to_dict
                d['srp'] = srp_pdu_to_dict(parse_srp_pdu(f['payload']))
                self.stats['srp_parsed'] += 1
            except Exception:
                d['srp'] = None
        return d


# ---------------------------------------------------------------------------
# Input sources
# ---------------------------------------------------------------------------

def _lines_from_socket(sock: socket.socket) -> Iterator[str]:
    buf = b''
    while True:
        data = sock.recv(65536)
        if not data:
            return
        buf += data
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            yield line.decode('ascii', 'replace')


def lines_listen(spec: str) -> Iterator[str]:
    host, _, port = spec.rpartition(':')
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host or '0.0.0.0', int(port)))
    srv.listen(2)
    logger.info(f"waiting for the IND to connect on {spec}")
    while True:
        c, addr = srv.accept()
        logger.info(f"IND connected from {addr[0]}:{addr[1]}")
        try:
            yield from _lines_from_socket(c)
        except Exception as e:
            logger.warning(f"IND connection error: {e}")
        finally:
            c.close()
        logger.info("IND disconnected; waiting for reconnect")


def lines_connect(spec: str) -> Iterator[str]:
    host, _, port = spec.rpartition(':')
    backoff = 1.0
    while True:
        try:
            s = socket.create_connection((host, int(port)), timeout=15)
            s.settimeout(None)
            logger.info(f"connected to IND at {spec}")
            backoff = 1.0
            yield from _lines_from_socket(s)
            logger.info("IND closed the connection")
        except Exception as e:
            logger.warning(f"IND at {spec}: {e}; retry in {backoff:.0f}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def lines_file(path: str, follow: bool = False) -> Iterator[str]:
    f = sys.stdin if path == '-' else open(path, 'r', encoding='utf-8', errors='replace')
    try:
        while True:
            line = f.readline()
            if line:
                yield line
            elif follow:
                time.sleep(0.5)
            else:
                return
    finally:
        if f is not sys.stdin:
            f.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='alert2-indapi',
        description="Read an ALERT2 IND's ASCII output (AL205R, A2X) and "
                    "feed alert2-actions / alert2-aprs / alert2-hub.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--listen', metavar='HOST:PORT',
                     help="TCP: wait for the IND to connect (its 'ASCII IP "
                          "Forward' host list points here)")
    src.add_argument('--connect', metavar='HOST:PORT',
                     help='TCP: connect to the IND or a terminal server')
    src.add_argument('--file', metavar='PATH',
                     help="read a log or a serial device node ('-' = stdin)")
    ap.add_argument('--follow', action='store_true',
                    help='with --file: keep reading as the file grows')
    ap.add_argument('--utc-offset', type=float, default=0.0, metavar='H',
                    help="hours the IND's clock is ahead of UTC (default 0: "
                         "the IND timestamps in UTC)")
    ap.add_argument('--udp-emit', metavar='HOST:PORT', action='append',
                    default=[], help='send packets here (repeatable)')
    ap.add_argument('--link', metavar='HOST:PORT', action='append',
                    default=[], help='send packets to an alert2-hub')
    ap.add_argument('--site-name', help='site name for --link')
    ap.add_argument('--link-key', default=None)
    ap.add_argument('--verify', action='store_true',
                    help='check every frame: the N records must rebuild the '
                         'P record byte for byte')
    ap.add_argument('--print', action='store_true', dest='show',
                    help='print one line per packet')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), 20),
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.link and not args.site_name:
        ap.error('--link needs --site-name')

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_dests = []
    for spec in args.udp_emit:
        h, _, p = spec.rpartition(':')
        udp_dests.append((h or '127.0.0.1', int(p)))
    links = []
    if args.link:
        import os
        from .link import LinkOutput
        key = args.link_key or os.environ.get('ALERT2_LINK_KEY') or None
        for spec in args.link:
            h, _, p = spec.rpartition(':')
            links.append(LinkOutput(h or '127.0.0.1', int(p),
                                    args.site_name, key))

    if args.listen:
        lines = lines_listen(args.listen)
    elif args.connect:
        lines = lines_connect(args.connect)
    else:
        lines = lines_file(args.file, args.follow)

    parser = Parser(args.utc_offset, args.verify)
    try:
        for line in lines:
            for d in parser.feed(line):
                if args.site_name:
                    d['site'] = args.site_name
                if udp_dests:
                    b = json.dumps(d).encode('utf-8')
                    for dest in udp_dests:
                        try:
                            udp.sendto(b, dest)
                        except Exception:
                            pass
                for ln in links:
                    ln.emit_dict(d)
                if args.show:
                    path = ','.join(str(a) for a in d['added_sas'])
                    rd = ''
                    for rp in (d['srp'] or {}).get('reports', []):
                        for r in rp.get('readings', []) or []:
                            rd += f" {r.get('sensor_id')}={r.get('value')}"
                    print(time.strftime('%Y-%m-%d %H:%M:%S',
                                        time.gmtime(d['rx_time_unix']))
                          + f"Z src={d['src']} path=[{path}] "
                            f"fec={d['fec_mode']} nlvl={d['noise_level']}"
                          + rd)
    except KeyboardInterrupt:
        pass
    finally:
        parser.finish()
        for ln in links:
            ln.close()
        logger.info(f"alert2-indapi: {parser.stats}")
    return 1 if parser.stats['mismatch'] else 0


if __name__ == '__main__':
    sys.exit(main())
