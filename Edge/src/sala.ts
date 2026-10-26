/**
 * sala.ts - Durable Object "Sala": una por cada tema público.
 *
 * Guarda las publicaciones del tema (solo hasta que vence su TTL) y las
 * reparte en tiempo real a los navegadores conectados por WebSocket. También
 * genera el feed Atom del tema.
 *
 * Usa la API de WebSockets con hibernación de Durable Objects: el objeto
 * puede "dormirse" con las conexiones abiertas, sin gastar tiempo de CPU.
 */
import { CABECERAS_SEGURAS, MAX_PUBLICACIONES_POR_SALA, error, escaparXml, json } from "./comun.ts";

interface Publicacion { id: string; texto: string; creada: number; vence: number }

export class Sala {
  state: any;
  env: any;

  constructor(state: any, env: any) {
    this.state = state;
    this.env = env;
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const accion = url.pathname.split("/").pop();
    if (accion === "publicar" && req.method === "POST") return this.publicar(await req.json());
    if (accion === "ws") return this.conectar(req);
    if (accion === "historial") return json({ ok: true, publicaciones: await this.vigentes() });
    if (accion === "feed") return this.feed(url.searchParams.get("tema") || "", url.searchParams.get("base") || "");
    return error(404, "acción desconocida");
  }

  async vigentes(): Promise<Publicacion[]> {
    const ahora = Date.now() / 1000;
    const todas = await this.state.storage.list({ prefix: "p:" });
    return [...todas.values()].filter((p: Publicacion) => p.vence > ahora);
  }

  async publicar(p: Publicacion): Promise<Response> {
    const clave = `p:${String(p.creada).padStart(10, "0")}:${p.id}`;
    if (await this.state.storage.get(clave)) return json({ ok: true, duplicada: true });
    await this.state.storage.put(clave, p);
    // límite de publicaciones guardadas: se borran las más viejas
    const claves = [...(await this.state.storage.list({ prefix: "p:" })).keys()];
    if (claves.length > MAX_PUBLICACIONES_POR_SALA) {
      await this.state.storage.delete(claves.slice(0, claves.length - MAX_PUBLICACIONES_POR_SALA));
    }
    await this.programarLimpieza();
    const mensaje = JSON.stringify({ tipo: "publicacion", ...p });
    for (const ws of this.state.getWebSockets()) {
      try { ws.send(mensaje); } catch { /* conexión cerrada */ }
    }
    return json({ ok: true });
  }

  async programarLimpieza(): Promise<void> {
    const lista = await this.vigentes();
    if (!lista.length) return;
    const proxima = Math.min(...lista.map((p) => p.vence)) * 1000;
    const actual = await this.state.storage.getAlarm();
    if (actual === null || actual > proxima) await this.state.storage.setAlarm(proxima);
  }

  /** Alarma: borra lo vencido (TTL) y programa la siguiente limpieza. */
  async alarm(): Promise<void> {
    const ahora = Date.now() / 1000;
    const todas = await this.state.storage.list({ prefix: "p:" });
    const vencidas = [...todas.entries()].filter(([, p]: any) => p.vence <= ahora).map(([k]) => k);
    if (vencidas.length) await this.state.storage.delete(vencidas);
    await this.programarLimpieza();
  }

  async conectar(req: Request): Promise<Response> {
    if (req.headers.get("Upgrade") !== "websocket") return error(426, "se esperaba un WebSocket");
    // @ts-ignore WebSocketPair existe en el runtime de Workers
    const par = new WebSocketPair();
    const [cliente, servidor] = Object.values(par) as any[];
    this.state.acceptWebSocket(servidor);
    servidor.send(JSON.stringify({ tipo: "historial", publicaciones: await this.vigentes() }));
    return new Response(null, { status: 101, webSocket: cliente } as any);
  }

  // Las salas son de solo lectura: lo que mande el navegador se ignora.
  async webSocketMessage(_ws: any, _mensaje: any): Promise<void> {}

  async webSocketClose(ws: any, codigo: number): Promise<void> {
    try { ws.close(codigo, "adiós"); } catch { /* ya cerrada */ }
  }

  async feed(tema: string, base: string): Promise<Response> {
    const lista = (await this.vigentes()).reverse();
    const actualizado = new Date((lista[0]?.creada ?? Date.now() / 1000) * 1000).toISOString();
    const enlace = `${base}/sala/${encodeURIComponent(tema)}`;
    const entradas = lista.map((p) => `
  <entry>
    <id>urn:faronet:${escaparXml(p.id)}</id>
    <title>${escaparXml(p.texto.slice(0, 60))}</title>
    <updated>${new Date(p.creada * 1000).toISOString()}</updated>
    <author><name>anónimo</name></author>
    <content type="text">${escaparXml(p.texto)}</content>
    <link href="${escaparXml(enlace)}"/>
  </entry>`).join("");
    const xml = `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:faronet:tema:${escaparXml(tema)}</id>
  <title>FaroNet · #${escaparXml(tema)}</title>
  <subtitle>Publicaciones públicas y anónimas que salieron de FaroNet</subtitle>
  <updated>${actualizado}</updated>
  <link rel="self" href="${escaparXml(`${base}/tema/${encodeURIComponent(tema)}/feed.xml`)}"/>
  <link href="${escaparXml(enlace)}"/>${entradas}
</feed>
`;
    return new Response(xml, {
      headers: { ...CABECERAS_SEGURAS, "Content-Type": "application/atom+xml; charset=utf-8" },
    });
  }
}
