#!/usr/bin/env python3
"""
verificar_directorio.py - Comprueba, por su cuenta, la réplica pública del
directorio de nodos que publica el Worker.

Para cada entrada verifica con PyNaCl:
    - la firma Ed25519 sobre la forma binaria de la entrada;
    - que el id sea BLAKE2b(llave pública)[:8] (nadie puede usar el id de otro);
    - que los campos que muestra el JSON coincidan con la forma binaria.
Así no hay que confiar en el Worker ni en Cloudflare.

    python3 Gateway/verificar_directorio.py --worker https://faronet.xxx.workers.dev
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Mesh"))
try:
    from identidad import Entrada, ErrorEntrada      # noqa: E402
except ImportError:
    sys.exit("Falta PyNaCl: pip install pynacl  (Raspberry Pi: sudo apt install python3-nacl)")


def verificar(datos):
    buenas, malas = 0, 0
    for nodo in datos.get("nodos", []):
        try:
            e = Entrada.desde_bytes(bytes.fromhex(nodo["entrada"]))
            if (e.id.hex(), e.alias, e.expira) != (nodo["id"], nodo["alias"], nodo["expira"]):
                raise ErrorEntrada("el JSON no coincide con la entrada firmada")
            print(f"  OK     {e.id.hex()}  {e.alias:16} {'[gateway]' if e.es_gateway else ''}")
            buenas += 1
        except (ErrorEntrada, ValueError, KeyError) as x:
            print(f"  FALLA  {nodo.get('id', '?')}  {x}")
            malas += 1
    return buenas, malas


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--worker", required=True)
    a = p.parse_args()
    req = urllib.request.Request(a.worker.rstrip("/") + "/directorio",
                                 headers={"User-Agent": "FaroNet/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        datos = json.load(r)
    buenas, malas = verificar(datos)
    print(f"\n{buenas} entradas verificadas, {malas} con problemas.")
    sys.exit(1 if malas else 0)


if __name__ == "__main__":
    main()
