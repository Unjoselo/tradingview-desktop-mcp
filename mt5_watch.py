# -*- coding: utf-8 -*-
"""
Read-only MT5 event watcher. Polls the terminal every --interval seconds and prints
one short line per event worth reacting to, so an agent (e.g. Claude Code's
Monitor tool) wakes up only when something happens instead of polling blindly.

Events:
  OPEN / CLOSE      position appeared / disappeared (CLOSE includes realized P/L)
  ORDER_GONE        pending order filled, cancelled or expired
  PROFIT            open position crossed +1.5 / +3.0 / +4.0 ... price units
  NEAR_SL           price within --near-sl of a position's SL (once per ticket)
  FAST              bid moved >= --fast units within --fast-window seconds
  BREAKOUT          last closed M1 bar closed beyond the prior 60-bar high/low
  ERROR             terminal/API problem

Usage:
  python mt5_watch.py --symbol XAUUSDm [--interval 2] [--fast 1.5] [--fast-window 15]
"""
import argparse
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5


def emit(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--fast", type=float, default=1.5)
    ap.add_argument("--fast-window", type=float, default=15.0)
    ap.add_argument("--fast-cooldown", type=float, default=30.0)
    ap.add_argument("--near-sl", type=float, default=0.4)
    args = ap.parse_args()

    if not mt5.initialize():
        emit(f"ERROR mt5.initialize failed: {mt5.last_error()}")
        sys.exit(1)
    mt5.symbol_select(args.symbol, True)

    positions: dict[int, object] = {}
    orders: set[int] = set()
    profit_marks: dict[int, float] = {}
    near_sl_warned: set[int] = set()
    ticks: deque = deque()
    last_fast = 0.0
    last_bar_time = None
    first = True
    emit(f"WATCH start {args.symbol} interval={args.interval}s fast={args.fast}/{args.fast_window}s")

    while True:
        try:
            tick = mt5.symbol_info_tick(args.symbol)
            if tick is None:
                emit(f"ERROR no tick: {mt5.last_error()}")
                time.sleep(5)
                continue
            now = time.time()
            bid = tick.bid

            # positions
            cur = {p.ticket: p for p in (mt5.positions_get(symbol=args.symbol) or [])}
            if not first:
                for t, p in cur.items():
                    if t not in positions:
                        d = "LONG" if p.type == 0 else "SHORT"
                        emit(f"OPEN {t} {d} {p.volume} @{p.price_open:.2f} sl={p.sl:.2f} tp={p.tp:.2f}")
                for t, p in positions.items():
                    if t not in cur:
                        deals = mt5.history_deals_get(position=t) or []
                        pnl = sum(x.profit + x.commission + x.swap for x in deals)
                        exit_d = [x for x in deals if x.entry in (1, 3)]
                        why = exit_d[-1].comment if exit_d else ""
                        emit(f"CLOSE {t} pnl={pnl:+.2f} {why}")
                    elif cur[t].volume < p.volume:
                        emit(f"PARTIAL {t} {p.volume}->{cur[t].volume}")
            for t, p in cur.items():
                move = (bid - p.price_open) if p.type == 0 else (p.price_open - tick.ask)
                mark = profit_marks.get(t, 0.0)
                step = 1.5 if mark < 1.5 else (3.0 if mark < 3.0 else mark + 1.0)
                if move >= step:
                    profit_marks[t] = step
                    emit(f"PROFIT {t} +{move:.2f} units (${p.profit:+.2f}) sl={p.sl:.2f}")
                if p.sl and t not in near_sl_warned:
                    dist = (bid - p.sl) if p.type == 0 else (p.sl - tick.ask)
                    if 0 < dist <= args.near_sl:
                        near_sl_warned.add(t)
                        emit(f"NEAR_SL {t} {dist:.2f} from sl={p.sl:.2f} (${p.profit:+.2f})")
            positions = cur

            # pending orders
            cur_o = {o.ticket for o in (mt5.orders_get(symbol=args.symbol) or [])}
            if not first:
                for t in orders - cur_o:
                    emit(f"ORDER_GONE {t} (filled/cancelled/expired)")
            orders = cur_o

            # fast move
            ticks.append((now, bid))
            while ticks and now - ticks[0][0] > args.fast_window:
                ticks.popleft()
            if len(ticks) > 1 and now - last_fast > args.fast_cooldown:
                lo = min(b for _, b in ticks)
                hi = max(b for _, b in ticks)
                if bid - lo >= args.fast:
                    last_fast = now
                    emit(f"FAST UP +{bid - lo:.2f} in {args.fast_window:.0f}s bid={bid:.2f}")
                elif hi - bid >= args.fast:
                    last_fast = now
                    emit(f"FAST DOWN -{hi - bid:.2f} in {args.fast_window:.0f}s bid={bid:.2f}")

            # M1 close beyond 60-bar range
            rates = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_M1, 1, 61)
            if rates is not None and len(rates) == 61:
                bar = rates[-1]
                if last_bar_time is not None and bar["time"] != last_bar_time:
                    prior = rates[:-1]
                    if bar["close"] > prior["high"].max():
                        emit(f"BREAKOUT UP close={bar['close']:.2f} > {prior['high'].max():.2f}")
                    elif bar["close"] < prior["low"].min():
                        emit(f"BREAKOUT DOWN close={bar['close']:.2f} < {prior['low'].min():.2f}")
                last_bar_time = bar["time"]

            first = False
        except Exception as e:  # keep watching through transient errors
            emit(f"ERROR {type(e).__name__}: {e}")
            time.sleep(5)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
