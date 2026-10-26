#!/usr/bin/env python3
"""
demo_red.py - Levanta una red FaroNet COMPLETA en una sola computadora, sin
Raspberry Pi: dos comunidades unidas solo por un Faro SIMULADO.

    Comunidad 1:  a1 - a2 - a3 - faroA ~~~ luz simulada ~~~ faroB - b1 - b2  :Comunidad 2
                                                           (gateway)

    python3 demo_red.py                    # luz simulada rápida
    python3 demo_red.py --lento            # luz simulada como la real (25 B/s, 5 % pérdida)

Luego, en otras terminales (un chat por nodo):
    python3 App/chat.py --api 7101 interactivo      # a1
    python3 App/chat.py --api 7111 interactivo      # b1

Ctrl+C detiene todos los nodos. Las salidas de cada nodo quedan en demo_logs/.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))

#        alias    UDP   API   vecinos fijos           extra
RED = [("a1",    7001, 7101, [7002], []),
       ("a2",    7002, 7102, [7003], []),
       ("a3",    7003, 7103, [7004], []),
       ("faroA", 7004, 7104, [7001], ["--faro", "simulado", "--faro-nombre", "demoA"]),
       ("faroB", 7013, 7113, [], ["--faro", "simulado", "--faro-nombre", "demoB", "--gateway"]),
       ("b1",    7011, 7111, [7013], []),
       ("b2",    7012, 7112, [7011, 7013], [])]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lento", action="store_true", help="Faro simulado a 25 B/s con 5 %% de pérdida")
    a = p.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    logs = os.path.join(AQUI, "demo_logs")
    os.makedirs(logs, exist_ok=True)
    procesos = []
    for alias, udp, api, vecinos, extra in RED:
        cmd = [sys.executable, "-u", os.path.join(AQUI, "faronet_nodo.py"), "--alias", alias,
               "--puerto", str(udp), "--api", str(api), "--sin-descubrir",
               "--carpeta", os.path.join(logs, "llaves")]
        for v in vecinos:
            cmd += ["--vecino", f"127.0.0.1:{v}"]
        cmd += extra
        if a.lento and "--faro" in extra:
            cmd += ["--velocidad", "25", "--perdida", "0.05"]
        salida = open(os.path.join(logs, f"{alias}.log"), "w")
        procesos.append((alias, api, subprocess.Popen(cmd, stdout=salida, stderr=subprocess.STDOUT)))
        print(f"  {alias:6} API {api}   python3 App/chat.py --api {api} interactivo")
    print("\nRed levantada. El directorio tarda unos segundos en llenarse "
          f"({'unos 3 minutos' if a.lento else 'unos 10 s'}). Ctrl+C para detener.")
    try:
        while True:
            time.sleep(1)
            for alias, _, proc in procesos:
                if proc.poll() is not None:
                    print(f"[el nodo {alias} terminó; vea demo_logs/{alias}.log]")
                    procesos = [x for x in procesos if x[2] is not proc]
    except KeyboardInterrupt:
        pass
    finally:
        for _, _, proc in procesos:
            proc.terminate()
        for _, _, proc in procesos:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Nodos detenidos.")


if __name__ == "__main__":
    main()
