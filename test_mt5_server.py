# -*- coding: utf-8 -*-
"""Smoke test de las tools del MCP de MT5 (solo lectura — no abre ordenes).

Uso: python test_mt5_server.py [SYMBOL]   (por defecto XAUUSD; en Exness p.ej. XAUUSDm)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import mt5_server as s

sym = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"

print("--- mt5_status ---");      print(s.mt5_status())
print("--- mt5_symbol_info ---"); print(s.mt5_symbol_info(sym))
print("--- mt5_candles ---");     print(s.mt5_candles(sym, "M15", 3))
print("--- mt5_indicators ---");  print(s.mt5_indicators(sym, "M5"))
print("--- mt5_snapshot ---");    print(s.mt5_snapshot(sym))
print("--- mt5_calc ---");        print(s.mt5_calc(sym, "LONG", 0.01))
print("--- mt5_positions ---");   print(s.mt5_positions(symbol=sym))
print("--- mt5_orders ---");      print(s.mt5_orders(symbol=sym))
print("--- mt5_history 1d ---");  print(s.mt5_history(days=1, symbol=sym)[:1500])
