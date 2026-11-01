#!/usr/bin/env python3
"""
verificar_rfc.py - Comprueba que RFC/rfc-faronet.txt cumple el formato de un
RFC en texto y que sigue coincidiendo con el código.

    python3 RFC/verificar_rfc.py

Revisa:
   1. Solo caracteres ASCII (el enunciado exige el RFC completo en ASCII).
   2. Ninguna línea de más de 72 columnas.
   3. Páginas de 58 líneas como máximo, con encabezado y pie numerado en orden.
   4. Que estén las secciones obligatorias (RFC 7322, privacidad RFC 6973,
      seguridad, IANA, referencias, direcciones de los autores).
   5. Que cada cita [X] tenga su referencia y cada referencia se cite.
   6. Que los números de página del índice sean los correctos.
   7. Que los vectores de prueba del Apéndice A coincidan con lo que calcula
      el código actual (si alguien cambia un formato, esto falla).
   8. Que el RFC esté generado con la fuente actual (que nadie lo editó a mano
      ni olvidó regenerarlo).
No necesita Raspberry Pi: solo Python 3 y PyNaCl.
"""
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
RFC = os.path.join(AQUI, "rfc-faronet.txt")

REQUERIDAS = ["Resumen", "Estado de este memorando", "Tabla de contenidos",
              "1.  Introduccion", "Consideraciones de privacidad",
              "Consideraciones de seguridad", "Consideraciones de IANA",
              "Referencias normativas", "Referencias informativas",
              "Direcciones de los autores"]

resultados = []


def revisar(nombre, ok, detalle=""):
    resultados.append(ok)
    print(f"  [{'OK' if ok else 'FALLA'}] {nombre}" + (f": {detalle}" if detalle and not ok else ""))


def main():
    if not os.path.exists(RFC):
        sys.exit("No existe RFC/rfc-faronet.txt: ejecute primero python3 RFC/formatear_rfc.py")
    crudo = open(RFC, "rb").read()
    print(f"Verificando {RFC}\n")

    malos = sorted({b for b in crudo if b > 126 or (b < 32 and b not in (10, 12))})
    revisar("1. Solo ASCII", not malos, f"bytes no permitidos: {malos[:10]}")
    texto = crudo.decode("ascii", errors="replace")
    lineas = texto.split("\n")

    largas = [i + 1 for i, l in enumerate(lineas) if len(l) > 72]
    revisar("2. Líneas de 72 columnas como máximo", not largas, f"líneas {largas[:10]}")

    paginas = texto.split("\f")
    problemas = []
    for n, pag in enumerate(paginas, 1):
        ls = pag.strip("\n").split("\n")
        if len(ls) > 58:
            problemas.append(f"página {n} tiene {len(ls)} líneas")
        if not re.search(rf"\[Pagina {n}\]\s*$", pag.rstrip()):
            problemas.append(f"página {n} sin pie [Pagina {n}]")
        if n > 1 and not ls[0].startswith("RFC FARONET-1"):
            problemas.append(f"página {n} sin encabezado")
    revisar(f"3. Paginación ({len(paginas)} páginas)", not problemas, "; ".join(problemas[:5]))

    sin_encab = [s for s in REQUERIDAS if not any(l.strip().startswith(s) or
                                                 re.match(rf"^\d+(\.\d+)*\.\s+{re.escape(s)}", l)
                                                 for l in lineas)]
    revisar("4. Secciones obligatorias", not sin_encab, f"faltan {sin_encab}")

    inicio_refs = re.search(r"^10\.1\.  Referencias normativas$", texto, re.M).start()
    fin_refs = re.search(r"^Apendice A\.  Vectores de prueba$", texto, re.M).start()
    refs = set(re.findall(r"^\s{3}\[([A-Za-z][\w.-]*)\]", texto[inicio_refs:fin_refs], re.M))
    cuerpo = texto[:inicio_refs] + texto[fin_refs:]
    citas = set(re.findall(r"\[([A-Za-z][A-Za-z0-9.-]*)\]", cuerpo)) - {"Pagina"}
    sin_ref, sin_cita = sorted(citas - refs), sorted(refs - citas)
    revisar(f"5. Citas y referencias ({len(refs)} referencias)", not sin_ref and not sin_cita,
            f"citas sin referencia {sin_ref}; referencias sin citar {sin_cita}")

    # índice: "título . . . N" -> el título aparece en la página N
    toc_ini = texto.index("Tabla de contenidos")
    toc_fin = texto.index("1.  Introduccion", texto.index("Direcciones de los autores", toc_ini) + 30)
    errores_toc = []
    for linea in texto[toc_ini:toc_fin].split("\n"):
        m = re.match(r"^\s{3,}(\S.*?)\s[ .]*\s(\d+)$", linea)
        if not m:
            continue
        titulo, pagina = m.group(1).rstrip(" ."), int(m.group(2))
        donde = [n for n, pag in enumerate(paginas, 1) if n > 2 and
                 any(l.rstrip() == titulo for l in pag.split("\n"))]
        if pagina not in donde:
            errores_toc.append(f"'{titulo}' dice {pagina}, está en {donde}")
    revisar("6. Números de página del índice", not errores_toc, "; ".join(errores_toc[:5]))

    import vectores
    sin_vector = []
    for nombre in vectores.VECTORES:
        bloque = "\n".join(vectores.calcular(nombre))
        plano = texto.replace("\n\f\n", "\n")
        if bloque not in plano and not all(l in plano for l in bloque.split("\n")):
            sin_vector.append(nombre)
    revisar(f"7. Vectores de prueba = código actual ({len(vectores.VECTORES)} vectores)",
            not sin_vector, f"no coinciden: {sin_vector}")

    import formatear_rfc
    import io
    import contextlib
    salida_original = formatear_rfc.SALIDA
    temporal = RFC + ".tmp"
    formatear_rfc.SALIDA = temporal
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            formatear_rfc.main()
        igual = open(temporal, "rb").read() == crudo
    finally:
        formatear_rfc.SALIDA = salida_original
        if os.path.exists(temporal):
            os.remove(temporal)
    revisar("8. Generado con la fuente actual", igual,
            "regenere con python3 RFC/formatear_rfc.py")

    total, buenos = len(resultados), sum(resultados)
    print(f"\n{buenos} de {total} revisiones correctas.")
    sys.exit(0 if buenos == total else 1)


if __name__ == "__main__":
    main()
