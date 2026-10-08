"""
Anota un MusicXML con las posiciones de vara.

Uso:
    python3 anotar.py entrada.mxl salida.musicxml [--transpositor]
"""

from __future__ import annotations

import argparse
from typing import List, Tuple

from music21 import chord, converter, dynamics, spanner, stream

from posiciones import Nota, elegir_posiciones


def _viene_ligada(n) -> bool:
    """True si la nota es continuación de una ligadura de expresión."""
    try:
        for sp in n.getSpannerSites():
            if isinstance(sp, spanner.Slur) and not sp.isFirst(n):
                return True
    except Exception:
        pass
    return False


def anotar_parte(parte: stream.Stream, transpositor: bool = False,
                 marcar_fuera_rango: bool = True,
                 preferir_cercanas: bool = False,
                 peso_movimiento=None) -> Tuple[int, int]:
    """Anota una parte. Devuelve (anotadas, fuera_de_rango)."""
    elementos: List[tuple] = []
    for el in parte.recurse().notes:
        # En divisi (acordes del OMR) anotamos solo la voz superior.
        objetivo = max(el.notes, key=lambda x: x.pitch.midi) if isinstance(el, chord.Chord) else el
        elementos.append((el, objetivo))

    if not elementos:
        return 0, 0

    datos = []
    for el, obj in elementos:
        try:
            offset = float(el.getOffsetInHierarchy(parte))
        except Exception:
            offset = float(el.offset)
        datos.append(Nota(
            midi=obj.pitch.midi,
            offset=offset,
            duracion=float(el.quarterLength or 1.0),
            ligada=_viene_ligada(el),
        ))

    elegidas = elegir_posiciones(datos, transpositor, preferir_cercanas,
                                 peso_movimiento)

    ok = fuera = 0
    for (el, obj), pos in zip(elementos, elegidas):
        if pos is None:
            fuera += 1
            if marcar_fuera_rango:
                el.addLyric("?")
            continue
        # Usamos el renglón de letra en vez de digitaciones: es la única forma
        # de que el número quede SIEMPRE debajo del pentagrama. MuseScore
        # coloca las digitaciones encima por estilo propio, ignorando el
        # placement del MusicXML.
        el.addLyric(pos.etiqueta)
        ok += 1

    return ok, fuera


def quitar_reguladores(score: stream.Score) -> int:
    """
    Elimina los reguladores (crescendo y diminuendo) que el reconocimiento
    óptico suele inventarse al confundir ligaduras, plicas u otros trazos en
    ángulo. Los matices escritos (p, f, mf...) no se tocan.
    """
    quitados = 0
    contenedores = [score] + list(score.recurse().getElementsByClass(stream.Stream))
    for c in contenedores:
        for sp in list(c.getElementsByClass(spanner.Spanner)):
            if isinstance(sp, dynamics.DynamicWedge):
                c.remove(sp)
                quitados += 1
    return quitados


def anotar(score: stream.Score, transpositor: bool = False,
           marcar_fuera_rango: bool = True,
           preferir_cercanas: bool = False,
           peso_movimiento=None) -> Tuple[int, int]:
    partes = list(score.parts) if hasattr(score, "parts") and len(score.parts) else [score]
    ok = fuera = 0
    for p in partes:
        a, b = anotar_parte(p, transpositor, marcar_fuera_rango,
                            preferir_cercanas, peso_movimiento)
        ok += a
        fuera += b
    return ok, fuera


def main() -> None:
    ap = argparse.ArgumentParser(description="Añade posiciones de vara a un MusicXML")
    ap.add_argument("entrada", help="fichero .mxl / .musicxml / .xml")
    ap.add_argument("salida", help="fichero .musicxml de salida")
    ap.add_argument("--transpositor", action="store_true",
                    help="permitir posiciones con válvula de Fa (T1..T6)")
    args = ap.parse_args()

    score = converter.parse(args.entrada)
    ok, fuera = anotar(score, args.transpositor)
    score.write("musicxml", args.salida)
    print(f"{ok} notas anotadas, {fuera} fuera de rango -> {args.salida}")


if __name__ == "__main__":
    main()
