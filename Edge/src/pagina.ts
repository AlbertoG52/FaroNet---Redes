/**
 * pagina.ts - Páginas HTML que ve el navegador: inicio y sala en tiempo real.
 * Todo el contenido de los usuarios se inserta con textContent (nunca como
 * HTML), y una Content-Security-Policy con nonce bloquea cualquier otro script.
 */
import { CABECERAS_SEGURAS, escaparXml } from "./comun.ts";

const ESTILO = `
  :root { --fondo:#f6f5f2; --texto:#1d1d1f; --suave:#6b6b70; --borde:#dcdad4; --acento:#b5541c; --tarjeta:#fff; }
  @media (prefers-color-scheme: dark) { :root { --fondo:#141414; --texto:#ececec; --suave:#9a9a9f; --borde:#2c2c2e; --acento:#f08a4b; --tarjeta:#1d1d1f; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--fondo); color:var(--texto); font:16px/1.5 system-ui, sans-serif; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 1.6rem; margin: 0 0 4px; } h2 { font-size: 1.1rem; margin: 28px 0 8px; }
  .sub { color: var(--suave); margin: 0 0 20px; }
  a { color: var(--acento); }
  ul { padding-left: 20px; }
  .pub { background: var(--tarjeta); border: 1px solid var(--borde); border-radius: 10px; padding: 12px 14px; margin: 10px 0; overflow-wrap: anywhere; }
  .meta { color: var(--suave); font-size: .85rem; margin-top: 4px; }
  .estado { font-size: .85rem; color: var(--suave); }
  code { font-size: .9em; }
`;

function responder(html: string, nonce: string): Response {
  return new Response(html, {
    headers: {
      ...CABECERAS_SEGURAS,
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy":
        `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; form-action 'none'`,
    },
  });
}

export function nuevoNonce(): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
}

export function paginaInicio(temas: string[]): Response {
  const nonce = nuevoNonce();
  const lista = temas.length
    ? temas.map((t) => {
        const e = encodeURIComponent(t);
        return `<li><a href="/sala/${e}">#${escaparXml(t)}</a> · <a href="/tema/${e}/feed.xml">feed Atom</a></li>`;
      }).join("")
    : "<li>Todavía no hay publicaciones públicas vigentes.</li>";
  return responder(`<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FaroNet edge</title>
<style>${ESTILO}</style></head><body><main>
<h1>FaroNet</h1>
<p class="sub">La ventana pública de una red mesh anónima que viaja por luz.</p>
<h2>Temas públicos</h2><ul>${lista}</ul>
<h2>Otros servicios</h2>
<ul>
  <li><a href="/directorio">Directorio público de nodos</a> (entradas firmadas, verificables)</li>
  <li>Buzón muerto: <code>PUT</code> / <code>GET /buzon/&lt;id de 64 hex&gt;</code> con blobs cifrados de 1024 bytes</li>
</ul>
<h2>Privacidad</h2>
<p>Este sitio no guarda su IP ni su navegador. Solo los temas marcados como públicos salen de FaroNet,
y no llevan autor. Los mensajes privados y los temas privados nunca llegan aquí.</p>
</main></body></html>`, nonce);
}

export function paginaSala(tema: string): Response {
  const nonce = nuevoNonce();
  const seguro = escaparXml(tema);
  const enlace = encodeURIComponent(tema);
  return responder(`<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>#${seguro} · FaroNet</title>
<style>${ESTILO}</style></head><body><main>
<p><a href="/">← FaroNet</a></p>
<h1>#${seguro}</h1>
<p class="sub">Sala pública en tiempo real · <a href="/tema/${enlace}/feed.xml">feed Atom</a> ·
<span class="estado" id="estado">conectando…</span></p>
<div id="lista"></div>
<script nonce="${nonce}">
  const lista = document.getElementById("lista");
  const estado = document.getElementById("estado");
  const vistos = new Set();
  function mostrar(p) {
    if (vistos.has(p.id)) return;
    vistos.add(p.id);
    const caja = document.createElement("div"); caja.className = "pub";
    const texto = document.createElement("div"); texto.textContent = p.texto;
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = "anónimo · " + new Date(p.creada * 1000).toLocaleString();
    caja.append(texto, meta); lista.prepend(caja);
  }
  function conectar() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/sala/${enlace}/ws");
    ws.onopen = () => estado.textContent = "en vivo";
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.tipo === "historial") m.publicaciones.forEach(mostrar);
      else if (m.tipo === "publicacion") mostrar(m);
    };
    ws.onclose = () => { estado.textContent = "reconectando…"; setTimeout(conectar, 3000); };
  }
  conectar();
</script>
</main></body></html>`, nonce);
}
