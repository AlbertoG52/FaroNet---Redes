#!/usr/bin/env python3
"""
buzon_muerto.py - Cliente del buzón muerto (dead drop) del Worker de FaroNet.

Dos personas acuerdan EN PERSONA una frase secreta. Con ella:
    - se deriva el identificador del buzón (64 hex), opaco para el Worker;
    - se deriva la clave con la que se cifra el mensaje (SecretBox de PyNaCl).
El blob siempre mide 1024 bytes (el mensaje se rellena) y se borra al leerlo.

    python3 Gateway/buzon_muerto.py --worker https://faronet.xxx.workers.dev \\
        depositar --frase "frase larga acordada en persona" --mensaje "nos vemos el martes"
    python3 Gateway/buzon_muerto.py --worker https://faronet.xxx.workers.dev \\
        recoger --frase "frase larga acordada en persona"

El Worker nunca ve el mensaje en claro, ni la frase, ni quién depositó.
"""
import argparse
import struct
import sys
import urllib.error
import urllib.request

try:
    import nacl.encoding
    import nacl.exceptions
    import nacl.hash
    import nacl.pwhash
    import nacl.secret
except ImportError:
    sys.exit("Falta PyNaCl: pip install pynacl  (Raspberry Pi: sudo apt install python3-nacl)")

TAM_BLOB = 1024
SOBRECOSTO = 24 + 16                       # nonce + MAC del SecretBox
MAX_MENSAJE = TAM_BLOB - SOBRECOSTO - 2    # 982 bytes


def derivar(frase):
    """Una sola derivación Argon2id de 64 bytes: 32 para el id, 32 para la clave."""
    sal = nacl.hash.blake2b(b"faronet-buzon-muerto", digest_size=16, encoder=nacl.encoding.RawEncoder)
    material = nacl.pwhash.argon2id.kdf(64, frase.encode("utf-8"), sal,
                                        opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
                                        memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE)
    return material[:32].hex(), material[32:]


def armar_blob(clave, mensaje):
    datos = mensaje.encode("utf-8")
    if len(datos) > MAX_MENSAJE:
        raise ValueError(f"mensaje demasiado largo ({len(datos)} > {MAX_MENSAJE} bytes)")
    claro = struct.pack(">H", len(datos)) + datos
    claro += bytes(TAM_BLOB - SOBRECOSTO - len(claro))           # relleno a tamaño fijo
    blob = bytes(nacl.secret.SecretBox(clave).encrypt(claro))
    assert len(blob) == TAM_BLOB
    return blob


def leer_blob(clave, blob):
    claro = nacl.secret.SecretBox(clave).decrypt(blob)
    (largo,) = struct.unpack(">H", claro[:2])
    return claro[2:2 + largo].decode("utf-8")


def pedir(metodo, url, cuerpo=None, ttl=None):
    cab = {"Content-Type": "application/octet-stream", "User-Agent": "FaroNet/1"}
    if ttl:
        cab["X-TTL"] = str(ttl)
    req = urllib.request.Request(url, data=cuerpo, method=metodo, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    p = argparse.ArgumentParser(description="Buzón muerto de FaroNet",
                                epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--worker", required=True, help="URL del Worker (https://...)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("depositar")
    s.add_argument("--frase", required=True)
    s.add_argument("--mensaje", required=True)
    s.add_argument("--ttl", type=int, default=3600, help="segundos (60 a 86400)")
    s = sub.add_parser("recoger")
    s.add_argument("--frase", required=True)
    a = p.parse_args()

    id_buzon, clave = derivar(a.frase)
    url = f"{a.worker.rstrip('/')}/buzon/{id_buzon}"
    try:
        if a.cmd == "depositar":
            estado, r = pedir("PUT", url, armar_blob(clave, a.mensaje), a.ttl)
            if estado == 201:
                print("Depositado. Solo se puede recoger una vez, antes de que venza.")
            elif estado == 409:
                sys.exit("Ese buzón ya tiene un mensaje sin recoger (use otra frase).")
            else:
                sys.exit(f"Error {estado}: {r.decode(errors='replace')}")
        else:
            estado, r = pedir("GET", url)
            if estado == 404:
                sys.exit("No hay nada en ese buzón (o ya se recogió, o venció).")
            if estado != 200:
                sys.exit(f"Error {estado}: {r.decode(errors='replace')}")
            print(leer_blob(clave, r))
    except ValueError as e:
        sys.exit(str(e))
    except nacl.exceptions.CryptoError:
        sys.exit("El blob no se pudo descifrar con esa frase.")
    except urllib.error.URLError as e:
        sys.exit(f"No se pudo conectar con el Worker: {e.reason}")


if __name__ == "__main__":
    main()
