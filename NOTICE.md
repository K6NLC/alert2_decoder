# Third-party components and notices

`alert2` is Copyright (C) 2026 Nicholas Collins (K6NLC) and is licensed
under the GNU General Public License, version 3 or (at your option) any
later version. See [LICENSE](LICENSE).

This repository contains **no third-party source code**. The components
below are used at runtime. They are installed separately and are not
redistributed here.

| Component | How it is used | License |
|---|---|---|
| [NumPy](https://numpy.org/) | Python dependency | BSD-3-Clause (with 0BSD, MIT, Zlib, CC0-1.0 parts) |
| [SciPy](https://scipy.org/) | Python dependency | BSD-3-Clause |
| libfec (Phil Karn, KA9Q) | Shared library, loaded at runtime through `ctypes` for Reed-Solomon | LGPL-2.1 |
| [rtl-sdr](https://osmocom.org/projects/rtl-sdr) | External program (`rtl_fm`), audio piped to stdin | GPL-2.0-or-later |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) | Optional, transmit playback | MIT |
| [pySerial](https://github.com/pyserial/pyserial) | Optional, serial RTS/DTR PTT keying | BSD-3-Clause |
| [gpiozero](https://github.com/gpiozero/gpiozero) / [RPi.GPIO](https://pypi.org/project/RPi.GPIO/) | Optional, GPIO PTT keying | BSD-3-Clause / MIT |

All of these licenses are compatible with GPL-3.0-or-later.

## Offline install bundles

`tools/build_offline_bundle.sh` collects third-party `.deb` packages
(rtl-sdr, libfec, NumPy, SciPy and their libraries) into a local bundle.
Some of those packages are GPL- or LGPL-licensed binaries, so a bundle
you **share publicly** (for example as a GitHub release asset) has to
meet those packages' source-availability terms. Bundles for your own
machines are fine. That is why bundles are excluded by `.gitignore`.

## Protocol specifications

This is an independent implementation of the ALERT2 AirLink, MANT and
Application Layer specifications published by the
[National Hydrologic Warning Council](https://hydrologicwarning.org/).
The specifications themselves are not included. Get them from the NHWC.
The project is not affiliated with or endorsed by the NHWC or by any
equipment vendor.
