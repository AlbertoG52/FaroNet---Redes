"""
servicio.py - La aplicación anónima de FaroNet (Capa 7) que corre en cada nodo.

Guarda EN MEMORIA (nunca en disco) y solo durante su TTL:
    - el buzón de mensajes privados recibidos;
    - las publicaciones de los temas (públicas y privadas);
    - las claves de los temas privados a los que este nodo se unió.

Y atiende al cliente de terminal (App/chat.py) por una API local: un socket
TCP en 127.0.0.1 (no se puede usar desde otra computadora) que recibe una
línea JSON por pedido y responde una línea JSON.

    {"cmd": "privado", "para": "ana", "texto": "hola"}  ->  {"ok": true, ...}

El pedido {"cmd": "escuchar"} deja la conexión abierta y el nodo va mandando
los mensajes nuevos a medida que llegan (lo usa el modo interactivo).
"""
import json
import os
import queue
import socket
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "..", "Mesh"))

import mensajes as M          # noqa: E402
from nodo import ErrorRuta    # noqa: E402

MAX_BUZON = 200
MAX_PUBLICACIONES = 1000
TOLERANCIA_RELOJ_S = 300


class ServicioChat:
    def __init__(self, nodo):
        self.nodo = nodo
        self.buzon = []                     # dicts de mensajes privados
        self.publicaciones = {}             # id_msg -> Publicacion
        self.temas_privados = {}            # etiqueta -> (nombre, clave)
        self._claves = {}                   # (nombre, frase) -> clave (evita repetir Argon2)
        self._oyentes = []                  # colas de las conexiones "escuchar"
        self._lock = threading.RLock()
        self.al_publicacion_publica = []    # funciones f(dict) (las usa el gateway)
        nodo.al_privado = self._al_privado
        nodo.al_publicacion = self._al_publicacion
        nodo.al_panico.append(self.borrar)
        self._activo = True
        threading.Thread(target=self._limpiar, daemon=True).start()

    # ------------------------------------------------------------ lo que llega de la mesh
    def _alias(self, id_nodo):
        e = self.nodo.directorio.obtener(id_nodo) if id_nodo else None
        return e.alias if e else ""

    def _al_privado(self, datos):
        def buscar_llave(id_nodo):
            e = self.nodo.directorio.obtener(id_nodo)
            return e.llave_cifrado if e else None
        try:
            m = M.leer_privado(datos, self.nodo.identidad, buscar_llave)
        except M.ErrorMensaje as e:
            self._avisar({"evento": "error", "detalle": f"mensaje privado ilegible: {e}"})
            return
        ahora = time.time()
        item = {"texto": m.texto, "enviado": m.enviado, "recibido": int(ahora),
                "vence": int(ahora + m.ttl),
                "remitente": m.remitente.hex() if m.remitente else None,
                "alias": self._alias(m.remitente), "verificado": m.verificado}
        with self._lock:
            self.buzon.append(item)
            del self.buzon[:-MAX_BUZON]
        self._avisar(dict(item, evento="privado"))

    def _al_publicacion(self, datos):
        """La mesh pregunta si la publicación es válida (para seguir inundándola)."""
        try:
            p = M.leer_publicacion(datos)
        except M.ErrorMensaje:
            return False
        ahora = time.time()
        if p.vence < ahora or p.creada > ahora + TOLERANCIA_RELOJ_S:
            return False
        with self._lock:
            if p.id_msg in self.publicaciones:
                return True
            self.publicaciones[p.id_msg] = p
            if len(self.publicaciones) > MAX_PUBLICACIONES:
                mas_vieja = min(self.publicaciones.values(), key=lambda x: x.creada)
                del self.publicaciones[mas_vieja.id_msg]
            privado = self.temas_privados.get(p.tema) if p.visibilidad == M.PRIVADA else None
        if p.visibilidad == M.PUBLICA:
            evento = self._pub_a_dict(p, p.tema, p.texto())
            self._avisar(dict(evento, evento="publicacion"))
            for funcion in list(self.al_publicacion_publica):
                try:
                    funcion(evento)
                except Exception:
                    pass
        elif privado:
            try:
                self._avisar(dict(self._pub_a_dict(p, privado[0], p.texto(privado[1])),
                                  evento="publicacion"))
            except M.ErrorMensaje:
                pass
        return True

    @staticmethod
    def _pub_a_dict(p, tema, texto):
        return {"tema": tema, "privado": p.visibilidad == M.PRIVADA, "texto": texto,
                "id": p.id_msg.hex(), "creada": p.creada, "vence": p.vence}

    # ------------------------------------------------------------ eventos y limpieza
    def _avisar(self, evento):
        with self._lock:
            oyentes = list(self._oyentes)
        for cola in oyentes:
            try:
                cola.put_nowait(evento)
            except queue.Full:
                pass

    def _limpiar(self):
        while self._activo:
            time.sleep(2)
            ahora = time.time()
            with self._lock:
                self.buzon = [m for m in self.buzon if m["vence"] > ahora]
                for k in [k for k, p in self.publicaciones.items() if p.vence <= ahora]:
                    del self.publicaciones[k]

    def borrar(self):
        """Pánico: borra buzón, publicaciones y claves de temas."""
        with self._lock:
            self.buzon.clear()
            self.publicaciones.clear()
            self.temas_privados.clear()
            self._claves.clear()
        self._avisar({"evento": "panico"})
        self._activo = False

    # ------------------------------------------------------------ temas privados
    def _clave(self, tema, frase):
        tema = tema.strip().lower()
        with self._lock:
            clave = self._claves.get((tema, frase))
        if clave is None:
            clave = M.clave_de_tema(tema, frase)            # Argon2id (tarda ~1 s en la Pi)
            with self._lock:
                self._claves[(tema, frase)] = clave
                self.temas_privados[M.etiqueta_de_clave(clave)] = (tema, clave)
        return tema, clave

    # ------------------------------------------------------------ comandos de la API
    def atender(self, pedido):
        cmd = pedido.get("cmd")
        nodo = self.nodo
        if nodo.panico_activado and cmd != "info":
            return {"ok": False, "error": "el nodo ejecutó el pánico"}
        if cmd == "info":
            return {"ok": True, **nodo.info(), "panico": nodo.panico_activado,
                    "buzon": len(self.buzon), "publicaciones": len(self.publicaciones)}
        if cmd == "vecinos":
            return {"ok": True, "vecinos": nodo.info_vecinos()}
        if cmd == "rutas":
            return {"ok": True, "rutas": nodo.info_rutas()}
        if cmd == "directorio":
            propia = nodo.entrada_propia
            lista = [e.a_dict() for e in nodo.directorio.vigentes()]
            return {"ok": True, "propia": propia.a_dict() if propia else None, "nodos": lista}
        if cmd == "privado":
            return self._cmd_privado(pedido)
        if cmd == "publicar":
            return self._cmd_publicar(pedido)
        if cmd == "unirse":
            tema, clave = self._clave(pedido["tema"], pedido["frase"])
            return {"ok": True, "tema": tema, "etiqueta": M.etiqueta_de_clave(clave)}
        if cmd == "leer":
            return self._cmd_leer(pedido)
        if cmd == "temas":
            return self._cmd_temas()
        if cmd == "buzon":
            with self._lock:
                lista = list(self.buzon)
                if pedido.get("vaciar"):
                    self.buzon.clear()
            return {"ok": True, "mensajes": lista}
        if cmd == "rotar_mac":
            nuevas = nodo.rotar_mac()
            if not nuevas:
                return {"ok": False, "error": "este nodo no tiene Faro"}
            return {"ok": True, "macs": nuevas}
        if cmd == "panico":
            threading.Thread(target=nodo.panico, daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": f"comando desconocido: {cmd}"}

    def _cmd_privado(self, pedido):
        nodo = self.nodo
        candidatos = nodo.directorio.buscar(pedido["para"])
        if not candidatos:
            return {"ok": False, "error": "no hay ningún nodo con ese alias o id en el directorio"}
        if len(candidatos) > 1:
            return {"ok": False, "error": "hay varios nodos con ese nombre; use el id",
                    "opciones": [{"id": e.id.hex(), "alias": e.alias} for e in candidatos]}
        destino = candidatos[0]
        anonimo = bool(pedido.get("anonimo"))
        try:
            datos = M.armar_privado(pedido["texto"], pedido.get("ttl", M.TTL_POR_DEFECTO),
                                    None if anonimo else nodo.identidad, destino.llave_cifrado)
            nodo.enviar_privado(destino.id, datos)
        except (M.ErrorMensaje, ErrorRuta) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "para": destino.alias or destino.id.hex(), "anonimo": anonimo}

    def _cmd_publicar(self, pedido):
        try:
            clave = None
            tema = pedido["tema"]
            if pedido.get("frase"):
                tema, clave = self._clave(tema, pedido["frase"])
            datos = M.armar_publicacion(tema, pedido["texto"], clave,
                                        pedido.get("ttl", M.TTL_POR_DEFECTO))
            self.nodo.publicar(datos)
        except (M.ErrorMensaje, ErrorRuta) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "tema": tema.strip().lower(), "privado": clave is not None}

    def _cmd_leer(self, pedido):
        tema = pedido["tema"].strip().lower()
        clave = None
        if pedido.get("frase"):
            tema, clave = self._clave(tema, pedido["frase"])
        etiqueta = M.etiqueta_de_clave(clave) if clave else None
        with self._lock:
            pubs = sorted(self.publicaciones.values(), key=lambda p: p.creada)
        salida = []
        for p in pubs:
            if clave is None and p.visibilidad == M.PUBLICA and p.tema == tema:
                salida.append(self._pub_a_dict(p, tema, p.texto()))
            elif clave is not None and p.visibilidad == M.PRIVADA and p.tema == etiqueta:
                try:
                    salida.append(self._pub_a_dict(p, tema, p.texto(clave)))
                except M.ErrorMensaje:
                    pass
        return {"ok": True, "tema": tema, "privado": clave is not None, "publicaciones": salida}

    def _cmd_temas(self):
        with self._lock:
            pubs = list(self.publicaciones.values())
            privados = dict(self.temas_privados)
        conteo = {}
        for p in pubs:
            if p.visibilidad == M.PUBLICA:
                clave = (p.tema, False)
            elif p.tema in privados:
                clave = (privados[p.tema][0], True)
            else:
                clave = ("(privado, sin clave)", True)
            conteo[clave] = conteo.get(clave, 0) + 1
        return {"ok": True, "temas": [{"tema": t, "privado": priv, "publicaciones": n}
                                      for (t, priv), n in sorted(conteo.items())]}

    # ------------------------------------------------------------ servidor de la API
    def servir(self, puerto):
        servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        servidor.bind(("127.0.0.1", puerto))        # solo desde esta computadora
        servidor.listen(8)
        self._servidor = servidor
        threading.Thread(target=self._aceptar, daemon=True).start()
        return servidor.getsockname()[1]

    def _aceptar(self):
        while True:
            try:
                conexion, _ = self._servidor.accept()
            except OSError:
                return
            threading.Thread(target=self._atender_conexion, args=(conexion,), daemon=True).start()

    def _atender_conexion(self, conexion):
        with conexion:
            archivo = conexion.makefile("rwb")
            for linea in archivo:
                try:
                    pedido = json.loads(linea)
                except ValueError:
                    respuesta = {"ok": False, "error": "JSON inválido"}
                else:
                    if pedido.get("cmd") == "escuchar":
                        self._transmitir_eventos(archivo)
                        return
                    try:
                        respuesta = self.atender(pedido)
                    except KeyError as e:
                        respuesta = {"ok": False, "error": f"falta el parámetro {e}"}
                    except Exception as e:
                        respuesta = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    archivo.write((json.dumps(respuesta, ensure_ascii=False) + "\n").encode())
                    archivo.flush()
                except OSError:
                    return

    def _transmitir_eventos(self, archivo):
        cola = queue.Queue(maxsize=500)
        with self._lock:
            self._oyentes.append(cola)
        try:
            archivo.write(b'{"ok": true, "evento": "escuchando"}\n')
            archivo.flush()
            while True:
                try:
                    evento = cola.get(timeout=15)
                except queue.Empty:
                    evento = {"evento": "latido"}       # detecta si el cliente se fue
                archivo.write((json.dumps(evento, ensure_ascii=False) + "\n").encode())
                archivo.flush()
                if evento.get("evento") == "panico":
                    return
        except OSError:
            pass
        finally:
            with self._lock:
                if cola in self._oyentes:
                    self._oyentes.remove(cola)
