"""
Servidor del conversor de partituras.

Sirve la web en / y expone una API de trabajos:

  POST   /api/trabajos            sube un PDF y encola el trabajo
  GET    /api/trabajos            lista los trabajos
  GET    /api/trabajos/{id}       estado de uno
  GET    /api/trabajos/{id}/pdf   descarga el resultado
  DELETE /api/trabajos/{id}       lo borra
  GET    /salud

El trabajo pesado (Audiveris) corre en un hilo aparte y de uno en uno, para no
saturar la máquina. Por eso la web pregunta el estado cada pocos segundos en
vez de esperar a que responda la subida.
"""

from __future__ import annotations

import json
import os
import zipfile
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from music21 import converter

import estilo
import partitura
import superponer
from anotar import anotar as anotar_score
from anotar import quitar_reguladores
from posiciones import CRITERIOS
from validar import validar

AUDIVERIS = os.environ.get("AUDIVERIS_BIN", "/opt/audiveris/bin/Audiveris")
MSCORE = os.environ.get("MSCORE_BIN", "mscore3")
TIMEOUT_OMR = int(os.environ.get("TIMEOUT_OMR", "2400"))
TIMEOUT_RENDER = int(os.environ.get("TIMEOUT_RENDER", "300"))
DATOS = Path(os.environ.get("DATOS_DIR", "/datos"))
ESTATICOS = Path(__file__).parent / "static"

DATOS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Posiciones de trombón", version="3.0")

_pool = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()


# --- persistencia mínima ----------------------------------------------------

def _sanear(nombre: str) -> str:
    """Nombre de archivo seguro, conservando tildes y espacios."""
    base = unicodedata.normalize("NFC", Path(nombre).name)
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return base[:120] or "partitura.pdf"


def _borrar_luego(d: Path) -> BackgroundTask:
    """Limpia una carpeta temporal cuando la descarga ha terminado."""
    return BackgroundTask(shutil.rmtree, str(d), ignore_errors=True)


def _dir(tid: str) -> Path:
    d = DATOS / tid
    if not d.is_dir() or ".." in tid or "/" in tid:
        raise HTTPException(404, "Ese trabajo no existe")
    return d


def _leer(tid: str) -> dict:
    f = _dir(tid) / "estado.json"
    if not f.exists():
        raise HTTPException(404, "Ese trabajo no existe")
    return json.loads(f.read_text(encoding="utf-8"))


def _guardar(tid: str, **cambios) -> dict:
    with _lock:
        f = DATOS / tid / "estado.json"
        actual = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        actual.update(cambios)
        actual["actualizado"] = time.time()
        f.write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
        return actual


# --- procesos externos ------------------------------------------------------

def _correr(cmd: list, timeout: int, etiqueta: str) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{etiqueta} tardó más de {timeout // 60} minutos y se ha cancelado.")
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout or "").strip().splitlines()
        cola = " ".join(detalle[-3:])[:400] if detalle else "sin detalle"
        raise RuntimeError(f"{etiqueta} no pudo completar el proceso: {cola}")


def _trabajar(tid: str, transpositor: bool, titulo_manual: str,
              orientacion: str, sin_reguladores: bool = True,
              etiqueta: str = "", criterio: str = "equilibrado",
              mantener_original: bool = False) -> None:
    d = DATOS / tid
    try:
        datos = json.loads((d / "estado.json").read_text(encoding="utf-8"))
        pdf_in = d / datos["archivo"]
        reglas = CRITERIOS.get(criterio, CRITERIOS["equilibrado"])
        cercanas = reglas["preferir_cercanas"]
        peso = reglas["peso"]

        _guardar(tid, estado="reconociendo")
        export = d / "export"
        export.mkdir(exist_ok=True)
        _correr([AUDIVERIS, "-batch", "-export", "-output", str(export), str(pdf_in)],
                TIMEOUT_OMR, "El reconocimiento de la partitura")

        encontrados = sorted(export.rglob("*.mxl")) or sorted(export.rglob("*.xml"))
        if not encontrados:
            raise RuntimeError(
                "No se ha reconocido ninguna partitura en el PDF. "
                "Comprueba que el archivo contiene música escrita y no solo texto o imágenes.")

        crudo = encontrados[0]

        # --- Vía A: estampar sobre el PDF original, sin redibujar nada ---
        if mantener_original:
            _guardar(tid, estado="anotando", modo="superpuesto")
            plano = superponer.preparar(partitura.cargar(crudo),
                                        transpositor, cercanas, peso)
            if plano is not None:
                _guardar(tid, estado="generando")
                pdf_out = d / "resultado.pdf"
                puestas, medido = superponer.estampar(pdf_in, pdf_out, plano)

                score = converter.parse(str(crudo))
                avisos = validar(score, transpositor)
                _guardar(
                    tid, estado="listo", notas=puestas,
                    modo_x=plano.modo_x,
                    pentagramas_medidos=medido,
                    encaje=round(plano.acierto * 100),
                    fuera_rango=max(plano.total - puestas, 0),
                    titulo=titulo_manual.strip() or pdf_in.stem,
                    papel="el de tu PDF original",
                    avisos=[{"gravedad": x.gravedad, "parte": x.parte,
                             "compas": str(x.compas), "texto": x.texto}
                            for x in avisos],
                )
                return

            _guardar(tid, nota_modo=(
                "El PDF no traía las coordenadas de las notas, así que se ha "
                "redibujado la partitura en lugar de escribir sobre el original."))

        # --- Vía B: redibujar la partitura ---
        # Lo que music21 se va a comer: título y tamaño de página.
        arbol = partitura.cargar(crudo)
        original = partitura.extraer_defaults(arbol)
        titulo = (titulo_manual.strip()
                  or partitura.detectar_titulo(arbol, ignorar=pdf_in.stem)
                  or pdf_in.stem)

        if orientacion == "horizontal":
            apaisado = True
        elif orientacion == "vertical":
            apaisado = False
        else:
            apaisado = bool(partitura.es_apaisado(original))
        defaults = partitura.defaults_estandar(apaisado)
        ancho_mm, alto_mm = partitura.medidas_papel(apaisado)
        papel = f"A4 {'horizontal' if apaisado else 'vertical'} " \
                f"({ancho_mm:.0f} x {alto_mm:.0f} mm)"

        _guardar(tid, estado="anotando", titulo=titulo,
                 apaisado=apaisado, papel=papel)
        score = converter.parse(str(crudo))
        avisos = validar(score, transpositor)
        if sin_reguladores:
            quitar_reguladores(score)
        notas, fuera = anotar_score(score, transpositor,
                                    preferir_cercanas=cercanas,
                                    peso_movimiento=peso)

        xml = d / "resultado.musicxml"
        score.write("musicxml", str(xml))
        partitura.rematar(xml, defaults, titulo, etiqueta.strip() or None)

        _guardar(tid, estado="generando")
        pdf_out = d / "resultado.pdf"
        # Tres pasos a propósito. MuseScore ignora parte de la hoja de estilo
        # cuando se la pasas por la línea de comandos, así que convertimos a
        # su formato nativo, le escribimos los ajustes dentro del archivo y
        # solo entonces generamos el PDF.
        nativo = d / "intermedio.mscx"
        _correr(["xvfb-run", "-a", MSCORE, "-o", str(nativo), str(xml)],
                TIMEOUT_RENDER, "La conversión intermedia")
        aplicados = estilo.aplicar_a_mscx(nativo, apaisado=apaisado)
        _guardar(tid, ancho_util_mm=round(
            float(aplicados["pagePrintableWidth"]) * 25.4))
        _correr(["xvfb-run", "-a", MSCORE, "-o", str(pdf_out), str(nativo)],
                TIMEOUT_RENDER, "La generación del PDF")
        if not pdf_out.exists():
            raise RuntimeError("La generación del PDF terminó sin crear el archivo.")

        _guardar(
            tid,
            estado="listo",
            notas=notas,
            fuera_rango=fuera,
            avisos=[{"gravedad": a.gravedad, "parte": a.parte,
                     "compas": str(a.compas), "texto": a.texto} for a in avisos],
        )
    except Exception as e:  # noqa: BLE001
        _guardar(tid, estado="error", error=str(e))


# --- API --------------------------------------------------------------------

@app.get("/salud")
def salud() -> dict:
    return {
        "ok": True,
        "audiveris": Path(AUDIVERIS).exists(),
        "mscore": shutil.which(MSCORE) is not None,
        "datos": str(DATOS),
    }


@app.post("/api/trabajos")
async def crear(
    archivo: UploadFile = File(...),
    transpositor: bool = Form(False),
    titulo: str = Form(""),
    orientacion: str = Form("auto"),
    sin_reguladores: bool = Form(True),
    etiqueta: str = Form(""),
    criterio: str = Form("equilibrado"),
    mantener_original: bool = Form(False),
):
    nombre = _sanear(archivo.filename or "partitura.pdf")
    if not nombre.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se admiten archivos PDF.")
    if orientacion not in ("auto", "vertical", "horizontal"):
        orientacion = "auto"
    if criterio not in CRITERIOS:
        criterio = "equilibrado"

    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(400, "El archivo llegó vacío. Vuelve a subirlo.")

    tid = uuid.uuid4().hex[:12]
    d = DATOS / tid
    d.mkdir(parents=True)
    (d / nombre).write_bytes(contenido)

    _guardar(tid, id=tid, nombre=nombre, archivo=nombre, estado="en_cola",
             transpositor=transpositor, orientacion=orientacion,
             sin_reguladores=sin_reguladores,
             criterio=criterio,
             mantener_original=mantener_original, creado=time.time())
    _pool.submit(_trabajar, tid, transpositor, titulo, orientacion,
                 sin_reguladores, etiqueta, criterio,
                 mantener_original)

    return JSONResponse({"id": tid}, status_code=202)


@app.get("/api/trabajos")
def listar() -> list:
    salida = []
    for f in DATOS.glob("*/estado.json"):
        try:
            salida.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    salida.sort(key=lambda x: x.get("creado", 0), reverse=True)
    return salida[:50]


@app.get("/api/trabajos/{tid}")
def consultar(tid: str) -> dict:
    return _leer(tid)


@app.get("/api/trabajos/{tid}/pdf")
def descargar(tid: str):
    datos = _leer(tid)
    pdf = _dir(tid) / "resultado.pdf"
    if not pdf.exists():
        raise HTTPException(404, "Ese trabajo todavía no tiene resultado")
    base = datos.get("titulo") or Path(datos.get("nombre", "partitura")).stem
    return FileResponse(pdf, filename=f"{base} (posiciones).pdf",
                        media_type="application/pdf")


@app.get("/api/trabajos/{tid}/musicxml")
def descargar_xml(tid: str):
    datos = _leer(tid)
    xml = _dir(tid) / "resultado.musicxml"
    if not xml.exists():
        raise HTTPException(404, "Ese trabajo todavía no tiene resultado")
    base = datos.get("titulo") or Path(datos.get("nombre", "partitura")).stem
    return FileResponse(xml, filename=f"{base}.musicxml",
                        media_type="application/vnd.recordare.musicxml")


@app.get("/api/descargar-todo")
def descargar_todo():
    """Todas las partituras terminadas en un solo ZIP."""
    listos = []
    for f in DATOS.glob("*/estado.json"):
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        pdf = f.parent / "resultado.pdf"
        if datos.get("estado") == "listo" and pdf.exists():
            listos.append((datos, pdf))

    if not listos:
        raise HTTPException(404, "Todavía no hay ninguna partitura terminada.")

    listos.sort(key=lambda x: x[0].get("creado", 0))

    tmp = Path(tempfile.mkdtemp(prefix="zip-"))
    destino = tmp / "Partituras con posiciones.zip"

    usados = {}
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        for datos, pdf in listos:
            base = datos.get("titulo") or Path(datos.get("nombre", "partitura")).stem
            nombre = f"{_sanear(base)} (posiciones).pdf"
            # Dos partituras pueden llamarse igual; no se pisan.
            if nombre in usados:
                usados[nombre] += 1
                nombre = f"{_sanear(base)} (posiciones) {usados[nombre]}.pdf"
            else:
                usados[nombre] = 1
            z.write(pdf, arcname=nombre)

    return FileResponse(destino, filename=destino.name,
                        media_type="application/zip",
                        background=_borrar_luego(tmp))


@app.post("/api/borrar-terminados")
def borrar_terminados() -> dict:
    """Vacía de la lista las partituras ya terminadas. Los fallos se quedan."""
    borrados = 0
    for f in list(DATOS.glob("*/estado.json")):
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if datos.get("estado") == "listo":
            shutil.rmtree(f.parent, ignore_errors=True)
            borrados += 1
    return {"borrados": borrados}


@app.delete("/api/trabajos/{tid}")
def borrar(tid: str) -> dict:
    shutil.rmtree(_dir(tid), ignore_errors=True)
    return {"borrado": tid}


# --- web --------------------------------------------------------------------

@app.get("/")
def inicio():
    return FileResponse(ESTATICOS / "index.html")


app.mount("/static", StaticFiles(directory=str(ESTATICOS)), name="static")
