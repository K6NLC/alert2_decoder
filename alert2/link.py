# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Site-to-hub link: wire format and the decoder-side sender.

One JSON object per line over TCP:

    {"site": "qth", "pkt": {...forwarder dict...}, "sig": "<hex>"}

`sig` is present when a shared key is configured: HMAC-SHA256 over the
canonical JSON of {"site", "pkt"}. The hub rejects lines whose signature
is missing or wrong when it has a key. This authenticates the sender; it
does not encrypt, so run the link inside a VPN if the contents matter.
"""
import collections
import hashlib
import hmac
import json
import logging
import socket
import threading
import time
from typing import Optional

from .output import PacketOutput, packet_to_forwarder_dict

logger = logging.getLogger('alert2.link')


def canonical(site: str, pkt: dict) -> bytes:
    return json.dumps({'site': site, 'pkt': pkt}, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def sign(key: str, site: str, pkt: dict) -> str:
    return hmac.new(key.encode('utf-8'), canonical(site, pkt),
                    hashlib.sha256).hexdigest()


def encode_line(site: str, pkt: dict, key: Optional[str] = None) -> bytes:
    msg = {'site': site, 'pkt': pkt}
    if key:
        msg['sig'] = sign(key, site, pkt)
    return json.dumps(msg, separators=(',', ':')).encode('utf-8') + b'\n'


def decode_line(line: bytes, key: Optional[str] = None):
    """-> (site, pkt). Raises ValueError on malformed or unauthenticated."""
    try:
        msg = json.loads(line.decode('utf-8'))
        site = str(msg['site'])
        pkt = msg['pkt']
    except Exception as e:
        raise ValueError(f"malformed link line: {e}")
    if not isinstance(pkt, dict) or not site:
        raise ValueError("malformed link line")
    if key:
        got = msg.get('sig', '')
        if not hmac.compare_digest(str(got), sign(key, site, pkt)):
            raise ValueError(f"bad signature from site {site!r}")
    return site, pkt


class LinkOutput(PacketOutput):
    """Send every decoded packet to an alert2-hub over TCP.

    A background thread owns the socket, so the decoder never blocks on
    the network. While the hub is unreachable packets are spooled (newest
    `spool` kept, oldest dropped) and sent on reconnect; the hub
    recognises late copies by their receive time.
    """

    def __init__(self, host: str, port: int, site: str,
                 key: Optional[str] = None, spool: int = 5000):
        self.host, self.port, self.site, self.key = host, port, site, key
        self.q = collections.deque(maxlen=spool)
        self.cv = threading.Condition()
        self.stop = False
        self.sent = 0
        self.dropped = 0
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def emit(self, pkt) -> None:
        self.emit_dict(packet_to_forwarder_dict(pkt))

    def emit_dict(self, d: dict) -> None:
        """Send an already-built packet dict (used by alert2-indapi)."""
        d = dict(d)
        d['site'] = self.site
        with self.cv:
            if len(self.q) == self.q.maxlen:
                self.dropped += 1
            self.q.append(encode_line(self.site, d, self.key))
            self.cv.notify()

    def _run(self):
        sock = None
        backoff = 1.0
        while True:
            with self.cv:
                while not self.q and not self.stop:
                    self.cv.wait(1.0)
                if self.stop and not self.q:
                    break
                line = self.q[0]
            if sock is None:
                try:
                    sock = socket.create_connection((self.host, self.port),
                                                    timeout=10)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    logger.info(f"link to hub {self.host}:{self.port} up "
                                f"({len(self.q)} spooled)")
                    backoff = 1.0
                except Exception as e:
                    logger.warning(f"link to hub {self.host}:{self.port} "
                                   f"down: {e}; retry in {backoff:.0f}s")
                    if self.stop:
                        break
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            try:
                sock.sendall(line)
                with self.cv:
                    if self.q and self.q[0] is line:
                        self.q.popleft()
                self.sent += 1
            except Exception as e:
                logger.warning(f"link send failed: {e}; reconnecting")
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def close(self) -> None:
        with self.cv:
            self.stop = True
            self.cv.notify()
        self.t.join(timeout=5)
