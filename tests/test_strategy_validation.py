"""G9 — strategies.yaml startup validation."""
from __future__ import annotations

import unittest
from unittest import mock

from strategies.validate import StrategyConfigError, validate_startup_config


class TestStrategyValidation(unittest.TestCase):
    def test_valid_config_passes(self):
        enabled = validate_startup_config()
        names = {e['name'] for e in enabled}
        self.assertIn('MEIC_IC', names)
        self.assertIn('MANUAL_SPREAD', names)

    def test_unknown_stop_profile_raises(self):
        bad = [{
            'name': 'BAD',
            'enabled': True,
            'module': 'strategies.meic.strategy',
            'broker': 'tastytrade',
            'stop_profile': 'nonexistent_profile',
        }]
        with mock.patch('strategies.validate.load_strategies_yaml_raw', return_value=bad):
            with self.assertRaises(StrategyConfigError):
                validate_startup_config()

    def test_paper_strategy_uses_journal_without_tastyware_key(self):
        entries = [
            {
                'name': 'MEIC_IC',
                'enabled': True,
                'module': 'strategies.meic.strategy',
                'broker': 'tastytrade',
                'stop_profile': 'meic_credit_spread',
            },
            {
                'name': 'SKYLINE_SPX',
                'enabled': True,
                'confirmed': True,
                'module': 'strategies.skyline.strategy',
                'broker': 'tastytrade',
                'stop_profile': 'skyline_hold_to_expiry',
                'scheduled': False,
                'paper': True,
            },
        ]
        with mock.patch('strategies.validate.load_strategies_yaml_raw', return_value=entries), mock.patch(
            'strategies.validate.tt_config.TASTYWARE_API_KEY', ''
        ), mock.patch('strategies.validate.tt_config.TT_REFRESH_TOKEN', 'rt'), mock.patch(
            'strategies.validate.tt_config.TT_CLIENT_SECRET', 'cs'
        ):
            enabled = validate_startup_config()
        names = {e['name'] for e in enabled}
        self.assertIn('MEIC_IC', names)
        self.assertIn('SKYLINE_SPX', names)


if __name__ == '__main__':
    unittest.main()
