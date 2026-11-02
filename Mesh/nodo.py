"""
nodo.py - Un nodo de la mesh FaroNet (capas 2 y 3).

Un nodo se encarga de lo siguiente:
    - Descubre a sus vecinos con HELLO en cada interfaz (UDP, Faro o ambas).
    - Mantiene la tabla de rutas con el vector de distancias.
    - Publica su entrada firmada en el directorio y propaga las de los demás.
    - Reenvía las celdas de la cebolla hacia su destino y, cuando una celda es
      para él, quita la capa que le corresponde.
    - Inunda las publicaciones de los temas y descarta las repetidas.
    - Le entrega a la Capa 7 (App/servicio.py) lo que es para la aplicación.

En cuanto a la privacidad:
    - Los paquetes no llevan campo de origen y siempre miden lo mismo.
    - Antes de reenviar una celda, el nodo espera un tiempo aleatorio (entre 0
      y retardo_max segundos) para dificultar la correlación por tiempos.
    - No se escriben bitácoras. Con --depurar solo se imprimen tipos y
      cantidades, nunca direcciones IP, MACs ni identificadores.
    - El pánico borra las llaves, el directorio, las rutas, los vecinos y todo
      lo de la aplicación, y detiene el nodo.
"""
import random
import threading
import time
from dataclasses import dataclass, field

import nacl.encoding
import nacl.hash

import cebolla
import paquete as P
from directorio import Directorio
from enrutamiento import INFINITO, TablaRutas
from identidad import ErrorEntrada, Entrada, VIDA_ENTRADA_S

REPUBLICAR_CADA_S = VIDA_ENTRADA_S // 3     # el nodo renueva su propia entrada cada 10 min
RECORDAR_VISTOS_S = 2 * 3600                 # cuánto tiempo se recuerdan las publicaciones ya vistas


class ErrorRuta(Exception):
    """No hay suficientes nodos o rutas para armar el camino de la cebolla."""


@dataclass
class Enlace:
    interfaz: object
    direccion: object
    visto: float
    vida: float


@dataclass
class Vecino:
    id: bytes
    enlaces: dict = field(default_factory=dict)      # nombre de la interfaz -> Enlace

    def mejor(self, ahora):
        vivos = [e for e in self.enlaces.values() if ahora - e.visto <= e.vida]
        return min(vivos, key=lambda e: e.interfaz.costo) if vivos else None


class Nodo:
    def __init__(self, identidad, interfaces, alias="", gateway=False, retardo_max=0.5,
                 depurar=False, rotar_mac_cada=1800):
        self.identidad = identidad
        self.id = identidad.id
        self.alias = alias
        self.gateway = gateway
        self.interfaces = list(interfaces)
        self.retardo_max = retardo_max
        self.depurar = depurar
        self.rotar_mac_cada = rotar_mac_cada      # segundos; con 0 la MAC del Faro no rota sola
        self._ultima_rotacion = time.time()

        self.directorio = Directorio()
        self.rutas = TablaRutas(self.id)
        self.vecinos = {}                       # id del vecino -> Vecino
        self._por_direccion = {}                # (interfaz, dirección) -> id del vecino
        self._vistos = {}                       # huella de una publicación -> hora en que se vio
        self._lock = threading.RLock()
        self._activo = True
        self._ultimo_hello = {}
        self._ultimas_rutas = {}
        self._rutas_cambiaron = False
        self._ultimo_disparo = 0.0
        self._disparo_pendiente = set()     # interfaces de Faro con unas RUTAS disparadas por enviar
        self._ultima_entrada = 0.0
        self.entrada_propia = None
        self.panico_activado = False

        # Funciones de la Capa 7, que registra App/servicio.py
        self.al_privado = None                  # recibe el mensaje de 316 B
        self.al_publicacion = None              # recibe la publicación de 365 B y dice si es válida
        self.al_panico = []                     # otras funciones de borrado para el pánico

        self.estadisticas = dict.fromkeys(
            ("paquetes_rx", "paquetes_tx", "descartados", "celdas_reenviadas",
             "celdas_para_mi", "publicaciones_nuevas", "duplicadas", "entradas_nuevas",
             "sin_ruta"), 0)

    # ------------------------------------------------------------ ciclo de vida
    def iniciar(self):
        self._renovar_entrada()
        for interfaz in self.interfaces:
            interfaz.iniciar(self)
        threading.Thread(target=self._mantenimiento, daemon=True).start()
        for interfaz in self.interfaces:
            self.anunciar_en(interfaz)
        return self

    def detener(self):
        self._activo = False
        for interfaz in self.interfaces:
            interfaz.cerrar()

    def _log(self, texto):
        if self.depurar:
            print(f"[nodo] {texto}", flush=True)

    # ------------------------------------------------------------ envío básico
    def _enviar(self, interfaz, direccion, paq, es_control=None):
        crudo = paq.a_bytes()
        es_control = paq.tipo not in (P.TIPO_CELDA, P.TIPO_PUBLICACION) if es_control is None \
            else es_control
        if hasattr(interfaz, "faro"):
            interfaz.enviar(crudo, direccion, es_control=es_control)
        else:
            interfaz.enviar(crudo, direccion)
        self.estadisticas["paquetes_tx"] += 1

    def _enviar_a_vecino(self, id_vecino, paq):
        with self._lock:
            vecino = self.vecinos.get(id_vecino)
            enlace = vecino.mejor(time.time()) if vecino else None
        if enlace is None:
            return False
        self._enviar(enlace.interfaz, enlace.direccion, paq)
        return True

    def _a_todos_los_vecinos(self, paq, excepto=None):
        ahora = time.time()
        with self._lock:
            destinos = [(v.id, v.mejor(ahora)) for v in self.vecinos.values() if v.id != excepto]
        for _id, enlace in destinos:
            if enlace is not None:
                self._enviar(enlace.interfaz, enlace.direccion, paq)

    def _direcciones_en(self, interfaz):
        with self._lock:
            return [e.direccion for v in self.vecinos.values()
                    for e in v.enlaces.values() if e.interfaz is interfaz]

    def anunciar_en(self, interfaz):
        """Envía un HELLO por una interfaz. También se usa después de rotar la
        MAC del Faro."""
        if not self._activo:
            return
        hello = P.armar_hello(self.id, interfaz.intervalo_hello).a_bytes()
        interfaz.anunciar(hello, self._direcciones_en(interfaz))
        self._ultimo_hello[interfaz.nombre] = time.time()

    # ------------------------------------------------------------ recepción
    def recibir_crudo(self, datos, interfaz, direccion):
        if not self._activo:
            return
        try:
            paq = P.Paquete.desde_bytes(datos)
        except P.ErrorPaquete:
            self.estadisticas["descartados"] += 1
            return
        self.estadisticas["paquetes_rx"] += 1
        try:
            if paq.tipo == P.TIPO_HELLO:
                self._recibir_hello(paq, interfaz, direccion)
                return
            with self._lock:
                origen = self._por_direccion.get((interfaz.nombre, direccion))
            if origen is None:
                # Todavía no se sabe quién es, así que se le pide que se presente.
                self.estadisticas["descartados"] += 1
                hello = P.armar_hello(self.id, interfaz.intervalo_hello)
                self._enviar(interfaz, direccion, hello)
                return
            with self._lock:                        # cualquier paquete mantiene vivo el enlace
                enlace = self.vecinos[origen].enlaces.get(interfaz.nombre)
                if enlace:
                    enlace.visto = time.time()
            if paq.tipo == P.TIPO_RUTAS:
                self._recibir_rutas(paq, interfaz, origen)
            elif paq.tipo == P.TIPO_DIRECTORIO:
                self._recibir_entrada(paq, origen)
            elif paq.tipo == P.TIPO_CELDA:
                self._recibir_celda(paq)
            elif paq.tipo == P.TIPO_PUBLICACION:
                self._recibir_publicacion(paq.cuerpo, origen, paq.ttl)
        except P.ErrorPaquete:
            self.estadisticas["descartados"] += 1
        except Exception as e:                          # un paquete defectuoso no debe detener el nodo
            self.estadisticas["descartados"] += 1
            self._log(f"error procesando {paq.nombre}: {type(e).__name__}")

    def _recibir_hello(self, paq, interfaz, direccion):
        id_vecino, intervalo = P.leer_hello(paq.cuerpo)
        if id_vecino == self.id:
            return                                      # es el HELLO del propio nodo
        ahora = time.time()
        vida = 3.5 * max(intervalo, 1)
        with self._lock:
            vecino = self.vecinos.get(id_vecino)
            nuevo_vecino = vecino is None
            if nuevo_vecino:
                vecino = self.vecinos[id_vecino] = Vecino(id_vecino)
            anterior = vecino.enlaces.get(interfaz.nombre)
            nuevo_enlace = anterior is None or anterior.direccion != direccion
            vecino.enlaces[interfaz.nombre] = Enlace(interfaz, direccion, ahora, vida)
            self._por_direccion[(interfaz.nombre, direccion)] = id_vecino
        if self.rutas.actualizar_vecino(id_vecino, interfaz.costo, vida):
            self._rutas_cambiaron = True
        if nuevo_enlace:
            self._log(f"vecino nuevo por {interfaz.nombre} (vecinos: {len(self.vecinos)})")
            # El nodo se presenta, le cuenta sus rutas y le pasa el directorio.
            self._enviar(interfaz, direccion, P.armar_hello(self.id, interfaz.intervalo_hello))
            self._enviar_rutas_a(id_vecino, interfaz, direccion)
            entradas = [self.entrada_propia] + self.directorio.vigentes()
            for entrada in filter(None, entradas):
                self._enviar(interfaz, direccion, P.Paquete(P.TIPO_DIRECTORIO, entrada.a_bytes(),
                                                            ttl=1))

    def _recibir_rutas(self, paq, interfaz, origen):
        lista = P.leer_rutas(paq.cuerpo)
        with self._lock:
            enlace = self.vecinos[origen].enlaces.get(interfaz.nombre)
            mejor = self.vecinos[origen].mejor(time.time())
        if enlace is None or mejor is None or mejor.interfaz is not interfaz:
            return              # solo cuentan las rutas que llegan por el mejor enlace
        vida = 3.5 * interfaz.intervalo_rutas
        if self.rutas.recibir_anuncio(origen, interfaz.costo, lista, vida):
            self._rutas_cambiaron = True

    def _recibir_entrada(self, paq, origen):
        try:
            entrada = Entrada.desde_bytes(paq.cuerpo)
        except ErrorEntrada:
            self.estadisticas["descartados"] += 1
            return
        if entrada.id == self.id:
            return
        if self.directorio.agregar(entrada):
            self.estadisticas["entradas_nuevas"] += 1
            self._log(f"directorio: {len(self.directorio)} nodos")
            self._a_todos_los_vecinos(P.Paquete(P.TIPO_DIRECTORIO, paq.cuerpo, ttl=1),
                                      excepto=origen)

    def _recibir_celda(self, paq):
        if paq.destino == self.id:
            self.estadisticas["celdas_para_mi"] += 1
            self._pelar(paq.cuerpo)
            return
        if paq.ttl <= 1:
            self.estadisticas["descartados"] += 1
            return
        self._reenviar_celda(paq.destino, paq.cuerpo, paq.ttl - 1)

    def _pelar(self, celda):
        try:
            resultado = cebolla.pelar(self.identidad.privada, celda)
        except cebolla.ErrorCebolla:
            self.estadisticas["descartados"] += 1
            return
        accion = resultado[0]
        if accion == "reenviar":
            _, siguiente, celda_nueva = resultado
            demora = random.uniform(0, self.retardo_max) if self.retardo_max else 0
            threading.Timer(demora, self._reenviar_celda,
                            args=(siguiente, celda_nueva, P.TTL_INICIAL)).start()
        elif accion == "difundir":
            self._recibir_publicacion(resultado[1], None, P.TTL_INICIAL + 1)
        elif accion == "app" and self.al_privado:
            self.al_privado(resultado[1])

    def _reenviar_celda(self, destino, celda, ttl):
        if not self._activo:
            return
        if destino == self.id:
            self._pelar(celda)
            return
        siguiente = self.rutas.siguiente_salto(destino)
        paq = P.Paquete(P.TIPO_CELDA, celda, destino=destino, ttl=ttl)
        if siguiente is None or not self._enviar_a_vecino(siguiente, paq):
            self.estadisticas["sin_ruta"] += 1
            return
        self.estadisticas["celdas_reenviadas"] += 1

    def _recibir_publicacion(self, blob, origen, ttl):
        huella = nacl.hash.blake2b(bytes(blob), digest_size=16, encoder=nacl.encoding.RawEncoder)
        with self._lock:
            if huella in self._vistos:
                self.estadisticas["duplicadas"] += 1
                return
            self._vistos[huella] = time.time()
        valida = self.al_publicacion(bytes(blob)) if self.al_publicacion else True
        if not valida:
            self.estadisticas["descartados"] += 1
            return
        self.estadisticas["publicaciones_nuevas"] += 1
        if ttl > 1:
            self._a_todos_los_vecinos(P.Paquete(P.TIPO_PUBLICACION, blob, ttl=ttl - 1),
                                      excepto=origen)

    # ------------------------------------------------------------ envío de la aplicación
    def _elegir_ruta(self, excluir):
        alcanzables = self.rutas.alcanzables()
        candidatos = [e for e in self.directorio.vigentes()
                      if e.id not in excluir and e.id in alcanzables]
        if len(candidatos) < cebolla.SALTOS:
            raise ErrorRuta(f"se necesitan al menos {cebolla.SALTOS} nodos intermedios alcanzables "
                            f"(hay {len(candidatos)}); espere a que se llene el directorio")
        elegidos = random.sample(candidatos, cebolla.SALTOS)
        # Los nodos se eligen al azar, pero se visitan del más cercano al más
        # lejano. Así la celda cruza la luz, que es el enlace más caro y lento,
        # la menor cantidad de veces posible.
        metrica = {r.destino: r.metrica for r in self.rutas.listar()}
        elegidos.sort(key=lambda e: metrica.get(e.id, 0))
        return [(e.id, e.llave_cifrado) for e in elegidos]

    def enviar_privado(self, id_destino, mensaje_app):
        """Envía los 316 bytes de un mensaje privado al nodo destino, pasando
        por 3 nodos intermedios."""
        destino = self.directorio.obtener(id_destino)
        if destino is None:
            raise ErrorRuta("el destino no está en el directorio")
        ruta = self._elegir_ruta({self.id, id_destino})
        if id_destino not in self.rutas.alcanzables():
            raise ErrorRuta("no hay ruta hacia el destino")
        primero, celda = cebolla.armar_privado(ruta, id_destino, destino.llave_cifrado, mensaje_app)
        self._reenviar_celda(primero, celda, P.TTL_INICIAL)
        return [r[0] for r in ruta]

    def publicar(self, publicacion):
        """Publica en un tema. La publicación pasa por 3 nodos intermedios y el
        último (R3) la inunda en la mesh."""
        ruta = self._elegir_ruta({self.id})
        primero, celda = cebolla.armar_publicacion(ruta, publicacion)
        self._reenviar_celda(primero, celda, P.TTL_INICIAL)
        return [r[0] for r in ruta]

    # ------------------------------------------------------------ tareas periódicas
    def _renovar_entrada(self):
        self.entrada_propia = self.identidad.crear_entrada(self.alias, self.gateway)
        self._ultima_entrada = time.time()
        if self.vecinos:
            self._a_todos_los_vecinos(P.Paquete(P.TIPO_DIRECTORIO, self.entrada_propia.a_bytes(),
                                                ttl=1))

    def _enviar_rutas_a(self, id_vecino, interfaz, direccion):
        for paq in P.armar_rutas(self.rutas.vector_para(id_vecino)):
            self._enviar(interfaz, direccion, paq)

    def _enviar_rutas(self, solo_interfaz=None):
        ahora = time.time()
        with self._lock:
            lista = [(v.id, v.mejor(ahora)) for v in self.vecinos.values()]
        for id_vecino, enlace in lista:
            if enlace is None or (solo_interfaz and enlace.interfaz is not solo_interfaz):
                continue
            self._enviar_rutas_a(id_vecino, enlace.interfaz, enlace.direccion)

    def _expirar_vecinos(self):
        ahora = time.time()
        perdidos = []
        with self._lock:
            for vecino in list(self.vecinos.values()):
                for nombre, enlace in list(vecino.enlaces.items()):
                    if ahora - enlace.visto > enlace.vida:
                        del vecino.enlaces[nombre]
                        self._por_direccion.pop((nombre, enlace.direccion), None)
                if not vecino.enlaces:
                    del self.vecinos[vecino.id]
                    perdidos.append(vecino.id)
            for clave in [c for c, i in self._por_direccion.items() if i in perdidos]:
                del self._por_direccion[clave]
        for id_vecino in perdidos:
            self._log(f"vecino perdido (vecinos: {len(self.vecinos)})")
            if self.rutas.quitar_vecino(id_vecino):
                self._rutas_cambiaron = True

    def _mantenimiento(self):
        while self._activo:
            time.sleep(0.5)
            try:
                ahora = time.time()
                for interfaz in self.interfaces:
                    if ahora - self._ultimo_hello.get(interfaz.nombre, 0) >= interfaz.intervalo_hello:
                        self.anunciar_en(interfaz)
                    if ahora - self._ultimas_rutas.get(interfaz.nombre, 0) >= interfaz.intervalo_rutas:
                        self._ultimas_rutas[interfaz.nombre] = ahora
                        self._enviar_rutas(interfaz)
                self._expirar_vecinos()
                if self.rutas.expirar():
                    self._rutas_cambiaron = True
                if self._rutas_cambiaron and ahora - self._ultimo_disparo > 2:
                    # Actualización disparada: por UDP sale de inmediato y por el
                    # Faro, como mucho, una vez cada interfaz.disparo_min segundos.
                    self._rutas_cambiaron = False
                    self._ultimo_disparo = ahora
                    for interfaz in self.interfaces:
                        if hasattr(interfaz, "faro"):
                            self._disparo_pendiente.add(interfaz.nombre)
                        else:
                            self._enviar_rutas(interfaz)
                for interfaz in self.interfaces:
                    if (interfaz.nombre in self._disparo_pendiente and ahora
                            - self._ultimas_rutas.get(interfaz.nombre, 0) >= interfaz.disparo_min):
                        self._disparo_pendiente.discard(interfaz.nombre)
                        self._ultimas_rutas[interfaz.nombre] = ahora
                        self._enviar_rutas(interfaz)
                if self.rotar_mac_cada and ahora - self._ultima_rotacion > self.rotar_mac_cada:
                    self.rotar_mac()
                if ahora - self._ultima_entrada > REPUBLICAR_CADA_S:
                    self._renovar_entrada()
                self.directorio.expirar()
                with self._lock:
                    for h in [h for h, t in self._vistos.items() if ahora - t > RECORDAR_VISTOS_S]:
                        del self._vistos[h]
            except Exception as e:
                self._log(f"error en mantenimiento: {type(e).__name__}: {e}")

    def rotar_mac(self):
        """Cambia la MAC de cada Faro, para que no se pueda seguir al
        dispositivo en el tiempo. El Faro anuncia la MAC nueva y el nodo envía
        un HELLO para que el vecino la asocie con el mismo nodo. Como el
        directorio no guarda MACs, no hace falta actualizarlo."""
        self._ultima_rotacion = time.time()
        nuevas = []
        for interfaz in self.interfaces:
            if hasattr(interfaz, "faro"):
                try:
                    nuevas.append(interfaz.faro.rotar_mac().hex())
                except Exception as e:
                    self._log(f"no se pudo rotar la MAC: {type(e).__name__}")
        return nuevas

    # ------------------------------------------------------------ pánico
    def panico(self):
        """Borra las llaves y todo el estado, y detiene el nodo."""
        if self.panico_activado:
            return
        self.panico_activado = True
        self._activo = False
        # Primero se borra lo de este nodo. Las funciones registradas se llaman
        # al final porque una de ellas puede terminar el programa.
        self.identidad.borrar()
        self.directorio.borrar_todo()
        self.rutas.borrar_todo()
        with self._lock:
            self.vecinos.clear()
            self._por_direccion.clear()
            self._vistos.clear()
        self.entrada_propia = None
        for interfaz in self.interfaces:
            if hasattr(interfaz, "panico"):
                interfaz.panico()                  # el Faro borra su MAC y enciende el LED rojo
        for interfaz in self.interfaces:
            try:
                interfaz.cerrar()
            except Exception:
                pass
        for funcion in list(self.al_panico):
            try:
                funcion()
            except Exception:
                pass

    # ------------------------------------------------------------ información
    def info_vecinos(self):
        ahora = time.time()
        with self._lock:
            salida = []
            for v in self.vecinos.values():
                e = self.directorio.obtener(v.id)
                mejor = v.mejor(ahora)
                salida.append({"id": v.id.hex(), "alias": e.alias if e else "",
                               "enlaces": sorted(v.enlaces),
                               "mejor": mejor.interfaz.nombre if mejor else None})
        return salida

    def info_rutas(self):
        salida = []
        for r in self.rutas.listar():
            e = self.directorio.obtener(r.destino)
            s = self.directorio.obtener(r.siguiente)
            salida.append({"destino": r.destino.hex(), "alias": e.alias if e else "",
                           "metrica": r.metrica, "siguiente": r.siguiente.hex(),
                           "alias_siguiente": s.alias if s else ""})
        return salida

    def info(self):
        faros = {}
        for interfaz in self.interfaces:            # lo que pasa dentro de la luz (Capa 1)
            faro = getattr(interfaz, "faro", None)
            if faro is None:
                continue
            try:
                e = dict(faro.estadisticas)
                cola = getattr(faro, "_cola_salida", None)
                e["en_cola_capa1"] = cola.qsize() if cola is not None else -1
                datos, control = interfaz.pendientes()      # la fila de la mesh, antes de llegar al Faro
                e["fila_mesh_datos"], e["fila_mesh_control"] = datos, control
                faros[interfaz.nombre] = e
            except Exception:
                pass
        return {"id": self.id.hex(), "alias": self.alias, "gateway": self.gateway,
                "faro": faros,
                "interfaces": [str(i) for i in self.interfaces],
                "vecinos": len(self.vecinos), "rutas": len(self.rutas.listar()),
                "directorio": len(self.directorio), "estadisticas": dict(self.estadisticas),
                "infinito": INFINITO}
