"""
Calidad Universo — recálculo por evento
==========================================
Primer script del pipeline de monitorización "Calidad + Barata".
Corre en GitHub Actions, ANTES de percentiles_diarios.py.

QUÉ HACE
  1. Chequeo ligero (1 llamada por ticker) para detectar si hay un
     trimestre fiscal nuevo desde la última vez que se calculó la calidad
     de esa compañía.
  2. Solo para los tickers con trimestre nuevo: recalcula las 4
     dimensiones de calidad completas (rentabilidad sostenida, consistencia,
     deuda/EBITDA, resiliencia 2020/2022) usando la lógica ya validada en
     filtro_calidad_v1.py.
  3. Para TODOS los tickers que ya superan el filtro (nuevos o no),
     recalcula/actualiza la serie histórica del denominador de valoración
     (FCF/FFO/TBV/EPS por acción, según método) — es lo que necesita
     percentiles_diarios.py para no tener que volver a llamar a los
     endpoints de estados financieros cada día.

POR QUÉ NO SE RECALCULA TODO A DIARIO
  Los datos fundamentales solo cambian con cada earnings report trimestral.
  Recalcular las 503 compañías cada día repetiría ~25-30 min de llamadas a
  FMP sin que el resultado cambie en la inmensa mayoría de los casos. Este
  chequeo evento-por-evento cuesta 503 llamadas ligeras (rápido) en un día
  normal, y dispara el recálculo pesado solo para los pocos tickers que
  publicaron resultados esa semana.

SALIDA
  calidad_universo.json — universo completo (503), con:
    - metodo, sector, subindustria, supera_filtro_calidad (bool)
    - ultima_fecha_reporte (para el chequeo de evento)
    - metrica_valoracion (P/FCF, P/FFO, P/TBV, P/E)
    - serie_valoracion: [{fecha, denominador_por_accion}, ...] — histórico
      completo del denominador, para que percentiles_diarios.py solo
      necesite el precio de hoy para calcular el ratio y el percentil.
    - exclusiones_valoracion (motivo si no es calculable, ej. AMP/ERIE/MRSH)
"""

import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# --- Reutiliza la configuración y funciones ya validadas en filtro_calidad_v1.py ---
# En producción: from filtro_calidad_v1 import (
#     get, col, cargar, calidad_roic, calidad_roe, calidad_ffo,
#     metodo_y_flags, API_KEY, BASE, EP, ...
# )
# Aquí se listan solo las piezas nuevas de este script; se asume que
# filtro_calidad_v1.py vive en el mismo repo y se importa tal cual.

from filtro_calidad_v1 import (  # noqa: E402
    get, col, cargar, calidad_roic, calidad_roe, calidad_ffo,
    metodo_y_flags, BASE, EP,
    metrica_valoracion_por_ticker, dimensiones_extendidas,
    UMBRAL_RENTABILIDAD_SOSTENIDO, UMBRAL_PCT_SOSTENIDO, UMBRAL_VARIACION_ESTRES,
    SECTORES_CORTE_RENTABILIDAD_PERCENTIL, PERCENTIL_RENTABILIDAD_SECTOR,
    SECTORES_SIN_DEUDA_EBITDA, GRUPOS_PERCENTIL_DEUDA,
    UMBRAL_DEUDA_EBITDA_ESTANDAR, PERCENTIL_DEUDA_SECTOR,
)
# Todos los imports de filtro_calidad_v1 viven aquí arriba, no dentro de las
# funciones — si falta una constante o función, el script falla en el primer
# segundo (ImportError inmediato), no después de recorrer los 503 tickers.
# Esto costó una hora de cómputo descartada la primera vez que se corrió.

RUTA_UNIVERSO = "data/calidad_universo.json"
RUTA_SP500 = "data/sp500_universo.csv"  # mismo CSV ya usado en el resto del pipeline

TRIMESTRES_SERIE_VALORACION = 40  # ~10 años, misma ventana que el resto del filtro


def cargar_universo_previo():
    """Lee el JSON existente, o devuelve estructura vacía si es la primera ejecución."""
    try:
        with open(RUTA_UNIVERSO) as f:
            data = json.load(f)
        return {c["ticker"]: c for c in data.get("universo", [])}
    except FileNotFoundError:
        return {}


def trimestre_mas_reciente(ticker):
    """1 llamada ligera: solo el último trimestre reportado, no toda la serie."""
    datos = get(f"{BASE}/income-statement", symbol=ticker, period="quarter", limit=1)
    if not datos:
        return None
    return datos[0]["date"]


def serie_valoracion(ticker, metodo, sector):
    """Serie histórica del denominador por acción, según el método de calidad
    ya asignado a este ticker. Reutiliza las funciones de valoración del
    script consolidado (ver metrica_valoracion_por_ticker en filtro_calidad_v1.py)."""
    etiqueta, serie = metrica_valoracion_por_ticker(ticker, metodo, sector)
    if serie is None:
        return None, None
    serie = serie.dropna()
    if len(serie) == 0:
        return etiqueta, None
    return etiqueta, [
        {"fecha": str(fecha), "denominador": round(float(valor), 4)}
        for fecha, valor in serie.items()
    ]


def actualizar_ticker(ticker, sector, subindustria, previo):
    """Devuelve la entrada actualizada para un ticker: solo recalcula si
    hay trimestre nuevo, si no existía antes, o si nunca se pudo calcular
    su serie de valoración (para reintentar sin esperar a un evento)."""
    try:
        fecha_actual = trimestre_mas_reciente(ticker)
    except Exception as e:
        # Fallo de red/API puntual: se conserva la entrada previa si existe,
        # no se marca como "sin trimestre nuevo" a ciegas.
        if previo:
            return previo, False
        return {
            "ticker": ticker, "sector": sector, "subindustria": subindustria,
            "metodo": "error", "supera_filtro_calidad": False,
            "error": f"{type(e).__name__}: {e}",
        }, False

    hay_trimestre_nuevo = (
        previo is None
        or previo.get("ultima_fecha_reporte") != fecha_actual
        or previo.get("metodo") == "error"
    )

    if not hay_trimestre_nuevo:
        return previo, False

    metodo, flags = metodo_y_flags(ticker, sector, subindustria)

    if metodo == "no_calculable":
        entrada = {
            "ticker": ticker, "sector": sector, "subindustria": subindustria,
            "metodo": metodo, "supera_filtro_calidad": False,
            "ultima_fecha_reporte": fecha_actual, "flags": flags,
        }
        return entrada, True

    try:
        funcion = {"roic": calidad_roic, "roe": calidad_roe, "ffo": calidad_ffo}[metodo]
        df = funcion(ticker)
        # Las 4 dimensiones completas (sostenido, deuda, resiliencia) usan
        # la misma lógica ya validada — ver aplicar_cortes_calidad_final en
        # filtro_calidad_v1.py para el detalle exacto de cada umbral.
        dims = dimensiones_extendidas(ticker, metodo, sector)
        etiqueta_val, serie_val = serie_valoracion(ticker, metodo, sector)

        entrada = {
            "ticker": ticker, "sector": sector, "subindustria": subindustria,
            "metodo": metodo, "flags": flags,
            "ultima_fecha_reporte": fecha_actual,
            **dims,
            "metrica_valoracion": etiqueta_val,
            "serie_valoracion": serie_val,
        }
    except Exception as e:
        # Este try/except cubre SOLO el cálculo caro (llamadas a FMP). Si
        # falla aquí, no hay nada que perder — no se había calculado nada
        # todavía.
        entrada = {
            "ticker": ticker, "sector": sector, "subindustria": subindustria,
            "metodo": "error", "supera_filtro_calidad": False,
            "ultima_fecha_reporte": fecha_actual,
            "error": f"{type(e).__name__}: {e}",
        }
        return entrada, True

    # supera_filtro_calidad NO se calcula aquí, ticker por ticker: dos de
    # las cuatro dimensiones (rentabilidad y deuda/EBITDA de Utilities,
    # deuda/EBITDA también de REITs estándar) usan un percentil relativo a
    # TODOS los pares del propio sector, no un umbral fijo — necesitan la
    # distribución completa. Se decide en un único paso final, después de
    # procesar todos los tickers (ver recalcular_todos_los_cortes()).
    return entrada, True


def recalcular_todos_los_cortes(universo):
    """Único paso final que decide supera_filtro_calidad para TODO el
    universo a la vez — no se puede hacer ticker por ticker de forma
    aislada, porque dos de las cuatro dimensiones dependen de la
    distribución completa del propio sector (percentil, no umbral fijo):
      - Rentabilidad + consistencia: percentil intra-sector solo en Utilities.
      - Deuda/EBITDA: percentil intra-sector en Utilities y en REITs
        estándar (GRUPOS_PERCENTIL_DEUDA); umbral fijo <2.0x en el resto;
        sin corte (siempre pasa) en Financials.
    Sustituye a dos versiones anteriores más frágiles: _pasa_filtro_individual
    (nunca aplicaba el corte de deuda porque 'pasa_deuda' no llegaba a
    existir en la entrada — bug real, detectado al comparar 98 supervivientes
    aquí contra los 78 validados a mano en el notebook) y
    recalcular_cortes_relativos (que solo cubría rentabilidad, no deuda)."""
    calculables = [
        u for u in universo.values()
        if u.get("metodo") not in (None, "no_calculable", "error")
    ]

    # --- Deuda/EBITDA ---
    for u in calculables:
        sector = u.get("sector")
        subindustria = u.get("subindustria")
        deuda = u.get("deuda_neta_ebitda")

        if sector in SECTORES_SIN_DEUDA_EBITDA:
            u["pasa_deuda"] = True
            continue

        aplica_percentil = False
        if sector in GRUPOS_PERCENTIL_DEUDA:
            subset_filtro = GRUPOS_PERCENTIL_DEUDA[sector]
            if subset_filtro is None or subindustria in subset_filtro:
                aplica_percentil = True

        if aplica_percentil:
            u["pasa_deuda"] = None  # pendiente del percentil del grupo, más abajo
        else:
            u["pasa_deuda"] = (deuda is not None) and (deuda < UMBRAL_DEUDA_EBITDA_ESTANDAR)

    for sector, subset_filtro in GRUPOS_PERCENTIL_DEUDA.items():
        del_grupo = [
            u for u in calculables
            if u.get("sector") == sector
            and (subset_filtro is None or u.get("subindustria") in subset_filtro)
            and u.get("deuda_neta_ebitda") is not None
        ]
        if not del_grupo:
            continue
        valores = [u["deuda_neta_ebitda"] for u in del_grupo]
        corte = float(np.quantile(valores, PERCENTIL_DEUDA_SECTOR))
        for u in del_grupo:
            u["pasa_deuda"] = u["deuda_neta_ebitda"] <= corte

    # --- Rentabilidad + consistencia ---
    for u in calculables:
        sector = u.get("sector")
        mediana = u.get("mediana_rentabilidad_8a")
        pct = u.get("pct_trimestres_sostenido")

        if sector in SECTORES_CORTE_RENTABILIDAD_PERCENTIL:
            u["pasa_rentabilidad"] = None  # pendiente del percentil del sector
            u["pasa_consistencia"] = None
        else:
            u["pasa_rentabilidad"] = (mediana is not None) and (mediana > UMBRAL_RENTABILIDAD_SOSTENIDO)
            u["pasa_consistencia"] = (pct is not None) and (pct >= UMBRAL_PCT_SOSTENIDO)

    for sector in SECTORES_CORTE_RENTABILIDAD_PERCENTIL:
        del_sector = [
            u for u in calculables
            if u.get("sector") == sector and u.get("mediana_rentabilidad_8a") is not None
        ]
        if not del_sector:
            continue
        medianas = [u["mediana_rentabilidad_8a"] for u in del_sector]
        consistencias = [u["pct_trimestres_sostenido"] for u in del_sector]
        corte_rent = float(np.quantile(medianas, PERCENTIL_RENTABILIDAD_SECTOR))
        corte_cons = float(np.quantile(consistencias, PERCENTIL_RENTABILIDAD_SECTOR))
        for u in del_sector:
            u["pasa_rentabilidad"] = u["mediana_rentabilidad_8a"] >= corte_rent
            u["pasa_consistencia"] = u["pct_trimestres_sostenido"] >= corte_cons

    # --- Resiliencia 2020/2022 + combinación final ---
    for u in calculables:
        v2020, v2022 = u.get("variacion_2020"), u.get("variacion_2022")
        pasa_resiliencia = (
            (v2020 is None or v2020 > UMBRAL_VARIACION_ESTRES) and
            (v2022 is None or v2022 > UMBRAL_VARIACION_ESTRES)
        )
        u["supera_filtro_calidad"] = bool(
            u.get("pasa_rentabilidad") and u.get("pasa_consistencia")
            and u.get("pasa_deuda") and pasa_resiliencia
        )

    return universo


def main():
    # Chequeo de sanidad: si el módulo filtro_calidad_v1 quedó cacheado de
    # una versión vieja en esta sesión (p. ej. Colab sin reiniciar tras
    # subir el archivo corregido), esto falla aquí con un mensaje claro,
    # en el primer segundo — no después de recorrer los 503 tickers.
    assert UMBRAL_PCT_SOSTENIDO == 0.70, (
        "filtro_calidad_v1 parece una versión desactualizada en memoria. "
        "Si estás en Colab: reinicia el entorno de ejecución (no basta con "
        "volver a subir el archivo) y vuelve a correr desde el principio."
    )

    sp500 = pd.read_csv(RUTA_SP500)
    previo = cargar_universo_previo()

    universo = {}
    n_actualizados = 0
    for _, row in sp500.iterrows():
        ticker, sector, subindustria = row["ticker"], row["sector"], row["subindustria"]
        entrada, actualizado = actualizar_ticker(
            ticker, sector, subindustria, previo.get(ticker)
        )
        universo[ticker] = entrada
        if actualizado:
            n_actualizados += 1
            print(f"  {ticker}: recalculado (trimestre nuevo)")

    # Checkpoint: guardado ANTES de recalcular_todos_los_cortes() — si algo
    # falla ahí, la hora larga de llamadas a FMP ya está a salvo en disco
    # (mismo principio que calidad_v4_raw.csv en el desarrollo del filtro).
    with open(RUTA_UNIVERSO + ".tmp", "w") as f:
        json.dump({
            "generado_en_utc": datetime.now(timezone.utc).isoformat(),
            "universo": list(universo.values()),
        }, f, indent=2, ensure_ascii=False)

    universo = recalcular_todos_los_cortes(universo)

    salida = {
        "generado_en_utc": datetime.now(timezone.utc).isoformat(),
        "universo": list(universo.values()),
    }
    with open(RUTA_UNIVERSO, "w") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    n_supervivientes = sum(1 for u in universo.values() if u.get("supera_filtro_calidad"))
    print(f"\nTotal: {len(universo)} | Actualizados hoy: {n_actualizados} | "
          f"Supervivientes de calidad: {n_supervivientes}")


if __name__ == "__main__":
    main()
