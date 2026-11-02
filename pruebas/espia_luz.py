#!/usr/bin/env python3
"""
espia_luz.py - Prueba de minimización de metadatos. Hace de observador del
medio: graba todo lo que viaja por la luz simulada y busca en esos bytes los
identificadores de los nodos de FaroNet.

    # terminal 1: la red, primero sin cifrar el enlace y luego cifrado
    python3 demo_red.py
    python3 demo_red.py --clave-enlace "frase del enlace"
    # terminal 2, mientras la red está corriendo:
    python3 pruebas/espia_luz.py --segundos 30 --api 7101

Lo que se espera ver:
    sin --clave-enlace : el espía encuentra los ids de todos los nodos, que viajan en los HELLO y las RUTAS
    con --clave-enlace : el espía no encuentra ninguno y los paquetes siguen midiendo lo mismo

Solo funciona con el Faro simulado, que usa UDP multicast. Con la luz real,
lo equivalente sería un tercer receptor infrarrojo grabando.
"""
import argparse
import json
import socket
import struct
import time

GRUPO, PUERTO = "239.255.70.78", 47078       # los mismos que usa DispositivoLuzSimulado


def ids_de_la_red(api):
    with socket.create_connection(("127.0.0.1", api), timeout=10) as s:
        f = s.makefile("rwb")
        f.write(b'{"cmd": "directorio"}\n')
        f.flush()
        r = json.loads(f.readline())
    ids = [n["id"] for n in r["nodos"]] + ([r["propia"]["id"]] if r.get("propia") else [])
    return [bytes.fromhex(i) for i in ids]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--segundos", type=float, default=30)
    p.add_argument("--api", type=int, default=7101, help="API de un nodo para saber qué ids buscar")
    a = p.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.bind(("", PUERTO))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("4s4s", socket.inet_aton(GRUPO), socket.inet_aton("0.0.0.0")))
    s.settimeout(0.5)
    print(f"Grabando la luz simulada durante {a.segundos:.0f} s...")
    luz, tramas = bytearray(), 0
    fin = time.time() + a.segundos
    while time.time() < fin:
        try:
            d, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        if d != b"\x00":                 # el byte 0 hace de "portadora" en el simulado
            luz += d
            tramas += 1
    ids = ids_de_la_red(a.api)
    visibles = sum(1 for i in ids if i in luz)
    print(f"Tramas capturadas: {tramas} ({len(luz)} bytes)")
    print(f"Ids de nodos que el espía puede leer en la luz: {visibles} de {len(ids)}")
    print("=> el enlace va EN CLARO" if visibles else "=> el enlace va CIFRADO: no se ve ningún id")


if __name__ == "__main__":
    main()
