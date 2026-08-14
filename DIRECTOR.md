# aiOS Director

An always-on coordinator on the Linux box, a chat app on the phone, and this
Windows desktop as a client. It replaces aiOS Remote: no agent/OPERATOR mode
split, no polling Windows through `phone_relay.py`.

```
  phone (PWA on Vercel) ─┐
                         ├── HTTPS/WebSocket ──> Director  (calle-linux)
  aiOS desktop / voice ──┘        (Tailscale Funnel)   │
                                                       ├─ coordinator chat + memory
                                                       ├─ pixel operator on :99
  Windows CODE harness <── outbound WebSocket ─────────┘   (Chrome + noVNC takeover)
```

Nothing listens on the Windows machine. It dials out to Director and answers
calls, so CODE sessions can be started from the phone without opening a port at
home.

## Where things live

| Path | What it is |
| --- | --- |
| `director/` | The coordinator: server, store, agents, tools, operator |
| `director/deploy/` | Installer and systemd units for the Linux box |
| `deploy-director.ps1` | Push this repo's `director/` to the box and install |
| `phone_site/` | The phone PWA (index.html, director.css, director.js) |
| `director_client.py` | Windows client: CODE dispatch, shell, files |
| `director_voice.py` | Sends this PC's spoken turns to Director |
| `aios_ui/director_link.py` | Points the desktop AGENT sidebar at Director |

On the box:

```
~/aios-director/                  the package and its virtualenv
~/.local/share/aios-director/     director.db, settings.json, logs, Chrome profile
```

## The machines

* **Director** — `calle-linux`, also `rocky-server` on Tailscale.
  LAN `192.168.0.17`, Tailscale `100.69.218.63`.
  Public: `https://rocky-server.tail4d08fd.ts.net/director` (Tailscale Funnel,
  mounted on a path so the existing dashboard keeps `/`).
* **Phone** — `https://phonesite-six.vercel.app`, installable.
* **Windows** — this repo, running `director_client.py`.

## Everyday commands

On the box:

```bash
systemctl --user restart aios-director        # restart the coordinator
journalctl --user -u aios-director -f         # watch it
cd ~/aios-director && .venv/bin/python -m director.cli status   # what is healthy
cd ~/aios-director && .venv/bin/python -m director.cli pair     # code for a new phone
```

From Windows, after changing anything in `director/`:

```powershell
.\deploy-director.ps1
```

## Pairing a phone

1. `director.cli pair` on the box prints a six-character code, good for ten minutes.
2. Open the PWA, enter the Director address and the code.
3. The phone gets a token; only its SHA-256 is stored. Codes are single-use.

## Connecting this Windows desktop

```bash
# on the box, once
cd ~/aios-director && .venv/bin/python -m director.cli enroll-machine --name calle-windows
```

Put the token in `aios_director_client.json` (gitignored; see the `.example`
file), then run:

```bash
python director_client.py
```

For the sidebar chat and voice, pair the desktop like a phone and add the
device token to `helper_config.json`:

```json
{"director": {"enabled": true, "voice": true,
              "url": "https://rocky-server.tail4d08fd.ts.net/director",
              "token": "<device token>", "agent_id": "agt_director"}}
```

With `enabled` false, or Director unreachable, the desktop falls back to the
local voice agent exactly as before.

## The agents

Director is only a first-run bootstrap. The phone's `+` button adds more, and
once another agent exists every agent can be removed.

| Agent | For |
| --- | --- |
| Director | Coordinates: decides where work goes, holds the memory |

**Every chat is its own Director.** A new agent gets the full tool set, not a
cut-down one — the difference is its name, its photo, its instructions and its
model. Tap an agent → ⋮ → *Edit this agent* for:

* **Name and photo** — a real picture (cropped square, stored on the agent) or
  an emoji.
* **Custom instructions** — the agent reads these as its own and can quote them
  back if you ask what its instructions are.
* **Model** — any Codex model, or an OpenRouter model id, plus a reasoning
  level. Blank means "use the default".
* **Permissions and alerts** — approve everything from this one agent, or mute
  its notifications.

The bootstrap Director keeps its tool list current while it exists. Other
agents keep whatever they were given. Every newly created agent starts with the
full Director toolset, including CODE sessions and the Linux screen operator.

## Routines

Anything recurring or delayed: `every day at 08:00`, `every weekday`,
`every Friday at 17:00`, `every N minutes`, or a one-off later today. When a
routine fires, its prompt is dropped into that agent's thread and the agent
runs a normal turn, so a routine can use every tool, ask you something, or
dispatch the operator.

Two ways to make one:

* **Just ask** — "remind me every Friday at five to do the invoices". Director
  has `schedule`, `list_schedules` and `cancel_schedule`, and sets it up itself.
* **⋮ → Routines** on any agent, to add, pause, run now, or delete.

A routine whose slot passed while the box was off is skipped to its next slot
rather than firing a backlog on boot.

## Notifications

Settings → *Turn on notifications* subscribes this phone to Web Push. You get:

* a reply when an agent finishes answering
* an **approval** request or a **question** that is blocking a run
* a routine firing, and a dispatched job finishing

A thread you are currently looking at does not also buzz your pocket. Tapping a
notification opens that agent's chat.

**On iPhone, add Director to the Home Screen first** — iOS only allows Web Push
for installed PWAs.

## Permissions

By default anything destructive or outward-facing raises an approval card. Each
card offers four answers: approve once, approve for the rest of this run,
always allow this agent, or approve everything from now on. The blanket option
is also a switch in Settings → Permissions, and can be turned off there.

## Models

Default is **gpt-5.6-luna through Codex OAuth**, which costs no per-token money
because it uses the ChatGPT subscription. Everything is settable — per agent
(`PATCH /api/agents/<id>`) or globally in the phone's settings sheet — and
OpenRouter is available for any catalogue model once a key is saved.

Codex tokens live ten days. Director refreshes them itself against the issuer's
published token endpoint and writes `~/.codex/auth.json` back atomically, so
the box does not need `codex login` every week. If a refresh ever fails, the
error says exactly that and OpenRouter can carry the load meanwhile.

## The operator screen

A virtual display, not the laptop's own panel: the lid is usually shut, and a
virtual screen never fights you for the mouse.

```
Xvfb :99  ->  openbox  ->  x11vnc (localhost:5999)  ->  Director bridges it to noVNC
```

Chrome runs there with a persistent profile in
`~/.local/share/aios-director/chrome-profile`, so web logins survive reboots.

**Takeover**: the screen button in the chat header opens noVNC in the app. The
operator first tries the persistent session, including an existing Google
account and ordinary SSO. It hands over only when password entry, manual 2FA,
a captcha, payment details or missing access genuinely blocks it. You finish
that step on the same screen and it continues. Director never sees the secret.

The window manager is not optional. Without one, Chrome renders but nothing
maximises or takes focus, and `_NET_CLIENT_LIST` is never published, so the
operator cannot tell what is on screen.

## Safety

* Destructive tools (`shell`, `write_file`, and anything outward-facing) raise
  an approval card and block until you answer.
* Existing sessions and normal account selection are automatic. Secret entry
  is always a handoff, never something Director types.
* The public endpoint requires a bearer token on every route except
  `/api/health` and `/api/pair`.

## Retiring aiOS Remote

The Vercel project now serves Director at the same URL, so the installed PWA
updates itself. The old pages (`phone.js`, `coding.js`) are still in the repo
but are no longer deployed.

`phone_relay.py` and the Cloudflare worker are no longer the remote path. The
bridge can be stopped whenever you like:

```powershell
Get-Process python* | Where-Object { $_.CommandLine -like '*phone_relay*' } | Stop-Process
```

and removed from startup by editing `install-startup.ps1`.

## Tests

```bash
python -m pytest tests/test_director_core.py tests/test_director_bridges.py tests/test_director_routines.py -q
```

Covers the store, pairing, history rebuilding, both backends' item
translation, the operator's reply parsing and coordinate scaling, schedule
maths, the approval grants, push signing, and the Windows bridges.

## Things that bit, so they do not again

* **A half-loaded tool registry.** `agents.py` imports one tool module for the
  memory block, which made the lazy loader think everything was loaded. The
  coordinator got 3 tools instead of 19 and told the user it could not run
  anything — which reads like a refusal, not a bug.
* **Frozen tool lists.** Built-in agents stored their tools at seed time, so a
  newly added tool never reached them. Asked to schedule something, Director
  read its own source and inserted a database row by hand instead.
* **A black operator screen.** Chrome's output went to /dev/null and there was
  no window manager, so every screenshot was black and the model answered from
  memory rather than saying it could not see. Chrome now logs to `chrome.log`,
  openbox runs, and window listing uses xdotool rather than wmctrl (which needs
  a WM to report anything).
* **PEM text as a push key.** pywebpush wants a Vapid object, not PEM contents;
  passing the text failed as "ASN.1 parsing error" and every notification died
  silently.
