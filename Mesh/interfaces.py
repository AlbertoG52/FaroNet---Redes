"""
interfaces.py - Los canales físicos de la mesh: UDP (Ethernet o WiFi) y Faro.

Las dos interfaces ofrecen lo mismo al nodo:
    enviar(datos, direccion)   a un vecino
    difundir(datos)            a todos los vecinos de ese canal
y le entregan lo recibido con  nodo.recibir_crudo(datos, interfaz, direccion).

`direccion` es (ip, puerto) en UDP y la MAC de 4 bytes del Faro en el Faro.
Las direcciones viven solo en memoria: nunca se escriben a disco ni a bitácoras.
"""
import os
import socket
import struct
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "..", "Faro"))

GRUPO_DESCUBRIMIENTO = "239.255.70.79"
PUERTO_DESCUBRIMIENTO = 47079


class InterfazUDP:
    """Ethernet o WiFi: paquetes de FaroNet dentro de datagramas UDP.

    Vecinos: los que se indiquen con --vecino ip:puerto y, con descubrir=True,
    los que respondan al HELLO enviado al grupo multicast de la red local."""

    def __init__(self, nombre="wifi", ip="0.0.0.0", puerto=7000, vecinos=(),
                 descubrir=True, costo=2, intervalo_hello=5, intervalo_rutas=10):
        self.nombre = nombre
        self.costo = costo
        self.intervalo_hello = intervalo_hello
        self.intervalo_rutas = intervalo_rutas
        self.descubrir = descubrir
        self.estaticos = [self._resolver(v) for v in vecinos]
        self.nodo = None
        self._activo = True

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, puerto))
        self.sock.settimeout(0.5)
        self.puerto = self.sock.getsockname()[1]
        if descubrir:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            d.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    d.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            d.bind(("", PUERTO_DESCUBRIMIENTO))
            d.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         struct.pack("4s4s", socket.inet_aton(GRUPO_DESCUBRIMIENTO),
                                     socket.inet_aton("0.0.0.0")))
            d.settimeout(0.5)
            self.sock_desc = d
        else:
            self.sock_desc = None

    @staticmethod
    def _resolver(texto):
        host, _, puerto = texto.rpartition(":")
        if not host or not puerto.isdigit():
            raise ValueError(f"vecino inválido {texto!r}: use ip:puerto")
        return (socket.gethostbyname(host), int(puerto))

    def __str__(self):
        return f"{self.nombre} (UDP {self.puerto})"

    def iniciar(self, nodo):
        self.nodo = nodo
        for s in filter(None, (self.sock, self.sock_desc)):
            threading.Thread(target=self._escuchar, args=(s,), daemon=True).start()

    def _escuchar(self, s):
        while self._activo:
            try:
                datos, direccion = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if not self._activo:
                    return
                time.sleep(0.1)
                continue
            # (el nodo ignora su propio HELLO multicast porque trae su mismo id)
            self.nodo.recibir_crudo(datos, self, direccion)

    def enviar(self, datos, direccion):
        try:
            self.sock.sendto(datos, direccion)
        except OSError:
            pass

    def difundir(self, datos, direcciones_vecinos=()):
        destinos = set(self.estaticos) | set(direcciones_vecinos)
        for d in destinos:
            self.enviar(datos, d)

    def anunciar(self, datos, direcciones_vecinos=()):
        """HELLO: a los vecinos conocidos, a los estáticos y al grupo de descubrimiento."""
        self.difundir(datos, direcciones_vecinos)
        if self.descubrir:
            self.enviar(datos, (GRUPO_DESCUBRIMIENTO, PUERTO_DESCUBRIMIENTO))

    def ocupada(self):
        return False

    def cerrar(self):
        self._activo = False
        for s in filter(None, (self.sock, self.sock_desc)):
            try:
                s.close()
            except OSError:
                pass


class InterfazFaro:
    """El enlace de luz entre las dos comunidades, sobre la biblioteca de la
    Capa 1 (DispositivoLuzAdaptador o DispositivoLuzSimulado). Los paquetes
    viajan con el protocolo PROTO_FARONET (0x0002) de la trama v3.

    El Faro va a unos 25 bytes/s: por eso los HELLO y las RUTAS se mandan
    cada 90 s y, si ya hay muchos envíos esperando, se descartan los de
    control (los de datos siempre se encolan)."""

    MAX_PENDIENTES = 12

    def __init__(self, faro, costo=10, intervalo_hello=90, intervalo_rutas=90):
        from DispositivoLuzAdaptador import DIFUSION, PROTO_FARONET
        self._DIFUSION, self._PROTO = DIFUSION, PROTO_FARONET
        self.faro = faro
        self.nombre = "faro"
        self.costo = costo
        self.intervalo_hello = intervalo_hello
        self.intervalo_rutas = intervalo_rutas
        self.nodo = None
        self._pendientes = []
        self._lock = threading.Lock()

    def __str__(self):
        return "faro (luz)"

    def iniciar(self, nodo):
        self.nodo = nodo
        self.faro.al_recibir(self._PROTO, self._al_recibir)
        self.faro.al_cambiar_mac(lambda _vieja, _nueva: nodo.anunciar_en(self))
        self.faro.registrar_borrado(nodo.panico)

    def _al_recibir(self, mensaje):
        self.nodo.recibir_crudo(mensaje.datos, self, bytes(mensaje.origen))

    def ocupada(self):
        with self._lock:
            self._pendientes = [e for e in self._pendientes if not e.listo.is_set()]
            return len(self._pendientes) >= self.MAX_PENDIENTES

    def _encolar(self, datos, destino, es_control):
        if es_control and self.ocupada():
            return None
        try:
            envio = self.faro.enviar(datos, destino, self._PROTO)
        except Exception:
            return None
        with self._lock:
            self._pendientes.append(envio)
        return envio

    def enviar(self, datos, direccion, es_control=False):
        return self._encolar(datos, bytes(direccion), es_control)

    def difundir(self, datos, direcciones_vecinos=(), es_control=False):
        # En la luz todos los Faros comparten el medio: una difusión llega a todos.
        return self._encolar(datos, self._DIFUSION, es_control)

    def anunciar(self, datos, direcciones_vecinos=()):
        return self._encolar(datos, self._DIFUSION, True)

    def cerrar(self):
        try:
            self.faro.cerrar()
        except Exception:
            pass
