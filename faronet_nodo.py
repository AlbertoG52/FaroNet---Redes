#!/usr/bin/env python3
"""
faronet_nodo.py - Arranca un nodo de FaroNet (mesh + aplicación anónima).

Todos los parámetros se reciben desde la terminal. Ejemplos:

  # Nodo común por WiFi/Ethernet (descubre solo a los vecinos de la red local):
  python3 faronet_nodo.py --alias ana --puerto 7001 --api 7101

  # Nodo con vecinos fijos (por ejemplo, otra computadora o una Pi por Tailscale):
  python3 faronet_nodo.py --alias beto --puerto 7000 --vecino 100.64.0.5:7000

  # Nodo puerta de enlace de una comunidad, con el Faro REAL (en la Raspberry Pi):
  sudo pigpiod
  python3 faronet_nodo.py --alias faroA --faro real --pausa 40

  # Lo mismo pero con el Faro SIMULADO (en la laptop, sin Raspberry Pi):
  python3 faronet_nodo.py --alias faroA --faro simulado --velocidad 25

  # Varias comunidades en una sola computadora (sin descubrimiento, vecinos fijos):
  python3 faronet_nodo.py --alias n1 --puerto 7001 --api 7101 --sin-descubrir --vecino 127.0.0.1:7002

Luego, en otra terminal, se usa el chat:  python3 App/chat.py --api 7101 ...
"""
import argparse
import os
import signal
import sys
import threading

AQUI = os.path.dirname(os.path.abspath(__file__))
for carpeta in ("Mesh", "App", "Faro"):
    sys.path.insert(0, os.path.join(AQUI, carpeta))

try:
    import nacl  # noqa: F401
except ImportError:
    sys.exit("Falta PyNaCl. En la Raspberry Pi: sudo apt install python3-nacl  "
             "(en Windows/Mac: pip install pynacl)")

from identidad import Identidad, id_a_texto    # noqa: E402
from interfaces import InterfazFaro, InterfazUDP    # noqa: E402
from nodo import Nodo                          # noqa: E402
from servicio import ServicioChat              # noqa: E402


def crear_faro(a):
    from DispositivoLuzAdaptador import DispositivoLuzAdaptador, DispositivoLuzSimulado
    if a.faro == "simulado":
        return DispositivoLuzSimulado(nombre=a.faro_nombre, velocidad=a.velocidad,
                                      perdida=a.perdida, t_us=a.t_us, pausa=a.pausa,
                                      depurar=a.debug_faro)
    return DispositivoLuzAdaptador(pin_emisor=a.pin_emisor, pin_receptor=a.pin_receptor,
                                   led_tx=a.led_tx, led_rx=a.led_rx, boton=a.boton,
                                   led_panico=a.led_panico, t_us=a.t_us, pausa=a.pausa,
                                   activo_alto=a.activo_alto, depurar=a.debug_faro)


def argumentos():
    p = argparse.ArgumentParser(description="Nodo de la red mesh anónima FaroNet",
                                epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--alias", default="", help="nombre visible en el directorio (máx. 16 bytes)")
    p.add_argument("--efimero", action="store_true",
                   help="no guardar las llaves: identidad nueva en cada arranque")
    p.add_argument("--carpeta", default="~/.faronet",
                   help="dónde guardar las llaves del nodo (por defecto ~/.faronet)")
    p.add_argument("--api", type=int, default=7100, help="puerto local del chat (127.0.0.1)")
    p.add_argument("--gateway", action="store_true",
                   help="anunciarse como gateway hacia el edge (Cloudflare Workers)")
    p.add_argument("--retardo-max", type=float, default=0.5,
                   help="espera aleatoria máxima (s) al reenviar una celda")
    p.add_argument("--rotar-mac-cada", type=float, default=1800,
                   help="segundos entre rotaciones de la MAC del Faro (0 = no rotar)")
    p.add_argument("--depurar", action="store_true", help="mostrar eventos (sin direcciones)")

    u = p.add_argument_group("UDP: Ethernet o WiFi")
    u.add_argument("--sin-udp", action="store_true", help="no usar la red IP")
    u.add_argument("--medio", choices=("wifi", "ethernet"), default="wifi",
                   help="nombre del enlace IP (cambia el costo por defecto: wifi 2, ethernet 1)")
    u.add_argument("--ip", default="0.0.0.0", help="IP local donde escuchar")
    u.add_argument("--puerto", type=int, default=7000, help="puerto UDP de la mesh")
    u.add_argument("--vecino", action="append", default=[], metavar="IP:PUERTO",
                   help="vecino fijo (se puede repetir)")
    u.add_argument("--sin-descubrir", action="store_true",
                   help="no buscar vecinos por multicast en la red local")
    u.add_argument("--costo", type=int, default=None, help="costo del enlace IP")

    f = p.add_argument_group("Faro (enlace de luz entre comunidades)")
    f.add_argument("--faro", choices=("ninguno", "real", "simulado"), default="ninguno")
    f.add_argument("--costo-faro", type=int, default=10)
    f.add_argument("--pausa", type=int, default=12, help="igual en los dos Faros")
    f.add_argument("--t-us", type=int, default=600, help="unidad T en µs (igual en los dos)")
    f.add_argument("--velocidad", type=float, default=None,
                   help="simulado: bytes por segundo (el real va a unos 25)")
    f.add_argument("--perdida", type=float, default=0.0, help="simulado: fracción de tramas perdidas")
    f.add_argument("--faro-nombre", default=None, help="simulado: nombre para guardar su MAC")
    f.add_argument("--pin-emisor", type=int, default=38)
    f.add_argument("--pin-receptor", type=int, default=40)
    f.add_argument("--led-tx", type=int, default=13)
    f.add_argument("--led-rx", type=int, default=15)
    f.add_argument("--boton", type=int, default=11)
    f.add_argument("--led-panico", type=int, default=16)
    f.add_argument("--activo-alto", action="store_true")
    f.add_argument("--debug-faro", action="store_true", help="mostrar cada trama del Faro")
    return p.parse_args()


def main():
    a = argumentos()
    if a.sin_udp and a.faro == "ninguno":
        sys.exit("El nodo necesita al menos una interfaz: quite --sin-udp o use --faro")
    nombre = a.alias or "nodo"
    archivo = None if a.efimero else os.path.join(os.path.expanduser(a.carpeta),
                                                  f"nodo-{nombre}-{a.api}.llave")
    identidad = Identidad(archivo)

    interfaces = []
    try:
        if not a.sin_udp:
            costo = a.costo if a.costo is not None else (1 if a.medio == "ethernet" else 2)
            interfaces.append(InterfazUDP(a.medio, a.ip, a.puerto, a.vecino,
                                          descubrir=not a.sin_descubrir, costo=costo))
        if a.faro != "ninguno":
            interfaces.append(InterfazFaro(crear_faro(a), costo=a.costo_faro))
    except (OSError, ValueError, RuntimeError) as e:
        for i in interfaces:
            i.cerrar()
        sys.exit(f"No se pudo crear una interfaz: {e}")

    nodo = Nodo(identidad, interfaces, alias=a.alias, gateway=a.gateway,
                retardo_max=a.retardo_max, depurar=a.depurar,
                rotar_mac_cada=a.rotar_mac_cada)
    servicio = ServicioChat(nodo)
    try:
        puerto_api = servicio.servir(a.api)
    except OSError as e:
        nodo.detener()
        sys.exit(f"No se pudo abrir la API en 127.0.0.1:{a.api}: {e}")

    terminado = threading.Event()
    nodo.al_panico.append(terminado.set)
    signal.signal(signal.SIGINT, lambda *_: terminado.set())
    signal.signal(signal.SIGTERM, lambda *_: terminado.set())
    nodo.iniciar()
    print(f"Nodo FaroNet listo: {a.alias or '(sin alias)'}  id {id_a_texto(nodo.id)}", flush=True)
    print(f"  interfaces: {', '.join(str(i) for i in interfaces)}", flush=True)
    print(f"  chat:       python3 App/chat.py --api {puerto_api} interactivo", flush=True)
    print("  Ctrl+C para salir.", flush=True)
    while not terminado.wait(0.5):
        pass
    if nodo.panico_activado:
        print("PÁNICO: llaves y datos borrados. Nodo detenido.", flush=True)
    nodo.detener()


if __name__ == "__main__":
    main()
