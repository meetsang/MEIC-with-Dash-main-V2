"""Tests for persistent strategy parameter overrides across sessions and days."""
import json
import os
import shutil
import tempfile
import unittest

from common.daily_overrides import delete_override, effective_config, load_session_state, set_overrides
from common.strategy_overrides import (
    delete_persistent_override_for_strategy,
    get_persistent_overrides_for_strategy,
    load_strategy_overrides,
    save_strategy_overrides,
    set_persistent_override_for_strategy,
)
from strategies.drma_hm.config import DRMAHMConfig


class TestStrategyOverridesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_set_and_get_persistent_overrides(self):
        updates = {
            'max_daily_signals': 60,
            'dir_max_daily_signals': 20,
            'non_dir_max_daily_signals': 20,
            'futures_max_daily_signals': 20,
            'non_dir_target_credit': 1.0,
            'dir_target_debit': 2.5,
        }
        set_persistent_override_for_strategy('DRMA_HM_SPX', updates, root=self.tmp)
        persisted = get_persistent_overrides_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertEqual(persisted['max_daily_signals'], 60)
        self.assertEqual(persisted['dir_max_daily_signals'], 20)
        self.assertEqual(persisted['non_dir_target_credit'], 1.0)

    def test_ephemeral_keys_not_persisted(self):
        updates = {
            'max_daily_signals': 60,
            'manual_onh': 5900.0,
            'manual_onl': 5850.0,
        }
        set_persistent_override_for_strategy('DRMA_HM_SPX', updates, root=self.tmp)
        persisted = get_persistent_overrides_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertEqual(persisted['max_daily_signals'], 60)
        self.assertNotIn('manual_onh', persisted)
        self.assertNotIn('manual_onl', persisted)

    def test_fresh_day_session_inherits_persistent_overrides(self):
        # 1. Set overrides on day 1
        set_overrides(
            'DRMA_HM_SPX',
            self.tmp,
            {'max_daily_signals': 60, 'dir_max_daily_signals': 20, 'quantity': 2},
            DRMAHMConfig,
        )

        # 2. Simulate next day: remove today's session file
        today_session_path = os.path.join(self.tmp, 'trades', 'active', 'DRMA_HM_SPX')
        for f in os.listdir(today_session_path):
            if f.startswith('session_'):
                os.remove(os.path.join(today_session_path, f))

        # 3. load_session_state on fresh day must carry forward overrides
        state = load_session_state('DRMA_HM_SPX', self.tmp)
        ov = state.get('overrides', {})
        self.assertEqual(ov.get('max_daily_signals'), 60)
        self.assertEqual(ov.get('dir_max_daily_signals'), 20)
        self.assertEqual(ov.get('quantity'), 2)

    def test_effective_config_preserves_overrides_without_reverting_to_4(self):
        set_overrides(
            'DRMA_HM_SPX',
            self.tmp,
            {'max_daily_signals': 60, 'dir_max_daily_signals': 20},
            DRMAHMConfig,
        )

        # Clear session file to simulate fresh date rollover
        today_session_path = os.path.join(self.tmp, 'trades', 'active', 'DRMA_HM_SPX')
        for f in os.listdir(today_session_path):
            if f.startswith('session_'):
                os.remove(os.path.join(today_session_path, f))

        cfg = DRMAHMConfig(name='DRMA_HM_SPX')
        eff = effective_config(cfg, 'DRMA_HM_SPX', self.tmp)
        self.assertEqual(eff.max_daily_signals, 60)
        self.assertEqual(eff.dir_max_daily_signals, 20)

    def test_delete_override_removes_from_persistent(self):
        set_overrides(
            'DRMA_HM_SPX',
            self.tmp,
            {'max_daily_signals': 60, 'quantity': 3},
            DRMAHMConfig,
        )
        delete_override('DRMA_HM_SPX', self.tmp, 'quantity')
        persisted = get_persistent_overrides_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertNotIn('quantity', persisted)
        self.assertEqual(persisted['max_daily_signals'], 60)


if __name__ == '__main__':
    unittest.main()
