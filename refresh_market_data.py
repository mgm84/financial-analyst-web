"""
Refresco diario de precios y rendimientos por periodo — para el dashboard
===========================================================================
Corre en GitHub Actions (tiene salida libre a internet, mi sandbox no),
escribe JSON, Claude lo lee y lo sincroniza al dashboard y al correo.

Calcula, para cada ticker, precio actual y variación % en varios periodos
frente al cierre más reciente disponible en o antes de cada fecha de
referencia (mismo criterio que usa screen_percentiles_v3.py con valor_en),
salvo 1D (ver nota abajo). También escribe el tipo de cambio EUR/USD del día.

NOTA 1D (fix 2026-09-15): antes se calculaba con "hoy - 1 dia calendario"
como fecha de referencia. El job corre a las 06:00 UTC, antes de la
apertura de NYSE -- a esa hora "el precio de hoy" (quote) y "el cierre de
ayer" (historico) resuelven ambos al mismo cierre de la ultima sesion, asi
que la resta siempre daba 0%. Fix: 1D ya no usa fechas de calendario, usa
directamente los dos ultimos cierres de la serie historica (el ultimo
cierre disponible vs el cierre de la sesion anterior a esa) -- funciona
igual corra el job cuando corra, y es justo lo que pidio Mariano: "el 1D
seria el cierre del dia anterior".

NOTA EU_TICKERS (añadido 2026-09-15): EQQQ y VUSA cotizan en bolsas
europeas (Xetra/LSE/Euronext) que el plan de FMP contratado no cubre.
FMP no tiene API pública documentada para esas plazas en este plan; en vez
de dar de alta un proveedor nuevo (EODHD) solo para dos ETFs que "no se
tocan de momento", uso yfinance (gratis, sin API key, ya estaba previsto
como complemento en el plan original del proyecto). Corren en una segunda
pasada, con sus propias funciones de descarga, pero reutilizan el mismo
periodo_pct/chg_ultimo_cierre/high_52w que los tickers de FMP.

VUSA confirmado por Mariano (2026-09-15): Euronext Amsterdam, EUR, sufijo
.AS -- verificado que existe en Yahoo Finance. No hace falta conversion de
divisa para este, a diferencia de TSM/ASML en screen_percentiles_v3.py.

EQQQ: OJO, sigue sin confirmar del todo. Mariano dijo "Amsterdam" para
los dos, pero al verificar por mi cuenta la cotizacion principal de EQQQ
en Euronext es Paris (XPAR), no Amsterdam -- Yahoo Finance no me devuelve
resultado para EQQQ.AS en la busqueda, si para EQQQ.PA (Paris), EQQQ.DE
(Xetra) y EQQQ.L (Londres). Dejo EQQQ.AS puesto como placeholder: si no
existe de verdad, el script lo registra en tickers_fallidos y sigue sin
romperse, pero muy probablemente falle cada dia hasta que se corrija.
Pendiente de que Mariano confirme mirando el ticker exacto en su DEGIRO.

NO cubre noticias (eso sigue siendo manual). Tampoco calcula nada de
cantidades/valor de cartera -- eso vive en un fichero de posiciones aparte
(pendiente) que se combina con estos precios en el paso de sync.

FIX 2026-09-17 (detectado por Mariano en el correo diario -- "los datos
son del cierre de un dia antes de lo que pone"): el JSON solo llevaba
"generado_en_utc" (el instante en que corrio el script), y
build_emails_prod.py usaba esa fecha tal cual para el titulo "Cierre
{fecha}". El job corre a las 06:00 UTC, horas antes de que abra
(13:30 UTC) y cierre (20:00 UTC) el NYSE -- asi que el precio que se trae
SIEMPRE es el del ultimo cierre ya consumado, que es el dia BURSATIL
ANTERIOR al dia en que corre el script. Al usar la fecha de ejecucion
como si fuera la fecha del cierre, el correo quedaba etiquetado un dia
por delante de los precios reales.

Fix: se anade "fecha_cierre" a nivel raiz del JSON (fecha real, en
formato YYYY-MM-DD, del ultimo cierre disponible en la serie historica
del primer ticker US que responda bien -- todos los tickers US cotizan
en el mismo calendario NYSE/Nasdaq) y tambien "fecha" por ticker
(informativo, por si algun dia hay que depurar un desfase puntual entre
plazas). compute_patrimonio.py y build_emails_prod.py se actualizan para
leer "fecha_cierre" en vez de derivar la fecha de "generado_en_utc".
"""

import os
import json
import time
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf

API_KEY = os.environ["FMP_API_KEY"]

BASE = "https://financialmodelingprep.com/stable"
EP_QUOTE = f"{BASE}/quote"
EP_HIST = f"{BASE}/historical-price-eod/full"

PAUSA = 0.4
DIAS_HISTORIA = 400  # ~52 semanas + margen, cubre también 1Y

TICKERS = [
    "EQIX", "ASML", "AVGO", "TSM", "PWR", "ETN", "ANET",   # Capas sectoriales IA
    "GOOGL", "AMD", "MSFT",                                   # Equity (acciones, DEGIRO) -- PLTR vendida 2026-09-15, ya no se pide
    "VOO", "QQQ", "QQQM", "IVW",                              # GBM México
]

# Sufijo .AS = Euronext Amsterdam (EUR) -- confirmado por Mariano.
EU_TICKERS = {
    "EQQQ": "EQQQ.DE",   # Invesco EQQQ Nasdaq-100 UCITS ETF (DEGIRO)
    "VUSA": "VUSA.AS",   # Vanguard S&P 500 UCITS ETF (DEGIRO)
}

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


def historical_close_series_yf(symbol, desde):
    """Equivalente a historical_close_series pero via yfinance, para
    tickers europeos que FMP no cubre en el plan contratado."""
    hist = yf.Ticker(symbol).history(start=desde, auto_adjust=False)
    if hist.empty:
        raise ValueError(f"yfinance sin datos para {symbol}")
    df = hist.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.sort_values("date").reset_index(drop=True)


def quote_yf(symbol):
    info = yf.Ticker(symbol).fast_info
    price = float(info["last_price"])
    currency = str(info.get("currency", "?"))
    return price, currency


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


def chg_ultimo_cierre(df):
    """1D real: ultimo cierre vs el cierre de la sesion inmediatamente
    anterior, sin pasar por fechas de calendario. Ver nota en la cabecera
    del fichero -- esto es lo que corrige el bug del 1D siempre en 0%."""
    if len(df) < 2:
        return None
    ultimo = float(df.iloc[-1]["close"])
    anterior = float(df.iloc[-2]["close"])
    if anterior == 0:
        return None
    return round((ultimo / anterior - 1) * 100, 2)


def high_52w(df):
    ventana = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=365))]
    if len(ventana) == 0:
        return None
    return round(float(ventana["close"].max()), 2)


def chg_periodos(df, price, hasta, year_start):
    return {
        "1D": chg_ultimo_cierre(df),
        "5D": periodo_pct(df, price, hasta, dias_atras=7),
        "1M": periodo_pct(df, price, hasta, dias_atras=30),
        "3M": periodo_pct(df, price, hasta, dias_atras=91),
        "6M": periodo_pct(df, price, hasta, dias_atras=182),
        "YTD": periodo_pct(df, price, hasta, fecha_ref=year_start),
        "1Y": periodo_pct(df, price, hasta, dias_atras=365),
    }


if __name__ == "__main__":
    hasta = pd.Timestamp.today().normalize()
    desde = (hasta - pd.Timedelta(days=DIAS_HISTORIA)).strftime("%Y-%m-%d")
    hasta_str = hasta.strftime("%Y-%m-%d")

    year_start = pd.Timestamp(year=hasta.year, month=1, day=1)

    resultado = {}
    fallos = []
    fecha_cierre_us = None  # fecha real (YYYY-MM-DD) del ultimo cierre US -- ver FIX 2026-09-17 en la cabecera

    # --- Tickers US/globales via FMP ---
    for t in TICKERS:
        try:
            df = historical_close_series(t, desde, hasta_str)
            quote = get_json(EP_QUOTE, symbol=t)
            price = float(quote[0]["price"])

            chg = chg_periodos(df, price, hasta, year_start)
            ultima_fecha = df.iloc[-1]["date"].strftime("%Y-%m-%d") if len(df) else None
            if fecha_cierre_us is None:
                fecha_cierre_us = ultima_fecha

            resultado[t] = {
                "price": price,
                "currency": "USD",
                "chg": chg,
                "high52": high_52w(df),
                "fecha": ultima_fecha,
            }
            print(f"  {t}: ok  ${price:,.2f}  1D={chg['1D']}%  (cierre {ultima_fecha})")
        except Exception as e:
            fallos.append(t)
            print(f"  {t}: FALLO -> {type(e).__name__}: {e}")

    # --- Tickers europeos via yfinance (FMP no los cubre en este plan) ---
    for nombre, simbolo_yf in EU_TICKERS.items():
        try:
            df = historical_close_series_yf(simbolo_yf, desde)
            price, currency = quote_yf(simbolo_yf)

            chg = chg_periodos(df, price, hasta, year_start)
            ultima_fecha = df.iloc[-1]["date"].strftime("%Y-%m-%d") if len(df) else None

            resultado[nombre] = {
                "price": price,
                "currency": currency,
                "chg": chg,
                "high52": high_52w(df),
                "fecha": ultima_fecha,
            }
            print(f"  {nombre} ({simbolo_yf}): ok  {price:,.2f} {currency}  1D={chg['1D']}%  (cierre {ultima_fecha})")
        except Exception as e:
            fallos.append(nombre)
            print(f"  {nombre} ({simbolo_yf}): FALLO -> {type(e).__name__}: {e}")

    # EUR/USD
    try:
        fx_df = historical_close_series("EURUSD", desde, hasta_str)
        fx_quote = get_json(EP_QUOTE, symbol="EURUSD")
        eur_usd = float(fx_quote[0]["price"])
    except Exception as e:
        eur_usd = None
        print(f"  EURUSD: FALLO -> {type(e).__name__}: {e}")

    if fecha_cierre_us is None:
        # Ningun ticker US respondio bien (dia muy malo) -- como ultimo recurso,
        # usa la fecha de cualquier ticker que si haya salido (p.ej. uno EU),
        # para no dejar fecha_cierre en null si hay datos utilizables.
        fecha_cierre_us = next((v["fecha"] for v in resultado.values() if v.get("fecha")), None)

    salida = {
        "generado_en_utc": pd.Timestamp.utcnow().isoformat(),
        "fecha_cierre": fecha_cierre_us,
        "eur_usd": eur_usd,
        "tickers_fallidos": fallos,
        "market": resultado,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(salida, indent=2, ensure_ascii=False))
    print(f"\nEscrito {OUT_PATH}")
