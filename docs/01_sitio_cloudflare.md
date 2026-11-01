# Prueba con los Faros: de la luz al sitio público

## Qué es
- **El sitio** es la página web pública de FaroNet, en Cloudflare: `https://faronet.<cuenta>.workers.dev`. Muestra los **temas públicos**: salas en vivo, un feed y el directorio de nodos. Christian lo publica.
- **El gateway** es el nodo de la **Pi B**: manda los temas públicos al sitio, firmados con su llave. Lo privado nunca sale.
- **Esta prueba** comprueba la cadena completa: un mensaje escrito en la Pi A cruza la **luz real**, llega a la Pi B, sale por el gateway y aparece en el sitio. Da igual si los Faros están en protoboard o soldados.
- **`pi_comunidad.py`** levanta 3 nodos dentro de cada Pi (el del Faro y 2 más), así que no hacen falta laptops.

## Antes
- Que el chat de luz (`Faro/faro.py --pausa 40`) funcione en los dos sentidos.
- **La Pi B con Internet y en hora.** La Pi A no necesita Internet.
- La URL del sitio (te la pasa Christian).

## Pasos
1. **Bajar el código e instalar PyNaCl, en las dos Pi.** Usen la rama con el código nuevo; `git checkout Chris` si todavía no se unió a `main`:
   ```
   cd ~/Desktop/Redes-faro-borrador && git pull
   sudo apt install python3-nacl
   ```
2. **Sacar la llave del gateway, en la Pi B,** y mandársela a Christian (él actualiza el sitio):
   ```
   python3 faronet_nodo.py --alias faroB --api 7113 --llave-gateway
   ```
3. **Revisar la hora, en la Pi B.** `timedatectl` debe decir `System clock synchronized: yes`.
4. **Arrancar pigpio, en las dos Pi,** y cerrar `faro.py` si está abierto:
   ```
   sudo systemctl start pigpiod
   ```
5. **Arrancar las comunidades.** La misma `--pausa` y la misma frase en las dos Pi:
   ```
   # Pi A
   python3 pi_comunidad.py --lado A --pausa 40 --clave-enlace "frase del enlace"
   # Pi B
   python3 pi_comunidad.py --lado B --pausa 40 --clave-enlace "frase del enlace" --worker <URL del sitio>
   ```
6. **Esperar unos 5 minutos** y revisar en la Pi A, en otra terminal:
   ```
   python3 App/chat.py --api 7101 directorio
   ```
   Debe decir **"5 nodos más"**.
7. **Publicar, en la Pi A:**
   ```
   python3 App/chat.py --api 7101 publicar --tema redes --texto "hola desde la comunidad 1"
   ```
   Abrir la URL en el celular: en 1 a 3 minutos aparece `#redes` con el mensaje.
8. **Mensaje privado entre comunidades:**
   ```
   # Pi A
   python3 App/chat.py --api 7101 privado --para b2 --texto "privado por la luz"
   # Pi B (esperar 1 a 3 minutos)
   python3 App/chat.py --api 7112 buzon
   ```
   El mensaje aparece en la Pi B y **no** en el sitio.
9. **Botón de pánico (al final), en la Pi A.** Se enciende el LED rojo, los nodos se detienen y `ls ~/.faronet` ya no muestra `mac` ni `nodo-faroA-7104.llave`.

**Evidencia:** fotos de los Faros y capturas de los pasos 6, 7 (celular), 8 y 9.

## Si algo falla
| Síntoma | Revisar |
|---|---|
| No llega a "5 nodos más" | La luz (`faro.py` en los dos sentidos); la misma `--pausa` y la misma frase en las dos Pi; con `--depurar` se ven los eventos |
| "se necesitan al menos 3 nodos intermedios" | Falta que llegue el directorio de la otra Pi: esperar |
| No aparece nada en el sitio | Internet y hora de la Pi B; la URL; que Christian ya haya puesto la llave del paso 2 |
| La luz se satura o pierde tramas | Agregar `--ciclo 15` o `--datos-por-trama 50` a `pi_comunidad.py` |
