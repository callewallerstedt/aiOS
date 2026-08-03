const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const ALLOWED_WEB_ORIGINS = new Set(["https://callewallerstedt.github.io"]);
// Phone attachments travel through R2, not the command row: a photo is orders
// of magnitude larger than anything D1 should hold.
const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
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

/* ── web push (RFC 8291 aes128gcm + RFC 8292 VAPID) ─────────────────────
 *
 * A phone that is asleep runs no JavaScript, so the app itself can never
 * raise the alert — on iOS a home-screen web app is suspended seconds after
 * you put it down. The relay has to push, which means encrypting the payload
 * to the subscription's own key here in the worker.
 */

const VAPID_SUBJECT = "mailto:aios-remote@users.noreply.github.com";
const PUSH_TTL_SECONDS = 3600;

export function base64UrlEncode(bytes) {
  let binary = "";
  const view = new Uint8Array(bytes);
  for (let index = 0; index < view.length; index += 1) binary += String.fromCharCode(view[index]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function base64UrlDecode(value) {
  const padded = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function concatBytes(...chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.length; }
  return out;
}

async function hkdf(salt, ikm, info, length) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, length * 8);
  return new Uint8Array(bits);
}

/** The application server keypair every subscription is bound to. Generated
 *  once and kept in D1 — rotating it silently invalidates every phone. */
export async function vapidKeys(env) {
  const stored = await env.DB.prepare("SELECT value FROM settings WHERE key = 'vapid'").first();
  if (stored?.value) return JSON.parse(String(stored.value));
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const keys = {
    publicKey: base64UrlEncode(await crypto.subtle.exportKey("raw", pair.publicKey)),
    privateKey: await crypto.subtle.exportKey("jwk", pair.privateKey)
  };
  await env.DB.prepare("INSERT OR IGNORE INTO settings (key, value, created_at) VALUES ('vapid', ?, ?)")
    .bind(JSON.stringify(keys), now()).run();
  // Another request may have won the race; the stored pair is the real one.
  const settled = await env.DB.prepare("SELECT value FROM settings WHERE key = 'vapid'").first();
  return settled?.value ? JSON.parse(String(settled.value)) : keys;
}

export async function vapidHeader(env, endpoint) {
  const keys = await vapidKeys(env);
  const audience = new URL(endpoint).origin;
  const header = base64UrlEncode(new TextEncoder().encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const claims = base64UrlEncode(new TextEncoder().encode(JSON.stringify({
    aud: audience,
    exp: Math.floor(now() / 1000) + 12 * 60 * 60,
    sub: VAPID_SUBJECT
  })));
  const signingKey = await crypto.subtle.importKey(
    "jwk", { ...keys.privateKey, key_ops: ["sign"] }, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" }, signingKey, new TextEncoder().encode(`${header}.${claims}`));
  return {
    authorization: `vapid t=${header}.${claims}.${base64UrlEncode(signature)}, k=${keys.publicKey}`,
    publicKey: keys.publicKey
  };
}

/** Encrypt one push payload for one subscription (aes128gcm, single record). */
export async function encryptPushPayload(plaintext, subscriberPublic, authSecret, options = {}) {
  const salt = options.salt || crypto.getRandomValues(new Uint8Array(16));
  const local = options.localKeys || await crypto.subtle.generateKey(
    { name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const localPublic = new Uint8Array(await crypto.subtle.exportKey("raw", local.publicKey));
  const shared = new Uint8Array(await crypto.subtle.deriveBits(
    { name: "ECDH", public: await crypto.subtle.importKey("raw", subscriberPublic, { name: "ECDH", namedCurve: "P-256" }, false, []) },
    local.privateKey,
    256
  ));

  const encoder = new TextEncoder();
  const keyInfo = concatBytes(encoder.encode("WebPush: info"), new Uint8Array([0]), subscriberPublic, localPublic);
  const ikm = await hkdf(authSecret, shared, keyInfo, 32);
  const cek = await hkdf(salt, ikm, concatBytes(encoder.encode("Content-Encoding: aes128gcm"), new Uint8Array([0])), 16);
  const nonce = await hkdf(salt, ikm, concatBytes(encoder.encode("Content-Encoding: nonce"), new Uint8Array([0])), 12);

  const aesKey = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  const padded = concatBytes(plaintext, new Uint8Array([2]));   // last record delimiter
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, aesKey, padded));

  const recordSize = new Uint8Array(4);
  new DataView(recordSize.buffer).setUint32(0, 4096);
  return concatBytes(salt, recordSize, new Uint8Array([localPublic.length]), localPublic, ciphertext);
}

async function sendPush(env, subscription, payload) {
  const bodyBytes = await encryptPushPayload(
    new TextEncoder().encode(JSON.stringify(payload)),
    base64UrlDecode(subscription.p256dh),
    base64UrlDecode(subscription.auth)
  );
  const vapid = await vapidHeader(env, subscription.endpoint);
  return fetch(subscription.endpoint, {
    method: "POST",
    headers: {
      "content-encoding": "aes128gcm",
      "content-type": "application/octet-stream",
      "content-length": String(bodyBytes.length),
      ttl: String(PUSH_TTL_SECONDS),
      urgency: payload.requireInteraction ? "high" : "normal",
      authorization: vapid.authorization
    },
    body: bodyBytes
  });
}

/** Alert every phone on this account. Dead subscriptions clean themselves up. */
async function pushToAccount(env, accountId, payload) {
  const result = await env.DB.prepare("SELECT endpoint, p256dh, auth FROM push_subs WHERE account_id = ?")
    .bind(accountId).all();
  const subscriptions = result.results || [];
  for (const subscription of subscriptions) {
    try {
      const response = await sendPush(env, subscription, payload);
      if (response.status === 404 || response.status === 410) {
        await env.DB.prepare("DELETE FROM push_subs WHERE endpoint = ?").bind(subscription.endpoint).run();
      }
    } catch (error) {
      console.error("push failed", error);
    }
  }
}

/** The moments worth waking a phone for. Anything else is just activity. */
export function alertFor(event, machineName) {
  const type = String(event?.type || "").toLowerCase();
  const payload = event?.payload && typeof event.payload === "object" ? event.payload : {};
  const on = machineName ? ` · ${machineName}` : "";
  if (type === "ask") {
    return {
      title: "OPERATOR needs your input",
      body: String(payload.message || "Waiting for your answer").slice(0, 220) + on,
      tag: "aios-ask",
      requireInteraction: true
    };
  }
  if (type === "max_steps") {
    return {
      title: "OPERATOR needs more steps",
      body: String(payload.message || "Continue the run?").slice(0, 220) + on,
      tag: "aios-max-steps",
      requireInteraction: true
    };
  }
  if (type === "done") {
    const steps = Number(payload.steps || 0);
    const detail = String(payload.message || (payload.ok ? "Task complete" : "The run stopped")).slice(0, 200);
    return {
      title: payload.ok ? "OPERATOR finished" : "OPERATOR run ended",
      body: [detail, steps ? `${steps} steps` : ""].filter(Boolean).join(" · ") + on,
      tag: "aios-done",
      requireInteraction: false
    };
  }
  return null;
}

async function ensureSchema(env) {
  if (schemaReady) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, account_id TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS push_subs_account ON push_subs(account_id)"),
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

/** Attachment ids never carry a path — the key is rebuilt from the caller's
 *  own account and machine, so one remote can never read another's upload. */
function safeUploadId(value) {
  return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 48);
}

function uploadKey(accountId, machineId, id) {
  return `uploads/${accountId}/${machineId}/${id}`;
}

function safeFileName(value) {
  const name = String(value || "").replace(/[\r\n\\"]/g, "").replace(/[/\\]/g, "_").trim().slice(0, 120);
  return name || "attachment";
}

async function handleApi(request, env, url, context) {
  await ensureSchema(env);
  const path = url.pathname;

  // The app deploys itself from GitHub Pages and this relay does not, so the
  // two halves drift. Naming what this build can do lets the phone say which
  // side is behind instead of leaving a feature quietly broken.
  if (path === "/api/health") {
    return json({
      ok: true,
      service: "aiOS Remote",
      features: ["uploads", "push", "event-latest", "agent-voice", "pc-transcription"]
    });
  }

  // The application server key is public by definition — the service worker
  // needs it before there is any session to authenticate with.
  if (path === "/api/push/key" && request.method === "GET") {
    const keys = await vapidKeys(env);
    return json({ key: keys.publicKey });
  }

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
        // Wake the phone for the moments that need a person. Never block the
        // PC's heartbeat on someone else's push service.
        const alerts = events.map((event) => alertFor(event, machine.name)).filter(Boolean);
        if (alerts.length) {
          const notice = { ...alerts[alerts.length - 1], url: "./", machine_id: machine.id };
          const delivery = pushToAccount(env, String(machine.account_id), notice);
          if (context?.waitUntil) context.waitUntil(delivery); else await delivery;
        }
      }
      if (input.status) {
        await env.DB.prepare("UPDATE machines SET status_json = ?, last_seen = ?, updated_at = ? WHERE id = ?")
          .bind(JSON.stringify(input.status), timestamp, timestamp, machine.id).run();
      }
      if (input.completed_command_id) {
        const completedId = Number(input.completed_command_id);
        const completed = await env.DB.prepare("SELECT type, payload_json FROM commands WHERE id = ? AND machine_id = ?")
          .bind(completedId, machine.id).first();
        const completedPayload = safeJson(completed?.payload_json);
        const effectiveType = completed?.type === "config"
          ? String(completedPayload?._aios_command || "config")
          : String(completed?.type || "");
        if (effectiveType === "ai_settings") {
          // API keys are transient transport data. Remove the command instead
          // of retaining its payload or result in the relay database.
          await env.DB.prepare("DELETE FROM commands WHERE id = ? AND machine_id = ?")
            .bind(completedId, machine.id).run();
        } else {
          await env.DB.prepare("UPDATE commands SET completed_at = ?, result_json = ? WHERE id = ? AND machine_id = ?")
            .bind(timestamp, JSON.stringify(input.result || {}), completedId, machine.id).run();
        }
      }
      return json({ ok: true });
    }

    const agentUploadMatch = path.match(/^\/api\/agent\/uploads\/([^/]+)$/);
    if (agentUploadMatch && (request.method === "GET" || request.method === "DELETE")) {
      const id = safeUploadId(agentUploadMatch[1]);
      if (!id) return json({ error: "Unknown attachment." }, 404);
      const key = uploadKey(machine.account_id, machine.id, id);
      if (request.method === "DELETE") {
        await env.FILES.delete(key);
        return json({ ok: true });
      }
      const object = await env.FILES.get(key);
      if (!object) return json({ error: "That attachment is no longer available." }, 404);
      const headers = new Headers({
        "content-type": object.httpMetadata?.contentType || "application/octet-stream",
        "cache-control": "no-store"
      });
      headers.set("x-aios-file-name", object.customMetadata?.name || "attachment");
      return new Response(object.body, { headers });
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
    const allowed = new Set([
      "prompt", "followup", "stop", "config", "clarify", "stream", "update",
      "codex_switch", "ai_settings", "agent", "agent_stop", "transcribe"
    ]);
    if (!allowed.has(input.type)) return json({ error: "Unsupported command." }, 400);
    const payload = input.payload && typeof input.payload === "object" ? input.payload : {};
    const effectiveType = input.type === "config" ? String(payload._aios_command || "config") : input.type;
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
    if (effectiveType === "ai_settings") {
      await env.DB.prepare("DELETE FROM commands WHERE machine_id = ? AND account_id = ? AND (type = 'ai_settings' OR (type = 'config' AND payload_json LIKE ?))")
        .bind(machineId, accountId, '%"_aios_command":"ai_settings"%').run();
    }
    const result = await env.DB.prepare("INSERT INTO commands (account_id, machine_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(accountId, machineId, input.type, JSON.stringify(payload), now()).run();
    return json({ ok: true, command_id: result.meta?.last_row_id }, 202);
  }

  if (path === "/api/push/subscribe" && request.method === "POST") {
    const input = await body(request);
    const endpoint = String(input.endpoint || "").trim();
    const p256dh = String(input.keys?.p256dh || "").trim();
    const auth = String(input.keys?.auth || "").trim();
    if (!/^https:\/\//.test(endpoint) || !p256dh || !auth) return json({ error: "That subscription is incomplete." }, 400);
    await env.DB.prepare("INSERT INTO push_subs (endpoint, account_id, p256dh, auth, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET account_id = excluded.account_id, p256dh = excluded.p256dh, auth = excluded.auth")
      .bind(endpoint, accountId, p256dh, auth, now()).run();
    return json({ ok: true }, 201);
  }

  if (path === "/api/push/subscribe" && request.method === "DELETE") {
    const input = await body(request);
    const endpoint = String(input.endpoint || "").trim();
    if (endpoint) {
      await env.DB.prepare("DELETE FROM push_subs WHERE endpoint = ? AND account_id = ?").bind(endpoint, accountId).run();
    }
    return json({ ok: true });
  }

  // Prove the round trip from the phone's own settings screen.
  if (path === "/api/push/test" && request.method === "POST") {
    const count = await env.DB.prepare("SELECT COUNT(*) AS total FROM push_subs WHERE account_id = ?").bind(accountId).first();
    if (!Number(count?.total || 0)) return json({ error: "This phone is not subscribed yet." }, 404);
    await pushToAccount(env, accountId, {
      title: "aiOS Remote",
      body: "Notifications are working. You'll hear from OPERATOR here.",
      tag: "aios-test",
      url: "./"
    });
    return json({ ok: true, phones: Number(count.total) });
  }

  const uploadsMatch = path.match(/^\/api\/machines\/([^/]+)\/uploads$/);
  if (uploadsMatch && request.method === "POST") {
    const machineId = uploadsMatch[1];
    const machine = await env.DB.prepare("SELECT id FROM machines WHERE id = ? AND account_id = ?").bind(machineId, accountId).first();
    if (!machine) return json({ error: "Computer not found." }, 404);
    if (Number(request.headers.get("content-length") || 0) > MAX_UPLOAD_BYTES) {
      return json({ error: "That file is too big — 15 MB max." }, 413);
    }
    const bytes = await request.arrayBuffer();
    if (!bytes.byteLength) return json({ error: "That file was empty." }, 400);
    if (bytes.byteLength > MAX_UPLOAD_BYTES) return json({ error: "That file is too big — 15 MB max." }, 413);
    const id = randomString(24);
    const name = safeFileName(url.searchParams.get("name"));
    await env.FILES.put(uploadKey(accountId, machineId, id), bytes, {
      httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" },
      customMetadata: { name, uploadedAt: String(now()) }
    });
    return json({ ok: true, key: id, name, size: bytes.byteLength }, 201);
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
    // The newest id we hold. A phone whose cursor is past this one is reading
    // a log that no longer exists, and can say so instead of waiting forever.
    const newest = await env.DB.prepare("SELECT MAX(id) AS latest FROM events WHERE machine_id = ?")
      .bind(machineId).first();
    return json({
      events,
      cursor: events.length ? events[events.length - 1].id : since,
      latest: Number(newest?.latest || 0)
    });
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
  async fetch(request, env, context) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/api/")) {
        if (request.method === "OPTIONS") return withCors(new Response(null, { status: 204 }), request);
        return withCors(await handleApi(request, env, url, context), request);
      }
      return env.ASSETS.fetch(request);
    } catch (error) {
      console.error(error);
      return withCors(json({ error: "The relay hit an unexpected error." }, 500), request);
    }
  }
};
