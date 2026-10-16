"""
Cancelar mata de verdad lo que esté corriendo.

Lo fácil de equivocar aquí es creer que cancelar es "no empieces el
siguiente paso". El paso que está corriendo es Audiveris y puede durar
media hora: si no se le mata, el usuario pulsa Cancelar y no pasa nada
durante veinte minutos.

Ejecutar: python pruebas/test_cancelar.py
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cancelacion import Cancelado, Registro

PYTHON = sys.executable


def dormilon(segundos: int = 60) -> subprocess.Popen:
    """Un proceso que no se va a ir por su cuenta."""
    return subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(%d)" % segundos],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def esperar_muerte(proceso: subprocess.Popen, limite: float = 10.0) -> float:
    inicio = time.time()
    while time.time() - inicio < limite:
        if proceso.poll() is not None:
            return time.time() - inicio
        time.sleep(0.05)
    return -1.0


def main() -> None:
    # --- en cola: basta con la marca --------------------------------------
    r = Registro()
    assert r.cancelar("aaaa01") is False, \
        "sin proceso corriendo no hay nada que matar"
    assert r.pedido("aaaa01")
    try:
        r.comprobar("aaaa01")
        raise AssertionError("comprobar() tenía que cortar el trabajo")
    except Cancelado:
        pass

    # --- corriendo: se mata ------------------------------------------------
    r = Registro()
    p = dormilon()
    r.vigilar("aaaa02", p)
    assert p.poll() is None, "el proceso de prueba se ha muerto solo"
    assert r.cancelar("aaaa02") is True, "había un proceso al que matar"
    tardanza = esperar_muerte(p)
    assert tardanza >= 0, "el proceso sigue vivo diez segundos después"
    print("  muerto en %.2f s" % tardanza)

    # --- el que no se va por las buenas ------------------------------------
    # Se ignora la señal amable: tiene que acabar matándolo igual.
    r = Registro(gracia=1.0)
    sordo = subprocess.Popen(
        [PYTHON, "-c",
         "import signal, time\n"
         "try:\n"
         "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "except Exception:\n"
         "    pass\n"
         "time.sleep(60)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r.vigilar("aaaa03", sordo)
    r.cancelar("aaaa03")
    tardanza = esperar_muerte(sordo, limite=15.0)
    assert tardanza >= 0, \
        "un proceso que ignora la señal amable tiene que morir igual"
    print("  el sordo, muerto en %.2f s" % tardanza)

    # --- carrera: cancelar justo antes de arrancar el proceso -------------
    # Es el hueco fácil de dejar: se comprueba la cancelación, se pide
    # cancelar, y después arranca el proceso. Sin cuidado se queda
    # corriendo huérfano hasta el final.
    r = Registro()
    r.cancelar("aaaa04")                 # se pide ANTES de que exista
    tarde = dormilon()
    r.vigilar("aaaa04", tarde)           # y el proceso nace después
    tardanza = esperar_muerte(tarde)
    assert tardanza >= 0, \
        "un proceso que arranca después de pedir la cancelación se queda vivo"
    print("  el que llegó tarde, muerto en %.2f s" % tardanza)

    # --- olvidar deja el trabajo limpio para reintentarlo -----------------
    r = Registro()
    r.cancelar("aaaa05")
    assert r.pedido("aaaa05")
    r.olvidar("aaaa05")
    assert not r.pedido("aaaa05"), \
        "tras olvidarlo se tiene que poder volver a lanzar"
    r.comprobar("aaaa05")                # ya no corta

    # --- varios trabajos a la vez no se pisan ------------------------------
    r = Registro()
    uno, dos = dormilon(), dormilon()
    r.vigilar("aaaa06", uno)
    r.vigilar("aaaa07", dos)
    r.cancelar("aaaa06")
    assert esperar_muerte(uno) >= 0, "no ha matado al que tocaba"
    assert dos.poll() is None, \
        "cancelar uno no puede llevarse por delante al otro"
    dos.kill()

    print("OK: cancelar mata lo que esté corriendo, incluido el que ignora "
          "la señal y el que arranca tarde, y no toca a los demás.")


if __name__ == "__main__":
    main()
