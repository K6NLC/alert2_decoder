# `alert2-indapi` — feeding from an AL205R / A2X

Reads the ASCII output of an ALERT2 IND and re-emits each received PDU
as this project's packet datagram, so a commercial receiver can drive
`alert2-actions`, `alert2-aprs` and `alert2-hub` exactly as the software
decoder does.

```bash
# the IND's "ASCII IP Forward" host list points at this machine
alert2-indapi --listen 0.0.0.0:60015 --udp-emit 127.0.0.1:9997      # -> alert2-actions

# as another site in the hub (merged with, and scored against, the decoders)
alert2-indapi --listen 0.0.0.0:60015 --site-name al205 --link HUBHOST:9990

# other ways in
alert2-indapi --connect 192.0.2.20:9950 ...        # connect out to the IND or a terminal server
alert2-indapi --file /dev/ttyUSB0 ...            # serial (set the port up with stty first)
alert2-indapi --file SLO-Courthouse_20260830 --verify --print     # check a log
```

## The format

Learned from 35 days of real AL205R output (90,263 frames, 118,424
PDUs). One received AirLink frame is:

```
M,P,<Y,M,D,h,m,s.frac>,FEC=<mode>,NLVL=<noise>
P,0,<time>,<n>,<size>,<size hex bytes>,<rs_corr>[,<size>,<bytes>,<rs_corr>...],<rx1>[,<rx2>]
M,N,<time>,ENC=<0|1>                     } once per MANT PDU
N,<time>,<10 header fields>,<len>,<src>,<n_added>,<added...>,<payload hex bytes>
```

- **P** is the AirLink PDU as received: payload length, then each FEC
  block as a decimal size, that many hex bytes, and its RS correction
  count. The first block is always 24 bytes (2-byte AirLink header
  first), padded with `55` when the PDU is shorter. A correction count
  of `-1` is a block the IND could not correct; that frame has no N
  records.
- **N** is one MANT PDU with the header split into fields: version,
  protocol id, TSSR, APSR, DAI, port, encrypted, ack, added header, hop
  limit; then payload length, source, the added-path count and
  addresses, and the payload.
- An N record with no P record and its own timestamp is the IND's own
  hourly status report under its own address. It is passed on with
  `local_origin: true`.
- `S` records are the IND's status line (counted, not used); lines
  holding only `.` are separators.
- Timestamps are UTC. `--utc-offset` exists for an IND set otherwise.

## What is verified, and what is not

`--verify` rebuilds the MANT header bytes from each N record's fields
and checks that a frame's PDUs concatenate to exactly the bytes in its P
record. Over the 35 days: 90,260 of 90,263 frames reproduce byte for
byte; the other 3 are frames the IND itself failed to correct (`-1`) and
printed no N records for. All 118,424 payloads parsed as sensor reports.

Rebuilding the exact bytes matters because the hub matches frames
between receivers on those bytes.

Not present in the reference logs, so unverified:

- a Destination Address (DAI=1): its position in the N record is a guess;
- protocol id 1 (a PDU id byte) and encrypted payloads;
- which of two always-zero columns is "ack" and which "added header";
- the **binary** output format: not implemented, there is no capture of it;

## The live TCP stream

Checked against a packet capture of a real session (56 s, 4 frames):

- The IND is the TCP **client**. It connected from an ephemeral port to
  the base station on port **60015**, so `--listen 0.0.0.0:60015` (or
  whatever port the IND is configured with) is the mode to use. The
  capture starts mid-session, so the handshake itself is not in it; the
  port numbers are what show the direction.
- The receiving side sent **no application data** back, only TCP
  acknowledgements. There is no login or keep-alive to implement.
- Lines end in a bare LF, sent as its own one-byte segment after each
  record. There are no `.` separator lines in this capture; the reader
  still ignores them if a different firmware sends them.
- All four frames pass `--verify`, and the readings decode (site 735
  stage 212.32).

## What the datagram carries

The same keys the decoder sends, with `mer_db`, `snr_db` and
`framesync_hd` null (an IND does not report them), plus `noise_level`
(the IND's NLVL), `rs_corrections` (summed over the frame's blocks),
`receiver: "ind-ascii"` and `local_origin`. In the hub an IND copy has
no MER, so a decoder's copy of the same frame wins the merge; the IND is
still credited with hearing it.
