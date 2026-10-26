# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Regression test: web UI templates must be read as UTF-8 regardless of
the system locale.

History: this bug has appeared TWICE.
  1. Production v0.8.5 needed a manual _read_template() UTF-8 patch
     because the deployment Pi's locale is ISO-8859-1.
  2. The v0.9.0 fork branched from v0.8.5 source (which never had the
     manual patch — it lived only on the Pi), silently reintroducing
     it. Caught visually in the web UI: "·" rendered as "Â·", "—" as
     "â€"" (mojibake).

Root cause: open() without encoding= uses the platform default. On the
Pi that's ISO-8859-1, so UTF-8 byte pairs in the templates are misread.

These tests pin TWO things:
  - _read_template returns proper Unicode with no mojibake, even when
    the process locale would otherwise default open() to Latin-1.
  - No text-mode open() anywhere in the package omits encoding= (the
    broader "audit all open() calls" item — prevents the same class of
    bug in log/CSV/JSONL/stats writers, which would mangle non-ASCII
    sensor descriptions on the Pi).
"""
import ast
import os
import glob

import pytest

from alert2.web import _read_template


ALERT2_PKG_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))) + "/alert2"


def test_templates_read_as_utf8_no_mojibake():
    for name in ("index.html", "source.html"):
        text = _read_template(name)
        # The separator the UI uses between path / SNR / hd fields.
        assert "\u00b7" in text, (
            f"{name}: U+00B7 middot missing — template not decoded as "
            f"UTF-8 (locale-dependent open() bug)")
        # The signature mojibake: U+00C2 (Â) immediately before U+00B7,
        # which is what UTF-8 'C2 B7' becomes when read as Latin-1.
        assert "\u00c2\u00b7" not in text, (
            f"{name}: mojibake 'Â·' present — _read_template is not "
            f"reading UTF-8")
        # Em dash used in the header/uptime placeholder.
        assert "\u2014" in text, f"{name}: em-dash U+2014 missing"
        # Its Latin-1 misread is 'â€"' (â = U+00E2).
        assert "\u00e2\u20ac" not in text, (
            f"{name}: em-dash mojibake present")


def test_read_template_signature_specifies_encoding():
    """Static guard: the open() inside _read_template must pass
    encoding=. Cheaper than spawning a Latin-1 subprocess and catches
    a regression even on a UTF-8 dev box (where behaviour would
    otherwise mask it)."""
    src = open(os.path.join(ALERT2_PKG_DIR, "web.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_read_template"):
            found = True
            opens = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)
                     and n.func.id == "open"]
            assert opens, "_read_template no longer calls open()?"
            for call in opens:
                kwargs = {kw.arg for kw in call.keywords}
                assert "encoding" in kwargs, (
                    "_read_template open() is missing encoding= — "
                    "locale-dependent bug will reappear on the Pi")
    assert found, "_read_template not found in web.py"


def test_no_textmode_open_without_encoding_in_package():
    """Broader audit (primer pending item): every text-mode open() in
    the package must specify encoding=. Binary opens ('rb'/'wb') and
    non-builtin .open() are exempt.
    """
    offenders = []
    for pyfile in glob.glob(os.path.join(ALERT2_PKG_DIR, "*.py")):
        src = open(pyfile, encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                continue
            # Determine mode (2nd positional or mode= kwarg)
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            is_binary = mode is not None and "b" in mode
            if is_binary:
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "encoding" not in kwargs:
                offenders.append(
                    f"{os.path.basename(pyfile)}:{node.lineno}")
    assert not offenders, (
        "text-mode open() without encoding= (locale-dependent bug "
        f"risk on the Pi): {offenders}")
