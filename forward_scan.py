# ==========================================
# FILE: forward_scan.py
#
# Runs the Malaysian SNR strategy against LIVE Bybit data, forward from the
# moment this was first run — not a replay of history. Every run:
#   1. Pulls recent 4H candles for ETH/USDT and SOL/USDT
#   2. Detects any CHoCH forming AFTER the forward-test start time
#   3. Waits for a retest fill (same as the backtest engine) before counting
#      anything as a real trade
#   4. Resolves trades that have hit stop/target/breakeven — but anything
#      that simply hasn't had enough real time pass yet is reported as
#      still OPEN/PENDING, never force-resolved early
#   5. Regenerates a static dashboard (index.html + history.html) and saves
#      state.json so the next run picks up exactly where this one left off
#
# This script is meant to be run on a schedule by GitHub Actions — see
# .github/workflows/forward-scan.yml
# ==========================================
import ccxt
import pandas as pd
import json
import os
from datetime import datetime, timezone
from jinja2 import Template

# ---------- Configuration ----------
STARTING_BALANCE = 62.0
TRADE_MODE = "multi"          # "single" = 1 concurrent trade, "multi" = up to 3
BREAKEVEN_ENABLED = False
PAIRS = ["ETH/USDT", "SOL/USDT"]
LOOKBACK_DAYS = 60             # candles fetched each run — plenty for the 6-candle structure window
BREAKEVEN_TRIGGER_R = 1.3
LIMIT_FILL_WINDOW = 6          # candles to wait for a retest (24h on 4H)
EXIT_WINDOW = 10                # candles to wait for stop/target before calling it expired

TRADE_MODE_LIMITS = {"single": 1, "multi": 3}
MAX_CONCURRENT = TRADE_MODE_LIMITS.get(TRADE_MODE, 1)

STATE_PATH = "state.json"
DOCS_DIR = "docs"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {
        "forward_test_start_ts": None,   # set on first run
        "starting_balance": STARTING_BALANCE,
        "balance": STARTING_BALANCE,
        "closed_trades": [],
        "last_run": None
    }


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def score_setup(displacement_ratio, volume_ratio, volatility_ratio):
    displacement_norm = min(displacement_ratio, 3.0) / 3.0
    volume_norm = min(volume_ratio, 3.0) / 3.0
    volatility_score = 1.0 - min(volatility_ratio / 5.0, 1.0)
    raw = (0.40 * displacement_norm) + (0.35 * volume_norm) + (0.25 * volatility_score)
    return round(raw * 100, 1)


def find_limit_fill_live(df, signal_index, limit_price, is_bullish, max_wait):
    """
    Same retest logic as the backtest engine, but distinguishes "checked
    every available candle and still no fill" from "genuinely ran out of
    the full wait window." Returns (status, fill_index):
      status = 'filled'  -> fill_index is set
      status = 'pending' -> not enough live candles have arrived yet
      status = 'expired' -> the full window passed with no retest
    """
    window_end = signal_index + 1 + max_wait
    scan_end = min(window_end, len(df))
    for j in range(signal_index + 1, scan_end):
        row = df.iloc[j]
        touched = (row['low'] <= limit_price) if is_bullish else (row['high'] >= limit_price)
        if touched:
            return 'filled', j
    if len(df) < window_end:
        return 'pending', None
    return 'expired', None


def simulate_exit_live(df, i, is_bullish, entry_price, sl_price, tp_price, risk_pips, breakeven_enabled):
    """
    Same exit logic as the backtest engine, but if fewer than EXIT_WINDOW
    future candles actually exist yet, the trade is reported OPEN instead
    of being force-resolved.
    Returns dict: {status: 'open'|'closed', outcome, exit_index, r_multiple}
    """
    future_window = df.iloc[i + 1: i + 1 + EXIT_WINDOW]

    breakeven_trigger = None
    if breakeven_enabled:
        breakeven_trigger = entry_price + (risk_pips * BREAKEVEN_TRIGGER_R) if is_bullish \
            else entry_price - (risk_pips * BREAKEVEN_TRIGGER_R)

    effective_sl = sl_price
    armed = False

    for idx, fut in future_window.iterrows():
        if is_bullish:
            if breakeven_enabled and not armed and fut['high'] >= breakeven_trigger:
                armed = True
                effective_sl = entry_price
            if fut['low'] <= effective_sl:
                return {'status': 'closed', 'outcome': 'BE' if armed else 'LOSS',
                        'exit_index': idx, 'r_multiple': 0.0 if armed else -1.0}
            elif fut['high'] >= tp_price:
                return {'status': 'closed', 'outcome': 'WIN', 'exit_index': idx, 'r_multiple': 4.0}
        else:
            if breakeven_enabled and not armed and fut['low'] <= breakeven_trigger:
                armed = True
                effective_sl = entry_price
            if fut['high'] >= effective_sl:
                return {'status': 'closed', 'outcome': 'BE' if armed else 'LOSS',
                        'exit_index': idx, 'r_multiple': 0.0 if armed else -1.0}
            elif fut['low'] <= tp_price:
                return {'status': 'closed', 'outcome': 'WIN', 'exit_index': idx, 'r_multiple': 4.0}

    # Ran through every available candle without hitting stop/target.
    if len(future_window) < EXIT_WINDOW:
        return {'status': 'open'}  # not enough real time has passed yet — still running

    final_row = future_window.iloc[-1]
    final_close = final_row['close']
    price_diff = (final_close - entry_price) if is_bullish else (entry_price - final_close)
    raw_r = (price_diff / risk_pips) if risk_pips else 0.0
    if armed and raw_r <= 0:
        return {'status': 'closed', 'outcome': 'BE', 'exit_index': future_window.index[-1], 'r_multiple': 0.0}
    return {'status': 'closed', 'outcome': 'EXP', 'exit_index': future_window.index[-1],
            'r_multiple': max(-1.0, min(raw_r, 4.0))}


def fetch_recent(exchange, symbol, days_back):
    since_time = exchange.milliseconds() - (days_back * 24 * 60 * 60 * 1000)
    raw = exchange.fetch_ohlcv(symbol, timeframe='4h', since=since_time, limit=1000)
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df.drop_duplicates(subset='timestamp').reset_index(drop=True)


def run_forward_scan():
    state = load_state()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    if state["forward_test_start_ts"] is None:
        state["forward_test_start_ts"] = now_ms  # anchor: only signals from this moment on ever count

    exchange = ccxt.okx({'enableRateLimit': True})
    all_candidates = []      # freshly detected + still-pending setups, across both symbols
    all_open_trades = []     # currently filled, unresolved trades
    symbol_frames = {}
    fetch_errors = []

    for symbol in PAIRS:
        try:
            df = fetch_recent(exchange, symbol, LOOKBACK_DAYS)
            if len(df) < 15:
                fetch_errors.append(f"{symbol}: not enough candles returned")
                continue
            symbol_frames[symbol] = df
        except Exception as e:
            fetch_errors.append(f"{symbol}: {e}")

    # --- re-derive every setup from the anchor point forward, each run ---
    # (cheap, self-correcting, and avoids ever having to hand-merge partial state)
    for symbol, df in symbol_frames.items():
        for i in range(6, len(df)):
            c_candle = df.iloc[i]
            if c_candle['timestamp'] < state["forward_test_start_ts"]:
                continue  # this setup formed before we started forward-testing — doesn't count

            window = df.iloc[i - 6:i]
            structural_high = window['high'].max()
            structural_low = window['low'].min()

            bearish_choch = (c_candle['close'] < structural_low) and (df.iloc[i - 1]['close'] >= structural_low)
            bullish_choch = (c_candle['close'] > structural_high) and (df.iloc[i - 1]['close'] <= structural_high)
            if not bearish_choch and not bullish_choch:
                continue

            is_bullish = bullish_choch
            sl_price = c_candle['high'] if not is_bullish else c_candle['low']
            broken_level = structural_high if is_bullish else structural_low
            detect_risk = abs(c_candle['close'] - sl_price)
            if c_candle['close'] == 0 or detect_risk == 0:
                continue

            displacement_ratio = abs(c_candle['close'] - broken_level) / detect_risk
            avg_vol = window['volume'].mean()
            volume_ratio = (c_candle['volume'] / avg_vol) if avg_vol > 0 else 1.0
            detect_volatility_ratio = (detect_risk / c_candle['close']) * 100
            score = score_setup(displacement_ratio, volume_ratio, detect_volatility_ratio)

            fill_status, fill_idx = find_limit_fill_live(df, i, broken_level, is_bullish, LIMIT_FILL_WINDOW)
            if fill_status == 'expired':
                continue  # never retested — this setup is dead, doesn't count
            if fill_status == 'pending':
                all_candidates.append({
                    'status': 'pending', 'symbol': symbol, 'signal_time': c_candle['timestamp'],
                    'is_bullish': is_bullish, 'score': score
                })
                continue

            # filled
            entry_price = broken_level
            risk_pips = abs(entry_price - sl_price)
            if risk_pips == 0:
                continue
            tp_price = entry_price - (risk_pips * 4.0) if not is_bullish else entry_price + (risk_pips * 4.0)
            volatility_ratio = (risk_pips / entry_price) * 100

            exit_result = simulate_exit_live(df, fill_idx, is_bullish, entry_price, sl_price, tp_price,
                                              risk_pips, BREAKEVEN_ENABLED)

            base = {
                'symbol': symbol, 'is_bullish': is_bullish, 'score': score,
                'signal_time': c_candle['timestamp'], 'entry_time': df.iloc[fill_idx]['timestamp'],
                'entry_price': entry_price, 'volatility_ratio': volatility_ratio
            }

            if exit_result['status'] == 'open':
                all_open_trades.append(base)
            else:
                base.update({
                    'exit_time': df.iloc[exit_result['exit_index']]['timestamp'],
                    'outcome': exit_result['outcome'],
                    'r_multiple': exit_result['r_multiple']
                })
                all_candidates.append({**base, 'status': 'resolved'})

    # --- settle resolved trades not already recorded, oldest first ---
    already_closed_keys = {(t['symbol'], t['entry_time']) for t in state['closed_trades']}
    newly_resolved = [c for c in all_candidates if c.get('status') == 'resolved'
                      and (c['symbol'], c['entry_time']) not in already_closed_keys]
    newly_resolved.sort(key=lambda t: t['entry_time'])

    balance = state['balance']
    for t in newly_resolved:
        risk_pct = 0.02 if t['volatility_ratio'] < 2.0 else 0.015
        risk_amount = state['starting_balance'] * risk_pct
        balance += (risk_amount * t['r_multiple'])
        if balance < 5.0:
            balance = 5.0
        state['closed_trades'].append({
            'symbol': t['symbol'],
            'type': 'BUY' if t['is_bullish'] else 'SELL',
            'signal_time': t['signal_time'],
            'entry_time': t['entry_time'],
            'exit_time': t['exit_time'],
            'entry_price': t['entry_price'],
            'score': t['score'],
            'outcome': t['outcome'],
            'r_multiple': t['r_multiple'],
            'balance_after': round(balance, 2)
        })

    state['balance'] = round(balance, 2)
    state['last_run'] = now.strftime('%Y-%m-%d %H:%M UTC')

    pending_setups = [c for c in all_candidates if c.get('status') == 'pending']

    save_state(state)
    render_dashboard(state, pending_setups, all_open_trades, fetch_errors)


def render_dashboard(state, pending_setups, open_trades, fetch_errors):
    os.makedirs(DOCS_DIR, exist_ok=True)

    closed = state['closed_trades']
    wins = sum(1 for t in closed if t['outcome'] == 'WIN')
    losses = sum(1 for t in closed if t['outcome'] == 'LOSS')
    breakevens = sum(1 for t in closed if t['outcome'] == 'BE')
    expired = sum(1 for t in closed if t['outcome'] == 'EXP')
    decisive = wins + losses
    win_rate = (wins / decisive * 100) if decisive > 0 else 0.0
    net_pl = state['balance'] - state['starting_balance']

    def fmt_ts(ms):
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')

    metrics = {
        'initial': f"${state['starting_balance']:.2f}",
        'final': f"${state['balance']:.2f}",
        'net': f"${net_pl:+,.2f}",
        'trades': len(closed),
        'wins': wins, 'losses': losses, 'breakevens': breakevens, 'expired': expired,
        'win_rate': f"{win_rate:.1f}%"
    }

    equity_curve = [{"time": "Start", "balance": state['starting_balance']}]
    for t in closed:
        equity_curve.append({"time": fmt_ts(t['exit_time']), "balance": t['balance_after']})

    display_closed = [{
        'id': i + 1, 'symbol': t['symbol'], 'type': t['type'],
        'signal_time': fmt_ts(t['signal_time']), 'entry_time': fmt_ts(t['entry_time']),
        'exit_time': fmt_ts(t['exit_time']), 'entry': f"${t['entry_price']:,.4f}",
        'score': f"{t['score']}%", 'outcome': t['outcome'],
        'r_multiple': f"{t['r_multiple']:+.2f}R", 'balance': f"${t['balance_after']:,.2f}"
    } for i, t in enumerate(closed)]

    display_open = [{
        'symbol': t['symbol'], 'type': 'BUY' if t['is_bullish'] else 'SELL',
        'entry_time': fmt_ts(t['entry_time']), 'entry': f"${t['entry_price']:,.4f}",
        'score': f"{t['score']}%"
    } for t in open_trades]

    display_pending = [{
        'symbol': t['symbol'], 'type': 'BUY' if t['is_bullish'] else 'SELL',
        'signal_time': fmt_ts(t['signal_time']), 'score': f"{t['score']}%"
    } for t in pending_setups]

    with open('templates/index_template.html') as f:
        index_tpl = Template(f.read())
    with open('templates/history_template.html') as f:
        history_tpl = Template(f.read())

    common = {
        'metrics': metrics, 'last_run': state['last_run'],
        'open_trades': display_open, 'pending_setups': display_pending,
        'fetch_errors': fetch_errors, 'trade_mode': TRADE_MODE, 'breakeven_enabled': BREAKEVEN_ENABLED
    }

    with open(os.path.join(DOCS_DIR, 'index.html'), 'w') as f:
        f.write(index_tpl.render(**common, equity=equity_curve))

    with open(os.path.join(DOCS_DIR, 'history.html'), 'w') as f:
        f.write(history_tpl.render(**common, logs=display_closed))

    os.makedirs(os.path.join(DOCS_DIR, 'static'), exist_ok=True)
    with open('static/style.css') as src, open(os.path.join(DOCS_DIR, 'static', 'style.css'), 'w') as dst:
        dst.write(src.read())


if __name__ == '__main__':
    run_forward_scan()
