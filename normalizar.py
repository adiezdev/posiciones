"""
Arreglar el PDF antes de que lo vea Audiveris.

El reconocimiento mejora mucho más limpiando la entrada que ajustando
umbrales después. Una fotocopia torcida dos grados le estropea a Audiveris
la detección de pentagramas, y a partir de ahí todo lo que venga detrás
—coordenadas, alturas, números— sale mal por mucho que afinemos.

Dos cosas se hacen aquí:

  descifrar   un PDF protegido (aunque sea con contraseña vacía, que es lo
              habitual en lo que sale de una fotocopiadora) hace que
              Audiveris se caiga sin explicar nada
  enderezar   si la página es un escaneo y está torcida, se rota

Enderezar cambia la geometría, así que la página corregida pasa a ser el
original a todos los efectos: sobre ella lee Audiveris y sobre ella se
estampan los números. Para un escaneo torcido eso es una mejora, no una
pérdida. En un PDF generado por ordenador la inclinación es cero y no se
toca nada: se devuelve el archivo tal cual.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Por debajo de esto no merece la pena tocar nada: el remedio (volver a
# rasterizar la página) cuesta más que la enfermedad.
INCLINACION_MINIMA = 0.20     # grados
INCLINACION_MAXIMA = 8.0      # más que esto no es inclinación, es otra cosa
UMBRAL_TINTA = 200
DPI_TRABAJO = 300
ANCHO_ANALISIS = 1200         # píxeles a los que se reduce para medir


@dataclass
class Pagina:
    numero: int
    escaneo: bool = False
    inclinacion: float = 0.0
    con_texto: bool = False
    con_imagen: bool = False


@dataclass
class Informe:
    origen: Path
    salida: Path
    cifrado: bool = False
    rasterizado: bool = False
    paginas: List[Pagina] = field(default_factory=list)
    aviso: Optional[str] = None

    @property
    def cambiado(self) -> bool:
        return self.cifrado or self.rasterizado

    def resumen(self) -> str:
        partes = []
        if self.cifrado:
            partes.append("descifrado")
        torcidas = [p for p in self.paginas
                    if abs(p.inclinacion) >= INCLINACION_MINIMA]
        if torcidas:
            peor = max(torcidas, key=lambda p: abs(p.inclinacion))
            partes.append("%d páginas torcidas (hasta %.1f°)"
                          % (len(torcidas), abs(peor.inclinacion)))
        if self.rasterizado:
            partes.append("enderezado")
        escaneos = sum(1 for p in self.paginas if p.escaneo)
        if escaneos:
            partes.append("%d de %d páginas son escaneo"
                          % (escaneos, len(self.paginas)))
        return "; ".join(partes) or "nada que corregir"


# --- medir ------------------------------------------------------------------

def medir_inclinacion(gris) -> float:
    """
    Cuántos grados está girada la página, por sus líneas de pentagrama.

    Una partitura es básicamente un montón de rectas horizontales muy
    largas. Si se suma la tinta de cada fila, derecha esa suma tiene picos
    altísimos en las líneas y valles entre ellas; torcida, los picos se
    reparten y la curva se aplana. Así que se prueba a "destorcer" por
    varios ángulos y se escoge el que deja la curva más picuda.

    Se hace sumando índices desplazados en vez de rotar la imagen: rotar
    sesenta veces una página a 300 ppp no acaba nunca.
    """
    import numpy as np

    tinta = gris < UMBRAL_TINTA
    ys, xs = np.nonzero(tinta)
    if ys.size < 500:
        return 0.0

    # Con muchísima tinta sobra con una muestra: el ángulo no cambia.
    if ys.size > 400000:
        paso = ys.size // 400000 + 1
        ys, xs = ys[::paso], xs[::paso]

    alto = gris.shape[0]

    def nitidez(grados: float) -> float:
        desplazado = ys - xs * np.tan(np.radians(grados))
        idx = np.rint(desplazado).astype(np.int64)
        idx -= idx.min()
        perfil = np.bincount(idx, minlength=alto).astype(np.float64)
        # La varianza premia los perfiles con picos altos y valles vacíos,
        # que es justo lo que da una página derecha.
        return float(perfil.var())

    mejor, mejor_valor = 0.0, None
    for grueso in np.arange(-INCLINACION_MAXIMA, INCLINACION_MAXIMA + 0.01, 0.5):
        v = nitidez(float(grueso))
        if mejor_valor is None or v > mejor_valor:
            mejor, mejor_valor = float(grueso), v

    for fino in np.arange(mejor - 0.5, mejor + 0.51, 0.05):
        v = nitidez(float(fino))
        if v > mejor_valor:
            mejor, mejor_valor = float(fino), v

    return round(mejor, 2)


def _tiene_texto(pagina) -> bool:
    try:
        return bool((pagina.extract_text() or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _tiene_imagen_grande(pagina) -> bool:
    """
    ¿La página es, básicamente, una foto?

    Es lo que distingue un escaneo de una partitura dibujada con vectores.
    No vale mirar si hay texto: una partitura grabada puede no tener ni una
    letra extraíble, y rasterizarla "para enderezarla" sería destrozarla
    sin ganar nada, porque derecha ya está.
    """
    try:
        recursos = pagina.get("/Resources")
        xobjetos = recursos.get("/XObject") if recursos else None
        if not xobjetos:
            return False
        caja = pagina.cropbox or pagina.mediabox
        area_pagina = float(caja.width) * float(caja.height)
        if area_pagina <= 0:
            return False
        for clave in xobjetos:
            obj = xobjetos[clave].get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            # Un escaneo trae una sola imagen con tantos píxeles como para
            # cubrir la hoja entera a resolución de escáner.
            ancho = float(obj.get("/Width") or 0)
            alto = float(obj.get("/Height") or 0)
            if ancho * alto >= area_pagina * 4.0:   # ~150 ppp o más
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def analizar(pdf: Path, dpi: int = 150) -> Informe:
    """Qué le pasa a este PDF, sin tocarlo."""
    from pypdf import PdfReader

    informe = Informe(origen=pdf, salida=pdf)
    try:
        lector = PdfReader(str(pdf))
        if lector.is_encrypted:
            informe.cifrado = True
            try:
                lector.decrypt("")
            except Exception:  # noqa: BLE001
                informe.aviso = ("El PDF está protegido con contraseña y no "
                                 "se ha podido abrir.")
                return informe
    except Exception as e:  # noqa: BLE001
        informe.aviso = "No se ha podido leer el PDF: %s" % e
        return informe

    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError:
        return informe

    try:
        doc = pdfium.PdfDocument(str(pdf))
    except Exception as e:  # noqa: BLE001
        informe.aviso = "No se ha podido abrir el PDF para mirarlo: %s" % e
        return informe

    try:
        for i in range(len(doc)):
            pag = Pagina(numero=i)
            if i < len(lector.pages):
                pag.con_texto = _tiene_texto(lector.pages[i])
                pag.con_imagen = _tiene_imagen_grande(lector.pages[i])
            try:
                imagen = doc[i].render(scale=dpi / 72.0).to_pil().convert("L")
            except Exception:  # noqa: BLE001
                informe.paginas.append(pag)
                continue
            if imagen.width > ANCHO_ANALISIS:
                alto = int(imagen.height * ANCHO_ANALISIS / imagen.width)
                imagen = imagen.resize((ANCHO_ANALISIS, alto))
            pag.inclinacion = medir_inclinacion(np.asarray(imagen))
            # Escaneo = una imagen que ocupa la hoja y nada de texto encima.
            # Solo en ese caso se puede rasterizar para enderezar sin perder
            # nada, porque ya era una imagen.
            pag.escaneo = pag.con_imagen and not pag.con_texto
            informe.paginas.append(pag)
    finally:
        doc.close()

    return informe


# --- corregir ---------------------------------------------------------------

def _hay_que_enderezar(informe: Informe) -> bool:
    return any(p.escaneo and INCLINACION_MINIMA <= abs(p.inclinacion)
               <= INCLINACION_MAXIMA for p in informe.paginas)


def normalizar(origen: Path, destino: Path, dpi: int = DPI_TRABAJO,
               enderezar: bool = True) -> Informe:
    """
    Deja en `destino` la versión sobre la que se va a trabajar.

    Si no hay nada que corregir, copia el original tal cual: no se
    re-rasteriza un PDF bueno, que solo serviría para perder calidad y
    engordar el archivo.
    """
    informe = analizar(origen)
    informe.salida = destino

    if informe.aviso and not informe.cifrado:
        shutil.copyfile(origen, destino)
        return informe

    necesita = enderezar and _hay_que_enderezar(informe)
    if not necesita:
        if informe.cifrado:
            _descifrar(origen, destino)
        else:
            shutil.copyfile(origen, destino)
        return informe

    try:
        _enderezar(origen, destino, informe, dpi)
        informe.rasterizado = True
    except Exception as e:  # noqa: BLE001
        informe.aviso = ("No se ha podido enderezar la partitura (%s); se "
                         "sigue con el original." % e)
        shutil.copyfile(origen, destino)
    return informe


def _descifrar(origen: Path, destino: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    lector = PdfReader(str(origen))
    if lector.is_encrypted:
        lector.decrypt("")
    escritor = PdfWriter()
    for pagina in lector.pages:
        escritor.add_page(pagina)
    with open(destino, "wb") as f:
        escritor.write(f)


def _enderezar(origen: Path, destino: Path, informe: Informe,
               dpi: int) -> None:
    """
    Rehace el PDF con cada página girada lo justo para enderezarla.

    Se conserva el tamaño del papel: lo que cambia es el contenido, así que
    todo lo que venga después —coordenadas de Audiveris, detección de
    pentagramas, estampado— trabaja sobre la página ya derecha y no hay que
    deshacer ningún giro más adelante.
    """
    import pypdfium2 as pdfium
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    doc = pdfium.PdfDocument(str(origen))
    try:
        lienzo = None
        for i in range(len(doc)):
            pagina = doc[i]
            ancho_pt = pagina.get_width()
            alto_pt = pagina.get_height()
            imagen = pagina.render(scale=dpi / 72.0).to_pil().convert("L")

            grados = 0.0
            if i < len(informe.paginas):
                p = informe.paginas[i]
                if p.escaneo and INCLINACION_MINIMA <= abs(p.inclinacion) \
                        <= INCLINACION_MAXIMA:
                    grados = p.inclinacion
            if grados:
                # `medir_inclinacion` devuelve la pendiente que hay que
                # compensar en su propio sistema de coordenadas (y hacia
                # abajo), que es el contrario al de PIL. Girar con el signo
                # equivocado no deja la página igual: la tuerce el doble.
                imagen = imagen.rotate(grados, resample=Image.BICUBIC,
                                       expand=False, fillcolor=255)

            if lienzo is None:
                lienzo = canvas.Canvas(str(destino),
                                       pagesize=(ancho_pt, alto_pt))
            else:
                lienzo.setPageSize((ancho_pt, alto_pt))
            lienzo.drawImage(ImageReader(imagen), 0, 0,
                             width=ancho_pt, height=alto_pt)
            lienzo.showPage()
        if lienzo is not None:
            lienzo.save()
    finally:
        doc.close()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--salida", type=Path,
                    help="si no se pone, solo analiza y no escribe nada")
    ap.add_argument("--sin-enderezar", action="store_true")
    args = ap.parse_args()

    if args.salida:
        informe = normalizar(args.pdf, args.salida,
                             enderezar=not args.sin_enderezar)
    else:
        informe = analizar(args.pdf)

    print(informe.resumen())
    if informe.aviso:
        print("aviso: %s" % informe.aviso)
    for p in informe.paginas:
        print("  pág %d: %s, inclinación %+.2f°%s"
              % (p.numero, "escaneo" if p.escaneo else "generado por ordenador",
                 p.inclinacion,
                 "" if abs(p.inclinacion) < INCLINACION_MINIMA else "  <-- torcida"))
    if args.salida:
        print("escrito %s" % informe.salida)


if __name__ == "__main__":
    main()
