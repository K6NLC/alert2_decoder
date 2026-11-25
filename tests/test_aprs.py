# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.aprs — config, packet builders, emitter behaviour."""
import json
import time

import pytest

from alert2 import aprs

CFG = """
[aprs]
server = rotate.aprs2.net:14580
login = N0CALL-10
passcode = 12345
listen = 127.0.0.1:9998
min_interval = 300
defs_every = 10
telemetry_format = counts

[site 735]
name = Arroyo Grande Ck
lat = 35.1213
lon = -120.5789
stage = 7
stage_range = 0,300
stage_units = ft

[site 720]
call = AGCRK
name = Test
lat = -44.656
lon = 170.9217
symbol = /_
stage = none
"""


def _cfg(tmp_path, text=CFG):
    p = tmp_path / "aprs.ini"
    p.write_text(text)
    return aprs.load_config(str(p))


def test_config_and_tactical_calls(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.login == 'N0CALL-10' and cfg.passcode == '12345'
    assert set(cfg.sites) == {735, 720}
    assert cfg.sites[735].call == 'AL2735'
    assert cfg.sites[720].call == 'AGCRK' and cfg.sites[720].stage is None
    assert cfg.sites[735].stage_range == (0.0, 300.0)


def test_config_rejects_bad_call(tmp_path):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, CFG.replace("call = AGCRK", "call = TOOLONGCALL"))


def test_passcode_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('APRS_PASSCODE', '999')
    cfg = _cfg(tmp_path, CFG.replace("passcode = 12345\n", ""))
    assert cfg.passcode == '999'


def test_position_formatting():
    assert aprs.aprs_lat(35.1213) == '3507.28N'
    assert aprs.aprs_lon(-120.5789) == '12034.73W'
    assert aprs.aprs_lat(-44.656) == '4439.36S'
    assert aprs.aprs_lon(170.9217) == '17055.30E'


def test_report_time_resolves_against_half_day_utc():
    # 2026-10-26 18:09:04 PDT = 2026-10-27 01:09:04 UTC; SRP ts 4115 s after
    # the most recent 12:00 AM/PM UTC (2026-10-27 00:00Z) -> 01:08:35Z
    import calendar
    rx = calendar.timegm(time.strptime('2026-10-27 01:09:04',
                                       '%Y-%m-%d %H:%M:%S'))
    t = aprs.report_time(rx, 4115)
    assert time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t)) == \
        '2026-10-27 01:08:35'
    # just after noon with a report stamped 'ahead' of a slow rx clock
    rx2 = rx - 3600 * 13 + 60          # 12:10:04Z on the 26th
    assert aprs.report_time(rx2, 700) == rx2 + 96      # 12:11:40Z
    assert aprs.report_time(rx, None) == rx


def _datagram(src=735, stage=212.3, batt=12.999, ts=19666, mer=10.4,
              path=(4604, 4601), truncated=False):
    readings = [{'sensor_id': 10, 'sensor_name': None, 'value': 0}]
    if stage is not None:
        readings.append({'sensor_id': 7, 'sensor_name': 'Stage',
                         'value': stage})
    if batt is not None:
        readings.append({'sensor_id': 8, 'sensor_name': 'Battery Voltage',
                         'value': batt})
    return {'rx_time_unix': 1761532144.0, 'src': src, 'added_sas': list(path),
            'truncated': truncated, 'mer_db': mer,
            'srp': {'has_timestamp': ts is not None, 'timestamp': ts,
                    'reports': [{'type': 'GSR', 'readings': readings}]}}


def test_build_packets_position_and_telemetry(tmp_path):
    cfg = _cfg(tmp_path)
    lines, used = aprs.build_packets(cfg, cfg.sites[735], _datagram(), 0,
                                     with_defs=True)
    assert used['stage'] == 212.3 and used['batt'] == 12.999
    pos, parm, unit, eqns, bits, tel = lines
    assert pos.startswith('AL2735>APZA2T,TCPIP*:/')
    assert '3507.28N/12034.73Ww' in pos
    assert pos.endswith('Ww Stage 212.30 ft Batt 13.0V via 4604,4601')
    assert parm == 'AL2735>APZA2T,TCPIP*::AL2735   :PARM.Stage,Batt,MER,Rain1h,Rain24h'
    assert unit.endswith(':UNIT.ft,V,dB,in,in')
    assert eqns.endswith(':EQNS.0,1.17647,0,0,0.1,0,0,0.1,0,0,0.01,0,0,0.1,0')
    assert bits.endswith(':BITS.00000000,Arroyo Grande Ck')
    # 212.3 ft over 0..300 -> 180; 12.999 V -> 130; 10.4 dB -> 104
    assert tel == 'AL2735>APZA2T,TCPIP*:T#000,180,130,104,000,000,00000000'


def test_build_packets_without_defs_and_clipping(tmp_path):
    cfg = _cfg(tmp_path)
    lines, _ = aprs.build_packets(cfg, cfg.sites[735],
                                  _datagram(stage=999.0, batt=None), 42,
                                  with_defs=False)
    assert len(lines) == 2
    assert lines[1].endswith('T#042,255,000,104,000,000,00000000')
    assert 'Batt' not in lines[0]


def test_build_packets_site_without_stage(tmp_path):
    cfg = _cfg(tmp_path)
    lines, used = aprs.build_packets(cfg, cfg.sites[720],
                                     _datagram(src=720, stage=None), 0, True)
    assert used['stage'] is None and used['batt'] == 12.999
    assert lines[0].startswith('AGCRK>APZA2T,TCPIP*:/')
    assert '4439.36S/17055.30E_' in lines[0]


class _FakeClient:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, line):
        self.sent.append(line)
        return self.ok


def test_emitter_dedups_relayed_copies_and_rate_limits(tmp_path):
    cfg = _cfg(tmp_path)
    cl = _FakeClient()
    em = aprs.Emitter(cfg, cl)
    assert len(em.handle(_datagram())) == 6          # defs on first frame
    # same report via another repeater path -> duplicate, not posted
    assert em.handle(_datagram(path=(4603, 4601))) == []
    assert em.stats['duplicate'] == 1
    # a new report inside min_interval -> rate limited
    assert em.handle(_datagram(ts=19666 + 600)) == []
    assert em.stats['rate_limited'] == 1
    # unknown source and truncated frames are ignored
    assert em.handle(_datagram(src=999)) == []
    assert em.handle(_datagram(truncated=True)) == []
    assert em.stats['unknown_src'] == 1
    assert em.seq[735] == 1 and em.stats['sent'] == 6


def test_emitter_sequence_and_defs_cadence(tmp_path):
    cfg = _cfg(tmp_path, CFG.replace("min_interval = 300", "min_interval = 0")
               .replace("defs_every = 10", "defs_every = 3"))
    cl = _FakeClient()
    em = aprs.Emitter(cfg, cl)
    counts = [len(em.handle(_datagram(ts=19666 + i))) for i in range(6)]
    assert counts == [6, 2, 2, 6, 2, 2]
    seqs = [ln.split('T#')[1][:3] for ln in cl.sent if 'T#' in ln]
    assert seqs == ['000', '001', '002', '003', '004', '005']


def test_emitter_failed_send_does_not_advance(tmp_path):
    cfg = _cfg(tmp_path)
    em = aprs.Emitter(cfg, _FakeClient(ok=False))
    em.handle(_datagram())
    assert em.seq.get(735, 0) == 0 and em.stats['failed'] == 6
    assert 735 not in em.last_report


def test_decoder_datagram_carries_srp(tmp_path):
    """The decoder side: packet_to_forwarder_dict must include the decoded
    SRP so this consumer never re-parses hex."""
    from alert2.output import packet_to_forwarder_dict
    from alert2.types import Alert2Packet, MantHeader
    from alert2.srp import parse_srp_pdu
    pdu = parse_srp_pdu(bytes.fromhex('041013011b0a1100083441538a760b3442a0ff2b0734410f82b0473400000000'))
    pkt = Alert2Packet(fec_mode=0, raw_payload=b'\x00' * 4, srp_pdu=pdu,
                       mant=MantHeader.parse(bytes.fromhex(
                           '0200002012070211fa11f9')))
    d = packet_to_forwarder_dict(pkt)
    assert d['src'] == 4615 and d['srp']['has_timestamp']
    assert d['srp']['timestamp'] == 4115
    ids = [r['sensor_id'] for rp in d['srp']['reports']
           for r in rp.get('readings', [])]
    assert 7 in ids and 8 in ids
    json.dumps(d)          # must serialise


def test_check_mode(tmp_path, capsys):
    p = tmp_path / "aprs.ini"
    p.write_text(CFG)
    assert aprs.main(['--config', str(p), '--check']) == 0
    out = capsys.readouterr().out
    assert 'AL2735' in out and 'AGCRK' in out


def test_telemetry_max_widens_resolution(tmp_path):
    cfg = _cfg(tmp_path, CFG.replace("defs_every = 10",
                                     "defs_every = 10\ntelemetry_max = 9999"))
    site = cfg.sites[735]
    site.stage_range = (1180.0, 1300.0)
    lines, _ = aprs.build_packets(cfg, site, _datagram(stage=1234.56), 0, True)
    tel = lines[-1]
    # (1234.56-1180)/120*9999 = 4546
    assert tel.endswith('T#000,4546,5097,4078,000,000,00000000')
    eq = [ln for ln in lines if 'EQNS.' in ln][0]
    assert eq.endswith(':EQNS.0,0.0120012,1180,0,0.00255026,0,0,0.00255026,0,'
                       '0,0.000255026,0,0,0.00255026,0')
    # reconstruct through the EQNS: within half a count
    assert abs(4546 * 0.0120012 + 1180 - 1234.56) < 0.012
    assert abs(5097 * 0.00255026 - 12.999) < 0.003


def test_default_resolution_and_clipping_at_255(tmp_path):
    cfg = _cfg(tmp_path)
    site = cfg.sites[735]
    site.stage_range = (0.0, 1300.0)
    lines, _ = aprs.build_packets(cfg, site, _datagram(stage=1234.56), 0, False)
    assert lines[-1].split(',')[2] == '242'       # 5.1 ft per count
    lines, _ = aprs.build_packets(cfg, site, _datagram(stage=1400.0), 0, False)
    assert lines[-1].split(',')[2] == '255'       # clipped, not wrapped


RAW = CFG.replace("telemetry_format = counts", "telemetry_format = raw")


def test_raw_telemetry_passes_decoder_values_verbatim(tmp_path):
    cfg = _cfg(tmp_path, RAW)
    lines, _ = aprs.build_packets(cfg, cfg.sites[735],
                                  _datagram(stage=9.11, batt=13.1, mer=10.04),
                                  73, with_defs=True)
    tel = lines[-1]
    assert tel == 'AL2735>APZA2T,TCPIP*:T#073,9.11,13.1,10.04,0,0,00000000'
    eq = [ln for ln in lines if 'EQNS.' in ln][0]
    assert eq.endswith(':EQNS.0,1,0,0,1,0,0,1,0,0,1,0,0,1,0')
    # large values and negatives are not clipped or scaled
    lines, _ = aprs.build_packets(cfg, cfg.sites[735],
                                  _datagram(stage=1301.25, batt=None), 1, False)
    assert lines[-1].endswith('T#001,1301.25,0,10.4,0,0,00000000')
    lines, _ = aprs.build_packets(cfg, cfg.sites[735],
                                  _datagram(stage=-0.004), 2, False)
    assert ',0,' in lines[-1].split('T#002')[1][:4]


def test_raw_is_the_default(tmp_path):
    cfg = _cfg(tmp_path, RAW.replace("telemetry_format = raw\n", ""))
    assert cfg.telemetry_format == 'raw'


def test_bad_telemetry_format_rejected(tmp_path):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, RAW.replace("telemetry_format = raw", "telemetry_format = hex"))
