/**
 * servidor_local.ts - Corre el Worker de FaroNet en http://127.0.0.1:8787 usando
 * la simulación en memoria (sin Cloudflare). Sirve para probar el gateway de
 * Python de punta a punta. (Los WebSockets de las salas no se simulan aquí;
 * para eso use `npx wrangler dev`.)
 *
 *     node prueba/servidor_local.ts <GATEWAY_LLAVE en hex> [puerto]
 */
import { createServer } from "node:http";
import { crearEntorno } from "./runtime.ts";

const llave = process.argv[2];
const puerto = Number(process.argv[3] || 8787);
if (!/^[0-9a-f]{64}$/.test(llave || "")) {
  console.error("uso: node prueba/servidor_local.ts <llave del gateway en hex> [puerto]");
  process.exit(1);
}
const { worker, env } = await crearEntorno(llave);

createServer(async (req, res) => {
  const partes: Buffer[] = [];
  for await (const p of req) partes.push(p as Buffer);
  const cuerpo = partes.length ? Buffer.concat(partes) : undefined;
  const cab = new Headers();
  for (const [k, v] of Object.entries(req.headers)) if (typeof v === "string") cab.set(k, v);
  cab.set("CF-Connecting-IP", req.socket.remoteAddress || "");
  const r: Response = await worker.fetch(new Request(`http://127.0.0.1:${puerto}${req.url}`, {
    method: req.method, headers: cab, body: req.method === "GET" ? undefined : cuerpo,
  }), env);
  res.writeHead(r.status, Object.fromEntries(r.headers));
  res.end(Buffer.from(await r.arrayBuffer()));
}).listen(puerto, "127.0.0.1", () => console.log(`Worker simulado en http://127.0.0.1:${puerto}`));
