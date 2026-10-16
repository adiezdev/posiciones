"""
Comprueba que lo dudoso se estampa, pero se nota.

La regla de la que cuelga media herramienta: **nunca se tira un número**.
Si la coordenada se ha deducido, si la nota se ha rescatado de la imagen o
si la página no tiene pentagramas fiables, el número sale igual, solo que
más claro. Un número gris avisa de que hay que mirarlo; uno que falta no
avisa de nada, y eso es exactamente lo que hacía que "se comiera notas".

Ejecutar: python pruebas/test_confianza.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import superponer as S
from sintetico import Caso


def construir() -> tuple:
    carpeta = Path(tempfile.mkdtemp(prefix="confianza-"))
    Caso(paginas=1, sistemas=3, compases=3).generar(carpeta)
    return carpeta, carpeta / "partitura.pdf", carpeta / "export.musicxml"


def main() -> None:
    import partitura

    carpeta, pdf, xml = construir()
    plano = superponer_plano(partitura, pdf, xml)

    total = len(plano.marcas)
    assert total > 10, "la partitura de prueba se ha quedado sin notas"

    # 1) Todas de fiar: ningún número en gris.
    puestas, _, dudosas = estampar(pdf, plano)
    assert puestas == total, "se han perdido números: %d de %d" % (puestas, total)
    assert dudosas == 0, "no debería haber dudosos y hay %d" % dudosas

    # 2) Un tercio con la coordenada deducida y sin cabeza que lo confirme:
    #    se estampan los mismos, pero marcados.
    for i, m in enumerate(plano.marcas):
        if i % 3 == 0:
            m.confianza = S.CONFIANZA_X_ESTIMADA * S.FACTOR_SIN_CABEZA
    puestas, _, dudosas = estampar(pdf, plano)
    esperados = len([1 for i in range(total) if i % 3 == 0])
    assert puestas == total, "estampar ha perdido números: %d de %d" % (puestas, total)
    assert dudosas == esperados, \
        "se esperaban %d dudosos y hay %d" % (esperados, dudosas)

    # 3) Notas rescatadas de la imagen: siempre dudosas.
    for m in plano.marcas:
        m.confianza = S.CONFIANZA_FANTASMA
    puestas, _, dudosas = estampar(pdf, plano)
    assert puestas == total, "estampar ha perdido números: %d de %d" % (puestas, total)
    assert dudosas == total, \
        "una nota rescatada de la imagen siempre es dudosa (%d de %d)" % (dudosas, total)

    print("OK: %d números, ninguno se pierde; los dudosos se cuentan y salen "
          "más claros." % total)


def superponer_plano(partitura, pdf: Path, xml: Path):
    plano = S.preparar(partitura.cargar(xml), pdf=pdf)
    assert plano is not None, "preparar() se ha rendido con la partitura de prueba"
    return plano


def estampar(pdf: Path, plano) -> tuple:
    with tempfile.TemporaryDirectory() as tmp:
        return S.estampar(pdf, Path(tmp) / "salida.pdf", plano)


if __name__ == "__main__":
    main()
