/**
 * runtime.ts - Simulación en memoria del runtime de Cloudflare (Durable Objects:
 * almacenamiento, alarmas, WebSockets) para probar el Worker sin Cloudflare.
 */
// ------------------------------------------------------------ simulación del runtime
export class Almacen {
  datos = new Map<string, any>();
  alarma: number | null = null;
  async get(k: any) { return Array.isArray(k) ? new Map(k.map((x) => [x, this.datos.get(x)])) : this.datos.get(k); }
  async put(k: any, v?: any) {
    if (typeof k === "string") this.datos.set(k, structuredClone(v));
    else for (const [a, b] of Object.entries(k)) this.datos.set(a, structuredClone(b));
  }
  async delete(k: any) { for (const x of [].concat(k)) this.datos.delete(x); }
  async deleteAll() { this.datos.clear(); }
  async list({ prefix = "", limit = Infinity } = {}) {
    const claves = [...this.datos.keys()].filter((k) => k.startsWith(prefix)).sort().slice(0, limit);
    return new Map(claves.map((k) => [k, structuredClone(this.datos.get(k))]));
  }
  async getAlarm() { return this.alarma; }
  async setAlarm(t: number) { this.alarma = t; }
  async deleteAlarm() { this.alarma = null; }
}

class SocketFalso {
  recibidos: any[] = [];
  otro: SocketFalso | null = null;
  send(m: string) { this.otro!.recibidos.push(JSON.parse(m)); }
  close() {}
}
(globalThis as any).WebSocketPair = class {
  0: SocketFalso; 1: SocketFalso;
  constructor() {
    this[0] = new SocketFalso(); this[1] = new SocketFalso();
    this[0].otro = this[1]; this[1].otro = this[0];
  }
};
const ResponseOriginal = Response;
(globalThis as any).Response = class extends ResponseOriginal {
  webSocket: any;
  constructor(cuerpo: any, init: any = {}) {
    if (init.status === 101) { super(null, { status: 200 }); this.webSocket = init.webSocket; }
    else super(cuerpo, init);
  }
};


export function espacio(Clase: any, env: any) {
  const instancias = new Map<string, any>();
  return {
    instancias,
    idFromName: (n: string) => n,
    get(id: string) {
      if (!instancias.has(id)) {
        const sockets: any[] = [];
        const state = { storage: new Almacen(), acceptWebSocket: (ws: any) => sockets.push(ws), getWebSockets: () => sockets };
        instancias.set(id, new Clase(state, env));
      }
      const obj = instancias.get(id);
      return { fetch: (r: any, init?: any) => obj.fetch(r instanceof Request ? r : new Request(r, init)) };
    },
  };
}


export async function crearEntorno(llaveGatewayHex: string) {
  const { default: worker, Sala, Buzon, Control } = await import("../src/index.ts");
  const env: any = { GATEWAY_LLAVE: llaveGatewayHex };
  env.SALAS = espacio(Sala, env);
  env.BUZONES = espacio(Buzon, env);
  env.CONTROL = espacio(Control, env);
  return { worker, env };
}
