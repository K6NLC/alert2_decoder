# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
devices.py — Audio and SDR device discovery for the alert2 CLI.

Used by the -d / --list-devices flag. Probes the system for:
  - ALSA audio capture devices (via `arecord -l` and `arecord -L`)
  - RTL-SDR devices (via `rtl_test`)

Each probe is best-effort: if the underlying tool isn't installed,
the relevant section is skipped with a brief note. Probes do not
modify state, do not open devices for capture, and do not block on
USB enumeration beyond what the probing tool naturally does.

Output is intended for human reading and includes ready-to-copy
example commands. The examples are suggestions; whether they actually
decode anything depends on whether the named device is connected to
a working ALERT2 source (a radio's discriminator output, an SDR tuned
to the right frequency, etc.) — the listing alone can't verify that.
"""

import re
import shutil
import subprocess
from typing import List, Optional


# ---------------------------------------------------------------------------
# ALSA audio capture device discovery
# ---------------------------------------------------------------------------

class AlsaDevice:
    """One ALSA capture device discovered via arecord."""
    __slots__ = ('card_num', 'card_name', 'device_num', 'device_name',
                 'plughw_id', 'hw_id')

    def __init__(self, card_num: int, card_name: str,
                 device_num: int, device_name: str):
        self.card_num = card_num
        self.card_name = card_name
        self.device_num = device_num
        self.device_name = device_name
        self.hw_id = f'hw:{card_num},{device_num}'
        self.plughw_id = f'plughw:{card_num},{device_num}'

    def __repr__(self):
        return (f'AlsaDevice({self.plughw_id}, card={self.card_name!r}, '
                f'device={self.device_name!r})')


def discover_alsa_devices() -> List[AlsaDevice]:
    """Run `arecord -l` and parse the output.

    Returns empty list if arecord is not installed or fails. The
    arecord output format:

        card 0: PCH [HDA Intel PCH], device 0: ALC3253 Analog [...]
        card 1: U0x46d0x82d [USB Cam], device 0: USB Audio [USB Audio]

    We parse with a single regex tolerant of name variations.
    """
    if not shutil.which('arecord'):
        return []
    try:
        result = subprocess.run(
            ['arecord', '-l'],
            capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    devices = []
    # arecord -l line format:
    #   card N: <short_id> [<long_name>], device M: <short_id> [<long_name>]
    # The short_id is no-spaces, but long_name in brackets can contain
    # spaces. Device short_id can also contain a space (e.g.,
    # "ALC3253 Analog"), so use .+? non-greedy for it, anchored by the
    # `[` that follows.
    line_re = re.compile(
        r'^card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\],\s+'
        r'device\s+(\d+):\s+(.+?)\s+\[([^\]]+)\]')
    for line in result.stdout.splitlines():
        m = line_re.match(line)
        if m:
            card_num = int(m.group(1))
            # Prefer the long human-readable name (bracketed group) over
            # the short identifier. The short form often has unhelpful
            # things like 'U0x46d0x82d'.
            card_name = m.group(3).strip()
            device_num = int(m.group(4))
            device_name = m.group(6).strip()
            devices.append(AlsaDevice(card_num, card_name,
                                       device_num, device_name))
    return devices


# ---------------------------------------------------------------------------
# RTL-SDR device discovery
# ---------------------------------------------------------------------------

class RtlSdrDevice:
    """One RTL-SDR device discovered via rtl_test."""
    __slots__ = ('index', 'name', 'serial')

    def __init__(self, index: int, name: str, serial: str = ''):
        self.index = index
        self.name = name
        self.serial = serial

    def __repr__(self):
        return f'RtlSdrDevice(#{self.index}, {self.name!r})'


def discover_rtlsdr_devices() -> List[RtlSdrDevice]:
    """Probe RTL-SDR devices via `rtl_test -t`.

    `rtl_test -t` lists devices then exits (the -t selects the
    tuner-test mode but it still enumerates first). We just read the
    enumeration output and parse it. If the tool isn't installed,
    return empty.

    Sample rtl_test output:
        Found 1 device(s):
          0:  Realtek, RTL2838UHIDIR, SN: 00000001
    """
    if not shutil.which('rtl_test'):
        return []
    try:
        # rtl_test -t exits after a short tuner test. -d 0 picks
        # device 0 but the "Found N device(s)" enumeration always
        # appears first. We give it 6 seconds — usually finishes in 2.
        result = subprocess.run(
            ['rtl_test', '-t'],
            capture_output=True, text=True, timeout=6)
    except (subprocess.TimeoutExpired, OSError):
        # Timeout might mean we got the enumeration but the test
        # hung; the stderr may still have the device list.
        # Fall through: try parsing whatever we got.
        try:
            stderr = result.stderr if 'result' in dir() else ''
        except UnboundLocalError:
            return []
    else:
        stderr = result.stderr

    # rtl_test writes the enumeration to stderr.
    devices = []
    in_list = False
    line_re = re.compile(r'^\s*(\d+):\s+(.+?)(?:,\s+SN:\s+(\S+))?\s*$')
    for line in stderr.splitlines():
        if 'Found' in line and 'device' in line:
            in_list = True
            continue
        if in_list:
            m = line_re.match(line)
            if m:
                idx = int(m.group(1))
                name = m.group(2).strip()
                serial = m.group(3) or ''
                devices.append(RtlSdrDevice(idx, name, serial))
            elif line.strip() == '':
                continue
            else:
                # End of the device list (next section)
                break
    return devices


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def format_device_listing(alsa_devices: List[AlsaDevice],
                           rtlsdr_devices: List[RtlSdrDevice]) -> str:
    """Format discovered devices for human reading, with examples.

    Returns a single multi-line string ready to print to stdout.
    """
    lines = []
    lines.append('=' * 70)
    lines.append('Audio and SDR Device Discovery')
    lines.append('=' * 70)
    lines.append('')

    # --- ALSA audio devices ---
    lines.append('ALSA capture devices (for radio discriminator audio):')
    lines.append('-' * 70)
    if alsa_devices:
        for d in alsa_devices:
            lines.append(f'  {d.plughw_id:<14s}  {d.card_name} — {d.device_name}')
    elif not shutil.which('arecord'):
        lines.append('  arecord is not installed. To enable ALSA support:')
        lines.append('    sudo apt install alsa-utils    # Debian/Ubuntu/Raspberry Pi OS')
    else:
        lines.append('  No ALSA capture devices found.')
        lines.append('  Check that a USB soundcard or onboard audio is connected.')
        lines.append('  Verify with:  arecord -l')
    lines.append('')

    # --- RTL-SDR devices ---
    lines.append('RTL-SDR devices (for direct RF reception):')
    lines.append('-' * 70)
    if rtlsdr_devices:
        for d in rtlsdr_devices:
            ser = f' SN:{d.serial}' if d.serial else ''
            lines.append(f'  rtl_fm -d {d.index}   {d.name}{ser}')
    elif not shutil.which('rtl_test'):
        lines.append('  rtl_test is not installed. To enable RTL-SDR support:')
        lines.append('    sudo apt install rtl-sdr       # Debian/Ubuntu/Raspberry Pi OS')
    else:
        lines.append('  No RTL-SDR devices found.')
        lines.append('  Check that the RTL-SDR USB stick is connected.')
        lines.append('  Verify with:  rtl_test -t')
    lines.append('')

    # --- Example commands ---
    lines.append('Example commands:')
    lines.append('-' * 70)

    if alsa_devices:
        lines.append('')
        lines.append('  # Live decode from a soundcard (radio discriminator audio):')
        d = alsa_devices[0]
        lines.append(f'  arecord -D {d.plughw_id} -f S16_LE -r 48000 -c 1 -t raw | \\')
        lines.append('      python3 -m alert2 --stdin \\')
        lines.append('          --iterative-mode demod \\')
        lines.append('          --diag-file alert2.diag \\')
        lines.append('          --web-ui')

        if len(alsa_devices) > 1:
            lines.append('')
            lines.append('  # Two soundcards in parallel (e.g., for A/B comparison):')
            d2 = alsa_devices[1]
            lines.append(f'  # Terminal 1: {d.plughw_id} ({d.card_name})')
            lines.append(f'  arecord -D {d.plughw_id} -f S16_LE -r 48000 -c 1 -t raw | \\')
            lines.append('      python3 -m alert2 --stdin --diag-file dev1.diag &')
            lines.append('')
            lines.append(f'  # Terminal 2: {d2.plughw_id} ({d2.card_name})')
            lines.append(f'  arecord -D {d2.plughw_id} -f S16_LE -r 48000 -c 1 -t raw | \\')
            lines.append('      python3 -m alert2 --stdin --diag-file dev2.diag &')

    if rtlsdr_devices:
        lines.append('')
        lines.append('  # Live decode from RTL-SDR (direct RF — change frequency to suit):')
        d = rtlsdr_devices[0]
        lines.append(f'  rtl_fm -d {d.index} -f 171.850M -s 48000 -M fm -E dc -g 30 - | \\')
        lines.append('      python3 -m alert2 --stdin \\')
        lines.append('          --iterative-mode demod \\')
        lines.append('          --diag-file alert2.diag \\')
        lines.append('          --web-ui')

        if len(rtlsdr_devices) > 1:
            lines.append('')
            lines.append('  # Two RTL-SDR sticks in parallel:')
            d2 = rtlsdr_devices[1]
            lines.append(f'  rtl_fm -d {d.index} -f 171.850M -s 48000 -M fm -E dc -g 30 - | \\')
            lines.append('      python3 -m alert2 --stdin --diag-file sdr0.diag &')
            lines.append(f'  rtl_fm -d {d2.index} -f 171.850M -s 48000 -M fm -E dc -g 30 - | \\')
            lines.append('      python3 -m alert2 --stdin --diag-file sdr1.diag &')

    if alsa_devices and rtlsdr_devices:
        lines.append('')
        lines.append('  # Soundcard + RTL-SDR in parallel (cross-source comparison):')
        a = alsa_devices[0]; r = rtlsdr_devices[0]
        lines.append(f'  arecord -D {a.plughw_id} -f S16_LE -r 48000 -c 1 -t raw | \\')
        lines.append('      python3 -m alert2 --stdin --diag-file audio.diag &')
        lines.append(f'  rtl_fm -d {r.index} -f 171.850M -s 48000 -M fm -E dc -g 30 - | \\')
        lines.append('      python3 -m alert2 --stdin --diag-file sdr.diag &')

    if not alsa_devices and not rtlsdr_devices:
        lines.append('  No audio or SDR devices detected — examples not generated.')
        lines.append('  Once a device is connected, re-run:  alert2 -d')

    lines.append('')
    lines.append('Notes:')
    lines.append('-' * 70)
    lines.append('  - Examples use --iterative-mode demod (Phase 1 iterative decoding).')
    lines.append('    Use --iterative-mode off for the standard soft-V baseline.')
    lines.append('  - Add --web-ui-bind 0.0.0.0 to make the web UI accessible from LAN.')
    lines.append('  - Add --rotate-daily for long-running captures (rolls logs at midnight).')
    lines.append('  - Add --log-file path.log for human-readable per-packet logging.')
    lines.append('  - Listed devices are detected by `arecord -l` and `rtl_test -t`.')
    lines.append('    A listed device is reachable, but the example only decodes')
    lines.append('    successfully if the device is connected to a working ALERT2 source')
    lines.append('    (radio discriminator output, SDR tuned to the right frequency).')
    lines.append('')

    return '\n'.join(lines)


def list_devices_and_exit():
    """Run the discovery and print results. Used by the -d CLI flag."""
    alsa = discover_alsa_devices()
    rtl = discover_rtlsdr_devices()
    print(format_device_listing(alsa, rtl))
