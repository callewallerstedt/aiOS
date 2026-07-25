import json
import subprocess

import aios_secret_transport


NODE_ENCRYPT = r"""
const { webcrypto } = require("crypto");
const publicKey = process.argv[1];
const secret = process.argv[2];
const fromB64 = (value) => Buffer.from(value.replace(/-/g, "+").replace(/_/g, "/"), "base64");
const toB64 = (value) => Buffer.from(value).toString("base64url");
(async () => {
  const remote = await webcrypto.subtle.importKey(
    "raw", fromB64(publicKey), { name: "ECDH", namedCurve: "P-256" }, false, []
  );
  const ephemeral = await webcrypto.subtle.generateKey(
    { name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]
  );
  const shared = await webcrypto.subtle.deriveBits(
    { name: "ECDH", public: remote }, ephemeral.privateKey, 256
  );
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const material = await webcrypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const encoder = new TextEncoder();
  const key = await webcrypto.subtle.deriveKey({
    name: "HKDF", hash: "SHA-256", salt: iv,
    info: encoder.encode("aiOS Phone API Key v1")
  }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const ciphertext = await webcrypto.subtle.encrypt({
    name: "AES-GCM", iv, additionalData: encoder.encode("aiOS Phone API Key")
  }, key, encoder.encode(secret));
  const ephemeralPublic = await webcrypto.subtle.exportKey("raw", ephemeral.publicKey);
  process.stdout.write(JSON.stringify({
    version: 1,
    ephemeral_public_key: toB64(ephemeralPublic),
    iv: toB64(iv),
    ciphertext: toB64(ciphertext)
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def test_browser_ciphertext_decrypts_only_on_selected_pc(tmp_path, monkeypatch):
    key_path = tmp_path / "phone-secret.pem"
    monkeypatch.setattr(aios_secret_transport, "PRIVATE_KEY_PATH", key_path)
    public_key = aios_secret_transport.public_key_payload()["transport_public_key"]
    secret = "sk-test-browser-to-pc-secret"
    encrypted = subprocess.check_output(
        ["node", "-e", NODE_ENCRYPT, public_key, secret],
        text=True,
    )

    assert aios_secret_transport.decrypt_secret(json.loads(encrypted)) == secret
    assert secret not in encrypted
    assert key_path.exists()
