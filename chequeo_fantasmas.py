"""
Chequeo del veto de texto en _fantasmas.

Dibuja un PDF con glifos de texto en posiciones conocidas y, sustituyendo
solo la detección de pentagramas y de cabezas (que necesitan partituras de
verdad), comprueba que:

  - un blob que lleva al lado un glifo de cabeza se conserva (nota olvidada)
  - un blob que solo lleva una cifra u otro texto se veta (falso positivo)

Ejecutar: python3 chequeo_fantasmas.py
"""

import sys
from pathlib import Path

from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).parent))
import superponer as S

ANCHO, ALTO = 842.0, 596.0
ruta = Path("/tmp/chequeo_fantasmas.pdf")


def construir_pdf():
    """Glifos en coords top-origin: X en la nota real y sus vecinas, y el
    fantasma bueno; '4' (cifra) en el fantasma malo."""
    c = canvas.Canvas(str(ruta), pagesize=(ANCHO, ALTO))
    c.setFont("Helvetica", 10)
    for x in (100, 200, 300):
        c.drawString(x, ALTO - 380, "X")
    c.drawString(250, ALTO - 380, "4")   # fantasma malo
    c.drawString(400, ALTO - 380, "X")   # fantasma bueno
    c.showPage()
    c.save()


def main() -> None:
    # _midi_fa: clave de fa, línea inferior Sol2; la armadura altera la altura
    # (sin esto un Si bemol sale como Si natural y da posición equivocada).
    assert S._midi_fa(392.0, 376.0, 392.0) == 43, "línea inferior debe ser Sol2"
    assert S._midi_fa(388.0, 376.0, 392.0) == 47, "2ª línea debe ser Si2 natural"
    assert S._midi_fa(388.0, 376.0, 392.0, -1) == 46, "1 bemol: Si2 -> Si bemol"
    assert S._midi_fa(380.0, 376.0, 392.0, 1) == 54, "1 sostenido: Fa3 -> Fa#"

    construir_pdf()

    # Fijamos pentagrama y cabezas (sup, inf, izq, der = 376..392).
    original_pent = S.detectar_pentagramas
    original_cabezas = S._cabezas
    S.detectar_pentagramas = (
        lambda pdf, indice, alto_pt, esperados=None, dpi=150: [(376.0, 392.0, 0.0, ANCHO)])
    # Cabezas: SOLO los dos blobs dudosos (el malo en 250, tinta de cifra; el
    # bueno en 400, tinta de cabeza). Las notas de verdad (100/200/300) no
    # aparecen aquí a propósito: así se comprueba que el alfabeto de cabeza se
    # saca de las posiciones del OMR y no de lo que devuelva la detección.
    S._cabezas = lambda a, sup, inf, izq, der, ex, ey: [
        (250.0, 380.0, 6.0), (400.0, 380.0, 6.0)]

    registros = [
        {"x": 100, "x_medida": 100, "x_sistema": 100, "duracion": 1.0,
         "offset": 0.0, "dx": 0, "pagina": 0, "sistema": 0},
        {"x": 200, "x_medida": 200, "x_sistema": 200, "duracion": 1.0,
         "offset": 1.0, "dx": 0, "pagina": 0, "sistema": 0},
        {"x": 300, "x_medida": 300, "x_sistema": 300, "duracion": 1.0,
         "offset": 2.0, "dx": 0, "pagina": 0, "sistema": 0},
    ]
    sistemas = [{"x_ini": 0.0, "x_fin": ANCHO, "y_numeros": 400.0}]

    try:
        salida = S._fantasmas(ruta, registros, [], sistemas,
                              {0: [0]}, ANCHO, "medida")
    finally:
        S.detectar_pentagramas = original_pent
        S._cabezas = original_cabezas

    kepts = [g["x"] for _, g in salida]
    assert kepts == [400.0], f"se esperaba solo el fantasma bueno en x=400, hay {kepts}"
    assert len(salida) == 1, f"faltan o sobran fantasmas: {salida}"
    print(f"OK: {len(salida)} fantasma en x={kepts[0]}, cifra veteada.")
    ruta.unlink(missing_ok=True)


if __name__ == "__main__":
    main()