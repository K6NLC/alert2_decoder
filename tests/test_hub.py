# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.hub and alert2.link — merge, add-path, wire format, end to end."""
import json
import socket
import threading
import time

import pytest

from alert2 import hub, link
from alert2.generator import build_mant_pdu
from alert2.types import MantHeader

CFG = """
[hub]
listen = 127.0.0.1:{port}
hold = 2
dedup_window = 30
{extra}

[site qth]
add_path = 4690

[site shop]
add_path = 4691

[site bare]

[output merged]
to = 127.0.0.1:1
mode = merged

[output sep]
to = 127.0.0.1:2
mode = separate

[output camp]
to = 127.0.0.1:3
mode = separate
add_path = yes
"""


def _cfg(tmp_path, port=0, extra=''):
    p = tmp_path / "hub.ini"
    p.write_text(CFG.format(port=port, extra=extra))
    return hub.load_config(str(p))


def _pdu(src=735, add_path=True, path=(4601,), payload=b'\x01\x02\x03\x04',
         dest=None, hop=3):
    raw = bytearray(build_mant_pdu(payload, src, dest_addr=dest,
                                   add_path=add_path, hop_limit=hop))
    if add_path and path:
        h = MantHeader.parse(bytes(raw))
        cnt = h.header_bytes - 1
        raw[cnt] = len(path)
        ins = b''.join(a.to_bytes(2, 'big') for a in path)
        raw = raw[:cnt + 1] + ins + raw[cnt + 1:]
    return bytes(raw)


def _pkt(raw=None, mer=10.0, truncated=False, rx=1000.0, rs=0, **kw):
    raw = raw if raw is not None else _pdu(**kw)
    h = MantHeader.parse(raw)
    return {'fec_mode': 0, 'raw_payload_hex': raw.hex(), 'mer_db': mer,
            'truncated': truncated, 'rx_time_unix': rx, 'rs_corrections': rs,
            'src': h.source_addr, 'added_sas': list(h.added_sas),
            'hop_limit': h.hop_limit}


class _H:
    def __init__(self, cfg):
        self.t = 1000.0
        self.sent = []
        self.hub = hub.Hub(cfg, send=lambda o, d: self.sent.append((o.name, d)),
                           now=lambda: self.t)

    def out(self, name):
        return [d for n, d in self.sent if n == name]


def test_config(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.sites['qth'].add_path == 4690 and cfg.sites['bare'].add_path is None
    assert cfg.outputs['camp'].add_path and cfg.outputs['sep'].mode == 'separate'
    p = tmp_path / "bad.ini"
    p.write_text(CFG.format(port=0, extra='').replace("4691", "4690"))
    with pytest.raises(SystemExit):
        hub.load_config(str(p))


def test_merge_picks_best_and_tags_heard_by(tmp_path):
    h = _H(_cfg(tmp_path))
    h.hub.ingest('qth', _pkt(mer=8.0))
    h.t += 0.5
    h.hub.ingest('shop', _pkt(mer=12.0, rx=1000.4))
    assert h.hub.flush() == 0 and h.out('merged') == []      # still holding
    assert len(h.out('sep')) == 2                             # immediate
    h.t += 2.0
    assert h.hub.flush() == 1
    (m,) = h.out('merged')
    assert m['site'] == 'shop'
    assert [x['site'] for x in m['heard_by']] == ['qth', 'shop']
    assert h.hub.stats['combos'] == {'qth+shop': 1}
    assert h.hub.stats['sites']['shop']['best'] == 1


def test_complete_beats_truncated_even_at_lower_mer(tmp_path):
    h = _H(_cfg(tmp_path))
    raw = _pdu(payload=bytes(range(40)))
    h.hub.ingest('qth', _pkt(raw=raw, mer=5.0))
    h.hub.ingest('shop', _pkt(raw=raw[:20], mer=14.0, truncated=True))
    h.t += 3
    assert h.hub.flush() == 1                 # prefix copy folded in
    (m,) = h.out('merged')
    assert m['site'] == 'qth' and not m['truncated']
    assert {x['site']: x['truncated'] for x in m['heard_by']} == \
        {'qth': False, 'shop': True}
    assert h.hub.stats['truncated_dropped'] == 1


def test_lone_truncated_frame_still_emitted(tmp_path):
    h = _H(_cfg(tmp_path))
    h.hub.ingest('qth', _pkt(truncated=True))
    h.t += 3
    h.hub.flush()
    assert len(h.out('merged')) == 1 and h.out('merged')[0]['truncated']


def test_late_copy_is_not_remerged_but_new_report_is(tmp_path):
    h = _H(_cfg(tmp_path))
    h.hub.ingest('qth', _pkt())
    h.t += 3
    h.hub.flush()
    h.t += 120                                # shop's link was down; spooled
    h.hub.ingest('shop', _pkt(rx=1000.6))
    h.hub.ingest('shop', _pkt(raw=_pdu()[:-2], truncated=True, rx=1000.6))
    h.t += 3
    assert h.hub.flush() == 0
    assert len(h.out('merged')) == 1 and h.hub.stats['late'] == 1
    assert h.hub.stats['combos'] == {'qth+shop': 1}       # credited late
    assert h.hub.stats['sites']['qth']['only'] == 0
    assert len(h.out('sep')) == 3             # separate still sees them
    # identical bytes 15 minutes later = a new report, not a duplicate
    h.hub.ingest('qth', _pkt(rx=1900.0))
    h.t += 3
    assert h.hub.flush() == 1


def test_different_repeater_paths_are_different_frames(tmp_path):
    h = _H(_cfg(tmp_path))
    h.hub.ingest('qth', _pkt(path=(4604, 4601)))
    h.hub.ingest('qth', _pkt(path=(4603, 4601)))
    h.t += 3
    assert h.hub.flush() == 2


def test_add_path_appends_site_address(tmp_path):
    raw = _pdu(src=735, path=(4604, 4601), dest=99)
    d, note = hub.apply_add_path(_pkt(raw=raw), 4690)
    assert note == ''
    new = bytes.fromhex(d['raw_payload_hex'])
    h0, h1 = MantHeader.parse(raw), MantHeader.parse(new)
    assert h1.added_sas == [4604, 4601, 4690] and h1.num_added_sas == 3
    assert h1.hop_limit == h0.hop_limit and h1.source_addr == 735
    assert h1.dest_addr == 99 and h1.payload_length == h0.payload_length
    assert new[h1.header_bytes:] == raw[h0.header_bytes:]     # payload untouched
    assert len(new) == len(raw) + 2
    assert d['raw_payload_original_hex'] == raw.hex()
    assert d['add_path_applied'] == 4690 and d['added_sas'][-1] == 4690
    d2, _ = hub.apply_add_path(_pkt(raw=raw, hop=3), 4690, decrement_hop=True)
    assert MantHeader.parse(bytes.fromhex(d2['raw_payload_hex'])).hop_limit == 2


def test_add_path_refusals_leave_frame_untouched(tmp_path):
    p = _pkt(add_path=False)
    assert hub.apply_add_path(p, 4690) == (p, 'no-apsr')
    p = _pkt(path=(4690, 4601))
    assert hub.apply_add_path(p, 4690) == (p, 'echo')
    p = _pkt(path=(1, 2, 3, 4, 5, 6))
    assert hub.apply_add_path(p, 4690) == (p, 'full')
    p = _pkt(truncated=True)
    assert hub.apply_add_path(p, 4690) == (p, 'truncated')
    p = dict(_pkt(), raw_payload_hex='zz')
    assert hub.apply_add_path(p, 4690)[1] == 'unparseable'
    # hop limit 7 (disabled) and 0 are never decremented
    for hop in (7, 0):
        d, _ = hub.apply_add_path(_pkt(hop=hop), 4690, decrement_hop=True)
        assert MantHeader.parse(bytes.fromhex(d['raw_payload_hex'])).hop_limit == hop


def test_outputs_get_each_sites_own_address(tmp_path):
    h = _H(_cfg(tmp_path))
    h.hub.ingest('qth', _pkt())
    h.hub.ingest('shop', _pkt())
    h.hub.ingest('bare', _pkt())
    camp = h.out('camp')
    assert [d.get('add_path_applied') for d in camp] == [4690, 4691, None]
    assert camp[0]['added_sas'] == [4601, 4690]
    assert all('add_path_applied' not in d for d in h.out('sep'))
    assert h.hub.stats['add_path'] == {'applied': 2}


def test_strict_sites(tmp_path):
    h = _H(_cfg(tmp_path, extra='strict_sites = yes'))
    h.hub.ingest('rogue', _pkt())
    assert h.sent == [] and h.hub.stats['rejected'] == 1


def test_link_signature():
    pkt = _pkt()
    line = link.encode_line('qth', pkt, 'k1')
    assert link.decode_line(line, 'k1') == ('qth', pkt)
    assert link.decode_line(line, None) == ('qth', pkt)
    with pytest.raises(ValueError):
        link.decode_line(line, 'other')
    with pytest.raises(ValueError):
        link.decode_line(link.encode_line('qth', pkt), 'k1')     # unsigned
    forged = line.replace(b'"site":"qth"', b'"site":"shop"')
    with pytest.raises(ValueError):
        link.decode_line(forged, 'k1')
    with pytest.raises(ValueError):
        link.decode_line(b'not json\n')


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_tcp_end_to_end_with_reconnect_spool(tmp_path):
    """LinkOutput -> hub over real TCP; hub starts late, so the first
    packets are spooled and delivered on connect."""
    from alert2.types import Alert2Packet
    port = _free_port()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0))
    rx.settimeout(5)
    text = CFG.format(port=port, extra='key = sekrit') \
        .replace("to = 127.0.0.1:1", f"to = 127.0.0.1:{rx.getsockname()[1]}") \
        .replace("hold = 2", "hold = 0.3")
    p = tmp_path / "hub.ini"
    p.write_text(text)
    cfg = hub.load_config(str(p))
    raw = _pdu()
    pk = Alert2Packet(fec_mode=0, raw_payload=raw, mant=MantHeader.parse(raw))
    pk.rx_time = time.time()
    a = link.LinkOutput('127.0.0.1', port, 'qth', 'sekrit')
    b = link.LinkOutput('127.0.0.1', port, 'shop', 'sekrit')
    bad = link.LinkOutput('127.0.0.1', port, 'rogue', 'wrong')
    a.emit(pk); b.emit(pk); bad.emit(pk)          # hub not up yet -> spooled
    time.sleep(0.3)
    stop = {'v': False}
    hb = hub.Hub(cfg)
    t = threading.Thread(target=hub.serve, args=(cfg, hb, lambda: stop['v']),
                         daemon=True)
    t.start()
    try:
        d = json.loads(rx.recvfrom(65535)[0])
    finally:
        stop['v'] = True
        t.join(5)
        for o in (a, b, bad):
            o.close()
    assert sorted(x['site'] for x in d['heard_by']) == ['qth', 'shop']
    assert d['src'] == 735
    assert hb.stats['rejected'] == 1 and hb.stats['frames'] == 1
