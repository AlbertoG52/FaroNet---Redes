"""
faro.py - Chat básico por terminal entre Faros, usando DispositivoLuzAdaptador.

Se corre el MISMO programa en las dos Raspberry Pi. Cada una envía y
recibe a la vez; los mensajes se confirman con ACK y se reenvían si se
pierden. Los mensajes del chat viajan con el protocolo 0x0001 (chat); los
de otros protocolos se muestran resumidos.

Antes de usarlo (una vez por cada arranque de la Pi):
    sudo pigpiod

Uso:
    python3 faro.py
    python3 faro.py --pausa 20          # más descanso entre bytes
    python3 faro.py --ciclo 15          # emitir más débil (si satura al otro Faro)
    python3 faro.py --datos-por-trama 50  # tramas más cortas
    python3 faro.py --destino 3FA107C2  # hablarle a un Faro específico
    python3 faro.py --simulado          # sin Raspberry Pi: Faro falso por UDP
    python3 faro.py --simulado --velocidad 25 --perdida 0.05   # como el real

Dentro del programa:
    <texto> + Enter   enviar el texto
    /para <MAC>       enviar los siguientes mensajes a ese Faro
    /todos            enviar a todos (difusión, sin confirmación)
    /vecinos          ver los Faros escuchados
    /mac              ver la MAC de este Faro
    /rotar            cambiar la MAC por una nueva y anunciarla
    /anunciar         volver a anunciar la MAC (si el otro Faro no la conoce)
    /stats            ver estadísticas de la Capa 1
    /panico           activar el pánico por software (útil en el simulado)
    /ayuda            ver esta lista
    /salir            terminar (o Ctrl+C)
"""
import _thread
import argparse
import sys
import threading
import time

from DispositivoLuzAdaptador import (DispositivoLuzAdaptador, DispositivoLuzSimulado,
                                     Panico, trama)

AYUDA = __doc__[__doc__.index("Dentro del programa:"):]


def argumentos_del_faro(p):
    """Opciones de la terminal comunes a los programas que usan el Faro."""
    g = p.add_argument_group("Faro")
    g.add_argument("--pin-emisor", type=int, default=38, help="pin físico del KY-005")
    g.add_argument("--pin-receptor", type=int, default=40, help="pin físico del KY-022")
    g.add_argument("--led-tx", type=int, default=13, help="pin físico del LED TX")
    g.add_argument("--led-rx", type=int, default=15, help="pin físico del LED RX")
    g.add_argument("--boton", type=int, default=11, help="pin físico del botón de pánico")
    g.add_argument("--led-panico", type=int, default=16, help="pin físico del LED de pánico")
    g.add_argument("--t-us", type=int, default=600,
                   help="unidad de tiempo T en µs (igual en todos los Faros)")
    g.add_argument("--pausa", type=int, default=12,
                   help="silencio después de cada byte, en unidades de T (igual en todos)")
    g.add_argument("--ciclo", type=int, default=33,
                   help="%% del tiempo que el LED está encendido en la portadora "
                        "(menos = luz más débil; por ejemplo 10 a 33). Solo afecta "
                        "a lo que emite este Faro")
    g.add_argument("--datos-por-trama", type=int, default=110,
                   help="bytes de datos por trama (1 a 110). Tramas más cortas "
                        "fallan menos si la luz es débil; solo afecta a lo que envía")
    g.add_argument("--activo-alto", action="store_true",
                   help="usar si el receptor da 1 (no 0) al recibir infrarrojo")
    g.add_argument("--debug", action="store_true", help="mostrar cada trama")
    s = p.add_argument_group("Faro simulado (sin Raspberry Pi)")
    s.add_argument("--simulado", action="store_true",
                   help="usar un Faro falso por UDP multicast en vez de la luz")
    s.add_argument("--nombre", help="guardar la MAC del Faro simulado con este nombre")
    s.add_argument("--velocidad", type=float,
                   help="bytes por segundo del Faro simulado (el real: unos 25)")
    s.add_argument("--perdida", type=float, default=0.0,
                   help="fracción de tramas perdidas en el simulado (0 a 1)")


def crear_faro(a, al_panico=None):
    """Crea el Faro real o el simulado según las opciones de la terminal."""
    if a.simulado:
        return DispositivoLuzSimulado(nombre=a.nombre, velocidad=a.velocidad,
                                      perdida=a.perdida, t_us=a.t_us, pausa=a.pausa,
                                      al_panico=al_panico, depurar=a.debug)
    return DispositivoLuzAdaptador(
        pin_emisor=a.pin_emisor, pin_receptor=a.pin_receptor, led_tx=a.led_tx,
        led_rx=a.led_rx, boton=a.boton, led_panico=a.led_panico, t_us=a.t_us,
        pausa=a.pausa, activo_alto=a.activo_alto, al_panico=al_panico, depurar=a.debug,
        ciclo=a.ciclo, datos_por_trama=a.datos_por_trama)


def main():
    p = argparse.ArgumentParser(description="Chat por luz infrarroja entre Faros")
    p.add_argument("--destino", help="MAC del Faro destino (8 dígitos hex)")
    argumentos_del_faro(p)
    a = p.parse_args()

    def al_panico():
        _thread.interrupt_main()            # despierta al programa aunque espere texto

    try:
        destino = trama.mac_desde_texto(a.destino) if a.destino else None
        faro = crear_faro(a, al_panico)
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(str(e))

    def destino_actual():
        """El elegido con /para; si no, el último Faro escuchado; si no, todos."""
        if destino:
            return destino
        activos = faro.vecinos_activos()
        if activos:
            return max(activos, key=faro.vecinos.get)
        return trama.DIFUSION

    def escuchar():
        try:
            while True:
                m = faro.recibir()
                if not m:
                    continue
                if m.protocolo == trama.PROTO_CHAT:
                    texto = m.datos.decode("utf-8", errors="replace")
                else:
                    texto = (f"<{len(m.datos)} bytes de protocolo "
                             f"{trama.protocolo_a_texto(m.protocolo)}>")
                print(f"\n[de {trama.mac_a_texto(m.origen)}] {texto}\n> ", end="", flush=True)
        except Panico:
            pass

    def avisar_resultado(envio, para):
        envio.esperar()
        if faro.panico_activado:
            return
        if para == trama.DIFUSION:
            texto = "enviado a todos (la difusión no se confirma)"
        elif envio.exito:
            texto = f"entregado a {trama.mac_a_texto(para)}, confirmado con ACK"
        else:
            texto = (f"NO se pudo entregar a {trama.mac_a_texto(para)} "
                     f"después de {envio.intentos} intentos")
        print(f"\n[{texto}]\n> ", end="", flush=True)

    threading.Thread(target=escuchar, daemon=True).start()
    faro.anunciar()                         # que los demás conozcan esta MAC
    tipo = "simulado (UDP)" if a.simulado else f"T = {a.t_us} µs"
    print(f"Faro listo. MAC {trama.mac_a_texto(faro.mac)}, {tipo}. "
          "Escriba /ayuda para ver los comandos (Ctrl+C para salir).")
    try:
        while True:
            linea = input("> ").strip()
            if not linea:
                continue
            if linea.startswith("/"):
                comando, _, argumento = linea.partition(" ")
                if comando == "/salir":
                    break
                elif comando == "/ayuda":
                    print(AYUDA)
                elif comando == "/mac":
                    print(f"MAC de este Faro: {trama.mac_a_texto(faro.mac)}")
                elif comando == "/anunciar":
                    faro.anunciar()
                    print("MAC anunciada.")
                elif comando == "/rotar":
                    print(f"MAC nueva: {trama.mac_a_texto(faro.rotar_mac())} (anunciada)")
                elif comando == "/todos":
                    destino = trama.DIFUSION
                    print("Los mensajes se enviarán a todos.")
                elif comando == "/para":
                    try:
                        destino = trama.mac_desde_texto(argumento)
                        print(f"Los mensajes se enviarán a {trama.mac_a_texto(destino)}.")
                    except ValueError as e:
                        print(e)
                elif comando == "/vecinos":
                    if not faro.vecinos:
                        print("Todavía no se ha escuchado a ningún Faro. Si el otro Faro "
                              "sí recibe sus mensajes, la luz de vuelta (de él hacia este) "
                              "no está llegando.")
                    for mac, hora in faro.vecinos.items():
                        print(f"  {trama.mac_a_texto(mac)}  (hace {time.time() - hora:.0f} s)")
                elif comando == "/stats":
                    for clave, valor in faro.estadisticas.items():
                        print(f"  {clave}: {valor}")
                elif comando == "/panico":
                    faro.activar_panico()
                else:
                    print("Comando desconocido. Escriba /ayuda.")
                continue
            datos = linea.encode("utf-8")
            linea = None
            para = destino_actual()
            try:
                envio = faro.enviar(datos, para, trama.PROTO_CHAT)
            except ValueError as e:
                print(e)
                continue
            threading.Thread(target=avisar_resultado, args=(envio, para), daemon=True).start()
    except (KeyboardInterrupt, EOFError, Panico):
        pass
    finally:
        if faro.panico_activado:
            print("\n\n*** BOTÓN DE PÁNICO ACTIVADO ***")
            print("Se detuvo el Faro, se borraron los datos en memoria y la dirección física.")
            if not a.simulado:
                print(f"El LED rojo (pin {a.led_panico}) queda encendido.")
        faro.cerrar()


if __name__ == "__main__":
    main()
