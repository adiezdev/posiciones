"""
Cálculo de posiciones de vara para trombón tenor en Sib.

La parte está escrita en Do (no transpositora, clave de fa), así que la altura
escrita coincide con la real y el mapeo MIDI -> posición es directo.

Cada nota se puede tocar en varias posiciones, una por cada armónico que la
alcance. `elegir_posiciones` resuelve la secuencia entera con programación
dinámica (Viterbi) minimizando el movimiento de vara, con más peso cuanto más
rápidas son las notas, en vez de escoger siempre la posición más corta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

# --- Series armónicas -------------------------------------------------------
# Serie en 1ª posición. Fundamental Sib1 = MIDI 34.
# Cada posición baja un semitono respecto a la anterior.
ARMONICOS = {
    1: 34,   # Sib1  (pedal)
    2: 46,   # Sib2
    3: 53,   # Fa3
    4: 58,   # Sib3
    5: 62,   # Re4   (algo calante)
    6: 65,   # Fa4
    7: 68,   # Lab4  (muy calante, se evita)
    8: 70,   # Sib4
    9: 72,   # Do5
    10: 74,  # Re5
    12: 77,  # Fa5
}

# Serie con el transpositor (válvula de Fa) accionado. Fundamental Fa1 = MIDI 29.
ARMONICOS_F = {
    2: 41,   # Fa2
    3: 48,   # Do3
    4: 53,   # Fa3
    5: 57,   # La3
    6: 60,   # Do4
    8: 65,   # Fa4
}

POS_MAX = 7      # sin transpositor
POS_MAX_F = 6    # con transpositor la vara es más larga, solo llegan 6

# Incomodidad de cada posición. La vara pesa cada vez más al alejarla.
COSTE_POS = {1: 0.0, 2: 0.20, 3: 0.42, 4: 0.75, 5: 1.70, 6: 3.00, 7: 4.30}

# --- Ajustes del solver -----------------------------------------------------
PENAL_ARMONICO_7 = 6.0    # el 7º armónico es inservible en la práctica
PENAL_PEDAL = 4.0         # pedales: solo si no hay otra cosa
PENAL_AGUDO = 0.25        # armónicos 9+ son más delicados de afinar
PENAL_TRANSPOSITOR = 1.2  # usar la válvula cuesta algo
PESO_MOVIMIENTO = 0.45    # coste por cada posición de vara recorrida
PENAL_CAMBIO_VALVULA = 0.9
PENAL_SALTO_MISMO_ARMONICO = 0.3
PENAL_LIGADURA_MISMO_ARMONICO = 1.2  # mover vara ligando en el mismo armónico = glissando
URGENCIA_MAX = 4.0


@dataclass(frozen=True)
class Posicion:
    numero: int
    armonico: int
    transpositor: bool = False

    @property
    def etiqueta(self) -> str:
        return f"T{self.numero}" if self.transpositor else str(self.numero)

    def __str__(self) -> str:  # pragma: no cover
        return self.etiqueta


@dataclass
class Nota:
    """Nota de entrada para el solver."""
    midi: int
    offset: float = 0.0      # posición absoluta en negras
    duracion: float = 1.0    # en negras
    ligada: bool = False     # True si viene ligada de la anterior


# Criterios que puede elegir el usuario desde la web.
#   cortas      -> descarta 5ª, 6ª y 7ª siempre que la nota tenga alternativa
#   equilibrado -> los pesos calibrados por defecto
#   vara        -> penaliza más el movimiento, para pasajes rápidos
CRITERIOS = {
    "cortas":      {"preferir_cercanas": True,  "peso": None},
    "equilibrado": {"preferir_cercanas": False, "peso": None},
    "vara":        {"preferir_cercanas": False, "peso": 0.85},
}


# Con "preferir cercanas", si una nota se puede tocar sin sacar mucho la vara
# se descartan las posiciones lejanas aunque salieran más baratas de movimiento.
POS_CERCANA = 4


def candidatos(midi: int, transpositor: bool = False,
               preferir_cercanas: bool = False) -> List[Posicion]:
    """Todas las posiciones que producen esa nota, sin ordenar."""
    out: List[Posicion] = []
    for armonico, base in ARMONICOS.items():
        pos = base - midi + 1
        if 1 <= pos <= POS_MAX:
            out.append(Posicion(pos, armonico, False))
    if transpositor:
        for armonico, base in ARMONICOS_F.items():
            pos = base - midi + 1
            if 1 <= pos <= POS_MAX_F:
                out.append(Posicion(pos, armonico, True))

    if preferir_cercanas:
        cercanas = [c for c in out if c.numero <= POS_CERCANA and c.armonico != 7]
        if cercanas:
            return cercanas

    return out


def posicion_simple(midi: int, transpositor: bool = False) -> Optional[Posicion]:
    """La posición más cómoda para una nota aislada, sin mirar el contexto."""
    cands = candidatos(midi, transpositor)
    return min(cands, key=_coste_base) if cands else None


def _coste_base(c: Posicion) -> float:
    k = COSTE_POS[c.numero]
    if c.armonico == 7:
        k += PENAL_ARMONICO_7
    if c.armonico == 1:
        k += PENAL_PEDAL
    if c.armonico >= 9:
        k += PENAL_AGUDO
    if c.transpositor:
        k += PENAL_TRANSPOSITOR
    return k


def _coste_transicion(a: Posicion, b: Posicion, urgencia: float,
                      peso: float = PESO_MOVIMIENTO) -> float:
    k = peso * abs(a.numero - b.numero)
    if a.transpositor != b.transpositor:
        k += PENAL_CAMBIO_VALVULA
    if a.armonico == b.armonico and abs(a.numero - b.numero) >= 5:
        k += PENAL_SALTO_MISMO_ARMONICO
    return k * urgencia


def _urgencia(tiempo_negras: float) -> float:
    """Cuanto menos tiempo hay entre ataques, más caro es mover la vara."""
    return min(URGENCIA_MAX, 1.0 / max(tiempo_negras, 0.25))


def elegir_posiciones(
    notas: Sequence[Nota], transpositor: bool = False,
    preferir_cercanas: bool = False,
    peso_movimiento: Optional[float] = None
) -> List[Optional[Posicion]]:
    """
    Devuelve una lista paralela a `notas`. Las notas fuera del alcance del
    instrumento salen como None para que quien llame decida qué hacer con ellas.
    """
    peso = PESO_MOVIMIENTO if peso_movimiento is None else peso_movimiento
    resultado: List[Optional[Posicion]] = [None] * len(notas)
    bloque: list = []
    for i, n in enumerate(notas):
        cands = candidatos(n.midi, transpositor, preferir_cercanas)
        if cands:
            bloque.append((i, n, cands))
        else:
            _resolver_bloque(bloque, resultado, peso)
            bloque = []
    _resolver_bloque(bloque, resultado, peso)
    return resultado


def _resolver_bloque(bloque: list, resultado: list,
                     peso: float = PESO_MOVIMIENTO) -> None:
    if not bloque:
        return

    _, _, cands0 = bloque[0]
    costes = [_coste_base(c) for c in cands0]
    tablas = [cands0]
    backs: List[List[int]] = [[-1] * len(cands0)]

    for k in range(1, len(bloque)):
        _, n, cands = bloque[k]
        _, n_ant, cands_ant = bloque[k - 1]

        salto = n.offset - n_ant.offset
        urgencia = _urgencia(salto if salto > 0 else n_ant.duracion)

        nuevos, back = [], []
        for cb in cands:
            mejor, mejor_j = None, 0
            for j, ca in enumerate(cands_ant):
                c = costes[j] + _coste_transicion(ca, cb, urgencia, peso)
                if n.ligada and ca.armonico == cb.armonico and ca.numero != cb.numero:
                    c += PENAL_LIGADURA_MISMO_ARMONICO
                if mejor is None or c < mejor:
                    mejor, mejor_j = c, j
            nuevos.append(mejor + _coste_base(cb))
            back.append(mejor_j)

        costes = nuevos
        tablas.append(cands)
        backs.append(back)

    j = min(range(len(costes)), key=costes.__getitem__)
    for k in range(len(bloque) - 1, -1, -1):
        resultado[bloque[k][0]] = tablas[k][j]
        j = backs[k][j]


if __name__ == "__main__":
    # Sanity check rápido: escala cromática Mi2 -> Sib4 sin contexto.
    NOMBRES = ["Do", "Do#", "Re", "Mib", "Mi", "Fa",
               "Fa#", "Sol", "Lab", "La", "Sib", "Si"]
    for midi in range(40, 71):
        p = posicion_simple(midi)
        nombre = f"{NOMBRES[midi % 12]}{midi // 12 - 1}"
        print(f"{midi:>3}  {nombre:<6} -> {p.etiqueta if p else '-':>3}"
              f"   (armónico {p.armonico if p else '-'})")
