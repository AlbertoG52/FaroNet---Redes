"""El RFC (RFC/rfc-faronet.txt) cumple el formato y coincide con el código."""
import os
import subprocess
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PruebaRFC(unittest.TestCase):
    def test_rfc_valido_y_al_dia(self):
        r = subprocess.run([sys.executable, os.path.join(RAIZ, "RFC", "verificar_rfc.py")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
