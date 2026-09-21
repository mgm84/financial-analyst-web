"""
Percentiles Diarios — recálculo diario del ranking "Calidad + Barata"
=======================================================================
Segundo script del pipeline. Corre en GitHub Actions, DESPUÉS de
calidad_universo.py, cada día de mercado.

QUÉ HACE
  Para cada ticker que supera el filtro de calidad (leído de
  calidad_universo.json, ya calculado por el script anterior):
    1. Actualiza la caché de precios de ese ticker SOLO con los días
       nuevos desde la última ejecución (no vuelve a descargar 10 años
       de historia cada día — ver actualizar_cache_precio()).
    2. Calcula el ratio de hoy = precio / último denominador conocido.
    3. Calcula el percentil y "trimestres más barato" contra la serie
       histórica ya guardada, cruzando cada fecha con SU PROPIO precio
       (no con el de hoy).
    4. Calcula vs. máx 52 semanas y vs. máx histórico (9a) a partir de la
       caché de precios.
    5. Filtra por el umbral de percentil ≤20 (no "los 10 mejores pase lo
       que pase" — si solo 4 cruzan el umbral, el correo muestra 4).
    6. Marca "nuevo_hoy" comparando el CONJUNTO COMPLETO de tickers bajo
       el umbral hoy contra el conjunto completo de ayer (no solo contra
       el top 10 mostrado — un ticker que pasa del puesto 25 al 9 es
       nuevo aunque nunca haya estado fuera del top 10 "visible").

SALIDAS
  percentiles_diarios_historico.json — APPEND, nunca se sobrescribe.
  ranking_hoy.json — snapshot de hoy, consumido por el correo y el dashboard.
  precios_cache.json — caché incremental de precios, no se reconstruye
    desde cero cada día (ver actualizar_cache_precio()).
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from filtro_calidad_v1 import get, BASE, EP  # noqa: E402

RUTA_UNIVERSO = "data/calidad_universo.json"
RUTA_HISTORICO = "data/percentiles_diarios_historico.json"
RUTA_RANKING_HOY = "data/ranking_hoy.json"
RUTA_CACHE_PRECIOS = "data/precios_cache.json"

TOP_GENERAL = 10
TOP_POR_SECTOR = 5
UMBRAL_PERCENTIL_ALERTA = 20  # "bajo el umbral" — filtra el ranking general, no solo lo etiqueta

# Umbral bajo el cual "más barata" no tiene significado económico fiable
# (mismo principio que ya aplicamos con capital invertido/equity casi nulo
# en la fase de calidad — un descuento gigante puede ser un denominador
# roto, no una oportunidad real). Se marca, no se filtra en silencio.
UMBRAL_RATIO_SOSPECHOSO = 0.5  # ratio hoy por debajo de la mitad del mínimo histórico

DIAS_HISTORIA_PRECIOS = 365 * 10  # ~10 años, misma ventana que el resto del filtro
DIAS_SOLAPE_ACTUALIZACION = 7  # margen al refrescar la caché, para cubrir festivos/fines de semana


# -----------------------------------------------------------------
# CACHÉ DE PRECIOS — se actualiza de forma incremental, no se
# reconstruye cada día (evita pedir ~2.500 puntos por ticker a diario
# cuando solo cambia 1 precio de cierre nuevo)
# -----------------------------------------------------------------

def cargar_cache_precios():
    if not os.path.exists(RUTA_CACHE_PRECIOS):
        return {}
    with open(RUTA_CACHE_PRECIOS) as f:
        cruda = json.load(f)
    # cada ticker guardado como lista de {fecha, cierre} — se reconstruye
    # el DataFrame al vuelo, no se mantiene en memoria entre tickers
    return cruda


def actualizar_cache_precio(ticker, cache):
    """Descarga solo los días que faltan desde la última fecha cacheada.
    Si no hay caché previa para este ticker (primera ejecución), descarga
    la ventana completa de ~10 años una única vez."""
    entradas_previas = cache.get(ticker, [])

    if entradas_previas:
        ultima_fecha = max(e["fecha"] for e in entradas_previas)
        desde = (pd.Timestamp(ultima_fecha) - pd.Timedelta(days=DIAS_SOLAPE_ACTUALIZACION)).strftime("%Y-%m-%d")
    else:
        desde = (pd.Timestamp.today() - pd.Timedelta(days=DIAS_HISTORIA_PRECIOS)).strftime("%Y-%m-%d")

    hoy = pd.Timestamp.today().strftime("%Y-%m-%d")
    filas = get(EP["precios"], symbol=ticker, **{"from": desde, "to": hoy})

    nuevas = {f["date"]: float(f["close"]) for f in filas}
    existentes = {e["fecha"]: e["cierre"] for e in entradas_previas}
    existentes.update(nuevas)  # las fechas nuevas sobrescriben el solape, el resto se conserva

    fusionado = sorted(
        [{"fecha": f, "cierre": c} for f, c in existentes.items()],
        key=lambda x: x["fecha"],
    )
    # recorte: no conservar más de la ventana de ~10 años (evita crecimiento
    # indefinido del archivo con el paso de los años)
    limite = (pd.Timestamp.today() - pd.Timedelta(days=DIAS_HISTORIA_PRECIOS)).strftime("%Y-%m-%d")
    fusionado = [f for f in fusionado if f["fecha"] >= limite]

    cache[ticker] = fusionado
    return fusionado


def serie_a_dataframe(entradas):
    df = pd.DataFrame(entradas)
    df["date"] = pd.to_datetime(df["fecha"])
    df = df.rename(columns={"cierre": "close"}).sort_values("date").reset_index(drop=True)
    return df[["date", "close"]]


def datos_precio_desde_cache(entradas):
    df = serie_a_dataframe(entradas)
    if df.empty:
        return None, df

    hoy = pd.Timestamp.today()
    precio_ayer_cierre = float(df.iloc[-1]["close"])
    max_52s = float(df[df["date"] >= hoy - pd.Timedelta(days=365)]["close"].max())
    max_9a = float(df["close"].max())

    if len(df) >= 2:
        variacion_1d = (df.iloc[-1]["close"] / df.iloc[-2]["close"] - 1) * 100
    else:
        variacion_1d = None

    datos = {
        "precio": precio_ayer_cierre,
        "variacion_1d": round(variacion_1d, 2) if variacion_1d is not None else None,
        "vs_max_52s": round((precio_ayer_cierre / max_52s - 1) * 100, 2) if max_52s else None,
        "vs_max_9a": round((precio_ayer_cierre / max_9a - 1) * 100, 2) if max_9a else None,
    }
    return datos, df


def valor_en(serie_precios_df, fecha):
    """Último precio de cierre en o antes de `fecha` — mismo patrón que
    valor_en() en screen_percentiles_v3.py."""
    prev = serie_precios_df[serie_precios_df["date"] <= fecha]
    return float(prev.iloc[-1]["close"]) if len(prev) else np.nan


# -----------------------------------------------------------------
# PERCENTIL DE VALORACIÓN
# -----------------------------------------------------------------

def calcular_percentil(entrada_calidad, datos_precio, serie_precios_historica):
    """IMPORTANTE: cada ratio histórico usa el precio DE ESA FECHA, no el
    precio de hoy — mismo patrón que serie_precios()/valor_en() en
    screen_percentiles_v3.py."""
    serie = entrada_calidad.get("serie_valoracion")
    if not serie:
        return None

    denominador_actual = serie[-1]["denominador"]
    if denominador_actual in (None, 0):
        return None

    ratio_hoy = datos_precio["precio"] / denominador_actual

    ratios_historicos = []
    for fila in serie[:-1]:
        denom = fila["denominador"]
        if not denom:
            continue
        fecha = pd.Timestamp(fila["fecha"])
        precio_en_fecha = valor_en(serie_precios_historica, fecha)
        if pd.notna(precio_en_fecha):
            ratios_historicos.append(precio_en_fecha / denom)

    if len(ratios_historicos) < 8:
        return {"motivo": "historia_insuficiente"}

    minimo_historico = min(ratios_historicos)
    if minimo_historico > 0 and ratio_hoy < minimo_historico * UMBRAL_RATIO_SOSPECHOSO:
        return {"motivo": "ratio_sospechoso_revisar_denominador"}

    mediana_historica = float(np.median(ratios_historicos))
    trimestres_mas_barato = sum(1 for r in ratios_historicos if r < ratio_hoy)
    trimestres_totales = len(ratios_historicos)
    percentil = round((trimestres_mas_barato / trimestres_totales) * 100) if trimestres_totales else None
    descuento = (1 - ratio_hoy / mediana_historica) * 100 if mediana_historica else None

    return {
        "metrica": entrada_calidad.get("metrica_valoracion"),
        "ratio_hoy": round(ratio_hoy, 2),
        "mediana_historica": round(mediana_historica, 2),
        "trimestres_mas_barato": trimestres_mas_barato,
        "trimestres_totales": trimestres_totales,
        "percentil": percentil,
        "cuanto_mas_barata": round(descuento, 1) if descuento is not None else None,
    }


# -----------------------------------------------------------------
# HISTÓRICO — para "nuevo_hoy" se compara el CONJUNTO COMPLETO bajo
# umbral, no solo el top 10 visible en el correo de ayer
# -----------------------------------------------------------------

def cargar_ultimo_dia_historico():
    try:
        with open(RUTA_HISTORICO) as f:
            lineas = f.readlines()
        if not lineas:
            return None
        return json.loads(lineas[-1])
    except FileNotFoundError:
        return None


def main():
    with open(RUTA_UNIVERSO) as f:
        universo = json.load(f)["universo"]

    supervivientes = [u for u in universo if u.get("supera_filtro_calidad")]
    cache_precios = cargar_cache_precios()

    resultados = []
    for entrada in supervivientes:
        ticker = entrada["ticker"]
        try:
            entradas_cache = actualizar_cache_precio(ticker, cache_precios)
            datos_precio, serie_precios_historica = datos_precio_desde_cache(entradas_cache)
            if datos_precio is None:
                print(f"  {ticker}: sin datos de precio")
                continue

            valoracion = calcular_percentil(entrada, datos_precio, serie_precios_historica)
            if valoracion is None or "motivo" in valoracion:
                print(f"  {ticker}: sin valoración ({valoracion.get('motivo') if valoracion else 'sin serie'})")
                continue

            resultados.append({
                "ticker": ticker,
                "sector": entrada["sector"],
                "subindustria": entrada["subindustria"],
                **datos_precio,
                **valoracion,
            })
            print(f"  {ticker}: ok")
        except Exception as e:
            print(f"  {ticker}: FALLO -> {type(e).__name__}: {e}")

    # Guardar la caché de precios actualizada ANTES de seguir — si algo
    # falla en el procesamiento posterior, el trabajo de red ya hecho no
    # se pierde (mismo principio que el checkpoint de calidad_v4_raw.csv).
    with open(RUTA_CACHE_PRECIOS, "w") as f:
        json.dump(cache_precios, f)

    df = pd.DataFrame(resultados)
    if df.empty:
        print("Sin resultados — no se genera ranking hoy.")
        return

    # "Nº/75" = posición real dentro de TODO el universo calculable, de
    # más barata a más cara — se calcula UNA VEZ sobre el df completo, no
    # solo sobre los que cruzan el umbral, para que las tablas de sector
    # (que muestran el top 5 aunque no crucen el umbral) tengan un número
    # de posición coherente y comparable con el ranking general.
    df = df.sort_values(["percentil", "cuanto_mas_barata"], ascending=[True, False]).reset_index(drop=True)
    df["puesto_general"] = df.index + 1

    # PUNTO 1 corregido: solo entra al ranking GENERAL quien de verdad
    # cruza el umbral — "hasta 10", no "los 10 mejores pase lo que pase".
    df_bajo_umbral = df[df["percentil"] <= UMBRAL_PERCENTIL_ALERTA].copy()

    # PUNTO 2 corregido: "nuevo_hoy" compara el CONJUNTO COMPLETO bajo
    # umbral de ayer contra el de hoy, no solo el top 10 mostrado — un
    # ticker que pasa del puesto 25 al 9 es nuevo aunque nunca haya
    # estado "fuera del top 10 visible" en sentido estricto.
    ultimo_dia = cargar_ultimo_dia_historico()
    tickers_bajo_umbral_ayer = set(ultimo_dia.get("tickers_bajo_umbral", [])) if ultimo_dia else set()

    tickers_bajo_umbral_hoy = set(df_bajo_umbral["ticker"])
    df_bajo_umbral["nuevo_hoy"] = ~df_bajo_umbral["ticker"].isin(tickers_bajo_umbral_ayer)

    ranking_general = df_bajo_umbral.head(TOP_GENERAL).copy()

    # Top 5 por sector: se calcula sobre TODO el universo calculable (df),
    # NO sobre df_bajo_umbral — a diferencia del ranking general, aquí sí
    # quieres ver "el mejor de cada sector" aunque ninguno esté por debajo
    # del umbral (decisión explícita: "quiero ver el top 5 de todos los
    # sectores, aunque no esté en la lista principal"). Filtrar por umbral
    # aquí también habría hecho desaparecer sectores enteros de la tabla
    # solo porque su mejor candidato no está barato HOY, confundiendo eso
    # con "ninguna compañía de este sector supera el filtro de calidad"
    # (que es lo que la leyenda del correo realmente promete explicar).
    df_ordenado = df  # ya viene ordenado y numerado desde arriba, sin necesidad de reordenar
    ranking_por_sector = {}
    for sector, grupo in df_ordenado.groupby("sector"):
        top_sector = grupo.head(TOP_POR_SECTOR)
        ranking_por_sector[sector] = top_sector.to_dict("records")

    sectores_gics_todos = {
        "Information Technology", "Industrials", "Health Care", "Financials",
        "Consumer Staples", "Real Estate", "Consumer Discretionary",
        "Communication Services", "Utilities", "Energy", "Materials",
    }
    # Basado en TODO el universo calculable (df), no en df_bajo_umbral: un
    # sector solo debe aparecer aquí si de verdad no tiene NINGÚN candidato
    # que supere el filtro de calidad y tenga percentil calculable — no
    # simplemente porque su mejor candidato no esté barato hoy.
    sectores_sin_representacion = sorted(sectores_gics_todos - set(df["sector"].unique()))

    entrada_hoy = {
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generado_en_utc": datetime.now(timezone.utc).isoformat(),
        "universo_calculable": len(df),
        "bajo_umbral": len(df_bajo_umbral),
        "tickers_bajo_umbral": sorted(tickers_bajo_umbral_hoy),  # conjunto completo, para el "nuevo_hoy" de mañana
        "ranking_general": ranking_general.to_dict("records"),
        "ranking_por_sector": ranking_por_sector,
        "sectores_sin_representacion": sectores_sin_representacion,
    }

    # Histórico: APPEND, una línea JSON por día (formato JSON Lines) — nunca
    # se sobrescribe, así queda el registro completo para análisis futuro.
    with open(RUTA_HISTORICO, "a") as f:
        f.write(json.dumps(entrada_hoy, ensure_ascii=False) + "\n")

    # Snapshot de hoy: SÍ se sobrescribe, es lo que lee el correo.
    with open(RUTA_RANKING_HOY, "w") as f:
        json.dump(entrada_hoy, f, indent=2, ensure_ascii=False)

    print(f"\nCalculables: {len(df)} | Bajo umbral: {len(df_bajo_umbral)} | "
          f"Ranking general mostrado: {len(ranking_general)} | "
          f"Sectores representados: {len(ranking_por_sector)} | "
          f"Sin representación: {sectores_sin_representacion}")


if __name__ == "__main__":
    main()
