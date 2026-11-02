#!/usr/bin/env python3
"""
formatear_rfc.py - Convierte la fuente del RFC, escrita en UTF-8 para que sea
cómoda de editar, en el RFC final en ASCII con el formato clásico de texto:

    - solo caracteres ASCII: las tildes y la ñ se quitan ("versión" queda "version");
    - líneas de 72 columnas como máximo;
    - páginas de 58 líneas separadas por un salto de página (form feed), con
      encabezado y un pie de página numerado;
    - una tabla de contenidos con el número de página de cada sección;
    - vectores de prueba calculados con el código real ({{VECTOR:NOMBRE}}).

    python3 RFC/formatear_rfc.py          # genera RFC/rfc-faronet.txt

La fuente usa estas marcas:
    #...                        comentario, que no aparece en el RFC
    1.  Título / 1.1.  Título   encabezados numerados, que se agregan al índice
    Apéndice A.  Título         apéndices, que también van al índice
    =Título=                    encabezado sin número que va al índice, como "Direcciones de los autores"
    texto normal                párrafo, que se ajusta a 72 columnas con sangría de 3
    - texto  o  1. texto        viñeta o lista numerada, con sangría francesa
    ~~~ ... ~~~                 bloque preformateado que se copia tal cual (diagramas y tablas)
    {{TOC}}                     lugar donde va la tabla de contenidos
    {{VECTOR:NOMBRE}}           vector de prueba (ver vectores.py)
    {{PAGINA}}                  salto de página forzado
"""
import os
import re
import sys
import textwrap
import unicodedata

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

ANCHO = 72
LINEAS_POR_PAGINA = 58
FUENTE = os.path.join(AQUI, "faronet-rfc.fuente.txt")
SALIDA = os.path.join(AQUI, "rfc-faronet.txt")

ENCABEZADO = ("RFC FARONET-1", "FaroNet: Faro y red mesh anonima", "Noviembre 2026")
PIE = ("Guerrero & Alvarado", "Experimental", "Pagina")

RE_SECCION = re.compile(r"^(\d+(\.\d+)*\.)\s{2}\S")
RE_APENDICE = re.compile(r"^(Apéndice|Apendice) [A-Z](\.\d+)*\.\s{2}\S")
RE_SUBAPENDICE = re.compile(r"^[A-Z](\.\d+)+\.\s{2}\S")

REEMPLAZOS = {"¿": "", "¡": "", "“": '"', "”": '"', "‘": "'", "’": "'", "—": "--", "–": "-",
              "…": "...", "→": "->", "←": "<-", "×": "x", "·": "-", "≥": ">=", "≤": "<=",
              "±": "+/-", "µ": "u", "°": " grados", "€": "EUR", "≈": "~"}


def a_ascii(texto):
    for a, b in REEMPLAZOS.items():
        texto = texto.replace(a, b)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    malos = sorted({c for c in texto if ord(c) > 126 or (ord(c) < 32 and c not in "\n\f")})
    if malos:
        raise SystemExit(f"Caracteres no ASCII sin conversión: {malos}")
    return texto


def linea_de_tres(izq, centro, der, ancho=ANCHO):
    espacio = ancho - len(izq) - len(der)
    centro = centro.center(espacio)
    return (izq + centro + der)[:ancho]


def leer_bloques(texto):
    """Divide la fuente en bloques y devuelve una lista de pares (tipo, líneas).
    Los tipos son titulo, parrafo, vineta, pre, toc y salto."""
    import vectores
    bloques = []
    lineas = texto.split("\n")
    i = 0
    parrafo = []

    def cerrar_parrafo():
        if parrafo:
            bloques.append(("parrafo", [" ".join(x.strip() for x in parrafo)]))
            parrafo.clear()

    while i < len(lineas):
        l = lineas[i].rstrip()
        if l.startswith("#"):
            i += 1
            continue
        if l.strip() == "~~~":
            cerrar_parrafo()
            j = i + 1
            pre = []
            while lineas[j].rstrip() != "~~~":
                pre.append(lineas[j].rstrip())
                j += 1
            bloques.append(("pre", pre))
            i = j + 1
            continue
        if not l.strip():
            cerrar_parrafo()
            i += 1
            continue
        m = re.match(r"^\s*\{\{VECTOR:(\w+)\}\}$", l)
        if m:
            cerrar_parrafo()
            bloques.append(("pre", vectores.calcular(m.group(1))))
            i += 1
            continue
        if l.strip() == "{{TOC}}":
            cerrar_parrafo()
            bloques.append(("toc", []))
            i += 1
            continue
        if l.strip() == "{{PAGINA}}":
            cerrar_parrafo()
            bloques.append(("salto", []))
            i += 1
            continue
        if (RE_SECCION.match(l) or RE_APENDICE.match(l) or RE_SUBAPENDICE.match(l)
                or (l.startswith("=") and l.endswith("="))):
            cerrar_parrafo()
            bloques.append(("titulo", [l.strip("=") if l.startswith("=") else l]))
            i += 1
            continue
        if re.match(r"^\s*(-|\d+\.)\s+\S", l) and not parrafo:
            cerrar_parrafo()
            items = [l.strip()]
            i += 1
            while i < len(lineas) and lineas[i].strip() and not re.match(
                    r"^\s*(-|\d+\.)\s+\S", lineas[i]) and not lineas[i].strip() == "~~~":
                items[-1] += " " + lineas[i].strip()
                i += 1
            bloques.append(("vineta", items))
            continue
        parrafo.append(l)
        i += 1
    cerrar_parrafo()
    return bloques


def renderizar(bloques, toc_lineas):
    """Convierte los bloques en líneas de texto, todavía sin paginar. Devuelve
    una lista de tuplas (linea, mantener_con_siguiente, es_titulo_texto)."""
    salida = []
    for tipo, contenido in bloques:
        if tipo == "titulo":
            salida.append((contenido[0], True, contenido[0]))
            salida.append(("", False, None))
        elif tipo == "parrafo":
            texto = contenido[0]
            for linea in textwrap.wrap(texto, ANCHO - 3, break_on_hyphens=False):
                salida.append(("   " + linea, False, None))
            salida.append(("", False, None))
        elif tipo == "vineta":
            for item in contenido:
                m = re.match(r"^(-|\d+\.)\s+(.*)$", item)
                marca, texto = m.group(1), m.group(2)
                prefijo = f"   {marca} "
                sangria = " " * len(prefijo)
                envuelto = textwrap.wrap(texto, ANCHO - len(prefijo), break_on_hyphens=False)
                for k, linea in enumerate(envuelto):
                    salida.append(((prefijo if k == 0 else sangria) + linea, False, None))
                salida.append(("", False, None))
        elif tipo == "pre":
            n = len(contenido)
            for k, linea in enumerate(contenido):
                salida.append((linea, k < n - 1 and n <= 40, None))   # los bloques cortos no se parten
            salida.append(("", False, None))
        elif tipo == "toc":
            for linea in toc_lineas:
                salida.append((linea, False, None))
            salida.append(("", False, None))
        elif tipo == "salto":
            salida.append(("\f", False, None))
    return salida


def paginar(lineas):
    """Reparte las líneas en páginas sin partir los bloques que deben ir juntos.
    Devuelve las páginas y la página en que quedó cada título."""
    cuerpo_max = LINEAS_POR_PAGINA - 6            # se descuentan el encabezado (2), el pie (3) y un margen
    paginas, actual, titulos = [], [], {}
    i = 0
    while i < len(lineas):
        texto, unido, titulo = lineas[i]
        if texto == "\f":
            if actual:
                paginas.append(actual)
                actual = []
            i += 1
            continue
        # Tamaño del grupo de líneas que debe ir junto.
        j = i
        while j < len(lineas) and lineas[j][1]:
            j += 1
        grupo = lineas[i:j + 1]
        if titulo:                                 # un título nunca queda solo al final de una página
            grupo = lineas[i:min(j + 4, len(lineas))]
        if len(actual) + len(grupo) > cuerpo_max and len(grupo) <= cuerpo_max and actual:
            paginas.append(actual)
            actual = []
        if not actual and not texto.strip():
            i += 1                                 # una página no empieza con una línea en blanco
            continue
        if titulo:
            titulos[titulo] = len(paginas) + 1
        actual.append(texto)
        if len(actual) >= cuerpo_max:
            paginas.append(actual)
            actual = []
        i += 1
    if actual:
        paginas.append(actual)
    return paginas, titulos


def armar_toc(titulos_orden, paginas_de):
    """Arma el índice, con los puntos alineados en columnas fijas y el número
    de página pegado al margen derecho (columna 72)."""
    lineas = []
    for t in titulos_orden:
        if RE_SECCION.match(t):
            nivel = t.split()[0].count(".") - 1
        elif RE_SUBAPENDICE.match(t):
            nivel = 1
        else:
            nivel = 0
        izq = "   " + "   " * nivel + t
        pagina = str(paginas_de.get(t, 0))
        relleno = ANCHO - len(izq) - len(pagina)
        if relleno < 4:
            izq = izq[:ANCHO - len(pagina) - 6].rstrip()
            relleno = ANCHO - len(izq) - len(pagina)
        puntos = [" " if (len(izq) + k) % 2 == 0 else "." for k in range(relleno)]
        puntos[0] = puntos[-1] = " "
        lineas.append(izq + "".join(puntos) + pagina)
    return lineas


def main():
    fuente = a_ascii(open(FUENTE, encoding="utf-8").read())
    bloques = leer_bloques(fuente)
    titulos = [c[0] for t, c in bloques if t == "titulo"]
    # El índice lista lo que va después de él.
    idx_toc = next(k for k, (t, _) in enumerate(bloques) if t == "toc")
    titulos_toc = [c[0] for t, c in bloques[idx_toc:] if t == "titulo"]

    paginas_de = {t: 0 for t in titulos_toc}
    for _ in range(3):                            # se repite hasta que los números de página se estabilizan
        toc = armar_toc(titulos_toc, paginas_de)
        paginas, nuevas = paginar(renderizar(bloques, toc))
        if all(nuevas.get(t) == paginas_de.get(t) for t in titulos_toc):
            break
        paginas_de = nuevas

    salida = []
    for n, cuerpo in enumerate(paginas, 1):
        hoja = []
        if n > 1:
            hoja += [linea_de_tres(*ENCABEZADO), ""]
        hoja += cuerpo
        while len(hoja) < LINEAS_POR_PAGINA - 2:
            hoja.append("")
        hoja += ["", linea_de_tres(PIE[0], PIE[1], f"[{PIE[2]} {n}]")]
        salida.append("\n".join(l.rstrip() for l in hoja))
    texto = "\n\f\n".join(salida) + "\n"
    largas = [l for l in texto.split("\n") if len(l) > ANCHO]
    if largas:
        raise SystemExit("Líneas de más de 72 columnas:\n" + "\n".join(largas[:10]))
    open(SALIDA, "w", encoding="ascii", newline="\n").write(texto)
    print(f"RFC generado: {SALIDA} ({len(paginas)} páginas, {len(titulos)} secciones)")


if __name__ == "__main__":
    main()
