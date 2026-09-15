"""
Monitor de aceleracion de tramos — version automatizada (GitHub Actions)
=========================================================================
Misma logica que monitor_tramos.py (Colab). Cambios para correr sin
intervencion humana en un runner de GitHub Actions:

  1. La API key ya no sale de `google.colab.userdata` — sale de la
     variable de entorno FMP_API_KEY (GitHub Secret).
  2. Ademas de imprimir el reporte en texto, escribe el resultado en
     data/tramos_status.json para que otro proceso (el dashboard) lo
     pueda leer sin tener que parsear el log.
  3. ESCALONES_USADOS y ULTIMA_ACELERACION YA NO SON CONSTANTES EN EL
     CODIGO. Se leen de data/tramos_state.json. Esto es deliberado:
     adelantar un tramo es una decision humana tuya, no algo que este
     script deba decidir o registrar solo. Cuando ejecutes una
     aceleracion, edita tu MISMO data/tramos_state.json a mano
     (o pidele a Claude que lo edite) y haz commit. El job diario
     solo LEE ese archivo, nunca lo escribe.

Requiere en el repo:
  data/tramos_state.json  ->  {"escalones_usados": [], "ultima_aceleracion": null}
  (si no existe, se asume estado inicial: nada usado, ninguna aceleracion)
"""

import os
import json
import time
from pathlib import Path

import requests
import pandas as pd
import numpy as np

API_KEY = os.environ["FMP_API_KEY"]

# ---------------------------------------------------------------
# ESTADO — se lee de disco, NUNCA se escribe desde este script
# ---------------------------------------------------------------

STATE_PATH = Path("data/tramos_state.json")


def cargar_estado():
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text())
        return s.get("escalones_usados", []), s.get("ultima_aceleracion")
    return [], None


ESCALONES_USADOS, ULTIMA_ACELERACION = cargar_estado()

# ---------------------------------------------------------------
# CONFIGURACION (idéntica a la version Colab)
# ---------------------------------------------------------------

PESOS = {
    "EQIX": 0.17,
    "ASML": 0.16,
    "TSM":  0.15,
    "PWR":  0.14,
    "ANET": 0.14,
    "AVGO": 0.13,
    "ETN":  0.11,
}

ESCALONES = [
    (25, "DOS tramos"),
    (15, "UN tramo completo"),
    (10, "MEDIO tramo"),
]

CAIDA_INDIVIDUAL = 25
DIAS = 400
PAUSA = 0.4

BASE = "https://financialmodelingprep.com/stable"
EP_PRECIOS = f"{BASE}/historical-price-eod/full"

OUT_PATH = Path("data/tramos_status.json")


# ---------------------------------------------------------------
# DATOS (idéntico a la version Colab)
# ---------------------------------------------------------------

def serie(symbol, desde, hasta):
    r = requests.get(EP_PRECIOS, timeout=30, params={
        "symbol": symbol, "from": desde, "to": hasta, "apikey": API_KEY})
    r.raise_for_status()
    data = r.json()
    time.sleep(PAUSA)
    filas = data.get("historical", data) if isinstance(data, dict) else data
    df = pd.DataFrame(filas)[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")["close"]
    return df.rename(symbol)


def construir_cesta():
    hasta = pd.Timestamp.today().strftime("%Y-%m-%d")
    desde = (pd.Timestamp.today() - pd.Timedelta(days=DIAS)).strftime("%Y-%m-%d")

    series = {}
    for t in PESOS:
        try:
            series[t] = serie(t, desde, hasta)
            print(f"  {t}: ok")
        except Exception as e:
            print(f"  {t}: FALLO -> {e}")

    fx = serie("EURUSD", desde, hasta)

    px = pd.concat(series.values(), axis=1).dropna()
    fx = fx.reindex(px.index).ffill()

    norm = px / px.iloc[0] * 100
    cesta_usd = sum(norm[t] * w for t, w in PESOS.items() if t in norm.columns)

    px_eur = px.div(fx, axis=0)
    norm_eur = px_eur / px_eur.iloc[0] * 100
    cesta_eur = sum(norm_eur[t] * w for t, w in PESOS.items() if t in norm_eur.columns)

    return px, cesta_usd, cesta_eur, fx


def drawdown(serie_idx):
    maximo = serie_idx.max()
    actual = serie_idx.iloc[-1]
    return (actual / maximo - 1) * 100, maximo, actual, serie_idx.idxmax()


def evaluar(dd_pct):
    caida = abs(dd_pct) if dd_pct < 0 else 0
    for umbral, accion in ESCALONES:
        if caida >= umbral:
            if umbral in ESCALONES_USADOS:
                return umbral, accion, "YA USADO — no repetir"
            return umbral, accion, "DISPARADO"
    return None, None, "sin senal"


def cooldown_ok():
    if ULTIMA_ACELERACION is None:
        return True, ""
    dias = (pd.Timestamp.today() - pd.Timestamp(ULTIMA_ACELERACION)).days
    if dias < 28:
        return False, f"solo han pasado {dias} dias desde la ultima (minimo 28)"
    return True, f"{dias} dias desde la ultima aceleracion"


# ---------------------------------------------------------------
# EJECUCION
# ---------------------------------------------------------------

if __name__ == "__main__":
    px, cesta_usd, cesta_eur, fx = construir_cesta()

    dd_usd, max_usd, act_usd, fecha_max = drawdown(cesta_usd)
    dd_eur, max_eur, act_eur, fecha_max_eur = drawdown(cesta_eur)

    print("\n" + "=" * 78)
    print("CAIDA DE LA CESTA DESDE SU MAXIMO DE 52 SEMANAS")
    print("=" * 78)
    print(f"  En USD: {dd_usd:+6.2f}%")
    print(f"  En EUR: {dd_eur:+6.2f}%   <-- el que manda")
    print(f"  Maximo en EUR alcanzado el: {fecha_max_eur.date()}")
    print(f"  EUR/USD hoy: {fx.iloc[-1]:.4f}")

    umbral, accion, estado = evaluar(dd_eur)
    ok_cool, msg_cool = cooldown_ok()

    filas = []
    for t in px.columns:
        dd_t = (px[t].iloc[-1] / px[t].max() - 1) * 100
        filas.append({
            "ticker": t,
            "peso_pct": round(PESOS[t] * 100),
            "precio": round(float(px[t].iloc[-1]), 2),
            "max_52s": round(float(px[t].max()), 2),
            "caida_pct": round(float(dd_t), 1),
            "senal_idiosincratica": bool(abs(dd_t) >= CAIDA_INDIVIDUAL and dd_t < 0),
        })

    resultado = {
        "generado_en_utc": pd.Timestamp.utcnow().isoformat(),
        "cesta": {
            "caida_usd_pct": round(float(dd_usd), 2),
            "caida_eur_pct": round(float(dd_eur), 2),
            "maximo_eur_fecha": str(fecha_max_eur.date()),
            "eur_usd": round(float(fx.iloc[-1]), 5),
        },
        "puerta_1": {
            "umbral_pct": umbral,
            "accion_sugerida": accion,
            "estado": estado,
            "cooldown_ok": ok_cool,
            "cooldown_msg": msg_cool,
            "escalones_usados": ESCALONES_USADOS,
            "ultima_aceleracion": ULTIMA_ACELERACION,
        },
        "posiciones": filas,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(resultado, indent=2, ensure_ascii=False))
    print(f"\nEscrito {OUT_PATH}")

    # log humano, igual que antes
    print("\nPUERTA 1:", estado, "-", umbral, accion if umbral else "")
    print("Cooldown:", "OK" if ok_cool else "BLOQUEADO", msg_cool)
