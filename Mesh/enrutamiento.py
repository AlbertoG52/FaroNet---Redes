"""
enrutamiento.py - Enrutamiento dinámico por vector de distancias.

Cada nodo les cuenta a sus vecinos, de forma periódica y cada vez que algo
cambia, a qué nodos sabe llegar y con qué costo (paquete RUTAS). Con esa
información cada nodo calcula, para cada destino, el vecino por el que le
conviene salir. Es el algoritmo de Bellman-Ford distribuido, el mismo de RIP.

    Costo de cada enlace: Ethernet 1, WiFi 2 y Faro 10 (configurables).
    La métrica 32 significa que el destino es inalcanzable.

Para evitar ciclos se usan las mismas protecciones de RIP:
    - Horizonte dividido con envenenamiento: a un vecino se le anuncian como
      inalcanzables (32) los destinos a los que el nodo llega a través de él.
    - Las rutas vencen si el vecino deja de anunciarlas.
    - Cada paquete lleva un TTL.
"""
import threading
import time
from dataclasses import dataclass

INFINITO = 32


@dataclass
class Ruta:
    destino: bytes
    metrica: int
    siguiente: bytes            # id del vecino por el que se sale hacia el destino
    vence: float


class TablaRutas:
    def __init__(self, id_propio):
        self.id_propio = id_propio
        self._rutas = {}                # id del destino -> su Ruta
        self._lock = threading.Lock()

    def actualizar_vecino(self, id_vecino, costo, vida):
        """Se oyó a un vecino directo: queda una ruta hacia él con el costo
        del enlace."""
        return self.recibir_anuncio(id_vecino, costo, [(id_vecino, 0)], vida)

    def recibir_anuncio(self, id_vecino, costo, lista, vida):
        """Procesa el vector de distancias que mandó un vecino. Devuelve True
        si la tabla cambió, para que el nodo envíe una actualización disparada."""
        cambio = False
        ahora = time.time()
        with self._lock:
            for destino, metrica in lista:
                if destino == self.id_propio:
                    continue
                nueva = min(INFINITO, metrica + costo)
                actual = self._rutas.get(destino)
                if actual is None:
                    if nueva < INFINITO:
                        self._rutas[destino] = Ruta(destino, nueva, id_vecino, ahora + vida)
                        cambio = True
                elif actual.siguiente == id_vecino:
                    # si la ruta ya salía por este vecino, se acepta lo que diga,
                    # aunque la métrica empeore
                    if nueva >= INFINITO:
                        del self._rutas[destino]
                        cambio = True
                    else:
                        cambio |= actual.metrica != nueva
                        actual.metrica, actual.vence = nueva, ahora + vida
                elif nueva < actual.metrica:
                    self._rutas[destino] = Ruta(destino, nueva, id_vecino, ahora + vida)
                    cambio = True
        return cambio

    def quitar_vecino(self, id_vecino):
        """Se perdió un vecino: se borran todas las rutas que salían por él."""
        with self._lock:
            borrar = [d for d, r in self._rutas.items() if r.siguiente == id_vecino]
            for d in borrar:
                del self._rutas[d]
        return bool(borrar)

    def expirar(self):
        ahora = time.time()
        with self._lock:
            borrar = [d for d, r in self._rutas.items() if r.vence < ahora]
            for d in borrar:
                del self._rutas[d]
        return bool(borrar)

    def siguiente_salto(self, destino):
        with self._lock:
            ruta = self._rutas.get(destino)
            return ruta.siguiente if ruta else None

    def alcanzables(self):
        with self._lock:
            return set(self._rutas)

    def vector_para(self, id_vecino):
        """Arma el vector que se le anuncia a un vecino, aplicando el horizonte
        dividido con envenenamiento."""
        with self._lock:
            lista = [(self.id_propio, 0)]
            for d, r in self._rutas.items():
                lista.append((d, INFINITO if r.siguiente == id_vecino else r.metrica))
        return lista

    def listar(self):
        with self._lock:
            return sorted(self._rutas.values(), key=lambda r: (r.metrica, r.destino))

    def borrar_todo(self):
        with self._lock:
            self._rutas.clear()
