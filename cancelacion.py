"""
Parar un trabajo a mitad.

Cancelar no puede ser solo "no empieces el siguiente paso". El paso que
está corriendo suele ser Audiveris y puede durar media hora: esperar a que
termine para dar el trabajo por cancelado no es cancelar nada. Hay que
matar el proceso.

Vive aparte de `api.py` por lo de siempre: así se puede probar sin
levantar FastAPI ni cargar music21, y lo que hay que probar aquí es justo
lo delicado —que un proceso que no se entera por las buenas se muere por
las malas, y que nadie se queda esperando para siempre—.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Dict, Optional, Set

# Cuánto se espera a que un proceso se vaya por las buenas antes de
# rematarlo. La JVM de Audiveris a veces ignora el primer aviso.
GRACIA = 5.0


class Cancelado(Exception):
    """El usuario ha pedido parar este trabajo."""


class Registro:
    """Qué trabajos se han mandado parar y qué proceso tiene cada uno."""

    def __init__(self, gracia: float = GRACIA):
        self._gracia = gracia
        self._lock = threading.Lock()
        self._pedidos: Set[str] = set()
        self._procesos: Dict[str, subprocess.Popen] = {}

    # -- desde el trabajador ------------------------------------------------

    def vigilar(self, tid: Optional[str], proceso: subprocess.Popen) -> None:
        """Anota el proceso que está corriendo ahora mismo este trabajo."""
        if not tid:
            return
        with self._lock:
            # Puede haberse pedido la cancelación entre que se comprobó y
            # que arrancó el proceso: entonces hay que matarlo ya, o se
            # queda corriendo huérfano media hora.
            pedido = tid in self._pedidos
            self._procesos[tid] = proceso
        if pedido:
            self._matar(proceso)

    def soltar(self, tid: Optional[str], proceso: subprocess.Popen) -> None:
        if not tid:
            return
        with self._lock:
            if self._procesos.get(tid) is proceso:
                del self._procesos[tid]

    def pedido(self, tid: Optional[str]) -> bool:
        if not tid:
            return False
        with self._lock:
            return tid in self._pedidos

    def comprobar(self, tid: Optional[str]) -> None:
        """Corta el trabajo si se ha pedido pararlo."""
        if self.pedido(tid):
            raise Cancelado()

    def olvidar(self, tid: str) -> None:
        with self._lock:
            self._pedidos.discard(tid)
            self._procesos.pop(tid, None)

    # -- desde la API -------------------------------------------------------

    def cancelar(self, tid: str) -> bool:
        """
        Marca el trabajo y mata lo que tenga corriendo.

        Devuelve si había un proceso al que matar: en cola no lo hay, y
        basta con la marca.
        """
        with self._lock:
            self._pedidos.add(tid)
            proceso = self._procesos.get(tid)
        if proceso is None:
            return False
        return self._matar(proceso)

    def _matar(self, proceso: subprocess.Popen) -> bool:
        if proceso.poll() is not None:
            return False
        try:
            proceso.terminate()
        except Exception:  # noqa: BLE001
            return False

        def rematar() -> None:
            try:
                proceso.wait(timeout=self._gracia)
            except Exception:  # noqa: BLE001
                try:
                    proceso.kill()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=rematar, daemon=True).start()
        return True
