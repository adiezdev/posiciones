"""
Estampa los números de posición sobre el PDF original, sin redibujar la música.

Es la alternativa a re-grabar la partitura. Ventaja grande: tu PDF sigue
siendo tu PDF, con su maquetación, su tipografía y sus saltos de línea, y es
imposible que se altere una nota porque nunca se vuelve a dibujar nada.

Para saber dónde va cada número hacen falta las coordenadas de cada cabeza de
nota. Audiveris las exporta en el MusicXML como atributos `default-x` de las
notas, junto con los anchos de compás y la disposición de páginas y sistemas.
Reconstruimos la posición absoluta a partir de todo eso.

Si el MusicXML no trae esas coordenadas, `preparar` devuelve None y quien
llama debe recurrir al método de siempre.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from posiciones import Nota, elegir_posiciones

# Un pentagrama de cinco líneas mide cuatro espacios, y un espacio son diez
# décimos. Esta es la unidad en la que MusicXML mide todo.
ALTO_PENTAGRAMA = 40.0

# Dónde caen los números dentro del hueco que queda entre un pentagrama y el
# siguiente. No puede ser una distancia fija: en una partitura apretada los
# sistemas van a 60 décimos y en una espaciada a 200. Los ponemos en la parte
# baja del hueco, lejos de los matices y los reguladores, que viven pegados al
# pentagrama, pero sin llegar a tocar el sistema de abajo.
FRACCION_HUECO = 0.65
SEPARACION_MIN = 18.0
SEPARACION_MAX = 90.0
HUECO_POR_DEFECTO = 60.0

# Una cabeza de nota mide algo más de un espacio de ancho. `default-x` apunta
# a su borde izquierdo, así que desplazamos para centrar el número debajo.
MEDIA_CABEZA = 6.5

PASOS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Si falta la coordenada de más de esta fracción de notas, no merece la pena.
TOLERANCIA_SIN_COORDENADA = 0.40


@dataclass
class Marca:
    """Un número listo para estampar, en décimos desde la esquina superior."""
    pagina: int
    x: float
    y: float
    texto: str
    sistema: int = 0


@dataclass
class Plano:
    marcas: List[Marca]
    ancho_pagina: float          # en décimos
    alto_pagina: float           # en décimos
    mm_por_decimo: float
    espacio_decimos: float = 10.0
    sin_coordenada: int = 0
    total: int = 0
    modo_x: str = "medida"       # respecto a qué mide Audiveris el default-x
    acierto: float = 0.0         # fracción de números que caen dentro de la caja
    tramos: Optional[Dict[int, Tuple[float, float]]] = None  # x inicio/fin por sistema
    # Qué sistemas hay en cada página, en orden, INCLUIDOS los que no llevan
    # ninguna nota. Sin esto, un sistema de silencios desplaza todo lo demás
    # un pentagrama al emparejar.
    sistemas_pagina: Optional[Dict[int, List[int]]] = None


# --- lectura del MusicXML ---------------------------------------------------

def _f(texto: Optional[str], defecto: float = 0.0) -> float:
    try:
        return float(texto)
    except (TypeError, ValueError):
        return defecto


def _midi(nota: ET.Element) -> Optional[int]:
    p = nota.find("pitch")
    if p is None:
        return None
    paso = PASOS.get((p.findtext("step") or "").strip().upper())
    if paso is None:
        return None
    octava = _f(p.findtext("octave"), -99)
    if octava < -50:
        return None
    return int((octava + 1) * 12 + paso + _f(p.findtext("alter")))


def _margenes(page_layout: Optional[ET.Element]) -> Optional[Tuple[float, float]]:
    """Margen izquierdo y superior en décimos."""
    if page_layout is None:
        return None
    for m in page_layout.findall("page-margins"):
        if m.get("type") in (None, "both", "odd"):
            return _f(m.findtext("left-margin")), _f(m.findtext("top-margin"))
    return None


def _referencias(registros: List[dict], margen_izq: float,
                 ancho_pagina: float) -> Tuple[str, float]:
    """
    Averigua respecto a qué mide el exportador la coordenada `default-x`.

    La norma dice que es relativa al inicio del compás, pero no todos los
    programas la escriben así, y si nos equivocamos los números salen
    corridos a la derecha.

    El indicio está en la primera nota de cada compás: siempre cae a la misma
    distancia de su barra de compás, un poco después. Así que para cada
    interpretación calculamos dónde quedaría esa primera nota respecto a su
    compás; la buena es la que da una distancia pequeña y constante, y las
    malas dan valores que crecen sistema a sistema.
    """
    primeras: Dict[Tuple[float, float], dict] = {}
    for r in registros:
        if r["dx"] is None:
            continue
        clave = (r["x_medida"], r["x_sistema"])
        if clave not in primeras or r["dx"] < primeras[clave]["dx"]:
            primeras[clave] = r

    muestras = list(primeras.values())

    def prediccion(modo: str, r: dict) -> float:
        if modo == "medida":
            return r["x_medida"] + r["dx"]
        if modo == "sistema":
            return r["x_sistema"] + r["dx"]
        return r["dx"]

    def evaluar(modo: str) -> Tuple[float, float]:
        """Devuelve (dispersión, media) de la distancia a la barra."""
        if not muestras:
            return float("inf"), 0.0
        huecos = [prediccion(modo, r) - r["x_medida"] for r in muestras]
        media = sum(huecos) / len(huecos)
        var = sum((h - media) ** 2 for h in huecos) / len(huecos)
        return var ** 0.5, media

    # Desempates: primero la interpretación más constante, luego la que deja
    # la primera nota a una distancia realista de la barra, y por último la
    # de la norma.
    HUECO_TIPICO = 45.0
    PRIORIDAD = {"medida": 0, "sistema": 1, "pagina": 2}

    candidatos = []
    for modo in ("medida", "sistema", "pagina"):
        disp, media = evaluar(modo)
        # La primera nota nunca se pega a la barra ni se va medio compás.
        plausible = -20.0 <= media <= 250.0
        candidatos.append((0 if plausible else 1, round(disp, 1),
                           round(abs(media - HUECO_TIPICO)),
                           PRIORIDAD[modo], modo))

    candidatos.sort()
    mejor = candidatos[0][4]

    # Comprobación final: con esa interpretación, cuántos números caen dentro
    # de la caja de la página.
    izq, der = margen_izq - 30.0, ancho_pagina - margen_izq + 30.0
    dentro = contados = 0
    for r in registros:
        if r["dx"] is None:
            continue
        contados += 1
        if izq <= prediccion(mejor, r) <= der:
            dentro += 1

    return mejor, (dentro / contados if contados else 0.0)



def _rellenar_dx(registros: List[dict]) -> None:
    i = 0
    n = len(registros)
    while i < n:
        if registros[i].get("dx") is not None:
            i += 1
            continue
        j = i - 1
        while j >= 0 and registros[j].get("dx") is None:
            j -= 1
        k = i + 1
        while k < n and registros[k].get("dx") is None:
            k += 1
        if j >= 0 and k < n:
            if (registros[j]["sistema"] == registros[k]["sistema"]
                    and registros[j]["pagina"] == registros[k]["pagina"]
                    and abs(k - j) <= 6):  # evita interpolar huecos grandes
                dx1 = registros[j]["dx"]
                dx2 = registros[k]["dx"]
                total = k - j
                paso = (dx2 - dx1) / total
                for t in range(1, total):
                    if registros[j + t].get("dx") is None:
                        registros[j + t]["dx"] = dx1 + paso * t
        i += 1


def preparar(ruta_musicxml, transpositor: bool = False,
             preferir_cercanas: bool = False,
             peso_movimiento=None) -> Optional[Plano]:
    """
    Recorre el MusicXML reconstruyendo la posición de cada nota en la página.
    Devuelve None si no hay coordenadas utilizables.
    """
    arbol = ET.parse(ruta_musicxml) if isinstance(ruta_musicxml, Path) else ruta_musicxml
    raiz = arbol.getroot()

    partes = raiz.findall("part")
    if len(partes) != 1:
        # Con varias partes haría falta seguir también la separación entre
        # pentagramas; no es el caso de una particella de trombón.
        return None

    defaults = raiz.find("defaults")
    if defaults is None:
        return None

    esc = defaults.find("scaling")
    pl_global = defaults.find("page-layout")
    if esc is None or pl_global is None:
        return None

    mm = _f(esc.findtext("millimeters"))
    tenths = _f(esc.findtext("tenths"))
    if mm <= 0 or tenths <= 0:
        return None
    mm_por_decimo = mm / tenths

    ancho_pagina = _f(pl_global.findtext("page-width"))
    alto_pagina = _f(pl_global.findtext("page-height"))
    if ancho_pagina <= 0 or alto_pagina <= 0:
        return None

    margen_izq, margen_sup = _margenes(pl_global) or (0.0, 0.0)

    # Valores por defecto de la disposición de sistemas. Se conservan de un
    # compás al siguiente mientras nadie los cambie; antes los reiniciaba a
    # cero en cada compás y por eso se descolocaban los sistemas.
    sys_izq = 0.0
    dist_primer_sistema = 0.0
    dist_sistema = 0.0
    sl_global = defaults.find("system-layout")
    if sl_global is not None:
        sm = sl_global.find("system-margins")
        if sm is not None:
            sys_izq = _f(sm.findtext("left-margin"))
        dist_primer_sistema = _f(sl_global.findtext("top-system-distance"))
        dist_sistema = _f(sl_global.findtext("system-distance"))

    pagina = -1
    sistema = -1
    sistemas: List[dict] = []        # (pagina, y superior) de cada sistema
    x_sistema = margen_izq
    y_sistema = margen_sup
    x_medida = margen_izq
    ancho_anterior = 0.0
    divisiones = 1.0
    offset = 0.0
    primera = True

    registros: List[dict] = []
    sin_coordenada = 0

    for medida in partes[0].findall("measure"):
        imp = medida.find("print")
        nueva_pagina = primera
        nuevo_sistema = primera

        if imp is not None:
            if imp.get("new-page") == "yes":
                nueva_pagina = True
            if imp.get("new-system") == "yes":
                nuevo_sistema = True

            nuevos = _margenes(imp.find("page-layout"))
            if nuevos:
                margen_izq, margen_sup = nuevos

            sl = imp.find("system-layout")
            if sl is not None:
                sm = sl.find("system-margins")
                if sm is not None and sm.find("left-margin") is not None:
                    sys_izq = _f(sm.findtext("left-margin"))
                if sl.find("top-system-distance") is not None:
                    dist_primer_sistema = _f(sl.findtext("top-system-distance"))
                if sl.find("system-distance") is not None:
                    dist_sistema = _f(sl.findtext("system-distance"))

        if nueva_pagina:
            pagina += 1
            sistema += 1
            y_sistema = margen_sup + dist_primer_sistema
            x_sistema = margen_izq + sys_izq
            x_medida = x_sistema
            sistemas.append({"pagina": pagina, "y": y_sistema,
                             "x_ini": x_sistema, "x_fin": x_sistema})
        elif nuevo_sistema:
            sistema += 1
            y_sistema = y_sistema + ALTO_PENTAGRAMA + dist_sistema
            x_sistema = margen_izq + sys_izq
            x_medida = x_sistema
            sistemas.append({"pagina": pagina, "y": y_sistema,
                             "x_ini": x_sistema, "x_fin": x_sistema})
        else:
            x_medida = x_medida + ancho_anterior

        ancho_anterior = _f(medida.get("width"))
        if sistemas:
            sistemas[-1]["x_fin"] = x_medida + ancho_anterior
        primera = False

        grupo: List[dict] = []

        def cerrar_grupo() -> None:
            if not grupo:
                return
            mejor = max(grupo, key=lambda r: r["midi"])
            mejor["dx"] = grupo[0]["dx"]
            registros.append(mejor)
            grupo.clear()

        for hijo in medida:
            if hijo.tag == "attributes":
                div = hijo.findtext("divisions")
                if div:
                    divisiones = _f(div, divisiones) or 1.0

            elif hijo.tag == "backup":
                cerrar_grupo()
                offset -= _f(hijo.findtext("duration")) / divisiones

            elif hijo.tag == "forward":
                cerrar_grupo()
                offset += _f(hijo.findtext("duration")) / divisiones

            elif hijo.tag == "note":
                en_acorde = hijo.find("chord") is not None
                if not en_acorde:
                    cerrar_grupo()

                # Las notas de adorno se tocan y necesitan su posición, pero
                # no ocupan tiempo: no llevan <duration> y no mueven el reloj.
                adorno = hijo.find("grace") is not None
                duracion = 0.0 if adorno else _f(hijo.findtext("duration")) / divisiones

                if hijo.find("rest") is not None:
                    offset += duracion
                    continue

                midi = _midi(hijo)
                if midi is None:
                    offset += 0 if en_acorde else duracion
                    continue

                dx = hijo.get("default-x")
                if dx is None:
                    sin_coordenada += 1

                grupo.append({
                    "midi": midi,
                    "offset": offset,
                    "duracion": duracion,
                    "pagina": pagina,
                    "dx": None if dx is None else _f(dx),
                    "x_medida": x_medida,
                    "x_sistema": x_sistema,
                    "sistema": max(sistema, 0),
                })

                if not en_acorde and not adorno:
                    offset += duracion

        cerrar_grupo()

    if not registros:
        return None

    _rellenar_dx(registros)
    utiles = [r for r in registros if r["dx"] is not None]
    if not utiles:
        return None
    # No descartamos por falta de coordenadas: rellenamos por interpolación
    pass

    # Altura de los números: en la parte baja del hueco hasta el sistema
    # siguiente, medido de verdad en lugar de a ojo.
    huecos = []
    for i, s_i in enumerate(sistemas):
        siguiente = sistemas[i + 1] if i + 1 < len(sistemas) else None
        if siguiente and siguiente["pagina"] == s_i["pagina"]:
            huecos.append(siguiente["y"] - (s_i["y"] + ALTO_PENTAGRAMA))
        else:
            huecos.append(None)

    medidos = [h for h in huecos if h and h > 0]
    tipico = sorted(medidos)[len(medidos) // 2] if medidos else HUECO_POR_DEFECTO

    for i, s_i in enumerate(sistemas):
        hueco = huecos[i] if (huecos[i] and huecos[i] > 0) else tipico
        sep = min(max(hueco * FRACCION_HUECO, SEPARACION_MIN), SEPARACION_MAX)
        s_i["y_numeros"] = s_i["y"] + ALTO_PENTAGRAMA + sep

    for r in registros:
        r["y"] = sistemas[r["sistema"]]["y_numeros"] if sistemas else 0.0

    modo, acierto = _referencias(registros, margen_izq, ancho_pagina)
    if acierto < 0.8:
        # Ninguna interpretación deja los números dentro de la página: mejor
        # no estampar nada y que quien llame recurra a redibujar.
        return None

    for r in registros:
        if r["dx"] is None:
            r["x"] = None
        elif modo == "medida":
            r["x"] = r["x_medida"] + r["dx"] + MEDIA_CABEZA
        elif modo == "sistema":
            r["x"] = r["x_sistema"] + r["dx"] + MEDIA_CABEZA
        else:
            r["x"] = r["dx"] + MEDIA_CABEZA

    elegidas = elegir_posiciones(
        [Nota(midi=r["midi"], offset=r["offset"], duracion=r["duracion"])
         for r in registros],
        transpositor, preferir_cercanas, peso_movimiento)

    # Las notas fuera del alcance del instrumento llevan un "?" en lugar de
    # quedarse sin nada: así se ve que la herramienta las ha leído y que el
    # hueco es intencionado.
    marcas = [
        Marca(pagina=r["pagina"], x=r["x"], y=r["y"],
              texto=(p.etiqueta if p is not None else "?"),
              sistema=r["sistema"])
        for r, p in zip(registros, elegidas)
        if r["x"] is not None
    ]

    return Plano(
        marcas=marcas,
        ancho_pagina=ancho_pagina,
        alto_pagina=alto_pagina,
        mm_por_decimo=mm_por_decimo,
        espacio_decimos=10.0,
        sin_coordenada=len(registros) - len(utiles),
        total=len(registros),
        modo_x=modo,
        acierto=acierto,
        tramos={i: (s_i["x_ini"], s_i["x_fin"]) for i, s_i in enumerate(sistemas)},
        sistemas_pagina=_por_pagina(sistemas),
    )


def _por_pagina(sistemas: List[dict]) -> Dict[int, List[int]]:
    salida: Dict[int, List[int]] = {}
    for i, s_i in enumerate(sistemas):
        salida.setdefault(s_i["pagina"], []).append(i)
    return salida


# --- dónde están de verdad los pentagramas -----------------------------------

# Audiveris declara un tamaño de página y unas distancias entre sistemas que no
# siempre son coherentes entre sí: en pruebas reales la escala vertical salía
# desviada un 10%. En vez de fiarnos, miramos el PDF y localizamos las líneas.

FRANJAS = ((0.30, 0.70), (0.20, 0.80), (0.40, 0.60))
UMBRAL_TINTA = 200
FRACCIONES = (0.70, 0.60, 0.50, 0.40, 0.30, 0.22)
REGULARIDAD = 2.2          # cuánto pueden diferir los espacios de un pentagrama


def _agrupar_lineas(tinta, alto_px: float, px_por_pt: float,
                    fraccion: float) -> List[float]:
    umbral = tinta.max() * fraccion
    filas = [y for y in range(int(alto_px)) if tinta[y] > umbral]
    if not filas:
        return []
    grupos, actual = [], [filas[0]]
    for y in filas[1:]:
        if y - actual[-1] <= 3:
            actual.append(y)
        else:
            grupos.append(actual)
            actual = [y]
    grupos.append(actual)
    return [(g[0] + g[-1]) / 2 / px_por_pt for g in grupos]


def _extremos(a, sup_px: int, inf_px: int, px_por_pt: float) -> Tuple[float, float]:
    """
    Dónde empieza y acaba la línea del pentagrama, en puntos.

    Buscamos el tramo continuo de tinta más largo en lugar del primer y el
    último píxel oscuro: así una mancha suelta o un borde de página no nos
    estiran la medida, y a la vez respetamos las partituras impresas sin
    márgenes, donde el pentagrama sí llega al filo del papel.
    """
    import numpy as np

    def tramo(fila) -> Tuple[int, int, int]:
        cols = np.flatnonzero(fila)
        if cols.size < 10:
            return 0, 0, 0
        # Toleramos huecos pequeños (barras de compás, claves, tinta fina).
        cortes = np.flatnonzero(np.diff(cols) > 8)
        inicio = 0
        mejor = (0, 0, 0)
        for fin in list(cortes) + [cols.size - 1]:
            largo = cols[fin] - cols[inicio]
            if largo > mejor[2]:
                mejor = (cols[inicio], cols[fin], largo)
            inicio = fin + 1
        return mejor

    candidatos = []
    for y in (sup_px, inf_px):
        banda = a[max(y - 1, 0):y + 2, :] < UMBRAL_TINTA
        if banda.size:
            candidatos.append(tramo(banda.any(axis=0)))

    candidatos = [c for c in candidatos if c[2] > 0]
    if not candidatos:
        return 0.0, 0.0

    izq, der, _ = max(candidatos, key=lambda c: c[2])
    return izq / px_por_pt, der / px_por_pt


def _mejores_cinco(grupo: List[float]) -> Optional[List[float]]:
    """
    Saca las cinco líneas de un pentagrama de un grupo que puede traer alguna
    de más.

    Un ligado largo, una barra de corchea gruesa o una línea adicional se
    pegan al pentagrama y el grupo acaba con seis o siete líneas. Antes
    descartábamos el grupo entero y perdíamos ese sistema. Ahora buscamos
    dentro las cinco consecutivas mejor repartidas, que son las de verdad.
    """
    if len(grupo) < 5:
        return None

    mejor, mejor_var = None, None
    for i in range(len(grupo) - 4):
        cinco = grupo[i:i + 5]
        pasos = [cinco[m + 1] - cinco[m] for m in range(4)]
        if min(pasos) <= 0:
            continue
        media = sum(pasos) / 4
        var = sum((p - media) ** 2 for p in pasos) / 4
        # Normalizamos para no premiar simplemente a los más juntos.
        relativa = var / (media ** 2)
        if mejor_var is None or relativa < mejor_var:
            mejor, mejor_var = cinco, relativa

    if mejor is None:
        return None
    pasos = [mejor[m + 1] - mejor[m] for m in range(4)]
    if max(pasos) > min(pasos) * REGULARIDAD:
        return None
    return mejor


def _a_pentagramas(lineas: List[float]) -> List[Tuple[float, float]]:
    """
    Agrupa las líneas sueltas en pentagramas.

    No vale partir la lista de cinco en cinco: basta que se cuele una línea de
    más, o que falte una, para que todo lo demás quede descolocado. Lo que sí
    es constante es la geometría: las cinco líneas de un pentagrama están
    equiespaciadas, y del último al primero del siguiente hay un salto mucho
    mayor. Agrupamos por eso y nos quedamos solo con los grupos de cinco.
    """
    if len(lineas) < 5:
        return []

    huecos = [lineas[i + 1] - lineas[i] for i in range(len(lineas) - 1)]
    if not huecos:
        return []

    # La separación entre líneas de un mismo pentagrama es la dominante: hay
    # cuatro por sistema frente a un solo salto entre sistemas.
    pequenos = sorted(huecos)[:max(int(len(huecos) * 0.8), 1)]
    espaciado = pequenos[len(pequenos) // 2]
    if espaciado <= 0:
        return []
    corte = espaciado * 1.9

    grupos, actual = [], [lineas[0]]
    for i in range(1, len(lineas)):
        if lineas[i] - lineas[i - 1] <= corte:
            actual.append(lineas[i])
        else:
            grupos.append(actual)
            actual = [lineas[i]]
    grupos.append(actual)

    salida = []
    for g in grupos:
        cinco = _mejores_cinco(g)
        if cinco is None:
            continue
        salida.append((cinco[0], cinco[4]))

    if not salida:
        return []

    # Todos los pentagramas de una página miden lo mismo.
    altos = [b - a for a, b in salida]
    if max(altos) > min(altos) * 1.6:
        return []
    return salida


def detectar_pentagramas(pdf: Path, indice: int, alto_pt: float,
                         esperados: Optional[int] = None,
                         dpi: int = 150) -> List[Tuple[float, float, float, float]]:
    """
    Devuelve [(superior, inferior, izquierda, derecha)] de cada pentagrama de
    la página, en puntos. Lista vacía si no se reconoce una estructura clara.

    Probamos varios umbrales de tinta y varias franjas horizontales, porque el
    grosor de las líneas y lo cargada que esté la página cambian mucho de una
    partitura a otra. Si sabemos cuántos sistemas esperamos, nos quedamos con
    la combinación que dé ese número exacto.
    """
    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError:
        return []

    try:
        doc = pdfium.PdfDocument(str(pdf))
        imagen = doc[indice].render(scale=dpi / 72.0).to_pil().convert("L")
        doc.close()
    except Exception:  # noqa: BLE001
        return []

    a = np.asarray(imagen)
    alto_px, ancho_px = a.shape
    px_por_pt = alto_px / alto_pt

    candidatos: List[List[Tuple[float, float]]] = []
    for franja in FRANJAS:
        i, j = int(ancho_px * franja[0]), int(ancho_px * franja[1])
        tinta = (a[:, i:j] < UMBRAL_TINTA).sum(axis=1)
        if tinta.max() == 0:
            continue
        for fraccion in FRACCIONES:
            p = _a_pentagramas(_agrupar_lineas(tinta, alto_px, px_por_pt, fraccion))
            if not p:
                continue
            if esperados is not None and len(p) == esperados:
                return [(sup, inf) + _extremos(a, int(sup * px_por_pt),
                                               int(inf * px_por_pt), px_por_pt)
                        for sup, inf in p]
            candidatos.append(
                [(sup, inf) + _extremos(a, int(sup * px_por_pt),
                                        int(inf * px_por_pt), px_por_pt)
                 for sup, inf in p])

    if esperados is not None:
        return []
    return candidatos[0] if candidatos else []


# --- estampado sobre el PDF -------------------------------------------------

# Cuerpo de letra de los números, en espacios de pentagrama. En pasajes
# rápidos las notas van a dos espacios unas de otras, así que la cifra tiene
# que encoger o se solapan entre sí.
CUERPO_MAX = 2.0
CUERPO_MIN = 1.25
OCUPACION = 0.80      # parte del hueco entre notas que puede ocupar la cifra


def _cuerpo_por_sistema(marcas: List[Marca], espacio: float) -> float:
    """Elige el tamaño mayor que no haga que los números se toquen."""
    xs = sorted(m.x for m in marcas)
    huecos = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 0.5]
    if not huecos:
        return CUERPO_MAX * espacio
    apretado = sorted(huecos)[max(len(huecos) // 20, 0)]   # el 5% más juntos
    # Una cifra ocupa aproximadamente 0,55 de su cuerpo de ancho.
    cuerpo = apretado * OCUPACION / 0.55
    return min(max(cuerpo, CUERPO_MIN * espacio), CUERPO_MAX * espacio)


def estampar(pdf_original: Path, pdf_salida: Path, plano: Plano,
             tamano_relativo: float = 2.0,
             fraccion_hueco: float = 0.60) -> Tuple[int, bool]:
    """
    Dibuja los números sobre el PDF original. Devuelve (cuántos, si se han
    usado los pentagramas medidos en el propio PDF).

    Las X vienen de Audiveris, que en eso acierta. Las Y salen de medir el
    PDF, porque la escala vertical que declara Audiveris no es de fiar.
    """
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    lector = PdfReader(str(pdf_original))
    escritor = PdfWriter()

    por_pagina: Dict[int, List[Marca]] = {}
    for m in plano.marcas:
        por_pagina.setdefault(m.pagina, []).append(m)

    puestas = 0
    medido = False

    for indice, pagina in enumerate(lector.pages):
        marcas = por_pagina.get(indice)
        giro = int(pagina.get("/Rotate") or 0) % 360

        if marcas:
            caja = pagina.mediabox
            ancho_caja = float(caja.width)
            alto_caja = float(caja.height)

            # Muchos PDF guardan la página en vertical y le ponen /Rotate para
            # que el visor la muestre apaisada. Todo el cálculo se hace sobre
            # la página tal como se ve, y al dibujar deshacemos el giro.
            if giro in (90, 270):
                ancho_pt, alto_pt = alto_caja, ancho_caja
            else:
                ancho_pt, alto_pt = ancho_caja, alto_caja

            fx = ancho_pt / plano.ancho_pagina
            fy = alto_pt / plano.alto_pagina

            # Posiciones de respaldo, calculadas con la escala de Audiveris.
            alturas = {m.sistema: m.y * fy for m in marcas}
            escalas = {}

            sistemas = sorted({m.sistema for m in marcas},
                              key=lambda s: min(x.y for x in marcas if x.sistema == s))

            # Emparejamos contra TODOS los sistemas de la página, no solo
            # contra los que llevan notas: si uno va entero de silencios, el
            # pentagrama existe igual y no debe descolocar a los siguientes.
            en_pagina = (plano.sistemas_pagina or {}).get(indice, sistemas)
            pentagramas = detectar_pentagramas(pdf_original, indice, alto_pt,
                                               esperados=len(en_pagina))

            if len(pentagramas) == len(en_pagina):
                medido = True
                for k, s in enumerate(en_pagina):
                    sup, inf, izq, der = pentagramas[k]
                    siguiente = pentagramas[k + 1][0] if k + 1 < len(pentagramas) else None
                    hueco = (siguiente - inf) if siguiente else (inf - sup) * 1.6
                    alturas[s] = inf + hueco * fraccion_hueco

                    # Calibración horizontal: estiramos el tramo que Audiveris
                    # dice que ocupa el sistema hasta los extremos reales de su
                    # pentagrama. Sin esto la escala se desvía varios puntos por
                    # compás y los números se van separando de sus notas.
                    tramo = (plano.tramos or {}).get(s)
                    if tramo and der > izq and tramo[1] > tramo[0]:
                        factor = (der - izq) / (tramo[1] - tramo[0])
                        # Red de seguridad: si la corrección es disparatada es
                        # que hemos medido mal algún extremo, y preferimos la
                        # escala de siempre a estampar los números en cualquier
                        # sitio.
                        if 0.8 <= factor / fx <= 1.25:
                            escalas[s] = (tramo[0], izq, factor)

            if pentagramas:
                espacio = (pentagramas[0][1] - pentagramas[0][0]) / 4.0
            else:
                espacio = plano.espacio_decimos * fy

            # Ya en puntos de la página vista, para medir lo juntas que van.
            colocadas = []
            for m in marcas:
                cal = escalas.get(m.sistema)
                if cal:
                    origen, izq, factor = cal
                    x = izq + (m.x - origen) * factor
                else:
                    x = m.x * fx
                colocadas.append((m, x))

            cuerpos = {}
            for s in sistemas:
                suyas = [Marca(m.pagina, x, m.y, m.texto, m.sistema)
                         for m, x in colocadas if m.sistema == s]
                cuerpos[s] = _cuerpo_por_sistema(suyas, espacio)

            buffer = BytesIO()
            lienzo = canvas.Canvas(buffer, pagesize=(ancho_caja, alto_caja))
            lienzo.setFillGray(0.0)
            for m, x_vista in colocadas:
                # En PDF la Y crece hacia arriba; nosotros medimos hacia abajo.
                y_vista = alto_pt - alturas[m.sistema]
                if giro == 90:
                    x_hoja, y_hoja = ancho_caja - y_vista, x_vista
                elif giro == 180:
                    x_hoja, y_hoja = ancho_caja - x_vista, alto_caja - y_vista
                elif giro == 270:
                    x_hoja, y_hoja = y_vista, alto_caja - x_vista
                else:
                    x_hoja, y_hoja = x_vista, y_vista

                lienzo.saveState()
                lienzo.translate(x_hoja, y_hoja)
                if giro:
                    lienzo.rotate(giro)
                lienzo.setFont("Helvetica",
                               cuerpos.get(m.sistema, espacio * tamano_relativo))
                lienzo.drawCentredString(0, 0, m.texto)
                lienzo.restoreState()
                puestas += 1
            lienzo.save()
            buffer.seek(0)

            pagina.merge_page(PdfReader(buffer).pages[0])

        escritor.add_page(pagina)

    with open(pdf_salida, "wb") as f:
        escritor.write(f)

    return puestas, medido
