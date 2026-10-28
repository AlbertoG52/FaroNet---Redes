/**
 * index.ts - Worker de FaroNet en el edge (Cloudflare Workers, plan gratuito).
 *
 *   GET  /                          página de inicio con los temas públicos
 *   GET  /sala/<tema>               sala pública en tiempo real (navegador)
 *   GET  /sala/<tema>/ws            WebSocket de la sala
 *   GET  /tema/<tema>/feed.xml      feed Atom del tema
 *   GET  /tema/<tema>/historial     publicaciones vigentes (JSON)
 *   GET  /directorio                réplica pública del directorio de nodos (JSON)
 *   PUT  /buzon/<id>                buzón muerto: depositar un blob cifrado de 1024 B
 *   GET  /buzon/<id>                buzón muerto: recoger (y borrar) el blob
 *   POST /gw/publicacion            gateway -> edge (firmado con Ed25519)
 *   POST /gw/directorio             gateway -> edge (firmado con Ed25519)
 *
 * Política de no registro: este código NO guarda CF-Connecting-IP, User-Agent
 * ni ningún identificador. La IP solo se usa, en memoria, para el límite de
 * peticiones (ver control.ts). Las bitácoras de Cloudflare están desactivadas
 * en wrangler.toml ([observability] enabled = false).
 */
import { type Env, error, normalizarTema } from "./comun.ts";
import { paginaInicio, paginaSala } from "./pagina.ts";

export { Buzon } from "./buzon.ts";
export { Control } from "./control.ts";
export { Sala } from "./sala.ts";

function control(env: Env) {
  return env.CONTROL.get(env.CONTROL.idFromName("control"));
}

async function permitido(req: Request, env: Env, categoria: string): Promise<boolean> {
  const r = await control(env).fetch("https://control/limite", {
    method: "POST",
    headers: {
      "X-Categoria": categoria,
      // solo en tránsito hacia el contador en memoria; nunca se guarda
      "X-Cliente": req.headers.get("CF-Connecting-IP") || "desconocido",
    },
  });
  return ((await r.json()) as any).permitido === true;
}

function sala(env: Env, tema: string) {
  return env.SALAS.get(env.SALAS.idFromName(tema));
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const partes = url.pathname.split("/").filter(Boolean);
    try {
      // --- gateway (autenticado con firma) ---
      if (partes[0] === "gw") {
        if (req.method !== "POST") return error(405, "use POST");
        if (!(await permitido(req, env, "gw"))) return error(429, "demasiadas peticiones");
        return control(env).fetch(new Request(`https://control${url.pathname}`, req));
      }

      // --- buzón muerto ---
      if (partes[0] === "buzon" && partes.length === 2) {
        if (!/^[0-9a-f]{64}$/.test(partes[1])) return error(400, "el id debe tener 64 caracteres hex");
        if (!["GET", "PUT"].includes(req.method)) return error(405, "use PUT o GET");
        if (!(await permitido(req, env, "buzon"))) return error(429, "demasiadas peticiones");
        const buzon = env.BUZONES.get(env.BUZONES.idFromName(partes[1]));
        return buzon.fetch(new Request("https://buzon/", req));
      }

      if (req.method !== "GET") return error(405, "método no permitido");
      const esWs = req.headers.get("Upgrade") === "websocket";
      if (!(await permitido(req, env, esWs ? "ws" : "lectura"))) return error(429, "demasiadas peticiones");

      if (partes.length === 0) {
        const r = await control(env).fetch("https://control/temas");
        return paginaInicio(((await r.json()) as any).temas);
      }
      if (partes[0] === "directorio" && partes.length === 1) {
        return control(env).fetch("https://control/directorio");
      }
      if ((partes[0] === "sala" || partes[0] === "tema") && partes.length >= 2) {
        const tema = normalizarTema(partes[1]);
        if (!tema) return error(400, "tema inválido");
        if (partes[0] === "sala" && partes.length === 2) return paginaSala(tema);
        if (partes[0] === "sala" && partes[2] === "ws") {
          return sala(env, tema).fetch(new Request("https://sala/ws", req));
        }
        if (partes[0] === "tema" && partes[2] === "feed.xml") {
          const base = `${url.protocol}//${url.host}`;
          return sala(env, tema).fetch(`https://sala/feed?tema=${encodeURIComponent(tema)}&base=${encodeURIComponent(base)}`);
        }
        if (partes[0] === "tema" && partes[2] === "historial") {
          return sala(env, tema).fetch("https://sala/historial");
        }
      }
      return error(404, "no encontrado");
    } catch {
      return error(500, "error interno");   // sin detalles: no se filtra nada
    }
  },
};
