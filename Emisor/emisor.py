"""
emisor.py - Envía un mensaje por luz encendiendo y apagando un LED.

Protocolo (sin Manchester), por cada byte:
    reposo : LED apagado
    inicio : 1 bit con el LED encendido
    datos  : 8 bits, del más significativo al menos significativo
             (1 = encendido, 0 = apagado)
    parada : 1 bit con el LED apagado
Al final del mensaje se envía un salto de línea ('\\n').

Uso:
    python3 emisor.py --mensaje "hola faro"
    python3 emisor.py --mensaje "hola" --bps 50 --repetir 3
"""
import argparse
import time

import RPi.GPIO as GPIO


def esperar_hasta(instante):
    while time.perf_counter() < instante:
        pass


def enviar_byte(pin, byte, periodo):
    bits = [1] + [(byte >> i) & 1 for i in range(7, -1, -1)] + [0]
    t = time.perf_counter()
    for bit in bits:
        GPIO.output(pin, GPIO.HIGH if bit else GPIO.LOW)
        t += periodo
        esperar_hasta(t)
    # pausa de 2 bits en reposo antes del siguiente byte
    esperar_hasta(t + 2 * periodo)


def main():
    p = argparse.ArgumentParser(description="Emisor de luz FaroNet (prueba)")
    p.add_argument("--mensaje", default="hola faro", help="texto a enviar")
    p.add_argument("--bps", type=float, default=10, help="bits por segundo")
    p.add_argument("--pin", type=int, default=13, help="pin físico del LED")
    p.add_argument("--repetir", type=int, default=1, help="veces a enviar")
    a = p.parse_args()

    periodo = 1 / a.bps
    datos = (a.mensaje + "\n").encode("utf-8")

    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(a.pin, GPIO.OUT, initial=GPIO.LOW)
    try:
        time.sleep(5 * periodo)          # reposo inicial
        for n in range(a.repetir):
            print(f"[{n + 1}/{a.repetir}] Enviando {datos!r} a {a.bps:g} bps...")
            for byte in datos:
                enviar_byte(a.pin, byte, periodo)
        print("Listo.")
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.output(a.pin, GPIO.LOW)
        GPIO.cleanup()


if __name__ == "__main__":
    main()