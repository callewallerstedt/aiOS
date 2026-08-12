"""Small command line for the box: pair a phone, check state, run the server.

    python -m director.cli pair
    python -m director.cli status
    python -m director.cli enroll-machine --name calle-windows
    python -m director.cli serve
"""
from __future__ import annotations

import argparse
import asyncio
import json

from . import agents as agents_mod
from . import auth, config, models, server, store
from .operator import display as display_mod


def cmd_pair(args: argparse.Namespace) -> int:
    code = auth.new_pairing_code(kind=args.kind)
    print(f"\n  Pairing code: {code['code']}\n")
    print(f"  Type it into the aiOS Director app within "
          f"{int(auth.CODE_TTL / 60)} minutes.\n")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = config.load_settings()

    async def gather() -> dict:
        backends = {}
        for backend in models.BACKENDS:
            ready, message = await models.backend_status(backend, settings=settings)
            backends[backend] = {"ready": ready, "message": message}
        return {"backends": backends, "operator": await display_mod.status(settings)}

    state = asyncio.run(gather())
    state["home"] = str(config.home())
    state["devices"] = store.list_devices()
    state["machines"] = store.list_machines()
    state["agents"] = [{"id": a["id"], "name": a["name"]} for a in agents_mod.ensure_seeded()]
    print(json.dumps(state, indent=2, default=str))
    return 0


def cmd_enroll_machine(args: argparse.Namespace) -> int:
    caps = {"code": True, "shell": True, "files": True}
    result = auth.enroll_machine(name=args.name, platform=args.platform, caps=caps)
    print(json.dumps({"machine": result["machine"]["name"], "token": result["token"]}, indent=2))
    print("\nPut that token in the Windows client's config (aios_director_client.json).")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    server.main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="director")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="mint a pairing code for a phone")
    pair.add_argument("--kind", default="phone")
    pair.set_defaults(func=cmd_pair)

    status = sub.add_parser("status", help="show backends, devices and the operator display")
    status.set_defaults(func=cmd_status)

    enroll = sub.add_parser("enroll-machine", help="create a token for a client machine")
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--platform", default="windows")
    enroll.set_defaults(func=cmd_enroll_machine)

    serve = sub.add_parser("serve", help="run the Director server")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
