const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const ALLOWED_WEB_ORIGINS = new Set(["https://callewallerstedt.github.io"]);
let schemaReady = false;

function withCors(response, request) {
  const origin = request.headers.get("origin") || "";
  if (!ALLOWED_WEB_ORIGINS.has(origin)) return response;
  const result = new Response(response.body, response);
  result.headers.set("access-control-allow-origin", origin);
  result.headers.set("access-control-allow-headers", "authorization, content-type");
  result.headers.set("access-control-allow-methods", "GET, POST, PATCH, DELETE, OPTIONS");
  result.headers.set("access-control-max-age", "86400");
  result.headers.append("vary", "Origin");
  return result;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function now() { return Date.now(); }

function randomString(length = 32, alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, (value) => alphabet[value % alphabet.length]).join("");
}

function makePairingCode() {
  const raw = randomString(20, CODE_ALPHABET);
  return `AIOS-${raw.slice(0, 5)}-${raw.slice(5, 10)}-${raw.slice(10, 15)}-${raw.slice(15, 20)}`;
}

function normalizeCode(value) {
  return String(value || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
}

async function hash(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function body(request) {
  try { return await request.json(); } catch { return {}; }
}

async function ensureSchema(env) {
  if (schemaReady) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, platform TEXT, status_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_seen INTEGER NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, machine_id TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER, completed_at INTEGER, result_json TEXT)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, machine_id TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS commands_machine_pending ON commands(machine_id, claimed_at, id)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS events_machine_id ON events(machine_id, id)")
  ]);
  schemaReady = true;
}

function bearer(request) {
  const value = request.headers.get("authorization") || "";
  return value.toLowerCase().startsWith("bearer ") ? value.slice(7).trim() : "";
}

async function userAccount(request, env) {
  const token = bearer(request);
  if (!token) return null;
  const row = await env.DB.prepare("SELECT account_id, expires_at FROM sessions WHERE token_hash = ?")
    .bind(await hash(token)).first();
  if (!row || Number(row.expires_at) < now()) return null;
  return String(row.account_id);
}

async function machineAccount(request, env) {
  const token = request.headers.get("x-aios-machine-token") || "";
  if (!token) return null;
  return env.DB.prepare("SELECT id, account_id, name FROM machines WHERE token_hash = ?")
    .bind(await hash(token)).first();
}

async function createSession(env, accountId) {
  const token = randomString(48);
  const timestamp = now();
  await env.DB.prepare("INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)")
    .bind(await hash(token), accountId, timestamp, timestamp + 1000 * 60 * 60 * 24 * 90).run();
  return token;
}

function safeJson(value, fallback = {}) {
  try { return JSON.parse(value || ""); } catch { return fallback; }
}

async function handleApi(request, env, url) {
  await ensureSchema(env);
  const path = url.pathname;

  if (path === "/api/health") return json({ ok: true, service: "aiOS Remote" });

  if (path === "/api/account/create" && request.method === "POST") {
    const accountId = crypto.randomUUID();
    const code = makePairingCode();
    await env.DB.prepare("INSERT INTO accounts (id, code_hash, created_at) VALUES (?, ?, ?)")
      .bind(accountId, await hash(normalizeCode(code)), now()).run();
    return json({ code, token: await createSession(env, accountId) }, 201);
  }

  if (path === "/api/account/login" && request.method === "POST") {
    const input = await body(request);
    const codeHash = await hash(normalizeCode(input.code));
    const account = await env.DB.prepare("SELECT id FROM accounts WHERE code_hash = ?").bind(codeHash).first();
    if (!account) return json({ error: "That private code was not recognized." }, 401);
    return json({ token: await createSession(env, String(account.id)) });
  }

  if (path === "/api/machines/pair" && request.method === "POST") {
    const input = await body(request);
    const account = await env.DB.prepare("SELECT id FROM accounts WHERE code_hash = ?")
      .bind(await hash(normalizeCode(input.code))).first();
    if (!account) return json({ error: "That private code was not recognized." }, 401);
    const id = crypto.randomUUID();
    const token = randomString(48);
    const timestamp = now();
    const name = String(input.name || "My computer").trim().slice(0, 80) || "My computer";
    const platform = String(input.platform || "Windows").slice(0, 120);
    await env.DB.prepare("INSERT INTO machines (id, account_id, name, token_hash, platform, status_json, created_at, updated_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .bind(id, String(account.id), name, await hash(token), platform, "{}", timestamp, timestamp, timestamp).run();
    return json({ machine_id: id, machine_token: token, name }, 201);
  }

  if (path.startsWith("/api/agent/")) {
    const machine = await machineAccount(request, env);
    if (!machine) return json({ error: "Machine authentication failed." }, 401);
    const timestamp = now();
    await env.DB.prepare("UPDATE machines SET last_seen = ?, updated_at = ? WHERE id = ?")
      .bind(timestamp, timestamp, machine.id).run();

    if (path === "/api/agent/commands" && request.method === "GET") {
      const result = await env.DB.prepare("SELECT id, type, payload_json, created_at FROM commands WHERE machine_id = ? AND claimed_at IS NULL ORDER BY id LIMIT 10")
        .bind(machine.id).all();
      const rows = result.results || [];
      if (rows.length) {
        await env.DB.batch(rows.map((row) => env.DB.prepare("UPDATE commands SET claimed_at = ? WHERE id = ? AND claimed_at IS NULL").bind(timestamp, row.id)));
      }
      return json({ commands: rows.map((row) => ({ id: row.id, type: row.type, payload: safeJson(row.payload_json), created_at: row.created_at })) });
    }

    if (path === "/api/agent/events" && request.method === "POST") {
      const input = await body(request);
      const events = Array.isArray(input.events) ? input.events.slice(0, 100) : [];
      if (events.length) {
        await env.DB.batch(events.map((event) => env.DB.prepare("INSERT INTO events (account_id, machine_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)")
          .bind(machine.account_id, machine.id, String(event.type || "log").slice(0, 40), JSON.stringify(event.payload || {}), Number(event.created_at || timestamp))));
      }
      if (input.status) {
        await env.DB.prepare("UPDATE machines SET status_json = ?, last_seen = ?, updated_at = ? WHERE id = ?")
          .bind(JSON.stringify(input.status), timestamp, timestamp, machine.id).run();
      }
      if (input.completed_command_id) {
        await env.DB.prepare("UPDATE commands SET completed_at = ?, result_json = ? WHERE id = ? AND machine_id = ?")
          .bind(timestamp, JSON.stringify(input.result || {}), Number(input.completed_command_id), machine.id).run();
      }
      return json({ ok: true });
    }

    if (path.startsWith("/api/agent/frame/") && request.method === "PUT") {
      const monitor = path.split("/").pop().replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 32) || "primary";
      const key = `frames/${machine.account_id}/${machine.id}/${monitor}.jpg`;
      await env.FILES.put(key, request.body, {
        httpMetadata: { contentType: request.headers.get("content-type") || "image/jpeg" },
        customMetadata: {
          updatedAt: String(timestamp),
          sequence: String(request.headers.get("x-aios-frame-seq") || "")
        }
      });
      // Status/event heartbeats already keep last_seen current. Avoid a D1
      // write for every video frame (10-20 writes per second per computer).
      return json({ ok: true, updated_at: timestamp });
    }
  }

  const accountId = await userAccount(request, env);
  if (!accountId) return json({ error: "Please unlock aiOS Remote again." }, 401);

  if (path === "/api/machines" && request.method === "GET") {
    const result = await env.DB.prepare("SELECT id, name, platform, status_json, created_at, updated_at, last_seen FROM machines WHERE account_id = ? ORDER BY last_seen DESC")
      .bind(accountId).all();
    return json({ machines: (result.results || []).map((row) => ({
      id: row.id, name: row.name, platform: row.platform, status: safeJson(row.status_json),
      created_at: row.created_at, updated_at: row.updated_at, last_seen: row.last_seen,
      online: now() - Number(row.last_seen) < 30000
    })) });
  }

  const commandMatch = path.match(/^\/api\/machines\/([^/]+)\/commands$/);
  if (commandMatch && request.method === "POST") {
    const machineId = commandMatch[1];
    const machine = await env.DB.prepare("SELECT id FROM machines WHERE id = ? AND account_id = ?").bind(machineId, accountId).first();
    if (!machine) return json({ error: "Computer not found." }, 404);
    const input = await body(request);
    const allowed = new Set(["prompt", "followup", "stop", "config", "clarify", "stream", "update", "codex_switch"]);
    if (!allowed.has(input.type)) return json({ error: "Unsupported command." }, 400);
    const payload = input.payload && typeof input.payload === "object" ? input.payload : {};
    if (input.type === "clarify") {
      await env.DB.prepare("DELETE FROM commands WHERE machine_id = ? AND account_id = ? AND type = 'clarify' AND claimed_at IS NULL")
        .bind(machineId, accountId).run();
    }
    if (input.type === "stream") {
      // A stream command is only a short lease heartbeat. Keep one row per
      // machine instead of growing the command table while the viewer is open.
      await env.DB.prepare("DELETE FROM commands WHERE machine_id = ? AND account_id = ? AND type = 'stream'")
        .bind(machineId, accountId).run();
    }
    const result = await env.DB.prepare("INSERT INTO commands (account_id, machine_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(accountId, machineId, input.type, JSON.stringify(payload), now()).run();
    return json({ ok: true, command_id: result.meta?.last_row_id }, 202);
  }

  const eventsMatch = path.match(/^\/api\/machines\/([^/]+)\/events$/);
  if (eventsMatch && request.method === "DELETE") {
    const machineId = eventsMatch[1];
    const machine = await env.DB.prepare("SELECT id FROM machines WHERE id = ? AND account_id = ?").bind(machineId, accountId).first();
    if (!machine) return json({ error: "Computer not found." }, 404);
    await env.DB.prepare("DELETE FROM events WHERE machine_id = ? AND account_id = ?").bind(machineId, accountId).run();
    return json({ ok: true });
  }
  if (eventsMatch && request.method === "GET") {
    const machineId = eventsMatch[1];
    const machine = await env.DB.prepare("SELECT id FROM machines WHERE id = ? AND account_id = ?").bind(machineId, accountId).first();
    if (!machine) return json({ error: "Computer not found." }, 404);
    const since = Math.max(0, Number(url.searchParams.get("since") || 0));
    const result = await env.DB.prepare("SELECT id, type, payload_json, created_at FROM events WHERE machine_id = ? AND id > ? ORDER BY id LIMIT 200")
      .bind(machineId, since).all();
    const events = (result.results || []).map((row) => ({ id: row.id, type: row.type, payload: safeJson(row.payload_json), created_at: row.created_at }));
    return json({ events, cursor: events.length ? events[events.length - 1].id : since });
  }

  const frameMatch = path.match(/^\/api\/machines\/([^/]+)\/frame\/([^/]+)$/);
  if (frameMatch && request.method === "GET") {
    const machineId = frameMatch[1];
    const monitor = frameMatch[2].replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 32) || "primary";
    const machine = await env.DB.prepare("SELECT id FROM machines WHERE id = ? AND account_id = ?").bind(machineId, accountId).first();
    if (!machine) return json({ error: "Computer not found." }, 404);
    const object = await env.FILES.get(`frames/${accountId}/${machineId}/${monitor}.jpg`);
    if (!object) return json({ error: "No screenshot yet." }, 404);
    const headers = new Headers({ "content-type": object.httpMetadata?.contentType || "image/jpeg", "cache-control": "no-store" });
    headers.set("x-aios-updated-at", object.customMetadata?.updatedAt || "");
    headers.set("x-aios-frame-seq", object.customMetadata?.sequence || "");
    return new Response(object.body, { headers });
  }

  const renameMatch = path.match(/^\/api\/machines\/([^/]+)$/);
  if (renameMatch && request.method === "PATCH") {
    const input = await body(request);
    const name = String(input.name || "").trim().slice(0, 80);
    if (!name) return json({ error: "Enter a computer name." }, 400);
    await env.DB.prepare("UPDATE machines SET name = ?, updated_at = ? WHERE id = ? AND account_id = ?")
      .bind(name, now(), renameMatch[1], accountId).run();
    return json({ ok: true });
  }

  return json({ error: "Not found." }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/api/")) {
        if (request.method === "OPTIONS") return withCors(new Response(null, { status: 204 }), request);
        return withCors(await handleApi(request, env, url), request);
      }
      return env.ASSETS.fetch(request);
    } catch (error) {
      console.error(error);
      return withCors(json({ error: "The relay hit an unexpected error." }, 500), request);
    }
  }
};
