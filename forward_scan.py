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
# Balance, trade mode, pairs, and run duration all live in config.json —
# normally updated automatically by the Settings issue form (see
# .github/ISSUE_TEMPLATE/settings.yml), or you can edit config.json by hand.
# These are only the fallback defaults used if config.json is missing.
DEFAULT_STARTING_BALANCE = 62.0
DEFAULT_TRADE_MODE = "single"   # "single" = 1 concurrent trade, "multi" = up to 3
DEFAULT_PAIRS = ["ETH/USDT", "SOL/USDT"]
ALL_PAIRS = ["ETH/USDT", "SOL/USDT"]

LOOKBACK_DAYS = 60             # candles fetched each run — plenty for the 6-candle structure window
LIMIT_FILL_WINDOW = 6          # candles to wait for a retest (24h on 4H)
EXIT_WINDOW = 10                # candles to wait for stop/target before calling it expired

TRADE_MODE_LIMITS = {"single": 1, "multi": 3}

STATE_PATH = "state.json"
CONFIG_PATH = "config.json"
DOCS_DIR = "docs"


def load_config():
    """
    Reads config.json — normally written by the Settings issue form, or
    editable by hand. Takes effect on the next scan run (minimum 30 min).
    """
    defaults = {
        "starting_balance": DEFAULT_STARTING_BALANCE,
        "trade_mode": DEFAULT_TRADE_MODE,
        "pairs": DEFAULT_PAIRS,
        "run_days": None
    }
    if not os.path.exists(CONFIG_PATH):
        return defaults
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except Exception:
        return defaults

    starting_balance = float(cfg.get("starting_balance", DEFAULT_STARTING_BALANCE))
    trade_mode = cfg.get("trade_mode", DEFAULT_TRADE_MODE)
    if trade_mode not in TRADE_MODE_LIMITS:
        trade_mode = DEFAULT_TRADE_MODE

    pairs = cfg.get("pairs", DEFAULT_PAIRS)
    pairs = [p for p in pairs if p in ALL_PAIRS]
    if not pairs:
        pairs = DEFAULT_PAIRS

    run_days = cfg.get("run_days", None)
    try:
        run_days = int(run_days) if run_days not in (None, "", "None") else None
    except (ValueError, TypeError):
        run_days = None

    return {"starting_balance": starting_balance, "trade_mode": trade_mode, "pairs": pairs, "run_days": run_days}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {
        "forward_test_start_ts": None,   # set on first run
        "run_until_ts": None,            # set once, first time run_days is configured — never shortened
        "starting_balance": DEFAULT_STARTING_BALANCE,
        "balance": DEFAULT_STARTING_BALANCE,
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


def simulate_exit_live(df, i, is_bullish, entry_price, sl_price, tp_price, risk_pips):
    """
    Same exit logic as the backtest engine, but if fewer than EXIT_WINDOW
    future candles actually exist yet, the trade is reported OPEN instead
    of being force-resolved. No breakeven — plain stop-loss / target only.
    Returns dict: {status: 'open'|'closed', outcome, exit_index, r_multiple}
    """
    future_window = df.iloc[i + 1: i + 1 + EXIT_WINDOW]

    for idx, fut in future_window.iterrows():
        if is_bullish:
            if fut['low'] <= sl_price:
                return {'status': 'closed', 'outcome': 'LOSS', 'exit_index': idx, 'r_multiple': -1.0}
            elif fut['high'] >= tp_price:
                return {'status': 'closed', 'outcome': 'WIN', 'exit_index': idx, 'r_multiple': 4.0}
        else:
            if fut['high'] >= sl_price:
                return {'status': 'closed', 'outcome': 'LOSS', 'exit_index': idx, 'r_multiple': -1.0}
            elif fut['low'] <= tp_price:
                return {'status': 'closed', 'outcome': 'WIN', 'exit_index': idx, 'r_multiple': 4.0}

    # Ran through every available candle without hitting stop/target.
    if len(future_window) < EXIT_WINDOW:
        return {'status': 'open'}  # not enough real time has passed yet — still running

    final_row = future_window.iloc[-1]
    final_close = final_row['close']
    price_diff = (final_close - entry_price) if is_bullish else (entry_price - final_close)
    raw_r = (price_diff / risk_pips) if risk_pips else 0.0
    return {'status': 'closed', 'outcome': 'EXP', 'exit_index': future_window.index[-1],
            'r_multiple': max(-1.0, min(raw_r, 4.0))}


def compute_signal_radar(pending_setups, open_trades, missed_setups, closed_trades, symbol_frames, min_count=10):
    """
    Builds the 'everything happening in the market right now' feed — not
    just trades our account took. Every setup that formed shows up here:
    still waiting for a retest (PENDING), triggered and running (RUNNING,
    with live % progress toward TP or SL), triggered but the account had no
    free slot (MISSED), or recently CLOSED. Always returns at least
    min_count entries by padding with the most recent closed trades if
    there simply isn't enough current activity — this feed should never
    look empty/dormant.
    """
    entries = []

    for p in pending_setups:
        entries.append({
            'symbol': p['symbol'], 'type': 'BUY' if p['is_bullish'] else 'SELL',
            'status': 'PENDING', 'score': p['score'], 'sort_ts': p['signal_time'],
            'detail': 'Setup formed, awaiting retest to trigger entry'
        })

    for o in open_trades:
        current_price = symbol_frames[o['symbol']]['close'].iloc[-1] if o['symbol'] in symbol_frames else o['entry_price']
        entry, sl, tp, is_bullish = o['entry_price'], o['sl_price'], o['tp_price'], o['is_bullish']

        if is_bullish:
            if current_price >= entry:
                pct = min(100, max(0, (current_price - entry) / (tp - entry) * 100)) if tp != entry else 0
                detail = f"{pct:.0f}% of the way to Take Profit"
            else:
                pct = min(100, max(0, (entry - current_price) / (entry - sl) * 100)) if entry != sl else 0
                detail = f"{pct:.0f}% of the way to Stop Loss"
        else:
            if current_price <= entry:
                pct = min(100, max(0, (entry - current_price) / (entry - tp) * 100)) if entry != tp else 0
                detail = f"{pct:.0f}% of the way to Take Profit"
            else:
                pct = min(100, max(0, (current_price - entry) / (sl - entry) * 100)) if sl != entry else 0
                detail = f"{pct:.0f}% of the way to Stop Loss"

        entries.append({
            'symbol': o['symbol'], 'type': 'BUY' if is_bullish else 'SELL',
            'status': 'RUNNING', 'score': o['score'], 'sort_ts': o['entry_time'], 'detail': detail
        })

    for msd in missed_setups:
        entries.append({
            'symbol': msd['symbol'], 'type': 'BUY' if msd['is_bullish'] else 'SELL',
            'status': 'MISSED (account full)', 'score': msd['score'], 'sort_ts': msd['entry_time'],
            'detail': 'Triggered in the market but no account slot was free'
        })

    entries.sort(key=lambda e: e['sort_ts'], reverse=True)

    if len(entries) < min_count:
        pad_needed = min_count - len(entries)
        recent_closed = sorted(closed_trades, key=lambda t: t['exit_time'], reverse=True)[:pad_needed]
        for c in recent_closed:
            entries.append({
                'symbol': c['symbol'], 'type': c['type'], 'status': f"CLOSED ({c['outcome']})",
                'score': c['score'], 'sort_ts': c['exit_time'],
                'detail': f"Finished at {c['r_multiple']:+.2f}R"
            })

    return entries


def fetch_recent(exchange, symbol, days_back):
    since_time = exchange.milliseconds() - (days_back * 24 * 60 * 60 * 1000)
    raw = exchange.fetch_ohlcv(symbol, timeframe='4h', since=since_time, limit=1000)
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df.drop_duplicates(subset='timestamp').reset_index(drop=True)


def run_forward_scan():
    config = load_config()
    global MAX_CONCURRENT
    MAX_CONCURRENT = TRADE_MODE_LIMITS[config['trade_mode']]

    state = load_state()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    if state["forward_test_start_ts"] is None:
        state["forward_test_start_ts"] = now_ms  # anchor: only signals from this moment on ever count

    # your balance is whatever config.json says — editable any time, no code changes
    state["starting_balance"] = config["starting_balance"]

    # run-duration lock: set ONCE the first time run_days is configured, and
    # never shortened after that — matches "once it's running I can't stop
    # it early." Leaving run_days unset keeps it running indefinitely.
    if state.get("run_until_ts") is None and config["run_days"] is not None:
        state["run_until_ts"] = state["forward_test_start_ts"] + (config["run_days"] * 24 * 60 * 60 * 1000)

    run_locked_and_expired = state.get("run_until_ts") is not None and now_ms >= state["run_until_ts"]

    exchange = ccxt.okx({'enableRateLimit': True})
    all_candidates = []       # pending setups (still awaiting retest) — always reported, no slot needed
    raw_filled = []           # every setup that got a retest fill, BEFORE concurrency selection
    missed_setups = []        # filled setups passed over because all concurrency slots were full
    symbol_frames = {}
    fetch_errors = []

    for symbol in config["pairs"]:
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
    # Once the locked run window has ended, no NEW setups are considered at
    # all — but trades already open still get resolved below.
    if not run_locked_and_expired:
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

                # filled — but NOT yet decided whether it's actually taken (concurrency limit applies below)
                entry_price = broken_level
                risk_pips = abs(entry_price - sl_price)
                if risk_pips == 0:
                    continue
                tp_price = entry_price - (risk_pips * 4.0) if not is_bullish else entry_price + (risk_pips * 4.0)
                volatility_ratio = (risk_pips / entry_price) * 100

                exit_result = simulate_exit_live(df, fill_idx, is_bullish, entry_price, sl_price, tp_price, risk_pips)

                raw_filled.append({
                    'symbol': symbol, 'is_bullish': is_bullish, 'score': score,
                    'signal_time': c_candle['timestamp'], 'entry_time': df.iloc[fill_idx]['timestamp'],
                    'entry_price': entry_price, 'sl_price': sl_price, 'tp_price': tp_price,
                    'volatility_ratio': volatility_ratio, 'exit_result': exit_result
                })

    # --- apply the concurrency limit: walk every filled setup in true time
    #     order, only accepting one when a slot is actually free. This is
    #     the same rule the backtest engine uses — a setup on the other pair
    #     simply doesn't count if the account was already occupied. ---
    raw_filled.sort(key=lambda t: t['entry_time'])
    grouped = {}
    for t in raw_filled:
        grouped.setdefault(t['entry_time'], []).append(t)

    open_exits = []   # exit timestamps of accepted trades; float('inf') for still-open ones
    all_open_trades = []
    all_candidates_filled = []

    for ts in sorted(grouped.keys()):
        group = grouped[ts]
        open_exits = [e for e in open_exits if e > ts]
        available_slots = MAX_CONCURRENT - len(open_exits)
        if available_slots <= 0:
            missed_setups.extend(group)  # account fully occupied — every setup at this moment is passed over
            continue

        ranked = sorted(group, key=lambda c: c['score'], reverse=True)
        chosen = ranked[:available_slots]
        missed_setups.extend(ranked[available_slots:])  # scored lower than the slots available at this moment

        for t in chosen:
            exit_result = t['exit_result']
            if exit_result['status'] == 'open':
                open_exits.append(float('inf'))
                all_open_trades.append(t)
            else:
                exit_ts_val = t['entry_time']  # placeholder, real value set below
                exit_index = exit_result['exit_index']
                # need the actual candle timestamp for this exit — resolved via symbol_frames
                exit_ts_val = symbol_frames[t['symbol']].iloc[exit_index]['timestamp']
                open_exits.append(exit_ts_val)
                t.update({
                    'exit_time': exit_ts_val,
                    'outcome': exit_result['outcome'],
                    'r_multiple': exit_result['r_multiple'],
                    'status': 'resolved'
                })
                all_candidates_filled.append(t)

    all_candidates.extend(all_candidates_filled)

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
    render_dashboard(state, pending_setups, all_open_trades, missed_setups, fetch_errors, run_locked_and_expired)


def render_dashboard(state, pending_setups, open_trades, missed_setups, fetch_errors, run_ended):
    os.makedirs(DOCS_DIR, exist_ok=True)

    closed = state['closed_trades']
    wins = sum(1 for t in closed if t['outcome'] == 'WIN')
    losses = sum(1 for t in closed if t['outcome'] == 'LOSS')
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
        'wins': wins, 'losses': losses, 'expired': expired,
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

    display_missed = [{
        'symbol': t['symbol'], 'type': 'BUY' if t['is_bullish'] else 'SELL',
        'entry_time': fmt_ts(t['entry_time']), 'score': f"{t['score']}%"
    } for t in missed_setups]

    with open('templates/index_template.html') as f:
        index_tpl = Template(f.read())
    with open('templates/history_template.html') as f:
        history_tpl = Template(f.read())

    mode_label = "multi" if MAX_CONCURRENT == TRADE_MODE_LIMITS["multi"] else "single"
    run_until_str = fmt_ts(state['run_until_ts']) if state.get('run_until_ts') else None
    common = {
        'metrics': metrics, 'last_run': state['last_run'],
        'open_trades': display_open, 'pending_setups': display_pending, 'missed_setups': display_missed,
        'fetch_errors': fetch_errors, 'trade_mode': mode_label,
        'run_until': run_until_str, 'run_ended': run_ended
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
