"""
Refresco diario de precios y rendimientos por periodo — para el dashboard
===========================================================================
No estaba en tu pedido original (solo pediste automatizar monitor_tramos y
screen_percentiles), pero sin esto el dashboard se queda con los precios
del día que yo los escribí a mano y nunca se actualiza — "monitorización
cercana" con datos de hace una semana no sirve. Mismo patrón que los otros
dos: corre en GitHub Actions (tiene salida a FMP, mi sandbox no), escribe
JSON, Claude lo lee y lo sincroniza al dashboard.

Calcula, para cada ticker, precio actual y variación % en 1D/5D/3M/6M/YTD/1Y
frente al cierre más reciente disponible en o antes de cada fecha de
referencia (mismo criterio que usa screen_percentiles_v3.py con valor_en).
También escribe el tipo de cambio EUR/USD del día.

NO cubre EQQQ/ETF S&P 500 de tu cartera Equity (tickers sin confirmar) ni
noticias (eso sigue siendo manual). Si confirmas esos tickers, se añaden
a TICKERS abajo y ya quedan cubiertos.
"""

import os
import json
import time
from pathlib import Path

import requests
import pandas as pd

API_KEY = os.environ["FMP_API_KEY"]

BASE = "https://financialmodelingprep.com/stable"
EP_QUOTE = f"{BASE}/quote"
EP_HIST = f"{BASE}/historical-price-eod/full"

PAUSA = 0.4
DIAS_HISTORIA = 400  # ~52 semanas + margen, cubre también 1Y

TICKERS = [
    "EQIX", "ASML", "AVGO", "TSM", "PWR", "ETN", "ANET",   # Capas sectoriales IA
    "GOOGL", "AMD", "MSFT", "PLTR",                          # Equity (acciones)
    "VOO", "QQQ", "IVW",                                     # GBM México
]

OUT_PATH = Path("data/market_data.json")


def get_json(url, **params):
    params["apikey"] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(PAUSA)
    return r.json()


def historical_close_series(symbol, desde, hasta):
    data = get_json(EP_HIST, symbol=symbol, **{"from": desde, "to": hasta})
    filas = data.get("historical", data) if isinstance(data, dict) else data
    df = pd.DataFrame(filas)[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def valor_en_o_antes(df, fecha):
    prev = df[df["date"] <= fecha]
    if len(prev) == 0:
        return None
    return float(prev.iloc[-1]["close"])


def periodo_pct(df, hoy_precio, hoy_fecha, dias_atras=None, fecha_ref=None):
    ref_fecha = fecha_ref if fecha_ref is not None else (hoy_fecha - pd.Timedelta(days=dias_atras))
    ref_precio = valor_en_o_antes(df, ref_fecha)
    if ref_precio is None or ref_precio == 0:
        return None
    return round((hoy_precio / ref_precio - 1) * 100, 2)


def high_52w(df):
    ventana = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=365))]
    if len(ventana) == 0:
        return None
    return round(float(ventana["close"].max()), 2)


if __name__ == "__main__":
    hasta = pd.Timestamp.today().normalize()
    desde = (hasta - pd.Timedelta(days=DIAS_HISTORIA)).strftime("%Y-%m-%d")
    hasta_str = hasta.strftime("%Y-%m-%d")

    year_start = pd.Timestamp(year=hasta.year, month=1, day=1)

    resultado = {}
    fallos = []

    for t in TICKERS:
        try:
            df = historical_close_series(t, desde, hasta_str)
            quote = get_json(EP_QUOTE, symbol=t)
            price = float(quote[0]["price"])

            chg = {
                "1D": periodo_pct(df, price, hasta, dias_atras=1),
                "5D": periodo_pct(df, price, hasta, dias_atras=7),
                "3M": periodo_pct(df, price, hasta, dias_atras=91),
                "6M": periodo_pct(df, price, hasta, dias_atras=182),
                "YTD": periodo_pct(df, price, hasta, fecha_ref=year_start),
                "1Y": periodo_pct(df, price, hasta, dias_atras=365),
            }

            resultado[t] = {
                "price": price,
                "currency": "USD",
                "chg": chg,
                "high52": high_52w(df),
            }
            print(f"  {t}: ok  ${price:,.2f}  1D={chg['1D']}%")
        except Exception as e:
            fallos.append(t)
            print(f"  {t}: FALLO -> {type(e).__name__}: {e}")

    # EUR/USD
    try:
        fx_df = historical_close_series("EURUSD", desde, hasta_str)
        fx_quote = get_json(EP_QUOTE, symbol="EURUSD")
        eur_usd = float(fx_quote[0]["price"])
    except Exception as e:
        eur_usd = None
        print(f"  EURUSD: FALLO -> {type(e).__name__}: {e}")

    salida = {
        "generado_en_utc": pd.Timestamp.utcnow().isoformat(),
        "eur_usd": eur_usd,
        "tickers_fallidos": fallos,
        "market": resultado,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(salida, indent=2, ensure_ascii=False))
    print(f"\nEscrito {OUT_PATH}")
