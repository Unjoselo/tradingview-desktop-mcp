# -*- coding: utf-8 -*-
"""
MCP server for MetaTrader 5 (Windows).

Exposes a running MT5 desktop terminal (any broker build: MetaQuotes, Pepperstone,
IC Markets, ...) as MCP tools: account status, symbol quotes, candles, ticks,
indicators, open positions and pending orders, market/pending orders, SL/TP
modification, full and partial closing.

Requires the official `MetaTrader5` Python package, which attaches to a desktop
terminal installed on Windows. MT5 web terminal / mobile apps are NOT supported
(they have no local API).

Environment variables:
  MT5_TERMINAL_PATH   full path to terminal64.exe (default: auto-detect — uses
                      the last terminal the package finds installed)
  MT5_ALLOW_REAL      set to "1" to allow trading tools on REAL accounts
                      (default: trading tools refuse anything but demo)

Safety model:
  - Read-only tools (status, quotes, candles, ticks, indicators, calc, positions,
    orders, history) work on any account.
  - Trading tools (order_open, pending_open, order_cancel, modify, close,
    close_partial) abort on non-demo accounts unless MT5_ALLOW_REAL=1. This is
    deliberate: an LLM should not place real-money orders because of a config
    oversight.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import MetaTrader5 as mt5
import numpy as np
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mt5")

TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", "")
ALLOW_REAL = os.environ.get("MT5_ALLOW_REAL", "") == "1"

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1,
}

PENDING_TYPES = {
    "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT, "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
    "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP, "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
}
ORDER_TYPE_NAMES = {
    0: "BUY", 1: "SELL", 2: "BUY_LIMIT", 3: "SELL_LIMIT", 4: "BUY_STOP",
    5: "SELL_STOP", 6: "BUY_STOP_LIMIT", 7: "SELL_STOP_LIMIT", 8: "CLOSE_BY",
}
DEAL_ENTRY_NAMES = {0: "entry", 1: "exit", 2: "reverse", 3: "exit_by"}

# order_send return codes -> human-readable reason
RETCODES = {
    10004: "Requote", 10006: "Request rejected", 10007: "Request canceled by trader",
    10008: "Order placed", 10009: "Request completed", 10010: "Only part of the request was completed",
    10011: "Request processing error", 10012: "Request canceled by timeout",
    10013: "Invalid request", 10014: "Invalid volume", 10015: "Invalid price",
    10016: "Invalid stops (SL/TP too close to price or on the wrong side)",
    10017: "Trade is disabled", 10018: "Market is closed", 10019: "Not enough money",
    10020: "Prices changed", 10021: "No quotes to process the request",
    10022: "Invalid order expiration date", 10023: "Order state changed",
    10024: "Too frequent requests", 10025: "No changes in request",
    10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client terminal — enable the 'Algo Trading' button in MT5",
    10028: "Request locked for processing", 10029: "Order or position frozen (freeze level)",
    10030: "Invalid order filling type", 10031: "No connection with the trade server",
    10032: "Operation allowed only for live accounts", 10033: "Pending orders limit reached",
    10034: "Order/position volume limit reached for this symbol", 10035: "Incorrect or prohibited order type",
    10036: "Position with the specified ticket is already closed",
    10038: "Close volume exceeds the current position volume",
    10039: "A close order already exists for this position",
    10040: "Open positions limit reached", 10041: "Pending order activation rejected",
    10042: "Only long positions allowed", 10043: "Only short positions allowed",
    10044: "Only position closing allowed", 10045: "Position close only allowed by FIFO rule",
}
OK_RETCODES = {mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL}

_connected = False


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _direction(direction: str) -> str:
    """Normalize LONG/SHORT, also accepting BUY/SELL."""
    d = direction.strip().upper()
    return {"BUY": "LONG", "SELL": "SHORT"}.get(d, d)


def _ts(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _connect() -> None:
    global _connected
    if _connected and mt5.terminal_info() is not None:
        return
    kwargs = {"path": TERMINAL_PATH} if TERMINAL_PATH else {}
    if not mt5.initialize(**kwargs):
        raise RuntimeError(
            f"mt5.initialize failed: {mt5.last_error()}. Is the MT5 desktop terminal "
            "installed and logged in? Set MT5_TERMINAL_PATH if auto-detection fails."
        )
    _connected = True


def _guard_trading() -> None:
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError(f"account_info failed: {mt5.last_error()}")
    if acc.trade_mode != 0 and not ALLOW_REAL:  # 0 = demo
        raise RuntimeError(
            f"Account '{acc.server}' is NOT a demo account (trade_mode={acc.trade_mode}). "
            "Trading tools are demo-only unless MT5_ALLOW_REAL=1 is set."
        )


def _filling(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _deviation(symbol: str, deviation: int) -> int:
    """Max slippage in points. 0 = auto: twice the current spread, at least 30 points."""
    if deviation > 0:
        return deviation
    info = mt5.symbol_info(symbol)
    return max(30, 2 * info.spread) if info else 30


def _send(req: dict) -> tuple[Any, dict | None]:
    """order_send wrapper. Returns (result, None) on success or (result, error_dict)."""
    res = mt5.order_send(req)
    if res is not None and res.retcode in OK_RETCODES:
        return res, None
    rc = res.retcode if res else None
    return res, {
        "error": f"order_send failed retcode={rc}: {RETCODES.get(rc, 'unknown')}",
        "broker_comment": res.comment if res else None,
        "last_error": str(mt5.last_error()),
    }


def _pos_dict(p) -> dict:
    return {
        "ticket": p.ticket, "symbol": p.symbol,
        "direction": "LONG" if p.type == 0 else "SHORT",
        "volume": p.volume, "open_price": p.price_open,
        "current_price": p.price_current, "profit": p.profit, "swap": p.swap,
        "sl": p.sl, "tp": p.tp, "magic": p.magic, "comment": p.comment,
        "opened_at": _ts(p.time),
    }


def _order_dict(o) -> dict:
    return {
        "ticket": o.ticket, "symbol": o.symbol,
        "type": ORDER_TYPE_NAMES.get(o.type, o.type),
        "volume": o.volume_current, "price": o.price_open,
        "sl": o.sl, "tp": o.tp, "magic": o.magic, "comment": o.comment,
        "placed_at": _ts(o.time_setup),
        "expires_at": _ts(o.time_expiration) if o.time_expiration else None,
    }


def _get_position(ticket: int):
    poss = mt5.positions_get(ticket=ticket)
    return poss[0] if poss else None


def _close_position(p, volume: float, deviation: int) -> dict:
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return {"ticket": p.ticket, "closed": False, "error": f"no tick data for {p.symbol}"}
    res, err = _send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
        "position": p.ticket,
        "price": tick.bid if p.type == 0 else tick.ask,
        # reuse the position's comment: on partial closes some brokers copy the closing
        # order's comment onto the remaining position, which would erase its tag
        "deviation": _deviation(p.symbol, deviation), "magic": p.magic,
        "comment": (p.comment or "mcp_close")[:31],
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(p.symbol),
    })
    if err:
        return {"ticket": p.ticket, "closed": False, **err}
    pnl = p.profit * volume / p.volume if p.volume else p.profit
    return {"ticket": p.ticket, "closed": True, "volume": volume,
            "price": res.price, "pnl_estimate": round(pnl, 2)}


def _rates(symbol: str, timeframe: str, count: int):
    tf = TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        return None, {"error": f"invalid timeframe '{timeframe}', use {list(TIMEFRAMES)}"}
    if not mt5.symbol_select(symbol, True):
        return None, {"error": f"symbol '{symbol}' not found"}
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, min(max(count, 1), 5000))
    if rates is None or len(rates) == 0:
        return None, {"error": f"no data: {mt5.last_error()}"}
    return rates, None


# ---------------------------------------------------------------- indicators

def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def _rsi(close: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    diff = np.diff(close)
    gain = _wilder(np.clip(diff, 0, None), n)
    loss = _wilder(np.clip(-diff, 0, None), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(loss == 0, 100.0, 100 - 100 / (1 + gain / loss))
    out[1:] = rsi
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return _wilder(tr, n)


def _last(arr: np.ndarray, k: int, digits: int) -> list:
    return [None if np.isnan(v) else round(float(v), digits) for v in arr[-k:]]


# ---------------------------------------------------------------------- tools

@mcp.tool()
def mt5_status() -> str:
    """MT5 terminal and account status: server, login, demo/real, balance, equity, margin."""
    _connect()
    acc = mt5.account_info()
    term = mt5.terminal_info()
    if acc is None:
        return _ok({"connected": False, "error": str(mt5.last_error())})
    return _ok({
        "connected": True,
        "server": acc.server, "login": acc.login,
        "account_type": "demo" if acc.trade_mode == 0 else ("real" if acc.trade_mode == 2 else "contest"),
        "trading_allowed_by_this_server": acc.trade_mode == 0 or ALLOW_REAL,
        "algo_trading_enabled_in_terminal": bool(term.trade_allowed) if term else None,
        "balance": acc.balance, "equity": acc.equity,
        "margin": acc.margin, "free_margin": acc.margin_free,
        "currency": acc.currency, "leverage": acc.leverage,
        "terminal": term.name if term else None,
        "terminal_path": term.path if term else None,
    })


@mcp.tool()
def mt5_symbol_info(symbol: str) -> str:
    """Quote and contract details for a symbol: bid/ask, spread (points and price),
    point/tick size/tick value, min stop distance, freeze level, swaps, lot limits."""
    _connect()
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found in Market Watch"})
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    return _ok({
        "symbol": info.name, "description": info.description,
        "bid": tick.bid if tick else None, "ask": tick.ask if tick else None,
        "last_tick_time": _ts(tick.time) if tick else None,
        "spread_points": info.spread, "spread_price": round(info.spread * info.point, info.digits),
        "digits": info.digits, "point": info.point,
        "tick_size": info.trade_tick_size, "tick_value": info.trade_tick_value,
        "contract_size": info.trade_contract_size,
        "stops_level_points": info.trade_stops_level,
        "stops_level_price": round(info.trade_stops_level * info.point, info.digits),
        "freeze_level_points": info.trade_freeze_level,
        "volume_min": info.volume_min, "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "swap_long": info.swap_long, "swap_short": info.swap_short, "swap_mode": info.swap_mode,
        "trade_mode": info.trade_mode,  # 0 disabled, 1 long only, 2 short only, 3 close only, 4 full
        "currency_base": info.currency_base, "currency_profit": info.currency_profit,
    })


@mcp.tool()
def mt5_candles(symbol: str, timeframe: str = "M15", count: int = 50) -> str:
    """Last N OHLCV candles for a symbol. timeframe: M1,M5,M15,M30,H1,H4,D1,W1,MN1.
    Each candle includes tick volume, real volume and spread (points).
    The LAST candle is still forming — do not treat it as closed."""
    _connect()
    rates, err = _rates(symbol, timeframe, min(count, 1000))
    if err:
        return _ok(err)
    return _ok([{
        "time": _ts(int(r["time"])),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": int(r["tick_volume"]), "real_volume": int(r["real_volume"]),
        "spread": int(r["spread"]),
    } for r in rates])


@mcp.tool()
def mt5_ticks(symbol: str, count: int = 200, lookback_seconds: int = 300) -> str:
    """Most recent ticks (up to `count`, within the last `lookback_seconds` of trading),
    plus a summary: spread min/avg/max, price change, ticks per minute."""
    _connect()
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found"})
    last = mt5.symbol_info_tick(symbol)
    if last is None:
        return _ok({"error": "no tick data"})
    to = last.time + 1
    ticks = mt5.copy_ticks_range(symbol, to - max(lookback_seconds, 1), to, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) == 0:
        return _ok({"error": f"no ticks: {mt5.last_error()}"})
    ticks = ticks[-min(max(count, 1), 5000):]
    digits = mt5.symbol_info(symbol).digits
    bid, ask = ticks["bid"], ticks["ask"]
    spread = ask - bid
    span_s = max((int(ticks["time_msc"][-1]) - int(ticks["time_msc"][0])) / 1000, 1e-9)
    return _ok({
        "count": len(ticks),
        "from": datetime.fromtimestamp(int(ticks["time_msc"][0]) / 1000, tz=timezone.utc).isoformat(),
        "to": datetime.fromtimestamp(int(ticks["time_msc"][-1]) / 1000, tz=timezone.utc).isoformat(),
        "summary": {
            "spread_min": round(float(spread.min()), digits),
            "spread_avg": round(float(spread.mean()), digits),
            "spread_max": round(float(spread.max()), digits),
            "bid_first": float(bid[0]), "bid_last": float(bid[-1]),
            "bid_change": round(float(bid[-1] - bid[0]), digits),
            "bid_high": float(bid.max()), "bid_low": float(bid.min()),
            "ticks_per_minute": round(len(ticks) / span_s * 60, 1),
        },
        "ticks": [{
            "time": datetime.fromtimestamp(int(t["time_msc"]) / 1000, tz=timezone.utc).isoformat(),
            "bid": float(t["bid"]), "ask": float(t["ask"]),
        } for t in ticks],
    })


@mcp.tool()
def mt5_indicators(symbol: str, timeframe: str = "M1", ema_periods: str = "9,21,50",
                   rsi_period: int = 14, atr_period: int = 14, history: int = 5) -> str:
    """Indicators computed server-side on CLOSED candles (the forming candle is excluded):
    EMAs (comma-separated periods), RSI (Wilder), ATR (Wilder), session VWAP (current
    day, tick-volume weighted), plus the last `history` values of each for slope."""
    _connect()
    try:
        periods = sorted({int(p) for p in ema_periods.split(",") if p.strip()})
    except ValueError:
        return _ok({"error": f"invalid ema_periods '{ema_periods}'"})
    need = max(periods + [rsi_period + 1, atr_period]) * 4 + history
    rates, err = _rates(symbol, timeframe, max(need, 300) + 1)
    if err:
        return _ok(err)
    rates = rates[:-1]  # drop forming candle
    digits = mt5.symbol_info(symbol).digits
    h, l, c = (rates[f].astype(float) for f in ("high", "low", "close"))
    k = max(history, 1)

    # session VWAP from the day's M1 bars, independent of the requested timeframe/count
    day_start = int(rates["time"][-1]) // 86400 * 86400
    day = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, day_start, int(rates["time"][-1]) + 86400)
    vwap = None
    if day is not None and len(day) and day["tick_volume"].sum():
        typical = (day["high"] + day["low"] + day["close"]) / 3
        vwap = float((typical * day["tick_volume"]).sum() / day["tick_volume"].sum())

    atr = _atr(h, l, c, atr_period)
    return _ok({
        "symbol": symbol, "timeframe": timeframe.upper(),
        "last_closed_candle": _ts(int(rates["time"][-1])),
        "close": round(c[-1], digits),
        "ema": {str(p): _last(_ema(c, p), k, digits) for p in periods},
        "rsi": _last(_rsi(c, rsi_period), k, 2),
        "atr": _last(atr, k, digits),
        "vwap_session": round(vwap, digits) if vwap is not None else None,
        "range_last_n": {
            "n": k, "high": round(float(h[-k:].max()), digits), "low": round(float(l[-k:].min()), digits),
        },
        "note": "arrays are oldest -> newest; the last value is the most recent closed candle",
    })


def _latest_ind(symbol: str, timeframe: str) -> dict:
    """Latest closed-candle EMA9/21/50, RSI14, ATR14 for one timeframe."""
    rates, err = _rates(symbol, timeframe, 301)
    if err:
        return err
    rates = rates[:-1]
    d = mt5.symbol_info(symbol).digits
    h, l, c = (rates[f].astype(float) for f in ("high", "low", "close"))
    r = lambda v: None if np.isnan(v) else round(float(v), d)
    rsi = _rsi(c, 14)
    return {
        "close": r(c[-1]), "ema9": r(_ema(c, 9)[-1]), "ema21": r(_ema(c, 21)[-1]),
        "ema50": r(_ema(c, 50)[-1]), "rsi": [round(float(v), 1) for v in rsi[-3:]],
        "atr": r(_atr(h, l, c, 14)[-1]),
    }


@mcp.tool()
def mt5_snapshot(symbol: str, candles: int = 5, range_bars: int = 60) -> str:
    """One compact call for a trading decision: account equity, bid/ask/spread,
    open positions and pending orders for the symbol, M1 + M5 indicator summary
    (EMA9/21/50, last 3 RSI14, ATR14 on closed candles), the last `candles` M1 bars
    as [time HH:MM, o, h, l, c], and the high/low of the last `range_bars` M1 bars."""
    _connect()
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found"})
    info, tick, acc = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol), mt5.account_info()
    d = info.digits
    rates, err = _rates(symbol, "M1", max(candles, range_bars) + 1)
    if err:
        return _ok(err)
    closed = rates[:-1]
    win = closed[-range_bars:]
    poss = mt5.positions_get(symbol=symbol) or []
    orders = mt5.orders_get(symbol=symbol) or []
    return _ok({
        "time": _ts(tick.time) if tick else None,
        "equity": acc.equity if acc else None, "balance": acc.balance if acc else None,
        "bid": tick.bid if tick else None, "ask": tick.ask if tick else None,
        "spread": round(info.spread * info.point, d),
        "positions": [{
            "ticket": p.ticket, "dir": "LONG" if p.type == 0 else "SHORT", "vol": p.volume,
            "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit, "comment": p.comment,
        } for p in poss],
        "orders": [{
            "ticket": o.ticket, "type": ORDER_TYPE_NAMES.get(o.type, o.type), "vol": o.volume_current,
            "price": o.price_open, "sl": o.sl, "tp": o.tp,
        } for o in orders],
        "m1": _latest_ind(symbol, "M1"), "m5": _latest_ind(symbol, "M5"),
        "m1_last": [[datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).strftime("%H:%M"),
                     round(float(r["open"]), d), round(float(r["high"]), d),
                     round(float(r["low"]), d), round(float(r["close"]), d)] for r in closed[-candles:]],
        f"m1_range_{range_bars}": {"high": round(float(win["high"].max()), d),
                                   "low": round(float(win["low"].min()), d)},
    })


@mcp.tool()
def mt5_calc(symbol: str, direction: str, lot: float,
             entry: float = 0.0, sl: float = 0.0, tp: float = 0.0) -> str:
    """Pre-trade calculator: required margin, loss at SL and profit at TP in account
    currency, reward:risk ratio, and the free margin left. entry=0 uses the current
    ask (LONG) / bid (SHORT)."""
    _connect()
    direction = _direction(direction)
    if direction not in ("LONG", "SHORT"):
        return _ok({"error": "direction must be LONG or SHORT (BUY/SELL also accepted)"})
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found"})
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _ok({"error": "no tick data"})
    otype = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
    price = entry or (tick.ask if direction == "LONG" else tick.bid)
    margin = mt5.order_calc_margin(otype, symbol, lot, price)
    risk = mt5.order_calc_profit(otype, symbol, lot, price, sl) if sl else None
    reward = mt5.order_calc_profit(otype, symbol, lot, price, tp) if tp else None
    acc = mt5.account_info()
    out = {
        "symbol": symbol, "direction": direction, "lot": lot, "entry": price,
        "margin_required": margin,
        "loss_at_sl": round(risk, 2) if risk is not None else None,
        "profit_at_tp": round(reward, 2) if reward is not None else None,
        "reward_to_risk": round(reward / -risk, 2) if risk and reward and risk < 0 else None,
        "free_margin_after": round(acc.margin_free - margin, 2) if acc and margin is not None else None,
    }
    if risk is not None and acc:
        out["loss_at_sl_pct_of_balance"] = round(-risk / acc.balance * 100, 2) if acc.balance else None
    return _ok(out)


@mcp.tool()
def mt5_positions(symbol: str = "", magic: int = 0) -> str:
    """Open positions, optionally filtered by symbol and/or magic number (0 = no filter)."""
    _connect()
    poss = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    poss = poss or []
    if magic:
        poss = [p for p in poss if p.magic == magic]
    return _ok({"count": len(poss), "positions": [_pos_dict(p) for p in poss]})


@mcp.tool()
def mt5_orders(symbol: str = "", magic: int = 0) -> str:
    """Active pending orders, optionally filtered by symbol and/or magic number (0 = no filter)."""
    _connect()
    orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    orders = orders or []
    if magic:
        orders = [o for o in orders if o.magic == magic]
    return _ok({"count": len(orders), "orders": [_order_dict(o) for o in orders]})


@mcp.tool()
def mt5_order_open(symbol: str, direction: str, lot: float,
                   sl: float = 0.0, tp: float = 0.0,
                   magic: int = 777000, comment: str = "mcp", deviation: int = 0) -> str:
    """Open a market position. direction: 'LONG' or 'SHORT' (BUY/SELL accepted). sl/tp are absolute prices (0 = none).
    deviation = max slippage in points (0 = auto: 2x current spread, min 30).
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    direction = _direction(direction)
    if direction not in ("LONG", "SHORT"):
        return _ok({"error": "direction must be LONG or SHORT (BUY/SELL also accepted)"})
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found"})
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _ok({"error": "no tick data"})
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if direction == "LONG" else tick.bid,
        "deviation": _deviation(symbol, deviation), "magic": magic, "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(symbol),
    }
    if sl:
        req["sl"] = sl
    if tp:
        req["tp"] = tp
    res, err = _send(req)
    if err:
        return _ok(err)
    return _ok({"opened": True, "ticket": res.order, "price": res.price,
                "direction": direction, "volume": res.volume, "magic": magic})


@mcp.tool()
def mt5_pending_open(symbol: str, order_type: str, lot: float, price: float,
                     sl: float = 0.0, tp: float = 0.0, expiration_minutes: int = 0,
                     magic: int = 777000, comment: str = "mcp") -> str:
    """Place a pending order. order_type: BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP.
    price/sl/tp are absolute prices (sl/tp 0 = none). expiration_minutes 0 = good till cancelled.
    BUY_LIMIT/SELL_STOP must be below the current price, SELL_LIMIT/BUY_STOP above it.
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    otype_name = order_type.upper()
    otype = PENDING_TYPES.get(otype_name)
    if otype is None:
        return _ok({"error": f"order_type must be one of {list(PENDING_TYPES)}"})
    if not mt5.symbol_select(symbol, True):
        return _ok({"error": f"symbol '{symbol}' not found"})
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _ok({"error": "no tick data"})
    ref = tick.ask if otype_name.startswith("BUY") else tick.bid
    must_be_below = otype_name in ("BUY_LIMIT", "SELL_STOP")
    if (must_be_below and price >= ref) or (not must_be_below and price <= ref):
        side = "below" if must_be_below else "above"
        return _ok({"error": f"{otype_name} price {price} must be {side} current "
                             f"{'ask' if otype_name.startswith('BUY') else 'bid'} {ref}"})
    req = {
        "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": lot,
        "type": otype, "price": price, "magic": magic, "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
    }
    if sl:
        req["sl"] = sl
    if tp:
        req["tp"] = tp
    if expiration_minutes > 0:
        req["type_time"] = mt5.ORDER_TIME_SPECIFIED
        req["expiration"] = tick.time + expiration_minutes * 60
    # brokers differ on which filling mode they accept for pending orders
    err = None
    for filling in dict.fromkeys((mt5.ORDER_FILLING_RETURN, _filling(symbol))):
        res, err = _send({**req, "type_filling": filling})
        if not err:
            return _ok({"placed": True, "ticket": res.order, "type": otype_name,
                        "price": price, "volume": lot, "sl": sl, "tp": tp,
                        "expires_at": _ts(req["expiration"]) if "expiration" in req else None})
        if res is None or res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            break
    return _ok(err)


@mcp.tool()
def mt5_order_cancel(ticket: int) -> str:
    """Cancel (delete) one pending order by ticket.
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    orders = mt5.orders_get(ticket=ticket)
    if not orders:
        return _ok({"error": f"no pending order with ticket {ticket}"})
    res, err = _send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
    if err:
        return _ok(err)
    return _ok({"cancelled": True, "ticket": ticket})


@mcp.tool()
def mt5_modify(ticket: int, sl: float | None = None, tp: float | None = None) -> str:
    """Change SL and/or TP of an open position. Omit a value to keep it, pass 0 to remove it.
    Use for break-even moves and trailing stops.
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    if sl is None and tp is None:
        return _ok({"error": "provide sl and/or tp"})
    p = _get_position(ticket)
    if p is None:
        return _ok({"error": f"no open position with ticket {ticket}"})
    new_sl = p.sl if sl is None else sl
    new_tp = p.tp if tp is None else tp
    res, err = _send({
        "action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": ticket,
        "sl": new_sl, "tp": new_tp,
    })
    if err:
        return _ok(err)
    return _ok({"modified": True, "ticket": ticket,
                "sl": {"old": p.sl, "new": new_sl}, "tp": {"old": p.tp, "new": new_tp}})


@mcp.tool()
def mt5_close(ticket: int = 0, symbol: str = "", magic: int = 0,
              confirm_all: bool = False, deviation: int = 0) -> str:
    """Close positions: a specific ticket, or all matching symbol and/or magic filters.
    Closing by symbol/magic without a ticket closes EVERY matching position and
    requires confirm_all=True. deviation = max slippage in points (0 = auto).
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    if not ticket and not symbol and not magic:
        return _ok({"error": "provide ticket, symbol or magic — refusing to close all positions blindly"})
    poss = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    poss = [p for p in (poss or [])
            if (not ticket or p.ticket == ticket) and (not magic or p.magic == magic)]
    if not poss:
        return _ok({"closed": 0, "note": "no matching positions"})
    if not ticket and not confirm_all:
        return _ok({"error": f"{len(poss)} positions match — pass confirm_all=True to close all of them",
                    "tickets": [p.ticket for p in poss]})
    results = [_close_position(p, p.volume, deviation) for p in poss]
    return _ok({"closed": sum(1 for r in results if r["closed"]), "results": results})


@mcp.tool()
def mt5_close_partial(ticket: int, volume: float, deviation: int = 0) -> str:
    """Close part of an open position (e.g. 0.02 of 0.03). The remainder keeps its SL/TP.
    volume is rounded down to the symbol's volume step.
    DEMO-ONLY unless the server was started with MT5_ALLOW_REAL=1."""
    _connect()
    _guard_trading()
    p = _get_position(ticket)
    if p is None:
        return _ok({"error": f"no open position with ticket {ticket}"})
    info = mt5.symbol_info(p.symbol)
    step = info.volume_step if info else 0.01
    volume = round(int(volume / step + 1e-9) * step, 8)
    if volume < (info.volume_min if info else step):
        return _ok({"error": f"volume below the symbol minimum {info.volume_min if info else step}"})
    if volume >= p.volume:
        return _ok({"error": f"volume {volume} >= position volume {p.volume} — use mt5_close for a full close"})
    result = _close_position(p, volume, deviation)
    if result["closed"]:
        result["remaining_volume"] = round(p.volume - volume, 8)
    return _ok(result)


@mcp.tool()
def mt5_history(days: int = 7, symbol: str = "", magic: int = 0) -> str:
    """Closed deals from the last N days, optionally filtered by symbol/magic.
    Each deal carries position_id (to pair entries with exits) and its
    commission/swap/fee; realized P&L is net of all of them."""
    _connect()
    frm = datetime.now(timezone.utc) - timedelta(days=days)
    deals = mt5.history_deals_get(frm, datetime.now(timezone.utc) + timedelta(days=1)) or []
    out = []
    for d in deals:
        if d.type not in (0, 1):  # buy/sell deals only
            continue
        if symbol and d.symbol != symbol:
            continue
        if magic and d.magic != magic:
            continue
        fee = getattr(d, "fee", 0.0)
        out.append({
            "time": _ts(d.time),
            "symbol": d.symbol, "type": "BUY" if d.type == 0 else "SELL",
            "entry_or_exit": DEAL_ENTRY_NAMES.get(d.entry, d.entry),
            "volume": d.volume, "price": round(d.price, 6),
            "profit": d.profit, "commission": d.commission, "swap": d.swap, "fee": fee,
            "net": round(d.profit + d.commission + d.swap + fee, 2),
            "magic": d.magic, "ticket": d.ticket, "position_id": d.position_id,
            "comment": d.comment,
        })
    return _ok({
        "days": days, "deals": out,
        "realized_pnl_gross": round(sum(x["profit"] for x in out), 2),
        "costs": round(sum(x["commission"] + x["swap"] + x["fee"] for x in out), 2),
        "realized_pnl": round(sum(x["net"] for x in out), 2),
    })


if __name__ == "__main__":
    mcp.run()
