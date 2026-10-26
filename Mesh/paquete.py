"""
paquete.py - Formato del paquete de FaroNet (capas 2 y 3), versión 1.

Todo lo que viaja entre dos nodos vecinos (por UDP o por el Faro) es un
paquete de TAMAÑO FIJO. Hay solo dos tamaños, según el tipo:

    CONTROL = 220 bytes   (2 fragmentos de 110 B del Faro)  HELLO, RUTAS, DIRECTORIO
    DATOS   = 550 bytes   (5 fragmentos de 110 B del Faro)  CELDA, PUBLICACION

Así un observador del medio no puede saber cuánto mide un mensaje: todo
mensaje de chat, corto o largo, ocupa una celda de 550 bytes. Los tamaños
son múltiplos de 110 B (datos de una trama v3 del Faro) para no desperdiciar
tramas, y son pequeños porque el Faro va a unos 25 bytes/s: una celda tarda
unos 22 s en cruzar la luz.

    +---------+------+-----+-------+-----------------+----------+--------+---------+
    | Versión | Tipo | TTL | Flags | Destino (nodo)  | Longitud | Cuerpo | Relleno |
    |   1 B   | 1 B  | 1 B |  1 B  |      8 B        |   2 B    |   N B  | aleatorio
    +---------+------+-----+-------+-----------------+----------+--------+---------+
    |<------------------- cabecera: 14 bytes -------------------->|

    Versión  : 1. Siempre el primer byte; un nodo descarta versiones que no conoce.
    Tipo     : ver TIPO_* abajo.
    TTL      : saltos que le quedan; se resta 1 en cada nodo (evita ciclos).
    Destino  : identificador de 8 bytes del nodo al que va (solo CELDA);
               ceros en los paquetes entre vecinos y en las inundaciones.
               NO existe campo de origen: el paquete no dice quién lo creó.
    Longitud : bytes útiles del cuerpo (campo dinámico); en CELDA y
               PUBLICACION es siempre el máximo, para no filtrar nada.
    Relleno  : bytes aleatorios hasta completar el tamaño fijo.
"""
import os
import struct
from dataclasses import dataclass

VERSION = 1

TIPO_HELLO = 1          # "estoy aquí": descubrimiento de vecinos
TIPO_RUTAS = 2          # vector de distancias (enrutamiento dinámico)
TIPO_DIRECTORIO = 3     # una entrada firmada del directorio de nodos
TIPO_CELDA = 4          # celda cebolla (onion) hacia un nodo
TIPO_PUBLICACION = 5    # publicación de un tema, inundada a toda la mesh

NOMBRES = {TIPO_HELLO: "HELLO", TIPO_RUTAS: "RUTAS", TIPO_DIRECTORIO: "DIRECTORIO",
           TIPO_CELDA: "CELDA", TIPO_PUBLICACION: "PUBLICACION"}

TAM_CONTROL = 220
TAM_DATOS = 550
TAM_CABECERA = 14
MAX_CUERPO_CONTROL = TAM_CONTROL - TAM_CABECERA     # 206
MAX_CUERPO_DATOS = TAM_DATOS - TAM_CABECERA         # 536

TTL_INICIAL = 16
SIN_DESTINO = bytes(8)
TAM_ID = 8

_CABECERA = struct.Struct(">BBBB8sH")


class ErrorPaquete(ValueError):
    pass


def tamano_de(tipo):
    return TAM_DATOS if tipo in (TIPO_CELDA, TIPO_PUBLICACION) else TAM_CONTROL


@dataclass
class Paquete:
    tipo: int
    cuerpo: bytes
    destino: bytes = SIN_DESTINO
    ttl: int = TTL_INICIAL
    flags: int = 0
    version: int = VERSION

    @property
    def nombre(self):
        return NOMBRES.get(self.tipo, f"tipo {self.tipo}")

    def a_bytes(self):
        tam = tamano_de(self.tipo)
        if len(self.destino) != TAM_ID:
            raise ErrorPaquete("el destino debe tener 8 bytes")
        if len(self.cuerpo) > tam - TAM_CABECERA:
            raise ErrorPaquete(f"cuerpo de {len(self.cuerpo)} B no cabe en un paquete {self.nombre}")
        if not 0 <= self.ttl <= 255:
            raise ErrorPaquete("TTL fuera de rango")
        cabecera = _CABECERA.pack(self.version, self.tipo, self.ttl, self.flags,
                                  self.destino, len(self.cuerpo))
        crudo = cabecera + self.cuerpo
        return crudo + os.urandom(tam - len(crudo))     # relleno aleatorio

    @classmethod
    def desde_bytes(cls, crudo):
        if len(crudo) not in (TAM_CONTROL, TAM_DATOS):
            raise ErrorPaquete(f"tamaño inválido: {len(crudo)} B")
        version, tipo, ttl, flags, destino, longitud = _CABECERA.unpack(crudo[:TAM_CABECERA])
        if version != VERSION:
            raise ErrorPaquete(f"versión no soportada: {version}")
        if tipo not in NOMBRES:
            raise ErrorPaquete(f"tipo desconocido: {tipo}")
        if len(crudo) != tamano_de(tipo):
            raise ErrorPaquete(f"un {NOMBRES[tipo]} debe medir {tamano_de(tipo)} B")
        if longitud > len(crudo) - TAM_CABECERA:
            raise ErrorPaquete("campo Longitud inválido")
        return cls(tipo, bytes(crudo[TAM_CABECERA:TAM_CABECERA + longitud]), destino, ttl,
                   flags, version)


# ---- Cuerpos de los paquetes de control --------------------------------------
# HELLO:  versión del cuerpo (1) | id del nodo (8) | intervalo de hello en s (2)
_HELLO = struct.Struct(">B8sH")


def armar_hello(id_nodo, intervalo):
    return Paquete(TIPO_HELLO, _HELLO.pack(1, id_nodo, int(intervalo)), ttl=1)


def leer_hello(cuerpo):
    if len(cuerpo) < _HELLO.size:
        raise ErrorPaquete("HELLO corto")
    _v, id_nodo, intervalo = _HELLO.unpack(cuerpo[:_HELLO.size])
    return id_nodo, intervalo


# RUTAS:  cantidad (1) | [ destino (8) | métrica (1) ] x cantidad
MAX_RUTAS_POR_PAQUETE = (MAX_CUERPO_CONTROL - 1) // 9       # 22


def armar_rutas(lista):
    """lista de (id_destino, metrica). Devuelve uno o más paquetes RUTAS."""
    paquetes = []
    for i in range(0, max(len(lista), 1), MAX_RUTAS_POR_PAQUETE):
        parte = lista[i:i + MAX_RUTAS_POR_PAQUETE]
        cuerpo = bytes([len(parte)]) + b"".join(d + bytes([m]) for d, m in parte)
        paquetes.append(Paquete(TIPO_RUTAS, cuerpo, ttl=1))
    return paquetes


def leer_rutas(cuerpo):
    if not cuerpo:
        raise ErrorPaquete("RUTAS vacío")
    n = cuerpo[0]
    if len(cuerpo) < 1 + 9 * n:
        raise ErrorPaquete("RUTAS incompleto")
    return [(bytes(cuerpo[1 + 9 * i:9 + 9 * i]), cuerpo[9 + 9 * i]) for i in range(n)]
