"""MEIC entry scan pick retries on transient empty scan."""
from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.meic_worker import SCAN_PICK_RETRIES, _scan_pick
from blocks.entry.spread_scan import SpreadCandidate


class TestMeicScanRetry(unittest.TestCase):
    def test_scan_pick_retries_until_success(self):
        entry = MagicMock()
        clean = SpreadCandidate(
            short_symbol='.SPXW260629C7450',
            long_symbol='.SPXW260629C7475',
            short_strike=7450,
            long_strike=7475,
            market_credit=0.80,
            short_mid=0.97,
            long_mid=0.17,
        )
        entry.scan_for_meic.side_effect = [[], [], [clean]]

        log = logging.getLogger('test')
        with patch('blocks.entry.meic_worker.time.sleep'):
            pick = _scan_pick(entry, 'C', '260629', '01-45', log)

        self.assertEqual(pick.short_strike, 7450)
        self.assertEqual(entry.scan_for_meic.call_count, 3)

    def test_scan_pick_raises_after_max_retries(self):
        entry = MagicMock()
        entry.scan_for_meic.return_value = []

        import meic0dte.app.utilities as util

        log = logging.getLogger('test')
        with patch('blocks.entry.meic_worker.time.sleep'):
            with self.assertRaises(util.TerminateRequest) as ctx:
                _scan_pick(entry, 'C', '260629', '01-45', log)

        self.assertIn('empty scan', str(ctx.exception))
        self.assertEqual(entry.scan_for_meic.call_count, SCAN_PICK_RETRIES)


if __name__ == '__main__':
    unittest.main()
