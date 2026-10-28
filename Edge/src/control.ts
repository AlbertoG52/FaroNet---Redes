/**
 * control.ts - Durable Object "Control" (una sola instancia).
 *
 * 1) Autentica al gateway: cada petición viene firmada con Ed25519 por la
 *    llave del nodo gateway, con marca de tiempo (±60 s) y un nonce que no se
 *    puede repetir (protección contra replay).
 * 2) Guarda la réplica pública del directorio de nodos (solo entradas con
 *    firma válida y vigentes).
 * 3) Lleva la lista de temas públicos activos.
 * 4) Límite de peticiones por cliente. La IP NUNCA se guarda: se convierte en
 *    un resumen SHA-256 con una sal aleatoria que solo vive en memoria (cambia
 *    cada vez que el objeto se reinicia) y los contadores duran 60 s.
 */
import {
  MAX_CUERPO_GATEWAY, MAX_TEXTO, TTL_MAX_S, VENTANA_FIRMA_S, bytesAHex, error, hexABytes, json,
  leerEntrada, mensajeFirmado, normalizarTema, verificarEd25519,
} from "./comun.ts";

const LIMITES: Record<string, number> = { lectura: 120, ws: 20, buzon: 20, gw: 30 };  // por minuto

export class Control {
  state: any;
  env: any;
  sal: Uint8Array;
  contadores: Map<string, { ventana: number; n: number }>;

  constructor(state: any, env: any) {
    this.state = state;
    this.env = env;
    this.sal = crypto.getRandomValues(new Uint8Array(16));
    this.contadores = new Map();
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const ruta = url.pathname;
    if (ruta === "/limite") return this.limite(req);
    if (ruta.startsWith("/gw/") && req.method === "POST") return this.gateway(req, ruta);
    if (ruta === "/directorio") return this.directorio();
    if (ruta === "/temas") return this.temas();
    return error(404, "no encontrado");
  }

  // ---------------------------------------------------------------- límite de peticiones
  async limite(req: Request): Promise<Response> {
    const categoria = req.headers.get("X-Categoria") || "lectura";
    const cliente = req.headers.get("X-Cliente") || "";
    const datos = new TextEncoder().encode(cliente);
    const mezcla = new Uint8Array(this.sal.length + datos.length);
    mezcla.set(this.sal);
    mezcla.set(datos, this.sal.length);
    const resumen = bytesAHex(new Uint8Array(await crypto.subtle.digest("SHA-256", mezcla))).slice(0, 16);
    const clave = `${categoria}:${resumen}`;
    const ventana = Math.floor(Date.now() / 60000);
    const c = this.contadores.get(clave);
    if (!c || c.ventana !== ventana) {
      this.contadores.set(clave, { ventana, n: 1 });
      if (this.contadores.size > 10000) {             // limpieza de ventanas viejas
        for (const [k, v] of this.contadores) if (v.ventana !== ventana) this.contadores.delete(k);
      }
      return json({ permitido: true });
    }
    c.n++;
    return json({ permitido: c.n <= (LIMITES[categoria] ?? 60) });
  }

  // ---------------------------------------------------------------- gateway autenticado
  async gateway(req: Request, ruta: string): Promise<Response> {
    const tiempo = req.headers.get("X-FaroNet-Tiempo") || "";
    const nonce = req.headers.get("X-FaroNet-Nonce") || "";
    const firma = hexABytes(req.headers.get("X-FaroNet-Firma") || "");
    const llave = hexABytes(this.env.GATEWAY_LLAVE || "");
    if (!llave || llave.length !== 32) return error(500, "GATEWAY_LLAVE no está configurada");
    if (!/^\d{1,12}$/.test(tiempo) || !/^[0-9a-f]{32}$/.test(nonce) || !firma) {
      return error(401, "faltan las cabeceras de autenticación");
    }
    if (Math.abs(Date.now() / 1000 - Number(tiempo)) > VENTANA_FIRMA_S) {
      return error(401, "marca de tiempo fuera de la ventana permitida");
    }
    const cuerpo = new Uint8Array(await req.arrayBuffer());
    if (cuerpo.length > MAX_CUERPO_GATEWAY) return error(413, "cuerpo demasiado grande");
    const ok = await verificarEd25519(llave, firma, mensajeFirmado("POST", ruta, tiempo, nonce, cuerpo));
    if (!ok) return error(401, "firma inválida");
    // replay: el nonce no puede haberse usado en los últimos 2 minutos
    const claveNonce = `n:${nonce}`;
    if (await this.state.storage.get(claveNonce)) return error(409, "petición repetida (replay)");
    await this.state.storage.put(claveNonce, Date.now() + 2 * VENTANA_FIRMA_S * 1000);
    await this.limpiarNonces();

    let datos: any;
    try {
      datos = JSON.parse(new TextDecoder().decode(cuerpo));
    } catch {
      return error(400, "JSON inválido");
    }
    if (ruta === "/gw/publicacion") return this.recibirPublicacion(datos, req.url);
    if (ruta === "/gw/directorio") return this.recibirDirectorio(datos);
    return error(404, "acción del gateway desconocida");
  }

  async limpiarNonces(): Promise<void> {
    const ahora = Date.now();
    const nonces = await this.state.storage.list({ prefix: "n:", limit: 500 });
    const viejos = [...nonces.entries()].filter(([, vence]: any) => vence < ahora).map(([k]) => k);
    for (let i = 0; i < viejos.length; i += 128) await this.state.storage.delete(viejos.slice(i, i + 128));
  }

  async recibirPublicacion(p: any, urlOriginal: string): Promise<Response> {
    const tema = normalizarTema(String(p.tema ?? ""));
    const ahora = Date.now() / 1000;
    if (!tema) return error(400, "tema inválido");
    if (typeof p.texto !== "string" || !p.texto || p.texto.length > MAX_TEXTO) return error(400, "texto inválido");
    if (!/^[0-9a-f]{16}$/.test(String(p.id))) return error(400, "id inválido");
    const creada = Math.floor(Number(p.creada));
    const vence = Math.min(Math.floor(Number(p.vence)), creada + TTL_MAX_S);
    if (!(creada > 0) || !(vence > ahora) || creada > ahora + 300) return error(400, "fechas inválidas");
    const sala = this.env.SALAS.get(this.env.SALAS.idFromName(tema));
    const r = await sala.fetch(new URL("/publicar", urlOriginal).toString(), {
      method: "POST", body: JSON.stringify({ id: p.id, texto: p.texto, creada, vence }),
    });
    const temas: Record<string, number> = (await this.state.storage.get("temas")) || {};
    temas[tema] = Math.max(temas[tema] || 0, vence);
    for (const t of Object.keys(temas)) if (temas[t] < ahora) delete temas[t];
    await this.state.storage.put("temas", temas);
    return r;
  }

  async recibirDirectorio(d: any): Promise<Response> {
    if (!Array.isArray(d.entradas) || d.entradas.length > 500) return error(400, "entradas inválidas");
    const ahora = Date.now() / 1000;
    const validas = [];
    let rechazadas = 0;
    for (const hex of d.entradas) {
      const e = typeof hex === "string" ? await leerEntrada(hex) : null;
      if (e && e.expira > ahora) validas.push(e);
      else rechazadas++;
    }
    await this.state.storage.put("directorio", { actualizado: Math.floor(ahora), nodos: validas });
    return json({ ok: true, aceptadas: validas.length, rechazadas });
  }

  async directorio(): Promise<Response> {
    const d = (await this.state.storage.get("directorio")) || { actualizado: null, nodos: [] };
    const ahora = Date.now() / 1000;
    return json({
      descripcion: "Réplica de solo lectura del directorio de nodos de FaroNet. Cada entrada " +
        "trae su forma binaria ('entrada', hex) y su firma Ed25519: verifíquela usted mismo " +
        "con verificar_directorio.py (también comprueba que id = BLAKE2b(llave)[:8]).",
      formato: "Ver(1) Id(8) Ed25519(32) Creada(4) Expira(4) Flags(1) LongAlias(1) Alias Firma(64)",
      actualizado: d.actualizado,
      nodos: d.nodos.filter((e: any) => e.expira > ahora),
    });
  }

  async temas(): Promise<Response> {
    const temas: Record<string, number> = (await this.state.storage.get("temas")) || {};
    const ahora = Date.now() / 1000;
    return json({ temas: Object.keys(temas).filter((t) => temas[t] > ahora).sort() });
  }
}
