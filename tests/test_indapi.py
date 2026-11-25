# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.indapi — AL205R/A2X ASCII output parser. The sample lines are
verbatim from a real AL205R log."""
import calendar
import json
import socket
import threading
import time

from alert2 import indapi, aprs
from alert2.types import MantHeader

SINGLE = """S,11,2026,08,30,00,00,00,103,0,244,1,4,130,0,0,130,0
M,P,2026,08,30,00,00,10.092514,FEC=0,NLVL=38
.
P,0,2026,08,30,00,00,10.092514,22,24,00,16,02,00,10,0F,11,F9,00,74,00,07,05,0A,3F,00,85,00,F0,CA,9D,B8,BD,00,0,696
.
M,N,2026,08,30,00,00,10.092514,ENC=0
.
N,2026,08,30,00,00,10.092514,0,0,0,1,0,0,0,0,0,1,15,4601,0,74,00,07,05,0A,3F,00,85,00,F0,CA,9D,B8,BD,00
"""

BUNDLE = """M,P,2026,08,30,00,00,20.092405,FEC=0,NLVL=38
P,0,2026,08,30,00,00,20.092405,71,24,00,47,02,00,70,0F,11,FB,01,11,F9,74,00,0C,05,0A,3F,00,8A,00,AF,2E,99,63,0,32,38,00,02,00,00,26,02,E9,01,11,F9,04,00,09,01,21,0A,11,00,08,34,41,58,C2,66,0B,34,42,A7,B9,06,07,0,17,34,43,84,EB,0C,4D,34,41,63,C9,BA,47,34,00,00,00,00,0,678,1412
M,N,2026,08,30,00,00,20.092405,ENC=0
N,2026,08,30,00,00,20.092405,0,0,0,1,0,0,0,0,0,7,15,4603,1,4601,74,00,0C,05,0A,3F,00,8A,00,AF,2E,99,63,38,00
M,N,2026,08,30,00,00,20.092405,ENC=0
N,2026,08,30,00,00,20.092405,0,0,0,1,0,0,0,0,0,0,38,745,1,4601,04,00,09,01,21,0A,11,00,08,34,41,58,C2,66,0B,34,42,A7,B9,06,07,34,43,84,EB,0C,4D,34,41,63,C9,BA,47,34,00,00,00,00
"""

PADDED = """M,P,2026,08,30,00,08,40.092395,FEC=0,NLVL=38
P,0,2026,08,30,00,08,40.092395,17,24,00,11,02,00,00,08,02,DB,01,11,F9,34,01,FD,03,03,40,16,51,55,55,55,55,55,0,681
M,N,2026,08,30,00,08,40.092395,ENC=0
N,2026,08,30,00,08,40.092395,0,0,0,1,0,0,0,0,0,0,8,731,1,4601,34,01,FD,03,03,40,16,51
"""

FAILED = """M,P,2026,08,21,16,00,20.092785,FEC=0,NLVL=84
P,0,2026,08,21,16,00,20.092785,24,24,00,18,02,00,70,0F,11,FB,01,11,F9,74,38,4C,05,0A,3F,00,8A,00,C7,22,49,56,0,2,C0,00,-1,681,1001
"""

LOCAL = BUNDLE + "N,2026,08,30,00,00,53.007513,0,0,1,1,0,0,0,0,0,1,13,4600,0,70,05,0A,3F,03,8F,01,02,45,7B,1F,E1,00\n"


def _run(text, **kw):
    ps = indapi.Parser(verify=True, **kw)
    out = []
    for line in text.splitlines():
        out += ps.feed(line)
    ps.finish()
    return out, ps.stats


def test_single_frame():
    (d,), st = _run(SINGLE)
    assert st['verified'] == 1 and st['mismatch'] == 0 and st['status'] == 1
    assert d['src'] == 4601 and d['added_sas'] == [] and d['hop_limit'] == 1
    assert d['fec_mode'] == 0 and d['noise_level'] == 38.0
    assert d['rs_corrections'] == 0 and d['airlink_length'] == 22
    assert d['rx_time_unix'] == calendar.timegm((2026, 8, 30, 0, 0, 10)) + 0.092514
    raw = bytes.fromhex(d['raw_payload_hex'])
    assert raw.hex() == '020010 0f11f9 00'.replace(' ', '') + d['application_payload_hex']
    h = MantHeader.parse(raw)
    assert h.source_addr == 4601 and h.add_path and h.payload_length == 15
    assert not d['truncated'] and not d['local_origin']
    json.dumps(d)


def test_bundle_rebuilds_p_record_and_decodes_readings():
    out, st = _run(BUNDLE)
    assert st == {**st, 'frames': 1, 'pdus': 2, 'verified': 1, 'mismatch': 0}
    a, b = out
    assert (a['src'], a['added_sas'], a['hop_limit']) == (4603, [4601], 7)
    assert (b['src'], b['added_sas'], b['hop_limit']) == (745, [4601], 0)
    assert a['rs_corrections'] == 0 and b['airlink_length'] == 71
    ids = {r['sensor_id']: r['value'] for rp in b['srp']['reports']
           for r in rp.get('readings', [])}
    assert 7 in ids and 8 in ids and abs(ids[8] - 13.55) < 0.5
    assert b['srp']['has_timestamp']


def test_short_frame_padding_is_not_data():
    (d,), st = _run(PADDED)
    assert st['verified'] == 1 and d['mant_payload_length'] == 8
    assert not d['raw_payload_hex'].endswith('55')


def test_uncorrectable_frame_yields_nothing():
    out, st = _run(FAILED)
    assert out == [] and st['undecoded'] == 1 and st['mismatch'] == 0


def test_ind_own_report_is_marked_local():
    out, st = _run(LOCAL)
    assert st['local'] == 1 and st['verified'] == 1 and st['mismatch'] == 0
    d = out[-1]
    assert d['src'] == 4600 and d['local_origin'] and d['airlink_length'] is None
    assert MantHeader.parse(bytes.fromhex(d['raw_payload_hex'])).tssr


def test_tampered_n_record_is_caught():
    bad = SINGLE.replace(",15,4601,0,74,", ",15,4602,0,74,")
    _, st = _run(bad)
    assert st['mismatch'] == 1


def test_garbage_and_utc_offset():
    out, st = _run("hello\nN,2026\n" + SINGLE, utc_offset=-8)
    assert len(out) == 1 and st['bad_lines'] == 2
    assert out[0]['rx_time_unix'] == \
        calendar.timegm((2026, 8, 30, 8, 0, 10)) + 0.092514


def test_report_time_12_hour_rollover():
    """Regression: rc48 and earlier resolved the SRP timestamp from noon
    only, putting reports received 00:00-12:00 UTC twelve hours early."""
    rx = calendar.timegm((2026, 8, 30, 0, 0, 20))          # just after 00:00Z
    assert aprs.report_time(rx, 9) == rx - 11               # 00:00:09Z
    rx = calendar.timegm((2026, 8, 30, 12, 0, 5))
    assert aprs.report_time(rx, 43190) == rx - 15           # 11:59:50Z
    rx = calendar.timegm((2026, 8, 30, 23, 59, 0))
    assert aprs.report_time(rx, 43130) == rx - 10
    # every timestamped report in the samples lands within 5 min of rx
    out, _ = _run(BUNDLE)
    for d in out:
        dt = aprs.report_time(d['rx_time_unix'], d['srp']['timestamp']) \
            - d['rx_time_unix']
        assert -300 < dt <= 0


def test_tcp_listen_to_actions_style_udp(tmp_path):
    """IND connects in over TCP; packets come out as UDP datagrams."""
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0)); rx.settimeout(5)

    def run():
        gen = indapi.lines_listen(f'127.0.0.1:{port}')
        ps = indapi.Parser()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for line in gen:
            for d in ps.feed(line):
                tx.sendto(json.dumps(d).encode(), rx.getsockname())
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)
    c = socket.create_connection(('127.0.0.1', port))
    c.sendall(BUNDLE.replace('\n', '\r\n').encode())
    got = [json.loads(rx.recvfrom(65535)[0]) for _ in range(2)]
    c.close()
    assert [g['src'] for g in got] == [4603, 745]


def test_live_tcp_capture_stream():
    """The payload of a real AL205R -> base TCP session (reassembled from
    a packet capture): bare LF line ends, each sent as its own segment."""
    import os
    path = os.path.join(os.path.dirname(__file__), 'data_al205_tcp_stream.txt')
    raw = open(path, 'rb').read()
    assert b'\r' not in raw and b'\n.\n' not in raw
    out, st = _run(raw.decode('ascii'))
    assert st['frames'] == 4 and st['verified'] == 4 and st['bad_lines'] == 0
    assert [d['src'] for d in out] == [734, 739, 735, 4615]
    assert all(d['added_sas'] == [] and d['hop_limit'] == 2 for d in out)
    stage = {d['src']: next(r['value'] for rp in d['srp']['reports']
                            for r in rp.get('readings', []) if r['sensor_id'] == 7)
             for d in out}
    assert abs(stage[735] - 212.32) < 0.01
