"""
receptor.py - Recibe por luz los mensajes que envía emisor.py.

Espera el bit de inicio (luz encendida), lee los 8 bits muestreando
a la mitad de cada uno y verifica el bit de parada (luz apagada).

Uso:
    python3 receptor.py                     # 10 bps, sensor en el pin 7
    python3 receptor.py --bps 50
    python3 receptor.py --invertir          # si su sensor da 0 con luz
    python3 receptor.py --monitor           # solo ver luz / oscuro en vivo

La velocidad (--bps) debe ser la MISMA en el emisor y en el receptor.
"""
import argparse
import sys
import time

import RPi.GPIO as GPIO


def esperar_hasta(instante):
    while time.perf_counter() < instante:
        pass


def monitorear(pin, luz):
    """Muestra cada cambio entre luz y oscuro, con el tiempo transcurrido."""
    print(f"Monitoreando el pin {pin} (Ctrl+C para salir)")
    inicio = time.perf_counter()
    anterior = None
    try:
        while True:
            actual = luz()
            if actual != anterior:
                t = time.perf_counter() - inicio
                print(f"{t:8.3f} s   {'LUZ' if actual else 'oscuro'}")
                anterior = actual
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


def main():
    p = argparse.ArgumentParser(description="Receptor de luz FaroNet (prueba)")
    p.add_argument("--bps", type=float, default=10, help="bits por segundo")
    p.add_argument("--pin", type=int, default=7, help="pin físico del sensor")
    p.add_argument("--invertir", action="store_true",
                   help="usar si el sensor da 0 cuando hay luz")
    p.add_argument("--debug", action="store_true",
                   help="mostrar cada byte recibido en binario")
    p.add_argument("--monitor", action="store_true",
                   help="solo mostrar en vivo cuándo cambia entre luz y oscuro")
    a = p.parse_args()

    periodo = 1 / a.bps
    inv = 1 if a.invertir else 0

    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(a.pin, GPIO.IN)

    def luz():
        return GPIO.input(a.pin) ^ inv

    if a.monitor:
        monitorear(a.pin, luz)
        return

    print(f"Escuchando en el pin {a.pin} a {a.bps:g} bps (Ctrl+C para salir)")
    print(f"Estado actual del sensor: {'LUZ' if luz() else 'oscuro'}")
    if luz():
        print("Aviso: el sensor ya ve luz en reposo. Si no recibe nada, "
              "pruebe con --invertir o ajuste el sensor.")

    linea = bytearray()
    buenos = errores = 0
    try:
        while True:
            # 1. esperar reposo (oscuro) y luego el bit de inicio (luz)
            while luz():
                pass
            while not luz():
                pass
            t0 = time.perf_counter()

            # 2. confirmar el bit de inicio a la mitad (descarta ruido)
            esperar_hasta(t0 + 0.5 * periodo)
            if not luz():
                continue

            # 3. leer los 8 bits a la mitad de cada uno
            byte = 0
            for i in range(8):
                esperar_hasta(t0 + (1.5 + i) * periodo)
                byte = (byte << 1) | luz()

            # 4. verificar el bit de parada (debe estar oscuro)
            esperar_hasta(t0 + 9.5 * periodo)
            if luz():
                errores += 1
                if a.debug:
                    print(f"\n[error de marco, byte {byte:08b}]")
                continue

            buenos += 1
            if a.debug:
                print(f"\n[{byte:08b} = {chr(byte)!r}]", end="")

            if byte == ord("\n"):
                texto = linea.decode("utf-8", errors="replace")
                print(f"\nRecibido: {texto}")
                linea.clear()
            else:
                linea.append(byte)
                sys.stdout.write(chr(byte) if 32 <= byte < 127 else "?")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print(f"\nBytes correctos: {buenos} | errores de marco: {errores}")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()