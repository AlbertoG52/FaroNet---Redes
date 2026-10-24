"""
receptor_IR.py - Recibe los mensajes de emisor_IR.py con un
receptor de control remoto (KY-022 / VS1838B / TSOP), usando pigpio.

Los bytes recibidos se pasan a un Desarmador de tramas (ver trama.py):
    INICIO: detecta la marca AA 7E.
    FIN:    lee el campo de longitud y cuenta exactamente esos bytes.
    CRC:    si no coincide, la trama se descarta.
trama.py debe estar en la carpeta Trama/.

pigpio avisa cada cambio del pin con la hora exacta en microsegundos,
así que el programa no tiene que revisar el pin en un ciclo.
    pausa corta (~T)  -> bit 0
    pausa larga (~3T) -> bit 1

Antes de usarlo (una vez por cada arranque de la Pi):
    sudo pigpiod

Conexión del receptor KY-022:
    S -> pin físico elegido (por defecto, el 40)
    + -> 3.3 V (pin 1)        - -> GND

LED indicador RX (opcional):
    pin 15 -> resistencia 330 Ω -> LED amarillo -> GND
    Se enciende mientras llega señal infrarroja.

Botón de pánico (opcional, pero recomendado):
    pin 11 -> resistencia 1 kΩ -> botón -> GND
    pin 16 -> resistencia 330 Ω -> LED rojo -> GND
    Al presionarlo se detiene la recepción, se borra lo recibido de la
    memoria y el LED rojo queda encendido.

Uso:
    python3 receptor_IR.py
    python3 receptor_IR.py --pin 40 --debug
    python3 receptor_IR.py --monitor      # ver ráfagas (control de TV)

La unidad --t-us debe ser IGUAL en el emisor y en el receptor.
"""
import argparse
import sys
import threading
import time

import os

import pigpio

# trama.py está en la carpeta Trama/, al lado de esta carpeta.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Trama"))
import trama  # noqa: E402

FISICO_A_GPIO = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22,
    16: 23, 18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0,
    28: 1, 29: 5, 31: 6, 32: 12, 33: 13, 35: 19, 36: 16, 37: 26, 38: 20,
    40: 21,
}


class Panico(Exception):
    """Se lanza cuando se presiona el botón de pánico."""


class BotonPanico:
    """Botón de pánico: pin -> botón -> GND, con pull-up interno.
    Normalmente el pin lee 1; al presionar lee 0 y se activa el pánico."""

    def __init__(self, pi, pin_boton, pin_led):
        self.pi = pi
        self.boton = FISICO_A_GPIO[pin_boton]
        self.led = FISICO_A_GPIO[pin_led]
        self.activado = False
        self.sensibles = []                 # bytearrays que se borran al activar

        pi.set_mode(self.led, pigpio.OUTPUT)
        pi.write(self.led, 0)
        pi.set_mode(self.boton, pigpio.INPUT)
        pi.set_pull_up_down(self.boton, pigpio.PUD_UP)
        pi.set_glitch_filter(self.boton, 10000)     # ignora rebotes de < 10 ms
        self._cb = pi.callback(self.boton, pigpio.FALLING_EDGE, self._al_presionar)

    def registrar(self, datos):
        """Agrega un bytearray a la lista de datos que se borran en el pánico."""
        self.sensibles.append(datos)
        return datos

    def _al_presionar(self, _gpio, _nivel, _tick):
        if self.activado:
            return
        self.activado = True
        for datos in self.sensibles:        # sobrescribir con ceros
            for i in range(len(datos)):
                datos[i] = 0
            datos.clear()
        # Aquí se agregará el borrado de llaves y de la dirección física.
        self.pi.write(self.led, 1)          # indicación visual: LED rojo fijo

    def revisar(self):
        if self.activado:
            raise Panico()

    def cerrar(self):
        self._cb.cancel()
        self.pi.set_glitch_filter(self.boton, 0)


def duraciones(flancos):
    """Convierte [(nivel, tick), ...] en duraciones en µs entre flancos."""
    return [pigpio.tickDiff(flancos[i][1], flancos[i + 1][1])
            for i in range(len(flancos) - 1)]


def decodificar(d, T):
    """d = [ráfaga, pausa, ráfaga, pausa, ...] en µs. Devuelve el byte o None."""
    if len(d) < 19:
        return None
    if not (2 * T <= d[0] <= 5 * T):          # cabecera: ráfaga ~3T
        return None
    if not (1.5 * T <= d[1] <= 5 * T):        # cabecera: pausa ~3T
        return None
    byte = 0
    for i in range(8):
        p = d[3 + 2 * i]
        if p > 5 * T:
            return None
        byte = (byte << 1) | (1 if p >= 2 * T else 0)
    return byte


def main():
    p = argparse.ArgumentParser(description="Receptor infrarrojo de 38 kHz con pigpio")
    p.add_argument("--pin", type=int, default=40, help="pin físico de la salida S")
    p.add_argument("--t-us", type=int, default=600,
                   help="unidad de tiempo T en microsegundos (igual en el emisor)")
    p.add_argument("--activo-alto", action="store_true",
                   help="usar si su módulo da 1 (no 0) al recibir infrarrojo")
    p.add_argument("--debug", action="store_true",
                   help="mostrar las duraciones medidas de cada byte")
    p.add_argument("--monitor", action="store_true",
                   help="solo mostrar cada ráfaga detectada")
    p.add_argument("--mac", default="00000002",
                   help="dirección física de este Faro (8 dígitos hex)")
    p.add_argument("--led-rx", type=int, default=15, help="pin físico del LED RX")
    p.add_argument("--boton", type=int, default=11, help="pin físico del botón de pánico")
    p.add_argument("--led-panico", type=int, default=16, help="pin físico del LED de pánico")
    a = p.parse_args()

    for nombre, pin in (("del receptor", a.pin), ("del LED RX", a.led_rx),
                        ("del botón", a.boton),
                        ("del LED de pánico", a.led_panico)):
        if pin not in FISICO_A_GPIO:
            sys.exit(f"El pin físico {pin} {nombre} no es un GPIO.")
    gpio = FISICO_A_GPIO[a.pin]
    try:
        mi_mac = trama.mac_desde_texto(a.mac)
    except ValueError as e:
        sys.exit(str(e))
    T = a.t_us
    nivel_ir = 1 if a.activo_alto else 0      # KY-022: 0 = recibiendo infrarrojo

    pi = pigpio.pi()
    if not pi.connected:
        sys.exit("No se pudo conectar con pigpio. Ejecute primero: sudo pigpiod")
    pi.set_mode(gpio, pigpio.INPUT)

    flancos = []
    candado = threading.Lock()

    def al_cambiar(_gpio, nivel, tick):
        if nivel == pigpio.TIMEOUT:
            return
        with candado:
            flancos.append((nivel, tick))

    cb = pi.callback(gpio, pigpio.EITHER_EDGE, al_cambiar)
    panico = BotonPanico(pi, a.boton, a.led_panico)

    # LED RX: se enciende al llegar señal y se apaga 0.2 s después de la última
    led_rx = FISICO_A_GPIO[a.led_rx]
    pi.set_mode(led_rx, pigpio.OUTPUT)
    pi.write(led_rx, 0)
    rx = {"encendido": False, "ultima": 0.0}

    def actualizar_led_rx(hubo_senal):
        ahora = time.time()
        if hubo_senal:
            rx["ultima"] = ahora
            if not rx["encendido"]:
                pi.write(led_rx, 1)
                rx["encendido"] = True
        elif rx["encendido"] and ahora - rx["ultima"] > 0.2:
            pi.write(led_rx, 0)
            rx["encendido"] = False

    desarmador = trama.Desarmador()
    panico.registrar(desarmador.buffer)
    mensaje = panico.registrar(bytearray())     # fragmentos del mensaje actual
    tramas_ok = tramas_mal = bytes_mal = 0
    ultimo_byte = time.time()
    try:
        if a.monitor:
            print(f"Monitoreando ráfagas en el pin {a.pin} (Ctrl+C para salir)")
            inicio = None
            while True:
                time.sleep(0.01)
                panico.revisar()
                with candado:
                    lista, flancos[:] = list(flancos), []
                actualizar_led_rx(bool(lista))
                for nivel, tick in lista:
                    if nivel == nivel_ir:
                        inicio = tick
                    elif inicio is not None:
                        print(f"ráfaga de {pigpio.tickDiff(inicio, tick):6d} µs")
                        inicio = None

        print(f"Escuchando tramas en el pin {a.pin}, T = {T} µs, "
              f"MAC {trama.mac_a_texto(mi_mac)} (Ctrl+C para salir)")
        while True:
            time.sleep(0.002)
            panico.revisar()

            # si una trama quedó a medias y ya no llegan bytes, se descarta
            if desarmador.en_medio and time.time() - ultimo_byte > 0.5:
                evento = desarmador.expirar()
                if evento:
                    tramas_mal += 1
                    print(f"\n[descartada: {evento[1]}]")

            with candado:
                hubo_senal = bool(flancos)
            actualizar_led_rx(hubo_senal)
            with candado:
                if not flancos:
                    continue
                # el byte terminó si la señal volvió a reposo hace más de 8T
                ultimo_nivel, ultimo_tick = flancos[-1]
                if ultimo_nivel == nivel_ir:
                    continue
                if pigpio.tickDiff(ultimo_tick, pi.get_current_tick()) < 8 * T:
                    continue
                lista, flancos[:] = list(flancos), []

            # descartar flancos sueltos hasta el inicio de una ráfaga
            while lista and lista[0][0] != nivel_ir:
                lista.pop(0)
            d = duraciones(lista)
            byte = decodificar(d, T)
            if a.debug:
                print(f"[byte: {'??' if byte is None else f'{byte:02X}'} | "
                      f"{len(d)} tramos: {' '.join(map(str, d[:20]))} µs]")
            if byte is None:
                if d:
                    bytes_mal += 1
                continue
            ultimo_byte = time.time()

            evento = desarmador.agregar(byte)
            if evento is None:
                continue
            tipo_evento, contenido = evento
            if tipo_evento == "error":
                tramas_mal += 1
                print(f"\n[descartada: {contenido}]")
                continue

            t = contenido
            if t.destino not in (mi_mac, trama.DIFUSION):
                print(f"\n[trama para {trama.mac_a_texto(t.destino)}: no es para mí, se ignora]")
                continue
            tramas_ok += 1
            print(f"\n[trama OK: v{t.version}, tipo {t.tipo}, de {trama.mac_a_texto(t.origen)} "
                  f"para {trama.mac_a_texto(t.destino)}, {len(t.datos)} bytes, CRC correcto]")
            mensaje.extend(t.datos)
            if t.tipo == trama.TIPO_FIN:            # último fragmento: mensaje completo
                print(f"Recibido: {mensaje.decode('utf-8', errors='replace')}")
                for i in range(len(mensaje)):
                    mensaje[i] = 0
                mensaje.clear()
    except Panico:
        with candado:
            flancos.clear()
            print("\n\n*** BOTÓN DE PÁNICO ACTIVADO ***")
            print("Se detuvo la operación y se borraron los datos en memoria.")
            print(f"El LED rojo (pin {a.led_panico}) queda encendido.")
    except KeyboardInterrupt:
        print(f"\nTramas correctas: {tramas_ok} | tramas descartadas: {tramas_mal} "
              f"| bytes ilegibles: {bytes_mal}")
    finally:
        pi.write(led_rx, 0)
        panico.cerrar()
        cb.cancel()
        pi.stop()


if __name__ == "__main__":
    main()
