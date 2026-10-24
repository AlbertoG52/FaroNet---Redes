"""
medicion.py - Mide la tasa de error de bits (BER) y la tasa de transferencia
efectiva (throughput) del enlace de luz.

Se usan dos Raspberry Pi: una corre el RECEPTOR y la otra el EMISOR.
Repita la medición a varias distancias (por ejemplo 10 cm, 50 cm y 1 m),
con la luz del cuarto encendida.

1) BER (tasa de error de bits): el emisor envía N tramas de prueba con un
   patrón conocido, sin ACK ni reenvíos. El receptor genera el mismo patrón
   y compara bit por bit lo que llegó (incluso las tramas con CRC malo).

       Pi B:  python3 medicion.py receptor --tramas 100
       Pi A:  python3 medicion.py ber --tramas 100

2) Throughput efectivo: el emisor envía una cantidad de datos útiles con
   ACK y reenvíos (como en uso normal) y mide cuánto tardó.

       Pi B:  python3 medicion.py receptor
       Pi A:  python3 medicion.py throughput --bytes 2000

   throughput = bytes útiles entregados x 8 / segundos que tomó

Use el mismo --t-us y --pausa en ambas Pi. Con --simulado se puede probar
en una laptop sin Raspberry Pi (por ejemplo con --velocidad 25 --perdida 0.05).
"""
import argparse
import difflib
import random
import sys
import time

from DispositivoLuzAdaptador import trama
import faro as programa_faro

LARGO_PRUEBA = 100          # bytes de datos en cada trama de prueba


def patron(n):
    """Datos conocidos de la trama de prueba número n (igual en ambas Pi)."""
    return random.Random(1000 + n).randbytes(LARGO_PRUEBA)


def bits_distintos(recibido, esperado):
    """Bits erróneos entre lo recibido y lo esperado. Primero alinea las dos
    secuencias (un byte ilegible se pierde y corre a los siguientes); cada
    byte faltante o sobrante cuenta como 8 bits erróneos."""
    errores = 0
    alineador = difflib.SequenceMatcher(None, esperado, recibido, autojunk=False)
    for op, e1, e2, r1, r2 in alineador.get_opcodes():
        if op == "equal":
            continue
        a, b = esperado[e1:e2], recibido[r1:r2]
        errores += sum(bin(x ^ y).count("1") for x, y in zip(a, b))
        errores += 8 * abs(len(a) - len(b))
    return errores


def crear_faro(a):
    try:
        return programa_faro.crear_faro(a)
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(str(e))


def receptor(a):
    faro = crear_faro(a)
    vistas = {}                 # n.º de trama -> (crc_ok, bits erróneos)
    tiempos = []

    def observar(t, crc_ok):
        if t.tipo != trama.TIPO_PRUEBA or t.n_msg is None or t.n_msg >= a.tramas:
            return
        if t.n_msg in vistas and vistas[t.n_msg][0]:
            return
        vistas[t.n_msg] = (crc_ok, bits_distintos(t.datos, patron(t.n_msg)))
        tiempos.append(time.time())

    faro.observador = observar
    faro.anunciar()             # para que el emisor de throughput lo encuentre
    print(f"Receptor de medición listo. MAC {trama.mac_a_texto(faro.mac)}. "
          f"Esperando {a.tramas} tramas de prueba y/o mensajes (Ctrl+C para terminar).")
    recibidos = 0
    try:
        while True:
            m = faro.recibir(timeout=1)
            if m:
                recibidos += len(m.datos)
                print(f"[mensaje de {trama.mac_a_texto(m.origen)}: {len(m.datos)} bytes, "
                      f"{recibidos} en total]")
            if len(vistas) >= a.tramas:
                break
            if tiempos and time.time() - tiempos[-1] > 15:
                break               # ya no llegan más tramas de prueba
    except KeyboardInterrupt:
        pass
    finally:
        faro.cerrar()

    if vistas:
        bits = len(vistas) * LARGO_PRUEBA * 8
        errores = sum(e for _, e in vistas.values())
        ok = sum(1 for c, _ in vistas.values() if c)
        print("\n===== Resultado BER =====")
        print(f"Tramas enviadas        : {a.tramas}")
        print(f"Tramas recibidas       : {len(vistas)}  (CRC correcto: {ok}, "
              f"con errores: {len(vistas) - ok})")
        print(f"Tramas perdidas        : {a.tramas - len(vistas)}")
        print(f"Bits comparados        : {bits}")
        print(f"Bits erróneos          : {errores}")
        print(f"BER                    : {errores / bits:.2e}")
        if len(tiempos) > 1:
            segundos = tiempos[-1] - tiempos[0]
            util = ok * LARGO_PRUEBA * 8
            print(f"Tasa de datos sin ACK  : {util / segundos:.1f} bit/s "
                  f"(solo tramas con CRC correcto)")


def ber(a):
    faro = crear_faro(a)
    print(f"Enviando {a.tramas} tramas de prueba de {LARGO_PRUEBA} bytes "
          f"(T = {a.t_us} µs, pausa = {a.pausa}T)...")
    inicio = time.time()
    try:
        tramas = [trama.armar_v3(patron(n), faro.mac, trama.DIFUSION, trama.TIPO_PRUEBA, n,
                                  protocolo=trama.PROTO_MEDICION)
                  for n in range(a.tramas)]
        for n, t in enumerate(tramas):
            faro.enviar_tramas([t]).esperar()
            print(f"\r  {n + 1}/{a.tramas}", end="", flush=True)
        print(f"\nListo en {time.time() - inicio:.1f} s. Vea el resultado en el receptor.")
    except KeyboardInterrupt:
        pass
    finally:
        faro.cerrar()


def throughput(a):
    faro = crear_faro(a)
    try:
        if a.destino:
            destino = trama.mac_desde_texto(a.destino)
        else:
            print("Buscando al receptor (debe estar corriendo)...")
            faro.anunciar()         # el receptor responde con su propio anuncio
            fin = time.time() + 20
            while not faro.vecinos and time.time() < fin:
                time.sleep(0.2)
            if not faro.vecinos:
                sys.exit("No se escuchó a ningún Faro. Use --destino <MAC del receptor>.")
            destino = next(iter(faro.vecinos))
        datos = random.Random(7).randbytes(a.bytes)
        trozo = faro.mtu
        print(f"Enviando {a.bytes} bytes a {trama.mac_a_texto(destino)} con ACK...")
        inicio = time.time()
        entregados = 0
        for i in range(0, len(datos), trozo):
            parte = datos[i:i + trozo]
            if faro.enviar(parte, destino, trama.PROTO_MEDICION).esperar():
                entregados += len(parte)
            print(f"\r  {entregados}/{a.bytes} bytes entregados", end="", flush=True)
        segundos = time.time() - inicio
        e = faro.estadisticas
        print("\n===== Resultado throughput =====")
        print(f"Bytes útiles entregados : {entregados} de {a.bytes}")
        print(f"Tiempo                  : {segundos:.1f} s")
        print(f"Throughput efectivo     : {entregados * 8 / segundos:.1f} bit/s "
              f"({entregados / segundos:.1f} bytes/s)")
        print(f"Tramas enviadas         : {e['tramas_enviadas']} (reenvíos: {e['reenvios']})")
        print(f"ACKs recibidos          : {e['acks_recibidos']}")
    except KeyboardInterrupt:
        pass
    finally:
        faro.cerrar()


def main():
    p = argparse.ArgumentParser(description="Mediciones de BER y throughput")
    p.add_argument("modo", choices=["receptor", "ber", "throughput"])
    p.add_argument("--tramas", type=int, default=100,
                   help="tramas de prueba para el BER (máx. 256, igual en ambas Pi)")
    p.add_argument("--bytes", type=int, default=2000, help="bytes para el throughput")
    p.add_argument("--destino", help="MAC del receptor (si no, se detecta sola)")
    programa_faro.argumentos_del_faro(p)
    a = p.parse_args()
    if not 1 <= a.tramas <= 256:
        sys.exit("--tramas debe estar entre 1 y 256")
    {"receptor": receptor, "ber": ber, "throughput": throughput}[a.modo](a)


if __name__ == "__main__":
    main()
