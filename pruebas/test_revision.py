"""
El repaso a mano: corregir un número no mueve los demás.

Ningún reconocimiento óptico acierta el cien por cien, y prometerlo es
engañar. Lo que sí se puede garantizar es que repasar sea barato: la hoja
de marcas son los números ya resueltos, en puntos de la página, así que
corregir uno es cambiar una entrada de una lista y volver a dibujarla. No
se recalcula nada, y por tanto no hay forma de que tocar un número desplace
a otro.

Ejecutar: python pruebas/test_revision.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import partitura
import superponer as S
from sintetico import Caso


def leer_texto(pdf: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)


def main() -> None:
    carpeta = Path(tempfile.mkdtemp(prefix="revision-"))
    Caso(paginas=1, sistemas=3, compases=3, cabezas_texto=True).generar(carpeta)
    pdf = carpeta / "partitura.pdf"

    plano = S.preparar(partitura.cargar(carpeta / "export.musicxml"), pdf=pdf)
    assert plano is not None, "preparar() se ha rendido"

    diag = []
    puestas, _, _ = S.estampar(pdf, carpeta / "primera.pdf", plano, diag=diag)
    hoja = S.hoja_de_marcas(diag)

    numeros = [n for p in hoja["paginas"] for n in p["numeros"]]
    assert len(numeros) == puestas, \
        "la hoja tiene %d números y se estamparon %d" % (len(numeros), puestas)
    assert all(n["cuerpo"] > 0 for n in numeros), \
        "sin el cuerpo guardado, al reestampar saldría otro tamaño de letra"

    # El cuerpo tiene que salir de la misma fórmula que usa el estampado.
    # Esta comprobación existe porque el PDF de diagnóstico calculaba el
    # suyo aparte: dibujaba las cifras un 40 % más estrechas, y el PDF
    # final parecía colocarlas mal cuando lo único que cambiaba era el
    # tamaño. Un diagnóstico que no enseña lo que se descarga engaña.
    espacio = diag[0].get("espacio") or 0.0
    assert espacio > 0, "la ficha de diagnóstico no trae el espacio"
    for n in numeros:
        assert (S.CUERPO_MIN * espacio - 0.01 <= n["cuerpo"]
                <= S.CUERPO_MAX * espacio + 0.01), (
            "cuerpo %.2f fuera de [%.2f, %.2f] para un espacio de %.2f"
            % (n["cuerpo"], S.CUERPO_MIN * espacio,
               S.CUERPO_MAX * espacio, espacio))

    # --- volver a estampar sin tocar nada da lo mismo ---------------------
    otra = S.estampar_resueltas(pdf, carpeta / "igual.pdf", hoja)
    assert otra == puestas, "%d != %d" % (otra, puestas)

    pagina = hoja["paginas"][0]
    antes = [(n["x"], n["y"], n["texto"]) for n in pagina["numeros"]]

    # --- corregir uno y borrar otro ---------------------------------------
    pagina["numeros"][0]["texto"] = "T6"
    pagina["numeros"][0]["a_mano"] = True
    quitado = pagina["numeros"].pop(1)

    tras = S.estampar_resueltas(pdf, carpeta / "corregida.pdf", hoja)
    assert tras == puestas - 1, "se esperaban %d y hay %d" % (puestas - 1, tras)

    despues = [(n["x"], n["y"], n["texto"]) for n in pagina["numeros"]]
    for i, (x, y, _t) in enumerate(despues[1:], start=1):
        # antes[0] cambió de texto y antes[1] se fue; el resto, intacto.
        assert (x, y) == antes[i + 1][:2], \
            "corregir un número ha movido el %d: %s -> %s" % (i, antes[i + 1], (x, y))
    assert despues[0][:2] == antes[0][:2], "el número corregido se ha movido"
    assert despues[0][2] == "T6"

    # --- añadir uno a mano -------------------------------------------------
    vecino = pagina["numeros"][0]
    pagina["numeros"].append({
        "x": vecino["x"] + 40.0, "y": vecino["y"], "texto": "4",
        "sistema": vecino["sistema"], "confianza": 1.0,
        "cuerpo": vecino["cuerpo"], "estimada": False, "a_mano": True})
    con_mas = S.estampar_resueltas(pdf, carpeta / "anadida.pdf", hoja)
    assert con_mas == tras + 1, "se esperaban %d y hay %d" % (tras + 1, con_mas)

    # --- deducir la nota de donde se pincha --------------------------------
    sup, inf = pagina["pentagramas"][0][0], pagina["pentagramas"][0][1]
    abajo = S.altura_en_punto(pagina, 200.0, inf)
    assert abajo["midi"] == 43, \
        "en clave de fa la línea de abajo es Sol2 (43), no %s" % abajo["midi"]
    arriba = S.altura_en_punto(pagina, 200.0, sup)
    assert arriba["midi"] == 57, \
        "la línea de arriba es La3 (57), no %s" % arriba["midi"]

    print("OK: %d números; corregir, borrar y añadir no mueve nada más. "
          "El que se quitó era un '%s'." % (puestas, quitado["texto"]))


if __name__ == "__main__":
    main()
