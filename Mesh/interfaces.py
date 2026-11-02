"""
interfaces.py - Los canales físicos de la mesh: UDP (Ethernet o WiFi) y el Faro.

Las dos interfaces le ofrecen al nodo las mismas operaciones:
    enviar(datos, direccion)   envía a un vecino
    difundir(datos)            envía a todos los vecinos de ese canal
y le entregan lo que reciben llamando a nodo.recibir_crudo(datos, interfaz, direccion).

En UDP la dirección es el par (ip, puerto); en el Faro es la MAC de 4 bytes.
Las direcciones solo existen en memoria y nunca se escriben a disco ni a una
bitácora.
"""
import collections
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
    """Ethernet o WiFi: cada paquete de FaroNet viaja dentro de un datagrama UDP.

    Los vecinos son los que se indican con --vecino ip:puerto y, si
    descubrir=True, también los que responden al HELLO que se envía al grupo
    multicast de la red local."""

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
            # El nodo ignora su propio HELLO multicast porque trae su mismo id.
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
        """Envía el HELLO a los vecinos conocidos, a los fijos y al grupo de
        descubrimiento."""
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
    """El enlace de luz entre las dos comunidades. Usa la biblioteca de la
    Capa 1 (DispositivoLuzAdaptador, o DispositivoLuzSimulado en las pruebas),
    y los paquetes viajan con el protocolo PROTO_FARONET (0x0002) de la trama v3.

    Como el Faro transmite a unos 20-30 bytes/s, la interfaz reparte la luz
    con cuidado:
      - Los HELLO y las RUTAS salen cada 300 s, mientras que por UDP salen
        cada 5 y 10 s. Una actualización disparada de RUTAS sale, como mucho,
        una vez cada 60 s.
      - La interfaz tiene su propia fila y le pasa al Faro un solo envío a la
        vez. Los paquetes de datos (celdas y publicaciones) salen antes que
        los de control (HELLO, RUTAS y DIRECTORIO).
      - Un paquete de control nuevo reemplaza al que todavía espera en la fila
        con el mismo tipo, destino e id: un HELLO nuevo reemplaza al viejo,
        unas RUTAS nuevas a las viejas y una entrada del directorio a la del
        mismo nodo. Así el control no se acumula. Si aun así hay MAX_CONTROL
        paquetes esperando, los nuevos se descartan; los de datos siempre se
        encolan.

    El enlace se puede cifrar, y es lo recomendado. Con `clave_enlace`, que
    debe ser la misma frase en los dos Faros, cada paquete viaja dentro de un
    SecretBox de libsodium. Así quien observa la luz no puede leer los HELLO ni
    las RUTAS, que llevan los ids de los nodos. Sin el cifrado, rotar la MAC no
    impediría seguir al Faro, porque el HELLO dice qué nodo está detrás. Además
    evita que alguien sin la frase inyecte paquetes por la luz. El cifrado
    agrega 40 bytes a cada paquete (220 pasan a 260 y 550 a 590), de modo que
    todos siguen midiendo lo mismo."""

    MAX_CONTROL = 12
    SAL_ENLACE = b"faronet-enlace-1"          # 16 bytes; es fija porque el secreto es la frase

    def __init__(self, faro, costo=10, intervalo_hello=300, intervalo_rutas=300,
                 clave_enlace=None, disparo_min=60):
        from DispositivoLuzAdaptador import DIFUSION, PROTO_FARONET
        self._DIFUSION, self._PROTO = DIFUSION, PROTO_FARONET
        self._caja = None
        if clave_enlace:
            import nacl.pwhash
            import nacl.secret
            clave = nacl.pwhash.argon2id.kdf(
                32, clave_enlace.encode("utf-8"), self.SAL_ENLACE,
                opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
                memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE)
            self._caja = nacl.secret.SecretBox(clave)
        self.descartados_enlace = 0
        self.faro = faro
        self.nombre = "faro"
        self.costo = costo
        self.intervalo_hello = intervalo_hello
        self.intervalo_rutas = intervalo_rutas
        self.disparo_min = disparo_min        # segundos mínimos entre dos RUTAS disparadas
        self.nodo = None
        self._datos = collections.deque()     # pares (bytes, destino) en orden de llegada
        self._control = collections.OrderedDict()   # clave del paquete -> (bytes, destino)
        self._en_vuelo = None                 # el envío que el Faro está transmitiendo
        self._lock = threading.Lock()
        self._hay_trabajo = threading.Event()
        self._cerrada = False
        self.estadisticas = {"control_reemplazados": 0, "control_descartados": 0}

    def __str__(self):
        return "faro (luz, enlace cifrado)" if self._caja else "faro (luz)"

    @property
    def cifrado(self):
        return self._caja is not None

    def iniciar(self, nodo):
        self.nodo = nodo
        self.faro.al_recibir(self._PROTO, self._al_recibir)
        self.faro.al_cambiar_mac(lambda _vieja, _nueva: nodo.anunciar_en(self))
        self.faro.registrar_borrado(nodo.panico)
        threading.Thread(target=self._despachador, daemon=True).start()

    def _al_recibir(self, mensaje):
        datos = mensaje.datos
        if self._caja is not None:
            import nacl.exceptions
            try:
                datos = self._caja.decrypt(bytes(datos))
            except nacl.exceptions.CryptoError:
                self.descartados_enlace += 1      # otra frase, o alguien que intenta inyectar paquetes
                return
        self.nodo.recibir_crudo(datos, self, bytes(mensaje.origen))

    @staticmethod
    def _clave_control(datos, destino):
        """Clave con la que se reconoce un paquete de control repetido: su
        tipo, su destino y el id que va al inicio del cuerpo. En un HELLO ese
        id es el nodo que saluda, en RUTAS el primer destino de la lista y en
        DIRECTORIO el nodo de la entrada. Los tres cuerpos empiezan con 1 byte
        de versión seguido del id."""
        return (datos[1], bytes(destino), bytes(datos[15:23]))

    def pendientes(self):
        with self._lock:
            return len(self._datos), len(self._control)

    def _encolar(self, datos, destino, es_control):
        if self._cerrada:
            return None
        datos = bytes(datos)
        with self._lock:
            if es_control:
                clave = self._clave_control(datos, destino)
                if clave in self._control:
                    self._control[clave] = (datos, destino)     # conserva su lugar en la fila con el contenido nuevo
                    self.estadisticas["control_reemplazados"] += 1
                elif len(self._control) >= self.MAX_CONTROL:
                    self.estadisticas["control_descartados"] += 1
                    return None
                else:
                    self._control[clave] = (datos, destino)
            else:
                self._datos.append((datos, destino))
        self._despachar()
        return True

    def _despachar(self):
        """Si el Faro está libre, le pasa el siguiente envío, dando prioridad
        a los datos."""
        with self._lock:
            if self._cerrada:
                return
            if self._en_vuelo is not None and not self._en_vuelo.listo.is_set():
                return
            self._en_vuelo = None
            if self._datos:
                datos, destino = self._datos.popleft()
            elif self._control:
                _clave, (datos, destino) = self._control.popitem(last=False)
            else:
                return
            if self._caja is not None:
                datos = bytes(self._caja.encrypt(datos))
            try:
                self._en_vuelo = self.faro.enviar(datos, destino, self._PROTO)
            except Exception:
                self._en_vuelo = None
        self._hay_trabajo.set()

    def _despachador(self):
        """Hilo que espera a que el Faro termine cada envío y le pasa el siguiente."""
        while not self._cerrada:
            self._hay_trabajo.wait(1.0)
            self._hay_trabajo.clear()
            while not self._cerrada:
                with self._lock:
                    envio = self._en_vuelo
                    quedan = bool(self._datos or self._control)
                if envio is not None and not envio.listo.wait(0.2):
                    continue                    # el Faro sigue transmitiendo
                if not quedan:
                    break
                self._despachar()

    def enviar(self, datos, direccion, es_control=False):
        return self._encolar(datos, bytes(direccion), es_control)

    def difundir(self, datos, direcciones_vecinos=(), es_control=False):
        # En la luz todos los Faros comparten el medio, así que una difusión
        # les llega a todos.
        return self._encolar(datos, self._DIFUSION, es_control)

    def anunciar(self, datos, direcciones_vecinos=()):
        return self._encolar(datos, self._DIFUSION, True)

    def panico(self):
        """Pánico activado por software. Hace lo mismo que el botón físico del
        Faro: borra la MAC y enciende el LED rojo. El Faro, a su vez, llama a
        nodo.panico(), que ya está en marcha y no vuelve a hacer nada."""
        self._vaciar()                    # lo que esperaba en la fila ya no sale
        try:
            self.faro.activar_panico()
        except Exception:
            pass
        self._caja = None

    def _vaciar(self):
        self._cerrada = True
        with self._lock:
            self._datos.clear()
            self._control.clear()
        self._hay_trabajo.set()

    def cerrar(self):
        self._vaciar()
        try:
            self.faro.cerrar()
        except Exception:
            pass
