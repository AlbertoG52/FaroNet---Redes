/**
 * simulacion.ts - Prueba el Worker SIN Cloudflare: simula en memoria los
 * Durable Objects (almacenamiento, alarmas, WebSockets) y ejecuta el código
 * real de src/. Requiere Node 22.18 o más nuevo (entiende TypeScript).
 *
 *     node prueba/simulacion.ts
 *     node prueba/simulacion.ts vectores.json   # además verifica firmas hechas en Python
 *
 * La prueba definitiva es con `npx wrangler dev`, que usa el runtime real.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { bytesAHex, leerEntrada, mensajeFirmado, verificarEd25519, hexABytes } from "../src/comun.ts";

import { crearEntorno } from "./runtime.ts";

// llave del gateway (en la realidad es la Ed25519 del nodo, generada con PyNaCl)
const par: any = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const publica = new Uint8Array(await crypto.subtle.exportKey("raw", par.publicKey));
const { worker, env } = await crearEntorno(bytesAHex(publica));

const BASE = "https://faronet.ejemplo.workers.dev";
const pedir = (ruta: string, init: any = {}) =>
  worker.fetch(new Request(BASE + ruta, { ...init, headers: { "CF-Connecting-IP": "203.0.113.9", ...(init.headers || {}) } }), env);

async function firmado(ruta: string, cuerpo: any, cambios: any = {}) {
  const datos = new TextEncoder().encode(JSON.stringify(cuerpo));
  const tiempo = String(cambios.tiempo ?? Math.floor(Date.now() / 1000));
  const nonce = cambios.nonce ?? bytesAHex(crypto.getRandomValues(new Uint8Array(16)));
  const firma = new Uint8Array(await crypto.subtle.sign("Ed25519", par.privateKey, mensajeFirmado("POST", ruta, tiempo, nonce, datos)));
  if (cambios.romper) firma[0] ^= 1;
  return pedir(ruta, { method: "POST", body: datos, headers: {
    "X-FaroNet-Tiempo": tiempo, "X-FaroNet-Nonce": nonce, "X-FaroNet-Firma": bytesAHex(firma) } });
}

let pruebas = 0;
async function prueba(nombre: string, f: () => Promise<void>) {
  await f();
  pruebas++;
  console.log(`  ok  ${nombre}`);
}

// ------------------------------------------------------------ pruebas
const ahora = Math.floor(Date.now() / 1000);
const pub = { tema: "Redes", texto: "hola <script>alert(1)</script> & más", id: "0123456789abcdef", creada: ahora, vence: ahora + 3600 };

await prueba("el gateway publica con firma válida", async () => {
  const r = await firmado("/gw/publicacion", pub);
  assert.equal(r.status, 200, await r.clone().text());
});

await prueba("replay: el mismo nonce se rechaza", async () => {
  const nonce = "aa".repeat(16);
  assert.equal((await firmado("/gw/publicacion", { ...pub, id: "1111111111111111" }, { nonce })).status, 200);
  assert.equal((await firmado("/gw/publicacion", { ...pub, id: "1111111111111111" }, { nonce })).status, 409);
});

await prueba("firma alterada, hora vieja o sin cabeceras: 401", async () => {
  assert.equal((await firmado("/gw/publicacion", pub, { romper: true })).status, 401);
  assert.equal((await firmado("/gw/publicacion", pub, { tiempo: ahora - 600 })).status, 401);
  assert.equal((await pedir("/gw/publicacion", { method: "POST", body: "{}" })).status, 401);
});

await prueba("historial, página de inicio y sala", async () => {
  const h: any = await (await pedir("/tema/redes/historial")).json();
  assert.equal(h.publicaciones.length, 2);
  const inicio = await (await pedir("/")).text();
  assert.match(inicio, /#redes/);
  const r = await pedir("/sala/redes");
  assert.match(r.headers.get("Content-Security-Policy")!, /script-src 'nonce-/);
});

await prueba("feed Atom con el texto escapado (sin XSS)", async () => {
  const r = await pedir("/tema/redes/feed.xml");
  assert.equal(r.headers.get("Content-Type"), "application/atom+xml; charset=utf-8");
  const xml = await r.text();
  assert.match(xml, /&lt;script&gt;/);
  assert.doesNotMatch(xml, /<script>/);
});

await prueba("WebSocket: historial al conectar y publicación en vivo", async () => {
  const r: any = await pedir("/sala/redes/ws", { headers: { Upgrade: "websocket" } });
  const cliente = r.webSocket;
  assert.equal(cliente.recibidos[0].tipo, "historial");
  await firmado("/gw/publicacion", { ...pub, id: "2222222222222222", texto: "en vivo" });
  assert.equal(cliente.recibidos.at(-1).texto, "en vivo");
});

await prueba("publicaciones inválidas o vencidas se rechazan", async () => {
  assert.equal((await firmado("/gw/publicacion", { ...pub, id: "x" })).status, 400);
  assert.equal((await firmado("/gw/publicacion", { ...pub, id: "3333333333333333", vence: ahora - 1 })).status, 400);
  assert.equal((await firmado("/gw/publicacion", { ...pub, id: "4444444444444444", tema: "<b>" })).status, 400);
});

await prueba("TTL: la alarma borra lo vencido", async () => {
  await firmado("/gw/publicacion", { ...pub, tema: "corto", id: "5555555555555555", vence: ahora + 2 });
  const s = env.SALAS.instancias.get("corto");
  assert.ok(s.state.storage.alarma);
  s.state.storage.datos.forEach((p: any) => { p.vence = ahora - 1; });
  await s.alarm();
  assert.equal(s.state.storage.datos.size, 0);
});

await prueba("buzón muerto: depositar, recoger una sola vez", async () => {
  const id = "ab".repeat(32);
  const blob = crypto.getRandomValues(new Uint8Array(1024));
  assert.equal((await pedir(`/buzon/${id}`, { method: "PUT", body: blob })).status, 201);
  assert.equal((await pedir(`/buzon/${id}`, { method: "PUT", body: blob })).status, 409);
  const r = await pedir(`/buzon/${id}`);
  assert.deepEqual(new Uint8Array(await r.arrayBuffer()), blob);
  assert.equal((await pedir(`/buzon/${id}`)).status, 404);           // ya se borró
  assert.equal((await pedir(`/buzon/${id}`, { method: "PUT", body: new Uint8Array(10) })).status, 400);
  assert.equal((await pedir(`/buzon/xyz`)).status, 400);
});

await prueba("ninguna IP ni User-Agent queda guardada", async () => {
  for (const ns of [env.SALAS, env.BUZONES, env.CONTROL]) {
    for (const obj of ns.instancias.values()) {
      const todo = JSON.stringify([...obj.state.storage.datos.entries()]);
      assert.doesNotMatch(todo, /203\.0\.113\.9/);
    }
  }
});

await prueba("límite de peticiones: 429 al pasarse", async () => {
  let estado = 0;
  for (let i = 0; i < 130 && estado !== 429; i++) {
    estado = (await pedir("/directorio", { headers: { "CF-Connecting-IP": "198.51.100.7" } })).status;
  }
  assert.equal(estado, 429);
  assert.equal((await pedir("/directorio", { headers: { "CF-Connecting-IP": "198.51.100.8" } })).status, 200);
});

// ------------------------------------------------------------ vectores hechos en Python
if (process.argv[2]) {
  const v = JSON.parse(readFileSync(process.argv[2], "utf8"));
  await prueba("firma del gateway hecha con PyNaCl se verifica en el Worker", async () => {
    const msg = mensajeFirmado("POST", v.ruta, v.tiempo, v.nonce, new TextEncoder().encode(v.cuerpo));
    assert.ok(await verificarEd25519(hexABytes(v.llave)!, hexABytes(v.firma)!, msg));
  });
  await prueba("entradas del directorio hechas en Python se leen y verifican", async () => {
    for (const e of v.entradas) {
      const leida = await leerEntrada(e.hex);
      assert.ok(leida, "entrada rechazada");
      assert.equal(leida!.alias, e.alias);
      assert.equal(leida!.id, e.id);
    }
    const mala = v.entradas[0].hex.slice(0, -2) + (v.entradas[0].hex.endsWith("00") ? "01" : "00");
    assert.equal(await leerEntrada(mala), null);
  });
  await prueba("el directorio publicado por el gateway queda en /directorio", async () => {
    env.GATEWAY_LLAVE = v.llave;          // ahora firma la llave de Python
    const r = await pedir(v.ruta_dir, { method: "POST", body: v.cuerpo_dir, headers: v.cabeceras_dir });
    assert.equal(r.status, 200, await r.clone().text());
    const d: any = await (await pedir("/directorio")).json();
    assert.equal(d.nodos.length, v.entradas.length);
  });
}

console.log(`\n${pruebas} pruebas OK`);
