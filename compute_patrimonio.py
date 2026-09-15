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

        ia = bloques["IA"]
        eq_degiro = bloques["Equity_DEGIRO"]
        eq_gbm = bloques["Equity_GBM"]

        equity_valor = eq_degiro["valor_eur"] + eq_gbm["valor_eur"]
        equity_incompleto = eq_degiro["incompleto"] or eq_gbm["incompleto"]
        equity_excluidas = eq_degiro["posiciones_excluidas"] + eq_gbm["posiciones_excluidas"]

        total_valor = ia["valor_eur"] + equity_valor
        total_incompleto = ia["incompleto"] or equity_incompleto

        resultado["periodos"][etiqueta] = {
            "bloques": bloques,
            "Equity": {
                "valor_eur": round(equity_valor, 2),
                "incompleto": equity_incompleto,
                "posiciones_excluidas": equity_excluidas,
            },
            "Total": {
                "valor_eur": round(total_valor, 2),
                "incompleto": total_incompleto,
            },
        }

    OUT_PATH.write_text(json.dumps(resultado, indent=2, ensure_ascii=False))
    print(f"Escrito {OUT_PATH}\n")

    for etiqueta, datos in resultado["periodos"].items():
        ia = datos["bloques"]["IA"]
        eq = datos["Equity"]
        tot = datos["Total"]
        print(f"[{etiqueta}] IA: {ia['valor_eur']:,.2f}€ ({ia['rendimiento_pct']}%)"
              f"  Equity: {eq['valor_eur']:,.2f}€{'  [INCOMPLETO]' if eq['incompleto'] else ''}"
              f"  Total: {tot['valor_eur']:,.2f}€{'  [INCOMPLETO]' if tot['incompleto'] else ''}")
        if eq["posiciones_excluidas"]:
            print(f"    excluidas de Equity: {eq['posiciones_excluidas']}")
