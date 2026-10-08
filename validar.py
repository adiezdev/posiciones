"""
Red de seguridad para el pipeline automático.

No sustituye a un par de ojos, pero pilla la mayoría de los fallos gordos del
OMR sin intervención humana:

  - compases cuya duración no cuadra con la indicación de compás
  - notas fuera del alcance físico del instrumento
  - saltos interválicos improbables (suelen ser una altura mal leída)
  - partes o compases vacíos

Devuelve una lista de avisos en texto plano para adjuntar a la notificación.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from music21 import chord, meter, stream

# Alcance del trombón tenor. Por debajo hace falta transpositor.
MIDI_MIN = 40   # Mi2
MIDI_MIN_F = 36  # Do2 con válvula de Fa
MIDI_MAX = 77   # Fa5

SALTO_SOSPECHOSO = 19  # semitonos: más de una duodécima seguida canta raro
TOLERANCIA_DURACION = 0.01


@dataclass
class Aviso:
    gravedad: str   # "error" | "duda"
    parte: str
    compas: object
    texto: str

    def __str__(self) -> str:
        return f"[{self.gravedad}] {self.parte} c.{self.compas}: {self.texto}"


def _nombre_parte(p, i: int) -> str:
    return (getattr(p, "partName", None) or f"parte {i + 1}").strip()


def validar(score, transpositor: bool = False) -> List[Aviso]:
    minimo = MIDI_MIN_F if transpositor else MIDI_MIN
    avisos: List[Aviso] = []

    partes = list(score.parts) if hasattr(score, "parts") and len(score.parts) else [score]

    for i, parte in enumerate(partes):
        nombre = _nombre_parte(parte, i)
        compases = list(parte.getElementsByClass(stream.Measure))

        if not compases:
            avisos.append(Aviso("duda", nombre, "-", "sin compases reconocibles"))
            continue

        ts_actual = None
        anterior = None

        for m in compases:
            nuevo_ts = m.getElementsByClass(meter.TimeSignature)
            if nuevo_ts:
                ts_actual = nuevo_ts[0]

            # --- duración del compás ---
            if ts_actual is not None:
                esperado = ts_actual.barDuration.quarterLength
                real = m.duration.quarterLength
                # Anacrusas y compases partidos: solo avisamos si sobra.
                if real > esperado + TOLERANCIA_DURACION:
                    avisos.append(Aviso(
                        "error", nombre, m.number,
                        f"duración {real} negras con compás {ts_actual.ratioString} "
                        f"(esperado {esperado})"))
                elif real < esperado - TOLERANCIA_DURACION and m is not compases[0]:
                    avisos.append(Aviso(
                        "duda", nombre, m.number,
                        f"compás corto: {real} de {esperado} negras"))

            # --- notas ---
            for el in m.recurse().notes:
                objetivo = (max(el.notes, key=lambda x: x.pitch.midi)
                            if isinstance(el, chord.Chord) else el)
                midi = objetivo.pitch.midi

                if midi < minimo:
                    avisos.append(Aviso(
                        "error", nombre, m.number,
                        f"{objetivo.nameWithOctave} por debajo del alcance"
                        + ("" if transpositor else " (¿necesita transpositor?)")))
                elif midi > MIDI_MAX:
                    avisos.append(Aviso(
                        "error", nombre, m.number,
                        f"{objetivo.nameWithOctave} por encima del alcance"))

                if anterior is not None:
                    salto = abs(midi - anterior)
                    if salto > SALTO_SOSPECHOSO:
                        avisos.append(Aviso(
                            "duda", nombre, m.number,
                            f"salto de {salto} semitonos hasta "
                            f"{objetivo.nameWithOctave} (¿altura mal leída?)"))
                anterior = midi

    return avisos


def informe(avisos: List[Aviso]) -> str:
    if not avisos:
        return "Sin avisos. El OMR parece coherente."
    errores = [a for a in avisos if a.gravedad == "error"]
    dudas = [a for a in avisos if a.gravedad == "duda"]
    lineas = [f"{len(errores)} errores, {len(dudas)} dudas.", ""]
    lineas += [str(a) for a in avisos]
    return "\n".join(lineas)


def resumen_corto(avisos: List[Aviso], maximo: int = 5) -> str:
    if not avisos:
        return "sin avisos"
    errores = sum(1 for a in avisos if a.gravedad == "error")
    dudas = len(avisos) - errores
    cabecera = f"{errores} errores / {dudas} dudas"
    detalle = "; ".join(str(a) for a in avisos[:maximo])
    if len(avisos) > maximo:
        detalle += f"; (+{len(avisos) - maximo} más)"
    return f"{cabecera} — {detalle}"
