"""
Comprueba que enderezar endereza, y no lo contrario.

Esta prueba existe porque el signo estaba al revés: la corrección giraba
la página en la misma dirección que ya estaba torcida y la dejaba con el
doble de inclinación (1,4° -> 2,8°). Y no se notaba, porque la partitura
sintética tenía una geometría declarada tan buena que el estampado salía
bien igualmente. Lo único que lo delató fue mirar los pentagramas
detectados, que pasaban de cuatro a cero.

Ejecutar: python pruebas/test_normalizar.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import normalizar as N
import superponer as S
from sintetico import Caso

INCLINACION = 1.4
SISTEMAS = 4


def alto_pt(pdf: Path) -> float:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        return doc[0].get_height()
    finally:
        doc.close()


def main() -> None:
    carpeta = Path(tempfile.mkdtemp(prefix="normalizar-"))

    # --- una página derecha no se toca ------------------------------------
    Caso(paginas=1, sistemas=SISTEMAS).generar(carpeta)
    limpio = carpeta / "partitura.pdf"
    informe = N.analizar(limpio)
    assert abs(informe.paginas[0].inclinacion) < N.INCLINACION_MINIMA, \
        "una página derecha no puede salir torcida: %.2f°" % informe.paginas[0].inclinacion
    # Lo que importa no es que traiga texto —una partitura grabada puede no
    # tener ni una letra extraíble— sino que no se confunda con un escaneo y
    # acabe rasterizada sin motivo.
    assert not informe.paginas[0].escaneo, (
        "un PDF vectorial no es un escaneo aunque no tenga texto")
    assert not informe.paginas[0].con_imagen, (
        "un PDF vectorial no lleva una imagen que ocupe la hoja")

    salida = carpeta / "igual.pdf"
    informe = N.normalizar(limpio, salida)
    assert not informe.rasterizado, \
        "un PDF bueno no se re-rasteriza: solo serviría para perder calidad"

    # --- una fotocopia torcida se endereza --------------------------------
    torcido = Path(tempfile.mkdtemp(prefix="torcido-"))
    Caso(paginas=1, sistemas=SISTEMAS, inclinar=INCLINACION).generar(torcido)
    pdf = torcido / "partitura.pdf"

    antes = N.analizar(pdf).paginas[0]
    assert antes.escaneo, "una página rasterizada es un escaneo"
    assert abs(abs(antes.inclinacion) - INCLINACION) < 0.2, \
        "se ha medido %.2f° donde había %.2f°" % (antes.inclinacion, INCLINACION)

    derecho = torcido / "normalizado.pdf"
    informe = N.normalizar(pdf, derecho)
    assert informe.rasterizado, "la página torcida tenía que haberse enderezado"

    despues = N.analizar(derecho).paginas[0]
    assert abs(despues.inclinacion) < abs(antes.inclinacion) / 2.0, \
        ("enderezar ha dejado la página a %.2f°, que no mejora los %.2f° de "
         "partida (¿signo invertido?)" % (despues.inclinacion, antes.inclinacion))
    assert abs(despues.inclinacion) < N.INCLINACION_MINIMA, \
        "sigue torcida: %.2f°" % despues.inclinacion

    # --- y eso devuelve los pentagramas que se habían perdido -------------
    sin = len(S.detectar_pentagramas(pdf, 0, alto_pt(pdf), esperados=SISTEMAS))
    con = len(S.detectar_pentagramas(derecho, 0, alto_pt(derecho),
                                     esperados=SISTEMAS))
    assert con == SISTEMAS, \
        "tras enderezar deberían detectarse %d pentagramas y hay %d" % (SISTEMAS, con)
    assert sin < con, \
        ("enderezar no cambia nada: %d pentagramas antes y %d después, así que "
         "esta prueba no está midiendo lo que cree" % (sin, con))

    print("OK: %.2f° -> %.2f°, y los pentagramas detectados pasan de %d a %d."
          % (antes.inclinacion, despues.inclinacion, sin, con))


if __name__ == "__main__":
    main()
