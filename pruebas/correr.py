"""
Pasa todas las pruebas que no necesitan Audiveris ni PDFs de verdad.

    python pruebas/correr.py                      # contra la base guardada
    python pruebas/correr.py --actualizar-base    # tras una mejora medida

Lo segundo solo se hace a conciencia: la base es el contrato de que el
estampado no ha empeorado. Si la actualizas sin mirar, deja de servir.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent / "base.json"


def paso(titulo: str, cmd: list) -> bool:
    print("\n=== %s ===" % titulo)
    r = subprocess.run([sys.executable] + cmd, cwd=str(RAIZ))
    return r.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--actualizar-base", action="store_true")
    args = ap.parse_args()

    fallos = []

    if not paso("veto de falsos positivos en _fantasmas",
                ["pruebas/test_fantasmas.py"]):
        fallos.append("test_fantasmas")

    if not paso("confianza: nada se descarta, lo dudoso se marca",
                ["pruebas/test_confianza.py"]):
        fallos.append("test_confianza")

    if not paso("la cola sobrevive a un reinicio",
                ["pruebas/test_trabajos.py"]):
        fallos.append("test_trabajos")

    if not paso("enderezar escaneos torcidos",
                ["pruebas/test_normalizar.py"]):
        fallos.append("test_normalizar")

    if not paso("repaso a mano: corregir no mueve lo demás",
                ["pruebas/test_revision.py"]):
        fallos.append("test_revision")

    if args.actualizar_base:
        if not paso("midiendo y guardando la nueva base",
                    ["metricas.py", "--sinteticos", "--json", str(BASE)]):
            fallos.append("metricas")
        else:
            print("\nbase actualizada en %s — revísala antes de commitear" % BASE)
    else:
        cmd = ["metricas.py", "--sinteticos"]
        if BASE.exists():
            cmd += ["--base", str(BASE)]
        else:
            print("(no hay %s todavía: solo se mide, no se compara)" % BASE.name)
        if not paso("batería sintética", cmd):
            fallos.append("metricas")

    print()
    if fallos:
        print("FALLAN: %s" % ", ".join(fallos))
        raise SystemExit(1)
    print("todo en orden")


if __name__ == "__main__":
    main()
