"""
faro_scapy.py - La trama del Faro (versión 3) como capa de Scapy.

Permite ARMAR tramas del Faro fuera del Faro y LEER las que llegan, igual
que Scapy hace con Ethernet o IP. El campo "protocolo" dice qué capa va
adentro, así Scapy la reconoce sola:

    from faro_scapy import FaroTrama, crc_correcto
    from scapy.all import IP, UDP, Raw

    t = FaroTrama(destino="3F:A1:07:C2", origen="01:02:03:04") / IP(dst="10.0.0.2") / UDP() / Raw(b"hola")
    datos = bytes(t)             # trama completa: AA 7E ... CRC (máximo 128 bytes)
    faro.enviar_trama_externa(datos)

    FaroTrama(datos).show()      # leerla de vuelta; el protocolo 0x0800 se ve como IP
    crc_correcto(FaroTrama(datos))

Si no se indican, la longitud y el CRC se calculan solos.

Instalar Scapy:  sudo apt install python3-scapy   (o pip en un entorno virtual)
"""
import os
import sys

from scapy.fields import (BitEnumField, BitField, ByteField, StrFixedLenField,
                          XShortEnumField, XShortField)
from scapy.layers.inet import IP
from scapy.layers.inet6 import IPv6
from scapy.layers.l2 import Ether
from scapy.packet import Packet, bind_layers

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Trama"))
import trama  # noqa: E402

TIPOS = {trama.TIPO_DATOS: "datos", trama.TIPO_ACK: "ack",
         trama.TIPO_ANUNCIO: "anuncio", trama.TIPO_PRUEBA: "prueba"}


class MACFaroField(StrFixedLenField):
    """Dirección física de 4 bytes; acepta 'AA:BB:CC:DD', 'AABBCCDD' o bytes."""

    def __init__(self, name, default):
        super().__init__(name, default, length=4)

    def any2i(self, pkt, x):
        if isinstance(x, str):
            return trama.mac_desde_texto(x)
        return super().any2i(pkt, x)

    def i2repr(self, pkt, x):
        return trama.mac_a_texto(x) if x is not None else repr(x)


class FaroTrama(Packet):
    name = "FaroTrama"
    fields_desc = [
        XShortField("inicio", 0xAA7E),
        BitField("version", trama.VERSION_ACTUAL, 4),
        BitEnumField("tipo", trama.TIPO_DATOS, 4, TIPOS),
        ByteField("longitud", None),                 # None = se calcula
        MACFaroField("destino", trama.DIFUSION),
        MACFaroField("origen", b"\x00\x00\x00\x01"),
        ByteField("n_msg", 0),
        BitField("fragmento", 0, 4),
        BitField("total", 1, 4),
        XShortEnumField("protocolo", trama.PROTO_CHAT, trama.PROTOCOLOS),
    ]

    def post_build(self, p, pay):
        if self.longitud is None:
            p = p[:3] + bytes([len(pay) & 0xFF]) + p[4:]
        cuerpo = p[2:] + pay
        return p + pay + trama.crc16(cuerpo).to_bytes(2, "big")

    def extract_padding(self, s):
        # los `longitud` bytes son la capa de adentro; lo que sigue (el CRC)
        # queda como relleno al final
        return s[:self.longitud], s[self.longitud:]


def crc_correcto(pkt):
    """True si el CRC de la trama coincide con su contenido."""
    crudo = bytes(pkt.original) if getattr(pkt, "original", None) else bytes(pkt)
    return trama.crc16(crudo[2:-2]) == int.from_bytes(crudo[-2:], "big")


# El campo protocolo dice qué capa de Scapy va adentro (como el EtherType).
bind_layers(FaroTrama, IP, protocolo=trama.PROTO_IPV4)
bind_layers(FaroTrama, IPv6, protocolo=trama.PROTO_IPV6)
bind_layers(FaroTrama, Ether, protocolo=trama.PROTO_ETHERNET)
