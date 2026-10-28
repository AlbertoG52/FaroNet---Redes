"""
nodo.py - Un nodo de la mesh FaroNet (capas 2 y 3).

Qué hace un nodo:
    - Descubre vecinos con HELLO en cada interfaz (UDP y/o Faro).
    - Mantiene la tabla de rutas por vector de distancias.
    - Publica su entrada firmada en el directorio y propaga las de los demás.
    - Reenvía celdas cebolla hacia su destino y quita la capa que le toca
      cuando la celda es para él.
    - Inunda las publicaciones de los temas (con detección de duplicados).
    - Entrega a la Capa 7 (App/servicio.py) lo que es para la aplicación.

Privacidad:
    - Los paquetes no llevan campo de origen y tienen tamaño fijo.
    - Al reenviar una celda se espera un tiempo aleatorio (0..retardo_max s)
      para dificultar la correlación por tiempos.
    - No se escriben bitácoras: con --depurar solo se imprimen tipos y
      cantidades, nunca direcciones IP, MACs ni identificadores.
    - Pánico: borra llaves, directorio, rutas, vecinos y todo lo de la
      aplicación, y detiene el nodo.
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

REPUBLICAR_CADA_S = VIDA_ENTRADA_S // 3     # la entrada propia se renueva cada 10 min
RECORDAR_VISTOS_S = 2 * 3600                 # memoria de publicaciones ya vistas


class ErrorRuta(Exception):
    """No hay suficientes nodos o rutas para armar el circuito."""


@dataclass
class Enlace:
    interfaz: object
    direccion: object
    visto: float
    vida: float


@dataclass
class Vecino:
    id: bytes
    enlaces: dict = field(default_factory=dict)      # nombre interfaz -> Enlace

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
        self.rotar_mac_cada = rotar_mac_cada      # s; 0 = no rotar la MAC del Faro sola
        self._ultima_rotacion = time.time()

        self.directorio = Directorio()
        self.rutas = TablaRutas(self.id)
        self.vecinos = {}                       # id -> Vecino
        self._por_direccion = {}                # (interfaz, direccion) -> id vecino
        self._vistos = {}                       # huella de publicación -> hora
        self._lock = threading.RLock()
        self._activo = True
        self._ultimo_hello = {}
        self._ultimas_rutas = {}
        self._rutas_cambiaron = False
        self._ultimo_disparo = 0.0
        self._ultima_entrada = 0.0
        self.entrada_propia = None
        self.panico_activado = False

        # Capa 7: funciones que pone App/servicio.py
        self.al_privado = None                  # f(mensaje_316B)
        self.al_publicacion = None              # f(publicacion_365B) -> bool (válida)
        self.al_panico = []                     # funciones extra de borrado

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
        """HELLO por una interfaz (también se llama al rotar la MAC del Faro)."""
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
                # todavía no sabemos quién es: pedirle que se presente
                self.estadisticas["descartados"] += 1
                hello = P.armar_hello(self.id, interfaz.intervalo_hello)
                self._enviar(interfaz, direccion, hello)
                return
            with self._lock:                        # cualquier paquete renueva el enlace
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
        except Exception as e:                          # un paquete malo no tumba el nodo
            self.estadisticas["descartados"] += 1
            self._log(f"error procesando {paq.nombre}: {type(e).__name__}")

    def _recibir_hello(self, paq, interfaz, direccion):
        id_vecino, intervalo = P.leer_hello(paq.cuerpo)
        if id_vecino == self.id:
            return                                      # nuestro propio HELLO
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
            # presentarse, contarle las rutas y pasarle el directorio
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
            return              # solo se usan las rutas que llegan por el mejor enlace
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
        # Se recorren de los más cercanos a los más lejanos: así la celda cruza
        # la luz (el enlace más caro y lento) las menos veces posibles. Los
        # nodos se eligen al azar; solo cambia el orden en que se visitan.
        metrica = {r.destino: r.metrica for r in self.rutas.listar()}
        elegidos.sort(key=lambda e: metrica.get(e.id, 0))
        return [(e.id, e.llave_cifrado) for e in elegidos]

    def enviar_privado(self, id_destino, mensaje_app):
        """Envía 316 bytes de la aplicación al nodo destino por 3 saltos."""
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
        """Publica en un tema: la publicación sale por 3 saltos y la inunda R3."""
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
                    # actualización disparada (solo por UDP; el Faro espera su turno)
                    self._rutas_cambiaron = False
                    self._ultimo_disparo = ahora
                    for interfaz in self.interfaces:
                        if not hasattr(interfaz, "faro"):
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
        """Cambia la MAC de cada Faro (privacidad de la Capa 1). El Faro la
        anuncia y el nodo manda un HELLO para que el vecino la asocie al mismo
        nodo. El directorio no guarda MACs, así que no hay que actualizarlo."""
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
        """Borra llaves y todo el estado, y detiene el nodo."""
        if self.panico_activado:
            return
        self.panico_activado = True
        self._activo = False
        # Primero lo de este nodo; las funciones registradas van al final porque
        # una de ellas puede terminar el programa.
        self.identidad.borrar()
        self.directorio.borrar_todo()
        self.rutas.borrar_todo()
        with self._lock:
            self.vecinos.clear()
            self._por_direccion.clear()
            self._vistos.clear()
        self.entrada_propia = None
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
        return {"id": self.id.hex(), "alias": self.alias, "gateway": self.gateway,
                "interfaces": [str(i) for i in self.interfaces],
                "vecinos": len(self.vecinos), "rutas": len(self.rutas.listar()),
                "directorio": len(self.directorio), "estadisticas": dict(self.estadisticas),
                "infinito": INFINITO}
