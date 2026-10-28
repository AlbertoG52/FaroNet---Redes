#!/usr/bin/env python3
"""
chat.py - Cliente de terminal de la aplicación anónima de FaroNet (Capa 7).

Habla con el nodo local (faronet_nodo.py) por su API en 127.0.0.1. Todos los
parámetros se reciben desde la terminal:

  python3 App/chat.py --api 7101 info
  python3 App/chat.py --api 7101 directorio
  python3 App/chat.py --api 7101 privado --para beto --texto "hola"
  python3 App/chat.py --api 7101 privado --para beto --texto "soy anónimo" --anonimo
  python3 App/chat.py --api 7101 publicar --tema redes --texto "mensaje público"
  python3 App/chat.py --api 7101 publicar --tema grupo --frase "clave larga" --texto "solo el grupo"
  python3 App/chat.py --api 7101 leer --tema redes
  python3 App/chat.py --api 7101 leer --tema grupo --frase "clave larga"
  python3 App/chat.py --api 7101 buzon
  python3 App/chat.py --api 7101 interactivo

Dentro del modo interactivo:
  <texto>               enviar al destino actual (tema o persona)
  /tema <nombre>        publicar en un tema público
  /tema <nombre> <frase>  tema privado (la frase es la clave; compártala en persona)
  /para <alias o id>    mensajes privados a esa persona
  /anonimo              alternar privados anónimos / identificados
  /leer                 ver lo guardado del tema actual
  /buzon  /temas  /vecinos  /rutas  /directorio  /info  /rotar_mac
  /panico               borrar llaves y datos del nodo
  /ayuda  /salir
"""
import argparse
import json
import socket
import sys
import threading
import time
from datetime import datetime

AYUDA = __doc__[__doc__.index("Dentro del modo interactivo:"):]


class Conexion:
    def __init__(self, puerto, host="127.0.0.1"):
        try:
            self.sock = socket.create_connection((host, puerto), timeout=60)
        except OSError:
            sys.exit(f"No hay un nodo escuchando en {host}:{puerto}. "
                     "¿Arrancó faronet_nodo.py con ese --api?")
        self.archivo = self.sock.makefile("rwb")

    def pedir(self, **pedido):
        self.archivo.write((json.dumps(pedido) + "\n").encode())
        self.archivo.flush()
        linea = self.archivo.readline()
        if not linea:
            raise ConnectionError("el nodo cerró la conexión")
        return json.loads(linea)

    def eventos(self):
        self.archivo.write(b'{"cmd": "escuchar"}\n')
        self.archivo.flush()
        self.sock.settimeout(None)
        for linea in self.archivo:
            yield json.loads(linea)


def hora(segundos):
    return datetime.fromtimestamp(segundos).strftime("%H:%M:%S")


def mostrar(r, cmd):
    if not r.get("ok"):
        print(f"Error: {r.get('error')}")
        for o in r.get("opciones", []):
            print(f"   {o['id']}  {o['alias']}")
        return False
    if cmd == "info":
        print(f"Nodo {r['alias'] or '(sin alias)'}  id {r['id']}  gateway: {'sí' if r['gateway'] else 'no'}")
        print(f"  interfaces: {', '.join(r['interfaces'])}")
        print(f"  vecinos {r['vecinos']} · rutas {r['rutas']} · directorio {r['directorio']} nodos "
              f"· buzón {r['buzon']} · publicaciones {r['publicaciones']}")
        e = r["estadisticas"]
        print("  " + ", ".join(f"{k} {v}" for k, v in e.items()))
    elif cmd == "vecinos":
        if not r["vecinos"]:
            print("(sin vecinos todavía)")
        for v in r["vecinos"]:
            print(f"  {v['id']}  {v['alias']:16} por {v['mejor']}  (enlaces: {', '.join(v['enlaces'])})")
    elif cmd == "rutas":
        if not r["rutas"]:
            print("(sin rutas todavía)")
        for x in r["rutas"]:
            print(f"  {x['destino']}  {x['alias']:16} costo {x['metrica']:2}  "
                  f"vía {x['alias_siguiente'] or x['siguiente']}")
    elif cmd == "directorio":
        p = r["propia"]
        if p:
            print(f"  (este) {p['id']}  {p['alias']}")
        for n in r["nodos"]:
            print(f"         {n['id']}  {n['alias']:16} {'[gateway] ' if n['gateway'] else ''}"
                  f"vence {hora(n['expira'])}")
        print(f"  {len(r['nodos'])} nodos más en el directorio (entradas firmadas y verificadas)")
    elif cmd in ("privado", "publicar"):
        if cmd == "privado":
            print(f"Enviado a {r['para']} por 3 saltos{' (anónimo)' if r['anonimo'] else ''}.")
        else:
            print(f"Publicado en '{r['tema']}'{' (privado)' if r['privado'] else ''} por 3 saltos.")
    elif cmd == "leer":
        if not r["publicaciones"]:
            print(f"(no hay publicaciones guardadas en '{r['tema']}')")
        for p in r["publicaciones"]:
            print(f"  [{hora(p['creada'])}] {p['texto']}")
    elif cmd == "buzon":
        if not r["mensajes"]:
            print("(buzón vacío)")
        for m in r["mensajes"]:
            print(f"  [{hora(m['recibido'])}] {quien(m)}: {m['texto']}")
    elif cmd == "temas":
        if not r["temas"]:
            print("(no hay publicaciones guardadas)")
        for t in r["temas"]:
            print(f"  {t['tema']:24} {'privado' if t['privado'] else 'público':8} {t['publicaciones']}")
    elif cmd == "unirse":
        print(f"Unido al tema privado '{r['tema']}' (etiqueta {r['etiqueta']}).")
    elif cmd == "rotar_mac":
        print(f"MAC del Faro rotada: {', '.join(r['macs'])} (anunciada a los vecinos)")
    elif cmd == "panico":
        print("Pánico enviado: el nodo borró sus llaves y se detuvo.")
    return True


def quien(m):
    if not m.get("remitente"):
        return "anónimo"
    nombre = m.get("alias") or m["remitente"][:8]
    return f"{nombre}{'' if m.get('verificado') else ' (sin verificar)'}"


def interactivo(puerto):
    con = Conexion(puerto)
    info = con.pedir(cmd="info")
    if not info.get("ok"):
        sys.exit(info.get("error"))
    estado = {"modo": "tema", "tema": "general", "frase": None, "para": None, "anonimo": False}

    def escuchar():
        try:
            for ev in Conexion(puerto).eventos():
                tipo = ev.get("evento")
                if tipo == "privado":
                    print(f"\r[privado de {quien(ev)}] {ev['texto']}\n> ", end="", flush=True)
                elif tipo == "publicacion":
                    candado = "*" if ev["privado"] else "#"
                    print(f"\r[{candado}{ev['tema']}] {ev['texto']}\n> ", end="", flush=True)
                elif tipo == "error":
                    print(f"\r[aviso] {ev['detalle']}\n> ", end="", flush=True)
                elif tipo == "panico":
                    print("\r[el nodo ejecutó el pánico]", flush=True)
        except (OSError, ValueError, ConnectionError):
            print("\r[se perdió la conexión con el nodo]", flush=True)

    threading.Thread(target=escuchar, daemon=True).start()
    print(f"Chat FaroNet en el nodo {info['alias'] or info['id'][:8]}. "
          f"Tema actual: #general. Escriba /ayuda.")

    def destino():
        if estado["modo"] == "para":
            return f"@{estado['para']}{' (anónimo)' if estado['anonimo'] else ''}"
        return f"{'*' if estado['frase'] else '#'}{estado['tema']}"

    while True:
        try:
            linea = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not linea:
            continue
        try:
            if not linea.startswith("/"):
                if estado["modo"] == "para":
                    r = con.pedir(cmd="privado", para=estado["para"], texto=linea,
                                  anonimo=estado["anonimo"])
                    if not r.get("ok"):
                        print(f"Error: {r['error']}")
                else:
                    r = con.pedir(cmd="publicar", tema=estado["tema"], texto=linea,
                                  frase=estado["frase"])
                    if not r.get("ok"):
                        print(f"Error: {r['error']}")
                continue
            partes = linea.split(maxsplit=2)
            orden = partes[0].lower()
            if orden == "/salir":
                return
            if orden == "/ayuda":
                print(AYUDA)
            elif orden == "/tema" and len(partes) >= 2:
                estado.update(modo="tema", tema=partes[1].lower(),
                              frase=partes[2] if len(partes) == 3 else None)
                if estado["frase"]:
                    print("Derivando la clave del tema (Argon2id)...")
                    mostrar(con.pedir(cmd="unirse", tema=estado["tema"], frase=estado["frase"]),
                            "unirse")
                print(f"Ahora escribe en {destino()}")
            elif orden == "/para" and len(partes) >= 2:
                estado.update(modo="para", para=" ".join(partes[1:]))
                print(f"Ahora escribe a {destino()}")
            elif orden == "/anonimo":
                estado["anonimo"] = not estado["anonimo"]
                print(f"Privados {'anónimos' if estado['anonimo'] else 'identificados'}.")
            elif orden == "/leer":
                mostrar(con.pedir(cmd="leer", tema=estado["tema"], frase=estado["frase"]), "leer")
            elif orden in ("/buzon", "/temas", "/vecinos", "/rutas", "/directorio", "/info",
                           "/rotar_mac"):
                mostrar(con.pedir(cmd=orden[1:]), orden[1:])
            elif orden == "/panico":
                mostrar(con.pedir(cmd="panico"), "panico")
                time.sleep(0.5)
                return
            else:
                print("Comando no reconocido. Escriba /ayuda.")
        except (OSError, ConnectionError, ValueError) as e:
            print(f"Se perdió la conexión con el nodo: {e}")
            return


def main():
    p = argparse.ArgumentParser(description="Chat anónimo de FaroNet",
                                epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", type=int, default=7100, help="puerto de la API del nodo local")
    sub = p.add_subparsers(dest="cmd", required=True)
    for nombre in ("info", "vecinos", "rutas", "directorio", "temas", "rotar_mac", "panico",
                   "interactivo"):
        sub.add_parser(nombre)
    s = sub.add_parser("privado", help="mensaje privado cifrado de extremo a extremo")
    s.add_argument("--para", required=True, help="alias o id (o el inicio del id)")
    s.add_argument("--texto", required=True)
    s.add_argument("--anonimo", action="store_true", help="no decir quién lo envía")
    s.add_argument("--ttl", type=int, default=3600, help="segundos que se guarda (máx. 86400)")
    s = sub.add_parser("publicar", help="publicar en un tema")
    s.add_argument("--tema", required=True)
    s.add_argument("--texto", required=True)
    s.add_argument("--frase", default=None, help="clave de un tema PRIVADO (no sale de FaroNet)")
    s.add_argument("--ttl", type=int, default=3600)
    s = sub.add_parser("leer", help="ver las publicaciones guardadas de un tema")
    s.add_argument("--tema", required=True)
    s.add_argument("--frase", default=None)
    s = sub.add_parser("unirse", help="unirse a un tema privado (para recibir sus mensajes)")
    s.add_argument("--tema", required=True)
    s.add_argument("--frase", required=True)
    s = sub.add_parser("buzon", help="ver los mensajes privados recibidos")
    s.add_argument("--vaciar", action="store_true")
    a = p.parse_args()

    if a.cmd == "interactivo":
        interactivo(a.api)
        return
    pedido = {k: v for k, v in vars(a).items() if k != "api" and v is not None}
    try:
        r = Conexion(a.api).pedir(**pedido)
    except (OSError, ConnectionError, ValueError) as e:
        sys.exit(f"Error de conexión con el nodo: {e}")
    sys.exit(0 if mostrar(r, a.cmd) else 1)


if __name__ == "__main__":
    main()
