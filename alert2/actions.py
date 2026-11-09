# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""alert2.actions — run things when a gauge crosses a threshold.

Another small consumer of the decoder's --udp-emit datagrams, like
alert2.aprs. A text config declares *blocks* (named lists of commands or
HTTP requests) and *rules* (site + sensor + thresholds that fire blocks).
A console gives every block a key for manual firing.

    alert2-decode --stdin ... --udp-emit 127.0.0.1:9997
    alert2-actions --config actions.ini            # rules + console
    alert2-actions --config actions.ini --fire cam_high   # one block, then exit

Rule semantics
--------------
Each rule is a two-state machine, NORMAL <-> ACTIVE, on one sensor of
one site, with hysteresis and debounce:

    above    = 12.0     enter ACTIVE once the value is >= above
    below    = 10.5     return to NORMAL once the value is <= below
    debounce = 2        consecutive qualifying reports before either change
    on_enter = cam_high, notify
    on_exit  = cam_normal

`below` defaults to `above` (no hysteresis); debounce defaults to 1.
`direction = falling` flips the sense (enter when <= above... written as
`under` / `over` for readability). Several rules may name the same site,
and a block may be named by any number of rules. Rule state is persisted
to `state_file`, so a restart does not re-fire on_enter for a river that
is still up, and does not forget one that is.

Timers
------
    [timer ir_snapshot]
    every        = 10m
    block        = ir_still
    while_active = lagoon_high   ; only while that rule is ACTIVE
    immediate    = yes           ; also run the moment it becomes eligible

Blocks
------
    [block cam_high]
    key  = h                              ; console hotkey
    run  = /usr/local/bin/cam-profile.sh high   ; shell, in order
    http = GET http://10.0.0.5/rcp.xml?command=...  auth=user:pass digest
    wait = 3                              ; seconds (or 2m, 1h)
    run  = logger "lagoon high"

Commands run in order on a worker thread (so a wait holds only that
block); a failure is logged and the block continues
(`stop_on_error = yes` to abort). Every command sees the event in its
environment: ALERT2_RULE, ALERT2_SITE, ALERT2_SENSOR, ALERT2_VALUE,
ALERT2_EVENT (enter|exit|manual), ALERT2_TIME. `http` lines support
basic and digest auth, which is what Bosch cameras use for RCP+ over
HTTP (`/rcp.xml?...`). `mail` lines send email through the SMTP server
configured in [actions].
"""
import argparse
import configparser
import json
import logging
import os
import select
import shlex
import socket
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger('alert2.actions')

DEFAULT_LISTEN = '127.0.0.1:9997'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Block:
    name: str
    steps: List[tuple] = field(default_factory=list)   # ('run', cmd) | ('http', spec)
    key: Optional[str] = None
    stop_on_error: bool = False
    timeout: float = 60.0


@dataclass
class Rule:
    name: str
    site: int
    sensor: int = 7
    above: float = 0.0
    below: Optional[float] = None
    debounce: int = 1
    falling: bool = False
    on_enter: List[str] = field(default_factory=list)
    on_exit: List[str] = field(default_factory=list)
    stale: Optional[float] = None          # s without a report -> on_stale
    on_stale: List[str] = field(default_factory=list)


@dataclass
class Timer:
    name: str
    every: float                       # seconds
    block: str
    while_active: Optional[str] = None # rule name: run only while ACTIVE
    while_normal: Optional[str] = None # rule name: run only while NORMAL
    immediate: bool = False            # first run at start / on becoming eligible


@dataclass
class Config:
    listen: str = DEFAULT_LISTEN
    state_file: str = 'alert2-actions.state'
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    smtp_starttls: bool = True
    blocks: Dict[str, Block] = field(default_factory=dict)
    rules: Dict[str, Rule] = field(default_factory=dict)
    timers: Dict[str, Timer] = field(default_factory=dict)


def _split_list(s: str) -> List[str]:
    return [x.strip() for x in s.replace(';', ',').split(',') if x.strip()]


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'),
                                   strict=False)
    cp.optionxform = str                     # keep key case (hotkeys)
    # allow repeated 'run =' lines: configparser keeps the last, so read
    # blocks by hand from the raw text
    if not cp.read(path):
        raise SystemExit(f"config not found: {path}")
    cfg = Config()
    if cp.has_section('actions'):
        a = cp['actions']
        cfg.listen = a.get('listen', cfg.listen)
        cfg.state_file = a.get('state_file', cfg.state_file)
        cfg.smtp_host = a.get('smtp_host', '')
        cfg.smtp_port = a.getint('smtp_port', 587)
        cfg.smtp_user = a.get('smtp_user', '')
        cfg.smtp_password = (a.get('smtp_password', '')
                             or os.environ.get('ALERT2_SMTP_PASSWORD', ''))
        cfg.smtp_from = a.get('smtp_from', '') or cfg.smtp_user
        cfg.smtp_starttls = a.getboolean('smtp_starttls', True)
    raw_blocks = _read_block_steps(path)
    for sec in cp.sections():
        if sec.startswith('block '):
            name = sec.split(None, 1)[1].strip()
            b = cp[sec]
            blk = Block(name=name, steps=raw_blocks.get(name, []),
                        key=b.get('key', None),
                        stop_on_error=b.getboolean('stop_on_error', False),
                        timeout=b.getfloat('timeout', 60.0))
            if blk.key and len(blk.key) != 1:
                raise SystemExit(f"[{sec}]: key must be one character")
            if not blk.steps:
                raise SystemExit(f"[{sec}]: no run= or http= lines")
            cfg.blocks[name] = blk
        elif sec.startswith('rule '):
            name = sec.split(None, 1)[1].strip()
            r = cp[sec]
            try:
                site = r.getint('site')
            except (TypeError, ValueError):
                raise SystemExit(f"[{sec}]: site = <id> is required")
            if site is None:
                raise SystemExit(f"[{sec}]: site = <id> is required")
            falling = r.get('direction', 'rising').lower().startswith('fall')
            if falling:
                above = r.getfloat('under', None)
                below = r.getfloat('over', None)
                if above is None:
                    raise SystemExit(f"[{sec}]: falling rule needs under=")
            else:
                above = r.getfloat('above', None)
                below = r.getfloat('below', None)
                if above is None:
                    raise SystemExit(f"[{sec}]: rule needs above=")
            rule = Rule(name=name, site=site, sensor=r.getint('sensor', 7),
                        above=above, below=below, falling=falling,
                        debounce=max(1, r.getint('debounce', 1)),
                        on_enter=_split_list(r.get('on_enter', '')),
                        on_exit=_split_list(r.get('on_exit', '')),
                        stale=r.getfloat('stale', None),
                        on_stale=_split_list(r.get('on_stale', '')))
            cfg.rules[name] = rule
        elif sec.startswith('timer '):
            name = sec.split(None, 1)[1].strip()
            t = cp[sec]
            every = _parse_duration(t.get('every', ''))
            if every is None or every <= 0:
                raise SystemExit(f"[{sec}]: every = <seconds|30s|10m|1h> "
                                 f"is required")
            block = t.get('block', '')
            if not block:
                raise SystemExit(f"[{sec}]: block = <name> is required")
            cfg.timers[name] = Timer(
                name=name, every=every, block=block,
                while_active=t.get('while_active', None),
                while_normal=t.get('while_normal', None),
                immediate=t.getboolean('immediate', False))
    for tm in cfg.timers.values():
        if tm.block not in cfg.blocks:
            raise SystemExit(f"[timer {tm.name}] names unknown block "
                             f"{tm.block!r}")
        for rn in (tm.while_active, tm.while_normal):
            if rn and rn not in cfg.rules:
                raise SystemExit(f"[timer {tm.name}] names unknown rule "
                                 f"{rn!r}")
    for rule in cfg.rules.values():
        for b in rule.on_enter + rule.on_exit + rule.on_stale:
            if b not in cfg.blocks:
                raise SystemExit(f"[rule {rule.name}] names unknown block "
                                 f"{b!r}")
    if any(k == 'mail' for b in cfg.blocks.values() for k, _ in b.steps) \
            and not cfg.smtp_host:
        raise SystemExit("a block has a mail= step but [actions] has no "
                         "smtp_host")
    keys = {}
    for b in cfg.blocks.values():
        if b.key:
            if b.key in keys:
                raise SystemExit(f"blocks {keys[b.key]} and {b.name} share "
                                 f"key {b.key!r}")
            keys[b.key] = b.name
    for b in cfg.blocks.values():
        for k, v in b.steps:
            if k == 'wait' and _parse_duration(v) is None:
                raise SystemExit(f"[block {b.name}]: wait = {v!r} is not a "
                                 f"duration (e.g. 5, 5s, 2m)")
    if not cfg.rules and not cfg.blocks:
        raise SystemExit("config has no [block ...] or [rule ...] sections")
    return cfg


def _parse_duration(text: str) -> Optional[float]:
    """'5', '5s', '2m', '1h', '1.5m' -> seconds."""
    t = str(text).strip().lower()
    if not t:
        return None
    mult = 1.0
    if t[-1] in 'smh':
        mult = {'s': 1.0, 'm': 60.0, 'h': 3600.0}[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _read_block_steps(path: str) -> Dict[str, List[tuple]]:
    """Ordered run=/http= lines per [block NAME], repeats preserved."""
    out: Dict[str, List[tuple]] = {}
    cur = None
    with open(path, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if not s or s[0] in ';#':
                continue
            if s.startswith('['):
                cur = None
                if s.lower().startswith('[block '):
                    cur = s[1:s.index(']')].split(None, 1)[1].strip()
                    out.setdefault(cur, [])
                continue
            if cur is None or '=' not in s:
                continue
            k, v = s.split('=', 1)
            k = k.strip().lower()
            v = v.strip()
            # strip an inline comment only if preceded by whitespace
            for mark in (' ;', ' #', '\t;', '\t#'):
                i = v.find(mark)
                if i >= 0:
                    v = v[:i].rstrip()
            if k in ('run', 'http', 'mail', 'wait') and v:
                out[cur].append((k, v))
    return out


# ---------------------------------------------------------------------------
# Running blocks
# ---------------------------------------------------------------------------

def _redact(spec: str) -> str:
    """Hide credentials in a step for logging."""
    import re
    s = re.sub(r'auth=([^:\s]+):\S+', r'auth=\1:***', spec)
    return re.sub(r'://([^:/@\s]+):[^@\s]+@', r'://\1:***@', s)


def _http(spec: str, env: dict, timeout: float) -> str:
    """'GET|POST url [auth=user:pass] [digest] [insecure] [body=...]'.

    insecure: accept any TLS certificate and allow weak keys / old
    ciphers — for devices such as IP cameras with self-signed, small-key
    certificates. Applies to this request only.
    """
    import ssl
    import urllib.request
    parts = shlex.split(spec)
    method = 'GET'
    if parts and parts[0].upper() in ('GET', 'POST', 'PUT'):
        method = parts.pop(0).upper()
    if not parts:
        raise ValueError("http line has no URL")
    url = parts.pop(0)
    auth = None
    digest = False
    insecure = False
    body = None
    for p in parts:
        if p.startswith('auth='):
            auth = p[5:]
        elif p == 'digest':
            digest = True
        elif p == 'insecure':
            insecure = True
        elif p.startswith('body='):
            body = p[5:].encode()
    for k, v in env.items():
        url = url.replace('${' + k + '}', str(v))
    handlers = []
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
        except ssl.SSLError:
            pass
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if auth:
        user, _, pw = auth.partition(':')
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, url, user, pw)
        handlers.append(urllib.request.HTTPDigestAuthHandler(pm) if digest
                        else urllib.request.HTTPBasicAuthHandler(pm))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, method=method)
    with opener.open(req, timeout=timeout) as resp:
        return f'{resp.status} {resp.read(200)!r}'


def _expand(text: str, env: dict) -> str:
    for k, v in env.items():
        if k.startswith('ALERT2_'):
            text = text.replace('${' + k + '}', str(v)).replace('$' + k, str(v))
    return text


def _mail(cfg: Config, spec: str, env: dict, timeout: float) -> str:
    """'to@example.com[,other@x] "subject" "body"'. ${ALERT2_*} expands in
    subject and body. Body defaults to a one-line event summary."""
    import smtplib
    from email.message import EmailMessage
    parts = shlex.split(spec)
    if not parts:
        raise ValueError("mail line has no recipient")
    to = [t.strip() for t in parts[0].split(',') if t.strip()]
    subject = _expand(parts[1], env) if len(parts) > 1 else \
        _expand('ALERT2 ${ALERT2_RULE} ${ALERT2_EVENT}', env)
    body = _expand(parts[2], env) if len(parts) > 2 else _expand(
        'Rule ${ALERT2_RULE}: ${ALERT2_EVENT}\nSite ${ALERT2_SITE} sensor '
        '${ALERT2_SENSOR} = ${ALERT2_VALUE}\n', env)
    msg = EmailMessage()
    msg['From'] = cfg.smtp_from
    msg['To'] = ', '.join(to)
    msg['Subject'] = subject
    msg.set_content(body)
    if cfg.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    with smtp:
        if cfg.smtp_starttls and cfg.smtp_port != 465:
            smtp.starttls()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)
    return f'sent to {", ".join(to)}: {subject}'


class Runner:
    """Executes blocks. `shell` and `http` are injectable for tests."""

    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry = dry_run
        self.history: List[dict] = []

    def run_block(self, name: str, event: dict) -> bool:
        blk = self.cfg.blocks.get(name)
        if blk is None:
            logger.error(f"no such block {name!r}")
            return False
        env = dict(os.environ)
        env.update({f'ALERT2_{k.upper()}': str(v) for k, v in event.items()})
        ok_all = True
        logger.info(f"block {name}: {len(blk.steps)} step(s) "
                    f"[{event.get('event')} {event.get('rule', '-')} "
                    f"site {event.get('site', '-')} value {event.get('value', '-')}]")
        for kind, spec in blk.steps:
            t0 = time.time()
            try:
                if self.dry:
                    logger.info(f"  DRY {kind}: {_redact(spec)}")
                    continue
                if kind == 'wait' and self.dry:
                    continue
                if kind == 'wait':
                    secs = _parse_duration(spec) or 0.0
                    logger.info(f"  wait {secs:g}s")
                    time.sleep(secs)
                    continue
                if kind == 'run':
                    r = subprocess.run(spec, shell=True, env=env,
                                       capture_output=True, text=True,
                                       timeout=blk.timeout)
                    out = (r.stdout + r.stderr).strip()[-300:]
                    if r.returncode != 0:
                        raise RuntimeError(f"exit {r.returncode}: {out}")
                    logger.info(f"  run ok ({time.time()-t0:.1f}s): "
                                f"{_redact(spec)}"
                                + (f" -> {out}" if out else ''))
                elif kind == 'mail':
                    res = _mail(self.cfg, spec, env, blk.timeout)
                    logger.info(f"  mail ok ({time.time()-t0:.1f}s): {res}")
                else:
                    res = _http(spec, env, blk.timeout)
                    logger.info(f"  http ok ({time.time()-t0:.1f}s): "
                                f"{_redact(spec)} -> {res}")
            except Exception as e:
                ok_all = False
                logger.error(f"  {kind} FAILED: {_redact(spec)} -> {e}")
                if blk.stop_on_error:
                    break
        self.history.append({'t': time.time(), 'block': name, 'ok': ok_all,
                             **event})
        return ok_all


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, cfg: Config, runner: Runner):
        self.cfg = cfg
        self.runner = runner
        # per rule: {'active': bool, 'count': int, 'last': value, 'last_t': t}
        self.state: Dict[str, dict] = {
            n: {'active': False, 'count': 0, 'last': None, 'last_t': None,
                'stale_fired': False}
            for n in cfg.rules}
        self.timer_due: Dict[str, float] = {}
        now = time.time()
        for tm in cfg.timers.values():
            self.timer_due[tm.name] = now if tm.immediate else now + tm.every
        self._load()

    # persistence -----------------------------------------------------------
    def _load(self):
        try:
            with open(self.cfg.state_file, encoding='utf-8') as f:
                saved = json.load(f)
            for n, st in saved.items():
                if n in self.state:
                    self.state[n].update(st)
            logger.info(f"state restored from {self.cfg.state_file}: "
                        + ', '.join(f"{n}={'ACTIVE' if s['active'] else 'normal'}"
                                    for n, s in self.state.items()))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"state file unreadable ({e}); starting fresh")

    def _save(self):
        try:
            tmp = self.cfg.state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f)
            os.replace(tmp, self.cfg.state_file)
        except Exception as e:
            logger.warning(f"could not save state: {e}")

    # evaluation ------------------------------------------------------------
    @staticmethod
    def _reading(d: dict, sensor: int) -> Optional[float]:
        srp = d.get('srp') or {}
        for rpt in srp.get('reports', []):
            for r in rpt.get('readings', []) or []:
                if r.get('sensor_id') == sensor and isinstance(
                        r.get('value'), (int, float)):
                    return float(r['value'])
            if rpt.get('type') == 'TBRG' and rpt.get('sensor_id') == sensor:
                return float(rpt.get('accumulator', 0))
        return None

    def handle(self, d: dict) -> List[str]:
        """Evaluate every rule against one decoder datagram. Returns the
        names of blocks fired."""
        fired = []
        if d.get('truncated'):
            return fired
        src = d.get('src')
        now = time.time()
        for rule in self.cfg.rules.values():
            if rule.site != src:
                continue
            v = self._reading(d, rule.sensor)
            if v is None:
                continue
            v = round(v, 4)      # float32 from the decoder: 212.3300018 -> 212.33
            st = self.state[rule.name]
            st['last'] = v
            st['last_t'] = now
            st['stale_fired'] = False
            if rule.falling:
                enter_cond = v <= rule.above
                exit_cond = v >= (rule.below if rule.below is not None
                                  else rule.above)
            else:
                enter_cond = v >= rule.above
                exit_cond = v <= (rule.below if rule.below is not None
                                  else rule.above)
            want = exit_cond if st['active'] else enter_cond
            if not want:
                st['count'] = 0
                continue
            st['count'] += 1
            if st['count'] < rule.debounce:
                logger.info(f"rule {rule.name}: {v} qualifies "
                            f"({st['count']}/{rule.debounce})")
                continue
            st['count'] = 0
            st['active'] = not st['active']
            event = {'rule': rule.name, 'site': src, 'sensor': rule.sensor,
                     'value': v, 'time': now,
                     'event': 'enter' if st['active'] else 'exit'}
            logger.warning(f"rule {rule.name}: {event['event'].upper()} at "
                           f"{v} (site {src} sensor {rule.sensor})")
            for b in (rule.on_enter if st['active'] else rule.on_exit):
                self.runner.run_block(b, event)
                fired.append(b)
            self._save()
        return fired

    def check_stale(self) -> List[str]:
        fired = []
        now = time.time()
        for rule in self.cfg.rules.values():
            if not rule.stale or not rule.on_stale:
                continue
            st = self.state[rule.name]
            if st['last_t'] is None or st['stale_fired']:
                continue
            if now - st['last_t'] > rule.stale:
                st['stale_fired'] = True
                event = {'rule': rule.name, 'site': rule.site,
                         'sensor': rule.sensor, 'value': st['last'],
                         'time': now, 'event': 'stale'}
                logger.warning(f"rule {rule.name}: no report for "
                               f"{rule.stale:.0f}s")
                for b in rule.on_stale:
                    self.runner.run_block(b, event)
                    fired.append(b)
                self._save()
        return fired

    def _timer_eligible(self, tm: Timer) -> bool:
        if tm.while_active and not self.state[tm.while_active]['active']:
            return False
        if tm.while_normal and self.state[tm.while_normal]['active']:
            return False
        return True

    def check_timers(self, now: Optional[float] = None) -> List[str]:
        """Fire due timers. An ineligible timer (its rule not in the
        wanted state) is held and, with immediate=yes, fires as soon as it
        becomes eligible; otherwise it waits for its next tick."""
        now = time.time() if now is None else now
        fired = []
        for tm in self.cfg.timers.values():
            if now < self.timer_due[tm.name]:
                continue
            if not self._timer_eligible(tm):
                if not tm.immediate:
                    self.timer_due[tm.name] = now + tm.every
                continue
            self.timer_due[tm.name] = now + tm.every
            st = (self.state.get(tm.while_active or tm.while_normal or '')
                  or {})
            event = {'event': 'timer', 'rule': tm.while_active
                     or tm.while_normal or '', 'timer': tm.name,
                     'site': self.cfg.rules[tm.while_active].site
                     if tm.while_active else (
                         self.cfg.rules[tm.while_normal].site
                         if tm.while_normal else ''),
                     'value': st.get('last', ''), 'time': now}
            self.runner.run_block(tm.block, event)
            fired.append(tm.block)
        return fired

    def manual(self, block: str) -> bool:
        return self.runner.run_block(block, {'event': 'manual',
                                             'time': time.time()})


# ---------------------------------------------------------------------------
# Console + main loop
# ---------------------------------------------------------------------------

def _console_help(cfg: Config, engine: Engine) -> str:
    lines = ["keys:  " + "  ".join(f"[{b.key}] {b.name}" for b in
                                   cfg.blocks.values() if b.key)
             + "   [?] help  [s] state  [q] quit"]
    return '\n'.join(lines)


def _state_text(cfg: Config, engine: Engine) -> str:
    out = []
    for n, r in cfg.rules.items():
        st = engine.state[n]
        last = f"{st['last']}" if st['last'] is not None else '-'
        age = (f"{time.time()-st['last_t']:.0f}s ago" if st['last_t']
               else 'never')
        out.append(f"  {n:<20} site {r.site:<5} sensor {r.sensor:<3} "
                   f"{'ACTIVE' if st['active'] else 'normal':<7} "
                   f"last {last} ({age})  "
                   f"{'under' if r.falling else 'above'} {r.above} / "
                   f"{'over' if r.falling else 'below'} "
                   f"{r.below if r.below is not None else r.above} "
                   f"x{r.debounce}")
    return '\n'.join(out) or '  (no rules)'


class _Async:
    """Run blocks on one worker thread, in order, so a block with waits
    never stalls datagram handling or the console."""

    def __init__(self, runner: Runner):
        import queue
        import threading
        self.runner = runner
        self.dry = runner.dry
        self.q: 'queue.Queue' = queue.Queue()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while True:
            name, event = self.q.get()
            try:
                self.runner.run_block(name, event)
            except Exception as e:
                logger.error(f"block {name} crashed: {e}")
            self.q.task_done()

    def run_block(self, name: str, event: dict) -> bool:
        self.q.put((name, event))
        return True


def serve(cfg: Config, runner: Runner, console: bool = True):
    engine = Engine(cfg, _Async(runner))
    host, _, port = cfg.listen.partition(':')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    sock.setblocking(False)
    logger.info(f"alert2-actions listening on {cfg.listen}: "
                f"{len(cfg.rules)} rule(s), {len(cfg.blocks)} block(s), "
                f"{len(cfg.timers)} timer(s)"
                + (" [DRY RUN]" if runner.dry else ''))
    keymap = {b.key: b.name for b in cfg.blocks.values() if b.key}
    fd = None
    old = None
    if console and sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        print(_console_help(cfg, engine))
        print(_state_text(cfg, engine))
    try:
        while True:
            rl = [sock] + ([sys.stdin] if fd is not None else [])
            ready, _, _ = select.select(rl, [], [], 1.0)
            for s in ready:
                if s is sock:
                    data, _ = sock.recvfrom(65535)
                    try:
                        engine.handle(json.loads(data.decode('utf-8')))
                    except Exception as e:
                        logger.warning(f"bad datagram: {e}")
                else:
                    ch = sys.stdin.read(1)
                    if ch == 'q':
                        return
                    elif ch == '?':
                        print(_console_help(cfg, engine))
                    elif ch == 's':
                        print(_state_text(cfg, engine))
                    elif ch in keymap:
                        print(f"-> manual: {keymap[ch]}")
                        engine.manual(keymap[ch])
            engine.check_stale()
            engine.check_timers()
    except KeyboardInterrupt:
        pass
    finally:
        if fd is not None and old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='alert2-actions',
        description='Run command blocks when ALERT2 readings cross '
                    'thresholds (with hysteresis and debounce), or on a '
                    'keypress. Feed it with alert2-decode --udp-emit.')
    ap.add_argument('--config', required=True)
    ap.add_argument('--fire', metavar='BLOCK',
                    help='run one block manually and exit')
    ap.add_argument('--dry-run', action='store_true',
                    help='log what would run, execute nothing')
    ap.add_argument('--no-console', action='store_true',
                    help='no keyboard (for a service)')
    ap.add_argument('--check', action='store_true',
                    help='validate the config and print rules/blocks')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), 20),
                        format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(args.config)
    if args.check:
        print(f"listen {cfg.listen}   state {cfg.state_file}")
        for b in cfg.blocks.values():
            print(f"[block {b.name}] key={b.key or '-'} "
                  f"stop_on_error={b.stop_on_error}")
            for kind, spec in b.steps:
                print(f"    {kind:<4} {_redact(spec)}")
        for r in cfg.rules.values():
            print(f"[rule {r.name}] site {r.site} sensor {r.sensor} "
                  f"{'under' if r.falling else 'above'} {r.above} / "
                  f"{'over' if r.falling else 'below'} "
                  f"{r.below if r.below is not None else r.above} "
                  f"debounce {r.debounce}  enter->{r.on_enter} "
                  f"exit->{r.on_exit}"
                  + (f" stale {r.stale:.0f}s->{r.on_stale}" if r.stale else ''))
        for t in cfg.timers.values():
            cond = (f" while {t.while_active} ACTIVE" if t.while_active else
                    f" while {t.while_normal} normal" if t.while_normal else '')
            print(f"[timer {t.name}] every {t.every:g}s -> {t.block}{cond}"
                  f"{' immediate' if t.immediate else ''}")
        return 0
    runner = Runner(cfg, dry_run=args.dry_run)
    if args.fire:
        ok = runner.run_block(args.fire, {'event': 'manual',
                                          'time': time.time()})
        return 0 if ok else 1
    serve(cfg, runner, console=not args.no_console)
    return 0


if __name__ == '__main__':
    sys.exit(main())
