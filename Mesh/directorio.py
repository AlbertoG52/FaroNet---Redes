"""
directorio.py - Directorio de nodos de FaroNet.

Cada nodo publica su entrada firmada (ver identidad.py) y la mesh la
propaga por inundación: cuando un nodo recibe una entrada válida que no
conocía, o una más nueva del mismo nodo, la guarda y se la pasa a sus
vecinos. Así cada nodo termina con una copia completa del directorio sin
depender de un servidor central. Las entradas vencidas se borran solas.

Solo se aceptan entradas con firma válida y con un id que corresponda a la
llave; esa verificación la hace identidad.Entrada.desde_bytes.
"""
import threading
import time


class Directorio:
    def __init__(self):
        self._entradas = {}             # id del nodo -> su Entrada
        self._lock = threading.Lock()

    def agregar(self, entrada, ahora=None):
        """Guarda la entrada si está vigente y es más nueva que la que ya se
        tenía. Devuelve True si el directorio cambió."""
        if not entrada.vigente(ahora):
            return False
        with self._lock:
            actual = self._entradas.get(entrada.id)
            if actual is not None and actual.creada >= entrada.creada:
                return False
            self._entradas[entrada.id] = entrada
            return True

    def obtener(self, id_nodo):
        with self._lock:
            entrada = self._entradas.get(id_nodo)
        return entrada if entrada is not None and entrada.vigente() else None

    def buscar(self, texto):
        """Busca un nodo por su alias exacto o por el inicio de su id en
        hexadecimal."""
        texto = texto.strip().lower()
        candidatos = [e for e in self.vigentes()
                      if e.alias.lower() == texto or e.id.hex().startswith(texto)]
        if len(candidatos) > 1:
            exactos = [e for e in candidatos if e.alias.lower() == texto or e.id.hex() == texto]
            candidatos = exactos if len(exactos) == 1 else candidatos
        return candidatos

    def vigentes(self):
        with self._lock:
            return [e for e in self._entradas.values() if e.vigente()]

    def expirar(self, ahora=None):
        ahora = time.time() if ahora is None else ahora
        with self._lock:
            for id_nodo in [i for i, e in self._entradas.items() if not e.vigente(ahora)]:
                del self._entradas[id_nodo]

    def borrar_todo(self):
        with self._lock:
            self._entradas.clear()

    def __len__(self):
        return len(self.vigentes())
