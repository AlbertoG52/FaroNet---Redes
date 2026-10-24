"""
tramas_externas.py - Enviar por el Faro tramas o paquetes creados FUERA del
Faro, y ver lo que llega con su protocolo.

Hay dos formas de usar el Faro con algo hecho afuera:

  1) enviar-trama: una trama del Faro COMPLETA (AA 7E ... CRC) armada por
     otro programa, por ejemplo con Scapy (faro_scapy.py). Se revisa que sea
     válida y se transmite tal cual, sin ACK.
  2) encapsular: un paquete de otro protocolo (IPv4, IPv6, una trama
     Ethernet...) va DENTRO del campo de datos, con el campo protocolo que
     corresponde. Se entrega completo y con ACK, como cualquier mensaje.

Uso (en la Pi que recibe, primero):
    python3 tramas_externas.py escuchar

En la Pi que envía:
    python3 tramas_externas.py enviar-trama --hex AA7E31...
    python3 tramas_externas.py enviar-trama --archivo trama.bin
    python3 tramas_externas.py encapsular --protocolo ipv4 --hex 4500001C...
    python3 tramas_externas.py encapsular --protocolo 0x6558 --archivo ethernet.bin --destino 3FA107C2
    python3 tramas_externas.py scapy-demo          # arma IP/UDP con Scapy y lo envía de las dos formas

Todas aceptan las opciones del Faro (--pausa, --t-us, --simulado...). Ver --help.
"""
import argparse
import secrets
import sys
import time

from DispositivoLuzAdaptador import Panico, trama
import faro as programa_faro


def leer_datos(a):
    if a.hex:
        return bytes.fromhex(a.hex.replace(" ", "").replace(":", ""))
    with open(a.archivo, "rb") as f:
        return f.read()


def elegir_destino(faro, texto):
    """El destino indicado; si no, el último Faro escuchado; si no, todos."""
    if texto:
        return trama.mac_desde_texto(texto)
    print("Buscando al otro Faro...")
    faro.anunciar()
    fin = time.time() + 10
    while not faro.vecinos_activos() and time.time() < fin:
        time.sleep(0.2)
    activos = faro.vecinos_activos()
    return max(activos, key=faro.vecinos.get) if activos else trama.DIFUSION


def mostrar(m, hexdump):
    print(f"\n[de {trama.mac_a_texto(m.origen)}, protocolo "
          f"{trama.protocolo_a_texto(m.protocolo)}, {len(m.datos)} bytes]")
    capa = None
    try:
        if m.protocolo == trama.PROTO_IPV4:
            from scapy.layers.inet import IP as capa
        elif m.protocolo == trama.PROTO_IPV6:
            from scapy.layers.inet6 import IPv6 as capa
        elif m.protocolo == trama.PROTO_ETHERNET:
            from scapy.layers.l2 import Ether as capa
    except ImportError:
        capa = None
    if capa is not None:
        try:
            capa(m.datos).show()
        except Exception as e:              # paquete mal formado: mostrar los bytes
            print(f"(Scapy no pudo interpretarlo: {e})")
            capa = None
    elif m.protocolo == trama.PROTO_CHAT:
        print(m.datos.decode("utf-8", errors="replace"))
    if hexdump or capa is None and m.protocolo != trama.PROTO_CHAT:
        print(m.datos.hex(" "))


def escuchar(faro, a):
    faro.anunciar()
    print(f"Escuchando con MAC {trama.mac_a_texto(faro.mac)} (Ctrl+C para salir)...")
    while True:
        m = faro.recibir(timeout=1)
        if m:
            mostrar(m, a.hexdump)


def enviar_trama(faro, a):
    datos = leer_datos(a)
    t = trama.leer_trama(datos)             # ValueError si está mal formada
    print(f"Trama válida: v{t.version}, tipo {t.tipo}, de {trama.mac_a_texto(t.origen)} "
          f"para {trama.mac_a_texto(t.destino)}, {len(datos)} bytes. Enviando (sin ACK)...")
    faro.enviar_trama_externa(datos).esperar()
    print("Enviada.")


def encapsular(faro, a):
    datos = leer_datos(a)
    protocolo = trama.protocolo_desde_texto(a.protocolo)
    destino = elegir_destino(faro, a.destino)
    print(f"Enviando {len(datos)} bytes con protocolo {trama.protocolo_a_texto(protocolo)} "
          f"a {trama.mac_a_texto(destino)}...")
    ok = faro.enviar(datos, destino, protocolo).esperar()
    print("Entregado con ACK." if ok and destino != trama.DIFUSION else
          "Enviado a todos (sin confirmación)." if ok else "NO se pudo entregar.")


def scapy_demo(faro, a):
    try:
        from scapy.all import IP, UDP, Raw
        from faro_scapy import FaroTrama
    except ImportError:
        sys.exit("Falta Scapy: sudo apt install python3-scapy")
    destino = elegir_destino(faro, a.destino)
    paquete = IP(src="10.0.0.1", dst="10.0.0.2") / UDP(sport=4000, dport=5000) / Raw(b"hola por la luz")

    print("\n1) Trama del Faro COMPLETA armada con Scapy (difusión, sin ACK):")
    t = FaroTrama(destino=trama.DIFUSION, origen=faro.mac, n_msg=secrets.randbelow(256),
                  protocolo=trama.PROTO_IPV4) / paquete
    t.show()
    faro.enviar_trama_externa(bytes(t)).esperar()
    print("Enviada.")

    print(f"\n2) El mismo paquete IP encapsulado, con ACK, a {trama.mac_a_texto(destino)}:")
    ok = faro.enviar(bytes(paquete), destino, trama.PROTO_IPV4).esperar()
    print("Entregado con ACK." if ok and destino != trama.DIFUSION else
          "Enviado a todos (sin confirmación)." if ok else "NO se pudo entregar.")


def main():
    p = argparse.ArgumentParser(description="Tramas y paquetes externos por el Faro")
    sub = p.add_subparsers(dest="modo", required=True)
    e = sub.add_parser("escuchar", help="mostrar lo que llega, con su protocolo")
    e.add_argument("--hexdump", action="store_true", help="mostrar también los bytes")
    for nombre, ayuda in (("enviar-trama", "enviar una trama completa armada afuera"),
                          ("encapsular", "enviar un paquete de otro protocolo dentro del Faro")):
        s = sub.add_parser(nombre, help=ayuda)
        fuente = s.add_mutually_exclusive_group(required=True)
        fuente.add_argument("--hex", help="los bytes en hexadecimal")
        fuente.add_argument("--archivo", help="archivo binario con los bytes")
        if nombre == "encapsular":
            s.add_argument("--protocolo", required=True,
                           help="ipv4, ipv6, ethernet, faronet, chat o un número (0x0800)")
            s.add_argument("--destino", help="MAC destino (si no, el último Faro escuchado)")
    d = sub.add_parser("scapy-demo", help="armar IP/UDP con Scapy y enviarlo")
    d.add_argument("--destino", help="MAC destino (si no, el último Faro escuchado)")
    for s in (e, d) + tuple(sub.choices[n] for n in ("enviar-trama", "encapsular")):
        programa_faro.argumentos_del_faro(s)
    a = p.parse_args()

    try:
        faro = programa_faro.crear_faro(a)
    except (ValueError, RuntimeError, OSError) as ex:
        sys.exit(str(ex))
    try:
        {"escuchar": escuchar, "enviar-trama": enviar_trama, "encapsular": encapsular,
         "scapy-demo": scapy_demo}[a.modo](faro, a)
    except (ValueError, OSError) as ex:
        print(f"Error: {ex}")
    except (KeyboardInterrupt, Panico):
        pass
    finally:
        faro.cerrar()


if __name__ == "__main__":
    main()
