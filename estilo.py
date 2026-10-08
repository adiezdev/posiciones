"""
Ajustes de maquetación para MuseScore.

En vez de pasarle una hoja de estilo por la línea de comandos, que unas veces
atiende y otras no, escribimos los ajustes directamente dentro del archivo
nativo (.mscx) que MuseScore genera. Ese bloque de estilo es el que el propio
programa usa al abrir cualquier partitura suya, así que no hay forma de que
lo ignore.

El .mscx es XML sin comprimir, se edita sin problema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
from xml.etree import ElementTree as ET

from partitura import MARGEN_MM, medidas_papel

MM_POR_PULGADA = 25.4

# Aspecto de los números de posición. Viajan en el renglón de letra, que es
# lo único que va siempre debajo del pentagrama pase lo que pase.
NUMEROS = {
    "lyricsOddFontFace": "FreeSans",
    "lyricsOddFontSize": "7",
    "lyricsEvenFontFace": "FreeSans",
    "lyricsEvenFontSize": "7",
    "lyricsMinBottomDistance": "0.2",
    "lyricsLineHeight": "1",
    "lyricsDashMinLength": "0",
    "lyricsDashMaxLength": "0",
}

# Partitura de atril: que quepa lo máximo por hoja sin dejar de leerse.
# Spatium es el tamaño del pentagrama en milímetros y es la palanca que más
# manda; MuseScore usa 1,75 por defecto y engorda demasiado los sistemas.
DENSIDAD = {
    "Spatium": "1.5",
    "staffUpperBorder": "3",
    "staffLowerBorder": "3",
    "minSystemDistance": "6",
    "maxSystemDistance": "10",
    "enableVerticalSpread": "0",
    "lastSystemFillLimit": "0.1",
    "titleFontSize": "14",
    "subTitleFontSize": "10",
    "frameSystemDistance": "3",
}


def ajustes_pagina(apaisado: bool = False, papel: str = "a4") -> Dict[str, str]:
    """Las medidas de página que entiende MuseScore, en pulgadas."""
    ancho_mm, alto_mm = medidas_papel(apaisado, papel)
    ancho = ancho_mm / MM_POR_PULGADA
    alto = alto_mm / MM_POR_PULGADA
    margen = MARGEN_MM / MM_POR_PULGADA
    util = ancho - 2 * margen

    return {
        "pageWidth": f"{ancho:.5f}",
        "pageHeight": f"{alto:.5f}",
        # Este es el que MuseScore se saltaba por la línea de comandos y el
        # que dejaba la música encajonada en el ancho de un A4 vertical.
        "pagePrintableWidth": f"{util:.5f}",
        "pageEvenLeftMargin": f"{margen:.5f}",
        "pageOddLeftMargin": f"{margen:.5f}",
        "pageEvenTopMargin": f"{margen:.5f}",
        "pageEvenBottomMargin": f"{margen:.5f}",
        "pageOddTopMargin": f"{margen:.5f}",
        "pageOddBottomMargin": f"{margen:.5f}",
        "pageTwosided": "0",
    }


def aplicar_a_mscx(ruta: Path, apaisado: bool = False,
                   papel: str = "a4") -> Dict[str, str]:
    """
    Mete los ajustes en el bloque <Style> del .mscx. Devuelve lo aplicado,
    para poder enseñarlo luego y no tener que adivinar qué ha pasado.
    """
    arbol = ET.parse(ruta)
    raiz = arbol.getroot()

    score = raiz.find("Score")
    if score is None:
        score = raiz

    estilo = score.find("Style")
    if estilo is None:
        estilo = ET.Element("Style")
        score.insert(0, estilo)

    ajustes: Dict[str, str] = {}
    ajustes.update(ajustes_pagina(apaisado, papel))
    ajustes.update(DENSIDAD)
    ajustes.update(NUMEROS)

    for clave, valor in ajustes.items():
        e = estilo.find(clave)
        if e is None:
            e = ET.SubElement(estilo, clave)
        e.text = valor

    arbol.write(ruta, encoding="UTF-8", xml_declaration=True)
    return ajustes
