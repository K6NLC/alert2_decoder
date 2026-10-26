# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for alert2.devices — device discovery and listing.

Most tests use mock subprocess output to verify parsing and formatting
without requiring actual hardware.
"""
import sys
from unittest import mock

import pytest

from alert2.devices import (
    AlsaDevice, RtlSdrDevice,
    discover_alsa_devices, discover_rtlsdr_devices,
    format_device_listing, list_devices_and_exit,
)


# ---------------------------------------------------------------------------
# ALSA parsing
# ---------------------------------------------------------------------------

ARECORD_L_TWO_DEVICES = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC3253 Analog [ALC3253 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 4: U0x46d0x82d [USB Cam], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

ARECORD_L_NO_DEVICES = """\
arecord: device_list:268: no soundcards found...
"""


def _mock_run(stdout='', stderr='', returncode=0):
    """Build a fake subprocess.CompletedProcess result."""
    result = mock.Mock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def test_discover_alsa_parses_two_devices():
    with mock.patch('alert2.devices.shutil.which', return_value='/usr/bin/arecord'):
        with mock.patch('alert2.devices.subprocess.run',
                         return_value=_mock_run(stdout=ARECORD_L_TWO_DEVICES)):
            devs = discover_alsa_devices()
    assert len(devs) == 2
    assert devs[0].card_num == 0
    assert devs[0].device_num == 0
    assert devs[0].plughw_id == 'plughw:0,0'
    assert 'HDA Intel' in devs[0].card_name
    assert devs[1].card_num == 4
    assert devs[1].plughw_id == 'plughw:4,0'
    assert 'USB Cam' in devs[1].card_name


def test_discover_alsa_returns_empty_when_arecord_missing():
    with mock.patch('alert2.devices.shutil.which', return_value=None):
        devs = discover_alsa_devices()
    assert devs == []


def test_discover_alsa_returns_empty_when_no_soundcards():
    with mock.patch('alert2.devices.shutil.which', return_value='/usr/bin/arecord'):
        with mock.patch('alert2.devices.subprocess.run',
                         return_value=_mock_run(stdout=ARECORD_L_NO_DEVICES)):
            devs = discover_alsa_devices()
    assert devs == []


def test_discover_alsa_handles_subprocess_timeout():
    import subprocess as subp
    with mock.patch('alert2.devices.shutil.which', return_value='/usr/bin/arecord'):
        with mock.patch('alert2.devices.subprocess.run',
                         side_effect=subp.TimeoutExpired('arecord', 5)):
            devs = discover_alsa_devices()
    assert devs == []


# ---------------------------------------------------------------------------
# RTL-SDR parsing
# ---------------------------------------------------------------------------

RTL_TEST_ONE_DEVICE = """\
Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

Using device 0: Generic RTL2832U OEM
"""

RTL_TEST_TWO_DEVICES = """\
Found 2 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
  1:  Realtek, RTL2838UHIDIR, SN: 00000002

Using device 0: Generic RTL2832U OEM
"""

RTL_TEST_NO_DEVICES = """\
No supported devices found.
"""


def test_discover_rtlsdr_parses_one_device():
    with mock.patch('alert2.devices.shutil.which', return_value='/usr/bin/rtl_test'):
        with mock.patch('alert2.devices.subprocess.run',
                         return_value=_mock_run(stderr=RTL_TEST_ONE_DEVICE)):
            devs = discover_rtlsdr_devices()
    assert len(devs) == 1
    assert devs[0].index == 0
    assert 'Realtek' in devs[0].name
    assert devs[0].serial == '00000001'


def test_discover_rtlsdr_parses_two_devices():
    with mock.patch('alert2.devices.shutil.which', return_value='/usr/bin/rtl_test'):
        with mock.patch('alert2.devices.subprocess.run',
                         return_value=_mock_run(stderr=RTL_TEST_TWO_DEVICES)):
            devs = discover_rtlsdr_devices()
    assert len(devs) == 2
    assert devs[0].serial == '00000001'
    assert devs[1].serial == '00000002'


def test_discover_rtlsdr_returns_empty_when_tool_missing():
    with mock.patch('alert2.devices.shutil.which', return_value=None):
        devs = discover_rtlsdr_devices()
    assert devs == []


# ---------------------------------------------------------------------------
# Format / listing
# ---------------------------------------------------------------------------

def test_format_listing_with_alsa_only():
    alsa = [AlsaDevice(0, 'TestCard', 0, 'TestDev')]
    out = format_device_listing(alsa, [])
    # Header
    assert 'Audio and SDR Device Discovery' in out
    # The device shows up
    assert 'plughw:0,0' in out
    assert 'TestCard' in out
    # ALSA example is present
    assert 'arecord -D plughw:0,0' in out
    assert 'python3 -m alert2 --stdin' in out


def test_format_listing_with_rtlsdr_only():
    rtl = [RtlSdrDevice(0, 'Realtek RTL2838UHIDIR')]
    out = format_device_listing([], rtl)
    assert 'rtl_fm -d 0' in out
    # The 171.850M example freq is in there (this is SLO County's freq;
    # documented as a placeholder users should change)
    assert '171.850M' in out


def test_format_listing_with_both_includes_cross_source_example():
    alsa = [AlsaDevice(0, 'TestCard', 0, 'TestDev')]
    rtl = [RtlSdrDevice(0, 'Realtek')]
    out = format_device_listing(alsa, rtl)
    # The cross-source example should be present when both are available
    assert 'cross-source comparison' in out.lower() or (
        'arecord' in out and 'rtl_fm' in out)


def test_format_listing_with_two_alsa_includes_parallel_example():
    alsa = [
        AlsaDevice(0, 'CardA', 0, 'DevA'),
        AlsaDevice(4, 'CardB', 0, 'DevB'),
    ]
    out = format_device_listing(alsa, [])
    # Should mention both plughw IDs
    assert 'plughw:0,0' in out
    assert 'plughw:4,0' in out
    # Parallel example for A/B comparison
    assert 'parallel' in out.lower() or 'A/B' in out


def test_format_listing_with_nothing_gives_helpful_message():
    out = format_device_listing([], [])
    # Should suggest re-running after a device is connected
    assert 're-run' in out.lower() or 'connect' in out.lower()


def test_list_devices_and_exit_prints_to_stdout(capsys):
    """End-to-end smoke: the function runs without crashing and prints
    something to stdout."""
    with mock.patch('alert2.devices.discover_alsa_devices', return_value=[]):
        with mock.patch('alert2.devices.discover_rtlsdr_devices', return_value=[]):
            list_devices_and_exit()
    captured = capsys.readouterr()
    assert 'Audio and SDR Device Discovery' in captured.out


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
