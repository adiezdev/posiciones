"""
Lee la geometría que Audiveris ya conoce, en vez de reconstruirla.

Audiveris no solo exporta MusicXML: si se le pasa `-save` deja también un
`.omr`, que es un ZIP con un `sheet#N.xml` por hoja. Dentro está lo que de
verdad ha visto en la imagen: cada pentagrama con sus líneas y sus extremos,
y cada cabeza de nota con su caja, todo en píxeles de la hoja escaneada.

Eso es exactamente lo que `superponer.py` se pasa mil líneas intentando
adivinar: dónde empieza y acaba cada pentagrama, y dónde cae cada cabeza.
Con el `.omr` no hay que adivinar nada, y sobre todo desaparece el peor
acantilado que tiene hoy el estampado: cuando `detectar_pentagramas` no
acierta el número exacto de sistemas de una página, esa página entera cae a
la escala que declara Audiveris, que se desvía hasta un 10 %.

AVISO
-----
El esquema del `.omr` no está documentado como formato público: sale de las
anotaciones JAXB del código de Audiveris y puede cambiar entre versiones.
Por eso este módulo:

  - lee de forma tolerante, sin exigir una estructura exacta
  - valida lo que ha leído antes de dejar que nadie lo use
  - devuelve None en cuanto algo no cuadra, y el estampado sigue por el
    camino de siempre

Para comprobar qué saca de TU Audiveris, en una línea:

    python depurar.py partitura.pdf export.mxl --omr libro.omr
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

MM_POR_PULGADA = 25.4

# Un pentagrama son cuatro interlíneas. Si lo leído se sale de este margen
# es que no hemos entendido el archivo y mejor no usarlo.
RELACION_ALTO_MIN = 3.0
RELACION_ALTO_MAX = 5.0


@dataclass
class Pentagrama:
    """Un pentagrama, en píxeles de la hoja."""
    id: int
    izq: float
    der: float
    sup: float
    inf: float


@dataclass
class Cabeza:
    """Una cabeza de nota, en píxeles de la hoja."""
    pentagrama: int
    x: float          # centro
    y: float          # centro
    ancho: float
    alto: float
    forma: str = ""


@dataclass
class Hoja:
    numero: int
    interlinea: float = 0.0
    pentagramas: List[Pentagrama] = field(default_factory=list)
    cabezas: List[Cabeza] = field(default_factory=list)

    def por_pentagrama(self) -> Dict[int, List[Cabeza]]:
        salida: Dict[int, List[Cabeza]] = {}
        for c in self.cabezas:
            salida.setdefault(c.pentagrama, []).append(c)
        for v in salida.values():
            v.sort(key=lambda c: c.x)
        return salida


@dataclass
class Libro:
    hojas: List[Hoja]

    def hoja(self, indice: int) -> Optional[Hoja]:
        """La hoja que corresponde a una página del PDF (0 es la primera)."""
        return self.hojas[indice] if 0 <= indice < len(self.hojas) else None


# --- lectura ----------------------------------------------------------------

def _num(texto: Optional[str]) -> Optional[float]:
    try:
        return float(texto)
    except (TypeError, ValueError):
        return None


def _puntos(elemento: ET.Element) -> List[tuple]:
    """
    Todos los pares (x, y) que cuelguen de un elemento, al nivel que sea.

    Las líneas de pentagrama se guardan como splines, y cómo exactamente
    depende de la versión. En vez de casarnos con una estructura concreta,
    recogemos cualquier cosa que tenga una x y una y: para lo que queremos
    —arriba y abajo del pentagrama— sobra y basta.
    """
    salida = []
    for nodo in elemento.iter():
        x, y = _num(nodo.get("x")), _num(nodo.get("y"))
        if x is not None and y is not None:
            salida.append((x, y))
    return salida


def _interlinea(raiz: ET.Element) -> float:
    """El alto de un espacio de pentagrama, en píxeles."""
    for nodo in raiz.iter():
        if "interline" not in nodo.tag.lower():
            continue
        v = _num(nodo.text) or _num(nodo.get("main")) or _num(nodo.get("value"))
        if v and v > 0:
            return v
    esc = raiz.find(".//scale")
    if esc is not None:
        for clave in ("interline", "main", "value"):
            v = _num(esc.get(clave))
            if v and v > 0:
                return v
    return 0.0


def _leer_hoja(xml: bytes, numero: int) -> Optional[Hoja]:
    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError:
        return None

    hoja = Hoja(numero=numero, interlinea=_interlinea(raiz))

    for nodo in raiz.iter("staff"):
        ident = _num(nodo.get("id"))
        izq, der = _num(nodo.get("left")), _num(nodo.get("right"))
        if ident is None or izq is None or der is None or der <= izq:
            continue
        # La caja vertical sale de las propias líneas: cojamos como las
        # cojamos, la de arriba y la de abajo son el mínimo y el máximo.
        ys = [y for _, y in _puntos(nodo)]
        if len(ys) < 2:
            continue
        hoja.pentagramas.append(
            Pentagrama(int(ident), izq, der, min(ys), max(ys)))

    for etiqueta in ("head", "note-head"):
        for nodo in raiz.iter(etiqueta):
            pent = _num(nodo.get("staff"))
            caja = nodo.find("bounds")
            if pent is None or caja is None:
                continue
            x, y = _num(caja.get("x")), _num(caja.get("y"))
            w = _num(caja.get("w")) or _num(caja.get("width"))
            h = _num(caja.get("h")) or _num(caja.get("height"))
            if None in (x, y, w, h):
                continue
            hoja.cabezas.append(Cabeza(
                pentagrama=int(pent), x=x + w / 2.0, y=y + h / 2.0,
                ancho=w, alto=h, forma=nodo.get("shape") or ""))

    hoja.pentagramas.sort(key=lambda p: p.sup)
    return hoja if hoja.pentagramas else None


def leer(ruta: Path) -> Optional[Libro]:
    """Abre un `.omr`. None si no se entiende o no supera la validación."""
    try:
        with zipfile.ZipFile(ruta) as z:
            nombres = [n for n in z.namelist()
                       if n.lower().startswith("sheet#")
                       and n.lower().endswith(".xml")]
            if not nombres:
                return None

            def numero(n: str) -> int:
                try:
                    return int(n.lower().split("sheet#")[1].split(".")[0])
                except (IndexError, ValueError):
                    return 0

            nombres.sort(key=numero)
            hojas = []
            for n in nombres:
                h = _leer_hoja(z.read(n), numero(n))
                if h is None:
                    return None    # una hoja ilegible invalida el conjunto
                hojas.append(h)
    except (zipfile.BadZipFile, KeyError, OSError):
        return None

    libro = Libro(hojas=hojas)
    return libro if validar(libro) else None


# --- validación -------------------------------------------------------------

def validar(libro: Libro) -> bool:
    """
    ¿Tiene sentido lo que hemos leído?

    Es la red que permite leer un formato no documentado sin jugársela: si
    el esquema cambia en una versión de Audiveris, lo más probable es que
    salgan números absurdos, y entonces no se usa nada de esto.
    """
    if not libro.hojas:
        return False
    for hoja in libro.hojas:
        if not hoja.pentagramas:
            return False
        altos = [p.inf - p.sup for p in hoja.pentagramas]
        anchos = [p.der - p.izq for p in hoja.pentagramas]
        if min(altos) <= 0 or min(anchos) <= 0:
            return False
        # Todos los pentagramas de una hoja miden prácticamente lo mismo.
        if max(altos) > min(altos) * 1.6:
            return False
        if hoja.interlinea > 0:
            relacion = (sum(altos) / len(altos)) / hoja.interlinea
            if not RELACION_ALTO_MIN <= relacion <= RELACION_ALTO_MAX:
                return False
        # Las cabezas tienen que caer sobre algún pentagrama.
        ids = {p.id for p in hoja.pentagramas}
        if hoja.cabezas and not any(c.pentagrama in ids for c in hoja.cabezas):
            return False
    return True


# --- paso a puntos del PDF --------------------------------------------------

def escala_a_puntos(hoja: Hoja, mm_por_decimo: float) -> Optional[float]:
    """
    Cuántos puntos de PDF mide un píxel de la hoja de Audiveris.

    Se deduce de la interlínea, que las dos partes conocen: en el MusicXML
    un espacio son 10 décimos, y `mm_por_decimo` los pasa a milímetros; en
    el `.omr` la interlínea viene en píxeles. No hace falta saber a qué
    resolución renderizó Audiveris el PDF.
    """
    if hoja.interlinea <= 0 or mm_por_decimo <= 0:
        return None
    mm_interlinea = 10.0 * mm_por_decimo
    pt_interlinea = mm_interlinea * 72.0 / MM_POR_PULGADA
    factor = pt_interlinea / hoja.interlinea
    return factor if 0.01 < factor < 10.0 else None


def pentagramas_en_puntos(hoja: Hoja, factor: float
                          ) -> List[tuple]:
    """[(sup, inf, izq, der)] en puntos, como `detectar_pentagramas`."""
    return [(p.sup * factor, p.inf * factor, p.izq * factor, p.der * factor)
            for p in hoja.pentagramas]


def describir(libro: Libro) -> str:
    """Resumen para comprobar a ojo que lo leído tiene sentido."""
    lineas = ["%d hojas" % len(libro.hojas)]
    for h in libro.hojas:
        lineas.append(
            "  hoja %d: %d pentagramas, %d cabezas, interlínea %.1f px"
            % (h.numero, len(h.pentagramas), len(h.cabezas), h.interlinea))
        for p in h.pentagramas[:12]:
            n = sum(1 for c in h.cabezas if c.pentagrama == p.id)
            lineas.append(
                "    pentagrama %-3d x %.0f..%.0f  y %.0f..%.0f  (%d cabezas)"
                % (p.id, p.izq, p.der, p.sup, p.inf, n))
        if len(h.pentagramas) > 12:
            lineas.append("    (+%d más)" % (len(h.pentagramas) - 12))
    return "\n".join(lineas)
