"""
Mide el estampado sobre un corpus y avisa si un cambio lo empeora.

Este proyecto se ha estado ajustando a ojo: mirar un PDF, subir un umbral,
mirar otro, bajarlo. El historial está lleno de commits que suben y bajan el
mismo número. Esto es lo que faltaba para dejar de hacer eso.

Dos modos:

  --sinteticos   fabrica una batería de partituras con verdad conocida
                 (giradas, recortadas, con coordenadas que faltan...) y mide
                 el error en puntos. No necesita Audiveris ni PDFs reales,
                 así que vale para CI.

  <carpeta>      corre sobre un corpus de partituras de verdad. Cada caso es
                 una subcarpeta con el PDF y, si lo hay, el export de
                 Audiveris y un verdad.json.

Comparar contra una medida anterior es lo que convierte esto en una red:

    python metricas.py --sinteticos --json base.json
    ...tocas algo...
    python metricas.py --sinteticos --base base.json     # sale 1 si empeora
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import depurar

# La batería estándar. Cada caso reproduce un fallo que hemos visto en PDFs
# reales; si alguno deja de pasar, es que se ha roto algo concreto.
BATERIA = [
    ("base", {}),
    ("una-pagina", {"paginas": 1, "sistemas": 6}),
    ("apretada", {"sistemas": 6, "compases": 6}),
    ("girada-90", {"rotate": 90}),
    ("girada-180", {"rotate": 180}),
    ("girada-270", {"rotate": 270}),
    ("recortada", {"margen_cropbox": 18.0}),
    ("recortada-y-girada", {"margen_cropbox": 12.0, "rotate": 90}),
    ("sin-coordenadas-20", {"sin_dx": 0.20}),
    ("sin-coordenadas-50", {"sin_dx": 0.50}),
    ("sin-coordenadas-80", {"sin_dx": 0.80}),
    ("ritmo-plano", {"ritmo": False, "sin_dx": 0.50}),
    # El borde negro de escáner revienta la detección de pentagramas por
    # umbral de tinta. Con el .omr la página se salva; sin él, cae a modo
    # degradado. Los dos casos miden justo esa diferencia.
    ("borde-negro", {"borde_negro": True}),
    ("borde-negro-con-omr", {"borde_negro": True, "_omr": True}),
    ("base-con-omr", {"_omr": True}),
    # Clave de do en 4ª, que en trombón aparece a mitad de obra.
    ("clave-tenor", {"clave": "tenor"}),
    # Particella de banda: "Trombón en Sib", clave de sol. Suena una novena
    # mayor más grave de lo escrito y el MusicXML no lo dice en ninguna
    # parte. Si no se deduce de la clave, TODOS los números salen mal sin
    # que nada avise.
    ("sib-clave-sol", {"clave": "sol", "transposicion": "sib"}),
    # Cabezas como glifo de texto, que es como las dibuja cualquier editor.
    # Activa la comprobación "¿hay una nota debajo de este número?", que con
    # cabezas vectoriales no llega a ejecutarse.
    ("cabezas-de-texto", {"cabezas_texto": True}),
    ("cabezas-de-texto-sin-dx", {"cabezas_texto": True, "sin_dx": 0.50}),
    # Fotocopia torcida: la página se rasteriza y se gira 1,4°. La verdad
    # sigue siendo la de la página derecha, así que esto mide si
    # normalizar.py la devuelve a su sitio.
    ("escaneo-torcido", {"inclinar": 1.4, "paginas": 1}),
    ("escaneo-derecho", {"inclinar": 0.01, "paginas": 1}),
]

CABECERA = ("caso", "notas", "estamp", "cob%", "dud", "err_med",
            "err_p95", "ok%", "num%", "incl", "pent", "geom", "s")


def generar_bateria(destino: Path) -> List[Path]:
    """Fabrica las partituras sintéticas de la batería."""
    sys.path.insert(0, str(Path(__file__).parent))
    from pruebas.sintetico import Caso

    carpetas = []
    for nombre, kwargs in BATERIA:
        kwargs = dict(kwargs)
        usar_omr = kwargs.pop("_omr", False)
        carpeta = destino / nombre
        Caso(**kwargs).generar(carpeta)
        if not usar_omr:
            # La batería genera siempre el .omr; en los casos que no lo usan
            # se quita, para medir cuánto aporta tenerlo.
            (carpeta / "libro.omr").unlink(missing_ok=True)
        carpetas.append(carpeta)
    return carpetas


def buscar_casos(raiz: Path) -> List[Path]:
    """Subcarpetas que tienen al menos un PDF."""
    if any(raiz.glob("*.pdf")):
        return [raiz]
    return sorted(d for d in raiz.iterdir()
                  if d.is_dir() and any(d.glob("*.pdf")))


def _primero(carpeta: Path, *patrones: str) -> Optional[Path]:
    for patron in patrones:
        encontrados = sorted(carpeta.glob(patron))
        if encontrados:
            return encontrados[0]
    return None


def correr_audiveris(pdf: Path, destino: Path) -> Optional[Path]:
    """Saca el MusicXML con Audiveris, si está instalado."""
    binario = os.environ.get("AUDIVERIS_BIN", "/opt/audiveris/bin/audiveris-cli")
    if not (Path(binario).exists() or shutil.which(binario)):
        return None
    destino.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([binario, "-batch", "-export", "-output",
                        str(destino), str(pdf)],
                       capture_output=True, text=True,
                       timeout=int(os.environ.get("TIMEOUT_OMR", "2400")))
    salidas = sorted(destino.rglob("*.mxl")) or sorted(destino.rglob("*.xml"))
    if not salidas:
        print("  audiveris no exportó nada (código %d)" % r.returncode)
        return None
    # La hoja más larga, por el mismo motivo que en api.py.
    def compases(p: Path) -> int:
        try:
            import partitura
            return len(partitura.cargar(p).getroot().findall(".//measure"))
        except Exception:  # noqa: BLE001
            return 0
    return max(salidas, key=compases)


def medir(carpeta: Path, con_omr: bool = False, con_debug: bool = False,
          normalizar_antes: bool = True) -> dict:
    """Mide un caso. Nunca lanza: un caso roto es un resultado, no un corte."""
    nombre = carpeta.name
    inicio = time.time()
    pdf = _primero(carpeta, "partitura.pdf", "*.pdf")
    if pdf is None:
        return {"caso": nombre, "ok": False, "motivo": "sin_pdf"}

    xml = _primero(carpeta, "export.mxl", "export.musicxml", "export.xml",
                   "*.mxl", "*.musicxml")
    if xml is None and con_omr:
        xml = correr_audiveris(pdf, carpeta / "omr")
    if xml is None:
        return {"caso": nombre, "ok": False, "motivo": "sin_export_musicxml"}

    # Mismo orden que en producción: primero se arregla la entrada.
    inclinacion = 0.0
    if normalizar_antes:
        import normalizar as _norm
        corregido = carpeta / "normalizado.pdf"
        inf = _norm.normalizar(pdf, corregido)
        inclinacion = max((abs(x.inclinacion) for x in inf.paginas),
                          default=0.0)
        pdf = corregido

    omr_ruta = carpeta / "libro.omr"
    try:
        analisis = depurar.analizar(
            pdf, xml, omr_ruta=omr_ruta if omr_ruta.exists() else None)
    except Exception as e:  # noqa: BLE001
        return {"caso": nombre, "ok": False,
                "motivo": "excepcion:%s: %s" % (type(e).__name__, e)}

    if not analisis["ok"]:
        return {"caso": nombre, "ok": False, "motivo": analisis["motivo"],
                "segundos": round(time.time() - inicio, 1)}

    fila = {"caso": nombre, "ok": True, "motivo": None,
            "inclinacion": round(inclinacion, 2)}
    fila.update(analisis["resumen"])

    verdad_ruta = carpeta / "verdad.json"
    if verdad_ruta.exists():
        verdad = json.loads(verdad_ruta.read_text(encoding="utf-8"))
        fila["comparacion"] = depurar.comparar(analisis, verdad)

    if con_debug:
        verdad = (json.loads(verdad_ruta.read_text(encoding="utf-8"))
                  if verdad_ruta.exists() else None)
        salida = carpeta / "diagnostico.pdf"
        depurar.pintar(pdf, salida, analisis, verdad)
        fila["diagnostico"] = str(salida)

    fila["segundos"] = round(time.time() - inicio, 1)
    return fila


# --- presentación -----------------------------------------------------------

def _celdas(fila: dict) -> tuple:
    if not fila["ok"]:
        return (fila["caso"], "-", "-", "-", "-", "-", "-", "-", "-", "-",
                "-", (fila.get("motivo") or "?")[:22],
                str(fila.get("segundos", "-")))
    c = fila.get("comparacion") or {}
    return (
        fila["caso"],
        str(fila["notas_omr"]),
        str(fila["estampadas"]),
        "%.0f" % (100 * fila["cobertura"]),
        str(fila.get("dudosas", 0)),
        ("%.2f" % c["error_mediano_pt"]) if c.get("error_mediano_pt") is not None else "-",
        ("%.2f" % c["error_p95_pt"]) if c.get("error_p95_pt") is not None else "-",
        ("%.0f" % (100 * c["tasa_acierto"])) if c else "-",
        ("%.0f" % (100 * c["tasa_numero"]))
        if c.get("tasa_numero") is not None else "-",
        "%.1f" % fila.get("inclinacion", 0.0),
        "%d/%d" % (fila["paginas_medidas"], fila["paginas_con_marcas"]),
        "omr" if fila.get("paginas_con_omr") else "imagen",
        str(fila["segundos"]),
    )


def tabla(filas: List[dict]) -> str:
    cuerpo = [CABECERA] + [_celdas(f) for f in filas]
    anchos = [max(len(c[i]) for c in cuerpo) for i in range(len(CABECERA))]
    lineas = []
    for i, c in enumerate(cuerpo):
        lineas.append("  ".join(t.ljust(anchos[j]) if j == 0
                                else t.rjust(anchos[j])
                                for j, t in enumerate(c)))
        if i == 0:
            lineas.append("  ".join("-" * a for a in anchos))
    return "\n".join(lineas)


def comparar_con_base(filas: List[dict], base: List[dict],
                      tolerancia: float) -> List[str]:
    """Qué ha empeorado respecto a la medida anterior."""
    antes = {f["caso"]: f for f in base}
    quejas = []
    for f in filas:
        a = antes.get(f["caso"])
        if a is None:
            continue
        if a.get("ok") and not f.get("ok"):
            quejas.append("%s: antes funcionaba y ahora se rinde (%s)"
                          % (f["caso"], f.get("motivo")))
            continue
        if not (a.get("ok") and f.get("ok")):
            continue
        for clave, etiqueta in (("cobertura", "cobertura"),):
            if f[clave] < a[clave] - tolerancia:
                quejas.append("%s: %s %.3f -> %.3f"
                              % (f["caso"], etiqueta, a[clave], f[clave]))
        ca, cf = a.get("comparacion"), f.get("comparacion")
        if ca and cf:
            if cf["tasa_acierto"] < ca["tasa_acierto"] - tolerancia:
                quejas.append("%s: notas bien situadas %.3f -> %.3f"
                              % (f["caso"], ca["tasa_acierto"],
                                 cf["tasa_acierto"]))
            if (ca.get("tasa_numero") is not None
                    and cf.get("tasa_numero") is not None
                    and cf["tasa_numero"] < ca["tasa_numero"] - tolerancia):
                quejas.append("%s: números posibles %.3f -> %.3f"
                              % (f["caso"], ca["tasa_numero"],
                                 cf["tasa_numero"]))
            if cf["error_p95_pt"] and ca["error_p95_pt"]:
                if cf["error_p95_pt"] > ca["error_p95_pt"] * 1.25 + 0.1:
                    quejas.append("%s: error p95 %.2f -> %.2f pt"
                                  % (f["caso"], ca["error_p95_pt"],
                                     cf["error_p95_pt"]))
    return quejas


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("carpeta", type=Path, nargs="?",
                    help="corpus: una subcarpeta por partitura")
    ap.add_argument("--sinteticos", action="store_true",
                    help="fabricar y medir la batería sintética")
    ap.add_argument("--guardar-sinteticos", type=Path, metavar="DIR",
                    help="dejar las partituras sintéticas aquí en vez de borrarlas")
    ap.add_argument("--json", type=Path, dest="json_salida")
    ap.add_argument("--base", type=Path, help="JSON anterior con el que comparar")
    ap.add_argument("--tolerancia", type=float, default=0.01)
    ap.add_argument("--omr", action="store_true",
                    help="llamar a Audiveris cuando falte el export")
    ap.add_argument("--sin-normalizar", action="store_true",
                    help="no pasar por normalizar.py (para medir cuánto aporta)")
    ap.add_argument("--debug", action="store_true",
                    help="escribir también el PDF de diagnóstico de cada caso")
    args = ap.parse_args()

    if not args.sinteticos and args.carpeta is None:
        ap.error("hace falta una carpeta de corpus o --sinteticos")

    temporal = None
    carpetas: List[Path] = []
    if args.sinteticos:
        destino = args.guardar_sinteticos
        if destino is None:
            temporal = tempfile.mkdtemp(prefix="sinteticos-")
            destino = Path(temporal)
        carpetas += generar_bateria(destino)
    if args.carpeta:
        carpetas += buscar_casos(args.carpeta)

    try:
        filas = [medir(c, con_omr=args.omr, con_debug=args.debug,
                       normalizar_antes=not args.sin_normalizar)
                 for c in carpetas]
    finally:
        if temporal and not args.debug:
            shutil.rmtree(temporal, ignore_errors=True)
        elif temporal:
            print("partituras sintéticas en %s" % temporal)

    print(tabla(filas))
    print()
    buenos = [f for f in filas if f["ok"]]
    print("%d de %d casos estampados" % (len(buenos), len(filas)))
    if buenos:
        notas = sum(f["notas_omr"] for f in buenos)
        puestas = sum(f["estampadas"] for f in buenos)
        print("cobertura global %.1f%% (%d de %d notas)"
              % (100 * puestas / notas if notas else 0, puestas, notas))
        conv = [f["comparacion"] for f in buenos if f.get("comparacion")]
        if conv:
            ok = sum(c["aciertos"] for c in conv)
            tot = sum(c["notas_verdad"] for c in conv)
            print("bien situadas %.1f%% (%d de %d, contra la verdad)"
                  % (100 * ok / tot if tot else 0, ok, tot))

    for f in filas:
        if not f["ok"]:
            print("  FALLA %-22s %s" % (f["caso"], f.get("motivo")))

    if args.json_salida:
        args.json_salida.write_text(
            json.dumps(filas, ensure_ascii=False, indent=1), encoding="utf-8")
        print("medidas en %s" % args.json_salida)

    if args.base:
        base = json.loads(args.base.read_text(encoding="utf-8"))
        quejas = comparar_con_base(filas, base, args.tolerancia)
        if quejas:
            print("\nREGRESIÓN respecto a %s:" % args.base)
            for q in quejas:
                print("  - %s" % q)
            raise SystemExit(1)
        print("\nsin regresiones respecto a %s" % args.base)

    if any(not f["ok"] for f in filas) and args.sinteticos:
        # En la batería sintética no hay excusa: todos deberían salir.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
