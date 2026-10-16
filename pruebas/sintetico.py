"""
Genera partituras falsas con verdad conocida.

El problema de este proyecto es que no había forma de saber si un cambio
mejoraba o empeoraba: cada ajuste de umbral se validaba abriendo un PDF y
mirándolo. Aquí fabricamos partituras donde sabemos exactamente dónde está
cada cabeza de nota, así que se puede medir el error en puntos.

Produce tres cosas que encajan entre sí:

  partitura.pdf    lo que vería el usuario
  export.musicxml  lo que Audiveris habría exportado de ese PDF
  verdad.json      dónde está de verdad cada nota, en puntos

Y sabe estropearlas a propósito, que es lo interesante: quitar coordenadas
como hace Audiveris en pasajes densos, girar la página con /Rotate, meter
una CropBox distinta de la MediaBox... Cada defecto reproduce un fallo real
que hemos visto en PDFs de verdad.

    python pruebas/sintetico.py /tmp/caso --paginas 2 --rotate 90
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Dict, List, Tuple
from xml.etree import ElementTree as ET

# --- escala -----------------------------------------------------------------
# MusicXML mide todo en "décimos": cuatro espacios de pentagrama son 40.
MM_POR_PULGADA = 25.4
MM_PENTAGRAMA = 7.0          # alto de un pentagrama de cinco líneas
DECIMOS_PENTAGRAMA = 40.0
MM_POR_DECIMO = MM_PENTAGRAMA / DECIMOS_PENTAGRAMA
PT_POR_DECIMO = MM_POR_DECIMO * 72.0 / MM_POR_PULGADA

# --- hoja -------------------------------------------------------------------
A4_ANCHO_MM, A4_ALTO_MM = 297.0, 210.0      # apaisado, como casi toda marcha

MARGEN_IZQ = 80.0
MARGEN_DER = 80.0
MARGEN_SUP = 100.0
MARGEN_INF = 80.0
DIST_PRIMER_SISTEMA = 150.0
DIST_SISTEMA_MAX = 240.0

DIVISIONES = 4               # por negra: permite corcheas sin decimales
MEDIA_CABEZA = 6.5           # el mismo valor que usa superponer.py
ANCHO_CABEZA = 13.0
ALTO_CABEZA = 10.0
# Cuerpo de la "o" que hace de cabeza cuando se piden cabezas de texto. A 9
# puntos su caja mide 4,87 pt de alto, que es casi exactamente un espacio de
# pentagrama: dentro de lo que `_cabezas_texto` acepta como cabeza.
CUERPO_CABEZA = 9.0

PASOS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
LETRAS = "CDEFGAB"

# Qué nota cae sobre la línea de abajo de cada clave, como índice diatónico
# (octava * 7 + grado, Do = 0). Sacado del solfeo, no del código que se está
# probando: si superponer.py se equivoca al cambiar de clave, aquí salta.
#   fa en 4ª -> Sol2    sol en 2ª -> Mi4    do en 4ª -> Re3
CLAVES = {
    "fa":    {"signo": "F", "linea": 4, "base": 2 * 7 + 4},
    "sol":   {"signo": "G", "linea": 2, "base": 4 * 7 + 2},
    "tenor": {"signo": "C", "linea": 4, "base": 3 * 7 + 1},
}

# Cuántos semitonos por debajo de lo escrito suena la particella. Una parte
# de trombón en clave de sol es, en la práctica, una parte en Sib: suena una
# novena mayor más grave.
TRANSPOSICIONES = {"ninguna": 0, "sib": -14, "octava": -12}


def _mm_a_pt(mm: float) -> float:
    return mm * 72.0 / MM_POR_PULGADA


def altura_de_grado(k: int, clave: str = "fa") -> Tuple[str, int, int]:
    """
    (letra, octava, midi escrito) de un grado diatónico sobre la línea de
    abajo del pentagrama, en la clave que sea.

    Es la inversa exacta de `superponer._midi_en_clave`, así que si esa
    función se rompe, estas pruebas lo cantan.
    """
    idx = CLAVES[clave]["base"] + k
    octava, paso = divmod(idx, 7)
    letra = LETRAS[paso]
    return letra, octava, (octava + 1) * 12 + PASOS[letra]


# Una línea de trombón plausible: grados sobre la línea inferior del
# pentagrama. Ni escala cromática ni saltos imposibles, para que el solver
# de posiciones tenga algo con sentido que resolver.
MELODIA = [0, 2, 4, 2, 5, 4, 2, 0, -1, 1, 3, 1, 4, 6, 4, 3,
           7, 5, 3, 5, 8, 6, 4, 6, 2, 4, 6, 8, 7, 5, 3, 1]

# Compases de 4/4, en negras. Mezclar duraciones es lo que hace la prueba
# honesta: con todo negras, el tiempo y la distancia son proporcionales y
# cualquier estimador acierta por casualidad.
RITMOS = [
    [1, 1, 1, 1],
    [0.5, 0.5, 1, 2],
    [2, 1, 0.5, 0.5],
    [0.5, 0.5, 0.5, 0.5, 2],
    [1, 0.5, 0.5, 2],
    [2, 2],
    [4],
    [0.5] * 8,
    [1, 1, 2],
    [0.5, 0.5, 0.5, 0.5, 1, 1],
]

# Un grabador no reparte el compás en partes iguales ni proporcionalmente a
# la duración: una blanca no ocupa cuatro veces lo que una corchea. El ancho
# crece mucho más despacio que la duración, en torno a esta potencia.
POTENCIA_ESPACIADO = 0.55
SANGRIA_COMPAS = 0.08        # hueco tras la barra antes de la primera nota
OCUPACION_COMPAS = 0.84      # parte del compás que ocupan las notas

TIPOS = {0.5: "eighth", 1: "quarter", 1.5: "quarter", 2: "half",
         3: "half", 4: "whole"}


class Caso:
    """Una partitura sintética con su verdad."""

    def __init__(self, paginas: int = 2, sistemas: int = 4, compases: int = 4,
                 rotate: int = 0, margen_cropbox: float = 0.0,
                 sin_dx: float = 0.0, ritmo: bool = True,
                 borde_negro: bool = False, dpi_omr: int = 300,
                 clave: str = "fa", transposicion: str = "ninguna",
                 cabezas_texto: bool = False, inclinar: float = 0.0,
                 dpi_escaneo: int = 300, semilla: int = 7):
        self.paginas = paginas
        self.sistemas = sistemas
        self.compases = compases
        self.rotate = rotate % 360
        self.margen_cropbox = margen_cropbox
        self.sin_dx = sin_dx
        self.ritmo = ritmo
        self.borde_negro = borde_negro
        self.dpi_omr = dpi_omr
        self.clave = clave
        self.cabezas_texto = cabezas_texto
        self.inclinar = inclinar
        self.dpi_escaneo = dpi_escaneo
        self.transposicion = transposicion
        self.semitonos = TRANSPOSICIONES[transposicion]
        self.azar = random.Random(semilla)

        self.ancho_pagina = A4_ANCHO_MM / MM_POR_DECIMO
        self.alto_pagina = A4_ALTO_MM / MM_POR_DECIMO
        self.ancho_util = self.ancho_pagina - MARGEN_IZQ - MARGEN_DER
        self.ancho_compas = self.ancho_util / compases

        # La separación entre sistemas se reparte para que quepan todos en la
        # hoja. Un grabador hace justo esto: apretar los sistemas cuando hay
        # muchos. Si se fija a ojo, con seis sistemas se salen de la página y
        # luego no hay quien entienda por qué falla la detección.
        libre = (self.alto_pagina - MARGEN_SUP - DIST_PRIMER_SISTEMA
                 - MARGEN_INF - sistemas * DECIMOS_PENTAGRAMA)
        self.dist_sistema = (min(DIST_SISTEMA_MAX, libre / (sistemas - 1))
                             if sistemas > 1 else DIST_SISTEMA_MAX)
        if self.dist_sistema < DECIMOS_PENTAGRAMA:
            raise ValueError("no caben %d sistemas en la hoja" % sistemas)

        # Lo que se ve: el visor aplica /Rotate, así que el papel puede estar
        # guardado en vertical y verse apaisado.
        self.vista_ancho_pt = _mm_a_pt(A4_ANCHO_MM)
        self.vista_alto_pt = _mm_a_pt(A4_ALTO_MM)
        if self.rotate in (90, 270):
            self.hoja_ancho_pt = self.vista_alto_pt
            self.hoja_alto_pt = self.vista_ancho_pt
        else:
            self.hoja_ancho_pt = self.vista_ancho_pt
            self.hoja_alto_pt = self.vista_alto_pt

        self.notas: List[dict] = []
        self.pentagramas: List[dict] = []
        self._componer()

    # -- geometría -----------------------------------------------------------

    def y_sistema(self, indice_en_pagina: int) -> float:
        """Décimos desde el borde superior hasta la línea de arriba."""
        return (MARGEN_SUP + DIST_PRIMER_SISTEMA
                + indice_en_pagina * (DECIMOS_PENTAGRAMA + self.dist_sistema))

    def a_vista(self, x_dec: float, y_dec: float) -> Tuple[float, float]:
        """Décimos (y hacia abajo) -> puntos de la página tal como se ve."""
        return x_dec * PT_POR_DECIMO, y_dec * PT_POR_DECIMO

    def a_hoja(self, x_vista: float, y_abajo: float) -> Tuple[float, float]:
        """
        Puntos de la vista (y hacia abajo) -> puntos de la hoja (y hacia
        arriba), deshaciendo el /Rotate.

        /Rotate gira la imagen en el sentido de las agujas del reloj al
        mostrarla, así que para colocar algo en un sitio concreto de la vista
        hay que girarlo al revés antes de dibujarlo.
        """
        W, H = self.vista_ancho_pt, self.vista_alto_pt
        if self.rotate == 90:
            xs, ys = y_abajo, x_vista
        elif self.rotate == 180:
            xs, ys = W - x_vista, y_abajo
        elif self.rotate == 270:
            xs, ys = H - y_abajo, W - x_vista
        else:
            xs, ys = x_vista, H - y_abajo
        return xs + self.margen_cropbox, ys + self.margen_cropbox

    # -- contenido -----------------------------------------------------------

    def _reparto(self, duraciones: List[float]) -> List[float]:
        """
        Dónde cae cada nota dentro del compás, en fracción de su ancho.

        Reparte el espacio con el ancho de cada nota creciendo como una
        potencia de su duración, que es lo que hace cualquier grabador. Con
        esto el tiempo y la distancia dejan de ser proporcionales y el
        estimador de coordenadas tiene algo de verdad que resolver.
        """
        anchos = [d ** POTENCIA_ESPACIADO for d in duraciones]
        total = sum(anchos) or 1.0
        salida, acumulado = [], 0.0
        for a in anchos:
            salida.append(SANGRIA_COMPAS
                          + OCUPACION_COMPAS * acumulado / total)
            acumulado += a
        return salida

    def _componer(self) -> None:
        n = 0
        indice_sistema = 0
        for pagina in range(self.paginas):
            for s in range(self.sistemas):
                y_top = self.y_sistema(s)
                y_bot = y_top + DECIMOS_PENTAGRAMA
                sup_pt, _ = self.a_vista(0, y_top)
                self.pentagramas.append({
                    "pagina": pagina,
                    "sistema": indice_sistema,
                    "sup_pt": y_top * PT_POR_DECIMO,
                    "inf_pt": y_bot * PT_POR_DECIMO,
                    "izq_pt": MARGEN_IZQ * PT_POR_DECIMO,
                    "der_pt": (self.ancho_pagina - MARGEN_DER) * PT_POR_DECIMO,
                })
                for c in range(self.compases):
                    x_medida = MARGEN_IZQ + c * self.ancho_compas
                    indice_compas = (pagina * self.sistemas * self.compases
                                     + s * self.compases + c)
                    duraciones = (RITMOS[indice_compas % len(RITMOS)]
                                  if self.ritmo else [1, 1, 1, 1])
                    reparto = self._reparto(duraciones)
                    tiempo = 0.0
                    for j, dur in enumerate(duraciones):
                        dx = self.ancho_compas * reparto[j]
                        k = MELODIA[n % len(MELODIA)]
                        letra, octava, midi = altura_de_grado(k, self.clave)
                        # Media línea por grado: la cabeza se apoya en la
                        # línea o se mete en el espacio.
                        y_cab = y_bot - k * (DECIMOS_PENTAGRAMA / 8.0)
                        x_cab = x_medida + dx + MEDIA_CABEZA
                        self.notas.append({
                            "n": n,
                            "pagina": pagina,
                            "sistema": indice_sistema,
                            "compas": indice_compas,
                            "dx": dx,
                            "x_medida": x_medida,
                            "duracion": dur,
                            "tiempo": tiempo,
                            "midi": midi,
                            "midi_escrito": midi,
                            # Lo que suena de verdad, que es lo que decide
                            # la posición de la vara.
                            "midi_sonante": midi + self.semitonos,
                            "letra": letra,
                            "octava": octava,
                            "grado": k,
                            # La verdad, en puntos de la página tal como se ve.
                            "x_pt": x_cab * PT_POR_DECIMO,
                            "y_pt": y_cab * PT_POR_DECIMO,
                            # Audiveris a veces no da la coordenada.
                            "con_dx": self.azar.random() >= self.sin_dx,
                        })
                        tiempo += dur
                        n += 1
                indice_sistema += 1

    # -- salida --------------------------------------------------------------

    def escribir_pdf(self, destino: Path) -> None:
        from reportlab.pdfgen import canvas

        ancho = self.hoja_ancho_pt + 2 * self.margen_cropbox
        alto = self.hoja_alto_pt + 2 * self.margen_cropbox
        c = canvas.Canvas(str(destino), pagesize=(ancho, alto))

        def linea(x1d, y1d, x2d, y2d, grosor=0.5):
            p1 = self.a_hoja(*self.a_vista(x1d, y1d))
            p2 = self.a_hoja(*self.a_vista(x2d, y2d))
            c.setLineWidth(grosor)
            c.line(p1[0], p1[1], p2[0], p2[1])

        for pagina in range(self.paginas):
            c.setStrokeColorRGB(0, 0, 0)
            c.setFillColorRGB(0, 0, 0)

            if self.borde_negro:
                # Franja negra como la que deja un escáner con la tapa
                # abierta. Dispara el máximo de tinta de la página y los
                # umbrales relativos dejan de encontrar las líneas.
                ancho_fr = 0.06 * self.vista_ancho_pt
                esquinas = [self.a_hoja(0, 0),
                            self.a_hoja(ancho_fr, 0),
                            self.a_hoja(ancho_fr, self.vista_alto_pt),
                            self.a_hoja(0, self.vista_alto_pt)]
                q = c.beginPath()
                q.moveTo(*esquinas[0])
                for e in esquinas[1:]:
                    q.lineTo(*e)
                q.close()
                c.drawPath(q, stroke=0, fill=1)

            for s in range(self.sistemas):
                y_top = self.y_sistema(s)
                izq, der = MARGEN_IZQ, self.ancho_pagina - MARGEN_DER
                for k in range(5):
                    y = y_top + k * (DECIMOS_PENTAGRAMA / 4.0)
                    linea(izq, y, der, y, 0.5)
                # Barras de compás, incluida la del final del sistema.
                for ci in range(self.compases + 1):
                    x = izq + ci * self.ancho_compas
                    linea(x, y_top, x, y_top + DECIMOS_PENTAGRAMA, 0.7)

            for nota in [x for x in self.notas if x["pagina"] == pagina]:
                self._cabeza(c, nota)

            c.showPage()
        c.save()

        if self.rotate or self.margen_cropbox:
            self._retocar_cajas(destino)
        if self.inclinar:
            self._escanear_torcido(destino)

    def _cabeza(self, c, nota: dict) -> None:
        """Una cabeza elíptica con su plica, en coordenadas de hoja.

        Las de duración dos negras o más van huecas y la redonda no lleva
        plica, como en cualquier partitura.
        """
        cx_pt, cy_pt = nota["x_pt"], nota["y_pt"]
        rx = ANCHO_CABEZA / 2.0 * PT_POR_DECIMO
        ry = ALTO_CABEZA / 2.0 * PT_POR_DECIMO

        if self.cabezas_texto:
            # Cabezas como glifo de texto, que es como las dibuja cualquier
            # programa de edición de partituras. Es la única forma de
            # ejercitar `_cabezas_texto`, la detección que decide si un
            # número tiene una nota debajo: con cabezas vectoriales esa
            # ruta no se ejecuta nunca, y es justo la que se comía notas.
            # Helvetica no tiene glifos de música, pero a la detección le
            # da igual el carácter: mira el alto y la proporción.
            px, py = self.a_hoja(cx_pt, cy_pt)
            c.saveState()
            c.translate(px, py)
            if self.rotate:
                c.rotate(self.rotate)
            c.setFont("Helvetica", CUERPO_CABEZA)
            c.drawCentredString(0, -0.271 * CUERPO_CABEZA, "o")
            c.restoreState()
            if nota.get("duracion", 1) < 4:
                a1 = self.a_hoja(cx_pt + rx, cy_pt)
                a2 = self.a_hoja(cx_pt + rx, cy_pt
                                 - 3.0 * DECIMOS_PENTAGRAMA / 4.0 * PT_POR_DECIMO)
                c.setLineWidth(0.7)
                c.line(a1[0], a1[1], a2[0], a2[1])
            return

        # La elipse se dibuja punto a punto porque hay que girarla con la
        # página; reportlab no sabe dibujar en un sistema girado.
        import math
        puntos = []
        for i in range(24):
            a = 2 * math.pi * i / 24
            puntos.append(self.a_hoja(cx_pt + rx * math.cos(a),
                                      cy_pt + ry * math.sin(a)))
        p = c.beginPath()
        p.moveTo(*puntos[0])
        for q in puntos[1:]:
            p.lineTo(*q)
        p.close()
        hueca = nota.get("duracion", 1) >= 2
        if hueca:
            c.setLineWidth(1.0)
            c.drawPath(p, stroke=1, fill=0)
        else:
            c.drawPath(p, stroke=0, fill=1)

        if nota.get("duracion", 1) >= 4:
            return    # la redonda no lleva plica

        # Plica hacia arriba, pegada al borde derecho de la cabeza.
        a1 = self.a_hoja(cx_pt + rx, cy_pt)
        a2 = self.a_hoja(cx_pt + rx, cy_pt - 3.0 * DECIMOS_PENTAGRAMA / 4.0
                         * PT_POR_DECIMO)
        c.setLineWidth(0.7)
        c.line(a1[0], a1[1], a2[0], a2[1])

    def _retocar_cajas(self, destino: Path) -> None:
        """Mete el /Rotate y la CropBox que reportlab no sabe poner."""
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, NumberObject

        lector = PdfReader(str(destino))
        escritor = PdfWriter()
        m = self.margen_cropbox
        for pag in lector.pages:
            if self.rotate:
                pag[NameObject("/Rotate")] = NumberObject(self.rotate)
            if m:
                pag.cropbox.lower_left = (m, m)
                pag.cropbox.upper_right = (m + self.hoja_ancho_pt,
                                           m + self.hoja_alto_pt)
            escritor.add_page(pag)
        with open(destino, "wb") as f:
            escritor.write(f)

    def escribir_musicxml(self, destino: Path) -> None:
        """Un MusicXML con la pinta del que exporta Audiveris."""
        raiz = ET.Element("score-partwise", version="4.0")

        defaults = ET.SubElement(raiz, "defaults")
        esc = ET.SubElement(defaults, "scaling")
        ET.SubElement(esc, "millimeters").text = "%.4f" % MM_PENTAGRAMA
        ET.SubElement(esc, "tenths").text = "%.0f" % DECIMOS_PENTAGRAMA
        pl = ET.SubElement(defaults, "page-layout")
        ET.SubElement(pl, "page-height").text = "%.2f" % self.alto_pagina
        ET.SubElement(pl, "page-width").text = "%.2f" % self.ancho_pagina
        pm = ET.SubElement(pl, "page-margins", type="both")
        ET.SubElement(pm, "left-margin").text = "%.2f" % MARGEN_IZQ
        ET.SubElement(pm, "right-margin").text = "%.2f" % MARGEN_DER
        ET.SubElement(pm, "top-margin").text = "%.2f" % MARGEN_SUP
        ET.SubElement(pm, "bottom-margin").text = "%.2f" % MARGEN_INF
        sl = ET.SubElement(defaults, "system-layout")
        sm = ET.SubElement(sl, "system-margins")
        ET.SubElement(sm, "left-margin").text = "0"
        ET.SubElement(sm, "right-margin").text = "0"
        ET.SubElement(sl, "system-distance").text = "%.2f" % self.dist_sistema
        ET.SubElement(sl, "top-system-distance").text = \
            "%.2f" % DIST_PRIMER_SISTEMA

        lista = ET.SubElement(raiz, "part-list")
        sp = ET.SubElement(lista, "score-part", id="P1")
        ET.SubElement(sp, "part-name").text = "Trombón"

        parte = ET.SubElement(raiz, "part", id="P1")
        por_compas: Dict[int, List[dict]] = {}
        for nota in self.notas:
            por_compas.setdefault(nota["compas"], []).append(nota)

        numero = 0
        for pagina in range(self.paginas):
            for s in range(self.sistemas):
                for c in range(self.compases):
                    numero += 1
                    m = ET.SubElement(parte, "measure", number=str(numero),
                                      width="%.2f" % self.ancho_compas)
                    if c == 0:
                        imp = ET.SubElement(m, "print")
                        if s == 0 and pagina > 0:
                            imp.set("new-page", "yes")
                        elif s > 0:
                            imp.set("new-system", "yes")
                    if numero == 1:
                        at = ET.SubElement(m, "attributes")
                        ET.SubElement(at, "divisions").text = str(DIVISIONES)
                        key = ET.SubElement(at, "key")
                        ET.SubElement(key, "fifths").text = "0"
                        tim = ET.SubElement(at, "time")
                        ET.SubElement(tim, "beats").text = "4"
                        ET.SubElement(tim, "beat-type").text = "4"
                        cl = ET.SubElement(at, "clef")
                        ET.SubElement(cl, "sign").text = CLAVES[self.clave]["signo"]
                        ET.SubElement(cl, "line").text = str(
                            CLAVES[self.clave]["linea"])
                        # A propósito NO se escribe <transpose>: Audiveris
                        # tampoco lo escribe, porque no puede saberlo. Que
                        # la herramienta se las arregle con la clave.

                    for nota in por_compas[numero - 1]:
                        atrs = {}
                        if nota["con_dx"]:
                            atrs["default-x"] = "%.2f" % nota["dx"]
                        n = ET.SubElement(m, "note", **atrs)
                        p = ET.SubElement(n, "pitch")
                        ET.SubElement(p, "step").text = nota["letra"]
                        ET.SubElement(p, "octave").text = str(nota["octava"])
                        ET.SubElement(n, "duration").text = str(
                            int(round(nota["duracion"] * DIVISIONES)))
                        ET.SubElement(n, "voice").text = "1"
                        ET.SubElement(n, "type").text = TIPOS.get(
                            nota["duracion"], "quarter")

        ET.ElementTree(raiz).write(str(destino), encoding="UTF-8",
                                   xml_declaration=True)

    def _escanear_torcido(self, destino: Path) -> None:
        """
        Convierte el PDF en un escaneo torcido: lo rasteriza y lo gira.

        La verdad NO se toca: sigue siendo la de la página derecha. Eso es
        justo lo que queremos medir, que `normalizar.py` devuelva la página
        a su sitio y los números acaben donde tenían que acabar.

        El giro es alrededor del centro de la imagen, que es el mismo punto
        sobre el que gira la corrección, así que el viaje de ida y vuelta
        cierra.
        """
        import pypdfium2 as pdfium
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        doc = pdfium.PdfDocument(str(destino))
        paginas = []
        try:
            for i in range(len(doc)):
                pag = doc[i]
                imagen = pag.render(
                    scale=self.dpi_escaneo / 72.0).to_pil().convert("L")
                imagen = imagen.rotate(-self.inclinar, resample=Image.BICUBIC,
                                       expand=False, fillcolor=255)
                paginas.append((imagen, pag.get_width(), pag.get_height()))
        finally:
            doc.close()

        lienzo = None
        for imagen, ancho, alto in paginas:
            if lienzo is None:
                lienzo = canvas.Canvas(str(destino), pagesize=(ancho, alto))
            else:
                lienzo.setPageSize((ancho, alto))
            lienzo.drawImage(ImageReader(imagen), 0, 0, width=ancho, height=alto)
            lienzo.showPage()
        if lienzo is not None:
            lienzo.save()

    def escribir_omr(self, destino: Path) -> None:
        """
        Un `.omr` con la pinta del que guarda Audiveris con `-save`.

        No sustituye a probarlo contra un archivo de verdad —el esquema sale
        de leer el código de Audiveris, no de una especificación— pero sí
        ejercita todo lo demás: el ZIP, la lectura tolerante, la validación,
        el paso de píxeles a puntos y la integración con el estampado.
        """
        import zipfile

        px = self.dpi_omr / 72.0          # píxeles por punto
        interlinea = (DECIMOS_PENTAGRAMA / 4.0) * PT_POR_DECIMO * px

        por_pagina: Dict[int, List[dict]] = {}
        for n in self.notas:
            por_pagina.setdefault(n["pagina"], []).append(n)

        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
            for pagina in range(self.paginas):
                raiz = ET.Element("sheet", number=str(pagina + 1))
                esc = ET.SubElement(raiz, "scale")
                ET.SubElement(esc, "interline").text = "%.1f" % interlinea

                pag = ET.SubElement(raiz, "page")
                sistema = ET.SubElement(pag, "system")
                suyos = [p for p in self.pentagramas if p["pagina"] == pagina]
                for k, pent in enumerate(suyos):
                    st = ET.SubElement(
                        sistema, "staff",
                        id=str(pagina * self.sistemas + k + 1),
                        left="%d" % round(pent["izq_pt"] * px),
                        right="%d" % round(pent["der_pt"] * px))
                    lineas = ET.SubElement(st, "lines")
                    for j in range(5):
                        y = (pent["sup_pt"]
                             + j * (pent["inf_pt"] - pent["sup_pt"]) / 4.0)
                        li = ET.SubElement(lineas, "line")
                        pts = ET.SubElement(li, "points")
                        for x_pt in (pent["izq_pt"], pent["der_pt"]):
                            ET.SubElement(pts, "point",
                                          x="%.1f" % (x_pt * px),
                                          y="%.1f" % (y * px))

                sig = ET.SubElement(raiz, "sig")
                for n in por_pagina.get(pagina, []):
                    ancho = ANCHO_CABEZA * PT_POR_DECIMO * px
                    alto = ALTO_CABEZA * PT_POR_DECIMO * px
                    h = ET.SubElement(
                        sig, "head",
                        staff=str(n["sistema"] + 1),
                        shape="NOTEHEAD_BLACK" if n["duracion"] < 2
                              else "NOTEHEAD_VOID")
                    ET.SubElement(
                        h, "bounds",
                        x="%d" % round(n["x_pt"] * px - ancho / 2),
                        y="%d" % round(n["y_pt"] * px - alto / 2),
                        w="%d" % round(ancho), h="%d" % round(alto))

                z.writestr("sheet#%d.xml" % (pagina + 1),
                           ET.tostring(raiz, encoding="unicode"))

    def escribir_verdad(self, destino: Path) -> None:
        destino.write_text(json.dumps({
            "paginas": self.paginas,
            "sistemas_por_pagina": self.sistemas,
            "rotate": self.rotate,
            "inclinar": self.inclinar,
            "clave": self.clave,
            "transposicion": self.transposicion,
            "semitonos": self.semitonos,
            "margen_cropbox": self.margen_cropbox,
            "sin_dx": self.sin_dx,
            "pt_por_decimo": PT_POR_DECIMO,
            "espacio_pt": (DECIMOS_PENTAGRAMA / 4.0) * PT_POR_DECIMO,
            "pentagramas": self.pentagramas,
            "notas": [{k: v for k, v in n.items()} for n in self.notas],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def generar(self, carpeta: Path) -> Dict[str, Path]:
        carpeta.mkdir(parents=True, exist_ok=True)
        rutas = {
            "pdf": carpeta / "partitura.pdf",
            "musicxml": carpeta / "export.musicxml",
            "verdad": carpeta / "verdad.json",
            "omr": carpeta / "libro.omr",
        }
        self.escribir_pdf(rutas["pdf"])
        self.escribir_musicxml(rutas["musicxml"])
        self.escribir_verdad(rutas["verdad"])
        self.escribir_omr(rutas["omr"])
        return rutas


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("carpeta", type=Path, help="dónde dejar los tres archivos")
    ap.add_argument("--paginas", type=int, default=2)
    ap.add_argument("--sistemas", type=int, default=4,
                    help="pentagramas por página")
    ap.add_argument("--compases", type=int, default=4, help="por sistema")
    ap.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="girar la página con /Rotate, como muchos escáneres")
    ap.add_argument("--cropbox", type=float, default=0.0, metavar="PT",
                    help="margen entre MediaBox y CropBox, para probar PDFs recortados")
    ap.add_argument("--sin-dx", type=float, default=0.0, metavar="FRAC",
                    help="fracción de notas sin default-x, como en pasajes densos")
    ap.add_argument("--sin-ritmo", action="store_true",
                    help="todo negras y repartido por igual (prueba fácil)")
    ap.add_argument("--borde-negro", action="store_true",
                    help="franja negra de escáner que rompe la detección por umbral")
    ap.add_argument("--inclinar", type=float, default=0.0, metavar="GRADOS",
                    help="rasterizar y torcer la pagina, como una fotocopia")
    ap.add_argument("--cabezas-texto", action="store_true",
                    help="dibujar las cabezas como glifo de texto, no como vector")
    ap.add_argument("--clave", default="fa", choices=sorted(CLAVES),
                    help="clave de la particella")
    ap.add_argument("--transposicion", default="ninguna",
                    choices=sorted(TRANSPOSICIONES),
                    help="cuánto más grave suena de lo que se escribe")
    args = ap.parse_args()

    caso = Caso(paginas=args.paginas, sistemas=args.sistemas,
                compases=args.compases, rotate=args.rotate,
                margen_cropbox=args.cropbox, sin_dx=args.sin_dx,
                ritmo=not args.sin_ritmo, borde_negro=args.borde_negro,
                clave=args.clave, transposicion=args.transposicion,
                cabezas_texto=args.cabezas_texto, inclinar=args.inclinar)
    rutas = caso.generar(args.carpeta)
    print("%d notas, %d pentagramas, %d páginas"
          % (len(caso.notas), len(caso.pentagramas), caso.paginas))
    for k, v in rutas.items():
        print("  %-9s %s" % (k, v))


if __name__ == "__main__":
    main()
