# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
ALERT2 Transmit Console — standalone bench tool.

A separate process from the read-only monitor (alert2.web). It imports
alert2.generator to build frames, serves a small local web page for
composing them, and emits either a WAV file or a live PTT transmission.
You run the decoder on a separate radio/computer to see responses.

Zones
-----
  Status   (default): send an MSR Type-5 status "pretending" to be a site,
                      OR query a repeater's status via bundled Get.
  Pretend           : freeform sensor value + site ID; TBRG / MSR / GSR.
  Get Explorer      : pick IND parameters (single or bundled) to query.
  Config Change     : Set IND parameters (source ID, hop, TDMA...) + NV-save.
                      *** UNVERIFIED over-the-air on AL205R — bench-test. ***

Safety: transmit is OFF unless started with --enable-tx. Binds to
127.0.0.1 by default. WAV export is the default emit; live PTT requires
an explicit device selection. Every emit is logged with the app UID.

Run:
    python -m alert2.txconsole --app-uid bench01 [--enable-tx] \
        [--host 127.0.0.1] [--port 8788]
"""
import argparse
import json
import os
import sys
import time
import base64
import io
import wave
import logging
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

from . import generator as G

logger = logging.getLogger(__name__)

_STATE = {
    "app_uid": "bench",
    "enable_tx": False,
    "log": [],          # list of emit records
}


# ---------------------------------------------------------------------
# Frame building dispatch (shared by preview and emit)
# ---------------------------------------------------------------------

def _build_bits(req: dict):
    """Build AirLink bits from a request dict. Returns (bits, summary)."""
    mode = req.get("mode")
    sa = int(req["sa"])
    da = req.get("da")
    da = int(da) if da not in (None, "", "none") else None
    ts = req.get("timestamp")
    ts = int(ts) if ts not in (None, "") else None
    hop = int(req.get("hop", 3))
    fec = int(req.get("fec_mode", 0))
    if fec not in (0, 1, 2):
        raise ValueError(f"fec_mode must be 0, 1 or 2 (got {fec})")

    if mode == "status":
        bits = G.make_status_frame(
            sa, battery_v=float(req.get("battery", 12.8)),
            clock_status=int(req.get("clock_status", 0)),
            ind_temp_c=float(req.get("temp", 25.0)),
            msgs_rcvd=int(req.get("msgs_rcvd", 0)),
            msgs_sent=int(req.get("msgs_sent", 0)),
            status_bits=int(req.get("status_bits", 0)), timestamp=ts,
            dest_addr=da, fec_mode=fec)
        summ = f"MSR-5 status from {sa}"
    elif mode == "tbrg":
        tips = [int(x) for x in req.get("tips", []) if str(x) != ""]
        bits = G.make_tbrg_frame(
            sa, sensor_id=int(req.get("sensor_id", 1)),
            accumulator=int(req.get("accumulator", 0)),
            tip_offsets=tips, timestamp=ts, dest_addr=da, hop_limit=hop, fec_mode=fec)
        summ = f"TBRG from {sa}"
    elif mode == "msr":
        rtype = int(req.get("report_type", 3))
        values = {int(k): float(v) for k, v in req.get("values", {}).items()}
        bits = G.make_msr_frame(sa, rtype, values, timestamp=ts,
                                dest_addr=da, hop_limit=hop, fec_mode=fec)
        summ = f"MSR-{rtype} from {sa}"
    elif mode == "sensor":
        readings = [(int(k), float(v) if "." in str(v) else int(v))
                    for k, v in req.get("readings", {}).items()]
        bits = G.make_sensor_frame(sa, readings, dest_addr=da,
                                   timestamp=ts, hop_limit=hop, fec_mode=fec)
        summ = f"GSR from {sa}"
    elif mode == "get":
        params = [int(p, 0) if isinstance(p, str) else int(p)
                  for p in req["params"]]
        bits = G.make_v2_query_frame(sa, int(da), params=params, hop_limit=hop, fec_mode=fec)
        summ = f"Get {[hex(p) for p in params]} -> {da}"
    elif mode == "set":
        if not _STATE["enable_tx"]:
            pass  # preview allowed; emit gated separately
        param = int(req["param"], 0) if isinstance(req["param"], str) \
            else int(req["param"])
        vbytes = bytes.fromhex(req["value_hex"])
        payload = G.build_ind_set(param, vbytes)
        if req.get("nv_save"):
            payload += G.build_ind_save()
        pdu = G.build_mant_pdu(payload, sa, dest_addr=int(da),
                               port=G.MANT_PORT_CC, hop_limit=hop, fec_mode=fec)
        bits = G.build_airlink_bits([pdu], fec_mode=fec)
        summ = f"Set {hex(param)}={req['value_hex']} -> {da}" + \
               (" +NVSave" if req.get("nv_save") else " (NOT saved)")
    elif mode == "raw":
        bits = G.make_raw_frame(sa, req["payload_hex"], dest_addr=da,
                                port=int(req.get("port", 0)), hop_limit=hop, fec_mode=fec)
        summ = f"raw -> {da}"
    elif mode == "tone":
        return None, "tone"  # handled separately
    else:
        raise ValueError(f"unknown mode {mode}")
    return bits, summ


def _decode_preview(bits):
    """Round-trip the bits through the decoder for a preview."""
    from .demod import demodulate
    from .decoder import Alert2Decoder
    audio = G.build_tx_audio(bits, G.TxTiming())
    dbits, _ = demodulate(audio, 43200)
    pkts = Alert2Decoder().decode_bits(dbits)
    out = []
    for p in pkts:
        d = {"sa": p.mant.source_addr, "da": p.mant.dest_addr,
             "port": p.mant.port, "payload": p.application_payload.hex()}
        if getattr(p, "srp_pdu", None):
            d["srp"] = str(p.srp_pdu)
        out.append(d)
    # airtime estimate (empirical FEC0)
    n_mant = len(bits)  # rough; report payload airtime via calc
    return out, len(audio) / 43200 * 1000.0


# ---------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._html(PAGE)
        elif path == "/api/params":
            cats = {}
            for code, (name, cat, acc) in sorted(G.IND_PARAMS.items()):
                cats.setdefault(cat, []).append(
                    {"code": code, "hex": f"0x{code:02X}",
                     "name": name, "access": acc})
            self._json({"categories": cats, "app_uid": _STATE["app_uid"],
                        "tx_enabled": _STATE["enable_tx"]})
        elif path == "/api/log":
            self._json({"log": _STATE["log"][-50:]})
        else:
            self.send_error(404)

    def _html(self, html):
        b = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or "{}")
        try:
            if path == "/api/preview":
                if req.get("mode") == "tone":
                    dur = float(req.get("seconds", 2)) * 1000
                    audio = G.test_tone(dur, float(req.get("amplitude", 0.8)),
                                        half=bool(req.get("half")))
                    self._json({"summary": "test tone",
                                "airtime_ms": len(audio) / 43200 * 1000,
                                "decoded": []})
                    return
                bits, summ = _build_bits(req)
                decoded, airtime = _decode_preview(bits)
                self._json({"summary": summ, "airtime_ms": airtime,
                            "decoded": decoded, "n_bits": len(bits)})
            elif path == "/api/emit":
                self._emit(req)
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, status=400)

    def _emit(self, req):
        mode = req.get("mode")
        # Build audio
        if mode == "tone":
            audio = G.test_tone(float(req.get("seconds", 2)) * 1000,
                                float(req.get("amplitude", 0.8)),
                                half=bool(req.get("half")))
            summ = "test tone" + (" (half)" if req.get("half") else "")
        else:
            bits, summ = _build_bits(req)
            timing = G.TxTiming(
                warmup_ms=float(req.get("warmup", 750)),
                co_ms=float(req.get("co", 10)),
                agc_ms=float(req.get("agc", 55)),
                tail_ms=float(req.get("tail", 5)))
            audio = G.build_tx_audio(bits, timing,
                                     float(req.get("amplitude", 0.8)))

        target = req.get("target", "wav")
        rec = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
               "app_uid": _STATE["app_uid"], "summary": summ,
               "target": target}

        if target == "wav":
            # Audio is synthesised at 43200 Hz (9 samples/symbol) but almost
            # no soundcard plays that natively. Exporting 43200 and playing
            # it on a 48000 Hz device stretches every symbol by 1.111x: the
            # preamble matches for ~15 bits then diverges, so the frame goes
            # out on the air looking strong and never decodes. Resample on
            # the way out; 48000 is the default.
            rate = int(req.get("tx_rate") or G.DEFAULT_OUTPUT_RATE)
            out = G.resample_to(audio, G.TX_SAMPLE_RATE, rate)
            buf = io.BytesIO()
            pcm = np.clip(out, -1, 1)
            pcm16 = (pcm * 32767).astype("<i2")
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(pcm16.tobytes())
            b64 = base64.b64encode(buf.getvalue()).decode()
            rec["tx_rate"] = rate
            if rate not in G.COMMON_OUTPUT_RATES:
                rec["warning"] = (f"{rate} Hz is not a common soundcard rate; "
                                  f"playing without resampling will not decode")
            _STATE["log"].append(rec)
            self._json({"ok": True, "wav_base64": b64, "summary": summ,
                        "tx_rate": rate,
                        "warning": rec.get("warning")})
            return

        # Live PTT — gated
        if not _STATE["enable_tx"]:
            self._json({"error": "live transmit disabled; start with "
                                  "--enable-tx"}, status=403)
            return
        if mode == "set" and not req.get("confirm_set"):
            self._json({"error": "config-change requires explicit confirm"},
                       status=403)
            return
        ptt_spec = req.get("ptt")
        from .txcli import parse_ptt
        ptt = parse_ptt(ptt_spec)
        try:
            _rate = int(req.get("tx_rate") or G.DEFAULT_OUTPUT_RATE)
            G.transmit(audio, ptt=ptt,
                       timing=G.TxTiming(
                           warmup_ms=float(req.get("warmup", 750))),
                       device=req.get("device"),
                       sample_rate=_rate)
            rec["ptt"] = ptt_spec
            rec["tx_rate"] = _rate
            _STATE["log"].append(rec)
            self._json({"ok": True, "summary": summ, "transmitted": True})
        finally:
            ptt.close()


# ---------------------------------------------------------------------
# Minimal single-page UI
# ---------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>ALERT2 TX Console</title><style>
body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0d1117;
color:#e6edf3;margin:0;padding:16px;font-size:13px}
h1{font-size:16px;margin:0 0 4px}.sub{color:#8b98a5;font-size:11px;margin-bottom:14px}
.tabs{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.tab{padding:6px 12px;background:#161b22;border:1px solid #2d3743;border-radius:6px;cursor:pointer}
.tab.active{background:#1f6feb;border-color:#1f6feb}
.panel{background:#161b22;border:1px solid #2d3743;border-radius:8px;padding:14px;display:none}
.panel.active{display:block}
label{display:block;margin:8px 0 2px;color:#8b98a5}
input,select{background:#0d1117;border:1px solid #2d3743;color:#e6edf3;
padding:5px 8px;border-radius:5px;font-family:inherit;width:180px}
button{background:#238636;border:none;color:#fff;padding:7px 14px;border-radius:6px;
cursor:pointer;font-family:inherit;margin-top:10px}button.sec{background:#30363d}
button.warn{background:#9e6a03}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
.out{margin-top:14px;background:#0d1117;border:1px solid #2d3743;border-radius:6px;
padding:10px;white-space:pre-wrap;font-size:12px;color:#7ee787;min-height:20px}
.warnbox{background:#2a1f08;border:1px solid #5a4a16;border-radius:6px;padding:8px;
color:#e3b341;font-size:11px;margin:8px 0}
.pill{display:inline-block;padding:1px 6px;border-radius:4px;background:#21262d;margin:2px}
.cat{margin:6px 0}.cat b{color:#79c0ff}
</style></head><body>
<h1>ALERT2 Transmit Console</h1>
<div class="sub" id="sub">bench tool — build & emit ALERT2 frames · decoder runs separately</div>
<div class="tabs">
<div class="tab active" data-t="status">Status</div>
<div class="tab" data-t="pretend">Pretend Site</div>
<div class="tab" data-t="get">Get Explorer</div>
<div class="tab" data-t="set">Config Change</div>
<div class="tab" data-t="tone">Test Tone</div>
</div>

<div class="panel active" id="p-status">
<div class="row">
<div><label>Site SA (pretend)</label><input id="s-sa" value="4601"></div>
<div><label>Battery V</label><input id="s-bat" value="12.8"></div>
<div><label>Clock status</label><input id="s-clk" value="0"></div>
<div><label>IND temp C</label><input id="s-tmp" value="25.0"></div>
<div><label>Status bits (0x80=overrun)</label><input id="s-sb" value="0"></div>
</div>
<button onclick="prev('status')">Preview</button>
<button class="sec" onclick="emit('status')">Emit</button>
</div>

<div class="panel" id="p-pretend">
<div class="row">
<div><label>Report type</label><select id="pt-type" onchange="ptFields()">
<option value="tbrg">Tipping Bucket (Type 2)</option>
<option value="msr">Multi-Sensor (Type 3/4/5)</option>
<option value="sensor">General Sensor (freeform)</option>
</select></div>
<div><label>Site SA</label><input id="pt-sa" value="707"></div>
<div><label>Timestamp (opt)</label><input id="pt-ts" placeholder="sec since 12:00"></div>
</div>
<div id="pt-body"></div>
<button onclick="prev('pretend')">Preview</button>
<button class="sec" onclick="emit('pretend')">Emit</button>
</div>

<div class="panel" id="p-get">
<div class="row">
<div><label>Source SA (you)</label><input id="g-sa" value="999"></div>
<div><label>Target SA</label><input id="g-da" value="4601"></div>
</div>
<label>Parameters (check to bundle)</label>
<div id="g-params"></div>
<button onclick="prev('get')">Preview</button>
<button class="sec" onclick="emit('get')">Emit</button>
</div>

<div class="panel" id="p-set">
<div class="warnbox">⚠ Config-change over-the-air is UNVERIFIED on AL205R firmware.
Bench-test before using on a live site. A Set is not persistent without NV-Save.</div>
<div class="row">
<div><label>Source SA (you)</label><input id="x-sa" value="999"></div>
<div><label>Target SA</label><input id="x-da" value="4601"></div>
<div><label>Parameter</label><select id="x-param"></select></div>
</div>
<div class="row">
<div><label>New value (hex bytes)</label><input id="x-val" placeholder="e.g. 1133"></div>
<div><label><input type="checkbox" id="x-save" style="width:auto"> NV-Save (persist)</label></div>
<div><label><input type="checkbox" id="x-confirm" style="width:auto"> I confirm target &amp; value</label></div>
</div>
<button onclick="prev('set')">Preview</button>
<button class="warn" onclick="emit('set')">Emit config change</button>
</div>

<div class="panel" id="p-tone">
<div class="row">
<div><label>Seconds</label><input id="t-sec" value="3"></div>
<div><label>Amplitude</label><input id="t-amp" value="0.8"></div>
<div><label><input type="checkbox" id="t-half" style="width:auto"> Half amplitude (-6dB)</label></div>
</div>
<button onclick="prev('tone')">Preview</button>
<button class="sec" onclick="emit('tone')">Emit</button>
</div>

<div class="row" style="margin-top:12px">
<div><label>Emit target</label><select id="target">
<option value="wav">WAV download</option>
<option value="ptt">Live PTT</option></select></div>
<div><label>PTT spec</label><input id="ptt" placeholder="serial:/dev/ttyUSB0:rts or gpio:17"></div>
<div><label>Amplitude</label><input id="amp" value="0.8" style="width:70px"></div>
<div><label>FEC mode</label><select id="fecmode">
<option value="0" selected>0 — k=7 rate 1/2 (most networks)</option>
<option value="1">1 — k=9 rate 2/3</option>
<option value="2">2 — k=9 rate 3/4</option>
</select></div>
<div><label>Sample rate</label><select id="txrate">
<option value="48000" selected>48000 Hz (standard)</option>
<option value="44100">44100 Hz</option>
<option value="96000">96000 Hz</option>
<option value="43200">43200 Hz (native — decode only, do NOT play)</option>
</select></div>
</div>
<div id="ratewarn" style="color:#d29922;margin-top:6px;display:none">
Audio is synthesised at 43200 Hz. Exporting at 43200 and playing it on a
48000 Hz soundcard stretches every symbol by 1.111x — the frame will go out
sounding strong but will NOT decode. Use 43200 only for a file you intend to
decode directly.
</div>

<div class="out" id="out">ready</div>

<script>
let params=null, appuid='bench', txen=false;
fetch('/api/params').then(r=>r.json()).then(d=>{
  params=d.categories; appuid=d.app_uid; txen=d.tx_enabled;
  document.getElementById('sub').textContent =
    'bench tool · app-uid='+appuid+(txen?' · LIVE TX ENABLED':' · WAV-only (no --enable-tx)');
  renderGetParams(); renderSetParams();
});
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('p-'+t.dataset.t).classList.add('active');
});
function renderGetParams(){
  let h='';for(const cat in params){h+='<div class="cat"><b>'+cat+'</b> ';
    params[cat].forEach(p=>{h+='<label style="display:inline;margin:0 8px"><input type="checkbox" '+
      'style="width:auto" value="'+p.code+'" '+(p.code==0x52?'checked':'')+'> '+p.hex+' '+p.name+'</label>';});
    h+='</div>';}
  document.getElementById('g-params').innerHTML=h;
}
function renderSetParams(){
  let s=document.getElementById('x-param'),h='';
  for(const cat in params) params[cat].forEach(p=>{
    if(p.access=='RW') h+='<option value="'+p.code+'">'+p.hex+' '+p.name+'</option>';});
  s.innerHTML=h;
}
function ptFields(){
  let t=document.getElementById('pt-type').value,h='';
  if(t=='tbrg') h='<div class="row"><div><label>Sensor ID</label><input id="pt-sid" value="1"></div>'+
    '<div><label>Accumulator (tips)</label><input id="pt-acc" value="1234"></div>'+
    '<div><label>Recent tip offsets (csv sec)</label><input id="pt-tips" value="10,45,90"></div></div>';
  else if(t=='msr') h='<div class="row"><div><label>MSR type</label><select id="pt-mtype">'+
    '<option value="3">3 English</option><option value="4">4 Metric</option><option value="5">5 IND</option></select></div>'+
    '<div><label>Air Temp</label><input id="pt-v1" placeholder="72.5"></div>'+
    '<div><label>Stage</label><input id="pt-v7" placeholder="4.25"></div>'+
    '<div><label>Battery</label><input id="pt-v8" placeholder="12.9"></div></div>';
  else h='<div class="row"><div><label>Sensor ID</label><input id="pt-gid" value="5"></div>'+
    '<div><label>Value</label><input id="pt-gval" value="12.34"></div></div>';
  document.getElementById('pt-body').innerHTML=h;
}
ptFields();
function common(req){
  req.target=document.getElementById('target').value;
  req.ptt=document.getElementById('ptt').value;
  req.amplitude=parseFloat(document.getElementById('amp').value);
  req.tx_rate=parseInt(document.getElementById('txrate').value);
  req.fec_mode=parseInt(document.getElementById('fecmode').value);
  return req;
}
function buildReq(zone){
  if(zone=='status') return {mode:'status',sa:v('s-sa'),battery:v('s-bat'),
    clock_status:v('s-clk'),temp:v('s-tmp'),status_bits:parseInt(v('s-sb'))||0};
  if(zone=='pretend'){let t=v('pt-type');
    if(t=='tbrg')return{mode:'tbrg',sa:v('pt-sa'),sensor_id:v('pt-sid'),
      accumulator:v('pt-acc'),tips:v('pt-tips').split(',').filter(x=>x!==''),timestamp:v('pt-ts')};
    if(t=='msr'){let vals={};['1','7','8'].forEach(id=>{let x=v('pt-v'+id);if(x!=='')vals[id]=x;});
      return{mode:'msr',sa:v('pt-sa'),report_type:v('pt-mtype'),values:vals,timestamp:v('pt-ts')};}
    return{mode:'sensor',sa:v('pt-sa'),readings:{[v('pt-gid')]:v('pt-gval')},timestamp:v('pt-ts')};}
  if(zone=='get'){let ps=[...document.querySelectorAll('#g-params input:checked')].map(x=>parseInt(x.value));
    return{mode:'get',sa:v('g-sa'),da:v('g-da'),params:ps};}
  if(zone=='set')return{mode:'set',sa:v('x-sa'),da:v('x-da'),param:parseInt(v('x-param')),
    value_hex:v('x-val'),nv_save:document.getElementById('x-save').checked,
    confirm_set:document.getElementById('x-confirm').checked};
  if(zone=='tone')return{mode:'tone',seconds:v('t-sec'),amplitude:v('t-amp'),
    half:document.getElementById('t-half').checked};
}
function v(id){let e=document.getElementById(id);return e?e.value:'';}
function prev(zone){
  fetch('/api/preview',{method:'POST',body:JSON.stringify(buildReq(zone))})
   .then(r=>r.json()).then(show);
}
document.addEventListener('DOMContentLoaded',function(){
  var r=document.getElementById('txrate');
  if(r) r.addEventListener('change',function(){
    document.getElementById('ratewarn').style.display =
      (r.value==='43200') ? 'block' : 'none';
  });
});
function emit(zone){
  fetch('/api/emit',{method:'POST',body:JSON.stringify(common(buildReq(zone)))})
   .then(r=>r.json()).then(d=>{
     if(d.wav_base64){let a=document.createElement('a');
       a.href='data:audio/wav;base64,'+d.wav_base64;a.download='alert2_tx.wav';a.click();
       show({summary:d.summary+' — WAV downloaded @ '+(d.tx_rate||48000)+' Hz'
             +(d.warning?('\nWARNING: '+d.warning):'')});}
     else show(d);});
}
function show(d){
  let o=document.getElementById('out');
  if(d.error){o.style.color='#f85149';o.textContent='ERROR: '+d.error;return;}
  o.style.color='#7ee787';
  let s=(d.summary||'')+'\n';
  if(d.airtime_ms)s+='airtime: '+d.airtime_ms.toFixed(0)+' ms\n';
  if(d.decoded&&d.decoded.length){s+='decodes back as:\n';
    d.decoded.forEach(p=>{s+='  SA='+p.sa+' DA='+p.da+' port='+p.port+' payload='+p.payload+'\n';
      if(p.srp)s+='  '+p.srp+'\n';});}
  if(d.transmitted)s+='TRANSMITTED via PTT\n';
  o.textContent=s;
}
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="alert2-txconsole")
    ap.add_argument("--app-uid", default="bench",
                    help="identity stamped on the emit log")
    ap.add_argument("--enable-tx", action="store_true",
                    help="allow live PTT transmit (default: WAV only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args(argv)

    _STATE["app_uid"] = args.app_uid
    _STATE["enable_tx"] = args.enable_tx

    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    tx = "ENABLED" if args.enable_tx else "WAV-only"
    print(f"ALERT2 TX Console  http://{args.host}:{args.port}  "
          f"[uid={args.app_uid}, live-tx={tx}]")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
