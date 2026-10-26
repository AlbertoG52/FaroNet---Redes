"""
identidad.py - Llaves del nodo y entradas firmadas del directorio de nodos.

Cada nodo tiene UNA semilla secreta de 32 bytes, de la que salen:
    - una llave Ed25519 para FIRMAR (su entrada del directorio, sus mensajes);
    - una llave X25519 para CIFRAR (cebolla y mensajes privados), derivada de
      la Ed25519 con la conversión estándar de libsodium.

El identificador del nodo (8 bytes) es BLAKE2b(llave pública Ed25519)[:8],
así que nadie puede publicar una entrada con el identificador de otro: el
identificador queda atado a la llave que firma.

Entrada del directorio (versión 1):

    +-----+--------+-------------+--------+--------+-------+-----------+-------+--------+
    | Ver | Id (8) | Ed25519 (32)| Creada | Expira | Flags | Long.alias| Alias | Firma  |
    | 1 B |        |             |  4 B   |  4 B   |  1 B  |    1 B    | 0..16 | 64 B   |
    +-----+--------+-------------+--------+--------+-------+-----------+-------+--------+
    La firma Ed25519 cubre todo lo anterior. Creada/Expira: segundos Unix.
    Flags: bit 0 = el nodo es gateway hacia el edge.

Criptografía: solo PyNaCl (libsodium). Instalar en la Raspberry Pi con
    sudo apt install python3-nacl        (en Windows/Mac: pip install pynacl)
"""
import os
import struct
import time
from dataclasses import dataclass

import nacl.encoding
import nacl.exceptions
import nacl.hash
import nacl.signing
import nacl.utils

VERSION_ENTRADA = 1
TAM_ID = 8
MAX_ALIAS = 16
FLAG_GATEWAY = 0x01
VIDA_ENTRADA_S = 30 * 60          # una entrada vale 30 minutos; se republica antes

_ENTRADA = struct.Struct(">B8s32sIIBB")


class ErrorEntrada(ValueError):
    pass


def id_de_llave(llave_verificacion: bytes) -> bytes:
    return nacl.hash.blake2b(bytes(llave_verificacion), digest_size=TAM_ID,
                             encoder=nacl.encoding.RawEncoder)


def id_a_texto(id_nodo: bytes) -> str:
    return id_nodo.hex()


class Identidad:
    """Llaves de este nodo. Con `archivo` se guardan (permisos 600) para que
    el nodo conserve su identidad entre reinicios; sin archivo son efímeras."""

    def __init__(self, archivo=None):
        self.archivo = archivo
        semilla = None
        if archivo and os.path.exists(archivo):
            with open(archivo, "rb") as f:
                semilla = f.read()
            if len(semilla) != 32:
                semilla = None
        if semilla is None:
            semilla = nacl.utils.random(32)
            if archivo:
                self._guardar(semilla)
        self._cargar(semilla)

    def _cargar(self, semilla):
        self.firma = nacl.signing.SigningKey(semilla)
        self.verificacion = self.firma.verify_key
        self.privada = self.firma.to_curve25519_private_key()      # X25519
        self.publica = self.privada.public_key
        self.id = id_de_llave(bytes(self.verificacion))

    def _guardar(self, semilla):
        carpeta = os.path.dirname(self.archivo)
        if carpeta:
            os.makedirs(carpeta, mode=0o700, exist_ok=True)
        temporal = self.archivo + ".nuevo"
        fd = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(semilla)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, self.archivo)

    def borrar(self):
        """Pánico: sobrescribe el archivo de llaves y suelta las referencias.
        (Python no permite garantizar que no queden copias en memoria.)"""
        if self.archivo and os.path.exists(self.archivo):
            try:
                with open(self.archivo, "r+b") as f:
                    f.write(os.urandom(64))
                    f.flush()
                    os.fsync(f.fileno())
                os.remove(self.archivo)
            except OSError:
                pass
        self.firma = self.verificacion = self.privada = self.publica = None

    def firmar(self, datos: bytes) -> bytes:
        return self.firma.sign(datos).signature

    def crear_entrada(self, alias="", gateway=False, vida=VIDA_ENTRADA_S):
        ahora = int(time.time())
        return Entrada(self.id, bytes(self.verificacion), ahora, ahora + vida,
                       FLAG_GATEWAY if gateway else 0, alias).firmada_por(self)


@dataclass
class Entrada:
    id: bytes
    llave_firma: bytes          # Ed25519 pública (32 B)
    creada: int
    expira: int
    flags: int = 0
    alias: str = ""
    firma: bytes = b""

    def _contenido(self):
        alias = self.alias.encode("utf-8")[:MAX_ALIAS]
        return _ENTRADA.pack(VERSION_ENTRADA, self.id, self.llave_firma, self.creada,
                             self.expira, self.flags, len(alias)) + alias

    def firmada_por(self, identidad):
        self.firma = identidad.firmar(self._contenido())
        return self

    def a_bytes(self):
        return self._contenido() + self.firma

    @property
    def es_gateway(self):
        return bool(self.flags & FLAG_GATEWAY)

    @property
    def llave_cifrado(self):
        """Llave pública X25519 del nodo, derivada de su llave de firma."""
        return nacl.signing.VerifyKey(self.llave_firma).to_curve25519_public_key()

    def vigente(self, ahora=None):
        ahora = time.time() if ahora is None else ahora
        return self.creada - 300 <= ahora < self.expira      # 5 min de tolerancia de reloj

    @classmethod
    def desde_bytes(cls, crudo):
        """Lee y VERIFICA una entrada: firma válida e id que corresponde a la llave."""
        if len(crudo) < _ENTRADA.size + 64:
            raise ErrorEntrada("entrada corta")
        version, id_nodo, llave, creada, expira, flags, largo = _ENTRADA.unpack(crudo[:_ENTRADA.size])
        if version != VERSION_ENTRADA:
            raise ErrorEntrada(f"versión de entrada no soportada: {version}")
        if largo > MAX_ALIAS or len(crudo) < _ENTRADA.size + largo + 64:
            raise ErrorEntrada("alias inválido")
        fin = _ENTRADA.size + largo
        try:
            alias = crudo[_ENTRADA.size:fin].decode("utf-8")
        except UnicodeDecodeError:
            raise ErrorEntrada("alias no es UTF-8") from None
        firma = bytes(crudo[fin:fin + 64])
        if id_de_llave(llave) != id_nodo:
            raise ErrorEntrada("el id no corresponde a la llave")
        try:
            nacl.signing.VerifyKey(llave).verify(bytes(crudo[:fin]), firma)
        except nacl.exceptions.BadSignatureError:
            raise ErrorEntrada("firma inválida") from None
        if expira <= creada:
            raise ErrorEntrada("fechas inválidas")
        return cls(id_nodo, bytes(llave), creada, expira, flags, alias, firma)

    def a_dict(self):
        return {"id": self.id.hex(), "alias": self.alias, "llave_firma": self.llave_firma.hex(),
                "creada": self.creada, "expira": self.expira, "gateway": self.es_gateway,
                "firma": self.firma.hex(), "entrada": self.a_bytes().hex()}
