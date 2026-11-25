# `alert2-hub` — linking two or more receive sites

Each site runs its decoder as usual, plus a name and a link to the hub.
The hub recognises the same air frame arriving from several sites, keeps
the best copy, and re-emits in the decoder's own `--udp-emit` JSON, so
the Campbell forwarder, `alert2-aprs` and `alert2-actions` run once, on
one stream.

```bash
# every receive site (the hub machine's own decoder uses 127.0.0.1)
alert2-decode --stdin ... --site-name qth  --link HUBHOST:9990
alert2-decode --stdin ... --site-name shop --link HUBHOST:9990

# the hub
alert2-hub --config hub.ini --check
alert2-hub --config hub.ini
```

Start from `hub.ini.example`; every field is commented there.

## The link

TCP, one JSON object per line. The decoder-side sender runs on its own
thread, so decoding never waits on the network; it reconnects with
backoff (1 s doubling to 30 s) and spools the newest 5000 packets while
the hub is unreachable. `--link-key KEY` (or `ALERT2_LINK_KEY` on both
ends, `key =` in `hub.ini`) signs every packet with HMAC-SHA256 and the
hub drops anything unsigned or wrongly signed. That authenticates the
sender; it does not encrypt, and it does not stop a captured line being
replayed, so keep the link inside the VPN.

## Merging

Two copies are the same air frame when their FEC mode and MANT PDU bytes
are identical. The first copy starts a `hold` timer (default 2 s); when
it expires the group is merged: a complete copy beats a truncated one,
then higher MER, then fewer RS corrections. A truncated copy whose bytes
are a prefix of a complete frame is folded into it rather than emitted.

A copy that arrives after its frame was merged (slow link, spool replay)
is recognised by its receive time (`dedup_window`, default 30 s): it is
not sent again on merged outputs, but the site is credited with having
heard it. Identical bytes with receive times further apart than the
window are a new report. The same report relayed by two different
repeaters has different path bytes, so those remain two frames, as they
are today.

## Outputs

```ini
[output campbell]
to       = 127.0.0.1:9999
mode     = separate        ; every site's copy, immediately
add_path = yes

[output aprs]
to   = 127.0.0.1:9998
mode = merged              ; one copy per air frame, after the hold
```

Every datagram gains `site`. Merged ones also carry `heard_by`: each
site's MER, truncation, RS corrections and receive time for that frame.
Merged outputs lag by `hold`.

## Add Path

```ini
[site qth]
add_path = 4690            ; this receive point's ALERT2 source address
```

On an output with `add_path = yes` the hub does what MANT spec 2.2.4
requires of a repeater, and nothing more: **only if the frame's Add Path
Service Request bit is set**, it increments the Number of Added Source
Addresses and appends the receiving site's address. The base software
then shows the receive point at the end of the path, and reports heard
by two sites arrive as two copies with different last hops.

- A frame without the request bit, a truncated frame, a path already
  holding six addresses, or one that already contains the site's
  address, passes through unchanged. The stats line counts each case.
- The hop limit is not touched unless `decrement_hop = yes` (and never
  when it is 7 or 0).
- The modified datagram keeps the bytes as received in
  `raw_payload_original_hex` and names the address in
  `add_path_applied`. The hub log and stats always describe what was on
  the air.
- Use only addresses assigned in the network's address plan; an invented
  one can collide with a real site in the base station's database.

## Metrics

`log_file` gets one line per merged frame:

```
FRAME src=735 path=[4603,4601] best=shop [qth=4.4 shop=7.1]
```

and every `stats_interval` (and at exit) the hub logs, and writes to
`stats_file` as JSON:

```
hub: 53 frames merged, 0 late copies, 0 truncated folded, 0 rejected
  qth        heard 53/53 (100.00%)  best 25  only-site 25  truncated 1  MER avg 5.24 dB
  shop       heard 28/53 (52.83%)   best 28  only-site 0   truncated 0  MER avg 5.96 dB
  heard by qth+shop             28
  heard by qth                  25
```

"heard N/M" is each site's recall against the union of all sites, live.

## Measured

The block above is a real run: two decoders on the same 2.1-minute QTH
recording, one with the full flag set as site `qth` and one deliberately
crippled (single phase, exact framesync only) as site `shop`, both
linked to one hub over TCP. The union was 53 frames, `qth` heard all of
them, `shop` 28, and the Campbell-style output received 80 copies with
4690 or 4691 appended (one truncated frame passed through unchanged).
Both sites decoding one recording says nothing about real two-site
diversity; that needs the QTH and shop running live.
