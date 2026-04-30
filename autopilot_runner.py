"""Multi-user Forex Autopilot — runs trading cycles for all active users."""

import os
import sys
import time
import random
from datetime import datetime

# Module paths
AGENT_DIR = os.environ.get("AGENT_DIR", "/app/agent")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv()

import config as fx_config
from data_fetcher import get_watchlist_prices, get_historical_data, get_market_regime
from market_calendar import is_market_open, get_active_sessions, now_et, time_to_market_open
from strategy import get_scored_signal

from users import (
    Session as DBSession, User, PortfolioRecord, PositionRecord,
    get_portfolio_summary, get_positions_detail,
    execute_buy, execute_sell,
)

import logging
LOG_DIR = os.path.join(AGENT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "autopilot.log")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("forex_autopilot")
# Also write to file for WebSocket streaming
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

INTERVAL_MIN = int(os.environ.get("AUTOPILOT_INTERVAL", "15"))
CONFIDENCE_THRESHOLD = 0.55


def get_active_users() -> list[User]:
    """Get all active non-admin users."""
    with DBSession() as db:
        users = db.query(User).filter(User.is_active == True).all()
        for u in users:
            db.expunge(u)
        return users


def run_cycle(cycle_num: int):
    """Run one trading cycle for all users."""
    et = now_et()
    logger.info(f"\n{'='*60}")
    logger.info(f"  CYCLE #{cycle_num} | {et.strftime('%Y-%m-%d %H:%M:%S')} ET")
    logger.info(f"  Sessions: {', '.join(get_active_sessions())}")
    logger.info(f"{'='*60}")

    # Fetch shared market data
    prices = get_watchlist_prices()
    if not prices:
        logger.warning("  Could not fetch prices. Skipping cycle.")
        return

    logger.info(f"  Fetched prices for {len(prices)}/{len(fx_config.WATCHLIST)} pairs")

    # Strategy engine: per-pair strategies with time windows
    signals = []
    try:
        from strategy_engine import scan_all_pairs
        all_signals = scan_all_pairs(get_historical_data)
        active = [s for s in all_signals if s.get("in_window")]
        signals = [s for s in all_signals if s.get("signal") in ("BUY", "SELL")]
        logger.info(f"  Strategy scan: {len(active)}/{len(all_signals)} pairs in window, {len(signals)} setups")
        for s in signals:
            logger.info(f"    {s['symbol']} {s['signal']} @ {s['price']:.5f} ({s['confidence']:.0%}) [{s.get('strategy','')}] — {s['reason'][:70]}")
        for s in all_signals:
            if not s.get("in_window"):
                logger.info(f"    {s['symbol']} SKIP — {s.get('reason','')[:60]}")
    except Exception as e:
        logger.error(f"  Strategy scan failed: {e}")
        import traceback
        traceback.print_exc()

    # Market regime
    regime = get_market_regime()
    confidence_threshold = 0.70 if regime == "BEAR" else CONFIDENCE_THRESHOLD
    logger.info(f"  Regime: {regime} | Confidence threshold: {confidence_threshold:.0%}")

    # Execute for each user
    users = get_active_users()
    logger.info(f"  Active users: {len(users)}")

    for user in users:
        try:
            _trade_for_user(user, prices, signals, confidence_threshold)
        except Exception as e:
            logger.error(f"  [{user.username}] Error: {e}")

    # Summary
    for user in users:
        summary = get_portfolio_summary(user, prices)
        logger.info(f"  [{user.username}] ${summary['total_value']:,.2f} | {summary['total_return_pct']:+.2f}% | {summary['open_positions']} positions")


def _trade_for_user(user: User, prices: dict, signals: list, confidence_threshold: float):
    """Execute trades for a single user based on shared signals."""
    positions = get_positions_detail(user, prices)

    # Check stop-loss / take-profit
    for sym, pos_data in positions.items():
        if pos_data["pnl_pct"] <= -fx_config.STOP_LOSS_PCT * 100:
            logger.info(f"  [{user.username}] STOP LOSS {sym} ({pos_data['pnl_pct']:.1f}%)")
            execute_sell(user, sym, prices.get(sym, pos_data["current_price"]))
        elif pos_data["pnl_pct"] >= fx_config.TAKE_PROFIT_PCT * 100:
            logger.info(f"  [{user.username}] TAKE PROFIT {sym} ({pos_data['pnl_pct']:.1f}%)")
            execute_sell(user, sym, prices.get(sym, pos_data["current_price"]))

    # Refresh positions after stops
    positions = get_positions_detail(user, prices)

    for sig in signals:
        symbol = sig.get("symbol", "")
        signal = sig.get("signal", "HOLD")
        confidence = sig.get("confidence", 0)

        if confidence < confidence_threshold:
            continue

        if signal == "BUY" and symbol not in positions:
            price = prices.get(symbol, 0)
            if price <= 0:
                continue
            slippage = fx_config.SLIPPAGE_PCT * random.uniform(0.5, 2.0)
            fill_price = price * (1 + slippage)
            summary = get_portfolio_summary(user, prices)
            size_pct = float(sig.get("position_size_pct", fx_config.MAX_POSITION_SIZE_PCT))
            max_spend = summary["cash"] * min(size_pct, fx_config.MAX_POSITION_SIZE_PCT)
            quantity = int(max_spend / fill_price)
            if quantity <= 0:
                continue
            total_cost = quantity * fill_price
            result = execute_buy(
                user, symbol, price, quantity, fill_price, slippage, total_cost,
                confidence=confidence, ai_signal=sig,
                dynamic_sl=fx_config.STOP_LOSS_PCT, dynamic_tp=fx_config.TAKE_PROFIT_PCT,
            )
            if result:
                logger.info(f"  [{user.username}] BUY {quantity}x {symbol} @ {fill_price:.5f} | {sig.get('reason', '')[:60]}")

        elif signal == "SELL" and symbol in positions:
            price = prices.get(symbol, 0)
            if price <= 0:
                continue
            result = execute_sell(user, symbol, price)
            if result:
                logger.info(f"  [{user.username}] SELL {symbol} @ {result['fill_price']:.5f} | P&L: ${result['pnl']:.2f}")


def main():
    logger.info(f"Forex Autopilot starting (interval: {INTERVAL_MIN}min)")
    logger.info(f"Watchlist: {len(fx_config.WATCHLIST)} pairs")

    # Write cycle count file for API
    cycle_file = os.path.join(AGENT_DIR, "logs", "cycle_count.txt")
    os.makedirs(os.path.dirname(cycle_file), exist_ok=True)

    cycle = 0
    while True:
        try:
            if not is_market_open():
                wait = time_to_market_open()
                if wait:
                    hours = wait.total_seconds() / 3600
                    et = now_et()
                    logger.info(f"  [{et.strftime('%H:%M ET')}] Market closed. Opens in {hours:.1f} hours.")
                    time.sleep(min(wait.total_seconds(), 300))
                    continue

            cycle += 1
            try:
                with open(cycle_file, "w") as f:
                    f.write(str(cycle))
            except Exception:
                pass

            run_cycle(cycle)

            logger.info(f"  Next cycle in {INTERVAL_MIN} minutes...")
            time.sleep(INTERVAL_MIN * 60)

        except KeyboardInterrupt:
            logger.info("Autopilot stopped.")
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
