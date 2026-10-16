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

from collections import Counter

from posiciones import Nota, elegir_posiciones

# Un pentagrama de cinco líneas mide cuatro espacios, y un espacio son diez
# décimos. Esta es la unidad en la que MusicXML mide todo.
ALTO_PENTAGRAMA = 40.0

# Dónde caen los números dentro del hueco que queda entre un pentagrama y el
# siguiente. No puede ser una distancia fija: en una partitura apretada los
# sistemas van a 60 décimos y en una espaciada a 200. Los ponemos en la parte
# baja del hueco, lejos de los matices y los reguladores, que viven pegados al
# pentagrama, pero sin llegar a tocar el sistema de abajo.
FRACCION_HUECO = 0.50
SEPARACION_MIN = 18.0
SEPARACION_MAX = 90.0
HUECO_POR_DEFECTO = 60.0

# Una cabeza de nota mide algo más de un espacio de ancho. `default-x` apunta
# a su borde izquierdo, así que desplazamos para centrar el número debajo.
MEDIA_CABEZA = 6.5

PASOS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
LETRAS = "CDEFGAB"

# Nota de referencia de cada clave y en qué línea se apoya, contando la de
# abajo como la 1. El índice es diatónico: octava * 7 + grado, con Do = 0.
#   sol en 2ª -> Sol4 ; fa en 4ª -> Fa3 ; do en 4ª (tenor) -> Do4
REFERENCIA_CLAVE = {"G": (4 * 7 + 4, 2), "F": (3 * 7 + 3, 4), "C": (4 * 7 + 0, 3)}
CLAVE_POR_DEFECTO = ("F", 4, 0)     # signo, línea, cambio de octava

# Cómo se lee la particella. Audiveris no puede saberlo: lee la altura
# escrita y punto. Pero una parte de "Trombón 1º en Sib" en clave de sol
# suena una novena mayor más grave de lo que se ve, y sin esto TODOS los
# números salen mal sin que nada avise, porque las alturas escritas caen
# dentro del registro y el validador no protesta.
LECTURAS = {
    "auto": None,          # deducir de la clave
    "do-fa": 0,            # lo normal: clave de fa, suena lo escrito
    "do-sol": 0,           # clave de sol, en Do
    "sib-sol": -14,        # clave de sol, en Sib: novena mayor abajo
    "octava-baja": -12,    # escrita una octava por encima de lo que suena
}


# Hasta qué punto nos fiamos de un número. No se descarta nada por tener
# poca confianza: se estampa más claro, para que al leer la partitura se vea
# de un vistazo qué conviene repasar. Un número que falta no se nota; uno
# gris sí.
CONFIANZA_X_ESTIMADA = 0.75    # la coordenada la hemos deducido nosotros
CONFIANZA_FANTASMA = 0.50      # la nota no está en el MusicXML
FACTOR_SIN_MEDIR = 0.80        # la página no tiene pentagramas fiables
FACTOR_SIN_CABEZA = 0.85       # no hay cabeza dibujada que lo confirme
CONFIANZA_DUDOSA = 0.70        # por debajo de esto se cuenta como "a repasar"


@dataclass
class Marca:
    """Un número listo para estampar, en décimos desde la esquina superior."""
    pagina: int
    x: float
    y: float
    texto: str
    sistema: int = 0
    estimada: bool = False   # la nota la hemos leído del PDF, no del OMR
    confianza: float = 1.0


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
    rellenadas: int = 0          # notas que solo existen en el PDF, no en el OMR
    sin_x: int = 0               # notas descartadas por no tener x reconstruible
    # Pentagramas que Audiveris ya había medido, en puntos y por página.
    # Cuando están, evitan el peor acantilado que tiene esto: que la
    # detección por imagen no acierte el número de sistemas de una página.
    pentagramas_omr: Optional[Dict[int, List[Tuple[float, float, float, float]]]] = None


# --- lectura del MusicXML ---------------------------------------------------

def _f(texto: Optional[str], defecto: float = 0.0) -> float:
    try:
        return float(texto)
    except (TypeError, ValueError):
        return defecto


def _clave(elemento: Optional[ET.Element]) -> Optional[Tuple[str, int, int]]:
    """(signo, línea, cambio de octava) de un <clef>."""
    if elemento is None:
        return None
    signo = (elemento.findtext("sign") or "").strip().upper()
    if signo not in REFERENCIA_CLAVE:
        return None
    _, linea_defecto = REFERENCIA_CLAVE[signo]
    linea = int(_f(elemento.findtext("line"), linea_defecto))
    octava = int(_f(elemento.findtext("clef-octave-change"), 0))
    return signo, linea, octava


def _transposicion_declarada(elemento: Optional[ET.Element]) -> Optional[int]:
    """Semitonos del <transpose> de un <attributes>, si lo trae."""
    if elemento is None:
        return None
    t = elemento.find("transpose")
    if t is None:
        return None
    return int(_f(t.findtext("chromatic"), 0)
               + 12 * _f(t.findtext("octave-change"), 0))


def _lectura_automatica(raiz: ET.Element) -> int:
    """
    Cuántos semitonos hay que bajar lo escrito para saber lo que suena.

    Sin `<transpose>` en el archivo —y Audiveris nunca lo pone, porque no
    tiene forma de saberlo— lo único que queda es la clave. Una particella
    de trombón en clave de sol es, en la práctica, una parte en Sib: así se
    escriben las de banda. En clave de fa o de do se lee lo que suena.
    """
    for clef in raiz.iter("clef"):
        c = _clave(clef)
        if c and c[0] == "G":
            return LECTURAS["sib-sol"]
    return 0


def _midi_de_grado(idx: int, fifths: int = 0) -> Optional[int]:
    """MIDI de un grado diatónico absoluto, aplicándole la armadura."""
    octava, paso = divmod(idx, 7)
    letra = LETRAS[paso]
    midi = ((octava + 1) * 12 + PASOS[letra]
            + _alteracion_armadura(letra, fifths))
    return midi if 0 <= midi <= 127 else None


def _grado_linea_inferior(clave: Tuple[str, int, int]) -> int:
    """Grado diatónico que cae sobre la línea de abajo del pentagrama."""
    signo, linea, octava = clave
    referencia, _ = REFERENCIA_CLAVE.get(signo, REFERENCIA_CLAVE["F"])
    return referencia - 2 * (linea - 1) + 7 * octava


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



def _recta(muestras: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Ajusta y = a + b·x por mínimos cuadrados. None si no da."""
    n = len(muestras)
    if n < 2:
        return None
    sx = sy = sxx = sxy = 0.0
    for x, y in muestras:
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    b = (n * sxy - sx * sy) / den
    return (sy - b * sx) / n, b


# Cómo reparte el grabador el ancho de un compás entre sus notas. No es
# proporcional a la duración: una blanca no ocupa cuatro veces lo que una
# corchea, ni de lejos. Cada programa usa su ley, así que probamos varias y
# nos quedamos con la que mejor explica las notas que SÍ traen coordenada en
# esta partitura concreta. Es la diferencia entre acertar de casualidad en
# las partituras de negras y acertar siempre.
import math as _math

LEYES = (
    ("potencia-0.0", lambda d: 1.0),                       # todas igual
    ("potencia-0.3", lambda d: max(d, 1e-6) ** 0.30),
    ("potencia-0.45", lambda d: max(d, 1e-6) ** 0.45),
    ("potencia-0.6", lambda d: max(d, 1e-6) ** 0.60),
    ("potencia-0.8", lambda d: max(d, 1e-6) ** 0.80),
    ("proporcional", lambda d: max(d, 1e-6)),
    ("logaritmica", lambda d: 1.0 + _math.log2(max(d, 1e-6) / 0.125)),
)


def _avance(rs: List[dict], ley) -> List[float]:
    """
    Qué fracción del compás lleva recorrida cada nota, según una ley de
    espaciado. `rs` tiene que venir ordenado por tiempo.
    """
    anchos = [max(ley(r["duracion"]), 0.0) for r in rs]
    total = sum(anchos)
    if total <= 0:
        n = max(len(rs) - 1, 1)
        return [i / n for i in range(len(rs))]
    salida, acumulado = [], 0.0
    for a in anchos:
        salida.append(acumulado / total)
        acumulado += a
    return salida


def _estimar_dx(registros: List[dict]) -> int:
    """
    Rellena las coordenadas que Audiveris no ha dado, compás a compás.

    Antes se interpolaba linealmente por tiempo entre la primera y la última
    nota del SISTEMA con coordenada. Un sistema son cuatro o cinco compases,
    y dentro de ellos la música no se reparte de forma uniforme: un compás de
    corcheas ocupa lo mismo que uno de redondas. El resultado era que una
    nota sin coordenada acababa a decenas de puntos de su cabeza, el número
    caía encima de otra nota y el filtro de duplicados lo tiraba. Ahí es
    donde se comían las notas.

    De un compás sí sabemos dos cosas exactas: dónde empieza y cuánto mide,
    porque el MusicXML las trae. Así que el error se acota a un compás en
    lugar de a un sistema. Dentro del compás la relación entre tiempo y
    distancia es casi una recta, y además se ajusta con las notas del propio
    compás que sí traen coordenada.

    Devuelve cuántas ha tenido que estimar.
    """
    por_medida: Dict[int, List[dict]] = {}
    for r in registros:
        por_medida.setdefault(r["medida"], []).append(r)

    for rs in por_medida.values():
        rs.sort(key=lambda r: r["offset"])

    # Qué ley de espaciado usa esta partitura. Se elige por la que menos se
    # equivoca al predecir las notas que sí traen coordenada: un ajuste a los
    # datos reales, no una constante elegida a ojo en un despacho.
    mejor_ley, mejor_modelo, mejor_error = None, None, None
    for nombre, ley in LEYES:
        muestras: List[Tuple[float, float]] = []
        for rs in por_medida.values():
            ancho = rs[0].get("ancho_medida") or 0.0
            if ancho <= 0:
                continue
            avances = _avance(rs, ley)
            for r, u in zip(rs, avances):
                if r["dx"] is not None:
                    muestras.append((u, r["dx"] / ancho))
        modelo = _recta(muestras)
        if modelo is None:
            continue
        a, b = modelo
        error = sum((y - (a + b * u)) ** 2 for u, y in muestras) / len(muestras)
        if mejor_error is None or error < mejor_error:
            mejor_ley, mejor_modelo, mejor_error = ley, modelo, error

    if mejor_ley is None:
        mejor_ley = LEYES[3][1]
        mejor_modelo = (0.10, 0.78)
    general = mejor_modelo

    estimadas = 0
    for rs in por_medida.values():
        faltan = [r for r in rs if r["dx"] is None]
        if not faltan:
            continue
        ancho = rs[0].get("ancho_medida") or 0.0
        if ancho <= 0:
            continue    # sin ancho de compás no hay nada que escalar
        avances = dict(zip((id(r) for r in rs), _avance(rs, mejor_ley)))
        conocidas = [(avances[id(r)], r["dx"])
                     for r in rs if r["dx"] is not None]

        propia = _recta(conocidas) if len(conocidas) >= 2 else None
        if propia is not None:
            a, b = propia
        elif conocidas:
            # Una sola referencia: se usa la pendiente general y se desplaza
            # la recta para que pase por ella.
            b = general[1] * ancho
            t1, dx1 = conocidas[0]
            a = dx1 - b * t1
        else:
            a, b = general[0] * ancho, general[1] * ancho

        for r in faltan:
            dx = a + b * avances[id(r)]
            r["dx"] = min(max(dx, -0.05 * ancho), 1.05 * ancho)
            r["dx_estimada"] = True
            estimadas += 1

        # Dentro de un compás las notas van de izquierda a derecha. Si el
        # ajuste sale torcido (pocas muestras, un compás raro), al menos que
        # no se crucen entre sí.
        for i in range(1, len(rs)):
            if rs[i]["dx"] < rs[i - 1]["dx"]:
                rs[i]["dx"] = rs[i - 1]["dx"]

    return estimadas


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
                    and registros[j]["pagina"] == registros[k]["pagina"]):
                dx1 = registros[j]["dx"]
                dx2 = registros[k]["dx"]
                total = k - j
                paso = (dx2 - dx1) / total if total > 0 else 0
                for t in range(1, total):
                    if registros[j + t].get("dx") is None:
                        registros[j + t]["dx"] = dx1 + paso * t
                i = k
                continue
        if j >= 0:
            # copiar hacia delante hasta encontrar válido o fin sistema
            dx1 = registros[j]["dx"]
            t = 1
            while j + t < n and registros[j + t].get("dx") is None and t <= 20:
                registros[j + t]["dx"] = dx1
                t += 1
            i = j + t
            continue
        if k < n:
            dxk = registros[k]["dx"]
            t = 0
            while i + t < k and t <= 20:
                if registros[i + t].get("dx") is None:
                    registros[i + t]["dx"] = dxk
                t += 1
            i = k
            continue
        i += 1


def preparar(ruta_musicxml, transpositor: bool = False,
             preferir_cercanas: bool = False,
             peso_movimiento=None,
             pdf: Optional[Path] = None,
             diag: Optional[dict] = None,
             libro_omr=None,
             lectura: str = "auto") -> Optional[Plano]:
    """
    Recorre el MusicXML reconstruyendo la posición de cada nota en la página.
    Devuelve None si no hay coordenadas utilizables.

    Si se pasa `diag`, se rellena con el motivo exacto de la renuncia. Sin
    eso, un None es indistinguible de otro y no hay forma de saber qué PDF
    falla por qué.
    """
    def rendirse(motivo: str):
        if diag is not None:
            diag["motivo"] = motivo
        return None

    arbol = ET.parse(ruta_musicxml) if isinstance(ruta_musicxml, Path) else ruta_musicxml
    raiz = arbol.getroot()

    partes = raiz.findall("part")
    if not partes:
        return rendirse("sin_partes")
    if len(partes) > 1:
        # Con varias partes habría que seguir también la separación entre
        # pentagramas. Pero lo normal cuando Audiveris saca varias de una
        # particella es que sobren: una con la música y el resto vacías de
        # un pentagrama mal segmentado. Antes se renunciaba y la partitura
        # se redibujaba entera; ahora nos quedamos con la que tiene notas,
        # y solo si hay más de una con música se deja estar.
        con_notas = [p for p in partes if p.find(".//note/pitch") is not None]
        if len(con_notas) != 1:
            return rendirse("varias_partes_con_musica:%d" % len(con_notas))
        partes = con_notas

    defaults = raiz.find("defaults")
    if defaults is None:
        return rendirse("sin_defaults")

    esc = defaults.find("scaling")
    pl_global = defaults.find("page-layout")
    if esc is None or pl_global is None:
        return rendirse("sin_escala_o_disposicion")

    mm = _f(esc.findtext("millimeters"))
    tenths = _f(esc.findtext("tenths"))
    if mm <= 0 or tenths <= 0:
        return rendirse("escala_invalida")
    mm_por_decimo = mm / tenths

    ancho_pagina = _f(pl_global.findtext("page-width"))
    alto_pagina = _f(pl_global.findtext("page-height"))
    if ancho_pagina <= 0 or alto_pagina <= 0:
        return rendirse("pagina_sin_medidas")

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
    fifths = 0                # armadura vigente; cambia si el compás lo pide
    clave = CLAVE_POR_DEFECTO
    primera = True

    # Lo que suena = lo escrito + esto. El <transpose> del archivo manda si
    # está; si no, lo elige quien llama, y "auto" lo deduce de la clave.
    semitonos = LECTURAS.get(lectura)
    if semitonos is None:
        semitonos = _lectura_automatica(raiz)
    declarada = _transposicion_declarada(raiz.find(".//attributes"))
    if declarada:
        semitonos = declarada
    if diag is not None:
        diag["lectura"] = lectura
        diag["semitonos"] = semitonos

    registros: List[dict] = []
    descansos: List[dict] = []
    sin_coordenada = 0
    indice_medida = -1

    for medida in partes[0].findall("measure"):
        indice_medida += 1
        offset_medida = offset
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
        # `ancho_anterior` ya es el ancho de ESTE compás: las notas de abajo
        # lo necesitan para situarse dentro de él.

        grupo: List[dict] = []

        def cerrar_grupo() -> None:
            if not grupo:
                return
            mejor = max(grupo, key=lambda r: r["midi"])
            # La x del acorde es la de su cabeza principal; si esa no traía
            # coordenada nos quedamos con la de la nota elegida en vez de
            # perderla y tener que interpolarla después.
            if grupo[0]["dx"] is not None:
                mejor["dx"] = grupo[0]["dx"]
            registros.append(mejor)
            grupo.clear()

        for hijo in medida:
            if hijo.tag == "attributes":
                div = hijo.findtext("divisions")
                if div:
                    divisiones = _f(div, divisiones) or 1.0
                kk = hijo.find("key/fifths")
                if kk is not None and (kk.text or "").strip():
                    fifths = int(_f(kk.text, fifths))
                nueva_clave = _clave(hijo.find("clef"))
                if nueva_clave is not None:
                    clave = nueva_clave

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
                    rdx = hijo.get("default-x")
                    if rdx is not None:
                        # Los silencios no llevan número, pero su posición
                        # sirve para no confundirlos con una nota olvidada.
                        descansos.append({
                            "dx": _f(rdx), "x_medida": x_medida,
                            "x_sistema": x_sistema, "pagina": pagina,
                            "sistema": max(sistema, 0)})
                    offset += duracion
                    continue

                midi = _midi(hijo)
                if midi is None:
                    offset += 0 if en_acorde else duracion
                    continue

                dx = hijo.get("default-x")
                if dx is None:
                    sin_coordenada += 1

                # Ligaduras: mover la vara dentro del mismo armónico ligando
                # suena a glissando. La vía "redibujar" ya lo evitaba; esta
                # no se enteraba.
                nots = hijo.find("notations")
                ligada = False
                if nots is not None:
                    ligada = any(
                        e.get("type") in ("stop", "continue")
                        for e in list(nots.findall("slur"))
                        + list(nots.findall("tied")))

                grupo.append({
                    # Lo que suena, que es lo que determina la vara. Lo
                    # escrito solo sirve para dibujar.
                    "midi": midi + semitonos,
                    "midi_escrito": midi,
                    "ligada": ligada,
                    "medida": indice_medida,
                    "offset_medida": offset_medida,
                    "ancho_medida": ancho_anterior,
                    "offset": offset,
                    "duracion": duracion,
                    "pagina": pagina,
                    "dx": None if dx is None else _f(dx),
                    "x_medida": x_medida,
                    "x_sistema": x_sistema,
                    "sistema": max(sistema, 0),
                    "fifths": fifths,
                    "clave": clave,
                })

                if not en_acorde and not adorno:
                    offset += duracion

        cerrar_grupo()

    if not registros:
        return rendirse("sin_notas_en_el_musicxml")

    # Con varias voces, <backup> hace retroceder el reloj y los registros
    # quedan en orden de documento, no de lectura. Reordenamos antes de
    # interpolar coordenadas y antes de resolver la secuencia de posiciones:
    # si no, el Viterbi encadena notas que no van seguidas y la interpolación
    # de dx cruza de una voz a otra.
    registros.sort(key=lambda r: (r["pagina"], r["sistema"], r["offset"]))

    sin_coordenada = sum(1 for r in registros if r["dx"] is None)
    if sin_coordenada == len(registros):
        return rendirse("ninguna_nota_trae_default_x")
    estimadas_dx = _estimar_dx(registros)
    # Red de seguridad para los compases sin ancho declarado, que el
    # estimador no puede escalar: ahí seguimos tirando de los vecinos.
    _rellenar_dx(registros)
    registros.sort(key=lambda r: (r["pagina"], r["sistema"], r["offset"]))
    if diag is not None:
        diag["dx_estimadas"] = estimadas_dx

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

    pent_omr = _pentagramas_del_omr(libro_omr, mm_por_decimo, diag)

    modo, acierto = _referencias(registros, margen_izq, ancho_pagina)
    if diag is not None:
        diag["modo_x"] = modo
        diag["encaje"] = acierto
    if acierto < 0.8 and not pent_omr:
        # Ninguna interpretación de las coordenadas deja los números dentro
        # de la página. Sin nada más de lo que tirar, mejor no estampar y
        # que quien llame recurra a redibujar.
        #
        # Con el .omr delante la cosa cambia: ahí están los pentagramas
        # medidos por Audiveris, así que los números se pueden recolocar
        # aunque el MusicXML traiga las coordenadas raras.
        return rendirse("encaje_bajo:%.2f(%s)" % (acierto, modo))

    # Fallback dx: para cada sistema, interpolación lineal por offset entre
    # el primer y el último registro con dx válido. Rellena huecos grandes
    # aunque entre medias no haya ninguna coordenada.
    nreg = len(registros)
    por_sis = {}
    for k, r in enumerate(registros):
        por_sis.setdefault((r["pagina"], r["sistema"]), []).append(k)
    for keys, idxs in por_sis.items():
        validos = [(registros[k]["offset"], registros[k]["dx"])
                   for k in idxs if registros[k]["dx"] is not None]
        if not validos:
            # Sin ninguna coordenada fiable en el sistema: mejor no estampar
            # que poner los números en x=0.
            continue
        validos.sort()
        o1, dx1 = validos[0]
        o2, dx2 = validos[-1]
        rango = o2 - o1
        if rango <= 0:
            for k in idxs:
                if registros[k]["dx"] is None:
                    registros[k]["dx"] = dx1
            continue
        for k in idxs:
            if registros[k]["dx"] is not None:
                continue
            t = (registros[k]["offset"] - o1) / rango
            t = min(max(t, 0.0), 1.0)   # ponytail: clamp, extrapolación si hiciera falta
            registros[k]["dx"] = dx1 + (dx2 - dx1) * t

    for r in registros:
        if r["dx"] is None:
            r["x"] = None
            continue
        if modo == "medida":
            r["x"] = r["x_medida"] + r["dx"] + MEDIA_CABEZA
        elif modo == "sistema":
            r["x"] = r["x_sistema"] + r["dx"] + MEDIA_CABEZA
        else:
            r["x"] = r["dx"] + MEDIA_CABEZA

    # Notas que el OMR no llegó a reconocer: en el PDF sí están, así que
    # miramos la imagen, leemos la altura de cada cabeza sin número y la
    # metemos en la secuencia para que el solver decida su posición como a
    # cualquier otra. Se estampan en gris, para distinguir lo leído de lo
    # estimado.
    por_pag = _por_pagina(sistemas)
    if pdf is not None:
        for ancla, g in _fantasmas(pdf, registros, descansos, sistemas,
                                   por_pag, ancho_pagina, modo, semitonos):
            registros.insert(ancla, g)

    elegidas = elegir_posiciones(
        [Nota(midi=r["midi"], offset=r["offset"], duracion=r["duracion"],
              ligada=bool(r.get("ligada")))
         for r in registros],
        transpositor, preferir_cercanas, peso_movimiento)

    # Las notas fuera del alcance del instrumento llevan un "?" en lugar de
    # quedarse sin nada: así se ve que la herramienta las ha leído y que el
    # hueco es intencionado.
    def confianza(r: dict) -> float:
        if r.get("estimada"):
            return CONFIANZA_FANTASMA
        if r.get("dx_estimada"):
            return CONFIANZA_X_ESTIMADA
        return 1.0

    marcas = [
        Marca(pagina=r["pagina"], x=r["x"], y=r["y"],
              texto=(p.etiqueta if p is not None else "?"),
              sistema=r["sistema"], estimada=bool(r.get("estimada")),
              confianza=confianza(r))
        for r, p in zip(registros, elegidas)
        if r["x"] is not None
    ]

    if diag is not None:
        diag["motivo"] = None
        diag["notas"] = [
            {"midi": r["midi"], "offset": r["offset"], "pagina": r["pagina"],
             "sistema": r["sistema"], "dx": r["dx"], "x": r["x"],
             "estimada": bool(r.get("estimada")),
             "pos": (p.etiqueta if p is not None else None)}
            for r, p in zip(registros, elegidas)]
        diag["sistemas"] = [dict(s_i) for s_i in sistemas]

    return Plano(
        marcas=marcas,
        ancho_pagina=ancho_pagina,
        alto_pagina=alto_pagina,
        mm_por_decimo=mm_por_decimo,
        espacio_decimos=10.0,
        sin_coordenada=sin_coordenada,
        total=len(registros),
        modo_x=modo,
        acierto=acierto,
        tramos={i: (s_i["x_ini"], s_i["x_fin"]) for i, s_i in enumerate(sistemas)},
        sistemas_pagina=por_pag,
        rellenadas=sum(1 for r in registros if r.get("estimada")),
        sin_x=sum(1 for r in registros if r["x"] is None),
        pentagramas_omr=pent_omr,
    )


def _pentagramas_del_omr(libro, mm_por_decimo: float, diag: Optional[dict]
                         ) -> Optional[Dict[int, List[Tuple[float, float, float, float]]]]:
    """Pasa a puntos los pentagramas que trae el .omr, si lo hay."""
    if libro is None:
        return None
    try:
        import omr
    except ImportError:
        return None
    salida: Dict[int, List[Tuple[float, float, float, float]]] = {}
    for indice, hoja in enumerate(libro.hojas):
        factor = omr.escala_a_puntos(hoja, mm_por_decimo)
        if factor is None:
            continue
        salida[indice] = omr.pentagramas_en_puntos(hoja, factor)
    if diag is not None:
        diag["pentagramas_omr"] = {k: len(v) for k, v in salida.items()}
    return salida or None


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


_CACHE_PENT: Dict[tuple, List[Tuple[float, float, float, float]]] = {}


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
    # Esto se llamaba dos veces por página (una desde _fantasmas y otra
    # desde estampar), renderizando y probando 18 combinaciones cada vez.
    clave = (str(pdf), indice, round(alto_pt, 2), esperados, dpi)
    if clave in _CACHE_PENT:
        return _CACHE_PENT[clave]

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
                hallado = [(sup, inf) + _extremos(a, int(sup * px_por_pt),
                                                  int(inf * px_por_pt), px_por_pt)
                           for sup, inf in p]
                _CACHE_PENT[clave] = hallado
                return hallado
            candidatos.append(
                [(sup, inf) + _extremos(a, int(sup * px_por_pt),
                                        int(inf * px_por_pt), px_por_pt)
                 for sup, inf in p])

    hallado = ([] if esperados is not None
               else (candidatos[0] if candidatos else []))
    _CACHE_PENT[clave] = hallado
    return hallado


# --- notas que el OMR se comió ----------------------------------------------

# Audiveris no siempre reconoce todas las notas de una pasaje rápida: si se
# salta una, esa nota no existe en el MusicXML y su número no aparece. Aquí
# la buscamos en la imagen —una cabeza sin número, dentro de un pentagrama
# medido, entre dos notas que sí existen— y leemos su altura de la posición
# vertical sobre el pentagrama (en clave de fa la línea inferior es Sol2).
# La x es la medida en el PDF. El número lo decide el solver con el resto de
# la secuencia y sale estampado en gris.


def _alteracion_armadura(letra: str, fifths: int) -> int:
    """Semitono que la armadura aplica a una letra (0, +1 o -1)."""
    if fifths < 0 and letra in "BEADGCF"[:min(-fifths, 7)]:
        return -1
    if fifths > 0 and letra in "FCGDAEB"[:min(fifths, 7)]:
        return 1
    return 0


def _midi_en_clave(y_pt: float, sup: float, inf: float,
                   clave: Tuple[str, int, int] = CLAVE_POR_DEFECTO,
                   fifths: int = 0) -> Optional[int]:
    """
    Altura escrita de una cabeza por su posición sobre el pentagrama.

    Antes esto daba por hecho que la clave era de fa. En una partitura con
    cambio a clave de do en 4ª —habitual en trombón— o en una particella en
    clave de sol, cada nota que hubiera que rescatar de la imagen salía
    varios grados equivocada.

    La armadura también cuenta: sin aplicarla, un Si bemol se lee como Si
    natural y la posición sale mal.
    """
    espacio = (inf - sup) / 4.0
    if espacio <= 0:
        return None
    k = round((inf - y_pt) / (espacio / 2.0))   # medio espacio = un grado
    midi = _midi_de_grado(_grado_linea_inferior(clave) + k, fifths)
    return midi if midi is not None and 20 <= midi <= 100 else None


def _midi_fa(y_pt: float, sup: float, inf: float,
             fifths: int = 0) -> Optional[int]:
    """Compatibilidad: clave de fa en 4ª."""
    return _midi_en_clave(y_pt, sup, inf, CLAVE_POR_DEFECTO, fifths)


def _cabezas(a, sup: float, inf: float, izq: float, der: float,
             escala_x: float, escala_y: float) -> List[Tuple[float, float]]:
    """
    [(x, y)] en puntos de las cabezas de la banda de un pentagrama.

    Se quitan primero las cinco líneas (tinta continua de lado a lado) y
    después, dentro de cada tramo de tinta, se separan los bloques verticales:
    una cabeza es un bloque grueso, y las plicas y las banderas quedan en
    bloques aparte porque entre medias hay una columna de dos píxeles. De los
    bloques con forma de cabeza se queda el más cercano al centro del
    pentagrama, que es la nota; los demás son adornos.
    """
    import numpy as np

    espacio_px = ((inf - sup) / 4.0) * escala_y
    if espacio_px <= 0:
        return []
    y0 = max(int(sup * escala_y - 0.75 * espacio_px), 0)
    y1 = min(int(inf * escala_y + 0.75 * espacio_px) + 1, a.shape[0])
    x0 = max(int(izq * escala_x), 0)
    x1 = min(int(der * escala_x) + 1, a.shape[1])
    if y1 <= y0 or x1 <= x0:
        return []

    tinta = a[y0:y1, x0:x1] < UMBRAL_TINTA
    if not tinta.any():
        return []
    cuerpo = tinta & ~(tinta.sum(axis=1) > 0.55 * tinta.shape[1])[:, None]

    cols = np.flatnonzero(cuerpo.any(axis=0))
    if not cols.size:
        return []
    trozos, inicio, prev = [], cols[0], cols[0]
    for c in cols[1:]:
        if c - prev > 5:
            trozos.append([inicio, prev])
            inicio = c
        prev = c
    trozos.append([inicio, prev])

    # Una cabeza con su siniestra o su armónico pegados es un solo trazo.
    unidos = []
    for t in trozos:
        if unidos and t[0] - unidos[-1][1] < 0.5 * espacio_px:
            unidos[-1][1] = t[1]
        else:
            unidos.append(t)

    centro = (sup + inf) / 2.0 * escala_y - y0
    salidas = []
    for ini, fin in unidos:
        ancho = fin - ini + 1
        if not 0.45 * espacio_px <= ancho <= 2.2 * espacio_px:
            continue
        trozo = cuerpo[:, ini:fin + 1]
        pes = trozo.sum(axis=1)
        if not pes.size or pes.max() == 0:
            continue

        # Bloques verticales: la plica (dos píxeles) separa la cabeza de lo
        # que tenga encima (bandera, ligadura, otro bloque).
        corte = max(2.0, 0.3 * pes.max())
        bloques, actual = [], None
        for y in range(len(pes)):
            if pes[y] > corte:
                actual = [y, y] if actual is None else [actual[0], y]
            elif actual is not None:
                bloques.append(actual)
                actual = None
        if actual is not None:
            bloques.append(actual)

        # La línea del pentagrama se come la fila donde se apoya la cabeza:
        # los trozos resultantes son la misma nota. Un acorde ocupa bloques
        # enteros en espacios distintos, así que solo se une si hay un
        # esquirla (la mitad partida por la línea).
        unidas = []
        for b in bloques:
            if (unidas and b[0] - unidas[-1][1] - 1 <= 2
                    and min(unidas[-1][1] - unidas[-1][0] + 1,
                            b[1] - b[0] + 1) < 0.5 * espacio_px):
                unidas[-1][1] = b[1]
            else:
                unidas.append(b)
        bloques = unidas

        buenos = []
        for ya, yb in bloques:
            alto = yb - ya + 1
            if not 0.4 * espacio_px <= alto <= 1.5 * espacio_px:
                continue
            sub = trozo[ya:yb + 1]
            colsn = int(sub.any(axis=0).sum())
            if not 0.45 * espacio_px <= colsn <= 2.2 * espacio_px:
                continue
            filas_sub = sub.sum(axis=1)
            # Silencios de blanco y de negra: rectángulos cuyos renglones
            # miden todos lo mismo. La cabeza es un óvalo y va estrechando.
            if (filas_sub >= 0.85 * filas_sub.max()).mean() >= 0.75:
                continue
            ys, xs = np.nonzero(sub)
            if not xs.size:
                continue
            buenos.append((ya, yb, xs.mean(), ys.mean(), colsn))

        if not buenos:
            continue
        # El bloque con forma de cabeza más cerca del centro del pentagrama
        # es la nota; los otros son banderas o ligas de la misma plica.
        ya, yb, lx, ly, colsn = min(
            buenos, key=lambda b: abs((b[0] + b[1]) / 2 - centro))
        salidas.append(((x0 + ini + lx) / escala_x,
                        (y0 + ya + ly) / escala_y, colsn / escala_x))
    return salidas


def _texto_pagina(lector, pagina: int, alto_pt: float,
                  orig_x: float = 0.0, cima: Optional[float] = None
                  ) -> List[Tuple[float, float, str]]:
    """[(x, y, run)] en puntos (y de arriba abajo) del texto de una página.

    `orig_x` y `cima` son los bordes izquierdo y superior de la caja que se
    recorta al mostrar: las coordenadas salen relativas a ella, que es la que
    se renderiza y sobre la que medimos los pentagramas.
    """
    if cima is None:
        cima = alto_pt
    salida: List[Tuple[float, float, str]] = []
    try:
        def _vis(texto, cm, tm, fd, fs):
            if not texto or not texto.strip():
                return
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            salida.append((x - orig_x, cima - y, texto.strip()))

        lector.pages[pagina].extract_text(visitor_text=_vis)
    except Exception:  # noqa: BLE001
        return []
    return salida


def _cabezas_texto(pdf: Path, indice: int, giro: int, ancho_pt: float,
                   alto_pt: float, espacio: float
                   ) -> Optional[List[Tuple[float, float]]]:
    """
    Centros de las cabezas de nota de la página, en coordenadas de la vista
    (x a la derecha, y hacia abajo). None si no se reconoce la fuente.

    Una cabeza es un glifo tan alto como un espacio de pentagrama y más o
    menos cuadrado. No miramos el nombre ni el carácter: así vale para
    cualquier fuente (Opus, Anastasia, Sibelius) y cualquier orientación, y no
    se cuelan silencios, claves ni cifras (altos y estrechos todos ellos).
    Nos quedamos con la fuente que más glifos así acumula, que es la de música.
    """
    try:
        import ctypes

        import pypdfium2 as pdfium
        from pypdfium2 import raw as _raw
    except ImportError:
        return None
    if espacio <= 0:
        return None

    if giro in (90, 270):
        ancho_u, alto_u = alto_pt, ancho_pt
    else:
        ancho_u, alto_u = ancho_pt, alto_pt

    try:
        tp = pdfium.PdfDocument(str(pdf))[indice].get_textpage()
    except Exception:  # noqa: BLE001
        return None

    por_fuente: Dict[str, List[Tuple[float, float]]] = {}
    try:
        for i in range(tp.count_chars()):
            b = tp.get_charbox(i)
            w, h = b[2] - b[0], b[3] - b[1]
            if h <= 0 or h < 0.72 * espacio or h > 1.35 * espacio:
                continue
            if not 0.6 <= w / h <= 1.9:
                continue
            buf = ctypes.create_string_buffer(128)
            flg = ctypes.c_int()
            _raw.FPDFText_GetFontInfo(tp.raw, i, buf, 128, ctypes.byref(flg))
            fuente = buf.value.decode("latin1", "replace")
            por_fuente.setdefault(fuente, []).append(
                ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
            )
    finally:
        tp.close()

    grupos = [v for v in por_fuente.values() if len(v) >= 12]
    if not grupos:
        return None    # no reconocemos la fuente: no filtramos nada
    puntos = max(grupos, key=len)

    salida: List[Tuple[float, float]] = []
    for cxu, cyu in puntos:
        if giro == 90:
            salida.append((cyu, cxu))
        elif giro == 270:
            salida.append((ancho_u - cyu, alto_u - cxu))
        elif giro == 180:
            salida.append((ancho_u - cxu, cyu))
        else:
            salida.append((cxu, alto_u - cyu))
    return salida


def _fantasmas(pdf: Path, registros: List[dict], descansos: List[dict],
               sistemas: List[dict],
               sistemas_pagina: Dict[int, List[int]],
               ancho_pagina: float, modo: str,
               semitonos: int = 0) -> List[Tuple[int, dict]]:
    """
    Notas que están en el PDF pero no en el MusicXML.

    Devuelve [(índice donde insertar, registro)] ya ordenado para que quien
    llame pueda insertarlos con `list.insert` sin renumerar nada. Si la
    detección suena demasiado a falsos positivos, no devuelve nada.
    """
    try:
        import numpy as np
        import pypdfium2 as pdfium
        from pypdf import PdfReader
    except ImportError:
        return []
    if not registros or not sistemas:
        return []

    lector = PdfReader(str(pdf))
    doc = pdfium.PdfDocument(str(pdf))
    salida: List[Tuple[int, dict]] = []
    try:
        for pagina in range(min(len(lector.pages), len(doc))):
            en_pagina = sistemas_pagina.get(pagina)
            if not en_pagina:
                continue
            pag = lector.pages[pagina]
            caja = pag.cropbox or pag.mediabox
            ancho_pt, alto_pt = float(caja.width), float(caja.height)
            rotado = int(pag.get("/Rotate") or 0) % 180 == 90
            if rotado:
                ancho_pt, alto_pt = alto_pt, ancho_pt

            # Texto visible: sirve para vetar silencios, cifras y demás blobs
            # que se parecen a una cabeza. Con rotación no traducimos bien las
            # coordenadas y dejamos el comportamiento de antes.
            glifos = [] if rotado else _texto_pagina(
                lector, pagina, alto_pt,
                float(caja.left), float(caja.top))

            pent = detectar_pentagramas(pdf, pagina, alto_pt,
                                        esperados=len(en_pagina))
            if len(pent) != len(en_pagina):
                continue    # sin medición fiable no inventamos nada
            try:
                img = doc[pagina].render(scale=150 / 72).to_pil().convert("L")
            except Exception:  # noqa: BLE001
                continue
            a = np.asarray(img)
            escala_x = a.shape[1] / ancho_pt
            escala_y = a.shape[0] / alto_pt
            fx = ancho_pt / ancho_pagina

            # Alfabeto de cabeza de toda la página: glifos de fuente musical
            # con alto ~un espacio (œ, ˙, w...). Una cabeza que en su sistema
            # no cae junto a ninguna nota del OMR se reconocía mal; aquí se
            # reconoce igual. Fuente + alto evitan colar letras, silencios
            # (altos y estrechos) y alteraciones.
            forma: set = set()
            try:
                import ctypes
                from pypdfium2 import raw as _raw
                tp_txt = doc[pagina].get_textpage()
                for i in range(tp_txt.count_chars()):
                    ch = tp_txt.get_text_range(i, 1)
                    if ch in forma or not ch.strip():
                        continue
                    b = tp_txt.get_charbox(i)
                    buf = ctypes.create_string_buffer(128)
                    flg = ctypes.c_int()
                    _raw.FPDFText_GetFontInfo(tp_txt.raw, i, buf, 128,
                                              ctypes.byref(flg))
                    if "Opus" in buf.value.decode("latin1", "replace") \
                            and 4.3 <= b[3] - b[1] <= 6.3:
                        forma.add(ch)
                tp_txt.close()
            except Exception:  # noqa: BLE001
                forma = set()

            for k, s in enumerate(en_pagina):
                sup, inf, izq, der = pent[k]
                espacio = (inf - sup) / 4.0
                if espacio <= 0:
                    continue
                tramo = (sistemas[s]["x_ini"], sistemas[s]["x_fin"])
                factor = 0.0
                if tramo[1] > tramo[0] and der > izq:
                    f = (der - izq) / (tramo[1] - tramo[0])
                    if 0.8 <= f / fx <= 1.25:
                        factor = f

                def a_pt(xd: float, tramo=tramo, factor=factor,
                         izq=izq, fx=fx) -> float:
                    return izq + (xd - tramo[0]) * factor if factor else xd * fx

                xs = sorted(
                    (a_pt(registros[i]["x"]), i)
                    for i, r in enumerate(registros)
                    if r["pagina"] == pagina and r["sistema"] == s
                    and r.get("x") is not None)
                if len(xs) < 2:
                    continue

                # Dónde caen los silencios de este sistema, en puntos: un
                # descanso no lleva número y no debe parecer nota olvidada.
                reposo = []
                for d in descansos:
                    if d["pagina"] != pagina or d["sistema"] != s:
                        continue
                    origen = (d["x_medida"] if modo == "medida"
                              else d["x_sistema"] if modo == "sistema"
                              else 0.0)
                    reposo.append(a_pt(origen + d["dx"] + MEDIA_CABEZA))

                cabezas = _cabezas(a, sup, inf, izq, der, escala_x, escala_y)

                # Caracteres que salen en las posiciones de nota del OMR (las
                # de xs): sus cabezas y adornos. Cualquier blob que en el PDF
                # cargue SOLO texto de otro tipo (silencios, cifras,
                # alteraciones) es un falso positivo: una nota de verdad lleva
                # al lado uno de estos signos. Se cuenta por posición de nota,
                # no por detección: _cabezas devuelve pocas y dejaría el
                # alfabeto vacío justo donde más falta hace.
                cefalo = Counter()
                for nx, _ in xs:
                    for gx, gy, run in glifos:
                        if abs(gx - nx) <= 4 and sup - espacio <= gy <= inf + espacio:
                            cefalo[run] += 1
                cefalo = {r for r, n in cefalo.items() if n >= 2}
                cefalo |= forma

                # Cabezas que el OMR no leyó pero sí están dibujadas: glifos de
                # cabeza que no caen sobre ninguna nota del MusicXML. En pasajes
                # densos (notas con plica unida) la detección por imagen se
                # pierde, pero el glifo de texto está siempre. Solo se usa si la
                # partitura trae texto (alfabeto no vacío).
                glifos_cab = []
                if glifos and cefalo:
                    tol = max(3.5, 1.3 * espacio)
                    for gx, gy, run in glifos:
                        if run not in cefalo:
                            continue
                        # Una cabeza reconocida por forma puede ir varias líneas
                        # por encima o debajo del pentagrama (notas con líneas
                        # adicionales); las de alfabeto local van más ajustadas.
                        margen = 3.5 * espacio if run in forma else 1.5 * espacio
                        if not sup - margen <= gy <= inf + margen:
                            continue
                        if any(abs(gx - nx) <= tol for nx, _ in xs):
                            continue    # ya lleva número
                        glifos_cab.append((gx, gy, 1.2 * espacio))
                # El glifo es prueba directa de una nota: si un blob de imagen
                # cae justo encima, sobra (la plica o la barra lo desplazan y
                # el blob acababa vetado, perdiendo la nota). Manda el glifo.
                cabezas = [
                    c for c in cabezas
                    if not any(abs(c[0] - gx) <= 0.9 * espacio
                               and abs(c[1] - gy) <= 1.0 * espacio
                               for gx, gy, _ in glifos_cab)]
                n_px = len(cabezas)
                cabezas = cabezas + glifos_cab

                for n, (cx, cy, ancho) in enumerate(cabezas):
                    de_glifo = n >= n_px
                    # En columna (clave, puntos de repetición...) no es nota:
                    # dos cabezas de verdad nunca quedan tan pegadas en x.
                    if not de_glifo and any(abs(cx - ox) < 0.6 * espacio
                                            and abs(cy - oy) > 0.8 * espacio
                                            for m, (ox, oy, _) in enumerate(cabezas)
                                            if m != n):
                        continue
                    if any(abs(cx - nx) < 0.8 * espacio for nx, _ in xs):
                        continue    # ya lleva número
                    if not de_glifo and any(abs(cx - rx) < 0.8 * espacio
                                            for rx in reposo):
                        continue    # hay un silencio ahí: no es una nota
                    # Si hay texto en la imagen, el blob tiene que llevar al
                    # lado un glifo de cabeza; si solo hay silencio/cifra es
                    # tinta de esas y no una nota olvidada.
                    if not de_glifo:
                        vecinos = [run for gx, gy, run in glifos
                                   if abs(gx - cx) <= 4 and abs(gy - cy) <= 4]
                        if vecinos and cefalo and not (set(vecinos) & cefalo):
                            continue
                    b = next((t for t in range(len(xs) - 1)
                              if xs[t][0] < cx < xs[t + 1][0]), None)
                    if b is not None:
                        (xa, ia), (xb, ib) = xs[b], xs[b + 1]
                        frac = min(max((cx - xa) / (xb - xa), 0.0), 1.0)
                        ra, rb = registros[ia], registros[ib]
                        offset = (ra["offset"]
                                  + (rb["offset"] - ra["offset"]) * frac)
                        dur = ra["duracion"] + (
                            (rb["duracion"] - ra["duracion"]) * frac)
                        ancla = ib
                    elif cx > xs[-1][0]:
                        # Tras el último número: nota final del sistema que
                        # el OMR no leyó (zonas sin número en los bordes).
                        if not de_glifo and cx >= der - 0.35 * espacio:
                            continue    # barra final o puntos de repetición
                        ancla = xs[-1][1] + 1
                        ra = rb = registros[xs[-1][1]]
                        offset = ra["offset"] + ra["duracion"]
                        dur = ra["duracion"]
                    elif (ancho >= 0.75 * espacio and cx <= xs[0][0]
                          and cx > izq + 1.2 * espacio):
                        # Antes del primer número; ancho de cabeza para no
                        # confundir con la bemol de la armadura (más estrecha).
                        ancla = xs[0][1]
                        ra = rb = registros[ancla]
                        offset = max(ra["offset"] - ra["duracion"], 0.0)
                        dur = ra["duracion"]
                    else:
                        continue    # sin vecinos que la enmarquen: mejor no
                    midi = _midi_en_clave(cy, sup, inf,
                                          rb.get("clave", CLAVE_POR_DEFECTO),
                                          rb.get("fifths", 0))
                    if midi is None:
                        continue
                    midi += semitonos
                    x = tramo[0] + (cx - izq) / factor if factor else cx / fx
                    salida.append((ancla, {
                        "midi": midi,
                        "midi_escrito": midi - semitonos,
                        "clave": rb.get("clave", CLAVE_POR_DEFECTO),
                        "offset": offset,
                        "duracion": dur,
                        "pagina": pagina,
                        "dx": x - rb["x_medida"] - MEDIA_CABEZA,
                        "x_medida": rb["x_medida"],
                        "x_sistema": rb["x_sistema"],
                        "sistema": s,
                        "y": sistemas[s]["y_numeros"],
                        "x": x,
                        "estimada": True,
                    }))
    finally:
        doc.close()

    # Demasiadas "notas" para ser verdad: no confiamos en la detección y no
    # tocamos nada (las lecturas normales siguen intactas).
    if not salida or len(salida) > max(3, int(0.3 * len(registros))):
        return []
    por_ancla: Dict[int, List[dict]] = {}
    for ancla, g in salida:
        por_ancla.setdefault(ancla, []).append(g)
    return [(ancla, g) for ancla in sorted(por_ancla, reverse=True)
            for g in sorted(por_ancla[ancla], key=lambda g: -g["x"])]


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


def _pentagramas_fiables(pentagramas, esperados: int, ancho_pt: float,
                         alto_pt: float):
    """
    Acepta los pentagramas del .omr solo si cuadran con esta página.

    El formato del .omr no está documentado y puede cambiar de versión. Si
    lo leído no encaja —otro número de sistemas, coordenadas fuera del
    papel, pentagramas que se cruzan— se descarta entero y se mide la
    imagen como siempre. Más vale el camino lento que uno rápido y falso.
    """
    if not pentagramas or len(pentagramas) != esperados:
        return None
    margen = 0.02 * max(ancho_pt, alto_pt)
    anterior = None
    for sup, inf, izq, der in pentagramas:
        if not (-margen <= sup < inf <= alto_pt + margen):
            return None
        if not (-margen <= izq < der <= ancho_pt + margen):
            return None
        if anterior is not None and sup < anterior:
            return None    # se solapan: no son sistemas consecutivos
        anterior = inf
    return list(pentagramas)


def a_hoja(x_vista: float, y_arriba: float, giro: int,
           ancho_caja: float, alto_caja: float, alto_pt: float,
           orig_x: float = 0.0, orig_y: float = 0.0) -> Tuple[float, float]:
    """
    Pasa un punto de la página *tal como se ve* al espacio del PDF.

    `x_vista`/`y_arriba` van en puntos desde la esquina superior izquierda de
    lo que muestra el visor. `ancho_caja`/`alto_caja` son los de la caja sin
    girar, y `orig_x`/`orig_y` su esquina inferior izquierda, que no siempre
    es (0,0). `giro` es el /Rotate de la página.

    /Rotate gira la imagen en sentido horario al mostrarla, así que aquí se
    deshace ese giro. Lo usan por igual el estampado y el diagnóstico: si la
    fórmula estuviera mal, los dos se equivocarían igual y las pruebas no lo
    verían, por eso el generador de partituras sintéticas la deriva aparte.
    """
    y_vista = alto_pt - y_arriba
    if giro == 90:
        x, y = ancho_caja - y_vista, x_vista
    elif giro == 180:
        x, y = ancho_caja - x_vista, alto_caja - y_vista
    elif giro == 270:
        x, y = y_vista, alto_caja - x_vista
    else:
        x, y = x_vista, y_vista
    return x + orig_x, y + orig_y


def _ficha_marca(m: Marca, x_bruta: float, x: float,
                 alturas: Dict[int, float], estado: str,
                 confianza: float = 1.0) -> dict:
    """Qué ha pasado con un número, para el PDF de diagnóstico."""
    return {"id": id(m), "texto": m.texto, "sistema": m.sistema,
            "estimada": m.estimada, "x_omr": x_bruta, "x": x,
            "y": alturas.get(m.sistema, 0.0), "estado": estado,
            "confianza": round(confianza, 3)}


def estampar(pdf_original: Path, pdf_salida: Path, plano: Plano,
             tamano_relativo: float = 2.0,
             fraccion_hueco: float = 0.48,
             diag: Optional[list] = None) -> Tuple[int, bool, int]:
    """
    Dibuja los números sobre el PDF original. Devuelve (cuántos, si se han
    usado los pentagramas medidos en el propio PDF, cuántos son dudosos).

    Las X vienen de Audiveris, que en eso acierta. Las Y salen de medir el
    PDF, porque la escala vertical que declara Audiveris no es de fiar.

    Si se pasa `diag` (una lista), se le añade una ficha por página con todo
    lo que se ha medido y qué ha pasado con cada número. Es lo que consume
    `depurar.py` para pintar el PDF de diagnóstico.
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
    dudosas = 0
    medido = False

    for indice, pagina in enumerate(lector.pages):
        marcas = por_pagina.get(indice)
        giro = int(pagina.get("/Rotate") or 0) % 360
        ficha = None
        if diag is not None:
            ficha = {"pagina": indice, "giro": giro,
                     "marcas_entrada": len(marcas or []), "notas": []}
            diag.append(ficha)

        if marcas:
            # pypdfium2 renderiza la CropBox, no la MediaBox. Si el PDF trae
            # las dos distintas (recortes de escaneo, imposición) medíamos
            # sobre una y dibujábamos sobre la otra, y todo salía corrido.
            caja = pagina.cropbox or pagina.mediabox
            ancho_caja = float(caja.width)
            alto_caja = float(caja.height)
            orig_x = float(caja.left)
            orig_y = float(caja.bottom)

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
            # Lo que Audiveris ya midió manda sobre lo que podamos sacar de
            # la imagen: es la misma información sin el riesgo de no acertar
            # el número de sistemas y caer a modo degradado.
            pentagramas = _pentagramas_fiables(
                (plano.pentagramas_omr or {}).get(indice),
                len(en_pagina), ancho_pt, alto_pt)
            origen_pent = "omr" if pentagramas else "imagen"
            if not pentagramas:
                pentagramas = detectar_pentagramas(pdf_original, indice,
                                                   alto_pt,
                                                   esperados=len(en_pagina))

            if ficha is not None:
                ficha.update({
                    "caja": [float(caja.left), float(caja.bottom),
                             ancho_caja, alto_caja],
                    "ancho_pt": ancho_pt, "alto_pt": alto_pt,
                    "fx": fx, "fy": fy,
                    "sistemas_esperados": len(en_pagina),
                    "pentagramas": [list(x) for x in pentagramas],
                    "origen_pentagramas": origen_pent,
                })

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

            # Cabezas de nota dibujadas. Antes de poner un número comprobamos
            # que haya una cabeza en su sitio: así no aparecen cifras sobre
            # silencios, claves ni cifras de compás, pase lo que pase con las
            # coordenadas que traiga el OMR.
            cabeceas = None
            if pentagramas:
                cabeceas = _cabezas_texto(pdf_original, indice, giro,
                                          ancho_pt, alto_pt, espacio)
            banda_pent = {s: pentagramas[k]
                          for k, s in enumerate(en_pagina)
                          if k < len(pentagramas)}

            if ficha is not None:
                ficha.update({
                    "medido": medido,
                    "espacio": espacio,
                    "cabezas_texto": (None if cabeceas is None
                                      else [list(c) for c in cabeceas]),
                    "escalas": {str(k): list(v) for k, v in escalas.items()},
                    "alturas": {str(k): v for k, v in alturas.items()},
                })

            # Ya en puntos de la página vista, para medir lo juntas que van.
            colocadas = []
            for m in marcas:
                cal = escalas.get(m.sistema)
                if cal:
                    origen, izq, factor = cal
                    x = izq + (m.x - origen) * factor
                else:
                    x = m.x * fx
                x_bruta = x
                estado = "puesta"
                conf = m.confianza
                if not medido:
                    conf *= FACTOR_SIN_MEDIR
                banda = banda_pent.get(m.sistema)
                if cabeceas is not None and banda:
                    cerca = [hx for hx, hy in cabeceas
                             if banda[0] - 2.0 * espacio <= hy
                             <= banda[1] + 4.0 * espacio
                             and abs(hx - x) <= max(3.0, 2.5 * espacio)]
                    if cerca:
                        # Reajuste fino: la cabeza dibujada manda sobre la
                        # coordenada reconstruida, que acumula error compás a
                        # compás. Solo si está razonablemente cerca.
                        mejor_hx = min(cerca, key=lambda hx: abs(hx - x))
                        if abs(mejor_hx - x) <= 1.2 * espacio:
                            if abs(mejor_hx - x) > 0.05 * espacio:
                                estado = "ajustada"
                            x = mejor_hx
                        else:
                            estado = "cabeza_lejos"
                    elif m.estimada:
                        # Nota que solo existía en la imagen y que ahora no
                        # aparece: era un falso positivo.
                        if ficha is not None:
                            ficha["notas"].append(
                                _ficha_marca(m, x_bruta, x_bruta, alturas,
                                             "descartada_sin_cabeza"))
                        continue
                    else:
                        estado = "sin_cabeza"
                        conf *= FACTOR_SIN_CABEZA
                    # Una nota leída del MusicXML SIEMPRE se estampa. Que no
                    # encontremos su cabeza es un fallo de nuestra detección
                    # de glifos, no una razón para comerse la nota.
                if ficha is not None:
                    ficha["notas"].append(
                        _ficha_marca(m, x_bruta, x, alturas, estado, conf))
                colocadas.append((m, x, conf))

            # Dos cifras en el mismo punto (una leída y otra estimada) o tres
            # glifos que se creían notas distintas en una cabeza: nos quedamos
            # con una, dando prioridad a la leída del MusicXML.
            colocadas.sort(key=lambda mx: (mx[0].sistema, mx[0].estimada, mx[1]))
            limpias = []
            for m, x, conf in colocadas:
                if (limpias and limpias[-1][0].sistema == m.sistema
                        and abs(limpias[-1][1] - x) < 0.55 * espacio):
                    continue
                limpias.append((m, x, conf))
            colocadas = limpias

            if ficha is not None:
                vivas = {id(m) for m, _, _ in colocadas}
                for f in ficha["notas"]:
                    if (f["estado"] != "descartada_sin_cabeza"
                            and f["id"] not in vivas):
                        f["estado"] = "descartada_duplicada"

            cuerpos = {}
            for s in sistemas:
                suyas = [Marca(m.pagina, x, m.y, m.texto, m.sistema)
                         for m, x, _ in colocadas if m.sistema == s]
                cuerpos[s] = _cuerpo_por_sistema(suyas, espacio)

            buffer = BytesIO()
            lienzo = canvas.Canvas(buffer, pagesize=(ancho_caja, alto_caja))
            for m, x_vista, conf in colocadas:
                x_hoja, y_hoja = a_hoja(x_vista, alturas[m.sistema], giro,
                                        ancho_caja, alto_caja, alto_pt,
                                        orig_x, orig_y)

                lienzo.saveState()
                lienzo.translate(x_hoja, y_hoja)
                if giro:
                    lienzo.rotate(giro)
                # Cuanta menos confianza, más claro. Un número gris se
                # lee igual de bien y avisa de que conviene mirarlo; uno
                # que falta no avisa de nada.
                lienzo.setFillGray(min(max(1.0 - conf, 0.0), 1.0) * 0.72)
                lienzo.setFont("Helvetica",
                               cuerpos.get(m.sistema, espacio * tamano_relativo))
                lienzo.drawCentredString(0, 0, m.texto)
                lienzo.restoreState()
                puestas += 1
                if conf < CONFIANZA_DUDOSA:
                    dudosas += 1
            lienzo.save()
            buffer.seek(0)

            pagina.merge_page(PdfReader(buffer).pages[0])

        escritor.add_page(pagina)

    with open(pdf_salida, "wb") as f:
        escritor.write(f)

    return puestas, medido, dudosas
