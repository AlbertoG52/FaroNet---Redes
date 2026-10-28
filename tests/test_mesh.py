"""
Pruebas de las capas 2/3 (mesh) y 7 (aplicación). No necesitan Raspberry Pi
ni red: usan una "red en memoria".

    python3 -m unittest discover -s tests -v
"""
import os
import queue
import sys
import tempfile
import threading
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for carpeta in ("Mesh", "App", "Faro"):
    sys.path.insert(0, os.path.join(RAIZ, carpeta))

import cebolla                              # noqa: E402
import mensajes as M                        # noqa: E402
import paquete as P                         # noqa: E402
from directorio import Directorio           # noqa: E402
from enrutamiento import INFINITO, TablaRutas   # noqa: E402
from identidad import Entrada, ErrorEntrada, Identidad   # noqa: E402
from nodo import ErrorRuta, Nodo            # noqa: E402
from servicio import ServicioChat           # noqa: E402


# ---------------------------------------------------------------- red en memoria
class Hub:
    """Entrega paquetes entre interfaces de memoria, en un hilo aparte."""

    def __init__(self):
        self.puntos = {}
        self.cola = queue.Queue()
        threading.Thread(target=self._repartir, daemon=True).start()

    def _repartir(self):
        while True:
            destino, datos, origen = self.cola.get()
            interfaz = self.puntos.get(destino)
            if interfaz and interfaz.nodo:
                interfaz.nodo.recibir_crudo(datos, interfaz, origen)


class InterfazMemoria:
    def __init__(self, hub, nombre_punto, enlaces, costo=1, nombre="mem"):
        self.hub, self.punto, self.enlaces = hub, nombre_punto, list(enlaces)
        self.nombre, self.costo = nombre, costo
        self.intervalo_hello, self.intervalo_rutas = 1, 1
        self.nodo = None
        hub.puntos[nombre_punto] = self

    def iniciar(self, nodo):
        self.nodo = nodo

    def enviar(self, datos, direccion):
        self.hub.cola.put((direccion, datos, self.punto))

    def anunciar(self, datos, direcciones=()):
        for d in set(self.enlaces) | set(direcciones):
            self.enviar(datos, d)

    def cerrar(self):
        self.hub.puntos.pop(self.punto, None)


def esperar(condicion, segundos=15):
    fin = time.time() + segundos
    while time.time() < fin:
        if condicion():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- unidades
class PruebasPaquete(unittest.TestCase):
    def test_tamanos_fijos_y_relleno_aleatorio(self):
        a = P.Paquete(P.TIPO_RUTAS, b"\x00").a_bytes()
        b = P.Paquete(P.TIPO_RUTAS, b"\x00").a_bytes()
        self.assertEqual(len(a), P.TAM_CONTROL)
        self.assertNotEqual(a, b)                      # el relleno es aleatorio
        self.assertEqual(len(P.Paquete(P.TIPO_CELDA, b"x" * 536).a_bytes()), P.TAM_DATOS)
        self.assertEqual(P.Paquete.desde_bytes(a).cuerpo, b"\x00")

    def test_rechazos(self):
        crudo = bytearray(P.Paquete(P.TIPO_HELLO, b"abc").a_bytes())
        crudo[0] = 9
        with self.assertRaises(P.ErrorPaquete):
            P.Paquete.desde_bytes(bytes(crudo))         # versión desconocida
        with self.assertRaises(P.ErrorPaquete):
            P.Paquete.desde_bytes(b"x" * 100)           # tamaño inválido
        with self.assertRaises(P.ErrorPaquete):
            P.Paquete(P.TIPO_HELLO, b"x" * 300).a_bytes()

    def test_rutas_en_varios_paquetes(self):
        lista = [(bytes([i]) * 8, i % 32) for i in range(50)]
        paqs = P.armar_rutas(lista)
        self.assertEqual(len(paqs), 3)
        self.assertEqual(sum((P.leer_rutas(p.cuerpo) for p in paqs), []), lista)


class PruebasCebolla(unittest.TestCase):
    def setUp(self):
        self.ids = [Identidad() for _ in range(6)]
        self.ruta = [(i.id, i.publica) for i in self.ids[1:4]]
        self.dest = self.ids[4]

    def test_cada_nodo_quita_solo_su_capa(self):
        app = os.urandom(cebolla.TAM_MENSAJE_APP)
        primero, celda = cebolla.armar_privado(self.ruta, self.dest.id, self.dest.publica, app)
        self.assertEqual(primero, self.ids[1].id)
        intruso = self.ids[5]
        for i, nodo in enumerate(self.ids[1:4]):
            self.assertEqual(len(celda), cebolla.TAM_CELDA)       # siempre el mismo tamaño
            with self.assertRaises(cebolla.ErrorCebolla):
                cebolla.pelar(intruso.privada, celda)            # otro nodo no la abre
            accion, siguiente, celda = cebolla.pelar(nodo.privada, celda)
            self.assertEqual(accion, "reenviar")
            esperado = self.ids[i + 2].id if i < 2 else self.dest.id
            self.assertEqual(siguiente, esperado)
        self.assertEqual(cebolla.pelar(self.dest.privada, celda), ("app", app))

    def test_publicacion_la_difunde_r3(self):
        pub = M.armar_publicacion("redes", "hola")
        _, celda = cebolla.armar_publicacion(self.ruta, pub)
        for nodo in self.ids[1:3]:
            celda = cebolla.pelar(nodo.privada, celda)[2]
        self.assertEqual(cebolla.pelar(self.ids[3].privada, celda), ("difundir", pub))

    def test_celda_alterada(self):
        _, celda = cebolla.armar_privado(self.ruta, self.dest.id, self.dest.publica,
                                         bytes(cebolla.TAM_MENSAJE_APP))
        dañada = bytearray(celda)
        dañada[100] ^= 1
        with self.assertRaises(cebolla.ErrorCebolla):
            cebolla.pelar(self.ids[1].privada, bytes(dañada))


class PruebasDirectorio(unittest.TestCase):
    def test_firma_e_id(self):
        yo = Identidad()
        crudo = yo.crear_entrada("ana").a_bytes()
        self.assertEqual(Entrada.desde_bytes(crudo).alias, "ana")
        for posicion in (5, 30, len(crudo) - 3):           # id, llave, firma
            malo = bytearray(crudo)
            malo[posicion] ^= 1
            with self.assertRaises(ErrorEntrada):
                Entrada.desde_bytes(bytes(malo))

    def test_suplantacion(self):
        """Firmar con mi llave una entrada con el id de otro: se rechaza."""
        victima, atacante = Identidad(), Identidad()
        e = victima.crear_entrada("victima")
        e.llave_firma = bytes(atacante.verificacion)
        e.firmada_por(atacante)
        with self.assertRaises(ErrorEntrada):
            Entrada.desde_bytes(e.a_bytes())

    def test_mas_nueva_y_vencida(self):
        yo, d = Identidad(), Directorio()
        vieja = yo.crear_entrada("v1")
        self.assertTrue(d.agregar(vieja))
        self.assertFalse(d.agregar(vieja))
        nueva = yo.crear_entrada("v2")
        nueva.creada += 1
        nueva.firmada_por(yo)
        self.assertTrue(d.agregar(nueva))
        self.assertEqual(d.obtener(yo.id).alias, "v2")
        self.assertFalse(d.agregar(yo.crear_entrada("x", vida=-10)))
        d.expirar(ahora=time.time() + 3600)
        self.assertEqual(len(d), 0)


class PruebasRutas(unittest.TestCase):
    def test_vector_distancias(self):
        a, b, c = b"A" * 8, b"B" * 8, b"C" * 8
        t = TablaRutas(a)
        t.actualizar_vecino(b, 2, 10)
        t.recibir_anuncio(b, 2, [(b, 0), (c, 10), (a, 2)], 10)
        self.assertEqual(t.siguiente_salto(c), b)
        self.assertEqual({r.destino: r.metrica for r in t.listar()}, {b: 2, c: 12})
        # horizonte dividido: a B se le anuncia C como inalcanzable
        self.assertIn((c, INFINITO), t.vector_para(b))
        # B avisa que C se cayó
        t.recibir_anuncio(b, 2, [(c, INFINITO)], 10)
        self.assertIsNone(t.siguiente_salto(c))
        t.quitar_vecino(b)
        self.assertEqual(t.listar(), [])


class PruebasMensajes(unittest.TestCase):
    def test_privado_identificado_y_anonimo(self):
        ana, beto = Identidad(), Identidad()
        llaves = {ana.id: ana.publica}
        m = M.leer_privado(M.armar_privado("hola ñ", identidad=ana, llave_destino=beto.publica),
                           beto, llaves.get)
        self.assertEqual((m.texto, m.remitente, m.verificado), ("hola ñ", ana.id, True))
        m = M.leer_privado(M.armar_privado("anon"), beto, llaves.get)
        self.assertEqual((m.texto, m.remitente), ("anon", None))
        with self.assertRaises(M.ErrorMensaje):
            M.armar_privado("x" * (M.MAX_TEXTO_PRIVADO + 1), identidad=ana,
                            llave_destino=beto.publica)
        # un tercero que se hace pasar por ana no pasa la verificación
        falso = Identidad()
        datos = M.armar_privado("soy ana", identidad=falso, llave_destino=beto.publica)
        datos = datos[:3] + ana.id + datos[11:]
        with self.assertRaises(M.ErrorMensaje):
            M.leer_privado(datos, beto, llaves.get)

    def test_temas(self):
        p = M.leer_publicacion(M.armar_publicacion("Redes", "público"))
        self.assertEqual((p.tema, p.texto()), ("redes", "público"))
        clave = M.clave_de_tema("grupo", "frase larga")
        p = M.leer_publicacion(M.armar_publicacion("grupo", "secreto", clave))
        self.assertNotIn(b"grupo", p.crudo)               # ni el nombre del tema viaja
        self.assertNotIn(b"secreto", p.crudo)
        self.assertEqual(p.texto(clave), "secreto")
        with self.assertRaises(M.ErrorMensaje):
            p.texto(M.clave_de_tema("grupo", "otra"))


# ---------------------------------------------------------------- red completa
class PruebasRed(unittest.TestCase):
    """Dos comunidades unidas por un solo enlace caro (como el Faro):

        a1 - a2 - a3 - pA ====(costo 10)==== pB - b1 - b2
    """

    @classmethod
    def setUpClass(cls):
        cls.carpeta = tempfile.mkdtemp()
        hub = Hub()
        enlaces = {"a1": ["a2"], "a2": ["a1", "a3"], "a3": ["a2", "pA"], "pA": ["a3"],
                   "pB": ["b1"], "b1": ["pB", "b2"], "b2": ["b1"]}
        cls.nodos, cls.servicios = {}, {}
        for nombre, vecinos in enlaces.items():
            interfaces = [InterfazMemoria(hub, nombre, vecinos, costo=1)]
            if nombre in ("pA", "pB"):
                otro = "pB:luz" if nombre == "pA" else "pA:luz"
                interfaces.append(InterfazMemoria(hub, f"{nombre}:luz", [otro], costo=10,
                                                  nombre="luz"))
            ident = Identidad(os.path.join(cls.carpeta, f"{nombre}.llave"))
            nodo = Nodo(ident, interfaces, alias=nombre, retardo_max=0.05)
            cls.servicios[nombre] = ServicioChat(nodo)
            cls.nodos[nombre] = nodo.iniciar()
        ok = esperar(lambda: all(len(n.directorio) == 6 and len(n.rutas.listar()) == 6
                                 for n in cls.nodos.values()), 20)
        assert ok, "la red no convergió"

    @classmethod
    def tearDownClass(cls):
        for n in cls.nodos.values():
            n.detener()

    def test_rutas_cruzan_por_el_enlace_caro(self):
        a1 = self.nodos["a1"]
        costo = {r.destino: r.metrica for r in a1.rutas.listar()}
        self.assertEqual(costo[self.nodos["b2"].id], 1 + 1 + 1 + 10 + 1 + 1)

    def test_privado_entre_comunidades(self):
        r = self.servicios["a1"].atender({"cmd": "privado", "para": "b2", "texto": "hola b2"})
        self.assertTrue(r["ok"], r)
        buzon = self.servicios["b2"].buzon
        self.assertTrue(esperar(lambda: any(m["texto"] == "hola b2" for m in buzon)))
        m = [m for m in buzon if m["texto"] == "hola b2"][0]
        self.assertTrue(m["verificado"])
        self.assertEqual(m["alias"], "a1")

    def test_publicacion_llega_a_todos(self):
        r = self.servicios["b1"].atender({"cmd": "publicar", "tema": "redes", "texto": "a todos"})
        self.assertTrue(r["ok"], r)
        for nombre, s in self.servicios.items():
            self.assertTrue(esperar(lambda: s.atender({"cmd": "leer", "tema": "redes"})
                                    ["publicaciones"]), nombre)

    def test_tema_privado(self):
        self.servicios["a2"].atender({"cmd": "publicar", "tema": "club", "frase": "f",
                                      "texto": "secreto"})
        s = self.servicios["b2"]
        pedido = {"cmd": "leer", "tema": "club", "frase": "f"}
        self.assertTrue(esperar(lambda: s.atender(pedido)["publicaciones"]))
        sin = s.atender({"cmd": "leer", "tema": "club"})["publicaciones"]
        con = s.atender(pedido)["publicaciones"]
        self.assertEqual(sin, [])
        self.assertEqual([p["texto"] for p in con], ["secreto"])

    def test_zz_panico(self):
        nodo = self.nodos["b2"]
        archivo = nodo.identidad.archivo
        self.assertTrue(os.path.exists(archivo))
        self.servicios["b2"].buzon.append({"texto": "x", "vence": time.time() + 99})
        nodo.panico()
        self.assertFalse(os.path.exists(archivo))
        self.assertEqual(self.servicios["b2"].buzon, [])
        self.assertEqual(len(nodo.directorio), 0)
        self.assertFalse(self.servicios["b2"].atender({"cmd": "buzon"})["ok"])
        # los demás dejan de verlo cuando su HELLO vence
        self.assertTrue(esperar(lambda: nodo.id not in self.nodos["b1"].vecinos, 10))

    def test_sin_suficientes_nodos(self):
        solo = Nodo(Identidad(), [], alias="solo")
        with self.assertRaises(ErrorRuta):
            solo.publicar(M.armar_publicacion("t", "x"))


if __name__ == "__main__":
    unittest.main()
