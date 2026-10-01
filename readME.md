# Proyecto 1 — FaroNet: Documento de Kick-off

Escuela de Ingeniería en Computación · Ingeniería en Computación · IC7602 - Redes
**Profesor:** Kevin Moraga Garcia
**Integrantes:** Alberto Guerrero Gabay - 2023095089 · Christopher Alvarado Aguilar - 2023374347
II Semestre 2026 · Alajuela, Costa Rica · 1 de octubre de 2026

## 1. Introducción

FaroNet es una red de comunicación que se construye completa, desde el hardware hasta la aplicación. Dos comunidades se comunican únicamente por medio de un enlace de luz entre dos dispositivos construidos por el grupo, llamados Faros. Sobre ese enlace se forma una red mesh que no revela quién envía cada mensaje, y desde ella solo el contenido marcado como público se publica en Internet mediante Cloudflare Workers. El objetivo es que la red exponga lo mínimo posible sobre sus usuarios: no solo el contenido de los mensajes, sino también quién se comunica con quién y cuándo.

Para resolver el problema se plantea la siguiente estrategia:

* **Hardware:** el Faro se controla directamente con una Raspberry Pi, sin un microcontrolador adicional. Los componentes se sueldan en una placa que se conecta a los pines GPIO: un LED infrarrojo con un transistor para emitir, un fototransistor con un comparador para recibir, LEDs de actividad, un botón de pánico y una memoria EEPROM para guardar la dirección física.
* **Capa 1:** los bits se envían por luz usando el UART de la Raspberry Pi, con su codificación NRZ asíncrona (bit de inicio y bit de parada). Se eligió así porque el hardware del UART controla el tiempo de cada bit, mientras que Linux no garantiza tiempos exactos si los bits se generan por software. El formato de la trama (máximo 128 bytes, CRC, versión, dirección física y acceso al medio) se definirá después de lograr una transmisión estable de bytes por luz, y luego se implementará en la biblioteca `DispositivoLuzAdaptador`.
* **Capas 2 y 3:** una red mesh que funciona sobre Ethernet, WiFi y el Faro, con un directorio de nodos donde cada uno publica su llave pública firmada, y con enrutamiento dinámico entre nodos.
* **Privacidad:** cifrado de extremo a extremo con PyNaCl, onion routing de tres saltos, relleno de los paquetes a un tamaño fijo, dirección física rotable, botón de pánico para borrar las llaves y ningún registro de direcciones IP o identificadores.
* **Capa 7:** un chat por terminal para los mensajes privados dentro de FaroNet. Los mensajes públicos salen por el gateway hacia un Worker que ofrece salas en tiempo real, feeds por tema, un buzón de mensajes cifrados y una copia pública del directorio de nodos.

El trabajo avanza de las capas bajas hacia las altas. La parte del edge no depende del hardware, por lo que se desarrolla en paralelo. La Figura 1 muestra el esquema general.

```
     Comunidad 1                              Comunidad 2
  +----------------+                       +----------------+
  | Nodos          |                       | Nodos          |
  | (WiFi/Ethernet)|                       | (WiFi/Ethernet)|
  +-------+--------+                       +-------+--------+
          |                                        |
  +-------+--------+                       +-------+--------+
  | RPi A + Faro   | ~~~~~~~ luz ~~~~~~~~> | RPi B + Faro   |
  |                | <~~~~~~ luz ~~~~~~~~~ | (gateway)      |
  +----------------+                       +-------+--------+
                                                   | HTTPS / WSS
                                                   v
                                         +--------------------+
                                         | Cloudflare Workers |
                                         | (edge)             |
                                         +---------+----------+
                                                   |
                                                   v
                                              Navegadores
```
*Figura 1: Esquema general de la solución.*

## 2. Ambiente de desarrollo

### 2.1 Software
* Raspberry Pi OS en las dos Raspberry Pi, que funcionan como nodos con Faro; una de ellas también funciona como gateway.
* Python 3 para el código del Faro, los nodos y el gateway, con las bibliotecas `pyserial` (puerto serial), `gpiozero` (pines GPIO), `PyNaCl` (criptografía) y `Scapy` (tramas y paquetes).
* Node.js y `wrangler` para desarrollar y desplegar el Worker de Cloudflare (plan gratuito) en JavaScript/TypeScript.
* Visual Studio Code con la extensión Remote-SSH para trabajar directamente en las Raspberry Pi.
* Fritzing o KiCad para el esquemático del Faro.
* LaTeX y Markdown para la documentación; texto ASCII para el RFC.

### 2.2 Herramientas de hardware
* Cautín, estaño con flux y malla desoldadora.
* Multímetro para pruebas de continuidad, cortos y voltajes.
* Protoboard y cables jumper para el prototipo.
* Placa perforada y header hembra para montar la placa sobre la Raspberry Pi.
* Cámara de celular para comprobar que el LED infrarrojo emite.

### 2.3 Forma de debugging
* **Hardware:** revisión con multímetro antes de energizar la placa.
* **Capa 1:** primero se conectan TX y RX con un cable y luego por luz; se envían datos conocidos y se compara lo enviado con lo recibido.
* **Software:** pruebas unitarias de las funciones principales y mensajes de depuración que no incluyen datos sensibles.
* **Edge:** `wrangler dev` para probar el Worker en local antes de desplegarlo.

### 2.4 Flujo de trabajo en control de versiones
* Repositorio en GitHub.
* La rama `main` se mantiene funcional; cada funcionalidad se desarrolla en su propia rama y se integra mediante *pull request* revisado por el otro integrante.
* Cada integrante hace commits desde su cuenta con mensajes descriptivos.
* Llaves, secretos y archivos generados quedan fuera del repositorio mediante `.gitignore`; los secretos del Worker se manejan con `wrangler secret`.

## 3. Control de versiones

El repositorio del proyecto es https://github.com/AlbertoG52/FaroNet---Redes.git

## 4. Diseño

### 4.1 Esquemático preliminar del Faro

La Raspberry Pi controla el Faro a través de sus pines GPIO. La Tabla 1 resume los bloques del circuito y la Figura 2 sus conexiones preliminares.

*Tabla 1: Bloques del circuito del Faro.*

| Bloque | Componentes | Función |
|---|---|---|
| Emisor | LED infrarrojo, transistor NPN, resistencias | El pin TX activa el transistor, que entrega al LED más corriente de la que el pin puede dar |
| Receptor | Fototransistor, resistencia | Convierte la luz recibida en un voltaje |
| Comparador | LM393, trimpot, resistencia pull-up | Convierte el voltaje del receptor en un 0 o 1 limpio para el pin RX |
| Memoria | EEPROM I2C | Guarda la dirección física del Faro |
| Indicadores | LEDs TX, RX y pánico, resistencias | Muestran la actividad del enlace y el borrado de llaves |
| Botón de pánico | Pulsador | Activa el borrado de llaves y datos sensibles |

```
  EMISOR
  GPIO TX ---[R base]---> Base del transistor NPN
  5V ---[R limitadora]---[LED IR]---> Colector del transistor
  Emisor del transistor ---> GND

  RECEPTOR Y COMPARADOR (alimentado a 3.3V)
  3.3V ---> Colector del fototransistor
  Emisor del fototransistor ---+---[R]---> GND
                               |
                               +---> Entrada (+) del comparador
  Trimpot (entre 3.3V y GND) ---> Entrada (-) del comparador
  Salida del comparador ---+---[R pull-up]---> 3.3V
                           |
                           +---> GPIO RX

  MEMORIA (dirección física)
  EEPROM I2C ---> GPIO SDA / SCL

  INDICADORES
  GPIO ---[R]---[LED TX]---> GND
  GPIO ---[R]---[LED RX]---> GND
  GPIO ---[R]---[LED pánico]---> GND

  BOTÓN DE PÁNICO
  GPIO ---[pulsador]---> GND   (pull-up interno)
```
*Figura 2: Conexiones preliminares del Faro.*

### 4.2 Diagrama de componentes

La Figura 3 muestra los componentes de software de un nodo. El gateway tiene los mismos componentes y, además, la conexión con el Worker.

```
        +-------------------------------------------+
        |        Aplicación de chat (terminal)      |
        +-------------------------------------------+
        |  Privacidad: cifrado extremo a extremo,   |
        |  onion routing de 3 saltos, padding       |
        +-------------------------------------------+
        |  Red mesh: directorio de nodos firmado    |
        |  y enrutamiento                           |
        +-------------+--------------+--------------+
        |  Ethernet   |     WiFi     |     Faro     |
        +-------------+--------------+------+-------+
                                            |
                                 DispositivoLuzAdaptador
                                            |
                                  Placa soldada (Faro)
```
*Figura 3: Componentes de un nodo FaroNet.*

### 4.3 Borrador del modelo de amenazas

* **Nodo malicioso de la mesh:** participa en la red como cualquier otro nodo y puede ver los paquetes que pasan por él. Con onion routing solo conoce el salto anterior y el siguiente, y con el cifrado no puede leer el contenido.
* **Observador del medio físico:** alguien cerca del Faro con un sensor de luz puede captar las tramas. Ve que hay transmisión y cuánta, pero no el contenido, y la dirección física rotable evita que pueda seguir al mismo dispositivo en el tiempo.
* **Operador del gateway:** controla la Raspberry Pi que conecta FaroNet con Internet. Puede ver los mensajes públicos, pero no los privados ni quién originó cada mensaje.
* **Proveedor del edge:** Cloudflare ejecuta el Worker y ve las conexiones que llegan a sus servidores, incluidas las direcciones IP. El código del Worker no las guarda, pero el proveedor sí puede verlas.
* **Robo del dispositivo:** quien obtenga el Faro podría leer su memoria. El botón de pánico borra las llaves y los datos sensibles.
