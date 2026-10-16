"""
La cola de trabajos en disco.

Vive aparte de `api.py` por dos motivos. Uno práctico: así se puede probar
sin levantar FastAPI ni cargar music21, que tarda segundos. Y otro de
fondo: la cola es lo que decide qué pasa cuando el contenedor se reinicia a
mitad de una partitura, y eso conviene tenerlo escrito en un sitio donde se
lea entero de una vez.

El servicio se actualiza solo con watchtower. Un reinicio en mitad de un
reconocimiento de media hora es algo que va a pasar, no una rareza: antes
el trabajo se quedaba para siempre en "Leyendo la partitura" y el usuario
esperando a que terminara algo que ya no existía.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

# Estados en los que hay trabajo por hacer. Si el proceso se muere, estos
# son los que hay que recuperar; el resto ya están cerrados.
EN_MARCHA = ("en_cola", "preparando", "reconociendo", "anotando", "generando")
# Un trabajo cancelado está tan terminado como uno con error: no se reintenta
# al arrancar y se borra con la limpieza por antigüedad.
TERMINADOS = ("listo", "error", "cancelado")

RETENCION_DIAS = 30
MAX_REINTENTOS = 2

_lock = threading.Lock()


def sanear(nombre: str) -> str:
    """Nombre de archivo seguro, conservando tildes y espacios."""
    base = unicodedata.normalize("NFC", Path(nombre).name)
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return base[:120] or "partitura.pdf"


def es_id(tid: str) -> bool:
    """Los identificadores los generamos nosotros: hex y nada más."""
    return bool(re.fullmatch(r"[0-9a-f]{6,32}", tid or ""))


def carpeta(datos: Path, tid: str) -> Optional[Path]:
    if not es_id(tid):
        return None
    d = datos / tid
    return d if d.is_dir() else None


def leer(datos: Path, tid: str) -> Optional[dict]:
    d = carpeta(datos, tid)
    if d is None:
        return None
    f = d / "estado.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def guardar(datos: Path, tid: str, **cambios) -> dict:
    with _lock:
        f = datos / tid / "estado.json"
        actual = {}
        if f.exists():
            try:
                actual = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                actual = {}
        actual.update(cambios)
        actual["actualizado"] = time.time()
        f.write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
        return actual


def todos(datos: Path) -> List[dict]:
    salida = []
    for f in datos.glob("*/estado.json"):
        try:
            salida.append(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    salida.sort(key=lambda x: x.get("creado", 0), reverse=True)
    return salida


def pendientes(datos: Path) -> List[dict]:
    """Trabajos que se quedaron a medias cuando murió el proceso."""
    return [t for t in todos(datos) if t.get("estado") in EN_MARCHA]


def recuperar(datos: Path) -> Dict[str, List[dict]]:
    """
    Decide qué hacer con lo que quedó a medias, al arrancar.

    Se reintenta, pero no para siempre: un PDF que tumba el proceso lo
    volvería a tumbar en cada arranque y el servicio no levantaría nunca.
    A la tercera se marca como error y se explica qué ha pasado, que es
    más útil que una barra de progreso parada toda la noche.
    """
    reencolados, rendidos = [], []
    for t in pendientes(datos):
        tid = t.get("id")
        if not tid:
            continue
        intentos = int(t.get("intentos", 0)) + 1
        if intentos > MAX_REINTENTOS:
            rendidos.append(guardar(
                datos, tid, estado="error", intentos=intentos,
                error=("El servicio se reinició mientras se procesaba esta "
                       "partitura, y al reintentarlo ha vuelto a pasar. "
                       "Puede que el archivo sea demasiado grande o que "
                       "tenga algo que atasca el reconocimiento.")))
        else:
            reencolados.append(guardar(datos, tid, estado="en_cola",
                                       intentos=intentos))
    return {"reencolados": reencolados, "rendidos": rendidos}


def limpiar_viejos(datos: Path, dias: int = RETENCION_DIAS) -> int:
    """
    Borra los trabajos terminados que ya nadie va a mirar.

    Sin esto, /datos crece hasta llenar el disco: cada partitura deja el
    PDF original, el export de Audiveris, el .omr y el resultado.
    """
    if dias <= 0:
        return 0
    limite = time.time() - dias * 86400
    borrados = 0
    for f in list(datos.glob("*/estado.json")):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if t.get("estado") not in TERMINADOS:
            continue
        if t.get("actualizado", t.get("creado", 0)) >= limite:
            continue
        shutil.rmtree(f.parent, ignore_errors=True)
        borrados += 1
    return borrados


def parametros(t: dict) -> dict:
    """Los ajustes con los que se lanzó un trabajo, para poder repetirlo."""
    return {
        "transpositor": bool(t.get("transpositor", False)),
        "titulo_manual": t.get("titulo_manual", ""),
        "orientacion": t.get("orientacion", "auto"),
        "sin_reguladores": bool(t.get("sin_reguladores", True)),
        "etiqueta": t.get("etiqueta", ""),
        "criterio": t.get("criterio", "equilibrado"),
        "mantener_original": bool(t.get("mantener_original", False)),
        "lectura": t.get("lectura", "auto"),
    }
