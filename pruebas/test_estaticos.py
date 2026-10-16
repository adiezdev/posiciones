"""
Revisa el JavaScript de las páginas sin ejecutarlo.

Esta prueba nace de un fallo real: reordenando código se coló la
declaración de `$` por debajo de su primer uso, y la página de repaso
dejó de abrirse entera con un "Cannot access '$' before initialization".
No lo vio nadie porque todo el arnés mira Python y en el navegador no se
ejecuta nada aquí.

No es un intérprete de JavaScript, y no pretende serlo. Comprueba lo justo
que se puede comprobar leyendo, que resulta ser justo la familia de fallos
que hemos tenido: cosas usadas antes de declararse, declaradas dos veces, o
llaves que no cierran.

Lo que SÍ ejecuta el navegador en orden son las líneas de nivel superior.
Lo que está dentro de una función solo corre cuando se la llama, así que
ahí el orden da igual y no se mira.

Ejecutar: python pruebas/test_estaticos.py
"""

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PAGINAS = ["static/index.html", "static/revisar.html"]
SUELTOS = ["static/version.js"]

DECLARACION = re.compile(r"^(?:const|let)\s+(.+?)\s*(?:=|;)")
NOMBRE = re.compile(r"^([A-Za-z_$][\w$]*)")


def frontera(nombre: str):
    r"""
    Busca un identificador entero, no un trozo de otro.

    No vale la frontera de palabra de toda la vida (\b): en JavaScript el
    dólar es un nombre perfectamente válido y \b no lo considera palabra,
    así que \b\$\b no encuentra nada nunca. Y el dólar era justo el nombre
    del fallo que motivó esta prueba, con lo cual la prueba daba verde
    sobre el código roto.
    """
    return re.compile(r"(?<![\w$])%s(?![\w$])" % re.escape(nombre))


def guiones(texto: str) -> str:
    """Quita cadenas y comentarios, para no confundirlos con código."""
    texto = re.sub(r"/\*.*?\*/", "", texto, flags=re.S)
    salida = []
    for linea in texto.split("\n"):
        linea = re.sub(r"//.*$", "", linea)
        linea = re.sub(r'"(?:[^"\\]|\\.)*"', '""', linea)
        linea = re.sub(r"'(?:[^'\\]|\\.)*'", "''", linea)
        linea = re.sub(r"`(?:[^`\\]|\\.)*`", "``", linea)
        salida.append(linea)
    return "\n".join(salida)


def scripts(ruta: Path) -> list:
    """El JavaScript de dentro de un HTML, o el archivo entero si es .js."""
    texto = ruta.read_text(encoding="utf-8")
    if ruta.suffix == ".js":
        return [texto]
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                      texto, flags=re.S)


def declaradas(lineas: list) -> dict:
    """{nombre: línea} de lo declarado en el nivel superior."""
    salida = {}
    for i, linea in enumerate(lineas):
        if linea[:1].isspace() or not linea.strip():
            continue
        m = DECLARACION.match(linea)
        if not m:
            continue
        for trozo in m.group(1).split(","):
            n = NOMBRE.match(trozo.strip())
            if n:
                salida.setdefault(n.group(1), i)
    return salida


def revisar(ruta: Path, contar_llaves: bool = True) -> list:
    quejas = []
    for codigo in scripts(ruta):
        limpio = guiones(codigo)
        lineas = limpio.split("\n")
        nombres = declaradas(lineas)

        # --- declarada dos veces: el navegador ni carga el archivo --------
        vistas = {}
        for i, linea in enumerate(lineas):
            if linea[:1].isspace():
                continue
            m = DECLARACION.match(linea)
            if not m:
                continue
            for trozo in m.group(1).split(","):
                n = NOMBRE.match(trozo.strip())
                if not n:
                    continue
                if n.group(1) in vistas:
                    quejas.append(
                        "%s: '%s' se declara dos veces (líneas %d y %d)"
                        % (ruta.name, n.group(1), vistas[n.group(1)] + 1, i + 1))
                vistas[n.group(1)] = i

        # --- usada antes de declararse ------------------------------------
        for nombre, donde in nombres.items():
            patron = frontera(nombre)
            for i in range(donde):
                linea = lineas[i]
                if linea[:1].isspace() or not linea.strip():
                    continue      # dentro de una función: se ejecuta luego
                if patron.search(linea):
                    quejas.append(
                        "%s: se usa '%s' en la línea %d pero no se declara "
                        "hasta la %d (el navegador falla con \"Cannot access "
                        "'%s' before initialization\")"
                        % (ruta.name, nombre, i + 1, donde + 1, nombre))
                    break

        # --- llaves y paréntesis ------------------------------------------
        # Solo en los .js. En el HTML hay plantillas con `${...}` anidadas
        # unas dentro de otras, y quitarlas bien pide un analizador de
        # verdad: contar a ojo da falsos positivos y una prueba que grita
        # sin motivo acaba ignorándose, que es peor que no tenerla.
        if contar_llaves:
            for abre, cierra in (("{", "}"), ("(", ")"), ("[", "]")):
                if limpio.count(abre) != limpio.count(cierra):
                    quejas.append("%s: %d '%s' y %d '%s' no cuadran"
                                  % (ruta.name, limpio.count(abre), abre,
                                     limpio.count(cierra), cierra))
    return quejas


def main() -> None:
    quejas = []
    for nombre in PAGINAS + SUELTOS:
        ruta = RAIZ / nombre
        if not ruta.exists():
            quejas.append("falta %s" % nombre)
            continue
        quejas += revisar(ruta, contar_llaves=ruta.suffix == ".js")

    # El guardián del guardián: si la comprobación no encuentra nada que
    # mirar, no está comprobando nada y daría un falso verde.
    total = sum(len(declaradas(guiones(c).split("\n")))
                for n in PAGINAS + SUELTOS
                for c in scripts(RAIZ / n))
    if total < 10:
        quejas.append("solo se han encontrado %d declaraciones: la prueba no "
                      "está leyendo el código" % total)

    if quejas:
        for q in quejas:
            print("  " + q)
        print("FALLA: %d problemas en el JavaScript de las páginas" % len(quejas))
        raise SystemExit(1)
    print("OK: %d declaraciones revisadas, ninguna usada antes de tiempo ni "
          "repetida." % total)


if __name__ == "__main__":
    main()
