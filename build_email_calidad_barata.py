"""
Build Email Calidad + Barata
================================
Tercer script del pipeline. Genera el HTML del correo diario a partir de
ranking_hoy.json, siguiendo exactamente la estructura y estilo aprobados
en el mockup de sesión (tablas con celdas de fondo coloreado, mismo
lenguaje visual que build_emails_prod.py de la cartera IA).

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


def clase_num(valor):
    if valor is None:
        return ""
    return "subida" if valor >= 0 else "bajada"


def clase_barato(valor):
    if valor is None:
        return ""
    return "barato" if valor >= 0 else "caro"


def fila_general(fila, nombres):
    ticker = fila["ticker"]
    nombre = nombres.get(ticker, ticker)
    alertas = []
    if fila.get("nuevo_hoy"):
        alertas.append('<span class="pill pill-nuevo">NUEVO HOY</span>')
    # "deterioro doble" y "solapa cartera IA" se calculan aparte (ver
    # dimensiones_extendidas / SUBINDUSTRIAS_CARTERA_IA) y se incorporan
    # aquí si ranking_hoy.json los trae marcados — pendiente de que
    # calidad_universo.py también persista esas dos banderas por ticker.
    if fila.get("deterioro_doble"):
        alertas.append('<span class="pill pill-deterioro">deterioro doble</span>')
    if fila.get("solapa_cartera_ia"):
        alertas.append('<span class="pill pill-solapa">solapa IA</span>')

    return f"""
    <tr>
      <td class="rank">{fila['puesto_general']}</td>
      <td class="valor">{ticker}<span class="nombre">{nombre}</span></td>
      <td>{fila['sector']}</td>
      <td class="num">{fmt_precio(fila['precio'])}</td>
      <td class="num {clase_num(fila['variacion_1d'])}">{fmt_pct(fila['variacion_1d'])}</td>
      <td class="num bajada">{fmt_pct(fila['vs_max_52s'])}</td>
      <td class="num bajada">{fmt_pct(fila['vs_max_9a'])}</td>
      <td class="metrica">{fila['metrica']}</td>
      <td class="num">{fmt_ratio(fila['ratio_hoy'])}</td>
      <td class="num">{fmt_ratio(fila['mediana_historica'])}</td>
      <td class="num {'barato' if fila['percentil'] <= 20 else 'medio' if fila['percentil'] <= 60 else 'caro'}">
        {fila['trimestres_mas_barato']}/{fila['trimestres_totales']}</td>
      <td class="num {clase_barato(fila['cuanto_mas_barata'])}">{fmt_pct(fila['cuanto_mas_barata'])}</td>
      <td class="alertas">{''.join(alertas)}</td>
    </tr>"""


def tabla_sector(sector, filas, nombres):
    filas_html = "".join(
        fila_general(f, nombres).replace(
            f"<td>{f['sector']}</td>\n      ", ""  # sin columna Sector en tablas por sector
        )
        for f in filas
    )
    return f"""
  <table><tr><td class="titulo-subseccion">{sector}</td></tr></table>
  <table>
    <tr>
      <th class="col">Nº/75</th><th class="col">Valor</th>
      <th class="col num">Cierre</th><th class="col num">1D</th>
      <th class="col num">vs. máx 52s</th><th class="col num">vs. máx hist. (9a)</th>
      <th class="col">Métrica</th><th class="col num">Ratio hoy</th>
      <th class="col num">Mediana hist.</th><th class="col num">Trim. más barato</th>
      <th class="col num">Cuánto más barata</th><th class="col col-alertas"></th>
    </tr>
    {filas_html}
  </table>"""


def generar_html(ranking_hoy, nombres):
    filas_generales = "".join(fila_general(f, nombres) for f in ranking_hoy["ranking_general"])
    tablas_sector = "".join(
        tabla_sector(sector, filas, nombres)
        for sector, filas in ranking_hoy["ranking_por_sector"].items()
    )
    leyenda_sectores = ", ".join(ranking_hoy["sectores_sin_representacion"]) or "ninguno"

    # CSS y estructura idénticos al mockup aprobado en sesión — no se
    # reinterpreta el diseño aquí, solo se rellena con datos reales.
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; background:#f2f2f2; margin:0; padding:24px; color:#222; }}
  .contenedor {{ max-width: 960px; margin: 0 auto; background:#ffffff; border:1px solid #ddd; }}
  table {{ border-collapse: collapse; width:100%; }}
  td, th {{ padding: 6px 6px; text-align:left; font-size:11px; word-wrap:break-word; }}
  .cabecera-titulo {{ background:#1a3c5e; color:#ffffff; font-size:18px; font-weight:bold; padding:16px; }}
  .cabecera-sub {{ background:#1a3c5e; color:#cdddec; font-size:12px; padding:0 16px 14px 16px; }}
  .resumen-caja {{ background:#eef4fa; padding:14px 16px; font-size:13px; line-height:1.5; }}
  .titulo-seccion {{ background:#2d5f8a; color:#ffffff; font-size:14px; font-weight:bold; padding:10px 12px; }}
  .titulo-subseccion {{ background:#dbe7f2; color:#1a3c5e; font-size:12.5px; font-weight:bold; padding:7px 12px; }}
  th.col {{ background:#f4f6f8; color:#555; font-size:9px; text-transform:uppercase; border-bottom:2px solid #ddd; }}
  td.rank {{ color:#999; text-align:right; }}
  td.valor {{ font-weight:bold; color:#1a3c5e; }}
  td.nombre {{ font-weight:normal; color:#888; font-size:9.5px; display:block; }}
  td.num {{ text-align:right; }}
  td.metrica {{ text-align:center; color:#666; font-size:10px; }}
  .fila-alt {{ background:#fafbfc; }}
  .pill {{ display:inline-block; font-size:8.5px; font-weight:bold; padding:2px 5px; border-radius:8px; margin:1px 0; }}
  .pill-nuevo {{ background:#fde8cc; color:#a15c00; }}
  .pill-solapa {{ background:#f6dede; color:#a3282e; }}
  .pill-deterioro {{ background:#eee; color:#666; }}
  .barato {{ color:#1e7d3a; font-weight:bold; }}
  .medio {{ color:#a15c00; }}
  .caro {{ color:#a3282e; font-weight:bold; }}
  .subida {{ color:#1e7d3a; }}
  .bajada {{ color:#a3282e; }}
  .nota {{ font-size:11px; color:#888; padding:12px 16px; line-height:1.5; }}
  .leyenda-caja {{ background:#f8f8f8; padding:12px 16px; font-size:11.5px; line-height:1.6; border-top:1px solid #eee; }}
  .pie {{ background:#f4f4f4; color:#999; font-size:10.5px; padding:12px 16px; text-align:center; }}
</style></head><body><div class="contenedor">

  <table><tr><td class="cabecera-titulo">📊 Calidad + Barata — Monitor diario</td></tr>
  <tr><td class="cabecera-sub">{ranking_hoy['fecha']}</td></tr></table>

  <table><tr><td class="resumen-caja">
    Universo de calidad con precio calculable: <b>{ranking_hoy['universo_calculable']}</b> compañías ·
    Bajo el umbral hoy: <b>{ranking_hoy['bajo_umbral']}</b>
  </td></tr></table>

  <table><tr><td class="titulo-seccion">🏆 Ranking general — Top 10 más baratas de las {ranking_hoy['universo_calculable']}</td></tr></table>
  <table>
    <tr>
      <th class="col">Nº/75</th><th class="col">Valor</th><th class="col">Sector</th>
      <th class="col num">Cierre</th><th class="col num">1D</th>
      <th class="col num">vs. máx 52s</th><th class="col num">vs. máx hist. (9a)</th>
      <th class="col">Métrica</th><th class="col num">Ratio hoy</th>
      <th class="col num">Mediana hist.</th><th class="col num">Trim. más barato</th>
      <th class="col num">Cuánto más barata</th><th class="col col-alertas">Alertas</th>
    </tr>
    {filas_generales}
  </table>

  <table><tr><td class="titulo-seccion">📂 Ranking por sector</td></tr></table>
  {tablas_sector}

  <table><tr><td class="leyenda-caja">
    <b>Sectores sin representación hoy:</b> {leyenda_sectores}.<br>
    Ninguna compañía de estos sectores supera las cuatro dimensiones del filtro de calidad.
  </td></tr></table>

  <table><tr><td class="pie">
    Generado automáticamente · Financial Analyst · Subcartera Calidad + Barata<br>
    Este correo es independiente del correo diario/semanal de la cartera AI Infrastructure Stack
  </td></tr></table>

</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(os.path.join(args.data_dir, "ranking_hoy.json")) as f:
        ranking_hoy = json.load(f)

    # Nombres de compañía: opcional, desde sp500_universo.csv si está
    # disponible en data-dir; si no, se usa el ticker tal cual.
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
