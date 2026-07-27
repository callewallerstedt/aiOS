/**
 * Push encryption has no second chance: a phone that is asleep cannot tell us
 * the payload failed to decrypt — the notification simply never appears. So
 * check the record framing against RFC 8291, decrypt our own output the way a
 * browser does, and check the VAPID token a push service would refuse us over.
 */

import test from "node:test";
import assert from "node:assert/strict";

import relay, { encryptPushPayload, base64UrlDecode, base64UrlEncode, vapidHeader, vapidKeys, alertFor } from "./index.js";

const VECTOR = {
  // RFC 8291 §5
  plaintext: "When I grow up, I want to be a watermelon",
  uaPublic: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
  authSecret: "BTBZMqHH6r4Tts7J_aSIgg",
  salt: "DGv6ra1nlYgDCS1FRnbzlw",
  asPrivate: "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
  asPublic: "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
  // salt || rs=4096 || idlen=65 || as_public, before the ciphertext
  header: "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
};

function jwk(publicKey, privateKey) {
  const raw = base64UrlDecode(publicKey);
  return {
    kty: "EC",
    crv: "P-256",
    x: base64UrlEncode(raw.slice(1, 33)),
    y: base64UrlEncode(raw.slice(33, 65)),
    ...(privateKey ? { d: privateKey } : {})
  };
}

async function importPair(publicKey, privateKey, usages) {
  return {
    publicKey: await crypto.subtle.importKey("raw", base64UrlDecode(publicKey), { name: "ECDH", namedCurve: "P-256" }, true, []),
    privateKey: await crypto.subtle.importKey("jwk", jwk(publicKey, privateKey), { name: "ECDH", namedCurve: "P-256" }, false, usages)
  };
}

test("the record is framed exactly as RFC 8291 lays it out", async () => {
  const body = await encryptPushPayload(
    new TextEncoder().encode(VECTOR.plaintext),
    base64UrlDecode(VECTOR.uaPublic),
    base64UrlDecode(VECTOR.authSecret),
    {
      salt: base64UrlDecode(VECTOR.salt),
      localKeys: await importPair(VECTOR.asPublic, VECTOR.asPrivate, ["deriveBits"])
    }
  );

  // A push service parses this header before it ever reaches the phone.
  assert.equal(base64UrlEncode(body.slice(0, 86)), VECTOR.header);
  // plaintext + the 0x02 delimiter + the GCM tag, one record, no padding.
  assert.equal(body.length - 86, VECTOR.plaintext.length + 1 + 16);
});

test("the phone can decrypt what we send it", async () => {
  // The receiver half of RFC 8291, written straight from the spec. Its output
  // was cross-checked against http_ece — the library the reference web-push
  // client uses — which decrypts this encryptor's records unchanged.
  const subscriber = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const subscriberPublic = new Uint8Array(await crypto.subtle.exportKey("raw", subscriber.publicKey));
  const authSecret = crypto.getRandomValues(new Uint8Array(16));
  const message = JSON.stringify({ title: "OPERATOR finished", body: "Downloads sorted · 7 steps" });

  const body = await encryptPushPayload(new TextEncoder().encode(message), subscriberPublic, authSecret);

  const salt = body.slice(0, 16);
  const idlen = body[20];
  const senderPublic = body.slice(21, 21 + idlen);
  const ciphertext = body.slice(21 + idlen);
  assert.equal(idlen, 65, "the sender key must be an uncompressed P-256 point");
  assert.equal(new DataView(body.buffer, body.byteOffset).getUint32(16), 4096, "record size");

  const shared = new Uint8Array(await crypto.subtle.deriveBits(
    { name: "ECDH", public: await crypto.subtle.importKey("raw", senderPublic, { name: "ECDH", namedCurve: "P-256" }, false, []) },
    subscriber.privateKey, 256));
  const hkdf = async (hkdfSalt, ikm, info, length) => new Uint8Array(await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: hkdfSalt, info },
    await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]), length * 8));
  const join = (...parts) => {
    const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) { out.set(part, offset); offset += part.length; }
    return out;
  };
  const encoder = new TextEncoder();
  const ikm = await hkdf(authSecret, shared,
    join(encoder.encode("WebPush: info"), new Uint8Array([0]), subscriberPublic, senderPublic), 32);
  const cek = await hkdf(salt, ikm, join(encoder.encode("Content-Encoding: aes128gcm"), new Uint8Array([0])), 16);
  const nonce = await hkdf(salt, ikm, join(encoder.encode("Content-Encoding: nonce"), new Uint8Array([0])), 12);
  const plain = new Uint8Array(await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: nonce },
    await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["decrypt"]),
    ciphertext));

  assert.equal(plain[plain.length - 1], 2, "last record delimiter");
  assert.equal(new TextDecoder().decode(plain.slice(0, -1)), message);
});

test("every push gets a fresh sender key and salt", async () => {
  const subscriber = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", subscriber.publicKey));
  const auth = crypto.getRandomValues(new Uint8Array(16));
  const message = new TextEncoder().encode("same words twice");

  const first = await encryptPushPayload(message, publicKey, auth);
  const second = await encryptPushPayload(message, publicKey, auth);

  assert.notEqual(base64UrlEncode(first.slice(0, 16)), base64UrlEncode(second.slice(0, 16)));
  assert.notEqual(base64UrlEncode(first), base64UrlEncode(second));
});

/** The smallest D1 stand-in that vapidKeys needs: one row, remembered. */
function fakeDatabase() {
  const rows = new Map();
  return {
    prepare(sql) {
      const statement = {
        args: [],
        bind(...args) { statement.args = args; return statement; },
        async first() { return /SELECT value FROM settings/.test(sql) ? rows.get("vapid") || null : null; },
        async run() {
          if (/INSERT OR IGNORE INTO settings/.test(sql) && !rows.has("vapid")) {
            rows.set("vapid", { value: statement.args[0] });
          }
          return { success: true };
        }
      };
      return statement;
    },
    rows
  };
}

test("the application server key is generated once and then reused", async () => {
  const env = { DB: fakeDatabase() };

  const first = await vapidKeys(env);
  const second = await vapidKeys(env);

  assert.equal(first.publicKey, second.publicKey, "rotating this key silently unsubscribes every phone");
  assert.equal(base64UrlDecode(first.publicKey).length, 65);
  assert.equal(base64UrlDecode(first.publicKey)[0], 4, "uncompressed point");
});

test("the VAPID token is signed for the push service that will check it", async () => {
  const env = { DB: fakeDatabase() };

  const header = await vapidHeader(env, "https://web.push.apple.com/QABC123/xyz");

  const [, token, key] = header.authorization.match(/^vapid t=([^,]+), k=(.+)$/) || [];
  assert.ok(token && key, `unexpected header: ${header.authorization}`);
  const [head, claims, signature] = token.split(".");
  assert.deepEqual(JSON.parse(new TextDecoder().decode(base64UrlDecode(head))), { typ: "JWT", alg: "ES256" });
  const payload = JSON.parse(new TextDecoder().decode(base64UrlDecode(claims)));
  assert.equal(payload.aud, "https://web.push.apple.com", "the audience is the push host, not the endpoint path");
  assert.ok(payload.exp > Math.floor(Date.now() / 1000), "an expired token is rejected");
  assert.ok(payload.exp - Math.floor(Date.now() / 1000) <= 24 * 60 * 60, "Apple caps the lifetime at 24h");
  assert.ok(String(payload.sub).startsWith("mailto:"));

  const verifier = await crypto.subtle.importKey(
    "raw", base64UrlDecode(key), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" }, verifier,
    base64UrlDecode(signature), new TextEncoder().encode(`${head}.${claims}`));
  assert.ok(valid, "the push service verifies this signature with the key we advertise");
});

test("only the moments that need a person raise an alert", () => {
  assert.equal(alertFor({ type: "step_begin", payload: { n: 3 } }, "Studio PC"), null);
  assert.equal(alertFor({ type: "thought", payload: { say: "thinking" } }, "Studio PC"), null);
  assert.equal(alertFor({ type: "screenshot", payload: {} }, "Studio PC"), null);

  const ask = alertFor({ type: "ask", payload: { message: "Which account should I use?" } }, "Studio PC");
  assert.equal(ask.title, "OPERATOR needs your input");
  assert.match(ask.body, /Which account should I use\? · Studio PC/);
  assert.equal(ask.requireInteraction, true, "a question must stay on screen");

  const steps = alertFor({ type: "max_steps", payload: {} }, "Studio PC");
  assert.equal(steps.title, "OPERATOR needs more steps");

  const done = alertFor({ type: "done", payload: { ok: true, message: "Sorted", steps: 7 } }, "Studio PC");
  assert.equal(done.title, "OPERATOR finished");
  assert.match(done.body, /Sorted · 7 steps · Studio PC/);

  const failed = alertFor({ type: "done", payload: { ok: false, message: "Stopped" } }, "Studio PC");
  assert.equal(failed.title, "OPERATOR run ended");
});

test("a long question is trimmed instead of overflowing the notification", () => {
  const alert = alertFor({ type: "ask", payload: { message: "x".repeat(900) } }, "Studio PC");

  assert.ok(alert.body.length < 260, `${alert.body.length} characters`);
});

/** Just enough D1 for the path a PC's heartbeat takes through the relay. */
function relayDatabase({ subscriptions = [], machine = null } = {}) {
  const settings = new Map();
  const deleted = [];
  const inserted = [];
  const statement = (sql) => {
    const self = {
      args: [],
      bind(...args) { self.args = args; return self; },
      async first() {
        if (/FROM machines WHERE token_hash/.test(sql)) return machine;
        if (/FROM settings/.test(sql)) return settings.get("vapid") || null;
        return null;
      },
      async all() {
        if (/FROM push_subs WHERE account_id/.test(sql)) {
          return { results: subscriptions.filter((row) => row.account_id === self.args[0]) };
        }
        return { results: [] };
      },
      async run() {
        if (/INSERT OR IGNORE INTO settings/.test(sql) && !settings.has("vapid")) {
          settings.set("vapid", { value: self.args[0] });
        }
        if (/DELETE FROM push_subs/.test(sql)) deleted.push(self.args[0]);
        if (/INSERT INTO events/.test(sql)) inserted.push(self.args);
        return { success: true, meta: { last_row_id: 1 } };
      }
    };
    return self;
  };
  return {
    prepare: statement,
    async batch(statements) { return Promise.all(statements.map((item) => item.run?.() ?? item)); },
    deleted,
    inserted
  };
}

async function browserSubscription(accountId) {
  const keys = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  return {
    account_id: accountId,
    endpoint: "https://web.push.apple.com/QABC123/token",
    p256dh: base64UrlEncode(await crypto.subtle.exportKey("raw", keys.publicKey)),
    auth: base64UrlEncode(crypto.getRandomValues(new Uint8Array(16))),
    privateKey: keys.privateKey
  };
}

function agentRequest(events) {
  return new Request("https://relay.example/api/agent/events", {
    method: "POST",
    headers: { "content-type": "application/json", "x-aios-machine-token": "machine-token" },
    body: JSON.stringify({ events })
  });
}

test("a question from the PC wakes every phone on the account", async () => {
  const subscription = await browserSubscription("account-1");
  const env = { DB: relayDatabase({ subscriptions: [subscription], machine: { id: "m1", account_id: "account-1", name: "Studio PC" } }) };
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => { sent.push({ url: String(url), options }); return new Response("", { status: 201 }); };
  const waits = [];

  try {
    const response = await relay.fetch(
      agentRequest([{ type: "ask", payload: { message: "Which account should I use?" }, created_at: Date.now() }]),
      env,
      { waitUntil: (promise) => waits.push(promise) }
    );
    assert.equal(response.status, 200, "the PC's heartbeat still succeeds");
    await Promise.all(waits);
  } finally {
    globalThis.fetch = realFetch;
  }

  assert.equal(sent.length, 1, "one push, to the one subscribed phone");
  assert.equal(sent[0].url, subscription.endpoint);
  const headers = sent[0].options.headers;
  assert.equal(headers["content-encoding"], "aes128gcm");
  assert.match(headers.authorization, /^vapid t=[\w-]+\.[\w-]+\.[\w-]+, k=[\w-]+$/);
  assert.equal(headers.urgency, "high", "a question should not wait for the next unlock");
  assert.ok(sent[0].options.body.length > 86, "an empty body would show a blank alert");
});

test("routine activity never buzzes the phone", async () => {
  const subscription = await browserSubscription("account-1");
  const env = { DB: relayDatabase({ subscriptions: [subscription], machine: { id: "m1", account_id: "account-1", name: "Studio PC" } }) };
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => { sent.push({ url, options }); return new Response("", { status: 201 }); };
  const waits = [];

  try {
    await relay.fetch(agentRequest([
      { type: "step_begin", payload: { n: 2 } },
      { type: "screenshot", payload: { n: 2 } },
      { type: "thought", payload: { say: "Reading the page" } }
    ]), env, { waitUntil: (promise) => waits.push(promise) });
    await Promise.all(waits);
  } finally {
    globalThis.fetch = realFetch;
  }

  assert.equal(sent.length, 0);
});

test("a phone that uninstalled the app stops being pushed to", async () => {
  const subscription = await browserSubscription("account-1");
  const env = { DB: relayDatabase({ subscriptions: [subscription], machine: { id: "m1", account_id: "account-1", name: "Studio PC" } }) };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("", { status: 410 });   // Gone
  const waits = [];

  try {
    await relay.fetch(agentRequest([{ type: "done", payload: { ok: true, message: "Sorted", steps: 4 } }]),
      env, { waitUntil: (promise) => waits.push(promise) });
    await Promise.all(waits);
  } finally {
    globalThis.fetch = realFetch;
  }

  assert.deepEqual(env.DB.deleted, [subscription.endpoint], "a dead subscription must not be retried forever");
});

test("the event log says how far it goes, so a stale phone can tell", async () => {
  const rows = [
    { id: 41, type: "thought", payload_json: "{}", created_at: 1 },
    { id: 42, type: "done", payload_json: "{}", created_at: 2 }
  ];
  const env = { DB: {
    prepare(sql) {
      const self = {
        args: [],
        bind(...args) { self.args = args; return self; },
        async first() {
          if (/MAX\(id\) AS latest/.test(sql)) return { latest: 42 };
          if (/FROM machines WHERE id/.test(sql)) return { id: "m1" };
          if (/FROM sessions/.test(sql)) return { account_id: "account-1", expires_at: Date.now() + 60_000 };
          return null;
        },
        async all() { return { results: rows.filter((row) => row.id > Number(self.args[1] || 0)) }; },
        async run() { return { success: true }; }
      };
      return self;
    },
    async batch(statements) { return Promise.all(statements.map((item) => item.run?.() ?? item)); }
  } };

  const response = await relay.fetch(new Request(
    "https://relay.example/api/machines/m1/events?since=41",
    { headers: { authorization: "Bearer session-token" } }
  ), env, {});
  const data = await response.json();

  assert.equal(response.status, 200);
  assert.deepEqual(data.events.map((event) => event.id), [42]);
  assert.equal(data.latest, 42, "without this a rebuilt log leaves the phone waiting forever");
});
