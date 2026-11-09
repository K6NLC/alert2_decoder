# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.actions — config, rule state machine, block execution."""
import json
import os

import pytest

from alert2 import actions

CFG = """
[actions]
listen = 127.0.0.1:9997
state_file = {state}

[block high]
key = h
run = echo HIGH $ALERT2_VALUE >> {log}
run = echo second >> {log}

[block normal]
key = n
run = echo NORMAL $ALERT2_VALUE >> {log}

[block failing]
run = false
run = echo after-failure >> {log}

[block strict]
stop_on_error = yes
run = false
run = echo never >> {log}

[rule lagoon]
site = 769
sensor = 7
above = 12.0
below = 10.5
debounce = 2
on_enter = high
on_exit = normal

[rule lagoon_flood]
site = 769
above = 15
on_enter = high

[rule drawdown]
site = 787
direction = falling
under = 1250
over = 1255
on_enter = high
on_exit = normal

[rule silent]
site = 769
above = 1e9
stale = 0.01
on_stale = normal
"""


def _cfg(tmp_path, text=CFG):
    p = tmp_path / "actions.ini"
    p.write_text(text.format(state=tmp_path / "st.json", log=tmp_path / "log"))
    return actions.load_config(str(p)), tmp_path / "log"


def _dg(src=769, stage=0.0, sensor=7, truncated=False):
    return {'src': src, 'truncated': truncated,
            'srp': {'reports': [{'type': 'GSR', 'readings': [
                {'sensor_id': sensor, 'value': stage}]}]}}


class _Rec:
    """Runner stand-in that records block calls."""
    def __init__(self):
        self.calls = []
        self.dry = False

    def run_block(self, name, event):
        self.calls.append((name, event['event'], event.get('value')))
        return True


def test_config_blocks_keep_repeated_lines_in_order(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert [k for k, _ in cfg.blocks['high'].steps] == ['run', 'run']
    assert cfg.blocks['high'].steps[1][1].startswith('echo second')
    assert cfg.blocks['high'].key == 'h'
    assert cfg.blocks['strict'].stop_on_error
    r = cfg.rules['lagoon']
    assert (r.site, r.above, r.below, r.debounce) == (769, 12.0, 10.5, 2)
    assert cfg.rules['drawdown'].falling
    assert cfg.rules['silent'].stale == 0.01


def test_config_rejects_unknown_block_and_duplicate_key(tmp_path):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, CFG.replace("on_exit = normal", "on_exit = nope"))
    with pytest.raises(SystemExit):
        _cfg(tmp_path, CFG.replace("key = n", "key = h"))


def test_hysteresis_and_debounce(tmp_path):
    cfg, _ = _cfg(tmp_path)
    rec = _Rec()
    eng = actions.Engine(cfg, rec)
    assert eng.handle(_dg(stage=12.5)) == []          # 1/2
    assert eng.handle(_dg(stage=11.0)) == []          # drops below -> reset
    assert eng.handle(_dg(stage=12.5)) == []          # 1/2
    assert eng.handle(_dg(stage=13.0)) == ['high']    # 2/2 -> enter
    assert eng.state['lagoon']['active']
    assert eng.handle(_dg(stage=11.5)) == []          # between 10.5 and 12: hold
    assert eng.handle(_dg(stage=12.9)) == []          # still active, no re-fire
    assert eng.handle(_dg(stage=10.4)) == []          # 1/2 toward exit
    assert eng.handle(_dg(stage=10.0)) == ['normal']  # exit
    assert not eng.state['lagoon']['active']
    assert rec.calls == [('high', 'enter', 13.0), ('normal', 'exit', 10.0)]


def test_two_rules_one_site_and_truncated_ignored(tmp_path):
    cfg, _ = _cfg(tmp_path)
    rec = _Rec()
    eng = actions.Engine(cfg, rec)
    assert eng.handle(_dg(stage=16.0, truncated=True)) == []
    fired = eng.handle(_dg(stage=16.0))
    assert fired == ['high']                           # lagoon_flood (debounce 1)
    assert eng.state['lagoon']['count'] == 1           # lagoon at 1/2
    assert eng.handle(_dg(stage=16.0)) == ['high']     # lagoon enters too
    assert eng.state['lagoon']['active'] and eng.state['lagoon_flood']['active']
    # wrong sensor / wrong site: nothing
    assert eng.handle(_dg(stage=0.0, sensor=8)) == []
    assert eng.handle(_dg(src=1, stage=0.0)) == []


def test_falling_rule(tmp_path):
    cfg, _ = _cfg(tmp_path)
    rec = _Rec()
    eng = actions.Engine(cfg, rec)
    assert eng.handle(_dg(src=787, stage=1260.0)) == []
    assert eng.handle(_dg(src=787, stage=1249.9)) == ['high']
    assert eng.handle(_dg(src=787, stage=1252.0)) == []     # hysteresis band
    assert eng.handle(_dg(src=787, stage=1255.0)) == ['normal']


def test_state_persists_across_restart(tmp_path):
    cfg, _ = _cfg(tmp_path)
    eng = actions.Engine(cfg, _Rec())
    eng.handle(_dg(stage=16.0))
    eng.handle(_dg(stage=16.0))
    assert eng.state['lagoon']['active']
    rec2 = _Rec()
    eng2 = actions.Engine(cfg, rec2)                    # restart
    assert eng2.state['lagoon']['active']
    assert eng2.handle(_dg(stage=16.0)) == []           # still up: no re-fire
    assert eng2.handle(_dg(stage=9.0)) == []            # 1/2 (flood exits, no block)
    assert eng2.handle(_dg(stage=9.0)) == ['normal']    # 2/2 -> lagoon exits
    assert not eng2.state['lagoon']['active']


def test_stale_fires_once_until_next_report(tmp_path):
    import time
    cfg, _ = _cfg(tmp_path)
    rec = _Rec()
    eng = actions.Engine(cfg, rec)
    assert eng.check_stale() == []                      # never heard: no stale
    eng.handle(_dg(stage=1.0))
    time.sleep(0.02)
    assert eng.check_stale() == ['normal']
    assert eng.check_stale() == []                      # not repeated
    eng.handle(_dg(stage=1.0))                          # report -> re-armed
    time.sleep(0.02)
    assert eng.check_stale() == ['normal']
    assert rec.calls[-1][1] == 'stale'


def test_runner_executes_in_order_with_env(tmp_path):
    cfg, log = _cfg(tmp_path)
    r = actions.Runner(cfg)
    assert r.run_block('high', {'event': 'enter', 'value': 12.5, 'site': 769})
    assert log.read_text().splitlines() == ['HIGH 12.5', 'second']


def test_runner_failure_continues_unless_strict(tmp_path):
    cfg, log = _cfg(tmp_path)
    r = actions.Runner(cfg)
    assert r.run_block('failing', {'event': 'manual'}) is False
    assert 'after-failure' in log.read_text()
    log.write_text('')
    assert r.run_block('strict', {'event': 'manual'}) is False
    assert log.read_text() == ''
    assert r.run_block('nope', {'event': 'manual'}) is False


def test_dry_run_executes_nothing(tmp_path):
    cfg, log = _cfg(tmp_path)
    r = actions.Runner(cfg, dry_run=True)
    assert r.run_block('high', {'event': 'manual'})
    assert not log.exists()


def test_http_spec_parsing(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        def read(self, n): return b'ok'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_build_opener(*handlers):
        seen['handlers'] = handlers
        class _O:
            def open(self, req, timeout):
                seen['url'] = req.full_url
                seen['method'] = req.get_method()
                seen['body'] = req.data
                return _Resp()
        return _O()
    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', fake_build_opener)
    out = actions._http(
        'POST http://cam/rcp.xml?p=${ALERT2_VALUE} auth=u:p digest body=x=1',
        {'ALERT2_VALUE': 7}, 5)
    assert out.startswith('200')
    assert seen['url'] == 'http://cam/rcp.xml?p=7'
    assert seen['method'] == 'POST' and seen['body'] == b'x=1'
    assert any('Digest' in type(h).__name__ for h in seen['handlers'])


def test_fire_and_check_cli(tmp_path, capsys):
    cfg, log = _cfg(tmp_path)
    p = str(tmp_path / "actions.ini")
    assert actions.main(['--config', p, '--check']) == 0
    assert '[rule lagoon]' in capsys.readouterr().out
    assert actions.main(['--config', p, '--fire', 'normal']) == 0
    assert log.read_text().startswith('NORMAL')
    assert actions.main(['--config', p, '--fire', 'failing']) == 1


def test_insecure_adds_https_handler_and_redaction(monkeypatch):
    seen = {}
    import urllib.request

    class _Resp:
        status = 200
        def read(self, n): return b''
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_build_opener(*handlers):
        seen['handlers'] = handlers
        class _O:
            def open(self, req, timeout): return _Resp()
        return _O()
    monkeypatch.setattr(urllib.request, 'build_opener', fake_build_opener)
    actions._http('GET https://cam/x auth=u:secret digest insecure', {}, 5)
    names = [type(h).__name__ for h in seen['handlers']]
    assert 'HTTPSHandler' in names and 'HTTPDigestAuthHandler' in names
    assert actions._redact('GET https://cam/x auth=u:secret digest') == \
        'GET https://cam/x auth=u:*** digest'
    assert actions._redact('GET https://u:pw@cam/x') == 'GET https://u:***@cam/x'


def test_mail_step(tmp_path, monkeypatch):
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=None):
            sent['host'] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent['tls'] = True
        def login(self, u, p): sent['login'] = (u, p)
        def send_message(self, m): sent['msg'] = m
    import smtplib
    monkeypatch.setattr(smtplib, 'SMTP', _SMTP)
    monkeypatch.setenv('ALERT2_SMTP_PASSWORD', 'app-pass')
    text = CFG.replace("[actions]\n", "[actions]\nsmtp_host = smtp.example.com\n"
                       "smtp_user = me@example.com\n", 1).replace(
        "[block normal]", "[block mailme]\nmail = a@x.com,b@y.com "
        "\"S $ALERT2_RULE\" \"V=$ALERT2_VALUE\"\n\n[block normal]")
    cfg, _ = _cfg(tmp_path, text)
    r = actions.Runner(cfg)
    assert r.run_block('mailme', {'event': 'enter', 'rule': 'lagoon',
                                  'value': 12.5, 'site': 769, 'sensor': 7})
    assert sent['host'] == ('smtp.example.com', 587) and sent['tls']
    assert sent['login'] == ('me@example.com', 'app-pass')
    m = sent['msg']
    assert m['To'] == 'a@x.com, b@y.com' and m['Subject'] == 'S lagoon'
    assert m.get_content().strip() == 'V=12.5'


def test_mail_step_requires_smtp_host(tmp_path):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, CFG.replace("[block normal]",
                                   "[block m]\nmail = a@x.com\n\n[block normal]"))


def test_wait_step_and_duration_parsing(tmp_path):
    import time
    assert actions._parse_duration('5') == 5 and actions._parse_duration('2m') == 120
    assert actions._parse_duration('1.5h') == 5400 and actions._parse_duration('x') is None
    text = CFG.replace("[block normal]", "[block seq]\nrun = echo a >> {log}\n"
                       "wait = 0.3\nrun = echo b >> {log}\n\n[block normal]")
    cfg, log = _cfg(tmp_path, text)
    assert [k for k, _ in cfg.blocks['seq'].steps] == ['run', 'wait', 'run']
    t0 = time.time()
    assert actions.Runner(cfg).run_block('seq', {'event': 'manual'})
    assert time.time() - t0 >= 0.3
    assert log.read_text().split() == ['a', 'b']
    with pytest.raises(SystemExit):
        _cfg(tmp_path, text.replace("wait = 0.3", "wait = soon"))


def test_timers_interval_gating_and_immediate(tmp_path):
    text = CFG + """
[timer always]
every = 100
block = normal

[timer gated]
every = 100
block = high
while_active = lagoon
immediate = yes

[timer gated_lazy]
every = 100
block = high
while_active = lagoon
"""
    cfg, _ = _cfg(tmp_path, text)
    assert set(cfg.timers) == {'always', 'gated', 'gated_lazy'}
    rec = _Rec()
    eng = actions.Engine(cfg, rec)
    t = 1000.0
    eng.timer_due = {'always': t + 100, 'gated': t, 'gated_lazy': t + 100}
    assert eng.check_timers(t) == []            # gated: due but rule NORMAL -> held
    assert eng.check_timers(t + 100) == ['normal']   # always fires; gated_lazy ineligible -> rescheduled
    # rule goes ACTIVE
    eng.handle(_dg(stage=16.0)); eng.handle(_dg(stage=16.0))
    assert eng.state['lagoon']['active']
    assert eng.check_timers(t + 101) == ['high']     # gated (immediate) fires at once
    assert eng.check_timers(t + 150) == []
    assert eng.check_timers(t + 201) == ['normal', 'high', 'high']   # all three on their ticks
    ev = [c for c in rec.calls if c[1] == 'timer']
    assert ev and all(c[1] == 'timer' for c in ev)
    with pytest.raises(SystemExit):
        _cfg(tmp_path, text.replace("while_active = lagoon\nimmediate", "while_active = nope\nimmediate"))
    with pytest.raises(SystemExit):
        _cfg(tmp_path, CFG + "\n[timer bad]\nevery = 10\nblock = nope\n")
