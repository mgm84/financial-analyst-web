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

FIX 2026-09-22 (Mariano: "algunos dias tenemos problemas para obtener los
precios de EQQQ y VUSA, error de Yahoo"): yfinance/Yahoo falla de forma
intermitente (rate limiting o caida puntual del endpoint), y hasta ahora
eso dejaba a EQQQ/VUSA sin precio ese dia -- aparecian en
tickers_fallidos, sin valor en el dashboard ni en el correo. Fix: cuando
Yahoo no tiene un cierre de HOY fiable para uno de los EU_TICKERS, se usa
como referencia el ETF equivalente de mercado US que YA se pide via FMP
(EQQQ -> QQQ, ambos Nasdaq-100; VUSA -> VOO, ambos S&P 500 -- mismo indice
subyacente).

FIX 2026-09-23, y CORRECCION real 2026-09-26 v1 y v2 (ver el historial
completo en `claude/automatizacion-tramos-percentiles.md` del proyecto --
aqui solo el resumen de la version final, que es la que importa):

  - 23/09: encontrado que yfinance devuelve, para estos dos ETPs europeos
    de poco volumen, una ultima fila (la del dia mas reciente) con
    Close = NaN sin lanzar excepcion -- el "1D" salia vacio y el proxy
    nunca se disparaba porque no habia ningun error que lo activase. El
    primer intento de arreglo (descartar esa fila sin mas) estaba mal:
    dejaba "el ultimo cierre" apuntando a un dia entero mas viejo, sin
    avisar, y el 1D podia salir con el SIGNO CONTRARIO al movimiento
    real (visto en produccion el 26/09: EQQQ/VUSA en rojo el mismo dia
    que QQQ/VOO, mismos indices, cerraban en verde).

  - 26/09 v1: se cambio a lanzar una excepcion (activar el proxy) en vez
    de descartar la fila en silencio -- correcto en cuanto a CUANDO usar
    el proxy, pero el proxy en si tenia un bug de fondo que hasta
    entonces nunca se habia visto disparar de verdad: calculaba
    "price_eur = precio_del_proxy_en_usd / tipo_de_cambio", es decir,
    sustituia el precio ABSOLUTO del proxy (QQQ/VOO) por el de EQQQ/VUSA.
    Esto asume que el proxy y el ticker europeo tienen el mismo valor
    liquidativo (NAV) por participacion, ajustado solo por la divisa --
    cierto por pura coincidencia para EQQQ/QQQ (ambos rondan niveles de
    precio parecidos), pero FALSO para VUSA/VOO: VOO cotiza en torno a
    $700/participacion y VUSA en torno a 125-130 EUR/participacion --
    son estructuras de fondo distintas con el mismo indice subyacente,
    no la misma accion en dos monedas. Al dispararse el proxy en real por
    primera vez (26/09), esto infló el precio de VUSA a ~624 EUR (el
    nivel de VOO convertido a EUR) en vez de ~128 EUR reales, y disparo
    el patrimonio total de forma artificial (~+73.000 EUR de mas).

  - 26/09 v2 (ESTA VERSION, la correcta): el proxy ya NO sustituye el
    precio absoluto del proxy. En su lugar, usa el RETORNO relativo del
    proxy (convertido a EUR dia a dia, ya construido en
    `serie_proxy_en_eur`) entre el ULTIMO CIERRE REAL CONOCIDO de este
    mismo ticker (no del proxy) y hoy, y aplica ese retorno sobre el
    precio real del propio ticker: `price_eur = precio_base_real *
    (1 + retorno_proxy_en_eur)`. Asi el precio, el 1D, el 5D/1M/etc. y el
    high52 quedan siempre en la escala de precio REAL de EQQQ/VUSA, sea
    cual sea la escala del proxy -- el proxy solo aporta el movimiento
    relativo del dia, nunca un nivel absoluto. El "ultimo cierre real
    conocido" sale de la propia serie de yfinance si esta parcialmente
    disponible (fallo solo en la fila mas reciente), o si yfinance no
    responde nada en absoluto, del `data/market_data.json` que ya existe
    en el repo (el commit de ayer, antes de que este script lo
    sobreescriba) -- la cadena de proxies encadenados dia a dia no
    acumula el error de escala porque siempre se parte de un retorno, no
    de un precio absoluto ajeno.

Cada ticker que use proxy lleva "proxy": true, "proxy_de": <ticker US>,
"proxy_precio_base"/"proxy_fecha_base" (el ancla real usada) y
"proxy_retorno_aplicado_pct" en su entrada de "market". No se anade a
"tickers_fallidos" (si tiene precio, aunque sea proxy, no ha fallado) --
se anade a la nueva lista raiz "proxies_usados" en su lugar, para que sea
facil detectarlo sin recorrer todos los tickers.
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

# Proxy de mercado US para cuando Yahoo no tiene un cierre de HOY fiable
# para el equivalente europeo -- mismo indice subyacente, ya se pide de
# todas formas para GBM Mexico. Ver nota "CORRECCION 2026-09-26 v2" en la
# cabecera: se usa solo como fuente del RETORNO relativo del dia, nunca
# como sustituto del precio absoluto (QQQ y VOO NO tienen el mismo NAV
# por participacion que EQQQ/VUSA).
PROXY_TICKERS = {
    "EQQQ": "QQQ",   # Invesco EQQQ (Nasdaq-100 UCITS) -> Invesco QQQ (Nasdaq-100, US)
    "VUSA": "VOO",   # Vanguard S&P 500 UCITS -> Vanguard S&P 500 (US)
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
    tickers europeos que FMP no cubre en el plan contratado.

    Descarta cualquier fila sin cierre valido (yfinance devuelve, para
    estos dos ETPs de poco volumen, una fila del dia mas reciente con
    Close = NaN cuando ese cierre todavia no esta consolidado en el
    momento en que corre el job -- sin lanzar ninguna excepcion). NO se
    decide aqui si el resultado es "fresco" o no: eso lo compara el
    llamador contra fecha_cierre_us, porque solo el llamador sabe que
    fecha se espera. Ver "CORRECCION 2026-09-26" en la cabecera del
    fichero para el porque de este diseño (huyendo tanto de fingir un
    cierre de hoy que no lo es, como de descartar en silencio una serie
    que en realidad es utilizable como ancla del proxy)."""
    hist = yf.Ticker(symbol).history(start=desde, auto_adjust=False)
    if hist.empty:
        raise ValueError(f"yfinance sin datos para {symbol}")
    df = hist.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("date").reset_index(drop=True)
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if df.empty:
        raise ValueError(f"yfinance solo devuelve cierres NaN para {symbol}")
    return df


def quote_yf(symbol):
    info = yf.Ticker(symbol).fast_info
    price = float(info["last_price"])
    currency = str(info.get("currency", "?"))
    return price, currency


def ultimo_conocido_en_disco(ticker):
    """Best-effort: lee el market_data.json que YA existe en el repo (el
    commit de ayer, antes de que este mismo script lo sobreescriba al
    final) y devuelve (precio, fecha) del ultimo dato que tuviera ese
    ticker -- sea un cierre real o el resultado de un proxy de un dia
    anterior. Esto solo hace falta como ancla del proxy cuando yfinance
    no devuelve NADA hoy (ni siquiera una serie parcial) -- ver
    "CORRECCION 2026-09-26 v2" en la cabecera. La cadena de proxies
    encadenados dia a dia no acumula error de escala porque cada uno
    parte de un RETORNO relativo, nunca del precio absoluto del proxy.
    Devuelve (None, None) si no hay nada usable."""
    if not OUT_PATH.exists():
        return None, None
    try:
        anterior = json.loads(OUT_PATH.read_text())
        info = anterior.get("market", {}).get(ticker)
        if not info or info.get("price") is None or not info.get("fecha"):
            return None, None
        return float(info["price"]), pd.Timestamp(info["fecha"])
    except Exception:
        return None, None


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
    if anterior == 0 or pd.isna(ultimo) or pd.isna(anterior):
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


def serie_proxy_en_eur(df_proxy_usd, fx_df):
    """Convierte una serie historica de cierres USD (del ticker proxy) a
    EUR, dia a dia -- cada cierre se divide por el cierre EUR/USD de ESE
    MISMO dia (o el disponible mas reciente en o antes, via merge_asof),
    no por un tipo de cambio unico congelado. Necesario para que el
    RETORNO del proxy en EUR (usado para escalar el precio real de
    EQQQ/VUSA, ver CORRECCION 2026-09-26 v2) refleje el retorno real
    (indice en USD + movimiento de la divisa), no solo el retorno en USD
    del proxy."""
    df_eur = pd.merge_asof(
        df_proxy_usd.sort_values("date"),
        fx_df.sort_values("date"),
        on="date", suffixes=("", "_fx"), direction="backward",
    )
    df_eur["close"] = df_eur["close"] / df_eur["close_fx"]
    return df_eur[["date", "close"]].dropna().reset_index(drop=True)


if __name__ == "__main__":
    hasta = pd.Timestamp.today().normalize()
    desde = (hasta - pd.Timedelta(days=DIAS_HISTORIA)).strftime("%Y-%m-%d")
    hasta_str = hasta.strftime("%Y-%m-%d")

    year_start = pd.Timestamp(year=hasta.year, month=1, day=1)

    resultado = {}
    fallos = []
    proxies_usados = []
    fecha_cierre_us = None  # fecha real (YYYY-MM-DD) del ultimo cierre US -- ver FIX 2026-09-17 en la cabecera
    series_us = {}  # guarda el DataFrame historico de cada ticker US ya pedido -- se reutiliza si hace falta como proxy

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
            series_us[t] = df
            print(f"  {t}: ok  ${price:,.2f}  1D={chg['1D']}%  (cierre {ultima_fecha})")
        except Exception as e:
            fallos.append(t)
            print(f"  {t}: FALLO -> {type(e).__name__}: {e}")

    # --- EUR/USD (se pide ANTES de los tickers europeos: el proxy, si
    # hace falta, necesita la serie historica para construir el retorno
    # del proxy convertido a EUR dia a dia) ---
    fx_df = None
    eur_usd = None
    try:
        fx_df = historical_close_series("EURUSD", desde, hasta_str)
        fx_quote = get_json(EP_QUOTE, symbol="EURUSD")
        eur_usd = float(fx_quote[0]["price"])
    except Exception as e:
        print(f"  EURUSD: FALLO -> {type(e).__name__}: {e}")

    # --- Tickers europeos via yfinance (FMP no los cubre en este plan) ---
    # Si el cierre de HOY no esta disponible/fresco en yfinance, se
    # escala el ULTIMO PRECIO REAL CONOCIDO de este mismo ticker con el
    # retorno relativo del proxy de mercado US (ver PROXY_TICKERS y
    # "CORRECCION 2026-09-26 v2" en la cabecera -- NUNCA se sustituye por
    # el precio absoluto del proxy, porque no comparten NAV por
    # participacion).
    for nombre, simbolo_yf in EU_TICKERS.items():
        df = None
        price_hoy_yf = None
        currency_yf = None
        try:
            df = historical_close_series_yf(simbolo_yf, desde)
            price_hoy_yf, currency_yf = quote_yf(simbolo_yf)
        except Exception as e:
            print(f"  {nombre} ({simbolo_yf}): FALLO Yahoo -> {type(e).__name__}: {e}")

        ultima_fecha_yf = df.iloc[-1]["date"] if df is not None and len(df) else None
        fresco = (
            df is not None and price_hoy_yf is not None and ultima_fecha_yf is not None
            and fecha_cierre_us is not None
            and ultima_fecha_yf.strftime("%Y-%m-%d") == fecha_cierre_us
        )

        if fresco:
            chg = chg_periodos(df, price_hoy_yf, hasta, year_start)
            resultado[nombre] = {
                "price": price_hoy_yf,
                "currency": currency_yf,
                "chg": chg,
                "high52": high_52w(df),
                "fecha": fecha_cierre_us,
            }
            print(f"  {nombre} ({simbolo_yf}): ok  {price_hoy_yf:,.2f} {currency_yf}  "
                  f"1D={chg['1D']}%  (cierre {fecha_cierre_us})")
            continue

        # No fresco: o yfinance fallo del todo, o su cierre mas reciente
        # no es el de fecha_cierre_us (dato de hoy aun no consolidado).
        print(f"  {nombre} ({simbolo_yf}): dato de hoy no disponible/fresco en yfinance -- probando proxy FMP")

        if df is not None and len(df):
            precio_base = float(df.iloc[-1]["close"])
            fecha_base = df.iloc[-1]["date"]
        else:
            precio_base, fecha_base = ultimo_conocido_en_disco(nombre)

        proxy_t = PROXY_TICKERS.get(nombre)
        df_proxy = series_us.get(proxy_t)
        proxy_info = resultado.get(proxy_t)
        if precio_base is None or fecha_base is None or not proxy_t or df_proxy is None or proxy_info is None or fx_df is None:
            fallos.append(nombre)
            print(f"  {nombre}: proxy no disponible (falta precio base real o datos del proxy US) -- se omite")
            continue

        df_proxy_eur = serie_proxy_en_eur(df_proxy, fx_df)
        proxy_en_base = valor_en_o_antes(df_proxy_eur, fecha_base)
        proxy_hoy = valor_en_o_antes(df_proxy_eur, hasta)
        if proxy_en_base is None or proxy_en_base == 0 or proxy_hoy is None:
            fallos.append(nombre)
            print(f"  {nombre}: proxy no disponible (serie del proxy no cubre las fechas necesarias) -- se omite")
            continue

        retorno = proxy_hoy / proxy_en_base - 1
        price_eur = precio_base * (1 + retorno)

        # Serie propia del ticker (cierres reales conocidos) + el punto
        # de hoy estimado por el retorno del proxy -- asi chg%/high52
        # salen en la escala REAL de este ticker, nunca en la del proxy.
        fila_hoy = pd.DataFrame({"date": [hasta], "close": [price_eur]})
        base_df = df if (df is not None and len(df)) else pd.DataFrame({"date": [fecha_base], "close": [precio_base]})
        df_extendido = pd.concat([base_df[base_df["date"] < hasta], fila_hoy], ignore_index=True)

        chg = chg_periodos(df_extendido, price_eur, hasta, year_start)

        resultado[nombre] = {
            "price": price_eur,
            "currency": "EUR",
            "chg": chg,
            "high52": high_52w(df_extendido),
            "fecha": hasta_str,
            "proxy": True,
            "proxy_de": proxy_t,
            "proxy_precio_base": precio_base,
            "proxy_fecha_base": fecha_base.strftime("%Y-%m-%d") if hasattr(fecha_base, "strftime") else str(fecha_base),
            "proxy_retorno_aplicado_pct": round(retorno * 100, 2),
        }
        proxies_usados.append(nombre)
        print(f"  {nombre}: usando proxy {proxy_t} (retorno {retorno * 100:.2f}% desde "
              f"{fecha_base} sobre precio base {precio_base:,.2f}) -> {price_eur:,.2f} EUR  1D={chg['1D']}%")

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
        "proxies_usados": proxies_usados,
        "market": resultado,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(salida, indent=2, ensure_ascii=False))
    print(f"\nEscrito {OUT_PATH}")
    if proxies_usados:
        print(f"AVISO: se uso precio proxy para {proxies_usados} -- revisar en el correo de hoy")
