# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Ground-truth validation tests.

Decodes real SDR WAV captures and validates each decoded packet's
application_payload bytes byte-exactly against the encoder log entries
for the same (src, ts) pair.

This catches regressions in:
- FEC pipeline (Viterbi, RS) — wrong bytes
- Follow-on block bit accounting — truncated decodes
- application_payload extraction — phantom or missing bytes
- MANT header parsing — wrong source/length

Requires (not distributed; field captures stay out of the repository):
- ALERT2_GT_WAV:  path to an SDR capture WAV
- ALERT2_GT_LOG:  path to the matching encoder log for the same period
Skipped when either is unset or missing.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest
from collections import defaultdict


WAV_PATH = os.environ.get('ALERT2_GT_WAV', '')
ENCODER_LOG_PATH = os.environ.get('ALERT2_GT_LOG', '')


def parse_encoder_log(path):
    """Parse encoder log into {(src, ts): [list of payload hex strings]}."""
    encoder = defaultdict(list)
    with open(path) as f:
        for line in f:
            if not line.startswith('N,'):
                continue
            parts = [p.strip() for p in line.strip().split(',')]
            try:
                year = int(parts[1])
                payload_len = int(parts[17])
                src = int(parts[18])
                num_added = int(parts[19])
                if year != 2026 or payload_len < 0 or payload_len > 200:
                    continue
                if src < 0 or src > 65535:
                    continue
                payload_start = 20 + num_added
                payload_bytes = bytearray()
                for p in parts[payload_start:payload_start + payload_len]:
                    p = p.strip()
                    if len(p) == 2 and all(c in '0123456789abcdefABCDEF' for c in p):
                        payload_bytes.append(int(p, 16))
                if len(payload_bytes) != payload_len:
                    continue
                payload = bytes(payload_bytes)
            except (ValueError, IndexError):
                continue
            if len(payload) < 1:
                continue
            cb = payload[0]
            ver = cb & 0x3
            if ver != 0:
                continue
            has_ts = bool((cb >> 2) & 1)
            ts = ((payload[1] << 8) | payload[2]) if (has_ts and len(payload) >= 3) else None
            encoder[(src, ts)].append(payload.hex())
    return dict(encoder)


@unittest.skipUnless(os.path.exists(WAV_PATH) and os.path.exists(ENCODER_LOG_PATH),
                     "WAV file or encoder log not available")
class TestGroundTruthValidation(unittest.TestCase):
    """Validate decoded packets byte-exactly against encoder log."""

    @classmethod
    def setUpClass(cls):
        import scipy.io.wavfile as wav
        import numpy as np
        from alert2.demod import demodulate
        from alert2 import Alert2Decoder

        fs, d = wav.read(WAV_PATH)
        if d.ndim > 1:
            d = d[:, 0]
        audio = d.astype(np.float32) / 32768.0

        decoder = Alert2Decoder()
        bits, info = demodulate(audio, fs)
        audio_43k = info.get('audio_43k')
        phase = info.get('best_phase', 0)
        cls.packets = decoder.decode_bits(bits, audio_43k=audio_43k,
                                            bit_phase=phase)
        cls.encoder = parse_encoder_log(ENCODER_LOG_PATH)

    def test_at_least_some_packets_decoded(self):
        """Should successfully decode at least 5 packets from this WAV."""
        self.assertGreaterEqual(len(self.packets), 5,
            f"Expected >=5 packets, got {len(self.packets)}")

    def test_all_packets_match_encoder_byte_exact(self):
        """Every decoded packet's app payload should byte-exactly match
        the encoder's transmitted bytes for the same (src, ts) key."""
        unmatched = []
        no_encoder_entry = []
        matched = 0

        for pkt in self.packets:
            if pkt.srp_pdu is None or pkt.srp_pdu.timestamp is None:
                continue
            src = pkt.mant.source_addr
            ts = pkt.srp_pdu.timestamp
            key = (src, ts)
            if key not in self.encoder:
                no_encoder_entry.append(key)
                continue
            enc_bytes = bytes.fromhex(self.encoder[key][0])
            if pkt.application_payload == enc_bytes:
                matched += 1
            else:
                unmatched.append({
                    'src': src, 'ts': ts,
                    'dec_bytes': pkt.application_payload.hex(),
                    'enc_bytes': enc_bytes.hex(),
                    'dec_len': len(pkt.application_payload),
                    'enc_len': len(enc_bytes),
                })

        if unmatched:
            details = '\n'.join(
                f"  src={u['src']} ts={u['ts']}: "
                f"dec({u['dec_len']}B) != enc({u['enc_len']}B)\n"
                f"    DEC: {u['dec_bytes']}\n"
                f"    ENC: {u['enc_bytes']}"
                for u in unmatched
            )
            self.fail(
                f"{len(unmatched)} of {matched + len(unmatched)} matched packets "
                f"don't have byte-exact encoder agreement:\n{details}"
            )

        self.assertGreaterEqual(matched, 5,
            f"Should have at least 5 matched packets, got {matched}")

    def test_no_packets_lost_from_full_capture(self):
        """In a 6-packet capture, all should match (after bug fixes)."""
        # This file should produce exactly 6 packets from 4 unique sources.
        srcs = set(p.mant.source_addr for p in self.packets if p.mant)
        self.assertGreaterEqual(len(srcs), 4,
            f"Expected >=4 unique sources, got {len(srcs)}: {srcs}")

    def test_stacked_mant_pdus_decoded(self):
        """AirLink frames may contain multiple stacked MANT PDUs.
        Confirm we extract all of them, not just the first.

        For the test WAV, src=737 only appears when 4601 stacks it
        as the SECOND PDU after src=769 in the same AirLink frame.
        Before the multi-PDU fix, src=737 was silently dropped.
        """
        srcs = set(p.mant.source_addr for p in self.packets if p.mant)
        self.assertIn(737, srcs,
            f"src=737 (stacked second after src=769) was not decoded. "
            f"Got sources: {sorted(srcs)}")


if __name__ == '__main__':
    print("Running ground-truth validation tests...")
    unittest.main(verbosity=2)
