"""
trama.py - Formato de trama de la Capa 1 de FaroNet.

El campo "versión" de cada trama dice con qué formato está escrita, así el
receptor sabe cómo leerla y descarta las versiones que no conoce. Para un
formato nuevo basta con agregar su versión a CABECERAS (abajo) y su lectura
en _leer_cuerpo(); las versiones anteriores se siguen entendiendo.

VERSIÓN 3 (la actual, la usa DispositivoLuzAdaptador):

    +-------+------+------+---------+---------+------+------+-----------+---------+-----+
    |Inicio | Ver/ | Long.| MAC     | MAC     | N.º  | Frag-| Protocolo | Datos   | CRC |
    | AA 7E | Tipo |      | destino | origen  | msg  | mento|           |         | 16  |
    +-------+------+------+---------+---------+------+------+-----------+---------+-----+
      2 B     1 B    1 B    4 B       4 B       1 B    1 B    2 B         0..110 B  2 B
                                                                     máximo total: 128 B

    Ver/Tipo  : 4 bits de versión (3) + 4 bits de tipo.
    Tipo      : 1 = datos, 3 = ACK (confirmación), 4 = anuncio (aviso de
                la MAC de este Faro), 5 = trama de medición (BER).
    Longitud  : cuántos bytes de datos trae; con esto se sabe dónde TERMINA.
    MACs      : direcciones físicas de 4 bytes (FF FF FF FF = a todos).
    N.º msg   : 0..255; cada mensaje nuevo lleva otro número.
    Fragmento : 4 bits con el número de esta parte (0..14) + 4 bits con
                el total de partes (1..15). Un mensaje puede tener hasta
                15 x 110 = 1650 bytes.
    Protocolo : qué lleva adentro (como el EtherType de Ethernet). Permite
                transportar tramas o paquetes creados fuera del Faro:
                0x0001 chat de prueba (faro.py), 0x0002 paquete FaroNet
                (mesh), 0x0003 mediciones, 0x0800 IPv4, 0x86DD IPv6,
                0x6558 trama Ethernet completa. 0x0000 = sin contenido
                (ACK y anuncio). Ver PROTOCOLOS.
    CRC       : CRC-16/CCITT de todo lo que va después del inicio; si no
                coincide, la trama llegó dañada y se descarta.

VERSIÓN 2 (anterior, se sigue leyendo): igual a la 3 pero SIN el campo de
protocolo (cabecera de 12 bytes); sus datos se entregan como protocolo
0x0001 (chat).

VERSIÓN 1 (la usan emisor_IR.py y receptor_IR.py, programas de prueba):

    Inicio | Ver/Tipo | Longitud | MAC destino | MAC origen | Datos 0..112 | CRC
    Tipo: 1 = último fragmento, 2 = vienen más fragmentos.
"""
import binascii
from dataclasses import dataclass
from typing import Optional

INICIO = bytes([0xAA, 0x7E])
MAX_TRAMA = 128
DIFUSION = bytes([0xFF] * 4)

# Versión -> (largo de la cabecera después del inicio, máximo de datos).
# La cabecera = ver/tipo + longitud + MACs (+ n.º msg + fragmento) (+ protocolo)
CABECERAS = {1: (10, 112), 2: (12, 110), 3: (14, 110)}
VERSIONES_SOPORTADAS = tuple(CABECERAS)
VERSION_ACTUAL = 3

# ---- Versión 1 (programas de prueba) ----------------------------------------
VERSION = 1             # versión de armar() y fragmentar()
TIPO_FIN = 1            # último (o único) fragmento del mensaje
TIPO_SIGUE = 2          # vienen más fragmentos
MAX_DATOS = 112
LARGO_CABECERA = 10

# ---- Versiones 2 y 3 ---------------------------------------------------------
TIPO_DATOS = 1          # un fragmento de un mensaje
TIPO_ACK = 3            # "recibí el fragmento X del mensaje N"
TIPO_ANUNCIO = 4        # "esta es mi MAC" (al arrancar, cada 30 s o al rotarla)
TIPO_PRUEBA = 5         # trama de medición: no se entrega a la aplicación
MAX_DATOS_V3 = 110
MAX_FRAGMENTOS = 15
MAX_MENSAJE = MAX_DATOS_V3 * MAX_FRAGMENTOS      # 1650 bytes (la "MTU" del Faro)

# ---- Valores del campo protocolo (versión 3) ---------------------------------
PROTO_NINGUNO = 0x0000      # ACK, anuncio: no lleva datos de aplicación
PROTO_CHAT = 0x0001         # texto UTF-8 del chat de prueba (faro.py)
PROTO_FARONET = 0x0002      # paquete de la red mesh FaroNet (capas 2/3)
PROTO_MEDICION = 0x0003     # datos de las mediciones (medicion.py)
PROTO_IPV4 = 0x0800         # paquete IPv4 (mismo número que en Ethernet)
PROTO_IPV6 = 0x86DD         # paquete IPv6
PROTO_ETHERNET = 0x6558     # trama Ethernet completa (Transparent Ethernet Bridging)
PROTOCOLOS = {
    PROTO_NINGUNO: "ninguno", PROTO_CHAT: "chat", PROTO_FARONET: "faronet",
    PROTO_MEDICION: "medicion", PROTO_IPV4: "ipv4", PROTO_IPV6: "ipv6",
    PROTO_ETHERNET: "ethernet",
}


def crc16(datos):
    """CRC-16/CCITT-FALSE (polinomio 0x1021, valor inicial 0xFFFF)."""
    return binascii.crc_hqx(bytes(datos), 0xFFFF)


def mac_desde_texto(texto):
    """'0000000A' o '00:00:00:0A' -> b'\\x00\\x00\\x00\\x0a'"""
    mac = bytes.fromhex(texto.replace(":", "").replace("-", ""))
    if len(mac) != 4:
        raise ValueError("La dirección física debe tener 4 bytes (8 dígitos hex)")
    return mac


def mac_a_texto(mac):
    return ":".join(f"{b:02X}" for b in mac)


def protocolo_desde_texto(texto):
    """'chat', 'ipv4', '0x0800' o '2048' -> número del protocolo."""
    for numero, nombre in PROTOCOLOS.items():
        if texto.lower() == nombre:
            return numero
    numero = int(texto, 0)
    if not 0 <= numero <= 0xFFFF:
        raise ValueError("El protocolo debe estar entre 0x0000 y 0xFFFF")
    return numero


def protocolo_a_texto(numero):
    nombre = PROTOCOLOS.get(numero)
    return f"0x{numero:04X} ({nombre})" if nombre else f"0x{numero:04X}"


def _cerrar(cuerpo):
    trama = INICIO + cuerpo + crc16(cuerpo).to_bytes(2, "big")
    assert len(trama) <= MAX_TRAMA
    return trama


# ---- Armar tramas versión 1 (programas de prueba) ---------------------------
def armar(datos, origen, destino=DIFUSION, tipo=TIPO_FIN, version=VERSION):
    """Construye una trama de versión 1 a partir de los datos."""
    if len(datos) > MAX_DATOS:
        raise ValueError(f"Máximo {MAX_DATOS} bytes de datos por trama")
    cuerpo = bytes([(version << 4) | tipo, len(datos)]) + destino + origen + bytes(datos)
    return _cerrar(cuerpo)


def fragmentar(mensaje, origen, destino=DIFUSION):
    """Divide un mensaje en tramas de versión 1 de hasta 112 bytes de datos."""
    trozos = [mensaje[i:i + MAX_DATOS] for i in range(0, len(mensaje), MAX_DATOS)] or [b""]
    return [armar(t, origen, destino, TIPO_FIN if i == len(trozos) - 1 else TIPO_SIGUE)
            for i, t in enumerate(trozos)]


# ---- Armar tramas versión 3 --------------------------------------------------
def armar_v3(datos, origen, destino, tipo, n_msg=0, fragmento=0, total=1,
             protocolo=PROTO_NINGUNO):
    """Construye una trama de versión 3."""
    if len(datos) > MAX_DATOS_V3:
        raise ValueError(f"Máximo {MAX_DATOS_V3} bytes de datos por trama")
    if not (1 <= total <= MAX_FRAGMENTOS and 0 <= fragmento < total):
        raise ValueError("Número de fragmento inválido")
    if not 0 <= protocolo <= 0xFFFF:
        raise ValueError("El protocolo debe estar entre 0x0000 y 0xFFFF")
    cuerpo = (bytes([(3 << 4) | tipo, len(datos)]) + bytes(destino) + bytes(origen)
              + bytes([n_msg & 0xFF, (fragmento << 4) | total])
              + protocolo.to_bytes(2, "big") + bytes(datos))
    return _cerrar(cuerpo)


def fragmentar_v3(mensaje, origen, destino, n_msg, protocolo, max_datos=MAX_DATOS_V3):
    """Divide un mensaje en tramas de datos de versión 3 (hasta max_datos
    bytes de datos cada una; 110 como máximo)."""
    if not 1 <= max_datos <= MAX_DATOS_V3:
        raise ValueError(f"max_datos debe estar entre 1 y {MAX_DATOS_V3}")
    if len(mensaje) > max_datos * MAX_FRAGMENTOS:
        raise ValueError(f"El mensaje no puede pasar de {max_datos * MAX_FRAGMENTOS} bytes")
    trozos = [mensaje[i:i + max_datos]
              for i in range(0, len(mensaje), max_datos)] or [b""]
    return [armar_v3(t, origen, destino, TIPO_DATOS, n_msg, i, len(trozos), protocolo)
            for i, t in enumerate(trozos)]


def armar_ack(origen, destino, n_msg, fragmento, total):
    """ACK: confirma que llegó bien el fragmento indicado."""
    return armar_v3(b"", origen, destino, TIPO_ACK, n_msg, fragmento, total)


def armar_anuncio(origen):
    """Anuncio a todos: 'esta es mi dirección física'."""
    return armar_v3(b"", origen, DIFUSION, TIPO_ANUNCIO)


# ---- Leer tramas (todas las versiones) --------------------------------------
@dataclass
class Trama:
    version: int
    tipo: int
    destino: bytes
    origen: bytes
    datos: bytes
    n_msg: Optional[int] = None         # versiones 2 y 3
    fragmento: Optional[int] = None     # versiones 2 y 3
    total: Optional[int] = None         # versiones 2 y 3
    protocolo: Optional[int] = None     # versión 3 (la 2 se toma como chat)


def _leer_cuerpo(cuerpo):
    """Interpreta lo que va entre el inicio y el CRC, según la versión."""
    version = cuerpo[0] >> 4
    t = Trama(version=version, tipo=cuerpo[0] & 0x0F,
              destino=bytes(cuerpo[2:6]), origen=bytes(cuerpo[6:10]), datos=b"")
    if version == 1:
        t.datos = bytes(cuerpo[10:])
        return t
    t.n_msg = cuerpo[10]
    t.fragmento, t.total = cuerpo[11] >> 4, cuerpo[11] & 0x0F
    if version == 2:
        t.protocolo = PROTO_CHAT if t.tipo == TIPO_DATOS else PROTO_NINGUNO
        t.datos = bytes(cuerpo[12:])
    else:
        t.protocolo = int.from_bytes(cuerpo[12:14], "big")
        t.datos = bytes(cuerpo[14:])
    return t


def leer_trama(trama):
    """Valida una trama completa (bytes) y la devuelve como Trama.
    Lanza ValueError si está mal formada. Sirve para tramas creadas fuera
    del Faro (por ejemplo con Scapy) antes de enviarlas."""
    trama = bytes(trama)
    if len(trama) > MAX_TRAMA:
        raise ValueError(f"La trama mide {len(trama)} bytes; el máximo es {MAX_TRAMA}")
    if trama[:2] != INICIO:
        raise ValueError("La trama no empieza con AA 7E")
    d = Desarmador()
    resultado = None
    for byte in trama:
        resultado = d.agregar(byte) or resultado
    if resultado is None:
        raise ValueError("La trama está incompleta (la longitud no coincide)")
    if resultado[0] == "error":
        raise ValueError(resultado[1])
    return resultado[1]


class Desarmador:
    """Recibe los bytes uno por uno y detecta dónde empieza y termina
    cada trama:
        - INICIO: busca el patrón AA 7E.
        - VERSIÓN: con ella sabe cuánto mide la cabecera.
        - FIN:    lee la longitud y cuenta exactamente esos bytes + CRC.
    agregar() devuelve None, ("trama", Trama) o ("error", motivo).
    Si la trama llegó dañada (CRC malo o incompleta), lo que se alcanzó a
    leer queda en self.ultima_mala (se usa para medir la tasa de error de
    bits)."""

    def __init__(self):
        self.buffer = bytearray()
        self.ultima_mala = None
        self.reiniciar()

    def reiniciar(self):
        for i in range(len(self.buffer)):      # no dejar datos en memoria
            self.buffer[i] = 0
        self.buffer.clear()
        self.estado = "BUSCAR_AA"
        self.largo_cabecera = 0
        self.total = 0

    @property
    def en_medio(self):
        return self.estado not in ("BUSCAR_AA", "BUSCAR_7E")

    def expirar(self):
        """Llamar si pasó demasiado tiempo sin bytes a mitad de una trama."""
        if self.en_medio:
            if self.largo_cabecera and len(self.buffer) >= self.largo_cabecera:
                # guardar lo que alcanzó a llegar (sirve para medir el BER)
                parte = bytes(self.buffer[:self.largo_cabecera + self.buffer[1]])
                self.ultima_mala = _leer_cuerpo(parte)
            self.reiniciar()
            return ("error", "trama incompleta (se dejaron de recibir bytes)")
        self.estado = "BUSCAR_AA"
        return None

    def agregar(self, byte):
        if self.estado == "BUSCAR_AA":
            if byte == INICIO[0]:
                self.estado = "BUSCAR_7E"
            return None

        if self.estado == "BUSCAR_7E":
            if byte == INICIO[1]:
                self.estado = "CABECERA"          # ¡empieza una trama!
            elif byte != INICIO[0]:
                self.estado = "BUSCAR_AA"
            return None

        self.buffer.append(byte)

        if self.estado == "CABECERA" and len(self.buffer) == 2:
            version, longitud = self.buffer[0] >> 4, self.buffer[1]
            if version not in CABECERAS:
                self.reiniciar()
                return ("error", f"versión {version} no soportada (se descarta)")
            self.largo_cabecera, maximo = CABECERAS[version]
            if longitud > maximo:
                self.reiniciar()
                return ("error", f"longitud {longitud} inválida")
            self.total = self.largo_cabecera + longitud + 2
            return None

        if self.estado == "CABECERA" and len(self.buffer) == self.largo_cabecera:
            if self.buffer[0] >> 4 >= 2:
                fragmento, total = self.buffer[11] >> 4, self.buffer[11] & 0x0F
                if not (1 <= total <= MAX_FRAGMENTOS and fragmento < total):
                    self.reiniciar()
                    return ("error", "número de fragmento inválido")
            self.estado = "DATOS"
            # (sin return: si la trama no trae datos, el CRC se revisa abajo)

        if self.estado == "DATOS" and len(self.buffer) == self.total:
            cuerpo, crc = bytes(self.buffer[:-2]), int.from_bytes(self.buffer[-2:], "big")
            self.reiniciar()
            if crc16(cuerpo) != crc:
                self.ultima_mala = _leer_cuerpo(cuerpo)
                return ("error", "CRC incorrecto (la trama llegó dañada)")
            return ("trama", _leer_cuerpo(cuerpo))
        return None
