#!/usr/bin/env python3
"""
Generador de los correos de monitor de cartera (diario / semanal).

Uso:
    python3 build_emails_prod.py \
        --data-dir  <carpeta con market_data.json, patrimonio.json, positions.json, percentiles.json, tramos_status.json> \
        --content   <content.json con noticias y estado de Puerta 3, generado por WebSearch en la tarea programada> \
        --out-dir   <carpeta de salida> \
        [--weekly]  # además del correo diario, genera también el semanal (solo lunes)

Escribe en --out-dir: daily.html, meta.json (y weekly.html si --weekly).
meta.json trae {"daily_subject": "...", "weekly_subject": "..." (si aplica)}
para que la tarea programada sepa qué asunto poner en el envío.

Diseño (fondo de titulo de seccion en <td>, no en <h2>/<div>) verificado
contra Gmail real el 2026-09-15 -- ver notas en h2().

FIX 2026-09-17: el titulo "Cierre {fecha}" usaba generado_en_utc (el
instante en que corrio el job en GitHub Actions), no la fecha real del
cierre bursatil que reportan los precios -- ver FIX 2026-09-17 en
refresh_market_data.py y compute_patrimonio.py. Ahora usa
patrimonio["fecha_cierre"] si esta presente (con fallback a
generado_en_utc para snapshots viejos de antes del fix).

FIX 2026-09-22: cuando Yahoo falla para EQQQ/VUSA, refresh_market_data.py
cae a un precio proxy (ETF equivalente en mercado US via FMP, convertido
a EUR -- ver ese script) y marca la entrada con "proxy": true en
market_data.json; compute_patrimonio.py propaga eso a cada bloque como
"tiene_proxy"/"posiciones_proxy". Aqui se muestra esa advertencia en
tres sitios, tal como pidio Mariano: (1) junto al nombre de la posicion
afectada, en ambas tablas de performance; (2) junto al subtotal de la
subcartera que la contiene; (3) en las tarjetas de cabecera (Total/IA/
Equity) cuando el bloque incluye alguna posicion en proxy. El proxy no
trae un maximo de 52 semanas garantizado (puede venir None si algo
fallo tambien calculandolo) -- el "vs máx. 52 sem." se blinda contra
eso y muestra "—" en vez de reventar con una division por None.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

NOMBRES = {
    "EQIX": "Equinix", "ASML": "ASML Holding", "AVGO": "Broadcom", "TSM": "TSMC",
    "PWR": "Quanta Services", "ETN": "Eaton", "ANET": "Arista Networks",
    "GOOGL": "Alphabet", "MSFT": "Microsoft", "AMD": "AMD",
    "EQQQ": "EQQQ (Nasdaq-100 UCITS)", "VUSA": "VUSA (S&P 500 UCITS)",
    "VOO": "Vanguard S&P 500", "QQQ": "Invesco QQQ", "QQQM": "Invesco NASDAQ 100",
    "IVW": "iShares S&P 500 Growth",
}

DASHBOARD_URL = "https://claude.ai/artifact/R98hFKKJPtjtCDAiZDq5aB"

FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"

ACCENT = {
    "noticias": "#2f5fa8", "performance": "#1c7a72", "puertas": "#6a4c93",
    "puerta1": "#b3261e", "puerta2": "#a3720b", "puerta3": "#c2570e",
    "proxy": "#a3720b",
}
ACCENT_LIGHT = {
    "noticias": "#eaf1fb", "performance": "#e5f4f2", "puertas": "#f0eaf7",
    "puerta1": "#fdf2f1", "puerta2": "#faf3e1", "puerta3": "#fdece0",
}


# ---------------- helpers de formato ----------------

def eur(n, decimals=2):
    if n is None:
        return "—"
    s = f"{abs(n):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "−" if n < 0 else ""
    return f"{sign}{s} €"


def pct(n, show_plus=True):
    if n is None:
        return "—"
    s = f"{abs(n):.2f}".replace(".", ",")
    sign = "−" if n < 0 else ("+" if show_plus else "")
    return f"{sign}{s}%"


def color(n):
    if n is None:
        return "#666666"
    return "#1a7a3c" if n >= 0 else "#b3261e"


def banda_color(banda):
    if "CARA" in banda:
        return "#b3261e"
    if "COMODA" in banda or "CÓMODA" in banda:
        return "#1a7a3c"
    return "#5a5748"


def proxy_badge():
    return f' <span style="color:{ACCENT["proxy"]};font-weight:600;">⚠ proxy</span>'


def h2(title, key):
    # Nota (2026-09-15): un <h2>/<div> con background inline no se pinta en
    # Gmail real (aunque si en el render de Playwright usado para el mockup)
    # -- Gmail y varios clientes de correo ignoran background-color en
    # elementos de bloque sueltos mientras que si lo respetan en <td>. Por
    # eso el titulo va envuelto en una tabla de una celda -- verificado
    # contra un envio real ([TEST v6], confirmado por Mariano 2026-09-15.
    accent = ACCENT[key]
    light = ACCENT_LIGHT[key]
    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0 8px;"><tr>
      <td style="background:{light};background-color:{light};padding:8px 12px;border-radius:6px;{FONT}font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:{accent};">{title}</td>
    </tr></table>'''


# ---------------- bloques de tabla ----------------

def kpi_cards(bloque_total, bloque_ia, bloque_eq, label_periodo):
    def card(label, b):
        return f'''<td style="padding:14px 18px;background:#faf9f5;border:1px solid #e8e5db;border-radius:8px;{FONT}" width="33%">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a8677;margin-bottom:6px;">{label}</div>
          <div style="font-size:22px;font-weight:700;color:#141413;">{eur(b["valor_eur"])}</div>
          <div style="font-size:14px;font-weight:600;color:{color(b["rendimiento_pct"])};margin-top:2px;">{pct(b["rendimiento_pct"])} {label_periodo}</div>
          {f'<div style="font-size:11px;color:#b3261e;margin-top:4px;">⚠ datos incompletos: {", ".join(b["posiciones_excluidas"])}</div>' if b.get("incompleto") else ""}
          {f'<div style="font-size:11px;color:{ACCENT["proxy"]};margin-top:4px;">⚠ incluye precio proxy: {", ".join(b["posiciones_proxy"])}</div>' if b.get("tiene_proxy") else ""}
        </td>'''

    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0;"><tr>
      {card("Patrimonio total", bloque_total)}
      <td width="12"></td>
      {card("Capas IA", bloque_ia)}
      <td width="12"></td>
      {card("Equity (DEGIRO+GBM)", bloque_eq)}
    </tr></table>'''


def compute_portfolio_weights(market, positions, eur_usd):
    """Valor en EUR de cada posición (titulos x precio, convertido si hace
    falta) + peso dentro de su subcartera y dentro de la cartera total.
    Devuelve {grupo_label: {"total": float, "rows": [{ticker, info, value_eur,
    peso_sub, peso_total}, ...]}} con las filas de cada grupo ordenadas de
    mayor a menor peso dentro de esa subcartera; las posiciones sin datos de
    mercado van al final con value_eur=None y no cuentan para los totales."""
    grupos_def = [("Capas sectoriales IA", "IA"),
                  ("Equity — DEGIRO", "Equity_DEGIRO"),
                  ("Equity — GBM México", "Equity_GBM")]
    grupos = {}
    for label, key in grupos_def:
        rows = []
        for t, shares in positions[key].items():
            info = market.get(t)
            if not info or shares is None:
                rows.append({"ticker": t, "info": info, "value_eur": None})
                continue
            value_native = info["price"] * shares
            value_eur = value_native if info["currency"] == "EUR" else value_native / eur_usd
            rows.append({"ticker": t, "info": info, "value_eur": value_eur})
        total = sum(r["value_eur"] for r in rows if r["value_eur"] is not None)
        rows.sort(key=lambda r: (r["value_eur"] is None, -(r["value_eur"] or 0)))
        grupos[label] = {"total": total, "rows": rows}
    grand_total = sum(g["total"] for g in grupos.values())
    for g in grupos.values():
        for r in g["rows"]:
            if r["value_eur"] is not None:
                r["peso_sub"] = r["value_eur"] / g["total"] * 100 if g["total"] else None
                r["peso_total"] = r["value_eur"] / grand_total * 100 if grand_total else None
            else:
                r["peso_sub"] = r["peso_total"] = None
    return grupos, grand_total


def peso_line(r):
    if r["value_eur"] is None:
        return ""
    return f'{eur(r["value_eur"], 0)} · {pct(r["peso_sub"], False)} subcartera · {pct(r["peso_total"], False)} total'


def proxy_tickers_de(grupo):
    return [r["ticker"] for r in grupo["rows"] if r["info"] and r["info"].get("proxy")]


def subtotal_row(ncols, total_eur, grand_total, proxy_tickers=None):
    peso_total = pct(total_eur / grand_total * 100, False) if grand_total else "—"
    fila = f'''<tr>
      <td style="padding:6px 8px;border-top:1px solid #ddd;{FONT}font-size:13px;font-weight:700;">Subtotal</td>
      <td colspan="{ncols-2}" style="padding:6px 8px;border-top:1px solid #ddd;{FONT}font-size:13px;font-weight:700;text-align:right;">{eur(total_eur)}</td>
      <td style="padding:6px 8px;border-top:1px solid #ddd;{FONT}font-size:13px;font-weight:700;text-align:right;">{peso_total} cartera</td>
    </tr>'''
    if proxy_tickers:
        fila += f'''<tr><td colspan="{ncols}" style="padding:2px 8px 8px;{FONT}font-size:11px;color:{ACCENT["proxy"]};">⚠ incluye precio proxy para {", ".join(proxy_tickers)} — subtotal y rendimiento de este grupo son una aproximación, ver nota al pie</td></tr>'''
    return fila


def perf_vs_52w_table(market, positions, eur_usd):
    grupos, grand_total = compute_portfolio_weights(market, positions, eur_usd)
    rows = []
    for grupo_label, g in grupos.items():
        rows.append(f'<tr><td colspan="4" style="padding:10px 8px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#8a8677;{FONT}">{grupo_label}</td></tr>')
        for r in g["rows"]:
            t, info = r["ticker"], r["info"]
            if not info:
                rows.append(f'<tr><td style="padding:6px 8px;{FONT}font-size:13px;">{NOMBRES.get(t,t)} ({t})</td><td colspan="3" style="padding:6px 8px;color:#8a8677;{FONT}font-size:13px;">sin datos de mercado</td></tr>')
                continue
            high52 = info.get("high52")
            vs52 = round((info["price"] / high52 - 1) * 100, 2) if high52 else None
            chg1d = info["chg"].get("1D")
            precio_fmt = eur(info["price"], 2) if info["currency"] == "EUR" else f'${info["price"]:,.2f}'
            badge = proxy_badge() if info.get("proxy") else ""
            rows.append(f'''<tr>
              <td style="padding:6px 8px;border-bottom:1px solid #eee;{FONT}font-size:13px;">{NOMBRES.get(t,t)} ({t}){badge}<div style="font-size:11px;color:#8a8677;margin-top:2px;">{peso_line(r)}</div></td>
              <td style="padding:6px 8px;border-bottom:1px solid #eee;{FONT}font-size:13px;text-align:right;color:{color(chg1d)};">{pct(chg1d)}</td>
              <td style="padding:6px 8px;border-bottom:1px solid #eee;{FONT}font-size:13px;text-align:right;">{precio_fmt}</td>
              <td style="padding:6px 8px;border-bottom:1px solid #eee;{FONT}font-size:13px;text-align:right;color:{color(vs52)};">{pct(vs52)} vs máx. 52 sem.</td>
            </tr>''')
        rows.append(subtotal_row(4, g["total"], grand_total, proxy_tickers=proxy_tickers_de(g)))
    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:6px;">
      <tr><td style="padding:4px 8px;{FONT}font-size:11px;color:#8a8677;">Posición</td><td style="padding:4px 8px;{FONT}font-size:11px;color:#8a8677;text-align:right;">1D</td><td style="padding:4px 8px;{FONT}font-size:11px;color:#8a8677;text-align:right;">Precio</td><td style="padding:4px 8px;{FONT}font-size:11px;color:#8a8677;text-align:right;">vs. máx. 52 sem.</td></tr>
      {"".join(rows)}
    </table>'''


def hist_returns_table(market, positions, eur_usd):
    grupos, grand_total = compute_portfolio_weights(market, positions, eur_usd)
    periods = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y"]
    rows = []
    header_cells = "".join(f'<td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:right;">{p}</td>' for p in periods)
    rows.append(f'<tr><td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;">Posición</td>{header_cells}</tr>')
    for grupo_label, g in grupos.items():
        rows.append(f'<tr><td colspan="{1+len(periods)}" style="padding:10px 6px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#8a8677;{FONT}">{grupo_label}</td></tr>')
        for r in g["rows"]:
            t, info = r["ticker"], r["info"]
            if not info:
                rows.append(f'<tr><td style="padding:5px 6px;{FONT}font-size:12.5px;">{NOMBRES.get(t,t)}</td><td colspan="{len(periods)}" style="padding:5px 6px;color:#8a8677;{FONT}font-size:12.5px;">sin datos</td></tr>')
                continue
            badge = proxy_badge() if info.get("proxy") else ""
            cells = "".join(f'<td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:right;color:{color(info["chg"].get(p))};">{pct(info["chg"].get(p))}</td>' for p in periods)
            rows.append(f'<tr><td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;">{NOMBRES.get(t,t)} ({t}){badge}<div style="font-size:10.5px;color:#8a8677;margin-top:2px;">{peso_line(r)}</div></td>{cells}</tr>')
        rows.append(subtotal_row(1 + len(periods), g["total"], grand_total, proxy_tickers=proxy_tickers_de(g)))
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:6px;">{"".join(rows)}</table>'


def puerta2_table(percentiles):
    claves = [m for m in percentiles["metricas"] if m.get("es_clave")]
    claves.sort(key=lambda m: m["percentil"])
    rows = []
    for m in claves:
        rows.append(f'''<tr>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;">{NOMBRES.get(m["ticker"], m["ticker"])} ({m["ticker"]})</td>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:center;color:#8a8677;">{m["metrica"]}</td>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:right;">{m["hoy"]:.1f}</td>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:right;color:#8a8677;">{m["mediana"]:.1f}</td>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:right;font-weight:600;">{m["percentil"]}</td>
          <td style="padding:5px 6px;border-bottom:1px solid #eee;{FONT}font-size:12.5px;text-align:right;color:{banda_color(m["banda"])};">{m["banda"]}</td>
        </tr>''')
    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:6px;">
      <tr>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;">Posición</td>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:center;">Métrica clave</td>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:right;">Hoy</td>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:right;">Mediana hist.</td>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:right;">Percentil</td>
        <td style="padding:4px 6px;{FONT}font-size:11px;color:#8a8677;text-align:right;">Banda</td>
      </tr>
      {"".join(rows)}
    </table>'''


def noticia_block(item):
    fuente = item.get("fuente_nombre", "")
    url = item.get("fuente_url", "")
    fuente_html = f'<a href="{url}" style="color:#8a6d3b;">{fuente}</a>' if url else fuente
    return f'''<div style="padding:12px 14px;background:#faf9f5;border-left:3px solid {ACCENT["noticias"]};border-radius:0 8px 8px 0;margin-top:8px;">
    <div style="font-weight:600;font-size:14px;">{item.get("titulo","")}</div>
    <div style="font-size:13px;color:#555;margin-top:4px;">{item.get("resumen","")}</div>
    {f'<div style="font-size:12px;color:#8a8677;margin-top:6px;">{fuente_html}</div>' if fuente_html else ""}
  </div>'''


# ---------------- Puertas (generadas a partir de datos reales + investigación) ----------------

def puerta1_html(tramos):
    p1 = tramos["puerta_1"]
    cesta = tramos["cesta"]
    accent = ACCENT["puerta1"]
    if p1["estado"] == "DISPARADO":
        cabecera = (f'DISPARADA — {pct(cesta["caida_eur_pct"])} EUR desde el máximo del '
                    f'{fecha_es(cesta["maximo_eur_fecha"])} (umbral −{p1["umbral_pct"]}%). '
                    f'Acción sugerida: {p1["accion_sugerida"]}.')
        aviso = (f'''<div style="margin-top:8px;padding:10px 12px;background:#fdf2f1;border:1px solid #f3c9c5;border-radius:6px;{FONT}font-size:13px;color:#7a231d;">
          ⚠ Puerta 1 disparada — hay una decisión de aceleración de tramo pendiente de confirmar. Revisa el dashboard.
        </div>''')
    else:
        umbral_txt = f' (umbral −{p1["umbral_pct"]}%)' if p1.get("umbral_pct") is not None else ''
        cabecera = (f'NO disparada — {pct(cesta["caida_eur_pct"])} EUR desde el máximo del '
                    f'{fecha_es(cesta["maximo_eur_fecha"])}{umbral_txt}.')
        aviso = ""
    if not p1.get("cooldown_ok", True) and p1.get("cooldown_msg"):
        aviso += f'''<div style="margin-top:8px;padding:10px 12px;background:#faf3e1;border:1px solid #ecd9a8;border-radius:6px;{FONT}font-size:13px;color:#6b530a;">
          {p1["cooldown_msg"]}
        </div>'''
    return f'''<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:4px;">
        <tr><td style="padding:8px 0;{FONT}font-size:13px;border-bottom:1px solid #eee;"><b style="color:{accent};">Puerta 1 (drawdown):</b> {cabecera}</td></tr>
      </table>
      {aviso}
    </div>'''


def puerta2_html(percentiles):
    accent = ACCENT["puerta2"]
    claves = [m for m in percentiles["metricas"] if m.get("es_clave")]
    comodas = sorted([m for m in claves if "COMODA" in m["banda"] or "CÓMODA" in m["banda"]], key=lambda m: m["percentil"])
    caras = [m for m in claves if "CARA" in m["banda"]]
    if comodas:
        nombres = ", ".join(f'{NOMBRES.get(m["ticker"], m["ticker"])} (percentil {m["percentil"]})' for m in comodas)
        if len(comodas) == len(claves):
            resumen = f'Todas las posiciones clave están en banda cómoda: {nombres}.'
        else:
            resumen = f'{nombres} en banda cómoda; el resto ({len(caras)} de {len(claves)}) sigue caro.'
    else:
        resumen = f'Ninguna posición clave está en banda cómoda — las {len(claves)} siguen caras o en rango normal frente a su histórico.'
    return f'''<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;margin-top:18px;">
      <div style="font-size:13px;font-weight:600;{FONT}"><b style="color:{accent};">Puerta 2 (valoración):</b> percentil histórico de la métrica clave por posición, de más cómodo a más caro</div>
      <div style="font-size:12px;color:#8a8677;margin:2px 0 4px;{FONT}">{resumen}</div>
      {puerta2_table(percentiles)}
    </div>'''


def puerta3_html(content):
    accent = ACCENT["puerta3"]
    p3 = content.get("puerta3", {}) if content else {}
    activo = p3.get("activo", False)
    detalle = p3.get("detalle") or ("Sin descalificadores activos — ninguna de las 5 condiciones (escalada militar en Taiwán, "
                                     "recorte material de CapEx de un hyperscaler, evento de crédito en financiación de infraestructura IA, "
                                     "nuevos controles de exportación de semiconductores, laboratorios frontera pidiendo frenar el desarrollo) está presente.")
    etiqueta = "ACTIVO — bloquea cualquier aceleración" if activo else "sin activar"
    return f'''<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;margin-top:18px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="padding:8px 0;{FONT}font-size:13px;"><b style="color:{accent};">Puerta 3 (descalificador):</b> {etiqueta}. {detalle}</td></tr>
      </table>
    </div>'''


def ratios_puertas(tramos, percentiles, content):
    return puerta1_html(tramos) + puerta2_html(percentiles) + puerta3_html(content)


def footer(patri, tiene_proxy_en_algun_bloque):
    aviso_proxy = ""
    if tiene_proxy_en_algun_bloque:
        aviso_proxy = (f'⚠ Proxy: cuando Yahoo falla al pedir el precio de EQQQ/VUSA, se usa como estimación su '
                        f'ETF equivalente en mercado US (QQQ/VOO vía FMP, mismo índice subyacente), convertido a '
                        f'EUR con el cambio de cierre del día anterior — es una aproximación, no el precio real '
                        f'de la UCITS.<br>')
    return f'''<div style="margin-top:24px;padding-top:14px;border-top:1px solid #e8e5db;{FONT}font-size:11px;color:#8a8677;">
      Datos generados el {patri["generado_en_utc"][:16].replace("T"," ")} UTC · EUR/USD {patri["eur_usd"]}<br>
      Fuente: FMP (US/global) + yfinance (EQQQ.DE, VUSA.AS) · 1D = cierre del día anterior<br>
      {aviso_proxy}
      <a href="{DASHBOARD_URL}" style="color:#8a6d3b;">Ver dashboard en vivo →</a>
    </div>'''


# ---------------- fechas ----------------

MESES = {}  # DD/MM es suficiente para el formato usado en el diseño aprobado


def fecha_es(iso_date):
    # iso_date tipo "2026-06-22" -> "22/06"
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return d.strftime("%d/%m")
    except Exception:
        return iso_date


def hay_proxy(periodo_datos):
    """True si el Total, Equity o cualquier bloque de este periodo
    (diario/semanal) incluye alguna posición en modo proxy -- controla si
    se añade la nota explicativa al pie del correo."""
    bloques = list(periodo_datos["bloques"].values()) + [periodo_datos["Equity"], periodo_datos["Total"]]
    return any(b.get("tiene_proxy") for b in bloques)


# ---------------- construccion de los correos ----------------

def build_daily(data, content):
    market = data["market_data"]["market"]
    positions = data["positions"]
    percentiles = data["percentiles"]
    tramos = data["tramos_status"]
    patri = data["patrimonio"]
    diario = patri["periodos"]["diario"]

    fecha_cierre = fecha_es(data["patrimonio"].get("fecha_cierre") or data["patrimonio"]["generado_en_utc"])
    news_items = (content or {}).get("daily_news", [])[:1]
    noticias_html = "".join(noticia_block(n) for n in news_items) if news_items else \
        f'<div style="{FONT}font-size:13px;color:#8a8677;padding:8px 0;">Sin noticia relevante específica de la cartera identificada hoy.</div>'

    html = f'''<div style="{FONT}max-width:640px;margin:0 auto;padding:20px;color:#141413;">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8a8677;">Monitor de cartera · diario</div>
  <h1 style="font-size:20px;margin:4px 0 2px;">Cierre {fecha_cierre} — Total {eur(diario["Total"]["valor_eur"])} ({pct(diario["Total"]["rendimiento_pct"])})</h1>
  <div style="font-size:13px;color:#8a8677;margin-bottom:4px;">Rendimiento vs. cierre del día anterior</div>

  {kpi_cards(diario["Total"], diario["bloques"]["IA"], diario["Equity"], "1D")}

  {h2("Noticia que mueve la cartera hoy", "noticias")}
  {noticias_html}

  {h2("Performance vs. máximo de 52 semanas", "performance")}
  {perf_vs_52w_table(market, positions, patri["eur_usd"])}

  {h2("Ratios y puertas — Capas sectoriales IA", "puertas")}
  {ratios_puertas(tramos, percentiles, content)}

  {footer(patri, hay_proxy(diario))}
</div>'''
    subject = f'Monitor de cartera — Cierre {fecha_cierre} — Total {eur(diario["Total"]["valor_eur"])} ({pct(diario["Total"]["rendimiento_pct"])})'
    return html, subject


def build_weekly(data, content):
    market = data["market_data"]["market"]
    positions = data["positions"]
    percentiles = data["percentiles"]
    tramos = data["tramos_status"]
    patri = data["patrimonio"]
    semanal = patri["periodos"]["semanal"]

    hoy = datetime.now(timezone.utc)
    lunes_esta_semana = hoy - timedelta(days=hoy.weekday())
    lunes_semana_pasada = lunes_esta_semana - timedelta(days=7)
    fecha_semana = lunes_semana_pasada.strftime("%d/%m")

    news_items = (content or {}).get("weekly_news", [])[:2]
    noticias_html = "".join(noticia_block(n) for n in news_items) if news_items else \
        f'<div style="{FONT}font-size:13px;color:#8a8677;padding:8px 0;">Sin noticias relevantes específicas de la cartera identificadas esta semana.</div>'

    html = f'''<div style="{FONT}max-width:640px;margin:0 auto;padding:20px;color:#141413;">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8a8677;">Monitor de cartera · resumen semanal</div>
  <h1 style="font-size:20px;margin:4px 0 2px;">Semana del {fecha_semana} — Total {eur(semanal["Total"]["valor_eur"])} ({pct(semanal["Total"]["rendimiento_pct"])})</h1>
  <div style="font-size:13px;color:#8a8677;margin-bottom:4px;">Rendimiento vs. el lunes anterior</div>

  {kpi_cards(semanal["Total"], semanal["bloques"]["IA"], semanal["Equity"], "5D")}

  {h2("Lo más relevante de la semana", "noticias")}
  {noticias_html}

  {h2("Rendimientos históricos por posición", "performance")}
  {hist_returns_table(market, positions, patri["eur_usd"])}

  {h2("Ratios y puertas — Capas sectoriales IA", "puertas")}
  {ratios_puertas(tramos, percentiles, content)}

  {footer(patri, hay_proxy(semanal))}
</div>'''
    subject = f'Monitor de cartera — Semana del {fecha_semana} — Total {eur(semanal["Total"]["valor_eur"])} ({pct(semanal["Total"]["rendimiento_pct"])})'
    return html, subject


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--content", required=False, default=None)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--weekly", action="store_true")
    args = ap.parse_args()

    def load(name):
        with open(os.path.join(args.data_dir, name)) as f:
            return json.load(f)

    data = {
        "market_data": load("market_data.json"),
        "patrimonio": load("patrimonio.json"),
        "positions": load("positions.json"),
        "percentiles": load("percentiles.json"),
        "tramos_status": load("tramos_status.json"),
    }
    content = {}
    if args.content and os.path.exists(args.content):
        with open(args.content) as f:
            content = json.load(f)

    os.makedirs(args.out_dir, exist_ok=True)
    meta = {}

    daily_html, daily_subject = build_daily(data, content)
    with open(os.path.join(args.out_dir, "daily.html"), "w") as f:
        f.write(daily_html)
    meta["daily_subject"] = daily_subject

    if args.weekly:
        weekly_html, weekly_subject = build_weekly(data, content)
        with open(os.path.join(args.out_dir, "weekly.html"), "w") as f:
            f.write(weekly_html)
        meta["weekly_subject"] = weekly_subject

    with open(os.path.join(args.out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
