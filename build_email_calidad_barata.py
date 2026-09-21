"""
Build Email Calidad + Barata
================================
Tercer script del pipeline. Genera el HTML del correo diario a partir de
ranking_hoy.json, siguiendo la estructura y estilo aprobados en el mockup
de sesión.

TODO EL ESTILO VA INLINE (style="..." en cada elemento), NO en un bloque
<style> del <head> — confirmado en producción: Gmail y la mayoría de
clientes de correo eliminan o ignoran los bloques <style>, y solo
respetan de forma fiable los estilos puestos directamente en cada
elemento. La primera versión de este script usaba clases CSS (igual que
el mockup, que sí funciona en navegador) y el correo real llegó sin
ningún color — misma lección que ya se aprendió con los <h2> de fondo
coloreado del correo de la cartera IA (v5→v6), aplicada aquí también.

Uso:
  python3 build_email_calidad_barata.py --data-dir <carpeta con
    ranking_hoy.json> --out-dir <salida>

Genera: calidad_barata_daily.html + meta.json con el asunto ya formateado.

Correo INDEPENDIENTE del de la cartera IA — no reutiliza build_emails_prod.py
ni sus plantillas, aunque comparte el mismo lenguaje visual por consistencia
de marca.
"""

import argparse
import json
import os

# -----------------------------------------------------------------
# ESTILOS INLINE — un único sitio para cada "clase" visual, para no
# repetir el string de estilo suelto por todo el generador de HTML.
# -----------------------------------------------------------------

S_BODY = "font-family:Arial,Helvetica,sans-serif;background-color:#f2f2f2;margin:0;padding:24px;color:#222;"
S_CONTENEDOR = "max-width:960px;margin:0 auto;background-color:#ffffff;border:1px solid #ddd;"
S_TD_BASE = "padding:6px 6px;text-align:left;font-size:11px;word-wrap:break-word;"

S_CABECERA_TITULO = S_TD_BASE + "background-color:#1a3c5e;color:#ffffff;font-size:18px;font-weight:bold;padding:16px;"
S_CABECERA_SUB = S_TD_BASE + "background-color:#1a3c5e;color:#cdddec;font-size:12px;padding:0 16px 14px 16px;"
S_RESUMEN_CAJA = S_TD_BASE + "background-color:#eef4fa;padding:14px 16px;font-size:13px;line-height:1.5;"
S_TITULO_SECCION = S_TD_BASE + "background-color:#2d5f8a;color:#ffffff;font-size:14px;font-weight:bold;padding:10px 12px;"
S_TITULO_SUBSECCION = S_TD_BASE + "background-color:#dbe7f2;color:#1a3c5e;font-size:12.5px;font-weight:bold;padding:7px 12px;"
S_TH = S_TD_BASE + "background-color:#f4f6f8;color:#555;font-size:9px;text-transform:uppercase;border-bottom:2px solid #ddd;"
S_TD_RANK = S_TD_BASE + "color:#999;text-align:right;"
S_TD_VALOR = S_TD_BASE + "font-weight:bold;color:#1a3c5e;"
S_SPAN_NOMBRE = "font-weight:normal;color:#888;font-size:9.5px;display:block;"
S_TD_NUM = S_TD_BASE + "text-align:right;"
S_TD_METRICA = S_TD_BASE + "text-align:center;color:#666;font-size:10px;"
S_TD_PLANO = S_TD_BASE
S_NOTA = "font-size:11px;color:#888;padding:12px 16px;line-height:1.5;"
S_LEYENDA_CAJA = "background-color:#f8f8f8;padding:12px 16px;font-size:11.5px;line-height:1.6;border-top:1px solid #eee;"
S_PIE = "background-color:#f4f4f4;color:#999;font-size:10.5px;padding:12px 16px;text-align:center;"

C_BARATO, C_MEDIO, C_CARO = "#1e7d3a", "#a15c00", "#a3282e"
C_SUBIDA, C_BAJADA = "#1e7d3a", "#a3282e"

S_PILL_BASE = "display:inline-block;font-size:8.5px;font-weight:bold;padding:2px 5px;border-radius:8px;margin:1px 0;"
S_PILL_NUEVO = S_PILL_BASE + "background-color:#fde8cc;color:#a15c00;"
S_PILL_SOLAPA = S_PILL_BASE + "background-color:#f6dede;color:#a3282e;"
S_PILL_DETERIORO = S_PILL_BASE + "background-color:#eee;color:#666;"

FILA_BLANCA, FILA_ALT = "#ffffff", "#fafbfc"


def fmt_pct(valor, con_signo=True):
    if valor is None:
        return "—"
    signo = "+" if con_signo and valor >= 0 else ""
    return f"{signo}{valor:.1f}%".replace(".", ",")


def fmt_precio(valor):
    if valor is None:
        return "—"
    return f"${valor:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def fmt_ratio(valor):
    if valor is None:
        return "—"
    return f"{valor:.2f}x".replace(".", ",")


def color_num(valor):
    if valor is None:
        return "#222"
    return C_SUBIDA if valor >= 0 else C_BAJADA


def color_barato(valor):
    if valor is None:
        return "#222"
    return C_BARATO if valor >= 0 else C_CARO


def color_percentil(percentil):
    if percentil <= 20:
        return C_BARATO
    elif percentil <= 60:
        return C_MEDIO
    return C_CARO


def fila_general(fila, nombres, con_sector, fondo):
    ticker = fila["ticker"]
    nombre = nombres.get(ticker, ticker)
    alertas = []
    if fila.get("nuevo_hoy"):
        alertas.append(f'<span style="{S_PILL_NUEVO}">NUEVO HOY</span>')
    if fila.get("deterioro_doble"):
        alertas.append(f'<span style="{S_PILL_DETERIORO}">deterioro doble</span>')
    if fila.get("solapa_cartera_ia"):
        alertas.append(f'<span style="{S_PILL_SOLAPA}">solapa IA</span>')

    fondo_style = f"background-color:{fondo};" if fondo != FILA_BLANCA else ""
    td_rank = S_TD_RANK + fondo_style
    td_valor = S_TD_VALOR + fondo_style
    td_plano = S_TD_PLANO + fondo_style
    td_num = S_TD_NUM + fondo_style
    td_metrica = S_TD_METRICA + fondo_style

    celda_sector = f'<td bgcolor="{fondo}" style="{td_plano}">{fila["sector"]}</td>' if con_sector else ""

    return f"""
    <tr>
      <td bgcolor="{fondo}" style="{td_rank}">{fila['puesto_general']}</td>
      <td bgcolor="{fondo}" style="{td_valor}">{ticker}<span style="{S_SPAN_NOMBRE}">{nombre}</span></td>
      {celda_sector}
      <td bgcolor="{fondo}" style="{td_num}">{fmt_precio(fila['precio'])}</td>
      <td bgcolor="{fondo}" style="{td_num}color:{color_num(fila['variacion_1d'])};">{fmt_pct(fila['variacion_1d'])}</td>
      <td bgcolor="{fondo}" style="{td_num}color:{C_BAJADA};">{fmt_pct(fila['vs_max_52s'])}</td>
      <td bgcolor="{fondo}" style="{td_num}color:{C_BAJADA};">{fmt_pct(fila['vs_max_9a'])}</td>
      <td bgcolor="{fondo}" style="{td_metrica}">{fila['metrica']}</td>
      <td bgcolor="{fondo}" style="{td_num}">{fmt_ratio(fila['ratio_hoy'])}</td>
      <td bgcolor="{fondo}" style="{td_num}">{fmt_ratio(fila['mediana_historica'])}</td>
      <td bgcolor="{fondo}" style="{td_num}color:{color_percentil(fila['percentil'])};font-weight:bold;">{fila['trimestres_mas_barato']}/{fila['trimestres_totales']}</td>
      <td bgcolor="{fondo}" style="{td_num}color:{color_barato(fila['cuanto_mas_barata'])};font-weight:bold;">{fmt_pct(fila['cuanto_mas_barata'])}</td>
      <td bgcolor="{fondo}" style="{td_plano}width:100px;">{''.join(alertas)}</td>
    </tr>"""


def tabla_sector(sector, filas, nombres):
    filas_html = "".join(
        fila_general(f, nombres, con_sector=False, fondo=(FILA_ALT if i % 2 else FILA_BLANCA))
        for i, f in enumerate(filas)
    )
    return f"""
  <table><tr><td bgcolor="#dbe7f2" style="{S_TITULO_SUBSECCION}">{sector}</td></tr></table>
  <table style="border-collapse:collapse;width:100%;">
    <tr>
      <th bgcolor="#f4f6f8" style="{S_TH}">Nº/75</th><th bgcolor="#f4f6f8" style="{S_TH}">Valor</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Cierre</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">1D</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">vs. máx 52s</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">vs. máx hist. (9a)</th>
      <th bgcolor="#f4f6f8" style="{S_TH}">Métrica</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Ratio hoy</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Mediana hist.</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Trim. más barato</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Cuánto más barata</th><th bgcolor="#f4f6f8" style="{S_TH}width:100px;"></th>
    </tr>
    {filas_html}
  </table>"""


def generar_html(ranking_hoy, nombres):
    filas_generales = "".join(
        fila_general(f, nombres, con_sector=True, fondo=(FILA_ALT if i % 2 else FILA_BLANCA))
        for i, f in enumerate(ranking_hoy["ranking_general"])
    )
    tablas_sector = "".join(
        tabla_sector(sector, filas, nombres)
        for sector, filas in ranking_hoy["ranking_por_sector"].items()
    )
    leyenda_sectores = ", ".join(ranking_hoy["sectores_sin_representacion"]) or "ninguno"

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"></head>
<body style="{S_BODY}">
<div style="{S_CONTENEDOR}">

  <table style="border-collapse:collapse;width:100%;">
    <tr><td bgcolor="#1a3c5e" style="{S_CABECERA_TITULO}">📊 Calidad + Barata — Monitor diario</td></tr>
    <tr><td bgcolor="#1a3c5e" style="{S_CABECERA_SUB}">{ranking_hoy['fecha']}</td></tr>
  </table>

  <table style="border-collapse:collapse;width:100%;"><tr><td bgcolor="#eef4fa" style="{S_RESUMEN_CAJA}">
    Universo de calidad con precio calculable: <b>{ranking_hoy['universo_calculable']}</b> compañías ·
    Bajo el umbral hoy: <b>{ranking_hoy['bajo_umbral']}</b>
  </td></tr></table>

  <table style="border-collapse:collapse;width:100%;"><tr><td bgcolor="#2d5f8a" style="{S_TITULO_SECCION}">🏆 Ranking general — Top 10 más baratas de las {ranking_hoy['universo_calculable']}</td></tr></table>
  <table style="border-collapse:collapse;width:100%;">
    <tr>
      <th bgcolor="#f4f6f8" style="{S_TH}">Nº/75</th><th bgcolor="#f4f6f8" style="{S_TH}">Valor</th><th bgcolor="#f4f6f8" style="{S_TH}">Sector</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Cierre</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">1D</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">vs. máx 52s</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">vs. máx hist. (9a)</th>
      <th bgcolor="#f4f6f8" style="{S_TH}">Métrica</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Ratio hoy</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Mediana hist.</th><th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Trim. más barato</th>
      <th bgcolor="#f4f6f8" style="{S_TH}text-align:right;">Cuánto más barata</th><th bgcolor="#f4f6f8" style="{S_TH}width:100px;">Alertas</th>
    </tr>
    {filas_generales}
  </table>

  <table style="border-collapse:collapse;width:100%;"><tr><td bgcolor="#2d5f8a" style="{S_TITULO_SECCION}">📂 Ranking por sector</td></tr></table>
  {tablas_sector}

  <table style="border-collapse:collapse;width:100%;"><tr><td bgcolor="#f8f8f8" style="{S_LEYENDA_CAJA}">
    <b>Sectores sin representación hoy:</b> {leyenda_sectores}.<br>
    Ninguna compañía de estos sectores supera las cuatro dimensiones del filtro de calidad.
  </td></tr></table>

  <table style="border-collapse:collapse;width:100%;"><tr><td bgcolor="#f4f4f4" style="{S_PIE}">
    Generado automáticamente · Financial Analyst · Subcartera Calidad + Barata<br>
    Este correo es independiente del correo diario/semanal de la cartera AI Infrastructure Stack
  </td></tr></table>

</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(os.path.join(args.data_dir, "ranking_hoy.json")) as f:
        ranking_hoy = json.load(f)

    nombres = {}
    ruta_sp500 = os.path.join(args.data_dir, "sp500_universo.csv")
    if os.path.exists(ruta_sp500):
        import csv
        with open(ruta_sp500) as f:
            for fila in csv.DictReader(f):
                nombres[fila["ticker"]] = fila.get("nombre", fila["ticker"])

    html = generar_html(ranking_hoy, nombres)

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "calidad_barata_daily.html"), "w") as f:
        f.write(html)

    meta = {
        "daily_subject": f"Calidad + Barata — {ranking_hoy['bajo_umbral']} candidatas bajo umbral ({ranking_hoy['fecha']})",
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Generado: calidad_barata_daily.html — {len(ranking_hoy['ranking_general'])} filas en ranking general")


if __name__ == "__main__":
    main()
