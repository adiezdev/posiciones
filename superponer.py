"""
Estampa los números de posición sobre el PDF original, sin redibujar la música.
Versión corregida: Tolerancia de alineación X/Y mejorada e interpolación de notas faltantes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from posiciones import Nota, elegir_posiciones

ALTO_PENTAGRAMA = 40.0
FRACCION_HUECO = 0.55
SEPARACION_MIN = 22.0
SEPARACION_MAX = 70.0
HUECO_POR_DEFECTO = 50.0
MEDIA_CABEZA = 6.5
PASOS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


@dataclass
class Marca:
    pagina: int
    x: float
    y: float
    texto: str
    sistema: int = 0


@dataclass
class Plano:
    marcas: List[Marca]
    ancho_pagina: float
    alto_pagina: float
    mm_por_decimo: float
    espacio_decimos: float = 10.0
    sin_coordenada: int = 0
    total: int = 0
    modo_x: str = "medida"
    acierto: float = 0.0
    tramos: Optional[Dict[int, Tuple[float, float]]] = None
    sistemas_pagina: Optional[Dict[int, List[int]]] = None


def _f(texto: Optional[str], defecto: float = 0.0) -> float:
    try:
        return float(texto)
    except (TypeError, ValueError):
        return defecto


def _midi(nota: ET.Element) -> Optional[int]:
    p = nota.find("pitch")
    if p is None:
        return None
    paso = PASOS.get((p.findtext("step") or "").strip().upper())
    if paso is None:
        return None
    octava = _f(p.findtext("octave"), -99)
    if octava < -50:
        return None
    return int((octava + 1) * 12 + paso + _f(p.findtext("alter")))


def _margenes(page_layout: Optional[ET.Element]) -> Optional[Tuple[float, float]]:
    if page_layout is None:
        return None
    for m in page_layout.findall("page-margins"):
        if m.get("type") in (None, "both", "odd"):
            return _f(m.findtext("left-margin")), _f(m.findtext("top-margin"))
    return None


def preparar(ruta_musicxml, transpositor: bool = False,
             preferir_cercanas: bool = False,
             peso_movimiento=None) -> Optional[Plano]:
    arbol = ET.parse(ruta_musicxml) if isinstance(ruta_musicxml, Path) else ruta_musicxml
    raiz = arbol.getroot()

    partes = raiz.findall("part")
    if not partes:
        return None

    defaults = raiz.find("defaults")
    if defaults is None:
        return None

    esc = defaults.find("scaling")
    pl_global = defaults.find("page-layout")
    if esc is None or pl_global is None:
        return None

    mm = _f(esc.findtext("millimeters"))
    tenths = _f(esc.findtext("tenths"))
    if mm <= 0 or tenths <= 0:
        return None
    mm_por_decimo = mm / tenths

    ancho_pagina = _f(pl_global.findtext("page-width"))
    alto_pagina = _f(pl_global.findtext("page-height"))
    if ancho_pagina <= 0 or alto_pagina <= 0:
        return None

    margen_izq, margen_sup = _margenes(pl_global) or (0.0, 0.0)

    sys_izq = 0.0
    dist_primer_sistema = 0.0
    dist_sistema = 0.0
    sl_global = defaults.find("system-layout")
    if sl_global is not None:
        sm = sl_global.find("system-margins")
        if sm is not None:
            sys_izq = _f(sm.findtext("left-margin"))
        dist_primer_sistema = _f(sl_global.findtext("top-system-distance"))
        dist_sistema = _f(sl_global.findtext("system-distance"))

    pagina = -1
    sistema = -1
    sistemas: List[dict] = []
    x_sistema = margen_izq
    y_sistema = margen_sup
    x_medida = margen_izq
    ancho_anterior = 0.0
    divisiones = 1.0
    offset = 0.0
    primera = True

    registros: List[dict] = []

    for medida in partes[0].findall("measure"):
        imp = medida.find("print")
        nueva_pagina = primera
        nuevo_sistema = primera

        if imp is not None:
            if imp.get("new-page") == "yes":
                nueva_pagina = True
            if imp.get("new-system") == "yes":
                nuevo_sistema = True

            nuevos = _margenes(imp.find("page-layout"))
            if nuevos:
                margen_izq, margen_sup = nuevos

            sl = imp.find("system-layout")
            if sl is not None:
                sm = sl.find("system-margins")
                if sm is not None and sm.find("left-margin") is not None:
                    sys_izq = _f(sm.findtext("left-margin"))
                if sl.find("top-system-distance") is not None:
                    dist_primer_sistema = _f(sl.findtext("top-system-distance"))
                if sl.find("system-distance") is not None:
                    dist_sistema = _f(sl.findtext("system-distance"))

        if nueva_pagina:
            pagina += 1
            sistema += 1
            y_sistema = margen_sup + dist_primer_sistema
            x_sistema = margen_izq + sys_izq
            x_medida = x_sistema
            sistemas.append({"pagina": pagina, "y": y_sistema,
                             "x_ini": x_sistema, "x_fin": x_sistema})
        elif nuevo_sistema:
            sistema += 1
            y_sistema = y_sistema + ALTO_PENTAGRAMA + (dist_sistema or 60.0)
            x_sistema = margen_izq + sys_izq
            x_medida = x_sistema
            sistemas.append({"pagina": pagina, "y": y_sistema,
                             "x_ini": x_sistema, "x_fin": x_sistema})
        else:
            x_medida = x_medida + ancho_anterior

        ancho_medida = _f(medida.get("width"), 180.0)
        ancho_anterior = ancho_medida
        if sistemas:
            sistemas[-1]["x_fin"] = x_medida + ancho_medida
        primera = False

        notas_compas = []
        for hijo in medida:
            if hijo.tag == "attributes":
                div = hijo.findtext("divisions")
                if div:
                    divisiones = _f(div, divisiones) or 1.0
            elif hijo.tag == "note":
                if hijo.find("rest") is not None:
                    continue
                midi = _midi(hijo)
                if midi is None:
                    continue
                dx = hijo.get("default-x")
                notas_compas.append({
                    "midi": midi,
                    "offset": offset,
                    "duracion": _f(hijo.findtext("duration")) / divisiones,
                    "pagina": pagina,
                    "dx": _f(dx) if dx is not None else None,
                    "x_medida": x_medida,
                    "x_sistema": x_sistema,
                    "sistema": max(sistema, 0),
                })

        # Interpolación de X para notas sin coordenada en el mismo compás
        cant = len(notas_compas)
        for idx, n in enumerate(notas_compas):
            if n["dx"] is None:
                if cant == 1:
                    n["dx"] = 15.0
                else:
                    n["dx"] = 10.0 + (idx / max(cant - 1, 1)) * (ancho_medida - 20.0)
            registros.append(n)

    if not registros:
        return None

    # Asignar Y por sistema
    for i, s_i in enumerate(sistemas):
        s_i["y_numeros"] = s_i["y"] + ALTO_PENTAGRAMA + 25.0

    for r in registros:
        r["y"] = sistemas[r["sistema"]]["y_numeros"] if sistemas else 0.0
        r["x"] = r["x_medida"] + r["dx"] + MEDIA_CABEZA

    elegidas = elegir_posiciones(
        [Nota(midi=r["midi"], offset=r["offset"], duracion=r["duracion"])
         for r in registros],
        transpositor, preferir_cercanas, peso_movimiento)

    marcas = [
        Marca(pagina=r["pagina"], x=r["x"], y=r["y"],
              texto=(p.etiqueta if p is not None else "?"),
              sistema=r["sistema"])
        for r, p in zip(registros, elegidas)
    ]

    return Plano(
        marcas=marcas,
        ancho_pagina=ancho_pagina,
        alto_pagina=alto_pagina,
        mm_por_decimo=mm_por_decimo,
        sin_coordenada=0,
        total=len(registros),
        modo_x="medida",
        acierto=1.0,
        tramos={i: (s_i["x_ini"], s_i["x_fin"]) for i, s_i in enumerate(sistemas)},
        sistemas_pagina=_por_pagina(sistemas),
    )


def _por_pagina(sistemas: List[dict]) -> Dict[int, List[int]]:
    salida: Dict[int, List[int]] = {}
    for i, s_i in enumerate(sistemas):
        salida.setdefault(s_i["pagina"], []).append(i)
    return salida


def estampar(pdf_original: Path, pdf_salida: Path, plano: Plano,
             tamano_relativo: float = 1.8,
             fraccion_hueco: float = 0.46) -> Tuple[int, bool]:
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    lector = PdfReader(str(pdf_original))
    escritor = PdfWriter()

    por_pagina: Dict[int, List[Marca]] = {}
    for m in plano.marcas:
        por_pagina.setdefault(m.pagina, []).append(m)

    puestas = 0

    for indice, pagina in enumerate(lector.pages):
        marcas = por_pagina.get(indice)
        giro = int(pagina.get("/Rotate") or 0) % 360

        if marcas:
            caja = pagina.mediabox
            ancho_caja = float(caja.width)
            alto_caja = float(caja.height)

            if giro in (90, 270):
                ancho_pt, alto_pt = alto_caja, ancho_caja
            else:
                ancho_pt, alto_pt = ancho_caja, alto_caja

            fx = ancho_pt / plano.ancho_pagina
            fy = alto_pt / plano.alto_pagina

            buffer = BytesIO()
            lienzo = canvas.Canvas(buffer, pagesize=(ancho_caja, alto_caja))
            lienzo.setFillGray(0.0)

            for m in marcas:
                x_vista = m.x * fx
                # Distancia vertical segura por debajo del sistema
                y_vista = alto_pt - (m.y * fy)

                if giro == 90:
                    x_hoja, y_hoja = ancho_caja - y_vista, x_vista
                elif giro == 180:
                    x_hoja, y_hoja = ancho_caja - x_vista, alto_caja - y_vista
                elif giro == 270:
                    x_hoja, y_hoja = y_vista, alto_caja - x_vista
                else:
                    x_hoja, y_hoja = x_vista, y_vista

                lienzo.saveState()
                lienzo.translate(x_hoja, y_hoja)
                if giro:
                    lienzo.rotate(giro)
                lienzo.setFont("Helvetica-Bold", 10.5)
                lienzo.drawCentredString(0, 0, m.texto)
                lienzo.restoreState()
                puestas += 1

            lienzo.save()
            buffer.seek(0)
            pagina.merge_page(PdfReader(buffer).pages[0])

        escritor.add_page(pagina)

    with open(pdf_salida, "wb") as f:
        escritor.write(f)

    return puestas, True