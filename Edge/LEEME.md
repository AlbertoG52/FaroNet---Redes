# FaroNet: gateway y aplicación pública en el edge (Capa 7 pública)

```
 FaroNet (anónima)                          Internet (clearnet)
 ... nodos ... ── Pi B: faroB + GATEWAY ──HTTPS firmado──> Cloudflare Worker ──> navegadores, lectores RSS
                    Gateway/gateway.py                      Edge/src/*.ts
```

- **Gateway** (`Gateway/gateway.py`): corre dentro del nodo de la Pi B (`faronet_nodo.py --worker URL`). Envía al Worker **solo** las publicaciones de **temas públicos** y el directorio de nodos. Los privados y los temas privados nunca pasan por ahí.
- **Worker** (`Edge/`): código TypeScript que corre en los servidores de Cloudflare (plan gratuito).

| Requisito del enunciado | Dónde |
|---|---|
| Relay del gateway autenticado con firmas (Ed25519 + marca de tiempo contra replay) | `gateway.py` firma; `src/control.ts` verifica la firma, la hora (±60 s) y el nonce (no repetible) |
| Salas públicas en tiempo real con Durable Objects + WebSockets, visibles en el navegador | `src/sala.ts` (una sala por tema, WebSockets con hibernación) y `src/pagina.ts` (`/sala/<tema>`) |
| Feed Atom/RSS por tema | `/tema/<tema>/feed.xml` (Atom), en `src/sala.ts` |
| Buzón muerto: blobs cifrados, id opaco derivado de un secreto, tamaño fijo, TTL, se borra al leer | `src/buzon.ts` (Durable Object por id, 1024 B, alarma para el TTL) + cliente `Gateway/buzon_muerto.py` |
| Directorio público de solo lectura con firmas verificables | `/directorio` (`src/control.ts`) + `Gateway/verificar_directorio.py` para verificarlo uno mismo |
| Límite de peticiones | `src/control.ts`: por minuto, 120 lecturas, 20 WebSockets, 20 buzón y 30 gateway por cliente |
| No persistir CF-Connecting-IP ni User-Agent | La IP solo se usa en memoria, como SHA-256 con sal aleatoria, durante 60 s; bitácoras de Cloudflare apagadas (`[observability] enabled = false`) |
| Despliegue reproducible con wrangler, sin secretos en el repo | `wrangler.toml` + `package.json`; `GATEWAY_LLAVE` es una llave **pública** |

## Rutas del Worker
```
GET  /                        temas públicos activos
GET  /sala/<tema>             sala en vivo (navegador)
GET  /sala/<tema>/ws          WebSocket de la sala
GET  /tema/<tema>/feed.xml    feed Atom
GET  /tema/<tema>/historial   JSON
GET  /directorio              réplica del directorio (JSON con la forma binaria y la firma)
PUT  /buzon/<64 hex>          depositar blob de 1024 B (cabecera opcional X-TTL: 60..86400 s)
GET  /buzon/<64 hex>          recoger y borrar
POST /gw/publicacion          solo el gateway (firmado)
POST /gw/directorio           solo el gateway (firmado)
```

## Cómo probarlo (de menos a más real)

### E1. Pruebas automáticas (sin Cloudflare)
Necesita **Node 22.18 o más nuevo** (`node --version`). Corre el código real del Worker con una simulación de Cloudflare en memoria:
```
python3 -m unittest discover -s tests -v      # incluye 8 pruebas del gateway y el Worker
cd Edge && node prueba/simulacion.ts           # 11 pruebas del Worker
```
Si Node no está instalado, las pruebas del Worker aparecen como *skipped*.

### E2. El Worker REAL en tu laptop (`wrangler dev`) con la mesh simulada
```
cd Edge
npm install                                    # una sola vez
cd ..
python3 faronet_nodo.py --alias faroB --api 7113 --carpeta demo_logs/llaves --llave-gateway
#   ^ imprime la llave del gateway de la demo (64 hex)
cd Edge
npx wrangler dev --var GATEWAY_LLAVE:<esa llave>
#   queda en http://127.0.0.1:8787
```
En otra terminal, desde la raíz del repo:
```
python3 demo_red.py --worker http://127.0.0.1:8787
python3 App/chat.py --api 7101 interactivo
#   /tema redes   y escriba algo
```
Lista de chequeo:
- [ ] Abrir **http://127.0.0.1:8787**: aparece `#redes`.
- [ ] Abrir la sala `/sala/redes`. Al publicar otra cosa desde el chat, aparece **sola, en vivo**, sin recargar.
- [ ] `/tema/redes/feed.xml` muestra el feed Atom.
- [ ] Un tema privado (`/tema club frase`) y los privados **no aparecen** en el Worker.
- [ ] `python3 Gateway/verificar_directorio.py --worker http://127.0.0.1:8787` dice `7 entradas verificadas, 0 con problemas`.
- [ ] Buzón muerto:
  ```
  python3 Gateway/buzon_muerto.py --worker http://127.0.0.1:8787 depositar --frase "frase larga" --mensaje "hola"
  python3 Gateway/buzon_muerto.py --worker http://127.0.0.1:8787 recoger --frase "frase larga"
  python3 Gateway/buzon_muerto.py --worker http://127.0.0.1:8787 recoger --frase "frase larga"   # ya no hay nada
  ```

### E3. Desplegar en Cloudflare (plan gratuito)
1. Crear una cuenta gratuita en cloudflare.com.
2. Ejecutar:
   ```
   cd Edge
   npx wrangler login
   ```
3. Sacar la llave del gateway **real**. En la Pi B, con el mismo `--alias` y `--api` con que va a correr el nodo:
   ```
   python3 faronet_nodo.py --alias faroB --llave-gateway
   ```
4. Ponerla en `wrangler.toml` (`GATEWAY_LLAVE = "..."`) y hacer commit. No es secreta.
5. Desplegar con `npx wrangler deploy`. Al final muestra la URL, `https://faronet.<su-cuenta>.workers.dev`.
6. En la Pi B, arrancar el nodo como gateway:
   ```
   python3 faronet_nodo.py --alias faroB --faro real --pausa 40 --worker https://faronet.<su-cuenta>.workers.dev
   ```
7. Abrir la URL desde el celular: los temas públicos de FaroNet aparecen ahí. **Esa URL es la que va en la entrega.**

## Modelo de amenazas: el proveedor del edge (para el RFC)

Aunque el código no guarda IP ni User-Agent, **Cloudflare sigue viendo**:
- **La IP del gateway y la hora de cada publicación.** No ve quién la escribió: el origen ya se perdió en el onion de 3 saltos.
- **La IP de los visitantes** de las salas, del feed y del buzón. Cloudflare termina el TLS, así que ve las peticiones en claro, aunque nuestro código no las guarde.
- **El contenido de los temas públicos.** Es público por diseño.
- **Del buzón:** el tamaño (siempre 1024 B), la hora y quién deposita y quién recoge, por IP. **Nunca** el contenido (cifrado con SecretBox) ni la frase.

**Mitigaciones:** los visitantes pueden usar Tor o una VPN para el buzón, los blobs tienen tamaño fijo y TTL, y no hay cuentas ni identificadores.

**El operador del gateway (la Pi B)** ve las publicaciones públicas antes de que salgan, pero no su autor.
