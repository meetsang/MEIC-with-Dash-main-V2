"""Tests for persistent strategy and sub-strategy execution modes (Live vs Paper) across sessions."""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from common.broker_factory import is_strategy_paper
from common.daily_overrides import effective_config, load_session_state, set_overrides
from common.manual_close import close_structure
from common.strategy_modes import (
    delete_strategy_paper_mode,
    get_persistent_modes_for_strategy,
    get_strategy_paper_mode,
    load_strategy_modes,
    save_strategy_modes,
    set_strategy_paper_mode,
)
from strategies.drma_hm.config import DRMAHMConfig
from strategies.drma_hm.strategy import DRMAHMStrategy
from strategies.loader import _build_config


class TestStrategyModesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_set_and_get_strategy_paper_mode(self):
        # Default should be None if no persistent setting
        self.assertIsNone(get_strategy_paper_mode('SKYLINE_SPX', root=self.tmp))

        # Set Skyline to live (paper=False)
        set_strategy_paper_mode('SKYLINE_SPX', paper=False, root=self.tmp)
        self.assertFalse(get_strategy_paper_mode('SKYLINE_SPX', root=self.tmp))

        # Change back to paper
        set_strategy_paper_mode('SKYLINE_SPX', paper=True, root=self.tmp)
        self.assertTrue(get_strategy_paper_mode('SKYLINE_SPX', root=self.tmp))

    def test_drma_sub_strategy_independent_modes(self):
        # Set DRMA master to paper=False (live)
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_SPX', root=self.tmp))
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_DIR', root=self.tmp))
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_NONDIR', root=self.tmp))

        # Set only directional futures to paper=True while keeping options live
        set_strategy_paper_mode('DRMA_HM_FUT', paper=True, root=self.tmp)
        self.assertTrue(get_strategy_paper_mode('DRMA_HM_FUT', root=self.tmp))
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_DIR', root=self.tmp))
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_NONDIR', root=self.tmp))

        # Check raw persistent modes dict
        modes = get_persistent_modes_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertFalse(modes['paper'])
        self.assertTrue(modes['futures_paper'])
        self.assertFalse(modes['dir_paper'])
        self.assertFalse(modes['non_dir_paper'])
        self.assertFalse(get_strategy_paper_mode('DRMA_HM_DIR', root=self.tmp))

    def test_delete_mode_reverts_to_default(self):
        set_strategy_paper_mode('PERMA_BULL_PCS', paper=False, root=self.tmp)
        self.assertFalse(get_strategy_paper_mode('PERMA_BULL_PCS', root=self.tmp))

        delete_strategy_paper_mode('PERMA_BULL_PCS', key='paper', root=self.tmp)
        self.assertIsNone(get_strategy_paper_mode('PERMA_BULL_PCS', root=self.tmp))

    def test_is_strategy_paper_integration(self):
        # Persist DRMA to live in tmp
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)
        modes_file = os.path.join(self.tmp, 'trades', 'strategy_modes.json')
        with patch('common.strategy_modes.strategy_modes_path', return_value=modes_file):
            with patch('common.broker_factory.os.path.abspath', return_value=self.tmp):
                self.assertFalse(is_strategy_paper('DRMA_HM_SPX'))
                self.assertFalse(is_strategy_paper('DRMA_HM_DIR'))
                self.assertFalse(is_strategy_paper('DRMA_HM_NONDIR'))

    def test_effective_config_applies_persistent_mode_on_new_day(self):
        # Persist DRMA live
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)
        set_strategy_paper_mode('DRMA_HM_DIR', paper=False, root=self.tmp)
        set_strategy_paper_mode('DRMA_HM_NONDIR', paper=False, root=self.tmp)

        base_cfg = DRMAHMConfig(name='DRMA_HM_SPX', paper=True, dir_paper=True, non_dir_paper=True)
        eff = effective_config(base_cfg, 'DRMA_HM_SPX', root=self.tmp)
        self.assertFalse(eff.paper)
        self.assertFalse(eff.dir_paper)
        self.assertFalse(eff.non_dir_paper)

    def test_build_config_loads_persisted_mode(self):
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)
        set_strategy_paper_mode('DRMA_HM_DIR', paper=False, root=self.tmp)

        with patch('strategies.loader.ROOT', self.tmp):
            entry = {
                'name': 'DRMA_HM_SPX',
                'paper': True,
                'config_overrides': {'dir_paper': True, 'non_dir_paper': True},
            }
            cfg = _build_config(entry, DRMAHMConfig)
            self.assertFalse(cfg.paper)
            self.assertFalse(cfg.dir_paper)

    def test_load_session_state_seeds_overrides_on_fresh_day(self):
        # Persist DRMA master to live
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)

        # On a fresh day with no session file created yet
        state = load_session_state('DRMA_HM_SPX', self.tmp)
        ov = state.get('overrides', {})
        self.assertFalse(ov['paper'])
        self.assertFalse(ov['dir_paper'])
        self.assertFalse(ov['non_dir_paper'])
        self.assertFalse(ov['futures_paper'])

    def test_fresh_day_independent_sub_modes(self):
        # Persist independent modes: dir live, nondir paper, futures live
        set_strategy_paper_mode('DRMA_HM_DIR', paper=False, root=self.tmp)
        set_strategy_paper_mode('DRMA_HM_NONDIR', paper=True, root=self.tmp)
        set_strategy_paper_mode('DRMA_HM_FUT', paper=False, root=self.tmp)

        state = load_session_state('DRMA_HM_SPX', self.tmp)
        ov = state.get('overrides', {})
        self.assertFalse(ov['dir_paper'])
        self.assertTrue(ov['non_dir_paper'])
        self.assertFalse(ov['futures_paper'])

        base_cfg = DRMAHMConfig(name='DRMA_HM_SPX', paper=True, dir_paper=True, non_dir_paper=True, futures_paper=True)
        eff = effective_config(base_cfg, 'DRMA_HM_SPX', root=self.tmp)
        self.assertFalse(eff.dir_paper)
        self.assertTrue(eff.non_dir_paper)
        self.assertFalse(eff.futures_paper)

    def test_drma_parent_toggle_cascades_and_persists_across_sessions(self):
        # Master toggle to live
        set_strategy_paper_mode('DRMA_HM_SPX', paper=False, root=self.tmp)
        modes = get_persistent_modes_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertFalse(modes['paper'])
        self.assertFalse(modes['dir_paper'])
        self.assertFalse(modes['non_dir_paper'])
        self.assertFalse(modes['futures_paper'])

        # Master toggle back to paper
        set_strategy_paper_mode('DRMA_HM_SPX', paper=True, root=self.tmp)
        modes = get_persistent_modes_for_strategy('DRMA_HM_SPX', root=self.tmp)
        self.assertTrue(modes['paper'])
        self.assertTrue(modes['dir_paper'])
        self.assertTrue(modes['non_dir_paper'])
        self.assertTrue(modes['futures_paper'])

    def test_trade_provenance_safety_manual_close(self):
        """A paper trade must NEVER be closed via live broker, even if strategy is flipped to LIVE."""
        active_dir = os.path.join(self.tmp, 'trades', 'active', 'DRMA_HM_SPX')
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, 'drma_hm_2026-10-20_test.json')

        trade_data = {
            'trade_id': 'test_trade_123',
            'strategy': 'DRMA_HM_SPX',
            'sub_strategy': 'DRMA_HM_NONDIR',
            'status': 'open',
            'paper': True,  # Entered on paper!
            'structures': [{
                'structure_id': 'spread_1',
                'name': 'Credit Spread',
                'status': 'open',
                'legs': [
                    {'symbol': '.SPXW261020C5800', 'action': 'SELL_TO_OPEN', 'qty': 1},
                    {'symbol': '.SPXW261020C5810', 'action': 'BUY_TO_OPEN', 'qty': 1},
                ],
            }],
        }
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f)

        mock_paper_broker = MagicMock(_is_paper=True)
        mock_paper_broker.fetch_option_mids_api.return_value = {'.SPXW261020C5800': 1.0, '.SPXW261020C5810': 0.2}
        mock_paper_broker.place_multi_leg_order.return_value = MagicMock(success=True, order_id='journal_close_123')

        mock_live_broker = MagicMock(_is_paper=False)

        def mock_get_broker(strat, paper=True):
            return mock_paper_broker if paper else mock_live_broker

        with patch('common.manual_close.get_shared_broker_for_strategy', side_effect=mock_get_broker):
            # Attempt close while passing paper=False (strategy is live!)
            res = close_structure('DRMA_HM_SPX', 'spread_1', root=self.tmp, paper=False, trade_id='test_trade_123')

            self.assertTrue(res.success)
            # VERIFY: Paper broker was used despite paper=False passed! Live broker NEVER touched!
            mock_paper_broker.place_multi_leg_order.assert_called_once()
            mock_live_broker.place_multi_leg_order.assert_not_called()

    def test_trade_provenance_safety_strategy_exit(self):
        """DRMAHMStrategy._exit_trade must honor trade['paper'] and route to paper broker."""
        active_dir = os.path.join(self.tmp, 'trades', 'active', 'DRMA_HM_SPX')
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, 'drma_hm_2026-10-20_test2.json')

        trade_data = {
            'trade_id': 'test_trade_456',
            'strategy': 'DRMA_HM_SPX',
            'sub_strategy': 'DRMA_HM_NONDIR',
            'status': 'open',
            'paper': True,  # Entered on paper!
            'structures': [{
                'structure_id': 'spread_1',
                'name': 'Credit Spread',
                'status': 'open',
                'legs': [
                    {'symbol': '.SPXW261020C5800', 'action': 'SELL_TO_OPEN', 'qty': 1},
                    {'symbol': '.SPXW261020C5810', 'action': 'BUY_TO_OPEN', 'qty': 1},
                ],
            }],
        }
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f)

        # Strategy configured as LIVE (paper=False)
        cfg = DRMAHMConfig(root=self.tmp, paper=False, non_dir_paper=False)
        strat = DRMAHMStrategy(cfg)

        mock_paper_broker = MagicMock(_is_paper=True, _is_journal=True)
        mock_live_broker = MagicMock(_is_paper=False, _is_journal=False)

        strat._paper_broker = mock_paper_broker
        strat._live_broker = mock_live_broker

        strat._exit_trade(self.tmp, trade_path, reason='profit_target_hit')

        # Paper broker received simulated order, live broker was never touched
        mock_paper_broker.place_multi_leg_order.assert_called_once()
        mock_live_broker.place_multi_leg_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
