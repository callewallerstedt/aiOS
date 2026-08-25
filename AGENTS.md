# aiOS

## What "aiOS" means

**"aiOS" always means the WebView2 desktop app:** `aios_shell.py` + `aios_ui/`.
That is the live overlay with tabs such as Dashboard, Projects, CODE, Apps,
Drop, OPERATOR, and Settings. When the user says "aiOS", "the overlay", "the
app", "the desktop app", "the new gui", or "the code thing", they mean this
stack and nothing else.

| Path | What it is |
| --- | --- |
| `aios_shell.py` + `aios_ui/` | **aiOS itself** — WebView2 shell + web UI + Python API |
| `director/` | **aiOS Director** — the coordinator that runs on the Linux box (see `DIRECTOR.md`) |
| `phone_site/` | The Director phone PWA (deployed to Vercel) |
| `director_client.py`, `director_voice.py` | This PC's clients for Director |
| `code_jobs.py` | The CODE agent harness that aiOS drives |
| `helper_overlay.py` | **DEPRECATED Tkinter UI — do not edit for GUI work** |
| `agent_clicker/desktop_app.py` | A separate screen-clicking agent GUI |
| `agent_clicker/app/` | The phone/web Flask app |
| `voice_agent.py`, `helper_config.py` | Supporting services |

### Never edit the old Tk UI for aiOS requests

`helper_overlay.py` is replaced. Do **not** change its tabs, CODE view, layout,
or session list when asked to change aiOS. GUI work goes in `aios_ui/`
(`web/js/`, `web/css/`, `web/index.html`, and the Python modules next to them).

`helper_overlay.py` may still hold shared config helpers (`DEFAULT_CONFIG`,
`load_config`, `save_config`). Touch those **only** when a setting/default
truly lives there — never as a stand-in for the web GUI.

`agent_clicker/` is a different product. Never change it for an aiOS request.

If a request seems to be about aiOS but the obvious code lives in
`helper_overlay.py` or `agent_clicker/`, stop and ask before editing. Editing
the wrong app is the single most expensive mistake here.

## Working rules

- The CODE tab UI lives in `aios_ui/web/` (`js/code.js`, `css/code.css`,
  transcript, settings). Its provider/session engine lives in `code_jobs.py`.
- Run only the smallest focused test(s) that cover the changed behavior. Do not
  run the full test suite unless the user explicitly asks for it. Compile edited
  Python files with `python -m py_compile <file>` when relevant.
- Preserve each file's existing line endings; many files here are CRLF.
- Do not create files in the repo root for scratch work; use a temp folder.

## Verification rules

- Never state an API field, model id, function name, flag, or path that you have
  not seen in a file, in command output, or on a page you fetched. If you are
  reaching for an external API, confirm its real shape first with `fetch_url`
  against the official docs, or by finding existing working usage in this repo.
- A plausible-looking identifier is not evidence. `kimi/k3.5-turbo` and a
  top-level `web_search` request field both looked reasonable and were both
  invented; each cost a full rebuild.
- When the request is ambiguous, when a destructive action is implied, or when
  you cannot verify something that matters, call `ask_user` instead of guessing.
