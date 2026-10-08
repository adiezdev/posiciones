"""
Conserva del original lo que music21 se deja por el camino: el título de la
partitura y el tamaño y la orientación de la página.

music21 reescribe el MusicXML desde cero y en el `<defaults>` solo deja la
escala, así que el papel vuelve a ser vertical por defecto. Aquí leemos el
`<defaults>` que Audiveris había detectado y lo devolvemos al archivo final.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

DOCTYPE = (
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN"'
    ' "http://www.musicxml.org/dtds/partwise.dtd">'
)

# Textos que no son un título de verdad: nombres de archivo, numeración suelta.
_ARCHIVO = re.compile(r"\.(xml|mxl|musicxml|pdf|png|jpe?g|tiff?)\s*$", re.IGNORECASE)
_SOLO_SIGNOS = re.compile(r"^[\W\d_]+$", re.UNICODE)


# --- lectura ----------------------------------------------------------------

def _ruta_interna(z: zipfile.ZipFile) -> str:
    """Un .mxl es un zip; container.xml dice cuál es el archivo bueno."""
    try:
        cont = ET.fromstring(z.read("META-INF/container.xml"))
        rf = cont.find(".//rootfile")
        if rf is not None and rf.get("full-path"):
            return rf.get("full-path")
    except Exception:  # noqa: BLE001
        pass
    for n in z.namelist():
        if n.lower().endswith(".xml") and not n.startswith("META-INF"):
            return n
    raise ValueError("El archivo comprimido no contiene ningún MusicXML")


def cargar(ruta: Path) -> ET.ElementTree:
    if ruta.suffix.lower() == ".mxl":
        with zipfile.ZipFile(ruta) as z:
            return ET.ElementTree(ET.fromstring(z.read(_ruta_interna(z))))
    return ET.parse(ruta)


def _util(texto: Optional[str]) -> bool:
    if not texto:
        return False
    t = texto.strip()
    return bool(t) and len(t) <= 120 and not _ARCHIVO.search(t) and not _SOLO_SIGNOS.match(t)


def detectar_titulo(arbol: ET.ElementTree,
                    ignorar: Optional[str] = None) -> Optional[str]:
    """
    Busca el título de la partitura.

    Prioridad: el crédito marcado como título, luego el texto con la letra más
    grande de la primera página, y solo al final los campos declarados, porque
    Audiveris los rellena con el nombre del archivo.
    """
    r = arbol.getroot()
    prohibido = (ignorar or "").strip().lower()

    def sirve(t: Optional[str]) -> bool:
        return _util(t) and t.strip().lower() != prohibido

    grande, grande_tam = None, -1.0
    for c in r.findall("./credit"):
        if c.get("page") not in (None, "1"):
            continue
        tipo = (c.findtext("credit-type") or "").strip().lower()
        for w in c.findall("credit-words"):
            if not sirve(w.text):
                continue
            if tipo == "title":
                return w.text.strip()
            try:
                tam = float(w.get("font-size") or 0)
            except ValueError:
                tam = 0.0
            if tam > grande_tam:
                grande, grande_tam = w.text.strip(), tam

    if grande:
        return grande

    for ruta in ("./work/work-title", "./movement-title"):
        e = r.find(ruta)
        if e is not None and sirve(e.text):
            return e.text.strip()

    return None


def extraer_defaults(arbol: ET.ElementTree) -> Optional[ET.Element]:
    return arbol.getroot().find("./defaults")


def es_apaisado(defaults: Optional[ET.Element]) -> Optional[bool]:
    """True si el papel es más ancho que alto. None si no se sabe."""
    if defaults is None:
        return None
    pl = defaults.find("page-layout")
    if pl is None:
        return None
    try:
        ancho = float(pl.findtext("page-width"))
        alto = float(pl.findtext("page-height"))
    except (TypeError, ValueError):
        return None
    return ancho > alto


def forzar_orientacion(defaults: Optional[ET.Element], apaisado: bool) -> None:
    """Gira el papel si hace falta, intercambiando ancho y alto."""
    if defaults is None:
        return
    pl = defaults.find("page-layout")
    if pl is None:
        return
    ew, eh = pl.find("page-width"), pl.find("page-height")
    if ew is None or eh is None:
        return
    try:
        ancho, alto = float(ew.text), float(eh.text)
    except (TypeError, ValueError):
        return
    if apaisado != (ancho > alto):
        ew.text, eh.text = eh.text, ew.text
        # Los márgenes izquierdo/derecho y superior/inferior también giran.
        for m in pl.findall("page-margins"):
            izq, der = m.find("left-margin"), m.find("right-margin")
            arr, aba = m.find("top-margin"), m.find("bottom-margin")
            if None not in (izq, der, arr, aba):
                izq.text, arr.text = arr.text, izq.text
                der.text, aba.text = aba.text, der.text


def dimensiones_mm(defaults: Optional[ET.Element]):
    """Ancho y alto del papel en milímetros, o None si no se sabe."""
    if defaults is None:
        return None
    pl = defaults.find("page-layout")
    esc = defaults.find("scaling")
    if pl is None or esc is None:
        return None
    try:
        mm = float(esc.findtext("millimeters"))
        tenths = float(esc.findtext("tenths"))
        ancho = float(pl.findtext("page-width"))
        alto = float(pl.findtext("page-height"))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if tenths <= 0:
        return None
    factor = mm / tenths
    return ancho * factor, alto * factor


def spatium_mm(defaults: Optional[ET.Element]) -> Optional[float]:
    """
    Altura de un espacio del pentagrama en milímetros. MuseScore lo llama
    spatium y con él calcula el tamaño de todo lo demás.
    """
    if defaults is None:
        return None
    esc = defaults.find("scaling")
    if esc is None:
        return None
    try:
        mm = float(esc.findtext("millimeters"))
        tenths = float(esc.findtext("tenths"))
    except (TypeError, ValueError):
        return None
    if tenths <= 0:
        return None
    valor = mm / tenths * 10.0
    return valor if 0.8 <= valor <= 3.5 else None


def margenes_mm(defaults: Optional[ET.Element]) -> float:
    """Margen aproximado en milímetros. 10 mm si no viene declarado."""
    if defaults is None:
        return 10.0
    pl = defaults.find("page-layout")
    esc = defaults.find("scaling")
    if pl is None or esc is None:
        return 10.0
    m = pl.find("page-margins")
    if m is None:
        return 10.0
    try:
        factor = float(esc.findtext("millimeters")) / float(esc.findtext("tenths"))
        valores = [float(m.findtext(k)) for k in
                   ("left-margin", "right-margin", "top-margin", "bottom-margin")]
    except (TypeError, ValueError, ZeroDivisionError):
        return 10.0
    media = sum(valores) / len(valores) * factor
    return media if 3.0 <= media <= 40.0 else 10.0


# --- construcción de la página ---

# Con esta escala, un décimo de MusicXML son 0,175 mm.
MM_ESCALA, TENTHS_ESCALA = 7.0, 40.0
PAPELES_MM = {"a4": (210.0, 297.0), "carta": (215.9, 279.4), "a3": (297.0, 420.0)}
MARGEN_MM = 12.0


def _tenths(mm: float) -> str:
    return f"{mm / (MM_ESCALA / TENTHS_ESCALA):.0f}"


def medidas_papel(apaisado: bool = False, papel: str = "a4"):
    """Ancho y alto del papel en milímetros. Fuente única para todo."""
    ancho_mm, alto_mm = PAPELES_MM.get(papel, PAPELES_MM["a4"])
    if apaisado:
        ancho_mm, alto_mm = alto_mm, ancho_mm
    return ancho_mm, alto_mm


def defaults_estandar(apaisado: bool = False, papel: str = "a4") -> ET.Element:
    """
    Construye un bloque <defaults> con un papel estándar.

    El tamaño de página tiene que ir aquí, dentro del MusicXML: MuseScore lo
    lee al importar y pisa cualquier cosa que diga la hoja de estilo.
    """
    ancho_mm, alto_mm = medidas_papel(apaisado, papel)

    defaults = ET.Element("defaults")

    esc = ET.SubElement(defaults, "scaling")
    ET.SubElement(esc, "millimeters").text = f"{MM_ESCALA:g}"
    ET.SubElement(esc, "tenths").text = f"{TENTHS_ESCALA:g}"

    pl = ET.SubElement(defaults, "page-layout")
    ET.SubElement(pl, "page-height").text = _tenths(alto_mm)
    ET.SubElement(pl, "page-width").text = _tenths(ancho_mm)
    margen = ET.SubElement(pl, "page-margins")
    margen.set("type", "both")
    for lado in ("left-margin", "right-margin", "top-margin", "bottom-margin"):
        ET.SubElement(margen, lado).text = _tenths(MARGEN_MM)

    return defaults


# --- escritura --------------------------------------------------------------

def _limpiar_identificacion(raiz: ET.Element) -> None:
    """Quita el 'Music21' que se autoasigna como compositor."""
    ident = raiz.find("./identification")
    if ident is None:
        return
    for c in list(ident.findall("creator")):
        if (c.text or "").strip().lower().startswith("music21"):
            ident.remove(c)


def _poner_titulo(raiz: ET.Element, titulo: str) -> None:
    work = raiz.find("./work")
    if work is None:
        work = ET.Element("work")
        raiz.insert(0, work)
    wt = work.find("work-title")
    if wt is None:
        wt = ET.SubElement(work, "work-title")
    wt.text = titulo

    mt = raiz.find("./movement-title")
    if mt is None:
        # Va justo después de work / movement-number.
        pos = list(raiz).index(work) + 1
        mt = ET.Element("movement-title")
        raiz.insert(pos, mt)
    mt.text = titulo


def _poner_etiqueta(raiz: ET.Element, texto: str) -> None:
    """
    Texto de la esquina superior izquierda. MuseScore coloca ahí el campo de
    letrista, igual que pone el compositor arriba a la derecha.
    """
    ident = raiz.find("./identification")
    if ident is None:
        ident = ET.Element("identification")
        # Va después de work / movement-title y antes de defaults.
        destino = len(list(raiz))
        for i, h in enumerate(raiz):
            if h.tag in ("defaults", "credit", "part-list", "part"):
                destino = i
                break
        raiz.insert(destino, ident)

    for c in ident.findall("creator"):
        if c.get("type") == "lyricist":
            c.text = texto
            return

    creator = ET.Element("creator")
    creator.set("type", "lyricist")
    creator.text = texto
    # Los creator van los primeros dentro de identification.
    ident.insert(0, creator)


def rematar(ruta: Path, defaults: Optional[ET.Element],
            titulo: Optional[str], etiqueta: Optional[str] = None) -> None:
    """
    Reescribe el MusicXML de salida devolviéndole el tamaño de página original
    y el título correcto.
    """
    arbol = ET.parse(ruta)
    raiz = arbol.getroot()

    _limpiar_identificacion(raiz)

    if titulo:
        _poner_titulo(raiz, titulo)

    if etiqueta:
        _poner_etiqueta(raiz, etiqueta)

    if defaults is not None:
        for viejo in raiz.findall("defaults"):
            raiz.remove(viejo)
        # El orden del MusicXML manda: defaults va antes de credit y part-list.
        hijos = list(raiz)
        destino = len(hijos)
        for i, h in enumerate(hijos):
            if h.tag in ("credit", "part-list", "part"):
                destino = i
                break
        raiz.insert(destino, defaults)

    cuerpo = ET.tostring(raiz, encoding="unicode")
    ruta.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n{DOCTYPE}\n{cuerpo}\n',
        encoding="utf-8",
    )
