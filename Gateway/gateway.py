"""
gateway.py - Puerta de enlace entre FaroNet y el edge (Cloudflare Workers).

Corre dentro del nodo de la Pi B (faronet_nodo.py --worker URL). Hace dos cosas:

  1) Cada publicación de un TEMA PÚBLICO que llega a la mesh se envía al
     Worker (POST /gw/publicacion), que la pone en la sala del tema y en su
     feed. Los mensajes privados y los temas privados NUNCA pasan por aquí:
     el servicio de la Capa 7 solo avisa de las publicaciones públicas.
  2) Envía el directorio de nodos (entradas firmadas) para la réplica pública
     (POST /gw/directorio): cuando cambia (como máximo cada 30 s) y, aunque no
     cambie, cada 5 minutos.

Autenticación: cada petición va firmada con la llave Ed25519 del nodo
gateway. El mensaje firmado es (ver Edge/src/comun.ts):

    "FARONET-GW-1\\n" + "POST" + "\\n" + ruta + "\\n" + tiempo + "\\n" + nonce + "\\n" + cuerpo

y viaja en las cabeceras X-FaroNet-Tiempo, X-FaroNet-Nonce y X-FaroNet-Firma.
El Worker rechaza firmas malas, horas con más de 60 s de diferencia y nonces
repetidos (replay). Por eso el reloj de la Pi debe estar en hora (`date`).

Privacidad: las publicaciones no tienen autor; el gateway no agrega nada que
identifique a nadie y no guarda bitácoras. Lo que espera para salir vive en
memoria y se descarta al vencer su TTL.
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

VERSION = 1
INTERVALO_DIRECTORIO_S = 300
MINIMO_ENTRE_DIRECTORIOS_S = 30
REINTENTO_MIN_S, REINTENTO_MAX_S = 5, 120


def mensaje_firmado(ruta, tiempo, nonce, cuerpo):
    return f"FARONET-GW-{VERSION}\nPOST\n{ruta}\n{tiempo}\n{nonce}\n".encode() + cuerpo


def cabeceras_firmadas(identidad, ruta, cuerpo):
    tiempo = str(int(time.time()))
    nonce = os.urandom(16).hex()
    firma = identidad.firmar(mensaje_firmado(ruta, tiempo, nonce, cuerpo))
    return {"X-FaroNet-Tiempo": tiempo, "X-FaroNet-Nonce": nonce, "X-FaroNet-Firma": firma.hex(),
            "Content-Type": "application/json", "User-Agent": "FaroNet-Gateway/1"}


class Gateway:
    def __init__(self, nodo, servicio, url_worker, intervalo_directorio=INTERVALO_DIRECTORIO_S,
                 depurar=False):
        self.nodo = nodo
        self.url = url_worker.rstrip("/")
        if not self.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("la URL del Worker debe empezar con https:// (o http://localhost para pruebas)")
        self.intervalo_directorio = intervalo_directorio
        self.depurar = depurar
        self.cola = queue.Queue(maxsize=500)
        self._activo = True
        self._ultimo_directorio = 0.0
        self._huella_directorio = None
        self.estadisticas = dict.fromkeys(("publicadas", "reintentos", "descartadas",
                                           "directorios", "errores"), 0)
        servicio.al_publicacion_publica.append(self._encolar)
        nodo.al_panico.append(self.detener)

    @property
    def llave(self):
        return bytes(self.nodo.identidad.verificacion).hex()

    def iniciar(self):
        threading.Thread(target=self._trabajar, daemon=True).start()
        return self

    def detener(self):
        self._activo = False
        while True:                                  # pánico: vaciar lo pendiente
            try:
                self.cola.get_nowait()
            except queue.Empty:
                break

    def _log(self, texto):
        if self.depurar:
            print(f"[gateway] {texto}", flush=True)

    def _encolar(self, evento):
        """La llama ServicioChat por cada publicación PÚBLICA nueva."""
        item = {"tema": evento["tema"], "texto": evento["texto"], "id": evento["id"],
                "creada": evento["creada"], "vence": evento["vence"], "_intentos": 0}
        try:
            self.cola.put_nowait(item)
        except queue.Full:
            self.estadisticas["descartadas"] += 1

    # ------------------------------------------------------------ HTTP firmado
    def _post(self, ruta, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        pedido = urllib.request.Request(self.url + ruta, data=cuerpo, method="POST",
                                        headers=cabeceras_firmadas(self.nodo.identidad, ruta, cuerpo))
        try:
            with urllib.request.urlopen(pedido, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, str(e)

    def _entradas(self):
        entradas = sorted(e.a_bytes().hex() for e in self.nodo.directorio.vigentes())
        if self.nodo.entrada_propia:
            entradas.append(self.nodo.entrada_propia.a_bytes().hex())
        return entradas

    def _toca_directorio(self):
        transcurrido = time.time() - self._ultimo_directorio
        if transcurrido >= self.intervalo_directorio:
            return True
        return (transcurrido >= MINIMO_ENTRE_DIRECTORIOS_S
                and hash(tuple(self._entradas())) != self._huella_directorio)

    def enviar_directorio(self):
        entradas = self._entradas()
        self._huella_directorio = hash(tuple(entradas))
        estado, texto = self._post("/gw/directorio", {"entradas": entradas})
        self._ultimo_directorio = time.time()
        if estado == 200:
            self.estadisticas["directorios"] += 1
        else:
            self.estadisticas["errores"] += 1
            self._log(f"directorio rechazado ({estado}): {texto[:120]}")
        return estado

    def _publicar(self, item):
        datos = {k: v for k, v in item.items() if not k.startswith("_")}
        estado, texto = self._post("/gw/publicacion", datos)
        if estado == 200:
            self.estadisticas["publicadas"] += 1
            return True
        if estado in (0, 429) or estado >= 500:            # problema temporal: reintentar
            item["_intentos"] += 1
            espera = min(REINTENTO_MAX_S, REINTENTO_MIN_S * 2 ** (item["_intentos"] - 1))
            if item["vence"] > time.time() + espera:
                self.estadisticas["reintentos"] += 1
                threading.Timer(espera, self._reencolar, args=(item,)).start()
                return False
        self.estadisticas["descartadas"] += 1
        self._log(f"publicación descartada ({estado}): {texto[:120]}")
        return False

    def _reencolar(self, item):
        if self._activo:
            try:
                self.cola.put_nowait(item)
            except queue.Full:
                self.estadisticas["descartadas"] += 1

    def _trabajar(self):
        while self._activo:
            if self._toca_directorio():
                try:
                    self.enviar_directorio()
                except Exception as e:
                    self.estadisticas["errores"] += 1
                    self._log(f"error enviando el directorio: {type(e).__name__}")
            try:
                item = self.cola.get(timeout=1)
            except queue.Empty:
                continue
            if item["vence"] <= time.time():
                self.estadisticas["descartadas"] += 1
                continue
            try:
                self._publicar(item)
            except Exception as e:
                self.estadisticas["errores"] += 1
                self._log(f"error publicando: {type(e).__name__}")
