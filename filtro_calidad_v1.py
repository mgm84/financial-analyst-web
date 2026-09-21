"""
Filtro de calidad — Subcartera "calidad + barata", S&P 500
=============================================================
Validado sobre las 503 compañías del índice en sesión de 2026-09-16.

POR QUÉ EXISTEN CUATRO MÉTODOS DE CÁLCULO, NO UNO
  ROIC estándar asume que "deuda + equity - caja" es capital que la empresa
  eligió invertir. Eso rompe en tres sitios:
    - Bancos/aseguradoras: la "deuda" son depósitos/pólizas, no apalancamiento
      elegido. ROIC da resultados sin sentido (JPM: -53,9% con ROIC,
      +17,6% con ROE — el segundo es el real).
    - Utilities: apalancamiento estructural regulado. ROIC no discrimina
      entre las 31 compañías (rango de solo 5,1 puntos). ROE sí (33,8 puntos).
    - REITs: la depreciación GAAP sobre inmuebles deprime el beneficio neto
      de forma que no refleja la caja real del negocio. FFO (beneficio neto
      + D&A) es el estándar del sector, no un capricho.

  Cualquier empresa, en cualquier sector, con patrimonio neto o capital
  invertido negativo/casi nulo (por recompras agresivas) rompe la fórmula
  que le corresponda por el mismo motivo: denominador artificialmente
  pequeño dispara el ratio sin que sea señal de buen negocio. Se marca,
  no se fuerza el número.

HISTORIAL DE BUGS ENCONTRADOS Y CORREGIDOS EN ESTA SESIÓN (para no repetirlos)
  1. Función "stale" en memoria de Colab: reejecutar una celda con una función
     redefinida no sustituye la versión vieja si el bucle que la llama no se
     vuelve a ejecutar también, y un notebook no reiniciado puede seguir
     usando la función antigua sin avisar. Reiniciar entorno de ejecución
     ante cualquier duda de "por qué no cambió el resultado".
  2. FMP devuelve los trimestres del más reciente al más antiguo — sin
     sort_values("date") ascendente antes de cualquier .rolling()/.tail(),
     se cogen los trimestres equivocados.
  3. income-statement, cash-flow-statement y balance-sheet-statement pueden
     tener distinto número de trimestres entre sí para el mismo ticker
     (confirmado en RDDT, COIN, ABNB, DDOG). Sin .set_index("date") en las
     tres tablas antes de operar entre ellas, pandas alinea por posición y
     desplaza silenciosamente los datos más recientes.
"""

import time, requests
import pandas as pd
import numpy as np
import os

# API_KEY: Colab primero (userdata), con fallback a variable de entorno —
# así el mismo script corre sin cambios en Colab (desarrollo/pruebas) y en
# GitHub Actions (producción), donde no existe google.colab.
try:
    from google.colab import userdata, drive
    API_KEY = userdata.get('FMP_API_KEY')
except ImportError:
    API_KEY = os.environ.get('FMP_API_KEY')
    drive = None  # no aplica fuera de Colab; nada en este script debería llamarlo directamente
BASE = "https://financialmodelingprep.com/stable"
EP = {
    "income":   f"{BASE}/income-statement",
    "cashflow": f"{BASE}/cash-flow-statement",
    "balance":  f"{BASE}/balance-sheet-statement",
    "precios":  f"{BASE}/historical-price-eod/full",
}
PAUSA = 0.4
TRIMESTRES_ROIC = 40       # ~10 años; confirmado que Starter cubre esta profundidad en fundamentales
TASA_IMPUESTO = 0.25       # tasa fija, no efectiva por empresa — evita que un trimestre fiscal
                            # atípico (crédito fiscal, repatriación) distorsione el NOPAT

# -----------------------------------------------------------------
# ENRUTADO POR SUB-INDUSTRIA GICS — confirmado con datos reales, no teoría
# -----------------------------------------------------------------

SUBINDUSTRIAS_ROE = {
    # Financials — bancos, aseguradoras, brokers, gestoras (18/76 tenían capital
    # invertido negativo con ROIC; con ROE, 0/18 sospechosos)
    "Diversified Banks", "Regional Banks", "Asset Management & Custody Banks",
    "Property & Casualty Insurance", "Life & Health Insurance", "Multi-line Insurance",
    "Reinsurance", "Investment Banking & Brokerage", "Insurance Brokers", "Consumer Finance",
    # Utilities — ROE discrimina 33,8 puntos de rango vs 5,1 de ROIC
    "Electric Utilities", "Multi-Utilities", "Gas Utilities", "Water Utilities",
    "Independent Power Producers & Energy Traders",
}

# Dentro de Financials, estas NO tienen problema estructural de balance —
# negocio de comisión, no de depósitos/pólizas. Se quedan con ROIC estándar.
# (Financial Exchanges & Data, Transaction & Payment Processing Services:
#  no aparecieron ni una vez en los 18 casos de capital negativo)

REIT_ESTANDAR = {
    # FFO = beneficio neto + D&A, sobre patrimonio neto promedio
    "Multi-Family Residential REITs", "Retail REITs", "Health Care REITs",
    "Office REITs", "Data Center REITs", "Self-Storage REITs", "Hotel & Resort REITs",
    "Single-Family Residential REITs", "Other Specialized REITs", "Industrial REITs",
    "Timber REITs",
}
REIT_SERVICIOS = {"Real Estate Services"}      # no son REITs propietarios, ROIC estándar
REIT_TORRES = {"Telecom Tower REITs"}          # AMT/CCI/SBAC: equity/activos 5,8%-13,1%,
                                                 # estructuralmente sub-capitalizadas, no calculable

MERCHANT_POWER = {"AES", "NRG"}                # Utilities no reguladas (expuestas a precio
                                                 # mayorista) — usan ROE igual, pero se flaggean
                                                 # como contexto distinto al resto del sector
EXCLUSION_MANUAL_FFO = {"IRM", "SPG"}          # equity negativo (IRM) o comprimido por
                                                 # recompras (SPG, 7-12,8% equity/activos) —
                                                 # casos puntuales, no cubiertos por regla genérica
EXCLUSION_MANUAL_ROE = {"PGR"}                  # desajuste de fecha balance/income en el trimestre
                                                 # más reciente (balance: 2026-05-31, income:
                                                 # 2026-06-30, mismo Q2 2026 real) — no es un problema
                                                 # de negocio, es un artefacto de alineación aislado
                                                 # (0/60 en muestra de otros tickers). El equity real
                                                 # de PGR es positivo y sano en los 6 trimestres previos.

SECTORES_SIN_GROSS_MARGIN = {"Financials", "Utilities"}  # gross margin no tiene significado
                                                            # económico fiable en estos sectores
                                                            # (ver hallazgo Utilities: 14,5%-100%
                                                            # sin patrón económico creíble)


# -----------------------------------------------------------------
# CARGA DE DATOS
# -----------------------------------------------------------------

def get(url, **params):
    params["apikey"] = API_KEY
    # Diagnóstico granular: antes solo se veía "TICKER: recalculado" al
    # terminar el ticker completo — si se cuelga a mitad de proceso (como
    # pasó con GPN en producción, más de 10 min sin avanzar, muy por
    # encima de lo que timeout=30 debería permitir), no había forma de
    # saber en qué llamada concreta. Con este print, el log muestra el
    # endpoint y ticker exactos justo antes de cada request.
    simbolo = params.get("symbol", "?")
    print(f"    -> {url.split('/')[-1]} [{simbolo}]", flush=True)
    r = requests.get(url, params=params, timeout=(10, 30))
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

def cargar(endpoint, ticker):
    df = pd.DataFrame(get(EP[endpoint], symbol=ticker, period="quarter", limit=TRIMESTRES_ROIC))
    # sort_values obligatorio: FMP devuelve del más reciente al más antiguo;
    # sin esto, .tail(4)/.rolling(4) cogen los trimestres equivocados.
    return df.sort_values("date").reset_index(drop=True)


# -----------------------------------------------------------------
# LOS TRES MÉTODOS DE CÁLCULO
# -----------------------------------------------------------------

def _cargar_tres(ticker):
    # set_index("date") en las tres tablas es obligatorio: income/cashflow/balance
    # pueden tener distinto número de trimestres entre sí (confirmado en RDDT,
    # COIN, ABNB, DDOG). Sin indexar por fecha, pandas alinea por posición y
    # desplaza silenciosamente los datos del trimestre más reciente.
    inc = cargar("income", ticker).set_index("date")
    cfl = cargar("cashflow", ticker).set_index("date")
    bal = cargar("balance", ticker).set_index("date")
    return inc, cfl, bal

def calidad_roic(ticker, tasa=TASA_IMPUESTO):
    inc, cfl, bal = _cargar_tres(ticker)

    ebit_ttm = col(inc, "operatingIncome").rolling(4).sum()
    nopat_ttm = ebit_ttm * (1 - tasa)

    deuda = col(bal, "totalDebt")
    equity = col(bal, "totalStockholdersEquity", "totalEquity")
    caja = col(bal, "cashAndShortTermInvestments", "cashAndCashEquivalents")
    capital_invertido = deuda + equity - caja
    capital_promedio = capital_invertido.rolling(2).mean()

    roic = nopat_ttm / capital_promedio
    gross_margin = col(inc, "grossProfit") / col(inc, "revenue")
    fcf_ttm = col(cfl, "freeCashFlow").rolling(4).sum()
    net_income_ttm = col(inc, "netIncome").rolling(4).sum()
    conversion_fcf = fcf_ttm / net_income_ttm

    return pd.DataFrame({
        "metrica_rentabilidad": roic.round(3),
        "denominador_negativo": (capital_promedio < 0),
        "gross_margin": gross_margin.round(3),
        "conversion_fcf": conversion_fcf.round(2),
    }).reset_index()

def calidad_roe(ticker):
    inc, cfl, bal = _cargar_tres(ticker)

    net_income_ttm = col(inc, "netIncome").rolling(4).sum()
    equity = col(bal, "totalStockholdersEquity", "totalEquity")
    equity_promedio = equity.rolling(2).mean()

    roe = net_income_ttm / equity_promedio
    fcf_ttm = col(cfl, "freeCashFlow").rolling(4).sum()
    conversion_fcf = fcf_ttm / net_income_ttm

    return pd.DataFrame({
        "metrica_rentabilidad": roe.round(3),
        "denominador_negativo": (equity_promedio < 0),
        "gross_margin": np.nan,   # sin significado económico fiable en estos sectores
        "conversion_fcf": conversion_fcf.round(2),
    }).reset_index()

def calidad_ffo(ticker):
    inc, cfl, bal = _cargar_tres(ticker)

    net_income_ttm = col(inc, "netIncome").rolling(4).sum()
    da_ttm = col(cfl, "depreciationAndAmortization").rolling(4).sum()
    ffo_ttm = net_income_ttm + da_ttm

    equity = col(bal, "totalStockholdersEquity", "totalEquity")
    equity_promedio = equity.rolling(2).mean()

    ffo_sobre_equity = ffo_ttm / equity_promedio
    revenue_ttm = col(inc, "revenue").rolling(4).sum()
    ffo_margin = ffo_ttm / revenue_ttm   # proxy de "poder de fijación de precios" para REITs

    return pd.DataFrame({
        "metrica_rentabilidad": ffo_sobre_equity.round(3),
        "denominador_negativo": (equity_promedio < 0),
        "gross_margin": np.nan,
        "conversion_fcf": ffo_margin.round(3),   # reutilizamos la columna: aquí es FFO margin, no FCF/neto
    }).reset_index()


# -----------------------------------------------------------------
# ENRUTADO — decide método y flags de contexto por ticker
# -----------------------------------------------------------------

def metodo_y_flags(ticker, sector, subindustria):
    flags = []

    if subindustria in REIT_TORRES:
        return "no_calculable", ["torre_telecom_subcapitalizada"]
    if ticker in EXCLUSION_MANUAL_FFO:
        return "no_calculable", ["equity_negativo_o_comprimido_manual"]
    if ticker in EXCLUSION_MANUAL_ROE:
        return "no_calculable", ["desajuste_fecha_balance_income"]

    if subindustria in REIT_ESTANDAR:
        metodo = "ffo"
    elif subindustria in REIT_SERVICIOS:
        metodo = "roic"
    elif subindustria in SUBINDUSTRIAS_ROE:
        metodo = "roe"
    else:
        metodo = "roic"

    if ticker in MERCHANT_POWER:
        flags.append("utility_no_regulada")

    return metodo, flags


# -----------------------------------------------------------------
# BATCH COMPLETO
# -----------------------------------------------------------------

def correr_batch(sp500_df):
    resultados = []
    for i, row in sp500_df.iterrows():
        ticker, sector, subindustria = row["ticker"], row["sector"], row["subindustria"]
        metodo, flags = metodo_y_flags(ticker, sector, subindustria)

        if metodo == "no_calculable":
            resultados.append({
                "ticker": ticker, "sector": sector, "subindustria": subindustria,
                "metodo": metodo, "metrica_rentabilidad": np.nan,
                "gross_margin": np.nan, "conversion_fcf": np.nan,
                "flags": ",".join(flags),
            })
            print(f"  {ticker}: no_calculable ({flags[0]})")
            continue

        try:
            funcion = {"roic": calidad_roic, "roe": calidad_roe, "ffo": calidad_ffo}[metodo]
            df = funcion(ticker)
            ultimo = df.iloc[-1]

            if ultimo["denominador_negativo"]:
                flags.append("denominador_negativo")

            resultados.append({
                "ticker": ticker, "sector": sector, "subindustria": subindustria,
                "metodo": metodo,
                "metrica_rentabilidad": np.nan if "denominador_negativo" in flags else ultimo["metrica_rentabilidad"],
                "gross_margin": ultimo["gross_margin"],
                "conversion_fcf": ultimo["conversion_fcf"],
                "flags": ",".join(flags),
                "metrica_sin_filtrar": ultimo["metrica_rentabilidad"],  # auditoría, nunca se pierde el dato crudo
            })
            print(f"  {ticker}: ok [{metodo}]" + (f"  <- {flags}" if flags else ""))
        except Exception as e:
            resultados.append({
                "ticker": ticker, "sector": sector, "subindustria": subindustria,
                "metodo": metodo, "metrica_rentabilidad": np.nan,
                "gross_margin": np.nan, "conversion_fcf": np.nan,
                "flags": f"FALLO: {type(e).__name__}",
            })
            print(f"  {ticker}: FALLO -> {type(e).__name__}: {e}")

    return pd.DataFrame(resultados)


# -----------------------------------------------------------------
# DIMENSIONES ADICIONALES — sostenido 5-8 años, deuda/EBITDA, resiliencia 2020/2022
# -----------------------------------------------------------------
# Validado sobre 4 tickers de control (AAPL/roic, JPM/roe, EQIX/ffo, NEE/roe)
# en sesión de 2026-09-16, todos con resultado económicamente coherente.

TRIMESTRES_SOSTENIDO = 32   # ~8 años, tu propio criterio ("mirar 5-8 años")
UMBRAL_RENTABILIDAD_SOSTENIDO = 0.15

SECTORES_SIN_DEUDA_EBITDA = {"Financials"}  # ratios de capital regulatorio, no deuda/EBITDA;
                                              # sin equivalente fiable en datos disponibles
GRUPOS_PERCENTIL_DEUDA = {
    # sub-industrias ya definidas arriba: apalancamiento estructural del sector,
    # no elección de gestión — percentil a nivel de sector completo (las sub-industrias
    # individuales son demasiado pequeñas para un percentil fiable), corte en P75
    "Utilities": None,       # se calcula dinámicamente sobre todo el sector Utilities
    "Real Estate": REIT_ESTANDAR,  # solo REITs propietarios, no Real Estate Services/Torres
}
UMBRAL_DEUDA_EBITDA_ESTANDAR = 2.0   # tu criterio original, para el resto de sectores
PERCENTIL_DEUDA_SECTOR = 0.75        # top cuartil de apalancamiento del propio sector = excluido

# Utilities: "calidad" absoluta (ROE >15% sostenido) no tiene sentido para retorno
# regulado — todo el sector opera estructuralmente por debajo de ese umbral (mediana
# real del sector: 5,7%-12,6%, confirmado con datos, no solo teoría). Igual que con
# deuda/EBITDA, se evalúa relativo a los propios pares del sector: top tercio en
# rentabilidad Y top tercio en consistencia, no un umbral absoluto.
SECTORES_CORTE_RENTABILIDAD_PERCENTIL = {"Utilities"}
PERCENTIL_RENTABILIDAD_SECTOR = 0.667   # top tercio del propio sector

# Umbrales del corte final de calidad — promovidos a constantes de módulo
# (antes solo existían como valores por defecto de aplicar_cortes_calidad_final,
# lo que impedía importarlos desde otros scripts del pipeline, ej. calidad_universo.py)
UMBRAL_PCT_SOSTENIDO = 0.70       # tu propio criterio: "sostenido", no solo la mediana
UMBRAL_VARIACION_ESTRES = -0.30   # tolerancia máxima de caída en 2020/2022

# Sub-industrias GICS ya cubiertas por la cartera AI Infrastructure Stack —
# usado para marcar "solapa_cartera_ia" en cada candidato de esta subcartera
# nueva, de cara al tope del 10% de concentración acordado. Confirmado a
# mano en sesión: EQIX/ASML/AVGO/TSM/PWR/ETN/ANET (cartera IA) + GOOGL/AMD/
# MSFT (cartera preexistente DEGIRO). ASML y TSM no están en el S&P 500
# (extranjeras) y no tienen fila propia en sp500_universo.csv, pero sus
# sub-industrias sí están representadas aquí porque otros candidatos del
# S&P 500 pueden pertenecer a la misma categoría.
SUBINDUSTRIAS_CARTERA_IA = {
    "Communications Equipment", "Construction & Engineering", "Data Center REITs",
    "Electrical Components & Equipment", "Interactive Media & Services",
    "Semiconductor Materials & Equipment", "Semiconductors", "Systems Software",
}

# Trayectoria de negocio — detecta "deterioro doble" (crecimiento de ingresos
# Y margen bruto cayendo a la vez, últimos 3 años frente a los 3 anteriores):
# señal de posible trampa de valor cuando un candidato aparece barato en el
# ranking de precio, ver claude/subcartera-calidad-barata.md. Es información
# para el correo, NO un filtro que excluye candidatos del universo.
UMBRAL_DETERIORO_MARGEN_PP = 1.0   # caída de margen >1 punto porcentual para contar como deterioro

def trayectoria_negocio(ticker):
    inc = cargar("income", ticker).set_index("date")
    revenue = col(inc, "revenue")
    gross_margin = col(inc, "grossProfit") / col(inc, "revenue")

    revenue_ttm = revenue.rolling(4).sum()
    crecimiento_yoy = revenue_ttm.pct_change(4) * 100

    reciente = crecimiento_yoy.tail(12)      # últimos 3 años en trimestres
    anterior = crecimiento_yoy.tail(24).head(12)  # los 3 años antes de eso

    margen_reciente = gross_margin.tail(12).mean()
    margen_anterior = gross_margin.tail(24).head(12).mean()

    return {
        "crecimiento_reciente_3a": round(reciente.mean(), 1) if reciente.notna().any() else None,
        "crecimiento_anterior_3a": round(anterior.mean(), 1) if anterior.notna().any() else None,
        "margen_reciente": round(margen_reciente * 100, 1) if pd.notna(margen_reciente) else None,
        "margen_anterior": round(margen_anterior * 100, 1) if pd.notna(margen_anterior) else None,
    }


def calcular_deterioro_doble(trayectoria):
    """True solo si crecimiento Y margen caen a la vez — un solo síntoma
    (solo crecimiento desacelera, o solo margen cae) no cuenta, tal como
    se decidió en sesión al revisar CPRT/HSY frente al resto de candidatos."""
    cr, ca = trayectoria.get("crecimiento_reciente_3a"), trayectoria.get("crecimiento_anterior_3a")
    mr, ma = trayectoria.get("margen_reciente"), trayectoria.get("margen_anterior")
    if cr is None or ca is None or mr is None or ma is None:
        return None  # dato insuficiente, no se afirma nada
    deterioro_crecimiento = cr < ca
    deterioro_margen = mr < (ma - UMBRAL_DETERIORO_MARGEN_PP)
    return bool(deterioro_crecimiento and deterioro_margen)


def serie_completa(ticker, metodo):
    if metodo == "roic":
        return calidad_roic(ticker)
    elif metodo == "roe":
        return calidad_roe(ticker)
    elif metodo == "ffo":
        return calidad_ffo(ticker)
    return None

def deuda_neta_ebitda_serie(ticker):
    inc = cargar("income", ticker).set_index("date")
    cfl = cargar("cashflow", ticker).set_index("date")
    bal = cargar("balance", ticker).set_index("date")

    ebitda_ttm = (col(inc, "operatingIncome") + col(cfl, "depreciationAndAmortization")).rolling(4).sum()
    deuda = col(bal, "totalDebt")
    caja = col(bal, "cashAndShortTermInvestments", "cashAndCashEquivalents")
    deuda_neta = deuda - caja
    return (deuda_neta / ebitda_ttm).round(2)

def variacion_estres(df_fechas, anio_estres, anio_ref):
    # positivo = sin caída real (o mejora); negativo = caída relativa al propio historial.
    # Nombrado "variacion_", no "caida_", porque el signo no siempre es negativo — evita
    # que una lectura rápida del CSV interprete mal un valor positivo como error de cálculo.
    #
    # Guarda doble, en AMBOS años: si la mediana de cualquiera de los dos años está
    # muy cerca de cero (empresa con rentabilidad marginal, o con recompras que
    # comprimen el denominador dentro del propio año de estrés — visto en AON 2022,
    # donde dos trimestres normales y dos con equity casi nulo producen un mínimo de
    # -56 sin que el año en sí sea débil), se descarta el cálculo en vez de reportar
    # un número engañoso. Se usa mediana en ambos lados, no mínimo del año de estrés,
    # para no dejar que un solo trimestre atípico domine el resultado.
    estres = df_fechas.loc[df_fechas.index.astype(str).str.startswith(str(anio_estres)), "metrica_rentabilidad"]
    ref = df_fechas.loc[df_fechas.index.astype(str).str.startswith(str(anio_ref)), "metrica_rentabilidad"]
    if estres.empty or ref.empty:
        return np.nan
    ref_mediana = ref.median()
    estres_mediana = estres.median()
    if pd.isna(ref_mediana) or pd.isna(estres_mediana):
        return np.nan
    if abs(ref_mediana) < 0.02:      # <2 puntos porcentuales de rentabilidad de referencia:
        return np.nan                # el ratio no es fiable, denominador casi nulo
    return round((estres_mediana / ref_mediana) - 1, 2)

def dimensiones_extendidas(ticker, metodo, sector):
    df = serie_completa(ticker, metodo)
    if df is None:
        return {}

    serie_8a = df["metrica_rentabilidad"].tail(TRIMESTRES_SOSTENIDO)
    mediana_8a = serie_8a.median()
    pct_sostenido = (serie_8a > UMBRAL_RENTABILIDAD_SOSTENIDO).mean()

    de_actual = np.nan
    if sector not in SECTORES_SIN_DEUDA_EBITDA:
        try:
            de_actual = deuda_neta_ebitda_serie(ticker).iloc[-1]
        except Exception:
            pass

    df_fechas = df.set_index("date")
    variacion_2020 = variacion_estres(df_fechas, 2020, 2019)
    variacion_2022 = variacion_estres(df_fechas, 2022, 2021)

    return {
        "mediana_rentabilidad_8a": round(mediana_8a, 3) if pd.notna(mediana_8a) else np.nan,
        "pct_trimestres_sostenido": round(pct_sostenido, 2) if pd.notna(pct_sostenido) else np.nan,
        "deuda_neta_ebitda": round(de_actual, 2) if pd.notna(de_actual) else np.nan,
        "variacion_2020": variacion_2020,
        "variacion_2022": variacion_2022,
    }

def correr_batch_extendido(df_calidad_base, sp500_df):
    """Solo recoge datos de FMP — la parte cara (~25-30 min). No aplica ningún
    corte ni percentil, así el resultado se puede guardar y reprocesar después
    sin volver a llamar a la API si algo falla en el post-procesamiento."""
    resultados = []

    for i, row in df_calidad_base.iterrows():
        ticker, metodo, sector = row["ticker"], row["metodo"], row["sector"]
        if metodo == "no_calculable":
            resultados.append({"ticker": ticker, **{k: np.nan for k in
                ["mediana_rentabilidad_8a", "pct_trimestres_sostenido",
                 "deuda_neta_ebitda", "variacion_2020", "variacion_2022"]}})
            continue
        try:
            dims = dimensiones_extendidas(ticker, metodo, sector)
            resultados.append({"ticker": ticker, **dims})
            print(f"  {ticker}: ok")
        except Exception as e:
            resultados.append({"ticker": ticker, **{k: np.nan for k in
                ["mediana_rentabilidad_8a", "pct_trimestres_sostenido",
                 "deuda_neta_ebitda", "variacion_2020", "variacion_2022"]}})
            print(f"  {ticker}: FALLO -> {type(e).__name__}: {e}")

    df_ext = pd.DataFrame(resultados)
    return df_calidad_base.merge(df_ext, on="ticker", how="left")


def aplicar_cortes_deuda(df_completo):
    """Aplica el corte de deuda/EBITDA sobre datos YA recogidos — no llama a FMP,
    se puede reejecutar tantas veces como haga falta sin coste de tiempo."""
    df_completo = df_completo.copy()
    df_completo["pasa_deuda"] = True

    sectores_percentil = set(GRUPOS_PERCENTIL_DEUDA)
    sectores_excluidos = sectores_percentil | SECTORES_SIN_DEUDA_EBITDA  # ahora set | set, válido

    mask_estandar = ~df_completo["sector"].isin(sectores_excluidos)
    df_completo.loc[mask_estandar, "pasa_deuda"] = (
        df_completo.loc[mask_estandar, "deuda_neta_ebitda"] < UMBRAL_DEUDA_EBITDA_ESTANDAR
    )
    for sector_pct in sectores_percentil:
        mask = df_completo["sector"] == sector_pct
        if mask.sum() == 0:
            continue
        p75 = df_completo.loc[mask, "deuda_neta_ebitda"].quantile(PERCENTIL_DEUDA_SECTOR)
        df_completo.loc[mask, "pasa_deuda"] = df_completo.loc[mask, "deuda_neta_ebitda"] <= p75

    return df_completo


def aplicar_cortes_calidad_final(df_completo,
                                   umbral_rentabilidad=UMBRAL_RENTABILIDAD_SOSTENIDO,
                                   umbral_pct_sostenido=UMBRAL_PCT_SOSTENIDO,
                                   umbral_variacion_estres=UMBRAL_VARIACION_ESTRES):
    """Aplica el filtro de calidad completo sobre datos ya recogidos y con
    aplicar_cortes_deuda ya corrido. Sin llamadas a FMP — se puede reejecutar
    tantas veces como haga falta para probar umbrales distintos."""
    df = df_completo.copy()
    calc = df[df["metodo"] != "no_calculable"].copy()

    calc["pasa_rentabilidad"] = False
    calc["pasa_consistencia"] = False

    estandar = ~calc["sector"].isin(SECTORES_CORTE_RENTABILIDAD_PERCENTIL)
    calc.loc[estandar, "pasa_rentabilidad"] = calc.loc[estandar, "mediana_rentabilidad_8a"] > umbral_rentabilidad
    calc.loc[estandar, "pasa_consistencia"] = calc.loc[estandar, "pct_trimestres_sostenido"] >= umbral_pct_sostenido

    for sector_pct in SECTORES_CORTE_RENTABILIDAD_PERCENTIL:
        mask = calc["sector"] == sector_pct
        if mask.sum() == 0:
            continue
        corte_rent = calc.loc[mask, "mediana_rentabilidad_8a"].quantile(PERCENTIL_RENTABILIDAD_SECTOR)
        corte_cons = calc.loc[mask, "pct_trimestres_sostenido"].quantile(PERCENTIL_RENTABILIDAD_SECTOR)
        calc.loc[mask, "pasa_rentabilidad"] = calc.loc[mask, "mediana_rentabilidad_8a"] >= corte_rent
        calc.loc[mask, "pasa_consistencia"] = calc.loc[mask, "pct_trimestres_sostenido"] >= corte_cons

    pasa_deuda = calc["pasa_deuda"].fillna(True)
    pasa_resiliencia = (
        (calc["variacion_2020"].isna() | (calc["variacion_2020"] > umbral_variacion_estres)) &
        (calc["variacion_2022"].isna() | (calc["variacion_2022"] > umbral_variacion_estres))
    )

    calc["supera_filtro_calidad"] = (
        calc["pasa_rentabilidad"] & calc["pasa_consistencia"] & pasa_deuda & pasa_resiliencia
    )
    return calc


# -----------------------------------------------------------------
# FILTRO DE PRECIO — métrica de valoración según el método de calidad asignado
# -----------------------------------------------------------------
# roic -> P/FCF ; roe+Financials -> P/TBV ; roe+Utilities -> P/E ; ffo -> P/FFO
# Validado sobre 78 supervivientes del filtro de calidad en sesión 2026-09-16.
# Universo S&P 500 confirmado 100% en USD — sin necesidad de conversión de
# divisa (a diferencia de TSM/ASML en la cartera IA).

def precio_actual(ticker):
    q = get(f"{BASE}/quote", symbol=ticker)
    return float(q[0]["price"])

def valoracion_roic_serie(ticker):
    """Denominador de P/FCF: FCF por acción TTM"""
    inc = cargar("income", ticker).set_index("date")
    cfl = cargar("cashflow", ticker).set_index("date")
    acciones = col(inc, "weightedAverageShsOutDil", "weightedAverageShsOut")
    fcf_ttm = col(cfl, "freeCashFlow").rolling(4).sum()
    return fcf_ttm / acciones

def valoracion_ffo_serie(ticker):
    """Denominador de P/FFO: FFO por acción TTM"""
    inc = cargar("income", ticker).set_index("date")
    cfl = cargar("cashflow", ticker).set_index("date")
    acciones = col(inc, "weightedAverageShsOutDil", "weightedAverageShsOut")
    net_income_ttm = col(inc, "netIncome").rolling(4).sum()
    da_ttm = col(cfl, "depreciationAndAmortization").rolling(4).sum()
    ffo_ttm = net_income_ttm + da_ttm
    return ffo_ttm / acciones

def valoracion_pe_serie(ticker):
    """Denominador de P/E: EPS TTM (Utilities dentro del grupo roe)"""
    inc = cargar("income", ticker).set_index("date")
    acciones = col(inc, "weightedAverageShsOutDil", "weightedAverageShsOut")
    net_income_ttm = col(inc, "netIncome").rolling(4).sum()
    return net_income_ttm / acciones

def goodwill_intangibles_fiable(bal):
    """Un valor solo cuenta como fiable si es positivo y no-cero — goodwill+intangibles
    real nunca es exactamente 0 ni negativo para una empresa con historial de M&A.
    Confirmado en producción: AMP y ERIE reportan 0 o negativo de forma dispersa en
    toda la serie (no solo al principio, un simple corte de tramo no basta) — hay
    que exigir dato válido en cada trimestre individualmente."""
    gi = col(bal, "goodwillAndIntangibleAssets")
    valido = gi.notna() & (gi > 0)
    return gi.where(valido, np.nan)

MIN_TRIMESTRES_TBV = 8   # ~2 años; por debajo de esto, el percentil no es
                          # estadísticamente robusto (el dato de goodwill ya
                          # sabemos que es más ruidoso que el resto)
EXCLUSION_MANUAL_TBV = {"MRSH"}   # TBV negativo en 40/40 trimestres (goodwill
                                   # acumulado por M&A supera el patrimonio neto
                                   # de forma estructural) — no es artefacto de datos

def valoracion_tbv_serie(ticker):
    """Denominador de P/TBV: TBV por acción (Financials dentro del grupo roe)"""
    if ticker in EXCLUSION_MANUAL_TBV:
        bal = cargar("balance", ticker).set_index("date")
        return pd.Series(np.nan, index=bal.index)

    bal = cargar("balance", ticker).set_index("date")
    inc = cargar("income", ticker).set_index("date")
    acciones = col(inc, "weightedAverageShsOutDil", "weightedAverageShsOut")
    equity = col(bal, "totalStockholdersEquity", "totalEquity")
    gi = goodwill_intangibles_fiable(bal)
    tbv = equity - gi
    tbv_por_accion = (tbv / acciones).where(gi.notna(), np.nan)

    n_validos = tbv_por_accion.notna().sum()
    actual_valido = pd.notna(tbv_por_accion.iloc[-1])
    if n_validos < MIN_TRIMESTRES_TBV or not actual_valido:
        return pd.Series(np.nan, index=tbv_por_accion.index)  # no calculable
    return tbv_por_accion

def metrica_valoracion_por_ticker(ticker, metodo, sector):
    """Enruta a la serie de denominador correcta según método de calidad + sector."""
    if metodo == "roic":
        return "P/FCF", valoracion_roic_serie(ticker)
    elif metodo == "ffo":
        return "P/FFO", valoracion_ffo_serie(ticker)
    elif metodo == "roe" and sector == "Financials":
        return "P/TBV", valoracion_tbv_serie(ticker)
    elif metodo == "roe" and sector == "Utilities":
        return "P/E", valoracion_pe_serie(ticker)
    return None, None


if __name__ == "__main__":
    drive.mount('/content/drive')  # necesario en cada notebook/runtime nuevo, aunque sea la misma cuenta
    sp500 = pd.read_csv('/content/drive/MyDrive/financial-analyst/sp500_universo.csv')
    df_calidad = correr_batch(sp500)

    print(f"\nTotal: {len(df_calidad)}")
    print(f"No calculables: {(df_calidad['metodo'] == 'no_calculable').sum()}")
    print(f"Con flags de revisión: {(df_calidad['flags'] != '').sum()}")
    print("\nPor método:")
    print(df_calidad["metodo"].value_counts())

    df_calidad.to_csv('/content/drive/MyDrive/financial-analyst/calidad_v3_completo.csv', index=False)
    print("\nGuardado: calidad_v3_completo.csv")

    print("\n" + "=" * 60)
    print("DIMENSIONES EXTENDIDAS (sostenido 8a, deuda/EBITDA, resiliencia)")
    print("=" * 60)
    df_recogido = correr_batch_extendido(df_calidad, sp500)
    # Guardado ANTES de aplicar cortes: si algo falla en aplicar_cortes_deuda,
    # los ~25-30 min de llamadas a FMP ya están a salvo en disco.
    df_recogido.to_csv('/content/drive/MyDrive/financial-analyst/calidad_v4_raw.csv', index=False)
    print("\nGuardado (datos crudos, antes de cortes): calidad_v4_raw.csv")

    df_final = aplicar_cortes_deuda(df_recogido)
    df_final.to_csv('/content/drive/MyDrive/financial-analyst/calidad_v4_completo.csv', index=False)
    print("Guardado (con corte de deuda aplicado): calidad_v4_completo.csv")
