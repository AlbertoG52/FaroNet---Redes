# FaroNet: mesh anónima (capas 2 y 3) y aplicación anónima (capa 7)

Este código va **encima** de la Capa 1 (`Faro/DispositivoLuzAdaptador.py`) y la usa solo a través de su contrato: `enviar()`, `al_recibir()`, `rotar_mac()`, `registrar_borrado()`. El Faro es solo **una interfaz más** de la mesh, igual que el WiFi o el Ethernet.

```
 Capa 7   App/chat.py  (cliente de terminal)  ──API local 127.0.0.1──>  App/servicio.py
                                                                         App/mensajes.py
 Capa 3   Mesh/nodo.py: enrutamiento, directorio, cebolla, inundación de temas
 Capa 2   Mesh/interfaces.py:   InterfazUDP (Ethernet/WiFi)   InterfazFaro (luz)
 Capa 1                                                       Faro/DispositivoLuzAdaptador.py
```

| Archivo | Qué hace |
|---|---|
| `Mesh/paquete.py` | Formato del paquete FaroNet v1: **tamaño fijo** (220 B control / 550 B datos), sin campo de origen, relleno aleatorio |
| `Mesh/identidad.py` | Llaves Ed25519/X25519 del nodo, id = BLAKE2b(llave)[:8], **entradas del directorio firmadas** |
| `Mesh/directorio.py` | Directorio de nodos distribuido (por inundación), con vencimiento |
| `Mesh/enrutamiento.py` | **Vector de distancias** (Bellman-Ford, como RIP) con horizonte dividido envenenado |
| `Mesh/cebolla.py` | **Onion routing de 3 saltos** con SealedBox, celdas de tamaño constante |
| `Mesh/interfaces.py` | UDP (vecinos fijos + descubrimiento multicast) y Faro (protocolo 0x0002) |
| `Mesh/nodo.py` | El nodo: une todo lo anterior |
| `App/mensajes.py` | Protocolo de la Capa 7: mensajes privados y publicaciones en temas |
| `App/servicio.py` | Buzón, temas y claves en memoria con TTL; API local para el cliente |
| `App/chat.py` | Cliente de terminal (todos los parámetros por línea de comandos) |
| `faronet_nodo.py` | Arranca un nodo |
| `demo_red.py` | Levanta una red de 7 nodos en una sola computadora |
| `tests/test_mesh.py` | 18 pruebas automáticas |
| `Gateway/`, `Edge/` | Gateway y Worker de Cloudflare: ver `Edge/LEEME.md` |

## 1. Cómo funciona

### Paquete FaroNet (capas 2/3)
```
+---------+------+-----+-------+-----------------+----------+--------+-------------------+
| Versión | Tipo | TTL | Flags | Destino (8 B)   | Longitud | Cuerpo | Relleno aleatorio |
|   1 B   | 1 B  | 1 B |  1 B  | id del nodo     |   2 B    |        | hasta 220 o 550 B |
+---------+------+-----+-------+-----------------+----------+--------+-------------------+
Tipos: 1 HELLO, 2 RUTAS, 3 DIRECTORIO (220 B)  ·  4 CELDA, 5 PUBLICACION (550 B)
```
- **No hay campo de origen**: un paquete no dice quién lo creó.
- **Solo dos tamaños**: todo mensaje de chat, corto o largo, mide 550 B. Son múltiplos de 110 B, los datos de una trama v3 del Faro: 550 B = 5 tramas, unos 22 s por la luz.

### Vecinos y rutas
- **HELLO** cada 5 s por UDP y cada 90 s por el Faro. Un vecino que deja de oírse se borra.
- **RUTAS** con vector de distancias. Costos: Ethernet 1, WiFi 2, Faro 10. Las rutas entre comunidades salen solas por el Faro.
- Si un enlace se cae, sus rutas se borran y las demás se recalculan.

### Directorio de nodos
Cada nodo firma su entrada con Ed25519: id, llave pública, alias, vencimiento y si es gateway. Se propaga por inundación, de modo que todos tienen una copia y no hay servidor central.

Se rechazan las entradas con firma mala, con un id que no corresponde a la llave (nadie puede suplantar a otro nodo) o vencidas. Cada entrada se renueva cada 10 minutos y vence a los 30.

### Onion routing de 3 saltos
```
mensaje privado:   emisor → R1 → R2 → R3 → destino
publicación:       emisor → R1 → R2 → R3 ══> R3 la inunda en toda la mesh
```
- El emisor elige 3 nodos al azar del directorio y cifra una capa para cada uno (SealedBox).
- **R1** sabe de quién recibió la celda, pero no el destino. **R3** sabe el destino, pero no el origen. **R2** no sabe ninguno de los dos.
- La celda **siempre mide 536 B**: cada nodo, al quitar su capa, la rellena de nuevo con bytes aleatorios.
- Al reenviar, cada nodo espera un tiempo aleatorio (0–0,5 s) para dificultar la correlación por tiempos.

### Aplicación (Capa 7)
- **Mensajes privados:**
  - Cifrados de extremo a extremo: la capa del destino es un SealedBox.
  - **Identificados**: van además en un Box del remitente, y el destinatario verifica quién lo escribió.
  - **Anónimos** (`--anonimo`): no llevan remitente.
  - Hasta 245 B identificados y 285 B anónimos (las tildes ocupan 2 B).
- **Temas públicos**: el texto va en claro. Son los únicos que el gateway podrá publicar en Internet.
- **Temas privados**:
  - La clave sale de una frase compartida (Argon2id), y el texto va en un SecretBox.
  - En el paquete no viaja ni el nombre del tema, solo una etiqueta derivada de la clave.
  - Nunca salen de FaroNet.
- **Publicaciones anónimas**: no tienen campo de autor.
- **Todo vive en memoria con TTL**: 1 hora por defecto, 24 h como máximo. No se escribe nada a disco salvo la llave del nodo.

## 2. Requisitos de privacidad: dónde se cumplen

| Requisito | Cómo |
|---|---|
| Anonimato del origen | Onion de 3 saltos; paquetes sin campo de origen |
| Cifrado extremo a extremo | PyNaCl: SealedBox + Box (privados), SecretBox (temas privados) |
| Sin criptografía propia | Solo primitivas de libsodium; el diseño del protocolo es nuestro |
| Relleno a tamaños fijos | 220 / 550 B por paquete; 536 B por celda; 316 / 365 B por mensaje |
| MAC rotable | El nodo rota la MAC del Faro cada 30 min (`--rotar-mac-cada`) y avisa con HELLO |
| Botón de pánico | El Faro llama a `nodo.panico()`: borra llaves, directorio, rutas, buzón, temas y claves, y detiene el nodo |
| Sin registros | No hay bitácoras; `--depurar` imprime solo tipos y cantidades, nunca IP, MAC ni ids |
| Mensajes con TTL | Buzón y publicaciones vencen; entradas del directorio a los 30 min |

### Borrador del modelo de amenazas (para el RFC)

| Adversario | Qué ve | Qué no ve |
|---|---|---|
| Nodo malicioso de la mesh | Tipo de paquete; id del siguiente relay de una celda; publicaciones públicas | Contenido de privados y temas privados; quién originó una celda (salvo que sea R1 y el emisor sea su vecino directo) |
| Observador del medio (luz/WiFi) | Que hay tráfico, cuándo y cuántos paquetes (todos del mismo tamaño); MAC del Faro (rota) | Contenido; longitud real; origen |
| Operador del gateway | Lo que se publica en temas públicos | Privados, temas privados, autores |
| Proveedor del edge | Lo que el gateway le manda (solo temas públicos), la IP del gateway | Quién escribió cada publicación |
| Varios nodos coludidos | Si controlan R1 y R3 de un circuito pueden correlacionar por tiempos | — (limitación conocida, igual que en Tor) |

## 3. Instalación

```bash
# Raspberry Pi (Trixie):
sudo apt install python3-nacl
# Windows / Mac / Linux en la laptop:
pip install pynacl
```

## 4. Probar todo SIN Raspberry Pi (en la laptop)

```bash
python3 demo_red.py                # 7 nodos: a1 a2 a3 faroA ~~luz simulada~~ faroB b1 b2
# en otra terminal:
python3 App/chat.py --api 7101 interactivo      # chat de a1
# en otra:
python3 App/chat.py --api 7111 interactivo      # chat de b1
```
En Windows use `py` en vez de `python3`.

Dentro del chat:
```
/directorio                 ver los nodos (entradas firmadas)
/rutas                      ver las rutas (las de la otra comunidad salen "vía faroA")
/para b1                    cambiar a mensajes privados para b1
hola                        envía "hola" a b1 por 3 saltos
/anonimo                    alternar anónimo / identificado
/tema redes                 cambiar a publicar en el tema público "redes"
/tema club frase secreta    tema privado con esa frase como clave
/leer  /buzon  /temas  /vecinos  /info  /rotar_mac  /panico
```

Con `python3 demo_red.py --lento`, el Faro simulado va a 25 bytes/s con 5 % de pérdida, como el real. Así el directorio tarda unos 3 minutos en llenarse y un mensaje privado entre comunidades tarda entre 30 s y 2 minutos.

Pruebas automáticas:
```bash
python3 -m unittest discover -s tests -v
```

También se puede probar entre **dos laptops en la misma red**: en cada una se arranca un nodo sin `--sin-descubrir` y se encuentran solos por multicast. Por Tailscale se usa `--vecino 100.x.x.x:7000`.

## 5. Con las Raspberry Pi (la prueba final con luz real)

Comunidad 1 = laptop(s) + Pi A, y comunidad 2 = Pi B + otro nodo.

En cada Pi:
```bash
sudo systemctl start pigpiod
cd ~/Desktop/Redes-faro-borrador && git pull
python3 faronet_nodo.py --alias faroA --faro real --pausa 40 --vecino <IP de la laptop>:7000
python3 faronet_nodo.py --alias faroB --faro real --pausa 40 --gateway        # en la Pi B
```
En la laptop, los nodos de cada comunidad se arrancan con `--vecino <IP de su Pi>:7000`.

Para el onion hacen falta **al menos 5 nodos** para un privado (emisor, 3 relays y destino) y 4 para publicar. Pueden ser varios procesos en la laptop con distinto `--puerto` y `--api`.

## 6. Limitaciones conocidas
- **Borrado en el pánico:** Python no garantiza borrar todas las copias de una llave en memoria, y la tarjeta SD no garantiza que el sobrescrito llegue al bloque físico.
- **Velocidad del Faro:** a unos 25 B/s, cada cruce de la luz suma unos 25 s. El emisor ordena los relays elegidos de cercano a lejano para cruzar la luz lo menos posible.
- **Ataques de tiempo:** un adversario que controla R1 y R3 de un mismo circuito puede correlacionar por tiempos, igual que en Tor.
- **Mínimo de nodos:** con menos de 4 nodos alcanzables no se puede armar un circuito, y el chat lo avisa.
