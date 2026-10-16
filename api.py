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
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from music21 import converter

import depurar
import estilo
import normalizar
import omr
import partitura
import superponer
import trabajos
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
RETENCION_DIAS = int(os.environ.get("RETENCION_DIAS", "30"))

# La versión la pone la construcción de la imagen. Antes era una cadena
# escrita a mano en dos sitios, que había que acordarse de cambiar.
VERSION = (os.environ.get("VERSION")
           or os.environ.get("GIT_SHA")
           or str(int(Path(__file__).stat().st_mtime)))

DATOS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Posiciones de trombón", version="3.0")

_pool = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()


# --- persistencia mínima ----------------------------------------------------

_sanear = trabajos.sanear


def _borrar_luego(d: Path) -> BackgroundTask:
    """Limpia una carpeta temporal cuando la descarga ha terminado."""
    return BackgroundTask(shutil.rmtree, str(d), ignore_errors=True)


def _dir(tid: str) -> Path:
    d = trabajos.carpeta(DATOS, tid)
    if d is None:
        raise HTTPException(404, "Ese trabajo no existe")
    return d


def _leer(tid: str) -> dict:
    datos = trabajos.leer(DATOS, tid)
    if datos is None:
        raise HTTPException(404, "Ese trabajo no existe")
    return datos


def _guardar(tid: str, **cambios) -> dict:
    return trabajos.guardar(DATOS, tid, **cambios)


# --- procesos externos ------------------------------------------------------

def _libro_omr(export: Path):
    """El .omr que Audiveris deja junto al export, si se entiende."""
    for ruta in sorted(export.rglob("*.omr")):
        libro = omr.leer(ruta)
        if libro is not None:
            return libro
    return None


def _compases(ruta: Path) -> int:
    """Cuántos compases trae un export, para elegir el más completo."""
    try:
        return len(partitura.cargar(ruta).getroot().findall(".//measure"))
    except Exception:  # noqa: BLE001
        return 0


def _correr(cmd: list, timeout: int, etiqueta: str,
            tolerar_si: Optional[Path] = None) -> None:
    """
    Lanza un proceso externo.

    `tolerar_si` es una carpeta: si el proceso devuelve un código distinto
    de cero pero ha dejado algo ahí, se sigue adelante. Audiveris protesta
    por cosas que no impiden exportar la partitura —una página que no
    entiende, un aviso de fuente— y abortar el trabajo entero por eso era
    tirar a la basura lo que sí había reconocido.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{etiqueta} tardó más de {timeout // 60} minutos y se ha cancelado.")
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout or "").strip().splitlines()
        cola = " ".join(detalle[-3:])[:400] if detalle else "sin detalle"
        if tolerar_si is not None and any(tolerar_si.rglob("*")):
            return
        raise RuntimeError(f"{etiqueta} no pudo completar el proceso: {cola}")


def _trabajar(tid: str, transpositor: bool, titulo_manual: str,
              orientacion: str, sin_reguladores: bool = True,
              etiqueta: str = "", criterio: str = "equilibrado",
              mantener_original: bool = False,
              lectura: str = "auto") -> None:
    d = DATOS / tid
    try:
        datos = json.loads((d / "estado.json").read_text(encoding="utf-8"))
        pdf_in = d / datos["archivo"]
        reglas = CRITERIOS.get(criterio, CRITERIOS["equilibrado"])
        cercanas = reglas["preferir_cercanas"]
        peso = reglas["peso"]

        # Arreglar la entrada antes de que la vea Audiveris. Una fotocopia
        # torcida dos grados le estropea la detección de pentagramas y a
        # partir de ahí falla todo lo demás. Si no hay nada que corregir,
        # esto copia el archivo y ya está.
        _guardar(tid, estado="preparando")
        limpio = d / "normalizado.pdf"
        informe = normalizar.normalizar(pdf_in, limpio)
        if informe.cambiado:
            pdf_in = limpio
        _guardar(tid, entrada=informe.resumen(),
                 enderezado=informe.rasterizado,
                 aviso_entrada=informe.aviso)

        _guardar(tid, estado="reconociendo")
        export = d / "export"
        export.mkdir(exist_ok=True)
        # `-save` deja también el .omr, que trae la geometría que Audiveris
        # ha medido de verdad. Cuesta poco y evita tener que adivinarla.
        _correr([AUDIVERIS, "-batch", "-export", "-save",
                 "-output", str(export), str(pdf_in)],
                TIMEOUT_OMR, "El reconocimiento de la partitura",
                tolerar_si=export)

        encontrados = sorted(export.rglob("*.mxl")) or sorted(export.rglob("*.xml"))
        if not encontrados:
            raise RuntimeError(
                "No se ha reconocido ninguna partitura en el PDF. "
                "Comprueba que el archivo contiene música escrita y no solo texto o imágenes.")

        # Audiveris parte los libros grandes en varias hojas y exporta una por
        # hoja. Quedarse con la primera alfabética se comía el resto de la
        # partitura sin decir nada. Nos quedamos con la más completa y lo
        # avisamos para que se note que ha pasado.
        crudo = max(encontrados, key=_compases)

        # Cómo se lee la particella. Audiveris da la altura escrita y nunca
        # un <transpose>, porque no puede saberlo: en "auto" se deduce de la
        # clave, y una parte de trombón en clave de sol es una parte en Sib.
        arbol_crudo = partitura.cargar(crudo)
        semitonos = superponer.LECTURAS.get(lectura)
        if semitonos is None:
            semitonos = superponer._lectura_automatica(arbol_crudo.getroot())
        _guardar(tid, lectura=lectura, semitonos=semitonos)
        if len(encontrados) > 1:
            _guardar(tid, exportes=len(encontrados), aviso_exportes=(
                f"Audiveris ha partido el PDF en {len(encontrados)} hojas y "
                f"solo se ha procesado la más larga ({crudo.name}). "
                "Las demás se han quedado sin números."))

        # --- Vía A: estampar sobre el PDF original, sin redibujar nada ---
        if mantener_original:
            _guardar(tid, estado="anotando", modo="superpuesto")
            libro = _libro_omr(export)
            plano = superponer.preparar(partitura.cargar(crudo),
                                        transpositor, cercanas, peso,
                                        pdf=pdf_in, libro_omr=libro,
                                        lectura=lectura)
            if plano is not None:
                _guardar(tid, estado="generando")
                pdf_out = d / "resultado.pdf"
                diag_paginas: list = []
                puestas, medido, dudosas = superponer.estampar(
                    pdf_in, pdf_out, plano, diag=diag_paginas)

                # La hoja de marcas es lo que hace posible repasar a mano:
                # los números ya resueltos, en puntos de la página tal como
                # se ve. Corregir uno no obliga a rehacer nada de lo demás.
                hoja = superponer.hoja_de_marcas(diag_paginas)
                (d / "marcas.json").write_text(
                    json.dumps(hoja, ensure_ascii=False), encoding="utf-8")

                score = converter.parse(str(crudo))
                avisos = validar(score, transpositor, semitonos)
                _guardar(
                    tid, estado="listo", notas=puestas, dudosas=dudosas,
                    lectura=lectura, semitonos=semitonos,
                    modo_x=plano.modo_x,
                    geometria=("audiveris" if plano.pentagramas_omr
                               else "medida en el PDF"),
                    pentagramas_medidos=medido,
                    rellenadas=plano.rellenadas,
                    encaje=round(plano.acierto * 100),
                    fuera_rango=max(plano.total - puestas, 0),
                    titulo=titulo_manual.strip() or pdf_in.stem,
                    papel="el de tu PDF original",
                    base=pdf_in.name,
                    musicxml=str(crudo.relative_to(d)),
                    revisable=True,
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
        avisos = validar(score, transpositor, semitonos)
        if sin_reguladores:
            quitar_reguladores(score)
        notas, fuera = anotar_score(score, transpositor,
                                    preferir_cercanas=cercanas,
                                    peso_movimiento=peso,
                                    semitonos=semitonos)

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


def _encolar(t: dict) -> None:
    """Vuelve a meter en la cola un trabajo con sus mismos ajustes."""
    p = trabajos.parametros(t)
    _pool.submit(_trabajar, t["id"], p["transpositor"], p["titulo_manual"],
                 p["orientacion"], p["sin_reguladores"], p["etiqueta"],
                 p["criterio"], p["mantener_original"], p["lectura"])


@app.on_event("startup")
def al_arrancar() -> None:
    """
    Recoger lo que dejó el proceso anterior.

    El servicio se actualiza solo, así que un reinicio en mitad de un
    reconocimiento de media hora no es una rareza. Antes esas partituras se
    quedaban para siempre en "Leyendo la partitura" y el usuario esperando
    a que acabara algo que ya no existía.
    """
    try:
        borrados = trabajos.limpiar_viejos(DATOS, RETENCION_DIAS)
        if borrados:
            print(f"[inicio] {borrados} trabajos viejos borrados")
        resultado = trabajos.recuperar(DATOS)
        for t in resultado["rendidos"]:
            print(f"[inicio] {t['id']} se rinde tras varios reintentos")
        for t in resultado["reencolados"]:
            print(f"[inicio] {t['id']} vuelve a la cola")
            _encolar(t)
    except Exception as e:  # noqa: BLE001
        # Que un fallo recogiendo la cola no impida levantar el servicio.
        print(f"[inicio] no se ha podido recuperar la cola: {e}")


# --- API --------------------------------------------------------------------

@app.get("/salud")
def salud() -> dict:
    return {
        "ok": True,
        "audiveris": Path(AUDIVERIS).exists(),
        "mscore": shutil.which(MSCORE) is not None,
        "datos": str(DATOS),
        "version": VERSION,
        "en_marcha": len(trabajos.pendientes(DATOS)),
    }



@app.get("/api/version")
def api_version() -> JSONResponse:
    # Sin caché por si delante hay un proxy (en un NAS suele haberlo): una
    # respuesta cacheada aquí hace que la pestaña abierta no se entere nunca
    # de que el servidor ha cambiado, que es justo lo que esto evita.
    return JSONResponse(
        {"version": VERSION},
        headers={"Cache-Control": "no-store, max-age=0"})


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
    lectura: str = Form("auto"),
):
    nombre = _sanear(archivo.filename or "partitura.pdf")
    if not nombre.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se admiten archivos PDF.")
    if orientacion not in ("auto", "vertical", "horizontal"):
        orientacion = "auto"
    if criterio not in CRITERIOS:
        criterio = "equilibrado"
    if lectura not in superponer.LECTURAS:
        lectura = "auto"

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
             criterio=criterio, lectura=lectura,
             titulo_manual=titulo, etiqueta=etiqueta,
             mantener_original=mantener_original, creado=time.time(),
             intentos=0)
    _pool.submit(_trabajar, tid, transpositor, titulo, orientacion,
                 sin_reguladores, etiqueta, criterio,
                 mantener_original, lectura)

    return JSONResponse({"id": tid}, status_code=202)


@app.get("/api/trabajos")
def listar() -> list:
    # Sin recortar. Antes se devolvian 50 y con una carpeta de 60 partituras
    # faltaban diez sin que nada lo dijera, que es peor que tardar un poco
    # mas en pintar la lista.
    return trabajos.todos(DATOS)


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


# --- repaso a mano ----------------------------------------------------------
#
# Ningún reconocimiento óptico acierta el 100%, y prometer lo contrario es
# engañar. Lo que sí se puede es dejar el repaso en dos minutos: ver la
# partitura con sus números encima, tocar los que estén mal y volver a
# generar el PDF. Lo que no se toca sale exactamente igual que antes.

def _base_pdf(tid: str, datos: dict) -> Path:
    """El PDF sobre el que se estampa (el original, o el enderezado)."""
    d = _dir(tid)
    nombre = datos.get("base") or datos.get("archivo")
    ruta = d / nombre if nombre else None
    if ruta is None or not ruta.exists():
        raise HTTPException(404, "No está el PDF de partida de ese trabajo")
    return ruta


def _hoja(tid: str) -> dict:
    f = _dir(tid) / "marcas.json"
    if not f.exists():
        raise HTTPException(
            404, "Ese trabajo no se puede repasar: se generó redibujando la "
                 "partitura, no estampando sobre tu PDF.")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/api/trabajos/{tid}/revision")
def revision(tid: str) -> dict:
    datos = _leer(tid)
    hoja = _hoja(tid)
    hoja["titulo"] = datos.get("titulo") or datos.get("nombre")
    return hoja


@app.get("/api/trabajos/{tid}/pagina/{numero}.png")
def pagina_png(tid: str, numero: int, escala: float = 1.5):
    """La página sin números, para pintar encima en el navegador."""
    import pypdfium2 as pdfium

    datos = _leer(tid)
    base = _base_pdf(tid, datos)
    escala = min(max(escala, 0.5), 4.0)

    doc = pdfium.PdfDocument(str(base))
    try:
        if not 0 <= numero < len(doc):
            raise HTTPException(404, "Esa página no existe")
        imagen = doc[numero].render(scale=escala).to_pil()
    finally:
        doc.close()

    tmp = Path(tempfile.mkdtemp(prefix="pag-"))
    destino = tmp / f"{numero}.png"
    imagen.save(destino, "PNG", optimize=True)
    return FileResponse(destino, media_type="image/png",
                        background=_borrar_luego(tmp))


@app.post("/api/trabajos/{tid}/revision")
async def guardar_revision(tid: str, hoja: dict = Body(...)) -> dict:
    """Vuelve a estampar con las correcciones del usuario."""
    datos = _leer(tid)
    base = _base_pdf(tid, datos)
    anterior = _hoja(tid)

    limpia = _validar_hoja(hoja, anterior)
    d = _dir(tid)
    (d / "marcas.json").write_text(json.dumps(limpia, ensure_ascii=False),
                                   encoding="utf-8")
    puestas = superponer.estampar_resueltas(base, d / "resultado.pdf", limpia)
    a_mano = sum(1 for p in limpia["paginas"] for n in p["numeros"]
                 if n.get("a_mano"))
    _guardar(tid, notas=puestas, revisado=True, corregidas=a_mano,
             dudosas=sum(1 for p in limpia["paginas"] for n in p["numeros"]
                         if not n.get("a_mano")
                         and n.get("confianza", 1.0) < superponer.CONFIANZA_DUDOSA))
    return {"notas": puestas, "corregidas": a_mano}


def _validar_hoja(hoja: dict, anterior: dict) -> dict:
    """
    Se fía de las correcciones, pero no de la geometría.

    El navegador puede mandar los números que quiera y donde quiera dentro
    de la página, que es justo el sentido de poder corregir a mano. Lo que
    no puede es cambiar el tamaño de la hoja ni inventarse páginas: eso se
    coge de lo que ya había.
    """
    paginas_previas = {p["pagina"]: p for p in anterior.get("paginas", [])}
    salida = []
    for pag in hoja.get("paginas", []):
        previa = paginas_previas.get(pag.get("pagina"))
        if previa is None:
            continue
        numeros = []
        for n in (pag.get("numeros") or [])[:4000]:
            texto = str(n.get("texto") or "").strip()[:4]
            if not texto:
                continue    # borrar un número es dejarlo sin texto
            try:
                x, y = float(n["x"]), float(n["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= x <= previa["ancho_pt"] and 0 <= y <= previa["alto_pt"]):
                continue
            numeros.append({
                "x": round(x, 2), "y": round(y, 2), "texto": texto,
                "sistema": int(n.get("sistema", 0)),
                "confianza": min(max(float(n.get("confianza", 1.0)), 0.0), 1.0),
                "cuerpo": round(float(n.get("cuerpo") or 0.0), 2),
                "estimada": bool(n.get("estimada")),
                "a_mano": bool(n.get("a_mano")),
            })
        nueva = dict(previa)
        nueva["numeros"] = numeros
        salida.append(nueva)
    return {"version": anterior.get("version", 1), "paginas": salida}


@app.get("/api/trabajos/{tid}/diagnostico")
def diagnostico(tid: str):
    """
    El PDF de diagnóstico: lo que ha visto la máquina, pintado encima.

    Pentagramas detectados, cabezas reconocidas, dónde ha ido cada número y
    cuáles se han reajustado o descartado. Es lo que hay que mirar cuando
    una partitura sale rara, y tenerlo a un clic evita pedirle a nadie que
    se instale nada para poder enseñar qué está pasando.
    """
    datos = _leer(tid)
    d = _dir(tid)
    base = _base_pdf(tid, datos)
    relativa = datos.get("musicxml")
    xml = (d / relativa) if relativa else None
    if xml is None or not xml.exists():
        encontrados = sorted((d / "export").rglob("*.mxl"))             or sorted((d / "export").rglob("*.xml"))
        if not encontrados:
            raise HTTPException(
                404, "De este trabajo no queda el reconocimiento, así que no "
                     "se puede rehacer el diagnóstico.")
        xml = max(encontrados, key=_compases)

    analisis = depurar.analizar(base, xml, bool(datos.get("transpositor")),
                                lectura=datos.get("lectura", "auto"))
    tmp = Path(tempfile.mkdtemp(prefix="diag-"))
    nombre = datos.get("titulo") or Path(datos.get("nombre", "partitura")).stem
    salida = tmp / f"{_sanear(nombre)} (diagnostico).pdf"
    if not analisis["ok"]:
        raise HTTPException(
            409, "El estampado no llegó a hacerse (%s), así que no hay nada "
                 "que diagnosticar." % analisis["motivo"])
    depurar.pintar(base, salida, analisis)
    return FileResponse(salida, filename=salida.name,
                        media_type="application/pdf",
                        background=_borrar_luego(tmp))


@app.get("/revisar")
def revisar():
    return FileResponse(ESTATICOS / "revisar.html")


# --- web --------------------------------------------------------------------

@app.get("/")
def inicio():
    return FileResponse(ESTATICOS / "index.html")


app.mount("/static", StaticFiles(directory=str(ESTATICOS)), name="static")
