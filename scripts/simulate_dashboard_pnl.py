#!/usr/bin/env python3
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
from dashboard.server import build_summary, live_prices, TOPIC_PREFIX

live_prices.clear()
live_prices[TOPIC_PREFIX + 'SPX'] = 7505.0
for sym in (
    '.SPXW260701C7530', '.SPXW260701C7520', '.SPXW260701C7540',
    '.SPXW260701C7565', '.SPXW260701C7560', '.SPXW260701C7535',
):
    live_prices[TOPIC_PREFIX + sym] = 7505.0

s = build_summary()
print('meic_pnl', s['meic_pnl'], 'manual_pnl', s['manual_pnl'], 'combined', s['combined_pnl'])
for row in s['grid']:
    if row.get('live_pnl'):
        print(row['slot_key'], row.get('state'), row.get('session_state'), 'pnl', row['live_pnl'], 'cur', row.get('cur_short'), row.get('cur_long'))
