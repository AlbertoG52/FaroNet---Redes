/**
 * buzon.ts - Durable Object "Buzon": un buzón muerto (dead drop) por identificador.
 *
 *   PUT /buzon/<id>   deposita un blob cifrado de EXACTAMENTE 1024 bytes
 *   GET /buzon/<id>   lo entrega y lo BORRA (solo se puede leer una vez)
 *
 * - <id> es opaco: 64 caracteres hex que los dos participantes derivan de un
 *   secreto compartido (ver Gateway/buzon_muerto.py). El Worker no sabe quién
 *   lo creó ni qué contiene.
 * - El blob ya viene cifrado; el Worker nunca ve el contenido en claro.
 * - Tamaño fijo (1024 B) para no filtrar el largo del mensaje.
 * - TTL: si nadie lo recoge, una alarma lo borra al vencer (por defecto 1 h).
 * - Se usa un Durable Object (consistencia fuerte) y no KV, para que "borrar
 *   al leer" sea exacto: con KV, otra región podría leerlo una segunda vez.
 */
import { CABECERAS_SEGURAS, TAM_BLOB, TTL_BUZON_DEF_S, TTL_MAX_S, error, json } from "./comun.ts";

export class Buzon {
  state: any;

  constructor(state: any, _env: any) {
    this.state = state;
  }

  async fetch(req: Request): Promise<Response> {
    if (req.method === "PUT") return this.depositar(req);
    if (req.method === "GET") return this.recoger();
    return error(405, "use PUT o GET");
  }

  async depositar(req: Request): Promise<Response> {
    const blob = new Uint8Array(await req.arrayBuffer());
    if (blob.length !== TAM_BLOB) return error(400, `el blob debe medir exactamente ${TAM_BLOB} bytes`);
    if (await this.state.storage.get("blob")) return error(409, "ese buzón ya tiene un blob");
    const ttl = Math.min(Math.max(parseInt(req.headers.get("X-TTL") || "") || TTL_BUZON_DEF_S, 60),
                         TTL_MAX_S);
    const vence = Date.now() + ttl * 1000;
    await this.state.storage.put({ blob, vence });
    await this.state.storage.setAlarm(vence);
    return json({ ok: true, vence: Math.floor(vence / 1000) }, 201);
  }

  async recoger(): Promise<Response> {
    const [blob, vence] = await Promise.all([this.state.storage.get("blob"),
                                             this.state.storage.get("vence")]);
    await this.state.storage.deleteAll();               // se borra al leer
    await this.state.storage.deleteAlarm();
    if (!blob || vence <= Date.now()) return error(404, "no hay nada en ese buzón");
    return new Response(blob, {
      headers: { ...CABECERAS_SEGURAS, "Content-Type": "application/octet-stream" },
    });
  }

  async alarm(): Promise<void> {
    await this.state.storage.deleteAll();               // venció el TTL
  }
}
