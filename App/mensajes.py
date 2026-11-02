"""
mensajes.py - Protocolo de la aplicación anónima de FaroNet (Capa 7), versión 1.

La aplicación usa dos clases de mensaje.

1) Mensaje privado. Viaja dentro de la última capa de la cebolla y siempre
   mide 316 bytes:

    +-----+------+-------+-----------+----------+-----------+---------+
    | Ver | Tipo | Flags | Remitente | Long.    |  Cuerpo   | Relleno |
    | 1 B | 1 B  |  1 B  |   8 B     | cuerpo 2B|           |         |
    +-----+------+-------+-----------+----------+-----------+---------+
    El bit 0 de Flags indica que el mensaje es identificado. En ese caso,
    Remitente es el id de quien lo envía y el cuerpo va además dentro de un
    Box de libsodium, cifrado de la llave X25519 del remitente a la del
    destinatario; así el destinatario comprueba quién lo escribió. Si el bit
    no está, Remitente va en ceros y el mensaje es completamente anónimo.

    El cuerpo contiene:
    | Id del mensaje 8 B | Enviado (s Unix) 4 B | TTL (s) 4 B | Long. texto 2 B | Texto UTF-8 |

2) Publicación en un tema. Se inunda en toda la mesh y siempre mide 365 bytes:

    +-----+------+-------------+--------+--------+-------+-----------+------+-----------+--------+---------+
    | Ver | Tipo | Visibilidad | Id msg | Creada | TTL   | Long.tema | Tema | Long.cuerp| Cuerpo | Relleno |
    | 1 B | 1 B  |    1 B      |  8 B   |  4 B   |  4 B  |   1 B     | 0-32 |    2 B    |        |         |
    +-----+------+-------------+--------+--------+-------+-----------+------+-----------+--------+---------+
    Con Visibilidad 0 la publicación es pública: el texto va en claro y el
        gateway la publica en Internet (Cloudflare Workers).
    Con Visibilidad 1 es privada: el campo Tema lleva una etiqueta derivada
        de la clave del tema en lugar de su nombre, y el cuerpo va en un
        SecretBox con esa clave. Solo quienes conocen la clave pueden leerla,
        y nunca sale de FaroNet.
    No hay campo de autor, así que las publicaciones son anónimas.

El TTL indica cuánto tiempo se puede guardar cada mensaje, siempre en memoria
y nunca en disco.
"""
import os
import struct
import time
from dataclasses import dataclass

import nacl.encoding
import nacl.exceptions
import nacl.hash
import nacl.public
import nacl.pwhash
import nacl.secret

VERSION = 1
TIPO_PRIVADO = 1
TIPO_PUBLICACION = 2
FLAG_IDENTIFICADO = 0x01
PUBLICA, PRIVADA = 0, 1

TAM_PRIVADO = 316           # igual a cebolla.TAM_MENSAJE_APP
TAM_PUBLICACION = 365       # igual a cebolla.TAM_PUBLICACION
MAX_TEMA = 32
TTL_POR_DEFECTO = 3600      # 1 hora
TTL_MAXIMO = 24 * 3600
SOBRECOSTO_CAJA = 40        # 24 del nonce y 16 de la MAC

_PRIV = struct.Struct(">BBB8sH")
_CONTENIDO = struct.Struct(">8sIIH")
_PUB = struct.Struct(">BBB8sII")

MAX_TEXTO_PRIVADO_ANONIMO = TAM_PRIVADO - _PRIV.size - _CONTENIDO.size                     # 285
MAX_TEXTO_PRIVADO = MAX_TEXTO_PRIVADO_ANONIMO - SOBRECOSTO_CAJA                            # 245


def max_texto_publicacion(tema, privada):
    largo_tema = 16 if privada else len(tema.encode("utf-8"))
    return TAM_PUBLICACION - _PUB.size - 1 - largo_tema - 2 - (SOBRECOSTO_CAJA if privada else 0)


class ErrorMensaje(ValueError):
    pass


def _rellenar(datos, tam):
    if len(datos) > tam:
        raise ErrorMensaje(f"el mensaje no cabe ({len(datos)} > {tam} B)")
    return datos + os.urandom(tam - len(datos))


def _texto(texto, maximo):
    datos = texto.encode("utf-8")
    if len(datos) > maximo:
        raise ErrorMensaje(f"texto demasiado largo: {len(datos)} bytes (máximo {maximo}; "
                           "las tildes y la ñ ocupan 2)")
    return datos


# ---------------------------------------------------------------- privados
@dataclass
class Privado:
    texto: str
    id_msg: bytes
    enviado: int
    ttl: int
    remitente: bytes = None         # None si el mensaje es anónimo
    verificado: bool = False        # True si el Box confirmó quién lo envió


def armar_privado(texto, ttl=TTL_POR_DEFECTO, identidad=None, llave_destino=None):
    """Arma los 316 bytes de un mensaje privado. Si se pasan `identidad` y la
    llave del destinatario, el mensaje va identificado y autenticado; si no,
    es anónimo."""
    ttl = max(1, min(int(ttl), TTL_MAXIMO))
    maximo = MAX_TEXTO_PRIVADO if identidad else MAX_TEXTO_PRIVADO_ANONIMO
    datos = _texto(texto, maximo)
    contenido = _CONTENIDO.pack(os.urandom(8), int(time.time()), ttl, len(datos)) + datos
    if identidad is not None:
        cuerpo = bytes(nacl.public.Box(identidad.privada, llave_destino).encrypt(contenido))
        cabecera = _PRIV.pack(VERSION, TIPO_PRIVADO, FLAG_IDENTIFICADO, identidad.id, len(cuerpo))
    else:
        cuerpo = contenido
        cabecera = _PRIV.pack(VERSION, TIPO_PRIVADO, 0, bytes(8), len(cuerpo))
    return _rellenar(cabecera + cuerpo, TAM_PRIVADO)


def leer_privado(datos, identidad, buscar_llave):
    """Lee un mensaje privado. `buscar_llave(id)` debe devolver la llave X25519
    del remitente según el directorio, o None si no lo conoce."""
    if len(datos) != TAM_PRIVADO:
        raise ErrorMensaje("tamaño de mensaje privado inválido")
    version, tipo, flags, remitente, largo = _PRIV.unpack(datos[:_PRIV.size])
    if version != VERSION or tipo != TIPO_PRIVADO:
        raise ErrorMensaje("no es un mensaje privado v1")
    cuerpo = datos[_PRIV.size:_PRIV.size + largo]
    if len(cuerpo) != largo:
        raise ErrorMensaje("longitud inválida")
    verificado = False
    if flags & FLAG_IDENTIFICADO:
        llave = buscar_llave(remitente)
        if llave is None:
            raise ErrorMensaje("remitente desconocido: no está en el directorio")
        try:
            cuerpo = nacl.public.Box(identidad.privada, llave).decrypt(bytes(cuerpo))
        except nacl.exceptions.CryptoError:
            raise ErrorMensaje("no se pudo autenticar al remitente") from None
        verificado = True
    else:
        remitente = None
    if len(cuerpo) < _CONTENIDO.size:
        raise ErrorMensaje("contenido corto")
    id_msg, enviado, ttl, largo_texto = _CONTENIDO.unpack(cuerpo[:_CONTENIDO.size])
    texto = cuerpo[_CONTENIDO.size:_CONTENIDO.size + largo_texto]
    if len(texto) != largo_texto:
        raise ErrorMensaje("texto incompleto")
    return Privado(texto.decode("utf-8", errors="replace"), id_msg, enviado,
                   min(ttl, TTL_MAXIMO), remitente, verificado)


# ---------------------------------------------------------------- temas
def clave_de_tema(nombre, frase):
    """Deriva con Argon2id la clave de 32 B de un tema privado, a partir de una
    frase que sus miembros acordaron fuera de la red."""
    sal = nacl.hash.blake2b(b"faronet-tema:" + nombre.encode("utf-8"), digest_size=16,
                            encoder=nacl.encoding.RawEncoder)
    return nacl.pwhash.argon2id.kdf(32, frase.encode("utf-8"), sal,
                                    opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
                                    memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE)


def etiqueta_de_clave(clave):
    """Calcula la etiqueta de 16 caracteres que viaja en el campo Tema de una
    publicación privada, en lugar del nombre del tema."""
    return nacl.hash.blake2b(b"etiqueta" + clave, digest_size=8,
                             encoder=nacl.encoding.RawEncoder).hex()


def _validar_tema(tema):
    tema = tema.strip().lower()
    if not tema or len(tema.encode("utf-8")) > MAX_TEMA:
        raise ErrorMensaje(f"el tema debe tener entre 1 y {MAX_TEMA} bytes")
    return tema


@dataclass
class Publicacion:
    tema: str               # el nombre si es pública, o la etiqueta si es privada
    visibilidad: int
    id_msg: bytes
    creada: int
    ttl: int
    cuerpo: bytes           # el texto en claro si es pública, o cifrado si es privada
    crudo: bytes = b""

    @property
    def vence(self):
        return self.creada + self.ttl

    def texto(self, clave=None):
        if self.visibilidad == PUBLICA:
            return self.cuerpo.decode("utf-8", errors="replace")
        if clave is None:
            raise ErrorMensaje("tema privado: hace falta la clave")
        try:
            return nacl.secret.SecretBox(clave).decrypt(self.cuerpo).decode("utf-8", errors="replace")
        except nacl.exceptions.CryptoError:
            raise ErrorMensaje("clave incorrecta para este tema") from None


def armar_publicacion(tema, texto, clave=None, ttl=TTL_POR_DEFECTO):
    """Arma una publicación: pública si clave=None, o privada si se pasa la
    clave del tema."""
    tema = _validar_tema(tema)
    ttl = max(1, min(int(ttl), TTL_MAXIMO))
    privada = clave is not None
    datos = _texto(texto, max_texto_publicacion(tema, privada))
    if privada:
        campo_tema = etiqueta_de_clave(clave).encode()
        cuerpo = bytes(nacl.secret.SecretBox(clave).encrypt(datos))
    else:
        campo_tema = tema.encode("utf-8")
        cuerpo = datos
    crudo = (_PUB.pack(VERSION, TIPO_PUBLICACION, PRIVADA if privada else PUBLICA,
                       os.urandom(8), int(time.time()), ttl)
             + bytes([len(campo_tema)]) + campo_tema + struct.pack(">H", len(cuerpo)) + cuerpo)
    return _rellenar(crudo, TAM_PUBLICACION)


def leer_publicacion(datos):
    if len(datos) != TAM_PUBLICACION:
        raise ErrorMensaje("tamaño de publicación inválido")
    version, tipo, visibilidad, id_msg, creada, ttl = _PUB.unpack(datos[:_PUB.size])
    if version != VERSION or tipo != TIPO_PUBLICACION or visibilidad not in (PUBLICA, PRIVADA):
        raise ErrorMensaje("no es una publicación v1")
    i = _PUB.size
    largo_tema = datos[i]
    if largo_tema > MAX_TEMA:
        raise ErrorMensaje("tema inválido")
    tema = datos[i + 1:i + 1 + largo_tema].decode("utf-8", errors="replace")
    i += 1 + largo_tema
    (largo,) = struct.unpack(">H", datos[i:i + 2])
    cuerpo = bytes(datos[i + 2:i + 2 + largo])
    if len(cuerpo) != largo:
        raise ErrorMensaje("cuerpo incompleto")
    return Publicacion(tema, visibilidad, bytes(id_msg), creada, min(ttl, TTL_MAXIMO), cuerpo,
                       bytes(datos))
