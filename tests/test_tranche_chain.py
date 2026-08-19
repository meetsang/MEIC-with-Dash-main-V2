"""Tests for MEIC tranche chain prep."""
from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.tranche_chain import prepare_meic_tranche_chain, reset_prepared_for_tests


class TestTrancheChain(unittest.TestCase):
    def setUp(self):
        reset_prepared_for_tests()
        self.broker = MagicMock()
        self.log = logging.getLogger('test.tranche_chain')

    def tearDown(self):
        reset_prepared_for_tests()

    @patch('blocks.entry.tranche_chain.warm_chain_snapshot')
    @patch('blocks.entry.tranche_chain.util.central_date')
    def test_prepares_once_per_lot_per_day(self, mock_date, mock_warm):
        mock_date.return_value.isoformat.return_value = '2026-08-11'
        mock_warm.return_value = MagicMock()

        prepare_meic_tranche_chain(self.broker, '260811', '11-00', self.log, credit_min=0.60)
        prepare_meic_tranche_chain(self.broker, '260811', '11-00', self.log, credit_min=0.60)

        mock_warm.assert_called_once()
        args, kwargs = mock_warm.call_args
        self.assertEqual(args[1], '260811')
        self.assertEqual(kwargs['otm_range'], 300)

    @patch('blocks.entry.tranche_chain.warm_chain_snapshot')
    @patch('blocks.entry.tranche_chain.util.central_date')
    def test_different_lots_prepare_separately(self, mock_date, mock_warm):
        mock_date.return_value.isoformat.return_value = '2026-08-11'
        mock_warm.return_value = MagicMock()

        prepare_meic_tranche_chain(self.broker, '260811', '11-00', self.log)
        prepare_meic_tranche_chain(self.broker, '260811', '12-00', self.log)

        self.assertEqual(mock_warm.call_count, 2)


if __name__ == '__main__':
    unittest.main()
