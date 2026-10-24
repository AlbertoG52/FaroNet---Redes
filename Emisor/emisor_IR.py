"""
emisor_IR.py - Envía un mensaje por infrarrojo a un receptor de
control remoto (KY-022 / VS1838B / TSOP) usando pigpio.

El mensaje se empaqueta en TRAMAS (ver trama.py): cada trama empieza con
la marca de inicio AA 7E, lleva un campo de longitud que indica dónde
termina y un CRC para detectar errores. Los mensajes de más de 112 bytes
se dividen en varias tramas. trama.py debe estar en la carpeta Trama/.

pigpio arma cada letra como una "onda" completa (ráfagas de 38 kHz y
pausas) y la reproduce por DMA, con precisión de microsegundos, en
CUALQUIER pin GPIO. No hace falta tocar config.txt.

Protocolo, por cada byte (T = unidad de tiempo, por defecto 600 µs):
    cabecera : ráfaga de 3T + pausa de 3T
    8 bits   : ráfaga de T + pausa de T (bit 0) o de 3T (bit 1)
    fin      : ráfaga de T + pausa de 12T

Antes de usarlo (una vez por cada arranque de la Pi):
    sudo pigpiod

Conexión del emisor KY-005:
    S -> pin físico elegido (por defecto, el 38)      - -> GND

LED indicador TX (opcional):
    pin 13 -> resistencia 330 Ω -> LED verde -> GND
    Se enciende mientras se envía un mensaje.

Botón de pánico (opcional, pero recomendado):
    pin 11 -> resistencia 1 kΩ -> botón -> GND
    pin 16 -> resistencia 330 Ω -> LED rojo -> GND
    Al presionarlo se detiene el envío, se borra el mensaje de la memoria
    y el LED rojo queda encendido.

Uso:
    python3 emisor_IR.py --mensaje "hola"
    python3 emisor_IR.py --pin 38 --mensaje "hola" --repetir 5
    python3 emisor_IR.py --prueba         # portadora encendida 3 s
    python3 emisor_IR.py --mensaje "..." --pausa 40   # más descanso entre letras
"""
import argparse
import sys
import time

import os

import pigpio

# trama.py está en la carpeta Trama/, al lado de esta carpeta.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Trama"))
import trama  # noqa: E402

# Pin físico (1-40) -> número GPIO (BCM), que es el que usa pigpio
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


def rafaga(gpio, duracion_us, frecuencia):
    """Pulsos de una ráfaga de portadora (33 % del ciclo encendido)."""
    periodo = round(1e6 / frecuencia)          # 26 µs a 38 kHz
    encendido = max(1, periodo // 3)
    apagado = periodo - encendido
    ciclos = max(1, round(duracion_us / periodo))
    mascara = 1 << gpio
    pulsos = []
    for _ in range(ciclos):
        pulsos.append(pigpio.pulse(mascara, 0, encendido))
        pulsos.append(pigpio.pulse(0, mascara, apagado))
    return pulsos


def pausa(gpio, duracion_us):
    return [pigpio.pulse(0, 1 << gpio, int(duracion_us))]


def pulsos_de_byte(gpio, byte, T, frecuencia, pausa_final=12):
    """Lista de pulsos de pigpio para enviar un byte completo."""
    p = rafaga(gpio, 3 * T, frecuencia) + pausa(gpio, 3 * T)   # cabecera
    for i in range(7, -1, -1):                                  # 8 bits
        bit = (byte >> i) & 1
        p += rafaga(gpio, T, frecuencia) + pausa(gpio, 3 * T if bit else T)
    p += rafaga(gpio, T, frecuencia) + pausa(gpio, pausa_final * T)   # fin
    return p


def enviar_pulsos(pi, pulsos, panico=None):
    pi.wave_add_generic(pulsos)
    wid = pi.wave_create()
    if wid < 0:
        raise RuntimeError("pigpio no pudo crear la onda")
    pi.wave_send_once(wid)
    try:
        while pi.wave_tx_busy():
            if panico is not None and panico.activado:
                pi.wave_tx_stop()           # cortar la transmisión al instante
                raise Panico()
            time.sleep(0.001)
    finally:
        pi.wave_delete(wid)


def main():
    p = argparse.ArgumentParser(description="Emisor infrarrojo de 38 kHz con pigpio")
    p.add_argument("--mensaje", default="hola faro", help="texto a enviar")
    p.add_argument("--pin", type=int, default=38, help="pin físico del emisor")
    p.add_argument("--repetir", type=int, default=1, help="veces a enviar")
    p.add_argument("--t-us", type=int, default=600,
                   help="unidad de tiempo T en microsegundos (igual en el receptor)")
    p.add_argument("--frecuencia", type=int, default=38000,
                   help="frecuencia de la portadora en Hz")
    p.add_argument("--pausa", type=int, default=12,
                   help="silencio después de cada letra, en unidades de T "
                        "(más alto = el receptor descansa más)")
    p.add_argument("--prueba", action="store_true",
                   help="solo encender la portadora 3 segundos")
    p.add_argument("--mac", default="00000001",
                   help="dirección física de este Faro (8 dígitos hex)")
    p.add_argument("--destino", default="FFFFFFFF",
                   help="dirección física de destino (FFFFFFFF = a todos)")
    p.add_argument("--led-tx", type=int, default=13, help="pin físico del LED TX")
    p.add_argument("--boton", type=int, default=11, help="pin físico del botón de pánico")
    p.add_argument("--led-panico", type=int, default=16, help="pin físico del LED de pánico")
    a = p.parse_args()

    for nombre, pin in (("del emisor", a.pin), ("del LED TX", a.led_tx),
                        ("del botón", a.boton),
                        ("del LED de pánico", a.led_panico)):
        if pin not in FISICO_A_GPIO:
            sys.exit(f"El pin físico {pin} {nombre} no es un GPIO.")
    gpio = FISICO_A_GPIO[a.pin]
    try:
        origen = trama.mac_desde_texto(a.mac)
        destino = trama.mac_desde_texto(a.destino)
    except ValueError as e:
        sys.exit(str(e))

    pi = pigpio.pi()
    if not pi.connected:
        sys.exit("No se pudo conectar con pigpio. Ejecute primero: sudo pigpiod")

    pi.set_mode(gpio, pigpio.OUTPUT)
    pi.write(gpio, 0)
    pi.wave_clear()
    panico = BotonPanico(pi, a.boton, a.led_panico)
    led_tx = FISICO_A_GPIO[a.led_tx]
    pi.set_mode(led_tx, pigpio.OUTPUT)
    pi.write(led_tx, 0)
    try:
        if a.prueba:
            print(f"Portadora de {a.frecuencia} Hz en el pin {a.pin} durante 3 s...")
            # un solo ciclo de portadora, repetido sin parar durante 3 s
            pi.wave_add_generic(rafaga(gpio, 1, a.frecuencia))
            wid = pi.wave_create()
            pi.wave_send_repeat(wid)
            pi.write(led_tx, 1)                     # LED TX encendido
            fin = time.time() + 3
            while time.time() < fin and not panico.activado:
                time.sleep(0.01)
            pi.wave_tx_stop()
            pi.write(led_tx, 0)
            panico.revisar()
            pi.wave_delete(wid)
        else:
            datos = panico.registrar(bytearray(a.mensaje.encode("utf-8")))
            a.mensaje = None                # no dejar otra copia del texto
            tramas = [panico.registrar(bytearray(t))
                      for t in trama.fragmentar(bytes(datos), origen, destino)]
            for n in range(a.repetir):
                print(f"[{n + 1}/{a.repetir}] Enviando {len(datos)} bytes en "
                      f"{len(tramas)} trama(s), de {trama.mac_a_texto(origen)} "
                      f"para {trama.mac_a_texto(destino)} (T = {a.t_us} µs)...")
                pi.write(led_tx, 1)                 # LED TX encendido al empezar
                inicio = time.time()
                for t in tramas:
                    for i in range(len(t)):
                        panico.revisar()
                        enviar_pulsos(pi, pulsos_de_byte(gpio, t[i], a.t_us,
                                                         a.frecuencia, a.pausa), panico)
                    time.sleep(0.03)                # descanso entre tramas
                # que el LED se vea al menos 0.2 s, aunque el mensaje sea corto
                time.sleep(max(0, 0.2 - (time.time() - inicio)))
                pi.write(led_tx, 0)                 # LED TX apagado al terminar
                time.sleep(0.05)
        print("Listo.")
    except Panico:
            print("\n\n*** BOTÓN DE PÁNICO ACTIVADO ***")
            print("Se detuvo la operación y se borraron los datos en memoria.")
            print(f"El LED rojo (pin {a.led_panico}) queda encendido.")
    except KeyboardInterrupt:
        pass
    finally:
        pi.write(led_tx, 0)
        panico.cerrar()
        pi.wave_tx_stop()
        pi.wave_clear()
        pi.write(gpio, 0)
        pi.stop()


if __name__ == "__main__":
    main()
