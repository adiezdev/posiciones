"""
La cola sobrevive a un reinicio.

El servicio se actualiza solo con watchtower, así que un reinicio en mitad
de un reconocimiento de media hora va a pasar. Antes el trabajo se quedaba
para siempre en "Leyendo la partitura" y el usuario esperando a que
acabara algo que ya no existía.

Ejecutar: python pruebas/test_trabajos.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trabajos as T


def crear(datos: Path, tid: str, estado: str, **extra) -> None:
    d = datos / tid
    d.mkdir(parents=True, exist_ok=True)
    datos_trabajo = {"id": tid, "estado": estado, "creado": time.time()}
    datos_trabajo.update(extra)
    (d / "estado.json").write_text(json.dumps(datos_trabajo), encoding="utf-8")


def main() -> None:
    datos = Path(tempfile.mkdtemp(prefix="trabajos-"))

    crear(datos, "aaaa01", "reconociendo", criterio="vara", lectura="sib-sol")
    crear(datos, "aaaa02", "en_cola")
    crear(datos, "aaaa03", "listo")
    crear(datos, "aaaa04", "error")
    crear(datos, "aaaa05", "generando", intentos=T.MAX_REINTENTOS)

    # --- qué quedó a medias -----------------------------------------------
    pend = {t["id"] for t in T.pendientes(datos)}
    assert pend == {"aaaa01", "aaaa02", "aaaa05"}, pend

    # --- recuperar: se reintenta, pero no para siempre --------------------
    r = T.recuperar(datos)
    assert {t["id"] for t in r["reencolados"]} == {"aaaa01", "aaaa02"}, r
    assert {t["id"] for t in r["rendidos"]} == {"aaaa05"}, r
    assert T.leer(datos, "aaaa05")["estado"] == "error", \
        "a la tercera hay que rendirse: un PDF que tumba el proceso lo " \
        "tumbaría en cada arranque y el servicio no levantaría nunca"
    assert T.leer(datos, "aaaa01")["intentos"] == 1
    assert T.leer(datos, "aaaa03")["estado"] == "listo", \
        "lo ya terminado no se toca"

    # --- los ajustes sobreviven para poder repetir el trabajo -------------
    p = T.parametros(T.leer(datos, "aaaa01"))
    assert p["criterio"] == "vara" and p["lectura"] == "sib-sol", p

    # --- limpieza por antigüedad ------------------------------------------
    viejo = time.time() - 40 * 86400
    crear(datos, "aaaa06", "listo", actualizado=viejo)
    crear(datos, "aaaa07", "error", actualizado=viejo)
    crear(datos, "aaaa08", "en_cola", actualizado=viejo)
    borrados = T.limpiar_viejos(datos, dias=30)
    assert borrados == 2, "había dos terminados viejos y se han borrado %d" % borrados
    assert T.leer(datos, "aaaa08") is not None, \
        "un trabajo sin terminar no se borra por viejo que sea"
    assert T.leer(datos, "aaaa06") is None

    # --- identificadores: solo los que generamos nosotros -----------------
    for malo in ("..", "../otro", "a/b", "", "AAAA", "zz"):
        assert not T.es_id(malo), "%r no debería valer como id" % malo
        assert T.carpeta(datos, malo) is None
    assert T.es_id("aaaa01")

    # --- nombres de archivo ------------------------------------------------
    assert T.sanear("../../etc/passwd") == "passwd"
    assert T.sanear("Marcha: Nuestro Padre.pdf") == "Marcha_ Nuestro Padre.pdf"
    assert T.sanear("") == "partitura.pdf"

    print("OK: la cola se recupera, se rinde a tiempo, se limpia sola y no "
          "se deja engañar por un id raro.")


if __name__ == "__main__":
    main()
