"""
Screen de percentiles de valoracion — version automatizada (GitHub Actions)
=============================================================================
Misma logica que screen_percentiles_v3.py (Colab). Unicos cambios:
  1. API key desde la variable de entorno FMP_API_KEY (GitHub Secret),
     no desde google.colab.userdata.
  2. Ademas del log en texto, escribe data/percentiles.json para que
     otro proceso (el dashboard) lo lea sin parsear texto.
"""

import os
import json
import time
from pathlib import Path

import requests
import pandas as pd
import numpy as np

API_KEY = os.environ["FMP_API_KEY"]

TRIMESTRES = 28
PAUSA = 0.4
BASE = "https://financialmodelingprep.com/stable"

EP = {
    "income":   f"{BASE}/income-statement",
    "cashflow": f"{BASE}/cash-flow-statement",
    "balance":  f"{BASE}/balance-sheet-statement",
    "quote":    f"{BASE}/quote",
    "precios":  f"{BASE}/historical-price-eod/full",
}

CARTERA = {
    "ASML": {"peso": 16, "metricas": ["P/FCF", "PER", "EV/EBITDA"]},
    "ETN":  {"peso": 11, "metricas": ["EV/EBITDA", "PER", "P/FCF"]},
    "AVGO": {"peso": 13, "metricas": ["P/FCF", "EV/EBITDA", "PER"]},
    "TSM":  {"peso": 15, "metricas": ["P/FCF", "PER", "EV/EBITDA"]},
    "EQIX": {"peso": 17, "metricas": ["P/FFO", "EV/EBITDA"]},
    "PWR":  {"peso": 14, "metricas": ["EV/EBITDA", "PER", "P/FCF"]},
    "ANET": {"peso": 14, "metricas": ["PER", "P/FCF", "EV/EBITDA"]},
}

CONVERSION = {
    "TSM":  {"fx": "USDTWD", "invertir": False, "adr": 1},
    "ASML": {"fx": "EURUSD", "invertir": True,  "adr": 1},
}

OUT_PATH = Path("data/percentiles.json")


def get(url, **params):
    params["apikey"] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    time.sleep(PAUSA)
    if isinstance(data, dict):
        for k in ("historical", "data"):
            if k in data:
                return data[k]
    return data


def col(df, *nombres):
    for n in nombres:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def serie_precios(symbol, desde):
    filas = get(EP["precios"], symbol=symbol,
                **{"from": desde, "to": pd.Timestamp.today().strftime("%Y-%m-%d")})
    p = pd.DataFrame(filas)[["date", "close"]]
    p["date"] = pd.to_datetime(p["date"])
    return p.sort_values("date").reset_index(drop=True)


def valor_en(serie, fecha):
    prev = serie[serie["date"] <= fecha]
    return float(prev.iloc[-1]["close"]) if len(prev) else np.nan


def estados(ticker):
    inc = pd.DataFrame(get(EP["income"],   symbol=ticker, period="quarter", limit=TRIMESTRES))
    cfl = pd.DataFrame(get(EP["cashflow"], symbol=ticker, period="quarter", limit=TRIMESTRES))
    bal = pd.DataFrame(get(EP["balance"],  symbol=ticker, period="quarter", limit=TRIMESTRES))

    moneda = inc.get("reportedCurrency", pd.Series(["?"])).iloc[0]

    df = pd.DataFrame({"date": inc["date"]})
    df["netIncome"] = col(inc, "netIncome")
    df["ebitda"]    = col(inc, "ebitda", "EBITDA")
    df["opIncome"]  = col(inc, "operatingIncome")
    df["acciones"]  = col(inc, "weightedAverageShsOutDil", "weightedAverageShsOut")

    cfl_i, bal_i = cfl.set_index("date"), bal.set_index("date")
    df = df.set_index("date")
    df["da"]    = col(cfl_i, "depreciationAndAmortization")
    df["ocf"]   = col(cfl_i, "operatingCashFlow", "netCashProvidedByOperatingActivities")
    df["capex"] = col(cfl_i, "capitalExpenditure")
    df["fcf"]   = col(cfl_i, "freeCashFlow")
    df["deuda"] = col(bal_i, "totalDebt")
    df["caja"]  = col(bal_i, "cashAndShortTermInvestments", "cashAndCashEquivalents")
    df["netDebt_api"] = col(bal_i, "netDebt")

    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df["fcf"]     = df["fcf"].fillna(df["ocf"] + df["capex"])
    df["ebitda"]  = df["ebitda"].fillna(df["opIncome"] + df["da"])
    df["netDebt"] = df["netDebt_api"].fillna(df["deuda"] - df["caja"])

    return df, moneda


def tabla_multiplos(ticker, es_reit=False):
    df, moneda = estados(ticker)

    for c in ["netIncome", "ebitda", "fcf", "da"]:
        df[c + "_ttm"] = df[c].rolling(4).sum()
    df = df.dropna(subset=["netIncome_ttm"]).reset_index(drop=True)

    desde = df["date"].min().strftime("%Y-%m-%d")
    precios = serie_precios(ticker, desde)
    p_hoy_usd = float(get(EP["quote"], symbol=ticker)[0]["price"])

    conv = CONVERSION.get(ticker)
    if conv:
        fx = serie_precios(conv["fx"], desde)
        if conv["invertir"]:
            fx["close"] = 1.0 / fx["close"]
        adr = conv["adr"]

        def a_local(precio_usd, fecha):
            tipo = valor_en(fx, fecha)
            return precio_usd * tipo / adr
    else:
        def a_local(precio_usd, fecha):
            return precio_usd

    df["precio"] = [a_local(valor_en(precios, d), d) for d in df["date"]]

    hoy = df.iloc[-1].copy()
    hoy["date"] = pd.Timestamp.today().normalize()
    hoy["precio"] = a_local(p_hoy_usd, pd.Timestamp.today().normalize())
    df = pd.concat([df, hoy.to_frame().T], ignore_index=True)

    df["mktcap"] = df["precio"] * df["acciones"]
    df["ev"] = df["mktcap"] + df["netDebt"]

    df["PER"]       = df["mktcap"] / df["netIncome_ttm"]
    df["P/FCF"]     = df["mktcap"] / df["fcf_ttm"]
    df["EV/EBITDA"] = df["ev"] / df["ebitda_ttm"]
    if es_reit:
        df["P/FFO"] = df["mktcap"] / (df["netIncome_ttm"] + df["da_ttm"])

    return df, p_hoy_usd, moneda


def banda(pct):
    return "COMODA" if pct < 50 else ("normal" if pct <= 80 else "CARA -> escalonar")


def analizar(ticker, cfg):
    df, p_hoy, moneda = tabla_multiplos(ticker, es_reit="P/FFO" in cfg["metricas"])

    filas = []
    for m in cfg["metricas"]:
        if m not in df.columns:
            continue
        serie_m = pd.to_numeric(df[m], errors="coerce").replace([np.inf, -np.inf], np.nan)
        hist = serie_m.iloc[:-1].dropna()
        hist = hist[hist > 0]
        valor = serie_m.iloc[-1]
        if len(hist) < 8 or pd.isna(valor) or valor <= 0:
            continue
        pct = (hist < valor).mean() * 100
        filas.append({
            "ticker": ticker, "peso_pct": cfg["peso"], "metrica": m,
            "hoy": round(float(valor), 1), "mediana": round(float(hist.median()), 1),
            "min": round(float(hist.min()), 1), "max": round(float(hist.max()), 1),
            "percentil": round(float(pct)), "banda": banda(pct),
            "es_clave": m == cfg["metricas"][0],
        })
    return filas, p_hoy, moneda


if __name__ == "__main__":
    todo, monedas = [], {}

    for ticker, cfg in CARTERA.items():
        try:
            filas, p, mon = analizar(ticker, cfg)
            todo.extend(filas)
            monedas[ticker] = mon
            marca = "  <- convertido" if ticker in CONVERSION else ""
            print(f"  {ticker}: ok  ADR ${p:,.2f}  reporta en {mon}{marca}")
        except Exception as e:
            print(f"  {ticker}: FALLO -> {type(e).__name__}: {e}")

    sin_conv = [t for t, m in monedas.items()
                if m not in ("USD", "?") and t not in CONVERSION]
    if sin_conv:
        print(f"\n  AVISO: {sin_conv} no reportan en USD y NO estan en CONVERSION.")

    resultado = {
        "generado_en_utc": pd.Timestamp.utcnow().isoformat(),
        "avisos_conversion_faltante": sin_conv,
        "monedas_reportadas": monedas,
        "metricas": todo,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(resultado, indent=2, ensure_ascii=False))
    print(f"\nEscrito {OUT_PATH}")

    df = pd.DataFrame(todo)
    if len(df):
        clave = df[df["es_clave"]].sort_values("percentil")
        print("\nRESUMEN — metrica clave, mas barato a mas caro")
        print(clave[["ticker", "peso_pct", "metrica", "hoy", "mediana", "percentil", "banda"]].to_string(index=False))
