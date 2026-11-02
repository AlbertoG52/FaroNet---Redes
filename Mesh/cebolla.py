"""
cebolla.py - Enrutamiento por capas (onion routing) de 3 saltos.

El emisor elige del directorio 3 nodos intermedios (R1, R2 y R3) y envuelve
el mensaje en una capa cifrada para cada uno, con su llave pública X25519.
Cada capa es un SealedBox de libsodium, un cifrado anónimo: ni siquiera quien
la abre sabe quién la cifró.

    emisor ──> R1 ──> R2 ──> R3 ──> destino          (mensaje privado)
    emisor ──> R1 ──> R2 ──> R3 ══> inunda la mesh   (publicación en un tema)

    capa R1 = Sealed(R1, [REENVIAR | id R2 | capa R2])
    capa R2 = Sealed(R2, [REENVIAR | id R3 | capa R3])
    capa R3 = Sealed(R3, [REENVIAR | id destino | capa destino])  o  [DIFUNDIR | 0 | publicación]
    capa destino = Sealed(destino, [APP | mensaje de la aplicación])

Cada nodo quita solo su capa. R1 sabe de quién recibió la celda, pero no a
quién va al final; R3 conoce el destino, pero no quién la originó; R2 no
conoce ninguna de las dos cosas. Así ningún nodo intermedio sabe a la vez el
origen y el destino.

El tamaño es constante: en el cable la celda siempre mide 536 bytes. Al
quitar una capa el contenido se achica 57 bytes (48 del SealedBox y 9 de la
cabecera de la capa), y el nodo vuelve a rellenarlo con bytes aleatorios
hasta 536. Como la celda no dice cuántas capas le quedan, quien la recibe
intenta abrirla con los 4 tamaños posibles (536, 479, 422 y 365); solo uno
pasa la verificación de autenticidad (MAC Poly1305) de libsodium.
"""
import os

import nacl.exceptions
import nacl.public

from paquete import MAX_CUERPO_DATOS, TAM_ID

SALTOS = 3                          # cantidad de nodos intermedios
SOBRECOSTO_SELLADO = 48             # 32 de la llave efímera y 16 de la MAC
CABECERA_CAPA = 1 + TAM_ID          # acción y id del siguiente salto
SOBRECOSTO_CAPA = SOBRECOSTO_SELLADO + CABECERA_CAPA   # 57

REENVIAR = 1
DIFUNDIR = 2
APP = 3

TAM_CELDA = MAX_CUERPO_DATOS                                       # 536


def tamano_capa(k):
    """Tamaño del contenido cifrado después de quitar k capas."""
    return TAM_CELDA - SOBRECOSTO_CAPA * k


TAM_CAPA_FINAL = tamano_capa(SALTOS)                               # 365
TAM_MENSAJE_APP = TAM_CAPA_FINAL - SOBRECOSTO_SELLADO - 1          # 316 (mensaje privado)
TAM_PUBLICACION = TAM_CAPA_FINAL                                    # 365 (publicación)
SIN_ID = bytes(TAM_ID)


class ErrorCebolla(ValueError):
    pass


def _sellar(llave_publica, datos):
    return nacl.public.SealedBox(llave_publica).encrypt(datos)


def armar_privado(ruta, id_destino, llave_destino, mensaje_app):
    """Arma la celda de un mensaje privado. `ruta` es la lista de los 3 nodos
    intermedios como pares (id, llave pública X25519). Devuelve el id de R1 y
    la celda de 536 B."""
    if len(mensaje_app) != TAM_MENSAJE_APP:
        raise ErrorCebolla(f"el mensaje de la aplicación debe medir {TAM_MENSAJE_APP} B")
    interior = _sellar(llave_destino, bytes([APP]) + mensaje_app)
    return _envolver(ruta, REENVIAR, id_destino, interior)


def armar_publicacion(ruta, publicacion):
    """Arma la celda de una publicación. La última capa le pide a R3 que la
    inunde en la mesh en lugar de entregarla a un destino."""
    if len(publicacion) != TAM_PUBLICACION:
        raise ErrorCebolla(f"la publicación debe medir {TAM_PUBLICACION} B")
    return _envolver(ruta, DIFUNDIR, SIN_ID, publicacion)


def _envolver(ruta, accion_final, id_final, interior):
    if len(ruta) != SALTOS:
        raise ErrorCebolla(f"la ruta debe tener {SALTOS} nodos")
    if len({bytes(i) for i, _ in ruta}) != SALTOS:
        raise ErrorCebolla("los nodos de la ruta deben ser distintos")
    accion, siguiente = accion_final, id_final
    for id_nodo, llave in reversed(ruta):
        interior = _sellar(llave, bytes([accion]) + siguiente + interior)
        accion, siguiente = REENVIAR, id_nodo
    assert len(interior) == TAM_CELDA
    return ruta[0][0], interior


def pelar(llave_privada, celda):
    """Quita la capa que le corresponde a este nodo y dice qué hacer con el
    resto. Devuelve una de estas tuplas:
        ("reenviar", id_siguiente, celda_nueva_de_536B)
        ("difundir", publicacion_de_365B)
        ("app", mensaje_de_la_aplicacion_de_316B)
    Si la celda no trae una capa para este nodo, lanza ErrorCebolla.
    """
    if len(celda) != TAM_CELDA:
        raise ErrorCebolla("tamaño de celda inválido")
    caja = nacl.public.SealedBox(llave_privada)
    for k in range(SALTOS + 1):
        tam = tamano_capa(k)
        try:
            claro = caja.decrypt(bytes(celda[:tam]))
        except nacl.exceptions.CryptoError:
            continue
        accion = claro[0]
        if accion == APP and k == SALTOS and len(claro) == 1 + TAM_MENSAJE_APP:
            return ("app", claro[1:])
        if accion == REENVIAR and k < SALTOS:
            siguiente, interior = claro[1:1 + TAM_ID], claro[1 + TAM_ID:]
            if len(interior) != tamano_capa(k + 1):
                break
            return ("reenviar", siguiente, interior + os.urandom(TAM_CELDA - len(interior)))
        if accion == DIFUNDIR and k == SALTOS - 1:
            interior = claro[1 + TAM_ID:]
            if len(interior) != TAM_PUBLICACION:
                break
            return ("difundir", interior)
        break
    raise ErrorCebolla("la celda no trae una capa válida para este nodo")
