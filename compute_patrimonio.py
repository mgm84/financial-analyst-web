"""
Calculo de patrimonio total / IA / Equity -- para dashboard y correos
=======================================================================
Lee data/market_data.json (precios + variaciones, ya calculados por
refresh_market_data.py) y data/positions.json (cantidades por posicion,
mantenido a mano por Mariano -- este script NUNCA escribe en ese fichero,
solo lo lee). Calcula, en EUR, el valor de mercado y el rendimiento
ponderado de cada bloque (IA, Equity = DEGIRO preexistente + GBM Mexico
juntos, y Total = IA + Equity).

Rendimiento ponderado sin necesitar un historico dia a dia: reconstruye
el valor de cada posicion hace N dias a partir de su chg% (que ya viene
calculado por FMP/yfinance) y de la cantidad de titulos, y compara la
suma de hoy contra la suma reconstruida.

Dos periodos, pensados para encajar tal cual con lo que pidio Mariano:
  - "diario": usa chg.1D -> rendimiento vs el cierre de ayer (correo diario)
  - "semanal": usa chg.5D -> vs hace 7 dias naturales. Como el correo
    semanal se manda en lunes, esto equivale exactamente a "vs el lunes
    anterior" sin necesitar una metrica nueva.

Posiciones sin cantidad confirmada en positions.json (valor null, o
ticker que no aparece en market_data.json) se EXCLUYEN del calculo y se
listan en "posiciones_excluidas" de cada bloque -- nunca se asumen a 0 ni
se omiten en silencio. Mientras haya alguna excluida, el bloque se marca
"incompleto": true para que dashboard/correo lo puedan avisar.

FIX 2026-09-15 (encontrado al preparar el contenido del correo): los
bloques combinados "Equity" (DEGIRO + GBM) y "Total" (IA + Equity) solo
llevaban valor_eur -- nunca se les calculaba un rendimiento_pct propio,
asi que la cabecera del correo (que pide rendimiento del patrimonio
total y de Equity, no solo de IA) no tenia de donde sacarlo. Antes se
calculaba cada bloque por separado y se sumaban los valor_eur a mano;
ahora "Equity" y "Total" se calculan con la misma calcular_bloque()
sobre el diccionario de posiciones fusionado, para que salga un
rendimiento ponderado real (mismo criterio que los demas bloques) en
vez de quedar sin ese dato. No hay tickers duplicados entre IA/DEGIRO/
GBM en positions.json, así que fusionar los diccionarios es seguro.
"""

import json
from pathlib import Path

MARKET_PATH = Path("data/market_data.json")
POSITIONS_PATH = Path("data/positions.json")
OUT_PATH = Path("data/patrimonio.json")

MONEDAS_SOPORTADAS = ("USD", "EUR")


def valor_hoy_eur(qty, info, eur_usd):
    price = info["price"]
    currency = info.get("currency", "USD")
    if currency not in MONEDAS_SOPORTADAS:
        raise ValueError(f"Divisa no soportada en calculo de patrimonio: {currency}")
    valor = qty * price
    return valor / eur_usd if currency == "USD" else valor


def calcular_bloque(posiciones, market, periodo, eur_usd):
    total_hoy = 0.0
    total_pasado = 0.0
    incompleto = False
    excluidas = []

    for ticker, qty in posiciones.items():
        if qty is None:
            excluidas.append(ticker)
            incompleto = True
            continue
        info = market.get(ticker)
        if info is None:
            excluidas.append(ticker)
            incompleto = True
            continue

        hoy_eur = valor_hoy_eur(qty, info, eur_usd)
        total_hoy += hoy_eur

        chg = info["chg"].get(periodo)
        if chg is None:
            incompleto = True
        else:
            total_pasado += hoy_eur / (1 + chg / 100)

    rendimiento_pct = None
    if total_pasado > 0 and not incompleto:
        rendimiento_pct = round((total_hoy / total_pasado - 1) * 100, 2)

    return {
        "valor_eur": round(total_hoy, 2),
        "rendimiento_pct": rendimiento_pct,
        "incompleto": incompleto,
        "posiciones_excluidas": excluidas,
    }


if __name__ == "__main__":
    market_raw = json.loads(MARKET_PATH.read_text())
    positions = json.loads(POSITIONS_PATH.read_text())
    market = market_raw["market"]
    eur_usd = market_raw["eur_usd"]

    if eur_usd is None:
        raise SystemExit("eur_usd es null en market_data.json -- no se puede convertir nada a EUR, abortando")

    bloques_posiciones = {k: v for k, v in positions.items() if not k.startswith("_")}

    resultado = {
        "generado_en_utc": market_raw["generado_en_utc"],
        "eur_usd": eur_usd,
        "periodos": {},
    }

    for periodo, etiqueta in (("1D", "diario"), ("5D", "semanal")):
        bloques = {}
        for nombre_bloque, posiciones_bloque in bloques_posiciones.items():
            bloques[nombre_bloque] = calcular_bloque(posiciones_bloque, market, periodo, eur_usd)

        posiciones_equity = {**bloques_posiciones["Equity_DEGIRO"], **bloques_posiciones["Equity_GBM"]}
        posiciones_total = {**bloques_posiciones["IA"], **posiciones_equity}

        equity = calcular_bloque(posiciones_equity, market, periodo, eur_usd)
        total = calcular_bloque(posiciones_total, market, periodo, eur_usd)

        resultado["periodos"][etiqueta] = {
            "bloques": bloques,
            "Equity": equity,
            "Total": total,
        }

    OUT_PATH.write_text(json.dumps(resultado, indent=2, ensure_ascii=False))
    print(f"Escrito {OUT_PATH}\n")

    for etiqueta, datos in resultado["periodos"].items():
        ia = datos["bloques"]["IA"]
        eq = datos["Equity"]
        tot = datos["Total"]
        print(f"[{etiqueta}] IA: {ia['valor_eur']:,.2f}€ ({ia['rendimiento_pct']}%)"
              f"  Equity: {eq['valor_eur']:,.2f}€ ({eq['rendimiento_pct']}%){'  [INCOMPLETO]' if eq['incompleto'] else ''}"
              f"  Total: {tot['valor_eur']:,.2f}€ ({tot['rendimiento_pct']}%){'  [INCOMPLETO]' if tot['incompleto'] else ''}")
        if eq["posiciones_excluidas"]:
            print(f"    excluidas de Equity: {eq['posiciones_excluidas']}")
