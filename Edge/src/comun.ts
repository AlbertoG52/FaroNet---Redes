/**
 * comun.ts - Constantes y utilidades compartidas por el Worker de FaroNet.
 *
 * Solo se usa WebCrypto (la criptografía que trae Cloudflare Workers): no se
 * implementa ninguna primitiva criptográfica propia.
 */

export interface Env {
  SALAS: DurableObjectNamespaceLike;      // una sala (Durable Object) por tema público
  BUZONES: DurableObjectNamespaceLike;    // un buzón muerto por identificador
  CONTROL: DurableObjectNamespaceLike;    // autenticación del gateway, directorio, límites
  GATEWAY_LLAVE: string;                  // llave pública Ed25519 del gateway (hex, NO es secreta)
}

// Tipos mínimos de Durable Objects (los reales vienen de @cloudflare/workers-types).
export interface DurableObjectNamespaceLike {
  idFromName(nombre: string): unknown;
  get(id: unknown): { fetch(req: Request | string, init?: RequestInit): Promise<Response> };
}

export const VERSION = 1;
export const VENTANA_FIRMA_S = 60;          // el gateway firma con su hora; ±60 s
export const MAX_CUERPO_GATEWAY = 64 * 1024;
export const MAX_TEMA = 32;
export const MAX_TEXTO = 1024;
export const TTL_MAX_S = 24 * 3600;
export const TAM_BLOB = 1024;               // los blobs del buzón muerto miden SIEMPRE 1024 B
export const TTL_BUZON_DEF_S = 3600;
export const MAX_PUBLICACIONES_POR_SALA = 200;

/** Cabeceras de todas las respuestas: no cachear, no filtrar el origen, CSP estricta. */
export const CABECERAS_SEGURAS: Record<string, string> = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

export function json(datos: unknown, estado = 200): Response {
  return new Response(JSON.stringify(datos, null, 2), {
    status: estado,
    headers: { ...CABECERAS_SEGURAS, "Content-Type": "application/json; charset=utf-8" },
  });
}

export function error(estado: number, mensaje: string): Response {
  return json({ ok: false, error: mensaje }, estado);
}

export function hexABytes(hex: string): Uint8Array | null {
  if (!/^([0-9a-f]{2})*$/i.test(hex)) return null;
  const salida = new Uint8Array(hex.length / 2);
  for (let i = 0; i < salida.length; i++) salida[i] = parseInt(hex.substr(i * 2, 2), 16);
  return salida;
}

export function bytesAHex(b: Uint8Array): string {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function normalizarTema(tema: string): string | null {
  const t = decodeURIComponent(tema).trim().toLowerCase();
  const largo = new TextEncoder().encode(t).length;
  if (largo < 1 || largo > MAX_TEMA || /[\u0000-\u001f<>"'&]/.test(t)) return null;
  return t;
}

export function escaparXml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Verifica una firma Ed25519 con WebCrypto. */
export async function verificarEd25519(llave: Uint8Array, firma: Uint8Array,
                                       mensaje: Uint8Array): Promise<boolean> {
  if (llave.length !== 32 || firma.length !== 64) return false;
  try {
    const k = await crypto.subtle.importKey("raw", llave, { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "Ed25519" }, k, firma, mensaje);
  } catch {
    return false;
  }
}

/**
 * Mensaje que firma el gateway en cada petición (versión 1):
 *   "FARONET-GW-1\n" + MÉTODO + "\n" + RUTA + "\n" + TIEMPO + "\n" + NONCE + "\n" + CUERPO
 * TIEMPO (segundos Unix) y NONCE (16 bytes al azar, hex) evitan que una
 * petición capturada se pueda repetir (replay).
 */
export function mensajeFirmado(metodo: string, ruta: string, tiempo: string, nonce: string,
                               cuerpo: Uint8Array): Uint8Array {
  const cabecera = new TextEncoder().encode(
    `FARONET-GW-${VERSION}\n${metodo}\n${ruta}\n${tiempo}\n${nonce}\n`);
  const m = new Uint8Array(cabecera.length + cuerpo.length);
  m.set(cabecera, 0);
  m.set(cuerpo, cabecera.length);
  return m;
}

/**
 * Lee una entrada del directorio de FaroNet (formato binario v1, ver Mesh/identidad.py):
 *   Ver(1) Id(8) Ed25519(32) Creada(4) Expira(4) Flags(1) LongAlias(1) Alias Firma(64)
 * y verifica su firma Ed25519. La comprobación id = BLAKE2b(llave)[:8] no se puede hacer
 * aquí (WebCrypto no trae BLAKE2b); la hace cualquiera con verificar_directorio.py.
 */
export interface EntradaDirectorio {
  id: string; alias: string; llave_firma: string; creada: number; expira: number;
  gateway: boolean; firma: string; entrada: string; firma_verificada: boolean;
}

export async function leerEntrada(hex: string): Promise<EntradaDirectorio | null> {
  const b = hexABytes(hex);
  if (!b || b.length < 51 + 64 || b.length > 51 + 16 + 64) return null;
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  if (b[0] !== 1) return null;
  const largoAlias = b[50];
  if (largoAlias > 16 || b.length !== 51 + largoAlias + 64) return null;
  const contenido = b.slice(0, 51 + largoAlias);
  const firma = b.slice(51 + largoAlias);
  const llave = b.slice(9, 41);
  const ok = await verificarEd25519(llave, firma, contenido);
  if (!ok) return null;
  let alias: string;
  try {
    alias = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(b.slice(51, 51 + largoAlias));
  } catch {
    return null;
  }
  return {
    id: bytesAHex(b.slice(1, 9)), alias, llave_firma: bytesAHex(llave),
    creada: v.getUint32(41), expira: v.getUint32(45), gateway: (b[49] & 1) === 1,
    firma: bytesAHex(firma), entrada: hex.toLowerCase(), firma_verificada: true,
  };
}
