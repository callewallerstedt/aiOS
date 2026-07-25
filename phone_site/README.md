# aiOS Remote

Installable PWA and Cloudflare relay for securely controlling OPERATOR on multiple Windows PCs.

## Local development

```powershell
npm install
npm run build
npm run dev
```

The worker uses D1 for accounts, pairing, machines, commands, and events, plus R2 for current monitor frames. Local Wrangler development creates isolated local storage.

## Pair a PC

1. Open the deployed PWA and create or unlock a remote.
2. Save the private code.
3. In aiOS, open **Settings → Mobile remote**.
4. Enter the remote URL, private code, and a friendly computer name.
5. Press **Connect**.

The PC receives a unique machine token; the private code and session tokens are stored only as SHA-256 hashes by the relay.
