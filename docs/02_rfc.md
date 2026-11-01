# El RFC

## Qué es
- **Un RFC** (*Request for Comments*) es el tipo de documento con que se definen los protocolos de Internet; HTTP, por ejemplo, es el RFC 9110. Describe un protocolo con tanto detalle que otra persona podría programarlo solo leyéndolo.
- **El enunciado pide** uno para la **Capa 1 (Faro)** y para **FaroNet**, todo en texto ASCII, con secciones de Privacidad (modelo de amenazas) y de Seguridad. Vale 5 % y va dentro del PDF de la documentación.
- **El nuestro ya está escrito y verificado:** `RFC/rfc-faronet.txt`, 27 páginas. Las páginas 5 a 9 son la Sección 2, tu Capa 1: codificación, trama v3, versiones, dirección física, CSMA/CA, ACK, pánico y por qué no UART.

## Archivos
| Archivo | Para qué |
|---|---|
| `RFC/faronet-rfc.fuente.txt` | **Aquí se edita** (con tildes, sin preocuparse por el ancho) |
| `RFC/rfc-faronet.txt` | El RFC final que se entrega. **No editarlo a mano** |
| `RFC/formatear_rfc.py` | Genera el final a partir de la fuente |
| `RFC/verificar_rfc.py` | Comprueba formato, índice, referencias y que los ejemplos coincidan con el código |

## Qué hacer
1. `git pull`.
2. Leer la **Sección 2** de `RFC/rfc-faronet.txt` y corregir en la **fuente** cualquier dato de la Capa 1 que no sea exacto: T, la pausa, el ciclo, los módulos, los tiempos.
3. Poner tu correo en "Direcciones de los autores", al final de la fuente.
4. Regenerar y verificar (no hace falta la Raspberry Pi):
   ```
   python3 RFC/formatear_rfc.py
   python3 RFC/verificar_rfc.py
   ```
   Debe terminar con **`8 de 8 revisiones correctas`**.
5. Hacer commit de los dos archivos (la fuente y `rfc-faronet.txt`).

Si cambias algo de la Capa 1 en el código (por ejemplo, los tiempos de los bits), repite los pasos 2, 4 y 5. El verificador avisa si el RFC y el código dejan de coincidir.
