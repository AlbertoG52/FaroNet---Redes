"""
DispositivoLuzAdaptador.py - Biblioteca de la Capa 1 de FaroNet.

Es la única puerta entre la luz y las capas de arriba (la mesh, el chat).
Une en un solo objeto el emisor y el receptor infrarrojo del Faro y se
encarga de todo lo de la Capa 1:

    - Codificación: OOK sobre portadora de 38 kHz, distancia de pulsos.
    - Trama versión 3 (ver Trama/trama.py): inicio, longitud, MACs, n.º de
      mensaje, fragmento, PROTOCOLO (qué lleva adentro) y CRC. Se siguen
      leyendo tramas de versión 2.
    - Acceso al medio CSMA/CA: escucha antes de transmitir y, si el canal
      está ocupado o hubo un choque, espera un tiempo aleatorio.
    - ACK y reenvío: cada trama dirigida a un Faro se confirma; si la
      confirmación no llega, se reenvía (hasta MAX_INTENTOS veces).
    - Nunca entrega mensajes incompletos ni repetidos.
    - Ignora el eco (su propia luz reflejada).
    - Dirección física (MAC) guardada en la tarjeta SD, rotable. Se anuncia
      al arrancar, cada 30 s y al escuchar a un Faro nuevo.
    - LED TX, LED RX y botón de pánico (borra datos en memoria, la MAC y lo
      que las capas de arriba registren).
    - Puede enviar tramas creadas fuera del Faro (por ejemplo con Scapy) y
      transportar paquetes de otros protocolos (IPv4, IPv6, Ethernet...).

CONTRATO PARA LAS CAPAS DE ARRIBA (no cambia entre versiones menores):

    faro = DispositivoLuzAdaptador()            # Faro real (Raspberry Pi)
    faro = DispositivoLuzSimulado()             # Faro falso por UDP (laptop)

    envio = faro.enviar(datos, destino, protocolo)   # no bloquea; va a la cola
    envio.esperar()            # True = entregado (unicast con ACK) o enviado
                               # (difusión); False = no se pudo entregar
    m = faro.recibir(timeout, protocolo)        # Mensaje(origen, protocolo, datos) o None
    faro.al_recibir(protocolo, funcion)         # o recibir por función
    faro.mac, faro.mtu, faro.vecinos_activos()
    faro.rotar_mac(); faro.al_cambiar_mac(funcion)
    faro.registrar_borrado(funcion)             # se llama al presionar pánico
    faro.enviar_trama_externa(bytes)            # trama completa armada afuera
    faro.cerrar()

    Garantías: un mensaje de hasta faro.mtu (1650) bytes llega completo o no
    llega; no se entrega dos veces; a un destino (unicast) se confirma con
    ACK y se reenvía hasta 5 veces; a todos (difusión) no se confirma; el
    ORDEN entre mensajes distintos NO está garantizado. Velocidad real
    aproximada: 200 bit/s (unos 25 bytes/s).

Conexiones por defecto (números de pin FÍSICO):
    38 emisor KY-005 (S)          40 receptor KY-022 (S)
    13 LED TX                     15 LED RX
    11 botón de pánico            16 LED rojo de pánico
     1 3.3 V (+ del KY-022)        6 GND

Requiere el servicio pigpio corriendo:  sudo pigpiod
(DispositivoLuzSimulado no necesita pigpio ni Raspberry Pi.)
"""
import collections
import functools
import os
import queue
import random
import secrets
import socket
import struct
import sys
import threading
import time

try:
    import pigpio                   # solo existe en la Raspberry Pi
except ImportError:
    pigpio = None

# trama.py está en la carpeta Trama/, al lado de esta carpeta.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Trama"))
import trama  # noqa: E402
from trama import (DIFUSION, PROTO_CHAT, PROTO_FARONET, PROTO_MEDICION,  # noqa: E402
                   PROTO_IPV4, PROTO_IPV6, PROTO_ETHERNET)

# Lo que las capas de arriba pueden importar de esta biblioteca.
__all__ = ["DispositivoLuzAdaptador", "DispositivoLuzSimulado", "Mensaje", "Envio",
           "Panico", "trama", "DIFUSION", "PROTO_CHAT", "PROTO_FARONET",
           "PROTO_MEDICION", "PROTO_IPV4", "PROTO_IPV6", "PROTO_ETHERNET",
           "VERSION_BIBLIOTECA"]

# Pin físico (1-40) -> número GPIO (BCM), que es el que usa pigpio
FISICO_A_GPIO = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22,
    16: 23, 18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0,
    28: 1, 29: 5, 31: 6, 32: 12, 33: 13, 35: 19, 36: 16, 37: 26, 38: 20,
    40: 21,
}

ARCHIVO_MAC = os.path.expanduser("~/.faronet/mac")
VERSION_BIBLIOTECA = "1.0"

# --- Parámetros del acceso al medio (en unidades de T) ----------------------
# Los silencios se miden SUMADOS a la pausa entre bytes (--pausa), porque
# dentro de una trama ya hay un silencio de pausa·T después de cada byte.
CANAL_LIBRE_T = 40      # libre si no hubo luz en (pausa + 40)T
RANURA_T = 20           # duración de una "ranura" de espera aleatoria (12 ms)
VENTANA_MIN = 8         # espera aleatoria inicial: 0..7 ranuras
VENTANA_MAX = 64        # la ventana se duplica en cada reintento, hasta 64
MAX_INTENTOS = 5        # veces que se envía una trama antes de rendirse
FIN_BYTE_T = 8          # un byte terminó si hubo 8T de silencio
FLANCOS_POR_BYTE = 20   # cabecera (2) + 8 bits (16) + ráfaga final (2)
ESPERA_BYTE_S = 0.15    # cuánto esperar el resto de un byte cuyos flancos llegan tarde
TRAMA_INCOMPLETA_T = 60     # a mitad de trama, (pausa + 60)T sin luz: se perdió
TRAMA_INCOMPLETA_S = 0.5    # a mitad de trama, 0.5 s sin bytes válidos: también
ENTRE_TRAMAS_T = 80         # silencio mínimo entre dos tramas propias: (pausa + 80)T
MENSAJE_INCOMPLETO_S = 120  # un mensaje sin fragmentos NUEVOS por 120 s: se descarta
RECORDAR_S = 120            # cuánto tiempo se recuerdan mensajes ya entregados
ANUNCIO_CADA_S = 30         # cada cuánto se repite el anuncio de la MAC
MAC_VIEJA_S = 300           # tras rotar la MAC, se sigue aceptando la vieja 5 min
VECINO_ACTIVO_S = 90        # un vecino sigue "activo" si se oyó en los últimos 90 s
MAX_SIN_LEER = 256          # mensajes recibidos que nadie leyó (se borran los viejos)


class Panico(Exception):
    """Se lanza cuando se presionó el botón de pánico."""


Mensaje = collections.namedtuple("Mensaje", "origen protocolo datos")
Mensaje.__doc__ = "Un mensaje completo recibido: MAC de origen, protocolo y datos."


def _borrar(datos):
    """Sobrescribe un bytearray con ceros y lo vacía."""
    for i in range(len(datos)):
        datos[i] = 0
    datos.clear()


# ---- Codificación de línea (igual que emisor_IR.py / receptor_IR.py) -------
def rafaga(gpio, duracion_us, frecuencia, ciclo=33):
    """Pulsos de una ráfaga de portadora. `ciclo` = % del tiempo que el LED
    está encendido en cada periodo de la portadora (33 por defecto). Con
    menos % la luz llega más débil; la frecuencia (38 kHz) no cambia."""
    periodo = round(1e6 / frecuencia)          # 26 µs a 38 kHz
    encendido = max(1, min(periodo - 1, int(periodo * ciclo / 100)))
    apagado = periodo - encendido
    ciclos = max(1, round(duracion_us / periodo))
    mascara = 1 << gpio
    pulsos = []
    for _ in range(ciclos):
        pulsos.append(pigpio.pulse(mascara, 0, encendido))
        pulsos.append(pigpio.pulse(0, mascara, apagado))
    return pulsos


# Codificación de un byte (en unidades de T). Después de cada ráfaga va una
# pausa de al menos el DOBLE de la ráfaga: los receptores de 38 kHz (VS1838B,
# KY-022) dejan de ver ráfagas muy seguidas (las toman por ruido). Con la
# pausa de 1T que se usaba antes para el bit 0, los bytes con muchos ceros
# (por ejemplo "0", "A", "1") se perdían en las tramas largas.
CABECERA_T = 3          # ráfaga de cabecera
PAUSA_CABECERA_T = 4    # pausa después de la cabecera
PAUSA_BIT0_T = 2        # bit 0: ráfaga T + pausa 2T
PAUSA_BIT1_T = 4        # bit 1: ráfaga T + pausa 4T
UMBRAL_BIT_T = 3        # pausa >= 3T se lee como 1


@functools.lru_cache(maxsize=1024)
def pulsos_de_byte(gpio, byte, T, frecuencia, pausa_final=12, ciclo=33):
    """cabecera 3T+4T, 8 bits (T + pausa 2T ó 4T), fin T + pausa_final·T."""
    def pausa(us):
        return [pigpio.pulse(0, 1 << gpio, int(us))]
    p = rafaga(gpio, CABECERA_T * T, frecuencia, ciclo) + pausa(PAUSA_CABECERA_T * T)
    for i in range(7, -1, -1):
        bit = (byte >> i) & 1
        p += rafaga(gpio, T, frecuencia, ciclo) + pausa(
            (PAUSA_BIT1_T if bit else PAUSA_BIT0_T) * T)
    p += rafaga(gpio, T, frecuencia, ciclo) + pausa(pausa_final * T)
    return p


def decodificar(d, T):
    """d = [ráfaga, pausa, ráfaga, pausa, ...] en µs. Devuelve el byte o None."""
    if len(d) < 19:
        return None
    if not (2 * T <= d[0] <= 5 * T):          # cabecera: ráfaga ~3T
        return None
    if not (2.5 * T <= d[1] <= 6 * T):        # cabecera: pausa ~4T
        return None
    byte = 0
    for i in range(8):
        p = d[3 + 2 * i]
        if p > 6 * T:
            return None
        byte = (byte << 1) | (1 if p >= UMBRAL_BIT_T * T else 0)
    return byte


# ---- Dirección física en la tarjeta SD -------------------------------------
def _mac_nueva():
    while True:
        mac = secrets.token_bytes(4)
        if mac not in (trama.DIFUSION, bytes(4)):
            return mac


def _guardar_mac(archivo, mac):
    if archivo is None:                     # sin archivo: la MAC vive solo en memoria
        return
    os.makedirs(os.path.dirname(archivo), mode=0o700, exist_ok=True)
    temporal = archivo + ".nuevo"
    with open(temporal, "w") as f:
        f.write(mac.hex().upper() + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temporal, 0o600)
    os.replace(temporal, archivo)          # el cambio es completo o no ocurre


def cargar_o_crear_mac(archivo=ARCHIVO_MAC):
    """Lee la MAC del archivo; si no existe (o está dañado), crea una al azar."""
    if archivo is not None:
        try:
            with open(archivo) as f:
                return trama.mac_desde_texto(f.read().strip())
        except (OSError, ValueError):
            pass
    mac = _mac_nueva()
    _guardar_mac(archivo, mac)
    return mac


def borrar_mac(archivo=ARCHIVO_MAC):
    """Sobrescribe el archivo de la MAC con ceros y lo elimina."""
    if archivo is None:
        return
    try:
        tamano = os.path.getsize(archivo)
        with open(archivo, "r+b") as f:
            f.write(bytes(tamano))
            f.flush()
            os.fsync(f.fileno())
        os.remove(archivo)
    except OSError:
        pass


# ---- Lógica común (Faro real y simulado) ------------------------------------
class Envio:
    """Un mensaje en la cola de salida. Cuando termina, `listo` se activa y
    `exito` queda en True (todas las tramas confirmadas, o enviado a todos)
    o False (no se pudo entregar)."""

    def __init__(self, datos, destino, protocolo=PROTO_CHAT, tramas=None):
        self.datos = bytearray(datos)
        self.destino = destino
        self.protocolo = protocolo
        self.tramas = tramas            # tramas ya armadas (anuncios, externas, mediciones)
        self.listo = threading.Event()
        self.exito = None
        self.intentos = 0

    def esperar(self, timeout=None):
        """Espera a que termine el envío. Devuelve True, False o None (timeout)."""
        self.listo.wait(timeout)
        return self.exito


class _FaroBase:
    """Todo lo de la Capa 1 que no depende del medio: tramas, CSMA/CA, ACK,
    reensamblado, MAC, pánico. Las subclases solo saben transmitir bytes y
    entregar los bytes recibidos a _procesar_byte()."""

    def __init__(self, t_us, pausa, archivo_mac, al_panico, depurar,
                 datos_por_trama=trama.MAX_DATOS_V3):
        if not 1 <= datos_por_trama <= trama.MAX_DATOS_V3:
            raise ValueError(f"datos_por_trama debe estar entre 1 y {trama.MAX_DATOS_V3}")
        self.T = t_us
        self.pausa = pausa
        self.datos_por_trama = datos_por_trama     # tramas más cortas = menos riesgo
        self.depurar = depurar
        self.archivo_mac = archivo_mac
        self.al_panico = al_panico

        self.mac = cargar_o_crear_mac(archivo_mac)
        self._macs_propias = {self.mac}     # incluye las anteriores (eco tardío)
        self._macs_viejas = {}               # MAC anterior -> hora en que se dejó de usar
        self._n_msg = secrets.randbelow(256)
        self.vecinos = {}                    # MAC -> última vez que se escuchó

        self.estadisticas = dict(
            tramas_enviadas=0, reenvios=0, acks_enviados=0, acks_recibidos=0,
            tramas_ok=0, tramas_descartadas=0, bytes_ilegibles=0, ecos=0,
            duplicados=0, mensajes_entregados=0, mensajes_fallidos=0,
            mensajes_incompletos=0, acks_tardios=0)

        self._cola_salida = queue.Queue()
        self._cola_acks = queue.Queue()
        self._recibidos = collections.deque()      # Mensaje con datos en bytearray
        self._hay_recibidos = threading.Condition()
        self._oyentes = {}                   # protocolo -> función(Mensaje)
        self._al_cambiar_mac = []            # funciones(mac_vieja, mac_nueva)
        self._borradores = []                # funciones a llamar en el pánico
        self._sensibles = []                 # bytearrays a borrar en el pánico
        self._esperando_ack = {}             # (mac, n_msg, fragmento) -> Event
        self._parciales = {}                 # (mac, n_msg) -> fragmentos
        self._entregados = {}                # (mac, n_msg) -> hora
        self._ultima_luz = 0.0               # hora en que el receptor vio luz por última vez
        self._ultimo_byte = time.time()
        self._fin_tx = 0.0                   # hora en que terminó la última trama propia
        self._s_por_byte = None              # tiempo real que toma enviar un byte (medido)
        self._ultimo_anuncio = 0.0
        self._desarmador = trama.Desarmador()
        # ¿Se corta una trama a medias por silencio medido con el reloj de la
        # computadora? El Faro real lo mide con la hora exacta de los flancos
        # (más confiable si la Pi está ocupada) y aquí solo usa el plazo de 0.5 s.
        self._cortar_por_silencio = True
        self.observador = None               # función(trama, crc_ok): mediciones
        self.panico_activado = False
        self._activo = True
        self._hilos = []

    def _arrancar_hilos(self, *objetivos):
        self._hilos = [threading.Thread(target=f, daemon=True)
                       for f in (self._hilo_emisor,) + objetivos]
        for h in self._hilos:
            h.start()

    # ------------------------------------------------------------------ API
    @property
    def mtu(self):
        """Tamaño máximo de un mensaje para enviar(): 15 tramas de
        datos_por_trama bytes (1650 bytes con el valor normal, 110)."""
        return self.datos_por_trama * trama.MAX_FRAGMENTOS

    def enviar(self, datos, destino=DIFUSION, protocolo=PROTO_CHAT):
        """Pone un mensaje en la cola de salida y devuelve su Envio.
        destino: MAC de 4 bytes o DIFUSION (a todos, sin ACK).
        protocolo: qué lleva adentro (trama.PROTO_*), 0x0001..0xFFFF.
        Use envio.esperar() para saber si se entregó."""
        self._revisar_panico()
        if len(datos) > self.mtu:
            raise ValueError(f"Máximo {self.mtu} bytes por mensaje (mtu)")
        destino = bytes(destino)
        if len(destino) != 4:
            raise ValueError("El destino debe ser una MAC de 4 bytes")
        if not 1 <= protocolo <= 0xFFFF:
            raise ValueError("El protocolo debe estar entre 0x0001 y 0xFFFF")
        envio = Envio(datos, destino, protocolo)
        self._cola_salida.put(envio)
        return envio

    def recibir(self, timeout=None, protocolo=None):
        """Espera un mensaje completo. Devuelve Mensaje(origen, protocolo, datos)
        o None si pasó el timeout. Con `protocolo`, solo devuelve mensajes de
        ese protocolo (los demás quedan esperando a quien los pida)."""
        fin = None if timeout is None else time.time() + timeout
        with self._hay_recibidos:
            while True:
                self._revisar_panico()
                for m in self._recibidos:
                    if protocolo is None or m.protocolo == protocolo:
                        self._recibidos.remove(m)
                        copia = bytes(m.datos)
                        _borrar(m.datos)
                        return Mensaje(m.origen, m.protocolo, copia)
                espera = 0.2 if fin is None else min(0.2, fin - time.time())
                if espera <= 0:
                    return None
                self._hay_recibidos.wait(espera)

    def al_recibir(self, protocolo, funcion):
        """Entrega los mensajes de `protocolo` llamando funcion(Mensaje) en vez
        de guardarlos para recibir(). funcion=None quita el registro.
        La función corre en el hilo del receptor: debe ser rápida."""
        if funcion is None:
            self._oyentes.pop(protocolo, None)
        else:
            self._oyentes[protocolo] = funcion

    def enviar_trama_externa(self, datos_trama):
        """Envía una trama COMPLETA creada fuera del Faro (por ejemplo con
        Scapy), tal cual, sin ACK. Antes revisa que sea válida (inicio, versión,
        longitud, CRC, máximo 128 bytes); si no, lanza ValueError."""
        self._revisar_panico()
        trama.leer_trama(datos_trama)
        envio = Envio(b"", DIFUSION, tramas=[bytes(datos_trama)])
        self._cola_salida.put(envio)
        return envio

    def enviar_tramas(self, tramas_armadas):
        """Envía varias tramas ya armadas, sin ACK (mediciones)."""
        self._revisar_panico()
        envio = Envio(b"", DIFUSION, tramas=list(tramas_armadas))
        self._cola_salida.put(envio)
        return envio

    def anunciar(self):
        """Avisa a todos los Faros cercanos cuál es la MAC de este Faro."""
        self._revisar_panico()
        self._ultimo_anuncio = time.time()
        envio = Envio(b"", DIFUSION, tramas=[trama.armar_anuncio(self.mac)])
        self._cola_salida.put(envio)
        return envio

    def rotar_mac(self, anunciar=True):
        """Cambia la MAC por una nueva al azar, la guarda y (opcional) la anuncia.
        Avisa a las funciones registradas con al_cambiar_mac(). Cuándo rotarla
        y cómo informarlo al directorio de nodos lo decide la mesh."""
        self._revisar_panico()
        vieja, nueva = self.mac, _mac_nueva()
        _guardar_mac(self.archivo_mac, nueva)
        self.mac = nueva
        self._macs_propias.add(nueva)
        # Los vecinos tardan en enterarse de la MAC nueva (el aviso cruza la
        # luz y se puede perder): por un rato se siguen aceptando y
        # confirmando las tramas que llegan a la MAC vieja.
        self._macs_viejas[vieja] = time.time()
        if anunciar:
            self.anunciar()
        for funcion in list(self._al_cambiar_mac):
            funcion(vieja, nueva)
        return nueva

    def _es_mac_vieja(self, mac):
        hora = self._macs_viejas.get(mac)
        if hora is None:
            return False
        if time.time() - hora > MAC_VIEJA_S:
            del self._macs_viejas[mac]
            return False
        return True

    def al_cambiar_mac(self, funcion):
        """Registra funcion(mac_vieja, mac_nueva), llamada al rotar la MAC."""
        self._al_cambiar_mac.append(funcion)

    def vecinos_activos(self, segundos=VECINO_ACTIVO_S):
        """MACs de los Faros escuchados en los últimos `segundos`."""
        ahora = time.time()
        return [mac for mac, hora in self.vecinos.items() if ahora - hora <= segundos]

    def registrar_borrado(self, funcion):
        """Registra una función que se llama al presionar el botón de pánico
        (por ejemplo, para que la capa de privacidad borre sus llaves)."""
        self._borradores.append(funcion)

    def registrar_sensible(self, datos):
        """Registra un bytearray que se sobrescribe con ceros en el pánico."""
        self._sensibles.append(datos)
        return datos

    def canal_libre(self):
        """True si el receptor no vio luz en los últimos (pausa + CANAL_LIBRE_T)·T.
        Debe ser más largo que la pausa entre bytes; si no, el silencio entre
        dos bytes de una trama ajena parecería un canal libre.
        Además, si el receptor está a mitad de una trama (ya vio su inicio
        AA 7E y espera el resto), el canal sigue ocupado aunque entre dos
        bytes haya un silencio más largo de lo normal."""
        if self._desarmador.en_medio and time.time() - self._ultimo_byte < TRAMA_INCOMPLETA_S:
            return False
        return time.time() - self._ultima_luz > (self.pausa + CANAL_LIBRE_T) * self.T / 1e6

    def activar_panico(self):
        """Lo mismo que presionar el botón de pánico (para pruebas)."""
        if self.panico_activado:
            return
        self.panico_activado = True
        self._activo = False
        try:
            self._cortar_transmision()
        except Exception:
            pass
        self._borrar_todo()
        borrar_mac(self.archivo_mac)
        for funcion in list(self._borradores):      # llaves de otras capas
            try:
                funcion()
            except Exception:
                pass
        self._indicar_panico()                      # indicación visual
        with self._hay_recibidos:
            self._hay_recibidos.notify_all()
        if self.al_panico:
            self.al_panico()

    def cerrar(self):
        self._activo = False
        for h in self._hilos:
            if h is not threading.current_thread():
                h.join(timeout=2)
        try:
            self._cerrar_medio()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cerrar()

    # ------------------------------------------------------------ lo que cambia según el medio
    def _transmitir_bytes(self, datos):
        raise NotImplementedError

    def _cortar_transmision(self):
        pass

    def _led(self, cual, encendido):
        pass

    def _indicar_panico(self):
        pass

    def _cerrar_medio(self):
        pass

    # ------------------------------------------------------------ pánico
    def _borrar_todo(self):
        _borrar(self._desarmador.buffer)
        self._desarmador.ultima_mala = None
        while True:
            try:
                item = self._cola_salida.get_nowait()
            except queue.Empty:
                break
            _borrar(item.datos)
        with self._hay_recibidos:
            for m in self._recibidos:
                _borrar(m.datos)
            self._recibidos.clear()
        for parcial in list(self._parciales.values()):
            for datos in parcial["partes"].values():
                _borrar(datos)
        for datos in self._sensibles:
            _borrar(datos)
        self._parciales.clear()
        self._entregados.clear()
        self.vecinos.clear()
        self.mac = bytes(4)
        self._macs_propias.clear()
        self._macs_viejas.clear()

    def _revisar_panico(self):
        if self.panico_activado:
            raise Panico()

    # ------------------------------------------------------------ recepción
    def _vigilar(self, ahora):
        """Tareas periódicas del receptor: LED RX, tramas cortadas, limpieza."""
        self._led("rx", ahora - self._ultima_luz < 0.2)
        limite = (self.pausa + TRAMA_INCOMPLETA_T) * self.T / 1e6
        silencio = ahora - self._ultima_luz > limite
        if self._desarmador.en_medio and (
                (self._cortar_por_silencio and silencio and ahora - self._ultimo_byte > limite)
                or ahora - self._ultimo_byte > TRAMA_INCOMPLETA_S):
            evento = self._desarmador.expirar()
            if evento:
                self._avisar_mala()
                self._descartar(evento[1])
        self._limpiar_viejos(ahora)

    def _procesar_byte(self, byte):
        self._ultimo_byte = time.time()
        evento = self._desarmador.agregar(byte)
        if evento is None:
            return
        if evento[0] == "error":
            self._avisar_mala()
            self._descartar(evento[1])
        else:
            self._procesar(evento[1])

    def _avisar_mala(self):
        """Pasa la trama dañada al observador (mediciones de BER)."""
        mala, self._desarmador.ultima_mala = self._desarmador.ultima_mala, None
        if mala is not None and self.observador:
            self.observador(mala, False)

    def _descartar(self, motivo):
        self.estadisticas["tramas_descartadas"] += 1
        if self.depurar:
            print(f"[descartada: {motivo}]")

    def _procesar(self, t):
        if t.origen in self._macs_propias:          # mi propia luz reflejada
            self.estadisticas["ecos"] += 1
            return
        if t.destino != self.mac and t.destino != DIFUSION and not self._es_mac_vieja(t.destino):
            return                                  # no es para mí
        if t.version < 2:
            self._descartar(f"versión {t.version}: no tiene número de mensaje")
            return
        self.estadisticas["tramas_ok"] += 1
        nuevo = t.origen not in self.vecinos
        self.vecinos[t.origen] = time.time()
        if nuevo:
            self.anunciar()                         # un Faro nuevo: que también me conozca
        if self.observador:
            self.observador(t, True)
        if self.depurar:
            print(f"[trama OK: v{t.version}, tipo {t.tipo}, de {trama.mac_a_texto(t.origen)}, "
                  f"msg {t.n_msg}, parte {t.fragmento + 1}/{t.total}, "
                  f"protocolo {trama.protocolo_a_texto(t.protocolo)}, {len(t.datos)} B]")

        if t.tipo == trama.TIPO_ACK:
            evento = self._esperando_ack.get((t.origen, t.n_msg, t.fragmento))
            if evento is not None:
                self.estadisticas["acks_recibidos"] += 1
                evento.set()
            else:
                self.estadisticas["acks_tardios"] += 1
            if self.depurar:
                print(f"[ACK de la parte {t.fragmento + 1}/{t.total} "
                      f"{'a tiempo' if evento is not None else 'TARDÍO (ya no se esperaba)'}, "
                      f"{time.time() - self._fin_tx:.2f} s después de terminar de transmitir]")
            return
        if t.tipo != trama.TIPO_DATOS:
            return                                  # anuncio o medición

        clave = (t.origen, t.n_msg)
        if t.destino != DIFUSION:                   # a mí directamente: confirmar
            # el ACK sale con la MAC a la que vino la trama (por si era la vieja)
            self._cola_acks.put((t.destino, t.origen, t.n_msg, t.fragmento, t.total))
        if clave in self._entregados:               # reenvío de algo ya entregado
            self.estadisticas["duplicados"] += 1
            return
        parcial = self._parciales.get(clave)
        if (parcial is None or parcial["total"] != t.total
                or parcial["protocolo"] != t.protocolo):
            parcial = self._parciales[clave] = {"total": t.total, "partes": {},
                                                "protocolo": t.protocolo,
                                                "hora": time.time()}
        if t.fragmento in parcial["partes"]:
            self.estadisticas["duplicados"] += 1
            return
        parcial["partes"][t.fragmento] = bytearray(t.datos)
        parcial["hora"] = time.time()               # el plazo cuenta desde el último fragmento
        if len(parcial["partes"]) == t.total:       # ¡llegaron todas las partes!
            completo = bytearray()
            for i in range(t.total):
                completo.extend(parcial["partes"][i])
                _borrar(parcial["partes"][i])
            del self._parciales[clave]
            self._entregados[clave] = time.time()
            self.estadisticas["mensajes_entregados"] += 1
            self._entregar(Mensaje(t.origen, t.protocolo, completo))

    def _entregar(self, m):
        oyente = self._oyentes.get(m.protocolo)
        if oyente is not None:
            copia = Mensaje(m.origen, m.protocolo, bytes(m.datos))
            _borrar(m.datos)
            try:
                oyente(copia)
            except Exception as e:                  # un error arriba no tumba la Capa 1
                if self.depurar:
                    print(f"[error en al_recibir: {e}]")
            return
        with self._hay_recibidos:
            self._recibidos.append(m)
            while len(self._recibidos) > MAX_SIN_LEER:
                _borrar(self._recibidos.popleft().datos)
            self._hay_recibidos.notify_all()

    def _limpiar_viejos(self, ahora):
        for clave, parcial in list(self._parciales.items()):
            if ahora - parcial["hora"] > MENSAJE_INCOMPLETO_S:
                for datos in parcial["partes"].values():
                    _borrar(datos)
                del self._parciales[clave]
                self.estadisticas["mensajes_incompletos"] += 1
        for clave, hora in list(self._entregados.items()):
            if ahora - hora > RECORDAR_S:
                del self._entregados[clave]

    # ------------------------------------------------------------ envío
    def _hilo_emisor(self):
        while self._activo:
            envio = None
            try:
                self._enviar_acks()
                if (time.time() - self._ultimo_anuncio > ANUNCIO_CADA_S
                        and self._cola_salida.empty()):
                    self.anunciar()                 # anuncio periódico de la MAC
                try:
                    envio = self._cola_salida.get(timeout=0.02)
                except queue.Empty:
                    continue
                envio.exito = self._transmitir_envio(envio)
                if envio.exito is False:
                    self.estadisticas["mensajes_fallidos"] += 1
            except Panico:
                if envio is not None:
                    envio.exito = False
                return
            except Exception as e:                  # nunca dejar el hilo muerto
                if envio is not None:
                    envio.exito = False
                if self.depurar:
                    print(f"[error al transmitir: {e}]")
            finally:
                if envio is not None:
                    _borrar(envio.datos)
                    envio.listo.set()

    def _siguiente_n_msg(self):
        self._n_msg = (self._n_msg + 1) % 256
        return self._n_msg

    def _transmitir_envio(self, envio):
        if envio.tramas is not None:                # anuncios, externas, mediciones
            for t in envio.tramas:
                self._esperar_turno(0)
                self._transmitir(t)
            return True
        difusion = envio.destino == DIFUSION
        n_msg = self._siguiente_n_msg()
        tramas = trama.fragmentar_v3(bytes(envio.datos), self.mac, envio.destino,
                                     n_msg, envio.protocolo, self.datos_por_trama)
        largo_ack = len(trama.armar_ack(self.mac, envio.destino, 0, 0, 1))
        for i, t in enumerate(tramas):
            if difusion:                            # a todos: no hay ACK
                self._esperar_turno(0)
                self._transmitir(t)
                continue
            clave = (envio.destino, n_msg, i)
            confirmado = threading.Event()
            self._esperando_ack[clave] = confirmado
            try:
                for intento in range(MAX_INTENTOS):
                    envio.intentos += 1
                    if intento > 0:
                        self.estadisticas["reenvios"] += 1
                    self._esperar_turno(intento)
                    self._transmitir(t)
                    if self._esperar_ack(confirmado, largo_ack):
                        break
                    if self.depurar:
                        print(f"[sin ACK de la parte {i + 1}/{len(tramas)} "
                              f"(intento {intento + 1} de {MAX_INTENTOS})]")
                else:
                    return False                    # se rindió: el mensaje no llegó
            finally:
                del self._esperando_ack[clave]
        return True

    def _duracion_bytes(self, n):
        """Tiempo máximo (s) para transmitir n bytes, peor caso (todos los bits 1)."""
        por_byte = (CABECERA_T + PAUSA_CABECERA_T + 8 * (1 + PAUSA_BIT1_T) + 1
                    + self.pausa) * self.T / 1e6 + 0.005
        return n * por_byte

    def _esperar_ack(self, confirmado, largo_ack):
        """Espera el ACK: lo que tarda el otro en enviarlo (calculado con lo
        que de verdad tarda un byte en esta Pi, que puede ser más que la
        teoría), más un margen para que lo procese."""
        por_byte = self._duracion_bytes(1)
        if self._s_por_byte:
            por_byte = max(por_byte, 1.3 * self._s_por_byte)
        limite = time.time() + largo_ack * por_byte + 0.8
        while time.time() < limite:
            self._revisar_panico()
            if confirmado.wait(0.01):
                return True
            self._enviar_acks()                     # no dejar de confirmar a otros
        return False

    def _esperar_turno(self, intento):
        """CSMA/CA: esperar canal libre + un número aleatorio de ranuras."""
        ventana = min(VENTANA_MIN * (2 ** intento), VENTANA_MAX)
        # dejar un silencio claro después de la trama anterior, para que el
        # receptor note dónde terminó si a esa trama le faltaron bytes
        while time.time() - self._fin_tx < (self.pausa + ENTRE_TRAMAS_T) * self.T / 1e6:
            self._revisar_panico()
            self._enviar_acks()
            time.sleep(0.002)
        ranura = RANURA_T * self.T / 1e6
        while True:
            self._revisar_panico()
            self._enviar_acks()
            while not self.canal_libre():
                self._revisar_panico()
                self._enviar_acks()
                time.sleep(0.002)
            ranuras = random.randrange(ventana)
            ocupado = False
            for _ in range(ranuras):
                time.sleep(ranura)
                if self._enviar_acks() or not self.canal_libre():
                    ocupado = True                   # alguien habló: volver a esperar
                    break
            if not ocupado and self.canal_libre():
                return

    def _enviar_acks(self):
        """Envía las confirmaciones pendientes (tienen prioridad, sin espera
        aleatoria: el que mandó la trama está esperando). Devuelve True si
        envió alguna."""
        envio_alguna = False
        while True:
            try:
                propia, origen, n_msg, fragmento, total = self._cola_acks.get_nowait()
            except queue.Empty:
                return envio_alguna
            envio_alguna = True
            self._transmitir(trama.armar_ack(propia, origen, n_msg, fragmento, total))
            self.estadisticas["acks_enviados"] += 1

    def _transmitir(self, t):
        """Envía una trama por el medio, con el LED TX encendido."""
        datos = bytearray(t)
        self._led("tx", True)
        inicio = time.time()
        try:
            self._transmitir_bytes(datos)
            self.estadisticas["tramas_enviadas"] += 1
            if len(datos) >= 10:                # medir cuánto tarda de verdad un byte
                medido = (time.time() - inicio) / len(datos)
                self._s_por_byte = (medido if self._s_por_byte is None
                                    else 0.7 * self._s_por_byte + 0.3 * medido)
                if self.depurar:
                    print(f"[trama enviada: {len(datos)} bytes en {time.time() - inicio:.2f} s, "
                          f"{medido * 1000:.1f} ms por byte]")
        finally:
            _borrar(datos)
            # que el LED se vea al menos 0.2 s, aunque la trama sea corta
            if not self.panico_activado:
                time.sleep(max(0.0, 0.2 - (time.time() - inicio)))
            self._led("tx", False)
            self._fin_tx = time.time()


# ---- El Faro real: Raspberry Pi + KY-005 + KY-022 ----------------------------
class DispositivoLuzAdaptador(_FaroBase):
    """Faro real: transmite por el LED infrarrojo y recibe por el KY-022,
    usando pigpio. Ver el contrato al inicio del archivo."""

    def __init__(self, pin_emisor=38, pin_receptor=40, led_tx=13, led_rx=15,
                 boton=11, led_panico=16, t_us=600, frecuencia=38000, pausa=12,
                 archivo_mac=ARCHIVO_MAC, activo_alto=False, al_panico=None,
                 depurar=False, pi=None, ciclo=33, datos_por_trama=trama.MAX_DATOS_V3):
        if pigpio is None:
            raise RuntimeError("Falta pigpio: en la Raspberry Pi instálelo con "
                               "'sudo apt install pigpio python3-pigpio'; en una laptop "
                               "use DispositivoLuzSimulado")
        for nombre, pin in (("del emisor", pin_emisor), ("del receptor", pin_receptor),
                            ("del LED TX", led_tx), ("del LED RX", led_rx),
                            ("del botón", boton), ("del LED de pánico", led_panico)):
            if pin not in FISICO_A_GPIO:
                raise ValueError(f"El pin físico {pin} {nombre} no es un GPIO")
        if not 1 <= ciclo <= 50:
            raise ValueError("El ciclo de la portadora debe estar entre 1 y 50 %")
        super().__init__(t_us, pausa, archivo_mac, al_panico, depurar, datos_por_trama)
        self.frecuencia = frecuencia
        self.ciclo = ciclo                          # % encendido: potencia de la luz
        self._nivel_ir = 1 if activo_alto else 0     # KY-022: 0 = recibiendo luz
        self._g_tx = FISICO_A_GPIO[pin_emisor]
        self._g_rx = FISICO_A_GPIO[pin_receptor]
        self._g_leds = {"tx": FISICO_A_GPIO[led_tx], "rx": FISICO_A_GPIO[led_rx]}
        self._g_boton = FISICO_A_GPIO[boton]
        self._g_led_panico = FISICO_A_GPIO[led_panico]
        self._estado_leds = {"tx": None, "rx": None}
        self._candado = threading.Lock()
        self._flancos = []
        self._tick_fin_byte = None           # hora (tick) del final del último byte leído
        self._cortar_por_silencio = False    # se hace con los ticks (ver _hilo_receptor)

        self.pi = pi if pi is not None else pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("No se pudo conectar con pigpio. Ejecute: sudo pigpiod")
        p = self.pi
        p.set_mode(self._g_tx, pigpio.OUTPUT)
        p.write(self._g_tx, 0)
        p.wave_clear()
        p.set_mode(self._g_rx, pigpio.INPUT)
        for g in (self._g_leds["tx"], self._g_leds["rx"], self._g_led_panico):
            p.set_mode(g, pigpio.OUTPUT)
            p.write(g, 0)
        p.set_mode(self._g_boton, pigpio.INPUT)
        p.set_pull_up_down(self._g_boton, pigpio.PUD_UP)
        p.set_glitch_filter(self._g_boton, 10000)        # ignora rebotes < 10 ms
        self._cb_rx = p.callback(self._g_rx, pigpio.EITHER_EDGE, self._al_cambiar_luz)
        self._cb_boton = p.callback(self._g_boton, pigpio.FALLING_EDGE,
                                    lambda *_: self.activar_panico())
        self._arrancar_hilos(self._hilo_receptor)

    # --- medio ---
    def _led(self, cual, encendido):
        if self._estado_leds[cual] != encendido:
            self.pi.write(self._g_leds[cual], 1 if encendido else 0)
            self._estado_leds[cual] = encendido

    def _indicar_panico(self):
        self.pi.write(self._g_led_panico, 1)     # LED rojo fijo
        with self._candado:
            self._flancos.clear()

    def _cortar_transmision(self):
        self.pi.wave_tx_stop()
        self.pi.write(self._g_tx, 0)

    def _cerrar_medio(self):
        p = self.pi
        self._cb_rx.cancel()
        self._cb_boton.cancel()
        p.set_glitch_filter(self._g_boton, 0)
        p.wave_tx_stop()
        p.wave_clear()
        for g in (self._g_tx, self._g_leds["tx"], self._g_leds["rx"]):
            p.write(g, 0)
        p.stop()

    def _crear_onda(self, byte):
        self.pi.wave_add_generic(pulsos_de_byte(self._g_tx, byte, self.T,
                                                self.frecuencia, self.pausa, self.ciclo))
        wid = self.pi.wave_create()
        if wid < 0:
            raise RuntimeError("pigpio no pudo crear la onda")
        return wid

    def _esperar_onda(self, hasta_que):
        """Espera (revisando el pánico) hasta que hasta_que() sea verdadero."""
        while not hasta_que():
            if self.panico_activado:
                self.pi.wave_tx_stop()
                raise Panico()
            time.sleep(0.001)

    def _transmitir_bytes(self, datos):
        """Envía los bytes por la luz, uno por uno, como ondas de pigpio: se
        arma la onda del byte, se envía, se espera a que termine y se borra.
        (Siempre hay UNA sola onda en pigpio; con dos a la vez pigpio se
        quedaba sin espacio a mitad de las tramas largas.)"""
        p = self.pi
        for byte in datos:
            self._revisar_panico()
            wid = self._crear_onda(byte)
            try:
                p.wave_send_once(wid)
                self._esperar_onda(lambda: not p.wave_tx_busy())
            finally:
                p.wave_delete(wid)

    # --- recepción ---
    def _al_cambiar_luz(self, _gpio, nivel, tick):
        if nivel == pigpio.TIMEOUT:
            return
        self._ultima_luz = time.time()
        with self._candado:
            self._flancos.append((nivel, tick))

    def _hilo_receptor(self):
        T = self.T
        while self._activo:
            time.sleep(0.002)
            try:
                self._vigilar(time.time())
                with self._candado:
                    if not self._flancos:
                        continue
                    ultimo_nivel, ultimo_tick = self._flancos[-1]
                    if ultimo_nivel == self._nivel_ir:
                        continue                # todavía hay ráfaga
                    if pigpio.tickDiff(ultimo_tick, self.pi.get_current_tick()) < FIN_BYTE_T * T:
                        continue                # el byte no ha terminado
                    lista, self._flancos[:] = list(self._flancos), []
                    # Si la Pi estuvo ocupada, los avisos de flancos pueden llegar
                    # tarde: el último byte puede estar incompleto todavía. Sus
                    # flancos se dejan esperando el resto (hasta ESPERA_BYTE_S).
                    inicio = self._inicio_ultimo_byte(lista)
                    if (len(lista) - inicio < FLANCOS_POR_BYTE
                            and time.time() - self._ultima_luz < ESPERA_BYTE_S):
                        self._flancos[:0] = lista[inicio:]
                        lista = lista[:inicio]
                silencio_trama = (self.pausa + TRAMA_INCOMPLETA_T) * T
                for d, tick_inicio, tick_fin in self._separar_bytes(lista):
                    # Silencio largo (según la hora exacta de los flancos) a mitad
                    # de una trama: esa trama se cortó y este byte es de otra.
                    if (self._tick_fin_byte is not None and self._desarmador.en_medio
                            and pigpio.tickDiff(self._tick_fin_byte, tick_inicio) > silencio_trama):
                        evento = self._desarmador.expirar()
                        if evento:
                            self._avisar_mala()
                            self._descartar(evento[1])
                    self._tick_fin_byte = tick_fin
                    byte = decodificar(d, T)
                    if byte is None:
                        self.estadisticas["bytes_ilegibles"] += 1
                    else:
                        self._procesar_byte(byte)
            except Exception as e:              # nunca dejar el hilo muerto
                if self.depurar:
                    print(f"[error en el receptor: {e}]")

    def _inicio_ultimo_byte(self, lista):
        """Índice del primer flanco del último byte de la lista (después del
        último silencio de más de 8T)."""
        limite = FIN_BYTE_T * self.T
        for j in range(len(lista) - 2, -1, -1):
            if (lista[j][0] != self._nivel_ir
                    and pigpio.tickDiff(lista[j][1], lista[j + 1][1]) > limite):
                return j + 1
        return 0

    def _separar_bytes(self, lista):
        """Corta la lista de flancos en bytes: cada silencio de más de 8T
        separa un byte del siguiente (por si llegaron varios juntos).
        Devuelve, por byte: (duraciones [ráfaga, pausa, ráfaga, ...],
        tick del primer flanco, tick del último flanco)."""
        limite = FIN_BYTE_T * self.T
        bytes_d, actual, inicio = [], [], None
        for i in range(len(lista) - 1):
            nivel, tick = lista[i]
            dur = pigpio.tickDiff(tick, lista[i + 1][1])
            if not actual and nivel != self._nivel_ir:
                continue                    # flanco suelto antes de una ráfaga
            if not actual:
                inicio = tick
            if nivel != self._nivel_ir and dur > limite:
                bytes_d.append((actual, inicio, tick))   # silencio largo: terminó el byte
                actual = []
                continue
            actual.append(dur)
        if actual:
            bytes_d.append((actual, inicio, lista[-1][1]))
        return bytes_d


# ---- El Faro simulado: UDP multicast (para desarrollar sin Raspberry Pi) ------
class DispositivoLuzSimulado(_FaroBase):
    """Faro falso con EXACTAMENTE la misma interfaz que DispositivoLuzAdaptador,
    pero que transmite las tramas (las mismas, versión 3) por UDP multicast.
    Sirve para programar y probar la mesh en una laptop: varios programas en
    la misma computadora, o varias computadoras de la misma red, se ven entre
    sí como Faros que comparten la luz.

        faro = DispositivoLuzSimulado()                     # rápido
        faro = DispositivoLuzSimulado(velocidad=25, perdida=0.05)
                                    # ~25 bytes/s y 5 % de tramas perdidas,
                                    # parecido al Faro real

    Diferencias con el Faro real: no hay pines, LEDs ni botón (use
    activar_panico()); dos tramas a la vez no chocan. Por defecto la MAC no se
    guarda (archivo_mac=None); con `nombre` se guarda en ~/.faronet/sim-<nombre>."""

    GRUPO = "239.255.70.78"
    PUERTO = 47078
    PORTADORA = b"\x00"                  # "hay luz" mientras dura una transmisión lenta

    def __init__(self, nombre=None, grupo=GRUPO, puerto=PUERTO, velocidad=None,
                 perdida=0.0, solo_esta_computadora=False, t_us=600, pausa=12,
                 archivo_mac=None, al_panico=None, depurar=False):
        if nombre and archivo_mac is None:
            archivo_mac = os.path.expanduser(f"~/.faronet/sim-{nombre}")
        super().__init__(t_us, pausa, archivo_mac, al_panico, depurar)
        self.velocidad = velocidad
        self.perdida = perdida
        self._destino_udp = (grupo, puerto)

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", puerto))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(grupo), socket.inet_aton("0.0.0.0")))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                     0 if solo_esta_computadora else 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.settimeout(0.05)
        self._sock = s
        self._arrancar_hilos(self._hilo_receptor)

    def _transmitir_bytes(self, datos):
        if self.velocidad:                  # simular lo que tarda la luz
            fin = time.time() + len(datos) / self.velocidad
            while time.time() < fin:
                self._revisar_panico()
                self._sock.sendto(self.PORTADORA, self._destino_udp)
                time.sleep(min(0.01, max(0.0, fin - time.time())))
        self._revisar_panico()
        self._sock.sendto(bytes(datos), self._destino_udp)

    def _cerrar_medio(self):
        self._sock.close()

    def _hilo_receptor(self):
        while self._activo:
            try:
                self._vigilar(time.time())
                try:
                    datos, _ = self._sock.recvfrom(2048)
                except socket.timeout:
                    continue
                self._ultima_luz = time.time()
                if datos == self.PORTADORA:
                    continue
                if self.perdida and random.random() < self.perdida:
                    continue                # trama perdida en el "aire"
                for byte in datos:
                    self._procesar_byte(byte)
            except OSError:
                if self._activo:
                    time.sleep(0.05)
            except Exception as e:          # nunca dejar el hilo muerto
                if self.depurar:
                    print(f"[error en el receptor simulado: {e}]")
