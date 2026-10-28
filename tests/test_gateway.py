"""
Pruebas del gateway y del edge.

    python3 -m unittest discover -s tests -v

Las pruebas que hablan con el Worker usan Node (22.18 o más nuevo) para correr
el código REAL del Worker (Edge/src) con una simulación de Cloudflare en
memoria. Si Node no está instalado, esas pruebas se saltan.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for carpeta in ("Mesh", "App", "Gateway"):
    sys.path.insert(0, os.path.join(RAIZ, carpeta))

import nacl.signing                                # noqa: E402

import buzon_muerto                                # noqa: E402
import verificar_directorio                        # noqa: E402
from directorio import Directorio                  # noqa: E402
from gateway import Gateway, cabeceras_firmadas, mensaje_firmado   # noqa: E402
from identidad import Identidad                    # noqa: E402


def node_sirve():
    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        mayor, menor = (int(x) for x in v.lstrip("v").split(".")[:2])
    except (ValueError, OSError):
        return None
    return node if (mayor, menor) >= (22, 18) else None


NODE = node_sirve()


class NodoFalso:
    """Lo mínimo del nodo que usa el gateway."""

    def __init__(self, n_otros=3):
        self.identidad = Identidad()
        self.directorio = Directorio()
        self.otros = [Identidad() for _ in range(n_otros)]
        for i, o in enumerate(self.otros):
            self.directorio.agregar(o.crear_entrada(f"nodo{i}"))
        self.entrada_propia = self.identidad.crear_entrada("faroB", gateway=True)
        self.al_panico = []


class ServicioFalso:
    def __init__(self):
        self.al_publicacion_publica = []

    def publicar(self, tema, texto):
        ahora = int(time.time())
        evento = {"tema": tema, "texto": texto, "id": os.urandom(8).hex(), "creada": ahora,
                  "vence": ahora + 600}
        for f in self.al_publicacion_publica:
            f(evento)
        return evento


class PruebasFirma(unittest.TestCase):
    def test_firma_verificable(self):
        yo = Identidad()
        cuerpo = b'{"a": 1}'
        cab = cabeceras_firmadas(yo, "/gw/publicacion", cuerpo)
        msg = mensaje_firmado("/gw/publicacion", cab["X-FaroNet-Tiempo"], cab["X-FaroNet-Nonce"], cuerpo)
        nacl.signing.VerifyKey(bytes(yo.verificacion)).verify(msg, bytes.fromhex(cab["X-FaroNet-Firma"]))
        self.assertEqual(len(cab["X-FaroNet-Nonce"]), 32)

    def test_url_debe_ser_https(self):
        with self.assertRaises(ValueError):
            Gateway(NodoFalso(), ServicioFalso(), "http://ejemplo.com")


class PruebasBuzon(unittest.TestCase):
    def test_blob_fijo_y_cifrado(self):
        id_a, clave = buzon_muerto.derivar("frase larga")
        id_b, _ = buzon_muerto.derivar("frase larga")
        self.assertEqual(id_a, id_b)                      # los dos llegan al mismo buzón
        self.assertEqual(len(id_a), 64)
        corto = buzon_muerto.armar_blob(clave, "hola")
        largo = buzon_muerto.armar_blob(clave, "x" * 900)
        self.assertEqual(len(corto), len(largo))          # no se filtra el largo
        self.assertNotIn(b"hola", corto)
        self.assertEqual(buzon_muerto.leer_blob(clave, corto), "hola")


@unittest.skipUnless(NODE, "hace falta Node 22.18+ para correr el Worker")
class PruebasConElWorker(unittest.TestCase):
    """Gateway de Python  ->  HTTP  ->  código real del Worker (simulado en memoria)."""

    @classmethod
    def setUpClass(cls):
        cls.nodo = NodoFalso()
        cls.servicio = ServicioFalso()
        llave = bytes(cls.nodo.identidad.verificacion).hex()
        cls.puerto = 18787
        cls.proc = subprocess.Popen(
            [NODE, os.path.join(RAIZ, "Edge", "prueba", "servidor_local.ts"), llave, str(cls.puerto)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        linea = cls.proc.stdout.readline()
        assert "Worker simulado" in linea, linea
        cls.url = f"http://127.0.0.1:{cls.puerto}"
        cls.gw = Gateway(cls.nodo, cls.servicio, cls.url, intervalo_directorio=3600).iniciar()

    @classmethod
    def tearDownClass(cls):
        cls.gw.detener()
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def get(self, ruta):
        with urllib.request.urlopen(self.url + ruta, timeout=10) as r:
            return r.read().decode()

    def esperar(self, condicion, segundos=10):
        fin = time.time() + segundos
        while time.time() < fin:
            if condicion():
                return True
            time.sleep(0.1)
        return False

    def test_publicacion_publica_llega_a_la_sala_y_al_feed(self):
        self.servicio.publicar("Redes", "hola desde FaroNet <b>")
        self.assertTrue(self.esperar(lambda: "hola desde FaroNet" in self.get("/tema/redes/historial")))
        self.assertIn("&lt;b&gt;", self.get("/tema/redes/feed.xml"))
        self.assertIn("#redes", self.get("/"))
        self.assertEqual(self.gw.estadisticas["publicadas"] >= 1, True)

    def test_directorio_publico_verificable(self):
        self.assertEqual(self.gw.enviar_directorio(), 200)
        datos = json.loads(self.get("/directorio"))
        self.assertEqual(len(datos["nodos"]), 4)                 # 3 nodos + el gateway
        buenas, malas = verificar_directorio.verificar(datos)
        self.assertEqual((buenas, malas), (4, 0))

    def test_otra_llave_no_puede_publicar(self):
        intruso = Gateway(NodoFalso(), ServicioFalso(), self.url)
        estado, _ = intruso._post("/gw/publicacion", {"tema": "x", "texto": "y"})
        self.assertEqual(estado, 401)

    def test_buzon_muerto_de_punta_a_punta(self):
        id_buzon, clave = buzon_muerto.derivar("frase de prueba del buzón")
        url = f"{self.url}/buzon/{id_buzon}"
        estado, _ = buzon_muerto.pedir("PUT", url, buzon_muerto.armar_blob(clave, "nos vemos"), 120)
        self.assertEqual(estado, 201)
        estado, blob = buzon_muerto.pedir("GET", url)
        self.assertEqual(buzon_muerto.leer_blob(clave, blob), "nos vemos")
        self.assertEqual(buzon_muerto.pedir("GET", url)[0], 404)      # se borró al leerlo

    def test_vectores_cruzados_python_typescript(self):
        """Firmas y entradas hechas con PyNaCl, verificadas por el código TypeScript."""
        yo = self.nodo.identidad
        cuerpo = json.dumps({"tema": "a", "texto": "ñandú"}, ensure_ascii=False)
        cab = cabeceras_firmadas(yo, "/gw/publicacion", cuerpo.encode())
        cuerpo_dir = json.dumps({"entradas": [e.a_bytes().hex() for e in self.nodo.directorio.vigentes()]})
        vectores = {
            "llave": bytes(yo.verificacion).hex(), "ruta": "/gw/publicacion", "cuerpo": cuerpo,
            "tiempo": cab["X-FaroNet-Tiempo"], "nonce": cab["X-FaroNet-Nonce"],
            "firma": cab["X-FaroNet-Firma"],
            "entradas": [{"hex": e.a_bytes().hex(), "alias": e.alias, "id": e.id.hex()}
                         for e in self.nodo.directorio.vigentes()],
            "ruta_dir": "/gw/directorio", "cuerpo_dir": cuerpo_dir,
            "cabeceras_dir": cabeceras_firmadas(yo, "/gw/directorio", cuerpo_dir.encode()),
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(vectores, f)
        try:
            r = subprocess.run([NODE, os.path.join(RAIZ, "Edge", "prueba", "simulacion.ts"), f.name],
                               capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(f.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("14 pruebas OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
