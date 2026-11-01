#!/usr/bin/env python3
"""
pi_comunidad.py - Levanta UNA comunidad de FaroNet completa dentro de una
Raspberry Pi: el nodo con el Faro real más 2 nodos extra (procesos de la misma
Pi). Así cada Pi es una comunidad entera y la prueba con los Faros no necesita
laptops: para un mensaje privado hacen falta 5 nodos (emisor, 3 intermedios y
destino) y aquí hay 6 (3 en cada Pi).

    # Pi A (comunidad 1): no necesita Internet, solo la luz
    sudo systemctl start pigpiod
    python3 pi_comunidad.py --lado A --pausa 40 --clave-enlace "frase acordada"

    # Pi B (comunidad 2, gateway): necesita Internet y la hora correcta (date)
    sudo systemctl start pigpiod
    python3 pi_comunidad.py --lado B --pausa 40 --clave-enlace "frase acordada" \\
        --worker https://faronet.<cuenta>.workers.dev

Luego, en otra terminal de la misma Pi:
    python3 App/chat.py --api 7101 interactivo      # Pi A: nodo a1
    python3 App/chat.py --api 7111 interactivo      # Pi B: nodo b1

Sin Raspberry Pi (para ensayar en una laptop): --faro simulado.
Ctrl+C detiene los 3 nodos de esta Pi.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))

LADOS = {
    #     alias Faro, UDP, API ;  nodos extra (alias, UDP, API)
    "A": (("faroA", 7004, 7104), [("a1", 7001, 7101), ("a2", 7002, 7102)]),
    "B": (("faroB", 7013, 7113), [("b1", 7011, 7111), ("b2", 7012, 7112)]),
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lado", choices=("A", "B"), required=True)
    p.add_argument("--faro", choices=("real", "simulado"), default="real")
    p.add_argument("--pausa", type=int, default=40, help="IGUAL en las dos Pi")
    p.add_argument("--ciclo", type=int, default=33)
    p.add_argument("--datos-por-trama", type=int, default=110)
    p.add_argument("--clave-enlace", default=None, help="IGUAL en las dos Pi (recomendado)")
    p.add_argument("--worker", default=None, help="solo en la Pi B: URL del sitio de Cloudflare")
    p.add_argument("--depurar", action="store_true", help="ver eventos del nodo del Faro")
    a = p.parse_args()
    if a.worker and a.lado != "B":
        sys.exit("--worker solo va en la Pi B (el gateway)")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    (faro_alias, faro_udp, faro_api), extras = LADOS[a.lado]
    base = [sys.executable, "-u", os.path.join(AQUI, "faronet_nodo.py"), "--sin-descubrir"]
    cmd_faro = base + ["--alias", faro_alias, "--puerto", str(faro_udp), "--api", str(faro_api),
                       "--faro", a.faro, "--pausa", str(a.pausa)]
    if a.faro == "real":
        cmd_faro += ["--ciclo", str(a.ciclo), "--datos-por-trama", str(a.datos_por_trama)]
    else:
        cmd_faro += ["--faro-nombre", faro_alias]
    if a.clave_enlace:
        cmd_faro += ["--clave-enlace", a.clave_enlace]
    if a.worker:
        cmd_faro += ["--worker", a.worker]
    if a.depurar:
        cmd_faro += ["--depurar"]

    procesos = [(faro_alias, subprocess.Popen(cmd_faro))]          # el del Faro muestra su salida
    time.sleep(1)
    for alias, udp, api in extras:
        cmd = base + ["--alias", alias, "--puerto", str(udp), "--api", str(api),
                      "--vecino", f"127.0.0.1:{faro_udp}"]
        procesos.append((alias, subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.STDOUT)))
    print(f"\nComunidad {a.lado} levantada: {faro_alias} (con el Faro) + "
          f"{', '.join(x[0] for x in extras)}.")
    for alias, _, api in [LADOS[a.lado][0]] + extras:
        print(f"  {alias:6} chat: python3 App/chat.py --api {api} interactivo")
    print("Por la luz todo es lento: espere unos 5 minutos a que /directorio muestre "
          "los nodos de la otra Pi. Ctrl+C para detener.\n")
    try:
        while True:
            time.sleep(1)
            for alias, proc in list(procesos):
                if proc.poll() is not None:
                    print(f"[el nodo {alias} terminó (código {proc.returncode})]")
                    procesos.remove((alias, proc))
            if not procesos:
                return
    except KeyboardInterrupt:
        pass
    finally:
        for _, proc in procesos:
            proc.terminate()
        for _, proc in procesos:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Nodos detenidos.")


if __name__ == "__main__":
    main()
