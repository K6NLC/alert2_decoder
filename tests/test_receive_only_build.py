# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Guards for the receive-only (Android app) build.

* resample_poly_np must agree with scipy.signal.resample_poly -- it is the
  only SciPy function on the streaming receive path.
* the decoder must import without the iterative / SOVA modules and without
  SciPy, and refuse iterative modes cleanly when they are absent.
* stream_kwargs_from_args must keep mapping the CLI's --stdin flags.
"""
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
ROOT = os.path.join(os.path.dirname(__file__), '..')


@pytest.mark.parametrize('up,down', [(9, 10), (10, 9), (147, 160), (3, 1),
                                     (1, 4)])
@pytest.mark.parametrize('n', [1, 9, 10, 1001, 48000])
def test_resample_np_matches_scipy(up, down, n):
    sig = pytest.importorskip('scipy.signal')
    from alert2.demod import resample_poly_np
    rng = np.random.default_rng(n * 31 + up)
    for dt, tol in ((np.float64, 1e-12), (np.float32, 2e-6)):
        x = rng.normal(size=n).astype(dt)
        a = sig.resample_poly(x, up, down)
        b = resample_poly_np(x, up, down)
        assert a.shape == b.shape and a.dtype == b.dtype
        assert np.abs(a - b).max() <= tol * max(np.abs(a).max(), 1e-30)


def test_imports_without_scipy_sova_iterative():
    code = textwrap.dedent('''
        import sys
        for m in ('scipy', 'alert2.iterative', 'alert2.iterative_sova',
                  'alert2.sova'):
            sys.modules[m] = None          # import -> ImportError
        sys.path.insert(0, %r)
        from alert2.stream import stream_decode
        from alert2.decoder import Alert2Decoder
        from alert2.demod import resample_to_rx_rate
        import numpy as np
        assert len(resample_to_rx_rate(np.zeros(4800, np.float32), 48000)) == 4320
        Alert2Decoder(iterative='off')
        for mode in ('demod', 'sova'):
            try:
                Alert2Decoder(iterative=mode)
            except RuntimeError:
                pass
            else:
                raise SystemExit('iterative=%%s accepted without module' %% mode)
        print('ok')
    ''' % os.path.abspath(ROOT))
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True)
    assert r.returncode == 0 and r.stdout.strip() == 'ok', r.stderr


def test_stream_kwargs_from_recommended_flags():
    from alert2 import cli
    argv = ['--stdin', '--multiphase', '--phase-candidates', '9',
            '--phase-select', '--phase-block-scan', '--eq-retry',
            '--presync-eq', '--candidate-gate', '0.40', '--max-fs-hd', '3',
            '--chunk-seconds', '6', '--overlap-seconds', '3',
            '--mer-mode', 'full', '--timing-probe', '--rate-track']
    kw = cli.stream_kwargs_from_args(
        cli.resolve_args(cli.build_parser().parse_args(argv)))
    assert kw['multiphase'] and kw['phase_select'] and kw['phase_block_scan']
    assert kw['eq_retry'] and kw['presync_eq'] and kw['rate_track']
    assert kw['phase_candidates'] == 9 and kw['max_framesync_hd'] == 3
    assert kw['candidate_gate'] == 0.40 and kw['overlap_seconds'] == 3
    assert kw['iterative'] == 'off' and kw['burst_mode'] is False
    import inspect
    from alert2.stream import stream_decode
    params = inspect.signature(stream_decode).parameters
    assert set(kw) <= set(params), set(kw) - set(params)
