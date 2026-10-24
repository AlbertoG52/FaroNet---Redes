"""
detector_luz.py - Muestra en la terminal si el sensor recibe luz o no.

Uso:
    python3 detector_luz.py                 # sensor en el pin físico 38
    python3 detector_luz.py --pin 7
    python3 detector_luz.py --invertir      # si su circuito da 0 con luz
                                            # (por ejemplo, con el 2N2222)
    python3 detector_luz.py --intervalo 0.1 # leer más seguido

Ctrl+C para salir.
"""
import argparse
import time

import RPi.GPIO as GPIO


def main():
    p = argparse.ArgumentParser(description="Detector de luz (prueba de receptor)")
    p.add_argument("--pin", type=int, default=38, help="pin físico del sensor")
    p.add_argument("--invertir", action="store_true",
                   help="usar si el sensor da 0 cuando hay luz")
    p.add_argument("--intervalo", type=float, default=0.25,
                   help="segundos entre cada lectura")
    a = p.parse_args()

    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(a.pin, GPIO.IN)

    print(f"Leyendo el sensor en el pin {a.pin} "
          f"{'(invertido) ' if a.invertir else ''}- Ctrl+C para salir\n")

    inicio = time.time()
    anterior = None
    cambios = 0
    try:
        while True:
            valor = GPIO.input(a.pin)          # 1 o 0, lo que llega al pin
            hay_luz = (valor == 0) if a.invertir else (valor == 1)
            t = time.time() - inicio

            estado = "LUZ    ☀" if hay_luz else "oscuro ·"
            aviso = ""
            if anterior is not None and hay_luz != anterior:
                cambios += 1
                aviso = "   <-- cambió"
            print(f"{t:7.2f} s | pin = {valor} | {estado}{aviso}")

            anterior = hay_luz
            time.sleep(a.intervalo)
    except KeyboardInterrupt:
        print(f"\nCambios detectados: {cambios}")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()