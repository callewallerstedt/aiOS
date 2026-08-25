"""Minecraft voice assistant integration for aiOS.

Speech routing is model-driven: every Minecraft utterance goes to the AI with a
small set of whitelisted tools. The model decides whether to answer, inspect
inventory, create a shopping list, craft from owned ingredients, or send a short
in-game message.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import re
import socket
import struct
from copy import deepcopy
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"
DEFAULT_SERVER_DIR = Path(r"C:\Users\calle\My project\minecraft-server")


ITEM_GROUPS: dict[str, dict[str, Any]] = {
    "planks": {
        "label": "Planks",
        "items": [
            "minecraft:oak_planks",
            "minecraft:spruce_planks",
            "minecraft:birch_planks",
            "minecraft:jungle_planks",
            "minecraft:acacia_planks",
            "minecraft:dark_oak_planks",
            "minecraft:mangrove_planks",
            "minecraft:cherry_planks",
            "minecraft:bamboo_planks",
            "minecraft:crimson_planks",
            "minecraft:warped_planks",
        ],
    },
    "logs": {
        "label": "Logs",
        "items": [
            "minecraft:oak_log",
            "minecraft:spruce_log",
            "minecraft:birch_log",
            "minecraft:jungle_log",
            "minecraft:acacia_log",
            "minecraft:dark_oak_log",
            "minecraft:mangrove_log",
            "minecraft:cherry_log",
            "minecraft:crimson_stem",
            "minecraft:warped_stem",
            "minecraft:bamboo_block",
        ],
    },
    "stone_for_piston": {
        "label": "Cobblestone",
        "items": ["minecraft:cobblestone", "minecraft:blackstone", "minecraft:cobbled_deepslate"],
    },
    "coal_or_charcoal": {
        "label": "Coal",
        "items": ["minecraft:coal", "minecraft:charcoal"],
    },
}


RECIPES: dict[str, dict[str, Any]] = {
    "crafting_table": {
        "aliases": ["crafting table", "workbench"],
        "label": "Crafting Table",
        "output": "minecraft:crafting_table",
        "count": 1,
        "ingredients": [{"group": "planks", "needed": 4}],
    },
    "sticks": {
        "aliases": ["stick", "sticks"],
        "label": "Sticks",
        "output": "minecraft:stick",
        "count": 4,
        "ingredients": [{"group": "planks", "needed": 2}],
    },
    "chest": {
        "aliases": ["chest"],
        "label": "Chest",
        "output": "minecraft:chest",
        "count": 1,
        "ingredients": [{"group": "planks", "needed": 8}],
    },
    "furnace": {
        "aliases": ["furnace"],
        "label": "Furnace",
        "output": "minecraft:furnace",
        "count": 1,
        "ingredients": [{"item": "minecraft:cobblestone", "label": "Cobblestone", "needed": 8}],
    },
    "torch": {
        "aliases": ["torch", "torches"],
        "label": "Torches",
        "output": "minecraft:torch",
        "count": 4,
        "ingredients": [
            {"item": "minecraft:stick", "label": "Stick", "needed": 1},
            {"group": "coal_or_charcoal", "needed": 1},
        ],
    },
    "piston": {
        "aliases": ["piston"],
        "label": "Piston",
        "output": "minecraft:piston",
        "count": 1,
        "ingredients": [
            {"group": "planks", "needed": 3},
            {"group": "stone_for_piston", "needed": 4},
            {"item": "minecraft:iron_ingot", "label": "Iron Ingot", "needed": 1},
            {"item": "minecraft:redstone", "label": "Redstone Dust", "needed": 1},
        ],
    },
}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_recipe",
            "description": "Look up a known Minecraft recipe by item name, e.g. piston or crafting table.",
            "parameters": {
                "type": "object",
                "properties": {"item_name": {"type": "string"}},
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory_counts",
            "description": "Read how many of each requested item or item group the player has.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Minecraft item IDs or known group keys such as planks, logs, stone_for_piston.",
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_shopping_list",
            "description": "Create or replace the overlay shopping list and sync counts from inventory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "item": {"type": "string"},
                                "group": {"type": "string"},
                                "needed": {"type": "integer", "minimum": 1},
                            },
                            "required": ["needed"],
                        },
                    },
                },
                "required": ["goal", "ingredients"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_shopping_items",
            "description": "Add entries to the current overlay shopping list, preserving existing entries, then sync counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Optional new or current list goal."},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "item": {"type": "string"},
                                "group": {"type": "string"},
                                "needed": {"type": "integer", "minimum": 1},
                            },
                            "required": ["needed"],
                        },
                    },
                },
                "required": ["ingredients"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_shopping_list",
            "description": "Refresh the current shopping list counts from the player's inventory.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_shopping_items",
            "description": "Remove one or more entries from the current overlay shopping list by label, item ID, or group key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names like Cobblestone, redstone, minecraft:iron_ingot, or planks.",
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_shopping_list",
            "description": "Clear the current overlay shopping list.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_shopping_list",
            "description": "Share the current shopping list in Minecraft chat, either privately or to everyone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "audience": {"type": "string", "enum": ["self", "all"]},
                    "include_done": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "craft_known_recipe",
            "description": "Craft a known recipe if the player owns the ingredients. This consumes ingredients and gives the output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe_key": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 16},
                },
                "required": ["recipe_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_player_message",
            "description": "Send a short private in-game message to the player.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "color": {"type": "string", "enum": ["aqua", "green", "yellow", "white", "red"]},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_chat_message",
            "description": "Send a short MC AI message in Minecraft chat. Use audience=all when the user asks to share or tell the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "audience": {"type": "string", "enum": ["self", "all"]},
                    "color": {"type": "string", "enum": ["aqua", "green", "yellow", "white", "red"]},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_online_players",
            "description": "List players currently online on the server.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_context",
            "description": "Get context for the requesting player, optionally including online players and positions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_others": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_world_action",
            "description": "Run one allowed quality-of-life world action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["time_day", "weather_clear", "coords"],
                    }
                },
                "required": ["action"],
            },
        },
    },
]


def foreground_window_title() -> str:
    if os.name != "nt":
        return ""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value or ""


def foreground_window_rect() -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def is_minecraft_foreground() -> bool:
    return "minecraft" in foreground_window_title().casefold()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_server_properties(server_dir: Path) -> dict[str, str]:
    path = server_dir / "server.properties"
    props: dict[str, str] = {}
    if not path.exists():
        return props
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return props
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def clean_item_id(value: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    if not text:
        return ""
    if text in ITEM_GROUPS:
        return text
    return text if ":" in text else f"minecraft:{text}"


def recipe_public(recipe_key: str, recipe: dict[str, Any]) -> dict[str, Any]:
    result = {
        "recipe_key": recipe_key,
        "label": recipe["label"],
        "output": recipe["output"],
        "count": recipe["count"],
        "ingredients": deepcopy(recipe["ingredients"]),
    }
    for ingredient in result["ingredients"]:
        if ingredient.get("group") in ITEM_GROUPS:
            group = ITEM_GROUPS[ingredient["group"]]
            ingredient["label"] = group["label"]
            ingredient["acceptable_items"] = group["items"]
    return result


class RconError(RuntimeError):
    pass


class RconClient:
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_AUTH = 3

    def __init__(self, host: str, port: int, password: str, timeout: float = 2.5):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.request_id = 100

    def __enter__(self):
        if not self.password:
            raise RconError("RCON password is empty")
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        response_id, _packet_type, _payload = self._request(self.SERVERDATA_AUTH, self.password)
        if response_id == -1:
            raise RconError("RCON authentication failed")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def command(self, command: str) -> str:
        _response_id, _packet_type, payload = self._request(self.SERVERDATA_EXECCOMMAND, command)
        return payload

    def _request(self, packet_type: int, payload: str) -> tuple[int, int, str]:
        if self.sock is None:
            raise RconError("RCON socket is not connected")
        self.request_id += 1
        encoded = payload.encode("utf-8")
        packet = struct.pack("<iii", len(encoded) + 10, self.request_id, packet_type)
        packet += encoded + b"\x00\x00"
        self.sock.sendall(packet)
        return self._recv_packet()

    def _recv_packet(self) -> tuple[int, int, str]:
        if self.sock is None:
            raise RconError("RCON socket is not connected")
        header = self._recv_exact(4)
        (length,) = struct.unpack("<i", header)
        body = self._recv_exact(length)
        response_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return response_id, packet_type, payload

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise RconError("RCON socket is not connected")
        chunks = []
        remaining = length
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RconError("RCON connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class MinecraftVoiceAssistant:
    def __init__(self, player: str | None = None):
        config = load_config()
        mc_cfg = config.get("minecraft_ai") if isinstance(config.get("minecraft_ai"), dict) else {}
        server_dir = Path(str(mc_cfg.get("server_dir") or os.environ.get("AIOS_MINECRAFT_SERVER_DIR") or DEFAULT_SERVER_DIR))
        props = load_server_properties(server_dir)
        self.server_dir = server_dir
        self.player = str(player or mc_cfg.get("player") or os.environ.get("AIOS_MINECRAFT_PLAYER") or "drwormbat")
        self.rcon_host = str(mc_cfg.get("rcon_host") or "127.0.0.1")
        self.rcon_port = int(mc_cfg.get("rcon_port") or props.get("rcon.port") or 25575)
        self.rcon_password = str(mc_cfg.get("rcon_password") or props.get("rcon.password") or os.environ.get("AIOS_MINECRAFT_RCON_PASSWORD") or "")
        self.model = str(mc_cfg.get("model") or config.get("chat_model") or "gpt-5-mini")
        self.openai_api_key = str(config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "")
        self.messages: list[dict[str, Any]] = []
        self.chat_history: list[dict[str, str]] = []
        self.suppress_tool_messages = False
        self.shopping_goal = ""
        self.shopping_items: list[dict[str, Any]] = []
        self.last_user = ""
        self.last_reply = "Ready."
        self.last_tool_results: list[dict[str, Any]] = []

    def handle(self, text: str) -> dict[str, Any]:
        self.last_user = re.sub(r"\s+", " ", str(text or "").strip())
        if not self.last_user:
            self.last_reply = "I did not catch that."
            return self.state("empty")
        if not self.openai_api_key:
            self.last_reply = "OpenAI key missing in aiOS settings."
            return self.state("missing key")

        self.last_tool_results = []
        try:
            reply = self._run_ai_turn(self.last_user)
        except Exception as exc:
            reply = f"AI error: {exc}"
        if self._is_empty_or_generic_reply(reply):
            reply = self._fallback_reply(self.last_user)
        self.last_reply = (reply or "Done.").strip()
        self._remember_turn(self.last_user, self.last_reply)
        self.sync_shopping_list()
        return self.state("done")

    def state(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "user": self.last_user,
            "reply": self.last_reply,
            "goal": self.shopping_goal,
            "shopping": deepcopy(self.shopping_items),
        }

    def _run_ai_turn(self, user_text: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.openai_api_key)
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        messages.extend(self.chat_history[-20:])
        messages.append({"role": "user", "content": user_text})

        for _round in range(6):
            response = self._chat(client, messages, tools=TOOLS)
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            messages.append(self._message_to_dict(message))
            if not tool_calls:
                return str(getattr(message, "content", "") or "").strip()
            for call in tool_calls:
                result = self._execute_tool_call(call)
                self.last_tool_results.append({"tool": call.function.name, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        return "I ran out of tool steps. Try a shorter request."

    def _fallback_reply(self, user_text: str) -> str:
        if self.last_tool_results:
            return self._summarize_tool_results()
        return self._plain_answer(user_text)

    def _plain_answer(self, user_text: str) -> str:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.openai_api_key)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Answer this Minecraft question directly and briefly. "
                        "Do not say Done. Do not claim you changed the game. "
                        "Use the recent chat context if it helps. "
                        "Keep it under 35 words."
                    ),
                },
            ]
            messages.extend(self.chat_history[-20:])
            messages.append({"role": "user", "content": user_text})
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=420,
                    reasoning_effort="minimal",
                )
            except TypeError:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=140,
                )
            answer = (response.choices[0].message.content or "").strip()
            if answer and not self._is_empty_or_generic_reply(answer):
                return answer
        except Exception:
            pass
        return "I heard the question, but I could not generate a useful answer. Ask it again with a bit more detail."

    def _remember_turn(self, user_text: str, reply: str) -> None:
        user = re.sub(r"\s+", " ", str(user_text or "").strip())
        assistant = re.sub(r"\s+", " ", str(reply or "").strip())
        if user:
            self.chat_history.append({"role": "user", "content": user})
        if assistant:
            self.chat_history.append({"role": "assistant", "content": assistant})
        self.chat_history = self.chat_history[-20:]

    def _summarize_tool_results(self) -> str:
        for entry in reversed(self.last_tool_results):
            tool = entry.get("tool")
            result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
            if not result.get("ok", True):
                return str(result.get("error") or f"{tool} failed.")
            if tool in {"set_shopping_list", "add_shopping_items", "sync_shopping_list"}:
                goal = result.get("goal") or self.shopping_goal or "shopping list"
                items = result.get("items") or self.shopping_items
                bits = []
                for item in items[:4]:
                    label = item.get("label") or item.get("item") or item.get("group") or "item"
                    have = int(item.get("have") or 0)
                    needed = int(item.get("needed") or 1)
                    bits.append(f"{label} {min(have, needed)}/{needed}")
                return f"Updated {goal}: " + (", ".join(bits) if bits else "list is ready.")
            if tool == "share_shopping_list":
                return "Shared the shopping list."
            if tool == "remove_shopping_items":
                count = int(result.get("removed_count") or 0)
                return f"Removed {count} shopping list item(s)."
            if tool == "clear_shopping_list":
                return "Cleared the shopping list."
            if tool == "craft_known_recipe":
                if result.get("crafted"):
                    return f"Crafted {result.get('label', 'item')}."
                return "I could not craft it; I made or updated the shopping list instead."
            if tool == "send_chat_message":
                return "Sent the message."
            if tool == "get_online_players":
                players = result.get("players") or []
                return "Online: " + (", ".join(players) if players else "nobody listed.")
            if tool == "safe_world_action":
                return str(result.get("message") or "World action completed.")
        return "Done."

    @staticmethod
    def _is_empty_or_generic_reply(reply: str | None) -> bool:
        text = re.sub(r"[\s.!\-]+", " ", str(reply or "").strip().casefold()).strip()
        return text in {"", "done", "ok", "okay", "completed", "complete"}

    def _chat(self, client, messages, tools):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_completion_tokens": 900,
            "reasoning_effort": "minimal",
        }
        try:
            return client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
            return client.chat.completions.create(**kwargs)

    def _message_to_dict(self, message) -> dict[str, Any]:
        item: dict[str, Any] = {"role": "assistant", "content": getattr(message, "content", None)}
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ]
        return item

    def _execute_tool_call(self, call) -> dict[str, Any]:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        try:
            if name == "lookup_recipe":
                return self.lookup_recipe(str(args.get("item_name") or ""))
            if name == "get_inventory_counts":
                return self.get_inventory_counts(args.get("items") or [])
            if name == "set_shopping_list":
                return self.set_shopping_list(str(args.get("goal") or ""), args.get("ingredients") or [])
            if name == "add_shopping_items":
                return self.add_shopping_items(str(args.get("goal") or ""), args.get("ingredients") or [])
            if name == "sync_shopping_list":
                return self.sync_shopping_list()
            if name == "remove_shopping_items":
                return self.remove_shopping_items(args.get("items") or [])
            if name == "clear_shopping_list":
                return self.clear_shopping_list()
            if name == "share_shopping_list":
                return self.share_shopping_list(
                    str(args.get("audience") or "self"),
                    bool(args.get("include_done", True)),
                )
            if name == "craft_known_recipe":
                return self.craft_known_recipe(str(args.get("recipe_key") or ""), int(args.get("quantity") or 1))
            if name == "send_player_message":
                return self.send_player_message(str(args.get("message") or ""), str(args.get("color") or "aqua"))
            if name == "send_chat_message":
                return self.send_chat_message(
                    str(args.get("message") or ""),
                    str(args.get("audience") or "self"),
                    str(args.get("color") or "aqua"),
                )
            if name == "get_online_players":
                return self.get_online_players()
            if name == "get_player_context":
                return self.get_player_context(bool(args.get("include_others", False)))
            if name == "safe_world_action":
                return self.safe_world_action(str(args.get("action") or ""))
            return {"ok": False, "error": f"unknown tool {name}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lookup_recipe(self, item_name: str) -> dict[str, Any]:
        query = item_name.strip().casefold().replace("_", " ")
        for key, recipe in RECIPES.items():
            names = [key.replace("_", " "), recipe["label"].casefold(), *recipe.get("aliases", [])]
            if query in {name.casefold() for name in names}:
                return {"ok": True, "recipe": recipe_public(key, recipe)}
        matches = [recipe_public(key, recipe) for key, recipe in RECIPES.items() if query and query in recipe["label"].casefold()]
        return {"ok": bool(matches), "matches": matches, "known": sorted(RECIPES.keys())}

    def get_inventory_counts(self, items: list[str]) -> dict[str, Any]:
        results = []
        with self._connect() as rcon:
            for item in items:
                results.append(self._count_spec(rcon, {"item": item}))
        return {"ok": True, "items": results}

    def set_shopping_list(self, goal: str, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
        cleaned = self._clean_shopping_ingredients(ingredients)
        self.shopping_goal = goal.strip()[:64] or "Shopping List"
        self.shopping_items = cleaned[:8]
        sync = self.sync_shopping_list()
        try:
            self.send_player_message(f"Shopping list: {self.shopping_goal}", "aqua")
        except Exception:
            pass
        return sync

    def add_shopping_items(self, goal: str, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
        additions = self._clean_shopping_ingredients(ingredients)
        if goal.strip():
            self.shopping_goal = goal.strip()[:64]
        elif not self.shopping_goal:
            self.shopping_goal = "Shopping List"
        merged = list(self.shopping_items)
        for addition in additions:
            add_keys = self._shopping_item_keys(addition)
            existing = next((item for item in merged if self._shopping_item_keys(item) & add_keys), None)
            if existing:
                existing["needed"] = max(int(existing.get("needed") or 1), int(addition.get("needed") or 1))
                if addition.get("label"):
                    existing["label"] = addition["label"]
            else:
                merged.append(addition)
        self.shopping_items = merged[:8]
        return self.sync_shopping_list()

    def _clean_shopping_ingredients(self, ingredients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned = []
        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                continue
            needed = max(1, int(ingredient.get("needed") or 1))
            spec = {
                "label": str(ingredient.get("label") or "").strip(),
                "item": clean_item_id(str(ingredient.get("item") or "")),
                "group": str(ingredient.get("group") or "").strip(),
                "needed": needed,
                "have": 0,
            }
            if spec["group"] in ITEM_GROUPS and not spec["label"]:
                spec["label"] = ITEM_GROUPS[spec["group"]]["label"]
            if not spec["label"]:
                spec["label"] = spec["item"].replace("minecraft:", "").replace("_", " ").title()
            if spec["group"] or spec["item"]:
                cleaned.append(spec)
        return cleaned

    def sync_shopping_list(self) -> dict[str, Any]:
        if not self.shopping_items:
            return {"ok": True, "goal": "", "items": []}
        try:
            with self._connect() as rcon:
                synced = []
                for item in self.shopping_items:
                    count_info = self._count_spec(rcon, item)
                    item["have"] = int(count_info.get("count") or 0)
                    item["done"] = item["have"] >= int(item.get("needed") or 1)
                    synced.append(deepcopy(item))
            return {"ok": True, "goal": self.shopping_goal, "items": synced}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "goal": self.shopping_goal, "items": deepcopy(self.shopping_items)}

    def remove_shopping_items(self, items: list[str]) -> dict[str, Any]:
        targets = {self._shopping_match_key(item) for item in items if str(item or "").strip()}
        if not targets:
            return {"ok": False, "error": "no items requested", "goal": self.shopping_goal, "items": deepcopy(self.shopping_items)}
        before = len(self.shopping_items)
        removed = []
        kept = []
        for item in self.shopping_items:
            keys = self._shopping_item_keys(item)
            if keys & targets:
                removed.append(deepcopy(item))
            else:
                kept.append(item)
        self.shopping_items = kept
        if not self.shopping_items:
            self.shopping_goal = ""
        sync = self.sync_shopping_list()
        sync.update({"ok": True, "removed": removed, "removed_count": before - len(kept)})
        return sync

    def clear_shopping_list(self) -> dict[str, Any]:
        removed = deepcopy(self.shopping_items)
        self.shopping_goal = ""
        self.shopping_items = []
        return {"ok": True, "removed": removed, "goal": "", "items": []}

    def share_shopping_list(self, audience: str = "self", include_done: bool = True) -> dict[str, Any]:
        self.sync_shopping_list()
        audience = "all" if str(audience).strip().lower() == "all" else "self"
        if not self.shopping_items:
            return self.send_chat_message("Shopping list is empty.", audience, "yellow")
        lines = [f"{self.shopping_goal or 'Shopping list'}:"]
        for item in self.shopping_items:
            have = int(item.get("have") or 0)
            needed = max(1, int(item.get("needed") or 1))
            if not include_done and have >= needed:
                continue
            label = str(item.get("label") or item.get("item") or item.get("group") or "Item")
            lines.append(f"{label} {min(have, needed)}/{needed}")
        message = " | ".join(lines)
        return self.send_chat_message(message, audience, "aqua")

    def craft_known_recipe(self, recipe_key: str, quantity: int = 1) -> dict[str, Any]:
        key = recipe_key.strip().lower().replace(" ", "_")
        recipe = RECIPES.get(key)
        if not recipe:
            return {"ok": False, "error": f"unknown recipe {recipe_key}", "known": sorted(RECIPES.keys())}
        quantity = max(1, min(16, int(quantity or 1)))
        with self._connect() as rcon:
            required = []
            for ingredient in recipe["ingredients"]:
                spec = deepcopy(ingredient)
                spec["needed"] = int(spec["needed"]) * quantity
                count = self._count_spec(rcon, spec)
                spec["have"] = int(count["count"])
                required.append(spec)
            missing = [item for item in required if item["have"] < item["needed"]]
            if missing:
                self.set_shopping_list(recipe["label"], required)
                return {"ok": False, "crafted": False, "missing": missing, "shopping": self.shopping_items}
            for ingredient in required:
                self._consume_spec(rcon, ingredient, int(ingredient["needed"]))
            output_count = int(recipe["count"]) * quantity
            rcon.command(f"give {self.player} {recipe['output']} {output_count}")
        self.send_player_message(f"Crafted {recipe['label']}.", "green")
        self.shopping_goal = ""
        self.shopping_items = []
        return {"ok": True, "crafted": True, "label": recipe["label"], "count": output_count}

    def send_player_message(self, message: str, color: str = "aqua") -> dict[str, Any]:
        if self.suppress_tool_messages:
            clean = re.sub(r"\s+", " ", str(message or "").strip())[:320]
            return {"ok": True, "audience": "self", "message": clean, "suppressed": True}
        return self.send_chat_message(message, "self", color)

    def send_chat_message(self, message: str, audience: str = "self", color: str = "aqua") -> dict[str, Any]:
        color = color if color in {"aqua", "green", "yellow", "white", "red"} else "aqua"
        audience = "all" if str(audience).strip().lower() == "all" else "self"
        message = re.sub(r"\s+", " ", message).strip()[:320]
        if not message:
            return {"ok": False, "error": "empty message"}
        target = "@a" if audience == "all" else self.player
        prefix = "[MC AI] " if audience == "all" else "MC AI: "
        payload = json.dumps({"text": prefix + message, "color": color})
        with self._connect() as rcon:
            rcon.command(f"tellraw {target} {payload}")
        return {"ok": True, "audience": audience, "message": message}

    def get_online_players(self) -> dict[str, Any]:
        with self._connect() as rcon:
            response = rcon.command("list")
        players = []
        match = re.search(r"players online:\s*(.*)$", response)
        if match:
            players = [name.strip() for name in match.group(1).split(",") if name.strip()]
        return {"ok": True, "raw": response, "players": players}

    def get_player_context(self, include_others: bool = False) -> dict[str, Any]:
        with self._connect() as rcon:
            online = self.get_online_players()
            players = online.get("players") or []
            context = {
                "requester": self.player,
                "online_players": players,
                "requester_position": self._read_player_position(rcon, self.player),
                "shopping_goal": self.shopping_goal,
                "shopping_items": deepcopy(self.shopping_items),
            }
            if include_others:
                context["player_positions"] = {
                    player: self._read_player_position(rcon, player) for player in players
                }
        return {"ok": True, "context": context}

    def safe_world_action(self, action: str) -> dict[str, Any]:
        action = action.strip().lower()
        with self._connect() as rcon:
            if action == "time_day":
                rcon.command("time set day")
                return self.send_player_message("Time set to day.", "green")
            if action == "weather_clear":
                rcon.command("weather clear")
                return self.send_player_message("Weather cleared.", "green")
            if action == "coords":
                response = rcon.command(f"data get entity {self.player} Pos")
                nums = self._numbers_from_pos_response(response)
                if len(nums) >= 3:
                    coords = ", ".join(str(num) for num in nums[:3])
                    message = f"Coords: {coords}"
                else:
                    message = response.strip()[:180] or "Could not read coordinates."
                rcon.command(f"tellraw {self.player} {json.dumps({'text': 'MC AI: ' + message, 'color': 'aqua'})}")
                return {"ok": True, "message": message}
        return {"ok": False, "error": f"unsupported action {action}"}

    def _connect(self) -> RconClient:
        return RconClient(self.rcon_host, self.rcon_port, self.rcon_password)

    def _count_spec(self, rcon: RconClient, spec: dict[str, Any]) -> dict[str, Any]:
        group_key = str(spec.get("group") or "").strip()
        item = clean_item_id(str(spec.get("item") or ""))
        if group_key in ITEM_GROUPS:
            group = ITEM_GROUPS[group_key]
            variants = [{"item": candidate, "count": self._count_item(rcon, candidate)} for candidate in group["items"]]
            return {
                "label": spec.get("label") or group["label"],
                "group": group_key,
                "count": sum(entry["count"] for entry in variants),
                "variants": variants,
            }
        if item in ITEM_GROUPS:
            group = ITEM_GROUPS[item]
            variants = [{"item": candidate, "count": self._count_item(rcon, candidate)} for candidate in group["items"]]
            return {
                "label": spec.get("label") or group["label"],
                "group": item,
                "count": sum(entry["count"] for entry in variants),
                "variants": variants,
            }
        return {
            "label": spec.get("label") or item.replace("minecraft:", "").replace("_", " ").title(),
            "item": item,
            "count": self._count_item(rcon, item) if item else 0,
        }

    def _count_item(self, rcon: RconClient, item: str) -> int:
        response = rcon.command(f"clear {self.player} {item} 0")
        numbers = [int(match) for match in re.findall(r"\d+", response)]
        return numbers[0] if numbers else 0

    def _read_player_position(self, rcon: RconClient, player: str) -> dict[str, Any]:
        response = rcon.command(f"data get entity {player} Pos")
        nums = self._numbers_from_pos_response(response)
        if len(nums) >= 3:
            return {"ok": True, "x": nums[0], "y": nums[1], "z": nums[2]}
        return {"ok": False, "raw": response.strip()[:180]}

    @staticmethod
    def _numbers_from_pos_response(response: str) -> list[float]:
        match = re.search(r"\[([^\]]+)\]", response)
        source = match.group(1) if match else response
        return [round(float(num), 1) for num in re.findall(r"-?\d+(?:\.\d+)?", source)]

    def _consume_spec(self, rcon: RconClient, spec: dict[str, Any], needed: int) -> None:
        group_key = str(spec.get("group") or "").strip()
        item = clean_item_id(str(spec.get("item") or ""))
        candidates = ITEM_GROUPS[group_key]["items"] if group_key in ITEM_GROUPS else [item]
        remaining = needed
        for candidate in candidates:
            if remaining <= 0:
                return
            have = self._count_item(rcon, candidate)
            take = min(have, remaining)
            if take > 0:
                rcon.command(f"clear {self.player} {candidate} {take}")
                remaining -= take

    def _shopping_item_keys(self, item: dict[str, Any]) -> set[str]:
        values = [
            str(item.get("label") or ""),
            str(item.get("item") or ""),
            str(item.get("group") or ""),
        ]
        keys = {self._shopping_match_key(value) for value in values if value}
        if str(item.get("item") or "").startswith("minecraft:"):
            keys.add(self._shopping_match_key(str(item.get("item")).split(":", 1)[1]))
        return keys

    @staticmethod
    def _shopping_match_key(value: str) -> str:
        text = str(value or "").strip().casefold()
        text = text.replace("minecraft:", "")
        text = re.sub(r"[_-]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text

    def _system_prompt(self) -> str:
        recipes = ", ".join(sorted(RECIPES.keys()))
        return (
            "You are MC AI inside aiOS, helping one Minecraft Java player. "
            "Every user utterance has already been transcribed from voice while Minecraft is active. "
            "For normal questions, answer in text and do not change the game. "
            "Only execute commands, alter inventory, alter shopping lists, send public server messages, craft items, set time/weather, or teleport when the user directly asks for that action. "
            "Examples of direct action wording: add, remove, clear, share, craft, make this now, set, send, tell everyone, announce, time day, clear weather, where am I. "
            "If the user asks 'how', 'what do I need', 'can I', 'what should', or another general question, answer first without tools unless inventory counts are explicitly needed. "
            "Use tools for inventory/list/action requests, but avoid tools for ordinary knowledge questions. "
            "A single user request may need multiple tools. Chain them in order and do not stop after the first tool if the user asked for more. "
            "For 'I want to make X' or 'what do I need for X', answer with the ingredients; only call set_shopping_list if the user asks to add/create a list. "
            "For 'add ... to my shopping list', call add_shopping_items. "
            "For 'add ... to my shopping list and share it', call add_shopping_items, then sync_shopping_list, then share_shopping_list. "
            "For 'craft/make X now', call lookup_recipe, check inventory, then craft_known_recipe if it is a known recipe. "
            "For removing shopping-list entries, call remove_shopping_items or clear_shopping_list. "
            "For sharing the list, call share_shopping_list with audience all if the user wants friends/server chat to see it. "
            "For 'say/tell/announce/share this', call send_chat_message; use audience all only when requested. "
            "For questions that depend on who asked, locations, or other players, call get_player_context. "
            "For normal Minecraft questions, answer briefly in plain language. "
            "Do not claim you changed the game unless a tool result says it succeeded. "
            "Keep final answers under 30 words. One or two short sentences. "
            f"Known recipe keys: {recipes}. Player: {self.player}."
        )
