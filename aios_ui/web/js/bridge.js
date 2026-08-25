// Transport to the Python backend.
//
// Two channels, on purpose:
//   api()    request/response over fetch, for things the UI initiates
//   stream() server-sent events, for things the backend initiates
//
// The Tk build had only the first kind, so it had to poll -- 250ms for the
// transcript, 900ms for the session list. Every poll was a stutter you could
// see. Push removes the polling entirely.

const TOKEN = new URLSearchParams(location.search).get("token") || "";

function withToken(path) {
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}token=${encodeURIComponent(TOKEN)}`;
}

export async function api(path, { method = "GET", body = null, timeout = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeout) || 15000));
  try {
    const response = await fetch(withToken(path), {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    if (!response.ok) return { ok: false, error: `HTTP ${response.status}` };
    return await response.json();
  } catch (error) {
    const message = error && error.name === "AbortError"
      ? `Backend timed out after ${Math.round((Number(timeout) || 15000) / 1000)}s`
      : String(error);
    return { ok: false, error: message };
  } finally {
    clearTimeout(timer);
  }
}

export function authenticatedUrl(path) {
  return withToken(path);
}

/**
 * Open an SSE stream. Returns a handle with .close().
 *
 * EventSource reconnects on its own when the socket drops, but it would replay
 * from `since=0` and duplicate the transcript. So we own the retry: the caller
 * supplies a fresh URL each attempt via urlFor(), which carries the current
 * cursor.
 */
export function stream(urlFor, handlers = {}) {
  let source = null;
  let closed = false;
  let retry = 400;

  function connect() {
    if (closed) return;
    source = new EventSource(withToken(urlFor()));

    for (const [event, handler] of Object.entries(handlers)) {
      source.addEventListener(event, (message) => {
        retry = 400; // a delivered frame proves the link is healthy
        let payload = {};
        try {
          payload = JSON.parse(message.data);
        } catch {
          return;
        }
        handler(payload);
      });
    }

    source.onerror = () => {
      if (closed) return;
      source.close();
      // Back off so a backend that is down does not spin the CPU.
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 5000);
    };
  }

  connect();
  return {
    close() {
      closed = true;
      if (source) source.close();
    },
  };
}

/** pywebview's native bridge, for window-level things HTML cannot do. */
export function native(method, ...args) {
  const bridge = window.pywebview && window.pywebview.api;
  if (!bridge || typeof bridge[method] !== "function") return Promise.resolve(null);
  return bridge[method](...args);
}
