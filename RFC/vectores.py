"""
vectores.py - Vectores de prueba del RFC, calculados con el código real del
proyecto. formatear_rfc.py los usa para escribirlos en el RFC, y
verificar_rfc.py para comprobar que el RFC sigue coincidiendo con el código.

Todo es determinístico: las semillas, las horas y los nonces son fijos.
"""
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for carpeta in ("Trama", "Faro", "Mesh", "App", "Gateway"):
    sys.path.insert(0, os.path.join(RAIZ, carpeta))


def _hex(datos, ancho=16, sangria=6):
    """Muestra bytes en hexadecimal, 16 por línea y con su desplazamiento."""
    lineas = []
    for i in range(0, len(datos), ancho):
        trozo = " ".join(f"{b:02x}" for b in datos[i:i + ancho])
        lineas.append(" " * sangria + f"{i:04x}  {trozo}")
    return lineas


def trama_datos():
    import trama
    t = trama.armar_v3(b"hola", bytes.fromhex("0a0b0c0d"), bytes.fromhex("01020304"),
                       trama.TIPO_DATOS, n_msg=7, fragmento=0, total=1,
                       protocolo=trama.PROTO_FARONET)
    return (["      Datos \"hola\", origen 0A:0B:0C:0D, destino 01:02:03:04,",
             "      n.o de mensaje 7, fragmento 1 de 1, protocolo 0x0002:"]
            + _hex(t) + [f"      CRC-16 = 0x{int.from_bytes(t[-2:], 'big'):04x}"])


def trama_ack():
    import trama
    t = trama.armar_ack(bytes.fromhex("01020304"), bytes.fromhex("0a0b0c0d"), 7, 0, 1)
    return ["      ACK de la trama anterior (de 01:02:03:04 a 0A:0B:0C:0D):"] + _hex(t)


def byte_luz():
    import DispositivoLuzAdaptador as D
    byte = 0x41
    partes = [f"rafaga {D.CABECERA_T}T", f"pausa {D.PAUSA_CABECERA_T}T"]
    for i in range(7, -1, -1):
        bit = (byte >> i) & 1
        partes += ["rafaga 1T", f"pausa {D.PAUSA_BIT1_T if bit else D.PAUSA_BIT0_T}T"]
    partes += ["rafaga 1T", "pausa P*T"]
    lineas = ["      Byte 0x41 (01000001), de izquierda a derecha en el tiempo:"]
    for i in range(0, len(partes), 4):
        lineas.append("        " + ", ".join(partes[i:i + 4]))
    total = (D.CABECERA_T + D.PAUSA_CABECERA_T + 8 + 2 * D.PAUSA_BIT1_T
             + 6 * D.PAUSA_BIT0_T + 1)
    lineas.append(f"      Duracion: ({total} + P) * T; con T = 600 us y P = 12: "
                  f"{(total + 12) * 0.6:.1f} ms")
    return lineas


def paquete_hello():
    import paquete as P
    id_nodo = bytes.fromhex("1122334455667788")
    crudo = P.armar_hello(id_nodo, 5).a_bytes()
    util = crudo[:P.TAM_CABECERA + 11]
    return (["      HELLO del nodo 1122334455667788, intervalo 5 s (primeros 25",
             "      bytes; los 195 restantes son relleno aleatorio):"] + _hex(util))


def _identidad_fija():
    import nacl.signing
    from identidad import Identidad
    ident = Identidad.__new__(Identidad)
    ident.archivo = None
    ident._cargar(bytes(range(1, 33)))          # la semilla es 01 02 ... 20
    return ident


def entrada_directorio():
    from identidad import Entrada, FLAG_GATEWAY
    ident = _identidad_fija()
    e = Entrada(ident.id, bytes(ident.verificacion), 1793500000, 1793501800,
                FLAG_GATEWAY, "faroB").firmada_por(ident)
    crudo = e.a_bytes()
    return ([f"      Semilla 01 02 03 ... 20 (hex); id = {ident.id.hex()}",
             "      alias \"faroB\", gateway, creada 1793500000, expira 1793501800:"]
            + _hex(crudo))


def firma_gateway():
    from gateway import mensaje_firmado
    ident = _identidad_fija()
    cuerpo = b'{"tema":"redes","texto":"hola"}'
    msg = mensaje_firmado("/gw/publicacion", "1793500000", "00" * 16, cuerpo)
    firma = ident.firmar(msg)
    return (["      Llave: la de la semilla 01 02 ... 20. Ruta /gw/publicacion,",
             "      tiempo 1793500000, nonce 000...0 (32 ceros), cuerpo",
             "      {\"tema\":\"redes\",\"texto\":\"hola\"}.",
             "      Llave publica (va en GATEWAY_LLAVE; no viaja en la peticion):"]
            + _hex(bytes(ident.verificacion))
            + ["      X-FaroNet-Firma:"] + _hex(firma))


def tamanos_cebolla():
    import cebolla
    lineas = []
    for k in range(cebolla.SALTOS + 1):
        lineas.append(f"      con {k} capa(s) quitadas: {cebolla.tamano_capa(k)} bytes")
    lineas.append(f"      mensaje de la aplicacion: {cebolla.TAM_MENSAJE_APP} bytes;"
                  f" publicacion: {cebolla.TAM_PUBLICACION} bytes")
    return lineas


def etiqueta_tema():
    import mensajes as M
    clave = M.clave_de_tema("club", "frase de ejemplo")
    return ["      Tema \"club\", frase \"frase de ejemplo\":",
            "      clave    = " + clave.hex()[:32],
            "                 " + clave.hex()[32:],
            "      etiqueta = " + M.etiqueta_de_clave(clave)]


def id_buzon():
    import buzon_muerto
    id_b, _clave = buzon_muerto.derivar("frase de ejemplo")
    return ["      Frase \"frase de ejemplo\":",
            "      id = " + id_b[:32],
            "           " + id_b[32:]]


VECTORES = {
    "TRAMA_DATOS": trama_datos, "TRAMA_ACK": trama_ack, "BYTE_LUZ": byte_luz,
    "PAQUETE_HELLO": paquete_hello, "ENTRADA": entrada_directorio,
    "FIRMA_GATEWAY": firma_gateway, "TAMANOS_CEBOLLA": tamanos_cebolla,
    "ETIQUETA_TEMA": etiqueta_tema, "ID_BUZON": id_buzon,
}


def calcular(nombre):
    return VECTORES[nombre]()
