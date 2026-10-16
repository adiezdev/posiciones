"""
Ver por qué un PDF sale mal.

Hasta ahora, cuando a una partitura le faltaban números, la única forma de
averiguar por qué era abrir el PDF y mirarlo. Esto pinta encima del original
todo lo que la máquina ha creído ver:

  verde       pentagrama detectado (con su número de sistema)
  naranja     pentagramas que encuentra la detección libre cuando la
              detección estricta falla: si el número no cuadra, la página
              entera cae a modo degradado y ahí empiezan los problemas
  azul        cabezas de nota reconocidas por su glifo de texto
  rojo        número estampado
  turquesa    número reajustado a la cabeza que tenía al lado (con flecha
              desde donde lo había puesto el OMR)
  ámbar       número estampado sin cabeza que lo confirme
  gris con X  número descartado, y en qué sitio se ha perdido
  magenta     la verdad, cuando se le pasa un verdad.json

Uso:
    python depurar.py partitura.pdf export.musicxml -o diagnostico.pdf
    python depurar.py partitura.pdf export.mxl --json medidas.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import partitura
import superponer

# Colores en RGB 0..1
VERDE = (0.10, 0.60, 0.25)
NARANJA = (0.95, 0.55, 0.10)
AZUL = (0.15, 0.40, 0.90)
ROJO = (0.85, 0.12, 0.15)
TURQUESA = (0.00, 0.60, 0.62)
AMBAR = (0.85, 0.52, 0.00)
GRIS = (0.55, 0.55, 0.55)
MAGENTA = (0.80, 0.10, 0.65)

COLOR_ESTADO = {
    "puesta": ROJO,
    "ajustada": TURQUESA,
    "sin_cabeza": AMBAR,
    "cabeza_lejos": AMBAR,
    "descartada_sin_cabeza": GRIS,
    "descartada_duplicada": GRIS,
}


def analizar(pdf: Path, musicxml: Path, transpositor: bool = False,
             preferir_cercanas: bool = False, peso_movimiento=None) -> dict:
    """
    Corre el pipeline de estampado recogiendo todo lo que mide por el camino.

    Devuelve un diccionario con el plano, el diagnóstico por página y las
    cuentas. Si `preparar` se rinde, viene el motivo exacto en vez de un
    None mudo.
    """
    diag_plano: dict = {}
    plano = superponer.preparar(partitura.cargar(musicxml), transpositor,
                                preferir_cercanas, peso_movimiento,
                                pdf=pdf, diag=diag_plano)
    if plano is None:
        return {"ok": False, "motivo": diag_plano.get("motivo", "desconocido"),
                "plano": None, "paginas": [], "resumen": {}}

    paginas: List[dict] = []
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        estampadas, medido = superponer.estampar(
            pdf, Path(tmp) / "estampado.pdf", plano, diag=paginas)

    # Lo que encuentra la detección sin decirle cuántos sistemas esperar.
    # Es el dato que dice si la página se cayó a modo degradado por poco o
    # por mucho.
    for f in paginas:
        if "alto_pt" not in f:
            continue
        if len(f.get("pentagramas", [])) != f.get("sistemas_esperados"):
            libres = superponer.detectar_pentagramas(pdf, f["pagina"],
                                                     f["alto_pt"])
            f["pentagramas_libres"] = [list(x) for x in libres]

    return {"ok": True, "motivo": None, "plano": plano, "paginas": paginas,
            "resumen": _resumir(plano, paginas, estampadas, medido)}


def _resumir(plano, paginas: List[dict], estampadas: int,
             medido: bool) -> dict:
    estados: Dict[str, int] = {}
    for f in paginas:
        for n in f.get("notas", []):
            estados[n["estado"]] = estados.get(n["estado"], 0) + 1

    con_pent = sum(1 for f in paginas
                   if len(f.get("pentagramas", []))
                   == f.get("sistemas_esperados", -1)
                   and f.get("sistemas_esperados"))
    con_marcas = sum(1 for f in paginas if f.get("marcas_entrada"))

    descartadas = sum(v for k, v in estados.items() if k.startswith("descartada"))
    return {
        "notas_omr": plano.total,
        "marcas": len(plano.marcas),
        "sin_x": plano.sin_x,
        "estimadas": plano.rellenadas,
        "estampadas": estampadas,
        "descartadas": descartadas,
        "estados": estados,
        "modo_x": plano.modo_x,
        "encaje": round(plano.acierto, 4),
        "paginas": len(paginas),
        "paginas_con_marcas": con_marcas,
        "paginas_medidas": con_pent,
        "medido": medido,
        "cobertura": round(estampadas / plano.total, 4) if plano.total else 0.0,
    }


# --- comparación con la verdad ---------------------------------------------

def comparar(analisis: dict, verdad: dict) -> dict:
    """
    Cuánto se desvía cada número de la cabeza a la que debería ir.

    Solo tiene sentido con partituras sintéticas, donde sabemos la respuesta.
    Con un PDF real no hay verdad contra la que medir y lo mejor que se puede
    hacer es mirar la cobertura y el PDF de diagnóstico.
    """
    espacio = verdad.get("espacio_pt") or 1.0
    puestas: Dict[int, List[dict]] = {}
    for f in analisis["paginas"]:
        for n in f.get("notas", []):
            if n["estado"].startswith("descartada"):
                continue
            puestas.setdefault(f["pagina"], []).append(n)

    errores: List[float] = []
    huerfanas = 0
    for nota in verdad["notas"]:
        candidatas = [n for n in puestas.get(nota["pagina"], [])
                      if n["sistema"] == nota["sistema"]]
        if not candidatas:
            huerfanas += 1
            continue
        mejor = min(candidatas, key=lambda n: abs(n["x"] - nota["x_pt"]))
        errores.append(abs(mejor["x"] - nota["x_pt"]))

    errores.sort()
    def pct(q: float) -> float:
        if not errores:
            return float("nan")
        return errores[min(int(q * len(errores)), len(errores) - 1)]

    aciertos = sum(1 for e in errores if e <= 0.5 * espacio)
    return {
        "notas_verdad": len(verdad["notas"]),
        "sin_numero": huerfanas,
        "error_mediano_pt": round(pct(0.5), 3) if errores else None,
        "error_p95_pt": round(pct(0.95), 3) if errores else None,
        "error_max_pt": round(errores[-1], 3) if errores else None,
        "espacio_pt": round(espacio, 3),
        "aciertos": aciertos,
        "tasa_acierto": round(aciertos / len(verdad["notas"]), 4)
                        if verdad["notas"] else 0.0,
    }


# --- el PDF de diagnóstico --------------------------------------------------

def pintar(pdf: Path, destino: Path, analisis: dict,
           verdad: Optional[dict] = None) -> None:
    """Dibuja todo lo medido encima del PDF original."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    lector = PdfReader(str(pdf))
    escritor = PdfWriter()
    por_pagina = {f["pagina"]: f for f in analisis["paginas"]}
    verdad_pag: Dict[int, List[dict]] = {}
    for n in (verdad or {}).get("notas", []):
        verdad_pag.setdefault(n["pagina"], []).append(n)

    for indice, pagina in enumerate(lector.pages):
        ficha = por_pagina.get(indice)
        if ficha and "alto_pt" in ficha:
            caja = pagina.cropbox or pagina.mediabox
            ancho_caja, alto_caja = float(caja.width), float(caja.height)
            orig_x, orig_y = float(caja.left), float(caja.bottom)
            giro = ficha["giro"]
            alto_pt = ficha["alto_pt"]
            ancho_pt = ficha["ancho_pt"]

            buffer = BytesIO()
            lienzo = canvas.Canvas(buffer, pagesize=(float(pagina.mediabox.width),
                                                     float(pagina.mediabox.height)))

            def punto(xv: float, ya: float):
                return superponer.a_hoja(xv, ya, giro, ancho_caja, alto_caja,
                                         alto_pt, orig_x, orig_y)

            _pintar_pagina(lienzo, punto, ficha, verdad_pag.get(indice, []),
                           ancho_pt, alto_pt, indice == 0)
            lienzo.save()
            buffer.seek(0)
            pagina.merge_page(PdfReader(buffer).pages[0])
        escritor.add_page(pagina)

    with open(destino, "wb") as f:
        escritor.write(f)


def _recta(lienzo, punto, x1, y1, x2, y2) -> None:
    a, b = punto(x1, y1), punto(x2, y2)
    lienzo.line(a[0], a[1], b[0], b[1])


def _caja(lienzo, punto, x1, y1, x2, y2) -> None:
    _recta(lienzo, punto, x1, y1, x2, y1)
    _recta(lienzo, punto, x2, y1, x2, y2)
    _recta(lienzo, punto, x2, y2, x1, y2)
    _recta(lienzo, punto, x1, y2, x1, y1)


def _texto(lienzo, punto, x, y, texto, cuerpo=5.0, giro_texto=0.0) -> None:
    px, py = punto(x, y)
    lienzo.saveState()
    lienzo.translate(px, py)
    if giro_texto:
        lienzo.rotate(giro_texto)
    lienzo.setFont("Helvetica", cuerpo)
    lienzo.drawString(0, 0, texto)
    lienzo.restoreState()


def _pintar_pagina(lienzo, punto, ficha: dict, verdad: List[dict],
                   ancho_pt: float, alto_pt: float, leyenda: bool) -> None:
    espacio = ficha.get("espacio") or 5.0
    giro = ficha["giro"]
    # El texto tiene que girar con la página o sale del revés.
    giro_texto = {90: 90, 180: 180, 270: 270}.get(giro, 0)

    # --- pentagramas -------------------------------------------------------
    pents = ficha.get("pentagramas", [])
    lienzo.setStrokeColorRGB(*VERDE)
    lienzo.setLineWidth(0.4)
    for k, (sup, inf, izq, der) in enumerate(pents):
        _caja(lienzo, punto, izq, sup, der, inf)
        lienzo.setFillColorRGB(*VERDE)
        _texto(lienzo, punto, izq - 14, inf, "S%d" % k, 5.5, giro_texto)

    libres = ficha.get("pentagramas_libres")
    if libres:
        lienzo.setStrokeColorRGB(*NARANJA)
        lienzo.setDash(2, 2)
        for sup, inf, izq, der in libres:
            _caja(lienzo, punto, izq, sup, der, inf)
        lienzo.setDash()

    # --- cabezas reconocidas por glifo -------------------------------------
    cabezas = ficha.get("cabezas_texto")
    if cabezas:
        lienzo.setFillColorRGB(*AZUL)
        for hx, hy in cabezas:
            px, py = punto(hx, hy)
            lienzo.circle(px, py, 0.8, stroke=0, fill=1)

    # --- la verdad ---------------------------------------------------------
    if verdad:
        lienzo.setStrokeColorRGB(*MAGENTA)
        lienzo.setLineWidth(0.35)
        for n in verdad:
            x, y = n["x_pt"], n["y_pt"]
            _recta(lienzo, punto, x - 2.2, y, x + 2.2, y)
            _recta(lienzo, punto, x, y - 2.2, x, y + 2.2)

    # --- los números -------------------------------------------------------
    for n in ficha.get("notas", []):
        color = COLOR_ESTADO.get(n["estado"], ROJO)
        x, y = n["x"], n["y"]
        lienzo.setFillColorRGB(*color)
        lienzo.setStrokeColorRGB(*color)
        lienzo.setLineWidth(0.4)

        if n["estado"] == "ajustada":
            # De dónde venía y a dónde ha ido.
            _recta(lienzo, punto, n["x_omr"], y - 1.5, n["x"], y - 1.5)
            _recta(lienzo, punto, n["x_omr"], y - 3.0, n["x_omr"], y)

        etiqueta = n["texto"]
        _texto(lienzo, punto, x - 1.5, y + 2.0, etiqueta,
               max(4.0, 1.4 * espacio), giro_texto)

        if n["estado"].startswith("descartada"):
            _recta(lienzo, punto, x - 3, y - 3, x + 3, y + 4)
            _recta(lienzo, punto, x - 3, y + 4, x + 3, y - 3)
        if n["estimada"]:
            px, py = punto(x, y + 1.0)
            lienzo.circle(px, py, 1.3 * espacio, stroke=1, fill=0)

    # --- cabecera con las cuentas de la página -----------------------------
    from collections import Counter
    cuenta = Counter(n["estado"] for n in ficha.get("notas", []))
    resumen = "  ".join("%s=%d" % (k, v) for k, v in sorted(cuenta.items()))
    cabecera = ("pág %d | sistemas esperados %s / detectados %d | medido %s "
                "| espacio %.2fpt | giro %d | %s"
                % (ficha["pagina"], ficha.get("sistemas_esperados"),
                   len(pents), ficha.get("medido"), espacio, giro, resumen))
    lienzo.setFillColorRGB(0.1, 0.1, 0.1)
    _texto(lienzo, punto, 12, 14, cabecera, 6.0, giro_texto)

    if leyenda:
        items = [(VERDE, "pentagrama detectado"),
                 (NARANJA, "detección libre / sin cabeza"),
                 (AZUL, "cabeza por glifo"),
                 (ROJO, "número puesto"),
                 (TURQUESA, "número reajustado"),
                 (GRIS, "número descartado"),
                 (MAGENTA, "verdad")]
        y = alto_pt - 10 - 7 * 7
        for color, texto in items:
            lienzo.setFillColorRGB(*color)
            _texto(lienzo, punto, 12, y, "■ " + texto, 5.5, giro_texto)
            y += 7


# --- CLI --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("musicxml", type=Path, help=".mxl, .musicxml o .xml")
    ap.add_argument("-o", "--salida", type=Path,
                    help="PDF de diagnóstico (por defecto, junto al original)")
    ap.add_argument("--json", type=Path, dest="json_salida",
                    help="volcar las medidas en JSON")
    ap.add_argument("--verdad", type=Path,
                    help="verdad.json de pruebas/sintetico.py")
    ap.add_argument("--transpositor", action="store_true")
    args = ap.parse_args()

    analisis = analizar(args.pdf, args.musicxml, args.transpositor)
    if not analisis["ok"]:
        print("preparar() se ha rendido: %s" % analisis["motivo"])
        print("El trabajo habría caído a la vía de redibujar con MuseScore.")
        if args.json_salida:
            args.json_salida.write_text(
                json.dumps({"ok": False, "motivo": analisis["motivo"]},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        raise SystemExit(2)

    r = analisis["resumen"]
    print("notas en el MusicXML   %d" % r["notas_omr"])
    print("  de ellas estimadas   %d (leídas de la imagen, no del OMR)"
          % r["estimadas"])
    print("  sin x reconstruible  %d" % r["sin_x"])
    print("números estampados     %d  (cobertura %.1f%%)"
          % (r["estampadas"], 100 * r["cobertura"]))
    print("números descartados    %d" % r["descartadas"])
    for estado, n in sorted(r["estados"].items()):
        print("    %-24s %d" % (estado, n))
    print("coordenadas            modo %s, encaje %.0f%%"
          % (r["modo_x"], 100 * r["encaje"]))
    print("páginas                %d con números, %d con pentagramas medidos"
          % (r["paginas_con_marcas"], r["paginas_medidas"]))

    comparacion = None
    if args.verdad:
        verdad = json.loads(args.verdad.read_text(encoding="utf-8"))
        comparacion = comparar(analisis, verdad)
        print("frente a la verdad:")
        print("  error mediano        %.2f pt  (un espacio son %.2f pt)"
              % (comparacion["error_mediano_pt"], comparacion["espacio_pt"]))
        print("  error p95 / máximo   %.2f / %.2f pt"
              % (comparacion["error_p95_pt"], comparacion["error_max_pt"]))
        print("  notas bien situadas  %d de %d (%.1f%%)"
              % (comparacion["aciertos"], comparacion["notas_verdad"],
                 100 * comparacion["tasa_acierto"]))
        print("  notas sin número     %d" % comparacion["sin_numero"])

    salida = args.salida or args.pdf.with_name(args.pdf.stem + " (diagnóstico).pdf")
    verdad_datos = (json.loads(args.verdad.read_text(encoding="utf-8"))
                    if args.verdad else None)
    pintar(args.pdf, salida, analisis, verdad_datos)
    print("diagnóstico            %s" % salida)

    if args.json_salida:
        args.json_salida.write_text(json.dumps(
            {"ok": True, "resumen": r, "comparacion": comparacion,
             "paginas": analisis["paginas"]},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print("medidas                %s" % args.json_salida)


if __name__ == "__main__":
    main()
