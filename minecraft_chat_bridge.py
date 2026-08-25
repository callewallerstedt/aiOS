"""Bridge Minecraft player chat to MC AI.

The vanilla server logs normal player chat as:
  [time] [Server thread/INFO]: <Player> message

This bridge tails latest.log and dispatches messages that start with "ai" to a
per-player MinecraftVoiceAssistant instance. It does not react to tellraw output,
RCON output, server messages, or ordinary chat.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

from minecraft_ai import DEFAULT_SERVER_DIR, MinecraftVoiceAssistant, load_config


CHAT_RE = re.compile(r"\]: <([^>]{1,32})> (.*)$")
TRIGGER_RE = re.compile(r"^\s*(?:@?ai)\b[\s:,\-!]*(.*)$", re.IGNORECASE)
SET_WP_RE = re.compile(r"^\s*set\s+wp\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
TP_WP_RE = re.compile(r"^\s*tp\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
LIST_WP_RE = re.compile(r"^\s*(?:list\s+wp|wps|waypoints)\s*$", re.IGNORECASE)
DEL_WP_RE = re.compile(r"^\s*(?:del|delete|remove)\s+wp\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
SET_CHUNKLOAD_RE = re.compile(r"^\s*chunkload\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
LIST_CHUNKLOAD_RE = re.compile(r"^\s*(?:list\s+chunkload|chunkloads)\s*$", re.IGNORECASE)
DEL_CHUNKLOAD_RE = re.compile(r"^\s*(?:del|delete|remove)\s+chunkload\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
SET_SPAWNER_RE = re.compile(r"^\s*spawner\s+(?:set\s+)?([A-Za-z0-9_-]{1,32})\s+([A-Za-z_]{1,32})\s*$", re.IGNORECASE)
LIST_SPAWNER_RE = re.compile(r"^\s*(?:spawner\s+list|list\s+spawner|spawners)\s*$", re.IGNORECASE)
DEL_SPAWNER_RE = re.compile(r"^\s*(?:spawner\s+(?:del|delete|remove)|(?:del|delete|remove)\s+spawner)\s+([A-Za-z0-9_-]{1,32})\s*$", re.IGNORECASE)
BAKER_RE = re.compile(r"^\s*baker(?:\s+(.+?))?\s*$", re.IGNORECASE)
MINER_RE = re.compile(r"^\s*miner(?:\s+(.+?))?\s*$", re.IGNORECASE)
BUILD_RE = re.compile(r"^\s*(?:bt|build)\s+(.+?)\s*$", re.IGNORECASE)
DAY_RE = re.compile(r"^\s*(?:day|set\s+day|time\s+day)\s*$", re.IGNORECASE)
NIGHT_RE = re.compile(r"^\s*(?:night|set\s+night|time\s+night)\s*$", re.IGNORECASE)
WHERE_RE = re.compile(r"^\s*where\s+([A-Za-z0-9_]{1,32})\s*$", re.IGNORECASE)
BACK_RE = re.compile(r"^\s*back\s*$", re.IGNORECASE)
EAT_RE = re.compile(r"^\s*eat\s*$", re.IGNORECASE)
TOP_RE = re.compile(r"^\s*top\s*$", re.IGNORECASE)
MAX_BUILD_BLOCKS = 4096
BAKER_JOB_SECONDS = 180
MINER_TICK_SECONDS = 1.5
MINER_SEARCH_RADIUS = 9
MINER_REACH_DISTANCE = 4.2
SPAWNER_TICK_SECONDS = 10.0
SPAWNER_DEFAULT_RADIUS = 64
SPAWNER_DEFAULT_MIN_MOBS = 0
SPAWNER_DEFAULT_MAX_MOBS = 0
SPAWNER_COUNT_OBJECTIVE = "aios_spawner_counts"
BRIDGE_LOG_PATH = Path(__file__).resolve().parent / "voice-err.log"

CAKE_RECIPE = {
    "minecraft:milk_bucket": 3,
    "minecraft:sugar": 2,
    "minecraft:egg": 1,
    "minecraft:wheat": 3,
}

STACK_LIMITS = {
    "minecraft:cake": 1,
    "minecraft:bucket": 16,
    "minecraft:milk_bucket": 1,
}

MINER_TARGETS = {
    "coal": {"blocks": ["minecraft:coal_ore", "minecraft:deepslate_coal_ore"], "drop": "minecraft:coal"},
    "iron": {"blocks": ["minecraft:iron_ore", "minecraft:deepslate_iron_ore"], "drop": "minecraft:raw_iron"},
    "copper": {"blocks": ["minecraft:copper_ore", "minecraft:deepslate_copper_ore"], "drop": "minecraft:raw_copper"},
    "gold": {
        "blocks": ["minecraft:gold_ore", "minecraft:deepslate_gold_ore", "minecraft:nether_gold_ore"],
        "drop": "minecraft:raw_gold",
    },
    "redstone": {"blocks": ["minecraft:redstone_ore", "minecraft:deepslate_redstone_ore"], "drop": "minecraft:redstone"},
    "lapis": {"blocks": ["minecraft:lapis_ore", "minecraft:deepslate_lapis_ore"], "drop": "minecraft:lapis_lazuli"},
    "diamond": {"blocks": ["minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"], "drop": "minecraft:diamond"},
    "emerald": {"blocks": ["minecraft:emerald_ore", "minecraft:deepslate_emerald_ore"], "drop": "minecraft:emerald"},
    "quartz": {"blocks": ["minecraft:nether_quartz_ore"], "drop": "minecraft:quartz"},
    "debris": {"blocks": ["minecraft:ancient_debris"], "drop": "minecraft:ancient_debris"},
}

MINER_ALL_TARGETS = ["coal", "iron", "copper", "gold", "redstone", "lapis", "diamond", "emerald", "quartz", "debris"]

SPAWNER_MOBS = {
    "skeleton": "minecraft:skeleton",
    "skelly": "minecraft:skeleton",
    "zombie": "minecraft:zombie",
    "spider": "minecraft:spider",
    "cave_spider": "minecraft:cave_spider",
    "cavespider": "minecraft:cave_spider",
    "creeper": "minecraft:creeper",
    "blaze": "minecraft:blaze",
}

MINER_PICKAXES = [
    ("minecraft:netherite_pickaxe", 2031, 5),
    ("minecraft:diamond_pickaxe", 1562, 4),
    ("minecraft:iron_pickaxe", 251, 3),
    ("minecraft:stone_pickaxe", 132, 2),
    ("minecraft:golden_pickaxe", 33, 1),
    ("minecraft:wooden_pickaxe", 60, 1),
]

MINER_PASSABLE_BLOCKS = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
    "minecraft:water",
    "minecraft:torch",
    "minecraft:soul_torch",
}

SAFE_FOODS = [
    {"item": "minecraft:dried_kelp", "label": "dried kelp", "nutrition": 1, "priority": 1},
    {"item": "minecraft:cookie", "label": "cookie", "nutrition": 2, "priority": 1},
    {"item": "minecraft:melon_slice", "label": "melon slice", "nutrition": 2, "priority": 1},
    {"item": "minecraft:sweet_berries", "label": "sweet berries", "nutrition": 2, "priority": 1},
    {"item": "minecraft:glow_berries", "label": "glow berries", "nutrition": 2, "priority": 1},
    {"item": "minecraft:carrot", "label": "carrot", "nutrition": 3, "priority": 2},
    {"item": "minecraft:apple", "label": "apple", "nutrition": 4, "priority": 3},
    {"item": "minecraft:bread", "label": "bread", "nutrition": 5, "priority": 4},
    {"item": "minecraft:baked_potato", "label": "baked potato", "nutrition": 5, "priority": 4},
    {"item": "minecraft:cooked_cod", "label": "cooked cod", "nutrition": 5, "priority": 4},
    {"item": "minecraft:cooked_rabbit", "label": "cooked rabbit", "nutrition": 5, "priority": 4},
    {"item": "minecraft:cooked_chicken", "label": "cooked chicken", "nutrition": 6, "priority": 5},
    {"item": "minecraft:cooked_salmon", "label": "cooked salmon", "nutrition": 6, "priority": 5},
    {"item": "minecraft:cooked_mutton", "label": "cooked mutton", "nutrition": 6, "priority": 5},
    {"item": "minecraft:pumpkin_pie", "label": "pumpkin pie", "nutrition": 8, "priority": 5},
    {"item": "minecraft:cooked_beef", "label": "steak", "nutrition": 8, "priority": 6},
    {"item": "minecraft:cooked_porkchop", "label": "cooked porkchop", "nutrition": 8, "priority": 6},
]

UNSAFE_TOP_BLOCKS = (
    "water",
    "lava",
    "fire",
    "soul_fire",
    "powder_snow",
    "cactus",
    "campfire",
    "soul_campfire",
    "magma_block",
    "sweet_berry_bush",
    "pointed_dripstone",
)


def bridge_log(message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with BRIDGE_LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"{timestamp} minecraft chat bridge: {message}\n")
    except OSError:
        pass


class MinecraftChatBridge:
    def __init__(self):
        config = load_config()
        mc_cfg = config.get("minecraft_ai") if isinstance(config.get("minecraft_ai"), dict) else {}
        self.server_dir = Path(str(mc_cfg.get("server_dir") or DEFAULT_SERVER_DIR))
        self.log_path = self.server_dir / "logs" / "latest.log"
        self.waypoints_path = self.server_dir / "aios-waypoints.json"
        self.buildtools_path = self.server_dir / "aios-buildtools.json"
        self.chunkloads_path = self.server_dir / "aios-chunkloads.json"
        self.spawners_path = self.server_dir / "aios-spawners.json"
        self.bakers_path = self.server_dir / "aios-bakers.json"
        self.miners_path = self.server_dir / "aios-miners.json"
        self.assistants: dict[str, MinecraftVoiceAssistant] = {}
        self.locks: dict[str, threading.Lock] = {}
        self.waypoint_lock = threading.Lock()
        self.buildtools_lock = threading.Lock()
        self.chunkload_lock = threading.Lock()
        self.spawner_lock = threading.Lock()
        self.baker_lock = threading.Lock()
        self.miner_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.baker_thread: threading.Thread | None = None
        self.miner_thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        self.thread = threading.Thread(target=self._tail_loop, daemon=True)
        self.thread.start()
        if not self.baker_thread or not self.baker_thread.is_alive():
            self.baker_thread = threading.Thread(target=self._baker_loop, daemon=True)
            self.baker_thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _assistant_for(self, player: str) -> MinecraftVoiceAssistant:
        key = player.casefold()
        assistant = self.assistants.get(key)
        if assistant is None:
            assistant = MinecraftVoiceAssistant(player=player)
            self.assistants[key] = assistant
            self.locks[key] = threading.Lock()
        return assistant

    def _lock_for(self, player: str) -> threading.Lock:
        self._assistant_for(player)
        return self.locks[player.casefold()]

    def _tail_loop(self) -> None:
        last_inode = None
        position = 0
        while not self.stop_event.is_set():
            if not self.log_path.exists():
                time.sleep(1.0)
                continue
            try:
                stat = self.log_path.stat()
                inode = (stat.st_ino, stat.st_size, stat.st_mtime_ns) if hasattr(stat, "st_ino") else (stat.st_size, stat.st_mtime_ns)
                if last_inode is None or stat.st_size < position:
                    position = stat.st_size
                    last_inode = inode
                with self.log_path.open("r", encoding="utf-8", errors="ignore") as file:
                    file.seek(position)
                    lines = file.readlines()
                    position = file.tell()
                for line in lines:
                    self._handle_log_line(line.rstrip("\r\n"))
            except Exception:
                time.sleep(1.0)
            time.sleep(0.25)

    def _handle_log_line(self, line: str) -> None:
        match = CHAT_RE.search(line)
        if not match:
            return
        player = match.group(1).strip()
        message = match.group(2).strip()
        if self._handle_player_qol_command(player, message):
            return
        if self._handle_chunkload_command(player, message):
            return
        if self._handle_spawner_command(player, message):
            return
        if self._handle_time_command(player, message):
            return
        if self._handle_miner_command(player, message):
            return
        if self._handle_baker_command(player, message):
            return
        if self._handle_build_command(player, message):
            return
        if self._handle_waypoint_command(player, message):
            return
        trigger = TRIGGER_RE.match(message)
        if not trigger:
            return
        request = trigger.group(1).strip()
        if not request:
            request = "help"
        if self._is_help_request(request):
            threading.Thread(target=self._send_help, args=(player,), daemon=True).start()
            return
        threading.Thread(target=self._run_request, args=(player, request), daemon=True).start()

    def _handle_player_qol_command(self, player: str, message: str) -> bool:
        where_match = WHERE_RE.match(message)
        if where_match:
            threading.Thread(target=self._where_player, args=(player, where_match.group(1)), daemon=True).start()
            return True
        tp_match = TP_WP_RE.match(message)
        if tp_match:
            threading.Thread(target=self._teleport_player_or_waypoint, args=(player, tp_match.group(1)), daemon=True).start()
            return True
        if BACK_RE.match(message):
            threading.Thread(target=self._back_to_death, args=(player,), daemon=True).start()
            return True
        if EAT_RE.match(message):
            threading.Thread(target=self._eat_best_food, args=(player,), daemon=True).start()
            return True
        if TOP_RE.match(message):
            threading.Thread(target=self._teleport_top, args=(player,), daemon=True).start()
            return True
        return False

    def _handle_chunkload_command(self, player: str, message: str) -> bool:
        set_match = SET_CHUNKLOAD_RE.match(message)
        if set_match:
            threading.Thread(target=self._set_chunkload, args=(player, set_match.group(1)), daemon=True).start()
            return True
        delete_match = DEL_CHUNKLOAD_RE.match(message)
        if delete_match:
            threading.Thread(target=self._delete_chunkload, args=(player, delete_match.group(1)), daemon=True).start()
            return True
        if LIST_CHUNKLOAD_RE.match(message):
            threading.Thread(target=self._list_chunkloads, args=(player,), daemon=True).start()
            return True
        return False

    def _handle_spawner_command(self, player: str, message: str) -> bool:
        set_match = SET_SPAWNER_RE.match(message)
        if set_match:
            threading.Thread(
                target=self._set_spawner_keeper,
                args=(player, set_match.group(1), set_match.group(2)),
                daemon=True,
            ).start()
            return True
        delete_match = DEL_SPAWNER_RE.match(message)
        if delete_match:
            threading.Thread(target=self._delete_spawner_keeper, args=(player, delete_match.group(1)), daemon=True).start()
            return True
        if LIST_SPAWNER_RE.match(message):
            threading.Thread(target=self._list_spawner_keepers, args=(player,), daemon=True).start()
            return True
        return False

    def _handle_miner_command(self, player: str, message: str) -> bool:
        match = MINER_RE.match(message)
        if not match:
            return False
        text = re.sub(r"\s+", " ", str(match.group(1) or "").strip())
        threading.Thread(target=self._forward_miner_command, args=(player, text or "help"), daemon=True).start()
        return True

    def _forward_miner_command(self, player: str, text: str) -> None:
        assistant = self._assistant_for(player)
        try:
            safe_text = str(text or "help").strip()
            if not re.fullmatch(r"[A-Za-z0-9_ -]{1,120}", safe_text):
                assistant.send_player_message("Miner command contains unsupported characters.", "yellow")
                return
            with assistant._connect() as rcon:
                response = rcon.command(f"execute as {player} at {player} run miner {safe_text}")
            if "Unknown or incomplete command" in response or "Unknown command" in response:
                assistant.send_player_message("Real miner mod is not loaded yet. I am installing it now.", "yellow")
            elif response.strip():
                bridge_log(f"miner forward {player}: {response.strip()[:180]}")
        except Exception as exc:
            assistant.send_player_message(f"Miner command failed: {exc}", "red")

    def _send_miner_help(self, player: str) -> None:
        assistant = self._assistant_for(player)
        targets = ", ".join(["all", *MINER_TARGETS.keys()])
        for line in [
            "Miner: miner create name | miner chest name | miner start name iron | miner stop name",
            "Put pickaxes in the registered chest/barrel. Drops go back into that storage.",
            f"Targets: {targets}",
        ]:
            try:
                assistant.send_player_message(line, "aqua")
            except Exception:
                pass

    def _create_miner(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        tag = self._miner_tag(key)
        try:
            with self.miner_lock:
                state = self._read_miners_unlocked()
                if key in state:
                    assistant.send_player_message(f"Miner {safe_name} already exists.", "yellow")
                    return
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
                if not pos.get("ok"):
                    assistant.send_player_message("Could not read your position.", "red")
                    return
                x = float(pos["x"])
                y = float(pos["y"])
                z = float(pos["z"])
                custom_name = json.dumps(f"Miner {safe_name}")
                summon = (
                    f"execute in {dimension} run summon villager {x:.2f} {y:.2f} {z:.2f} "
                    "{NoAI:1b,PersistenceRequired:1b,Silent:0b,"
                    f'Tags:["aios_miner","{tag}"],'
                    f"CustomName:'{custom_name}',"
                    'VillagerData:{profession:"minecraft:toolsmith",level:2,type:"minecraft:plains"}}'
                )
                response = rcon.command(summon)
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "summon failed")
            miner = {
                "name": safe_name,
                "dimension": dimension,
                "x": x,
                "y": y,
                "z": z,
                "created_by": player,
                "created_at": time.time(),
                "storages": [],
                "target": None,
                "active": False,
                "current_tool": None,
                "mined": 0,
                "last_notice_at": 0,
                "last_action_at": 0,
            }
            with self.miner_lock:
                state = self._read_miners_unlocked()
                if key in state:
                    assistant.send_player_message(f"Miner {safe_name} already exists.", "yellow")
                    return
                state[key] = miner
                self._write_miners_unlocked(state)
            assistant.send_chat_message(f"Miner {safe_name} hired. Put pickaxes in a chest, then type: miner chest {safe_name}", "all", "green")
        except Exception as exc:
            assistant.send_player_message(f"Miner create failed: {exc}", "red")

    def _add_miner_storage(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
                if not pos.get("ok"):
                    assistant.send_player_message("Could not read your position.", "red")
                    return
                container = self._find_nearby_container(rcon, dimension, pos)
            if not container:
                assistant.send_player_message("No chest or barrel found within 4 blocks.", "yellow")
                return
            with self.miner_lock:
                state = self._read_miners_unlocked()
                miner = state.get(key)
                if not isinstance(miner, dict):
                    assistant.send_player_message(f"No miner named {safe_name}.", "yellow")
                    return
                storages = miner.setdefault("storages", [])
                if any(self._same_block(entry, container) for entry in storages if isinstance(entry, dict)):
                    assistant.send_player_message("That storage is already registered.", "yellow")
                    return
                storages.append(container)
                self._write_miners_unlocked(state)
            assistant.send_player_message(f"Added storage to miner {safe_name}.", "green")
        except Exception as exc:
            assistant.send_player_message(f"Miner chest failed: {exc}", "red")

    def _start_miner(self, player: str, name: str, target: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        target_key = self._normalize_miner_target(target)
        if not target_key:
            assistant.send_player_message("Unknown target. Try coal, iron, diamond, redstone, gold, copper, emerald, lapis, quartz, debris, or all.", "yellow")
            return
        try:
            with self.miner_lock:
                state = self._read_miners_unlocked()
                miner = state.get(safe_name.casefold())
                if not isinstance(miner, dict):
                    assistant.send_player_message(f"No miner named {safe_name}.", "yellow")
                    return
                if not miner.get("storages"):
                    assistant.send_player_message(f"Add storage first: miner chest {safe_name}", "yellow")
                    return
                miner["target"] = target_key
                miner["active"] = True
                miner["last_notice_at"] = 0
                self._write_miners_unlocked(state)
            assistant.send_chat_message(f"Miner {safe_name} is mining {target_key}.", "all", "green")
        except Exception as exc:
            assistant.send_player_message(f"Miner start failed: {exc}", "red")

    def _stop_miner(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        try:
            with self.miner_lock:
                state = self._read_miners_unlocked()
                miner = state.get(safe_name.casefold())
                if not isinstance(miner, dict):
                    assistant.send_player_message(f"No miner named {safe_name}.", "yellow")
                    return
                miner["active"] = False
                self._write_miners_unlocked(state)
            assistant.send_chat_message(f"Miner {safe_name} stopped.", "all", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Miner stop failed: {exc}", "red")

    def _remove_miner(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with self.miner_lock:
                state = self._read_miners_unlocked()
                removed = state.pop(key, None)
                self._write_miners_unlocked(state)
            if not removed:
                assistant.send_player_message(f"No miner named {safe_name}.", "yellow")
                return
            with assistant._connect() as rcon:
                rcon.command(f"kill {self._miner_selector(key)}")
            assistant.send_chat_message(f"Miner {safe_name} removed.", "all", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Miner remove failed: {exc}", "red")

    def _miner_status(self, player: str, name: str = "") -> None:
        assistant = self._assistant_for(player)
        with self.miner_lock:
            miners = self._read_miners_unlocked()
        if not miners:
            assistant.send_player_message("No miners yet. Use: miner create name", "yellow")
            return
        if not name:
            names = ", ".join(sorted(str(m.get("name") or key) for key, m in miners.items() if isinstance(m, dict)))
            assistant.send_player_message(f"Miners: {names}", "aqua")
            return
        miner = miners.get(self._safe_wp_name(name).casefold())
        if not isinstance(miner, dict):
            assistant.send_player_message(f"No miner named {name}.", "yellow")
            return
        tool = miner.get("current_tool") if isinstance(miner.get("current_tool"), dict) else None
        tool_text = "no tool loaded" if not tool else f"{tool.get('item', '').replace('minecraft:', '')} {int(tool.get('uses_left') or 0)} uses"
        status = "running" if miner.get("active") else "stopped"
        target = miner.get("target") or "none"
        assistant.send_player_message(
            f"{miner.get('name', name)}: {status}, target {target}, {len(miner.get('storages', []))} storage, {tool_text}, mined {int(miner.get('mined') or 0)}.",
            "aqua",
        )

    def _miner_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick_miners()
            except Exception as exc:
                bridge_log(f"miner loop failed: {exc}")
            self.stop_event.wait(MINER_TICK_SECONDS)

    def _tick_miners(self) -> None:
        with self.miner_lock:
            state = self._read_miners_unlocked()
            miners = [(key, miner) for key, miner in state.items() if isinstance(miner, dict) and miner.get("active")]
        if not miners:
            return
        assistant = self._assistant_for("drwormbat")
        with assistant._connect() as rcon:
            for key, miner in miners:
                try:
                    changed = self._process_miner(rcon, key, miner)
                    if changed:
                        with self.miner_lock:
                            state = self._read_miners_unlocked()
                            if key in state:
                                state[key] = miner
                                self._write_miners_unlocked(state)
                except Exception as exc:
                    bridge_log(f"miner {miner.get('name', key)} failed: {exc}")

    def _process_miner(self, rcon, key: str, miner: dict) -> bool:
        changed = False
        if not miner.get("storages"):
            miner["active"] = False
            self._miner_notice(rcon, miner, "needs a chest/barrel before mining.")
            return True
        target_key = self._normalize_miner_target(str(miner.get("target") or ""))
        if not target_key:
            miner["active"] = False
            self._miner_notice(rcon, miner, "has no valid target.")
            return True
        if not self._ensure_miner_entity(rcon, key, miner):
            return False
        pos = self._read_miner_position(rcon, key)
        if not pos:
            return False
        miner["x"], miner["y"], miner["z"] = pos["x"], pos["y"], pos["z"]

        if not self._ensure_miner_tool(rcon, miner):
            self._miner_notice(rcon, miner, "needs a pickaxe in storage.")
            return changed

        target = miner.get("target_pos") if isinstance(miner.get("target_pos"), dict) else None
        if not target or not self._is_target_block(rcon, target, target_key):
            now = time.time()
            if now - float(miner.get("last_scan_at") or 0) < 4:
                return changed
            miner["last_scan_at"] = now
            target = self._find_miner_target(rcon, miner, target_key, pos)
            miner["target_pos"] = target
            changed = True
            if not target:
                self._miner_notice(rcon, miner, f"can't find {target_key} nearby. Move him into a cave/tunnel or chunkload the area.")
                return changed

        distance = math.sqrt(
            (float(pos["x"]) - (int(target["x"]) + 0.5)) ** 2
            + (float(pos["y"]) - int(target["y"])) ** 2
            + (float(pos["z"]) - (int(target["z"]) + 0.5)) ** 2
        )
        if distance <= MINER_REACH_DISTANCE:
            if self._mine_target_block(rcon, miner, target, target_key):
                miner["target_pos"] = None
                miner["mined"] = int(miner.get("mined") or 0) + 1
                miner["last_action_at"] = time.time()
                changed = True
            return changed

        if self._move_miner_toward(rcon, key, miner, pos, target):
            changed = True
        return changed

    def _ensure_miner_entity(self, rcon, key: str, miner: dict) -> bool:
        if self._read_miner_position(rcon, key):
            return True
        dimension = str(miner.get("dimension") or "minecraft:overworld")
        x = float(miner.get("x") or 0)
        y = float(miner.get("y") or 80)
        z = float(miner.get("z") or 0)
        name = str(miner.get("name") or key)
        tag = self._miner_tag(key)
        custom_name = json.dumps(f"Miner {name}")
        response = rcon.command(
            f"execute in {dimension} run summon villager {x:.2f} {y:.2f} {z:.2f} "
            "{NoAI:1b,PersistenceRequired:1b,Silent:0b,"
            f'Tags:["aios_miner","{tag}"],'
            f"CustomName:'{custom_name}',"
            'VillagerData:{profession:"minecraft:toolsmith",level:2,type:"minecraft:plains"}}'
        )
        return not self._rcon_command_failed(response)

    def _read_miner_position(self, rcon, key: str) -> dict | None:
        response = rcon.command(f"data get entity {self._miner_selector(key)} Pos")
        nums = MinecraftVoiceAssistant._numbers_from_pos_response(response)
        if len(nums) >= 3:
            return {"x": float(nums[0]), "y": float(nums[1]), "z": float(nums[2])}
        return None

    def _ensure_miner_tool(self, rcon, miner: dict) -> bool:
        tool = miner.get("current_tool") if isinstance(miner.get("current_tool"), dict) else None
        if tool and int(tool.get("uses_left") or 0) > 0:
            return True
        for item_id, uses, _tier in MINER_PICKAXES:
            if self._storage_count(rcon, miner, item_id) <= 0:
                continue
            if self._remove_baker_item(rcon, miner, item_id, 1, allow_missing=True):
                miner["current_tool"] = {"item": item_id, "uses_left": uses}
                self._miner_notice(rcon, miner, f"equipped {item_id.replace('minecraft:', '').replace('_', ' ')}.")
                return True
        miner["current_tool"] = None
        return False

    def _use_miner_tool(self, miner: dict, amount: int = 1) -> bool:
        tool = miner.get("current_tool") if isinstance(miner.get("current_tool"), dict) else None
        if not tool:
            return False
        uses_left = int(tool.get("uses_left") or 0)
        if uses_left < amount:
            miner["current_tool"] = None
            return False
        tool["uses_left"] = uses_left - amount
        if tool["uses_left"] <= 0:
            miner["current_tool"] = None
        return True

    def _find_miner_target(self, rcon, miner: dict, target_key: str, pos: dict) -> dict | None:
        dimension = str(miner.get("dimension") or "minecraft:overworld")
        px = math.floor(float(pos["x"]))
        py = math.floor(float(pos["y"]))
        pz = math.floor(float(pos["z"]))
        radius = int(min(max(int(miner.get("range") or MINER_SEARCH_RADIUS), 4), 14))
        candidates = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    distance = dx * dx + dy * dy + dz * dz
                    candidates.append((distance, px + dx, py + dy, pz + dz))
        max_checks = 900 if target_key == "all" else 1400
        for _distance, x, y, z in sorted(candidates)[:max_checks]:
            target = {"dimension": dimension, "x": x, "y": y, "z": z}
            if self._is_target_block(rcon, target, target_key):
                return target
        return None

    def _is_target_block(self, rcon, pos: dict, target_key: str) -> bool:
        for block_id in self._miner_target_blocks(target_key):
            if self._block_is(rcon, pos, block_id):
                pos["block"] = block_id
                return True
        return False

    def _mine_target_block(self, rcon, miner: dict, target: dict, target_key: str) -> bool:
        block_id = str(target.get("block") or "")
        if not block_id:
            self._is_target_block(rcon, target, target_key)
            block_id = str(target.get("block") or "")
        if not block_id:
            return False
        drop_id, amount = self._miner_drop_for_block(block_id)
        if self._storage_capacity(rcon, miner, drop_id) < amount:
            self._miner_notice(rcon, miner, "needs storage space for mined drops.")
            return False
        if not self._use_miner_tool(miner, 1):
            return False
        response = rcon.command(
            f"execute in {target['dimension']} run setblock {int(target['x'])} {int(target['y'])} {int(target['z'])} minecraft:air"
        )
        if self._rcon_command_failed(response):
            return False
        self._add_item_to_baker_storage(rcon, miner, drop_id, amount)
        now = time.time()
        if now - float(miner.get("last_mined_notice_at") or 0) > 30:
            miner["last_mined_notice_at"] = now
            self._miner_announce(rcon, miner, f"mined {drop_id.replace('minecraft:', '').replace('_', ' ')}.")
        return True

    def _move_miner_toward(self, rcon, key: str, miner: dict, pos: dict, target: dict) -> bool:
        dimension = str(miner.get("dimension") or target.get("dimension") or "minecraft:overworld")
        cx = math.floor(float(pos["x"]))
        cy = math.floor(float(pos["y"]))
        cz = math.floor(float(pos["z"]))
        tx = int(target["x"])
        ty = int(target["y"])
        tz = int(target["z"])
        deltas = [("x", tx - cx), ("z", tz - cz), ("y", ty - cy)]
        deltas.sort(key=lambda item: abs(item[1]), reverse=True)
        nx, ny, nz = cx, cy, cz
        for axis, delta in deltas:
            if delta == 0:
                continue
            step = 1 if delta > 0 else -1
            if axis == "x":
                nx = cx + step
            elif axis == "z":
                nz = cz + step
            else:
                ny = cy + step
            break
        feet = {"dimension": dimension, "x": nx, "y": ny, "z": nz}
        head = {"dimension": dimension, "x": nx, "y": ny + 1, "z": nz}
        for block_pos in (feet, head):
            if self._is_miner_passable(rcon, block_pos):
                continue
            if self._is_miner_protected(rcon, block_pos):
                self._miner_notice(rcon, miner, "path is blocked by protected block.")
                return False
            if not self._use_miner_tool(miner, 1):
                return False
            rcon.command(
                f"execute in {dimension} run setblock {int(block_pos['x'])} {int(block_pos['y'])} {int(block_pos['z'])} minecraft:air destroy"
            )
        response = rcon.command(f"execute in {dimension} run tp {self._miner_selector(key)} {nx + 0.5:.2f} {ny:.2f} {nz + 0.5:.2f}")
        if self._rcon_command_failed(response):
            return False
        miner["x"], miner["y"], miner["z"] = nx + 0.5, ny, nz + 0.5
        return True

    def _is_miner_passable(self, rcon, pos: dict) -> bool:
        return any(self._block_is(rcon, pos, block_id) for block_id in MINER_PASSABLE_BLOCKS)

    def _is_miner_protected(self, rcon, pos: dict) -> bool:
        protected = (
            "minecraft:bedrock",
            "minecraft:chest",
            "minecraft:barrel",
            "minecraft:ender_chest",
            "minecraft:shulker_box",
            "minecraft:spawner",
            "minecraft:command_block",
            "minecraft:chain_command_block",
            "minecraft:repeating_command_block",
            "minecraft:lava",
        )
        return any(self._block_is(rcon, pos, block_id) for block_id in protected)

    def _block_is(self, rcon, pos: dict, block_id: str) -> bool:
        response = rcon.command(
            f"execute in {pos['dimension']} if block {int(pos['x'])} {int(pos['y'])} {int(pos['z'])} {block_id} run time query daytime"
        )
        text = str(response or "").strip()
        return bool(text) and not self._rcon_command_failed(text)

    def _miner_target_blocks(self, target_key: str) -> list[str]:
        if target_key == "all":
            blocks: list[str] = []
            for key in MINER_ALL_TARGETS:
                blocks.extend(MINER_TARGETS[key]["blocks"])
            return blocks
        spec = MINER_TARGETS.get(target_key)
        return list(spec["blocks"]) if spec else []

    def _miner_drop_for_block(self, block_id: str) -> tuple[str, int]:
        if block_id == "minecraft:nether_gold_ore":
            return "minecraft:gold_nugget", 4
        for spec in MINER_TARGETS.values():
            if block_id in spec["blocks"]:
                return str(spec["drop"]), 1
        return block_id, 1

    def _storage_count(self, rcon, worker: dict, item_id: str) -> int:
        count = 0
        for container in worker.get("storages", []):
            if not isinstance(container, dict):
                continue
            for item in self._read_container_items(rcon, container):
                if item.get("id") == item_id:
                    count += int(item.get("count") or 0)
        return count

    def _miner_notice(self, rcon, miner: dict, message: str) -> None:
        now = time.time()
        if now - float(miner.get("last_notice_at") or 0) < 45:
            return
        miner["last_notice_at"] = now
        self._miner_announce(rcon, miner, message)

    def _miner_announce(self, rcon, miner: dict, message: str) -> None:
        payload = json.dumps({"text": f"[Miner {miner.get('name', '?')}] {message}", "color": "gray"})
        rcon.command(f"tellraw @a {payload}")

    @staticmethod
    def _normalize_miner_target(target: str) -> str | None:
        text = str(target or "").strip().casefold().replace("minecraft:", "").replace("_ore", "")
        aliases = {
            "everything": "all",
            "ores": "all",
            "diamonds": "diamond",
            "emeralds": "emerald",
            "coal_ore": "coal",
            "iron_ore": "iron",
            "copper_ore": "copper",
            "gold_ore": "gold",
            "redstone_ore": "redstone",
            "lapis_lazuli": "lapis",
            "lapis_ore": "lapis",
            "ancient_debris": "debris",
            "netherite": "debris",
        }
        text = aliases.get(text, text)
        return text if text == "all" or text in MINER_TARGETS else None

    @staticmethod
    def _miner_tag(key: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", key.casefold())[:48]
        return f"aios_miner_{safe}"

    def _miner_selector(self, key: str) -> str:
        return f"@e[type=minecraft:villager,tag={self._miner_tag(key)},limit=1]"

    def _read_miners_unlocked(self) -> dict:
        if not self.miners_path.exists():
            return {}
        try:
            data = json.loads(self.miners_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_miners_unlocked(self, state: dict) -> None:
        temporary = self.miners_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(self.miners_path)

    def _handle_baker_command(self, player: str, message: str) -> bool:
        match = BAKER_RE.match(message)
        if not match:
            return False
        text = re.sub(r"\s+", " ", str(match.group(1) or "").strip())
        if not text or text.casefold() in {"help", "commands"}:
            threading.Thread(target=self._send_baker_help, args=(player,), daemon=True).start()
            return True
        parts = text.split(" ")
        action = parts[0].casefold()
        name = parts[1] if len(parts) > 1 else ""
        if action == "create" and name:
            threading.Thread(target=self._create_baker, args=(player, name), daemon=True).start()
        elif action in {"chest", "storage", "barrel"} and name:
            threading.Thread(target=self._add_baker_storage, args=(player, name), daemon=True).start()
        elif action in {"display", "slot"} and name:
            threading.Thread(target=self._add_baker_display, args=(player, name), daemon=True).start()
        elif action in {"station", "workstation"} and name:
            threading.Thread(target=self._add_baker_station, args=(player, name), daemon=True).start()
        elif action in {"remove", "delete", "del"} and name:
            threading.Thread(target=self._remove_baker, args=(player, name), daemon=True).start()
        elif action in {"status", "info"}:
            threading.Thread(target=self._baker_status, args=(player, name), daemon=True).start()
        elif len(parts) == 1:
            threading.Thread(target=self._baker_status, args=(player, parts[0]), daemon=True).start()
        else:
            threading.Thread(target=self._send_baker_help, args=(player,), daemon=True).start()
        return True

    def _send_baker_help(self, player: str) -> None:
        assistant = self._assistant_for(player)
        for line in [
            "Baker: baker create name | baker chest name | baker display name",
            "Baker: baker station name | baker status name | baker remove name",
            "Stand near a chest/barrel for baker chest. Stand on a display block for baker display.",
        ]:
            try:
                assistant.send_player_message(line, "aqua")
            except Exception:
                pass

    def _create_baker(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with self.baker_lock:
                state = self._read_bakers_unlocked()
                if key in state:
                    assistant.send_player_message(f"Baker {safe_name} already exists.", "yellow")
                    return
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
                if not pos.get("ok"):
                    assistant.send_player_message("Could not read your position.", "red")
                    return
                x = float(pos["x"])
                y = float(pos["y"])
                z = float(pos["z"])
                custom_name = json.dumps({"text": f"Baker {safe_name}", "color": "gold"})
                summon = (
                    f"execute in {dimension} run summon villager {x:.2f} {y:.2f} {z:.2f} "
                    "{NoAI:1b,PersistenceRequired:1b,Silent:0b,"
                    f"CustomName:'{custom_name}',"
                    'VillagerData:{profession:"minecraft:farmer",level:2,type:"minecraft:plains"}}'
                )
                response = rcon.command(summon)
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "summon failed")
            baker = {
                "name": safe_name,
                "dimension": dimension,
                "x": x,
                "y": y,
                "z": z,
                "created_by": player,
                "created_at": time.time(),
                "storages": [],
                "displays": [],
                "station": None,
                "job": None,
                "last_notice_at": 0,
            }
            with self.baker_lock:
                state = self._read_bakers_unlocked()
                if key in state:
                    assistant.send_player_message(f"Baker {safe_name} already exists.", "yellow")
                    return
                state[key] = baker
                self._write_bakers_unlocked(state)
            assistant.send_chat_message(f"Baker {safe_name} created. Add storage with: baker chest {safe_name}", "all", "green")
        except Exception as exc:
            assistant.send_player_message(f"Baker create failed: {exc}", "red")

    def _add_baker_storage(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
                if not pos.get("ok"):
                    assistant.send_player_message("Could not read your position.", "red")
                    return
                container = self._find_nearby_container(rcon, dimension, pos)
            if not container:
                assistant.send_player_message("No chest or barrel found within 4 blocks.", "yellow")
                return
            with self.baker_lock:
                state = self._read_bakers_unlocked()
                baker = state.get(key)
                if not isinstance(baker, dict):
                    assistant.send_player_message(f"No baker named {safe_name}.", "yellow")
                    return
                storages = baker.setdefault("storages", [])
                if any(self._same_block(entry, container) for entry in storages if isinstance(entry, dict)):
                    assistant.send_player_message("That storage is already registered.", "yellow")
                    return
                storages.append(container)
                self._write_bakers_unlocked(state)
            assistant.send_player_message(
                f"Added {container['type'].replace('minecraft:', '')} at {container['x']} {container['y']} {container['z']} to {safe_name}.",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Baker chest failed: {exc}", "red")

    def _add_baker_display(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
            if not pos.get("ok"):
                assistant.send_player_message("Could not read your position.", "red")
                return
            display = {
                "dimension": dimension,
                "x": math.floor(float(pos["x"])),
                "y": math.floor(float(pos["y"])),
                "z": math.floor(float(pos["z"])),
            }
            with self.baker_lock:
                state = self._read_bakers_unlocked()
                baker = state.get(key)
                if not isinstance(baker, dict):
                    assistant.send_player_message(f"No baker named {safe_name}.", "yellow")
                    return
                displays = baker.setdefault("displays", [])
                if any(self._same_block(entry, display) for entry in displays if isinstance(entry, dict)):
                    assistant.send_player_message("That display slot is already registered.", "yellow")
                    return
                displays.append(display)
                self._write_bakers_unlocked(state)
            assistant.send_player_message(f"Added display slot at {display['x']} {display['y']} {display['z']}.", "green")
        except Exception as exc:
            assistant.send_player_message(f"Baker display failed: {exc}", "red")

    def _add_baker_station(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
                if not pos.get("ok"):
                    assistant.send_player_message("Could not read your position.", "red")
                    return
                station = self._find_nearby_station(rcon, dimension, pos)
            if not station:
                assistant.send_player_message("No smoker, furnace, crafting table, or campfire found within 4 blocks.", "yellow")
                return
            with self.baker_lock:
                state = self._read_bakers_unlocked()
                baker = state.get(key)
                if not isinstance(baker, dict):
                    assistant.send_player_message(f"No baker named {safe_name}.", "yellow")
                    return
                baker["station"] = station
                self._write_bakers_unlocked(state)
            assistant.send_player_message(
                f"Added workstation {station['type'].replace('minecraft:', '')} at {station['x']} {station['y']} {station['z']}.",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Baker station failed: {exc}", "red")

    def _remove_baker(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        with self.baker_lock:
            state = self._read_bakers_unlocked()
            removed = state.pop(safe_name.casefold(), None)
            self._write_bakers_unlocked(state)
        if removed:
            assistant.send_chat_message(f"Baker {safe_name} removed. The villager entity was left in-world.", "all", "yellow")
        else:
            assistant.send_player_message(f"No baker named {safe_name}.", "yellow")

    def _baker_status(self, player: str, name: str = "") -> None:
        assistant = self._assistant_for(player)
        with self.baker_lock:
            state = self._read_bakers_unlocked()
            bakers = dict(state)
        if not bakers:
            assistant.send_player_message("No bakers yet. Use: baker create name", "yellow")
            return
        if not name:
            names = ", ".join(sorted(str(b.get("name") or key) for key, b in bakers.items() if isinstance(b, dict)))
            assistant.send_player_message(f"Bakers: {names}", "aqua")
            return
        baker = bakers.get(self._safe_wp_name(name).casefold())
        if not isinstance(baker, dict):
            assistant.send_player_message(f"No baker named {name}.", "yellow")
            return
        job = baker.get("job") if isinstance(baker.get("job"), dict) else None
        if job:
            remaining = max(0, int(float(job.get("finish_at", 0)) - time.time()))
            job_text = f"baking cake, {remaining}s left"
        else:
            missing = self._baker_missing_summary(baker)
            job_text = "ready" if not missing else "needs " + missing
        assistant.send_player_message(
            f"{baker.get('name', name)}: {len(baker.get('storages', []))} storage, {len(baker.get('displays', []))} display, "
            f"{'station set' if baker.get('station') else 'no station'}, {job_text}.",
            "aqua",
        )

    def _baker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick_bakers()
            except Exception as exc:
                bridge_log(f"baker loop failed: {exc}")
            self.stop_event.wait(10.0)

    def _tick_bakers(self) -> None:
        with self.baker_lock:
            state = self._read_bakers_unlocked()
            bakers = [(key, baker) for key, baker in state.items() if isinstance(baker, dict)]
        if not bakers:
            return
        assistant = self._assistant_for("drwormbat")
        changed = False
        with assistant._connect() as rcon:
            for key, baker in bakers:
                try:
                    if self._process_baker(rcon, baker):
                        with self.baker_lock:
                            state = self._read_bakers_unlocked()
                            state[key] = baker
                            self._write_bakers_unlocked(state)
                        changed = True
                except Exception as exc:
                    bridge_log(f"baker {baker.get('name', key)} failed: {exc}")
        if changed:
            bridge_log("baker state updated")

    def _process_baker(self, rcon, baker: dict) -> bool:
        changed = False
        now = time.time()
        changed = self._refill_baker_displays(rcon, baker) or changed
        job = baker.get("job") if isinstance(baker.get("job"), dict) else None
        if job and now >= float(job.get("finish_at") or 0):
            if self._place_baker_output(rcon, baker, "minecraft:cake", 1):
                baker["job"] = None
                self._baker_announce(rcon, baker, f"Baker {baker.get('name')} finished a cake.")
                changed = True
            return changed
        if job:
            return changed
        if not baker.get("storages"):
            return changed
        counts = self._baker_counts(rcon, baker)
        missing = {item: needed - counts.get(item, 0) for item, needed in CAKE_RECIPE.items() if counts.get(item, 0) < needed}
        if missing:
            if now - float(baker.get("last_notice_at") or 0) > 180:
                baker["last_notice_at"] = now
                self._baker_announce(rcon, baker, f"Baker {baker.get('name')} needs {self._format_missing(missing)} for cake.")
                changed = True
            return changed
        if not self._baker_has_output_capacity(rcon, baker):
            if now - float(baker.get("last_notice_at") or 0) > 180:
                baker["last_notice_at"] = now
                self._baker_announce(rcon, baker, f"Baker {baker.get('name')} needs an empty display or storage slot.")
                changed = True
            return changed
        for item, count in CAKE_RECIPE.items():
            self._remove_baker_item(rcon, baker, item, count)
        self._add_item_to_baker_storage(rcon, baker, "minecraft:bucket", 3)
        baker["job"] = {"type": "cake", "started_at": now, "finish_at": now + BAKER_JOB_SECONDS}
        baker["last_notice_at"] = now
        self._baker_announce(rcon, baker, f"Baker {baker.get('name')} started a cake. Ready in 3 minutes.")
        return True

    def _refill_baker_displays(self, rcon, baker: dict) -> bool:
        changed = False
        for display in baker.get("displays", []):
            if not isinstance(display, dict) or not self._display_is_air(rcon, display):
                continue
            if not self._remove_baker_item(rcon, baker, "minecraft:cake", 1, allow_missing=True):
                continue
            if self._set_display_cake(rcon, display):
                changed = True
            else:
                self._add_item_to_baker_storage(rcon, baker, "minecraft:cake", 1)
        return changed

    def _place_baker_output(self, rcon, baker: dict, item: str, count: int) -> bool:
        if item == "minecraft:cake":
            for display in baker.get("displays", []):
                if isinstance(display, dict) and self._display_is_air(rcon, display) and self._set_display_cake(rcon, display):
                    return True
        return self._add_item_to_baker_storage(rcon, baker, item, count)

    def _baker_counts(self, rcon, baker: dict) -> dict[str, int]:
        counts: dict[str, int] = {}
        for container in baker.get("storages", []):
            if not isinstance(container, dict):
                continue
            for item in self._read_container_items(rcon, container):
                item_id = str(item.get("id") or "")
                counts[item_id] = counts.get(item_id, 0) + int(item.get("count") or 0)
        return counts

    def _baker_missing_summary(self, baker: dict) -> str:
        try:
            assistant = self._assistant_for("drwormbat")
            with assistant._connect() as rcon:
                counts = self._baker_counts(rcon, baker)
        except Exception:
            return "storage check"
        missing = {item: needed - counts.get(item, 0) for item, needed in CAKE_RECIPE.items() if counts.get(item, 0) < needed}
        return self._format_missing(missing) if missing else ""

    @staticmethod
    def _format_missing(missing: dict[str, int]) -> str:
        labels = {
            "minecraft:milk_bucket": "milk bucket",
            "minecraft:sugar": "sugar",
            "minecraft:egg": "egg",
            "minecraft:wheat": "wheat",
        }
        return ", ".join(f"{amount} {labels.get(item, item.replace('minecraft:', ''))}" for item, amount in missing.items() if amount > 0)

    def _baker_has_output_capacity(self, rcon, baker: dict) -> bool:
        if any(isinstance(display, dict) and self._display_is_air(rcon, display) for display in baker.get("displays", [])):
            return True
        return self._can_add_to_baker_storage(rcon, baker, "minecraft:cake", 1)

    def _display_is_air(self, rcon, display: dict) -> bool:
        response = rcon.command(
            f"execute in {display['dimension']} if block {int(display['x'])} {int(display['y'])} {int(display['z'])} minecraft:air run time query daytime"
        )
        return bool(str(response or "").strip())

    def _set_display_cake(self, rcon, display: dict) -> bool:
        response = rcon.command(
            f"execute in {display['dimension']} if block {int(display['x'])} {int(display['y'])} {int(display['z'])} minecraft:air "
            f"run setblock {int(display['x'])} {int(display['y'])} {int(display['z'])} minecraft:cake"
        )
        return bool(str(response or "").strip()) and not self._rcon_command_failed(response)

    def _baker_announce(self, rcon, baker: dict, message: str) -> None:
        payload = json.dumps({"text": "[Baker] " + message, "color": "gold"})
        rcon.command(f"tellraw @a {payload}")

    def _find_nearby_container(self, rcon, dimension: str, pos: dict) -> dict | None:
        px = math.floor(float(pos["x"]))
        py = math.floor(float(pos["y"]))
        pz = math.floor(float(pos["z"]))
        candidates = []
        for dx in range(-4, 5):
            for dy in range(-2, 3):
                for dz in range(-4, 5):
                    candidates.append((dx * dx + dy * dy + dz * dz, px + dx, py + dy, pz + dz))
        for _distance, x, y, z in sorted(candidates):
            for block_type in ("minecraft:barrel", "minecraft:chest"):
                response = rcon.command(f"execute in {dimension} if block {x} {y} {z} {block_type} run data get block {x} {y} {z} Items")
                if str(response or "").strip():
                    return {"dimension": dimension, "x": x, "y": y, "z": z, "type": block_type}
        return None

    def _find_nearby_station(self, rcon, dimension: str, pos: dict) -> dict | None:
        px = math.floor(float(pos["x"]))
        py = math.floor(float(pos["y"]))
        pz = math.floor(float(pos["z"]))
        station_blocks = ("minecraft:smoker", "minecraft:furnace", "minecraft:crafting_table", "minecraft:campfire")
        candidates = []
        for dx in range(-4, 5):
            for dy in range(-2, 3):
                for dz in range(-4, 5):
                    candidates.append((dx * dx + dy * dy + dz * dz, px + dx, py + dy, pz + dz))
        for _distance, x, y, z in sorted(candidates):
            for block_type in station_blocks:
                response = rcon.command(f"execute in {dimension} if block {x} {y} {z} {block_type} run time query daytime")
                if str(response or "").strip():
                    return {"dimension": dimension, "x": x, "y": y, "z": z, "type": block_type}
        return None

    def _read_container_items(self, rcon, container: dict) -> list[dict]:
        response = rcon.command(
            f"execute in {container['dimension']} run data get block "
            f"{int(container['x'])} {int(container['y'])} {int(container['z'])} Items"
        )
        items = []
        for entry in re.finditer(r"\{([^{}]*)\}", str(response or "")):
            text = entry.group(1)
            slot_match = re.search(r"Slot:\s*(\d+)b?", text)
            id_match = re.search(r'id:\s*"([^"]+)"', text)
            count_match = re.search(r"(?:count|Count):\s*(\d+)b?", text)
            if not slot_match or not id_match:
                continue
            items.append(
                {
                    "slot": int(slot_match.group(1)),
                    "id": id_match.group(1),
                    "count": int(count_match.group(1)) if count_match else 1,
                    "container": container,
                }
            )
        return items

    def _remove_baker_item(self, rcon, baker: dict, item_id: str, count: int, allow_missing: bool = False) -> bool:
        remaining = int(count)
        slots = []
        for container in baker.get("storages", []):
            if not isinstance(container, dict):
                continue
            for item in self._read_container_items(rcon, container):
                if item.get("id") == item_id:
                    slots.append(item)
        if sum(int(item.get("count") or 0) for item in slots) < remaining:
            if allow_missing:
                return False
            raise RuntimeError(f"missing {count} {item_id}")
        for item in slots:
            if remaining <= 0:
                break
            take = min(remaining, int(item.get("count") or 0))
            left = int(item.get("count") or 0) - take
            self._replace_container_slot(rcon, item["container"], int(item["slot"]), item_id if left else "minecraft:air", left)
            remaining -= take
        return True

    def _can_add_to_baker_storage(self, rcon, baker: dict, item_id: str, count: int) -> bool:
        return self._storage_capacity(rcon, baker, item_id) >= int(count)

    def _storage_capacity(self, rcon, baker: dict, item_id: str) -> int:
        max_stack = self._stack_limit(item_id)
        capacity = 0
        for container in baker.get("storages", []):
            if not isinstance(container, dict):
                continue
            items = self._read_container_items(rcon, container)
            occupied = {int(item["slot"]) for item in items}
            for item in items:
                if item.get("id") == item_id:
                    capacity += max(0, max_stack - int(item.get("count") or 0))
            capacity += (27 - len(occupied)) * max_stack
        return capacity

    def _add_item_to_baker_storage(self, rcon, baker: dict, item_id: str, count: int) -> bool:
        remaining = int(count)
        max_stack = self._stack_limit(item_id)
        for container in baker.get("storages", []):
            if not isinstance(container, dict):
                continue
            items = self._read_container_items(rcon, container)
            for item in items:
                if remaining <= 0:
                    return True
                if item.get("id") != item_id:
                    continue
                current = int(item.get("count") or 0)
                add = min(remaining, max_stack - current)
                if add <= 0:
                    continue
                self._replace_container_slot(rcon, container, int(item["slot"]), item_id, current + add)
                remaining -= add
            occupied = {int(item["slot"]) for item in items}
            for slot in range(27):
                if remaining <= 0:
                    return True
                if slot in occupied:
                    continue
                add = min(remaining, max_stack)
                self._replace_container_slot(rcon, container, slot, item_id, add)
                remaining -= add
        return remaining <= 0

    def _replace_container_slot(self, rcon, container: dict, slot: int, item_id: str, count: int) -> None:
        pos = f"{int(container['x'])} {int(container['y'])} {int(container['z'])}"
        if item_id == "minecraft:air" or count <= 0:
            response = rcon.command(f"execute in {container['dimension']} run item replace block {pos} container.{slot} with minecraft:air")
        else:
            response = rcon.command(f"execute in {container['dimension']} run item replace block {pos} container.{slot} with {item_id} {int(count)}")
        if self._rcon_command_failed(response):
            raise RuntimeError(response.strip() or f"could not update container slot {slot}")

    @staticmethod
    def _stack_limit(item_id: str) -> int:
        if str(item_id).endswith("_pickaxe"):
            return 1
        return int(STACK_LIMITS.get(item_id, 64))

    @staticmethod
    def _same_block(left: dict, right: dict) -> bool:
        return (
            str(left.get("dimension")) == str(right.get("dimension"))
            and int(left.get("x")) == int(right.get("x"))
            and int(left.get("y")) == int(right.get("y"))
            and int(left.get("z")) == int(right.get("z"))
        )

    def _read_bakers_unlocked(self) -> dict:
        if not self.bakers_path.exists():
            return {}
        try:
            data = json.loads(self.bakers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_bakers_unlocked(self, state: dict) -> None:
        temporary = self.bakers_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(self.bakers_path)

    def _where_player(self, requester: str, requested_player: str) -> None:
        assistant = self._assistant_for(requester)
        try:
            with assistant._connect() as rcon:
                target = self._resolve_online_player(rcon, requested_player)
                if not target:
                    assistant.send_player_message(f"Player {requested_player} is not online.", "yellow")
                    return
                pos = assistant._read_player_position(rcon, target)
                dimension = self._read_player_dimension(rcon, target)
            if not pos.get("ok"):
                assistant.send_player_message(f"Could not read {target}'s position.", "red")
                return
            nearest = self._closest_waypoint(pos, dimension)
            coords = f"{float(pos['x']):.1f} {float(pos['y']):.1f} {float(pos['z']):.1f}"
            dimension_name = dimension.replace("minecraft:", "")
            if nearest:
                waypoint, distance = nearest
                detail = f"Closest waypoint: {waypoint.get('name', '?')} ({distance:.0f} blocks)."
            else:
                detail = "No waypoint exists in that dimension."
            assistant.send_player_message(f"{target}: {coords} ({dimension_name}). {detail}", "aqua")
        except Exception as exc:
            assistant.send_player_message(f"Where failed: {exc}", "red")

    def _teleport_player_or_waypoint(self, requester: str, target_name: str) -> None:
        assistant = self._assistant_for(requester)
        try:
            with assistant._connect() as rcon:
                online_target = self._resolve_online_player(rcon, target_name)
                if online_target:
                    if online_target.casefold() == requester.casefold():
                        assistant.send_player_message("You are already there.", "yellow")
                        return
                    response = rcon.command(f"tp {requester} {online_target}")
                else:
                    response = ""
            if not online_target:
                self._teleport_waypoint(requester, target_name)
                return
            if "No entity" in response or "not found" in response.casefold():
                assistant.send_player_message(f"Could not teleport to {online_target}.", "red")
                return
            assistant.send_player_message(f"Teleported to {online_target}.", "green")
        except Exception as exc:
            assistant.send_player_message(f"Teleport failed: {exc}", "red")

    def _back_to_death(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                response = rcon.command(f"data get entity {player} LastDeathLocation")
                death = self._parse_last_death_location(response)
                if not death:
                    assistant.send_player_message("No last death location was found.", "yellow")
                    return
                rcon.command(
                    f"execute in {death['dimension']} run tp {player} "
                    f"{death['x'] + 0.5:.1f} {death['y'] + 0.1:.1f} {death['z'] + 0.5:.1f}"
                )
            assistant.send_player_message(
                f"Returned to death location: {death['x']} {death['y']} {death['z']}.",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Back failed: {exc}", "red")

    def _teleport_top(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                dimension = self._read_player_dimension(rcon, player)
                if dimension == "minecraft:the_nether":
                    assistant.send_player_message("Top is disabled in the Nether to avoid trapping you on the roof.", "yellow")
                    return
                for dx, dz in self._top_search_offsets(3):
                    prefix = self._top_safe_execute_prefix(player, dx, dz)
                    response = rcon.command(prefix + " run tp @s ~ ~ ~")
                    if "Teleported" not in response:
                        continue
                    pos = assistant._read_player_position(rcon, player)
                    if pos.get("ok"):
                        coords = f"{float(pos['x']):.1f} {float(pos['y']):.1f} {float(pos['z']):.1f}"
                        assistant.send_player_message(f"Teleported to the highest safe block at {coords}.", "green")
                    else:
                        assistant.send_player_message("Teleported to the highest safe block.", "green")
                    return
            assistant.send_player_message("No safe surface block was found within 3 blocks.", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Top failed: {exc}", "red")

    @staticmethod
    def _top_search_offsets(radius: int) -> list[tuple[int, int]]:
        offsets = [
            (dx, dz)
            for dx in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
        ]
        return sorted(offsets, key=lambda point: (point[0] ** 2 + point[1] ** 2, abs(point[0]), abs(point[1])))

    @staticmethod
    def _top_safe_execute_prefix(player: str, dx: int, dz: int) -> str:
        rel_x = "~" if dx == 0 else f"~{dx}"
        rel_z = "~" if dz == 0 else f"~{dz}"
        parts = [
            f"execute as {player} at @s positioned {rel_x} ~ {rel_z}",
            "positioned over motion_blocking_no_leaves",
            "if block ~ ~ ~ minecraft:air",
            "if block ~ ~1 ~ minecraft:air",
            "unless block ~ ~-1 ~ minecraft:air",
        ]
        parts.extend(f"unless block ~ ~-1 ~ minecraft:{block}" for block in UNSAFE_TOP_BLOCKS)
        return " ".join(parts)

    def _eat_best_food(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                food_level = self._read_food_level(rcon, player)
                if food_level is None:
                    assistant.send_player_message("Could not read your hunger level.", "red")
                    return
                missing = max(0, 20 - food_level)
                if missing == 0:
                    assistant.send_player_message("Your hunger bar is already full.", "yellow")
                    return
                available = []
                for food in SAFE_FOODS:
                    count = assistant._count_item(rcon, food["item"])
                    if count > 0:
                        available.append({**food, "count": count})
                choice = self._best_food_combination(available, missing)
                if not choice:
                    if available:
                        message = f"You have food, but none fits the {missing} missing hunger without waste."
                    else:
                        message = "No safe everyday food found. Golden, harmful, teleporting, and mystery foods are preserved."
                    assistant.send_player_message(message, "yellow")
                    return
                for food, count in choice:
                    response = rcon.command(f"clear {player} {food['item']} {count}")
                    if "No items were found" in response:
                        raise RuntimeError(f"could not consume {food['label']}")
                restored = sum(int(food["nutrition"]) * count for food, count in choice)
                rcon.command(f"effect give {player} saturation 1 {restored - 1} true")
            eaten = ", ".join(
                f"{count} {food['label']}" if count > 1 else food["label"]
                for food, count in choice
            )
            remaining = max(0, missing - restored)
            suffix = f" {remaining} hunger remains; nothing else fit without waste." if remaining else " Hunger is full."
            assistant.send_player_message(f"Ate {eaten}. Restored {restored} hunger.{suffix}", "green")
        except Exception as exc:
            assistant.send_player_message(f"Eat failed: {exc}", "red")

    @staticmethod
    def _read_food_level(rcon, player: str) -> int | None:
        response = rcon.command(f"data get entity {player} foodLevel")
        match = re.search(r"entity data:\s*(-?\d+)", response)
        return max(0, min(20, int(match.group(1)))) if match else None

    @staticmethod
    def _best_food_combination(available: list[dict], missing: int) -> list[tuple[dict, int]]:
        limit = max(0, missing)
        states: dict[int, tuple[int, int, list[tuple[dict, int]]]] = {0: (0, 0, [])}
        for food in available:
            max_count = min(int(food.get("count") or 0), limit)
            nutrition = int(food["nutrition"])
            priority = int(food.get("priority") or 0)
            previous = dict(states)
            for total, (items, cost, combo) in previous.items():
                for count in range(1, max_count + 1):
                    new_total = total + nutrition * count
                    if new_total > limit:
                        break
                    candidate = (items + count, cost + priority * count, combo + [(food, count)])
                    current = states.get(new_total)
                    if current is None or candidate[:2] < current[:2]:
                        states[new_total] = candidate
        best_total = max((total for total in states if total > 0), default=0)
        return states[best_total][2] if best_total else []

    def _handle_time_command(self, player: str, message: str) -> bool:
        if DAY_RE.match(message):
            threading.Thread(target=self._set_time_and_clear, args=(player, "day", 0), daemon=True).start()
            return True
        if NIGHT_RE.match(message):
            threading.Thread(target=self._set_time_and_clear, args=(player, "night", 13000), daemon=True).start()
            return True
        return False

    def _set_time_and_clear(self, player: str, label: str, ticks: int) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                rcon.command(f"time set {ticks}")
                rcon.command("weather clear")
            assistant.send_chat_message(f"{player} set {label} with clear weather.", "all", "green")
        except Exception as exc:
            assistant.send_player_message(f"Could not set {label}: {exc}", "red")

    def _run_request(self, player: str, request: str) -> None:
        assistant = self._assistant_for(player)
        lock = self._lock_for(player)
        if not lock.acquire(blocking=False):
            try:
                assistant.send_chat_message(f"{player}: I am still working on your previous request.", "all", "yellow")
            except Exception:
                pass
            return
        try:
            bridge_log(f"{player} -> {request[:140]}")
            previous_suppression = assistant.suppress_tool_messages
            assistant.suppress_tool_messages = True
            try:
                result = assistant.handle(request)
            finally:
                assistant.suppress_tool_messages = previous_suppression
            reply = str(result.get("reply") or "").strip() if isinstance(result, dict) else ""
            bridge_log(f"{player} <- {reply[:220]}")
            if reply and not self._public_tool_already_answered(assistant, reply):
                try:
                    self._send_public_ai_reply(assistant, player, reply)
                except Exception:
                    pass
        finally:
            lock.release()

    @staticmethod
    def _public_tool_already_answered(assistant: MinecraftVoiceAssistant, reply: str) -> bool:
        text = re.sub(r"[\s.!\-]+", " ", str(reply or "").strip().casefold()).strip()
        receipt = text in {
            "sent the message",
            "shared the shopping list",
            "shared the list",
            "done",
            "ok",
            "okay",
        }
        if not receipt:
            return False
        for entry in reversed(getattr(assistant, "last_tool_results", []) or []):
            tool = entry.get("tool")
            result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
            if tool in {"send_chat_message", "share_shopping_list"} and result.get("audience") == "all":
                return True
        return False

    def _send_public_ai_reply(self, assistant: MinecraftVoiceAssistant, player: str, reply: str) -> None:
        message = self._format_reply_message(reply)
        if not message:
            return
        assistant.send_chat_message(f"{player}: {message}", "all", "green")

    @staticmethod
    def _format_reply_message(reply: str) -> str:
        text = re.sub(r"\s+", " ", str(reply or "").strip())
        if not text:
            return ""
        max_len = 220
        if len(text) <= max_len:
            return text
        truncated = text[:max_len].rsplit(" ", 1)[0].strip()
        return (truncated or text[:max_len]).rstrip(".,;:") + "..."

    @staticmethod
    def _split_chat_text(text: str, max_len: int) -> list[str]:
        words = re.sub(r"\s+", " ", str(text or "").strip()).split()
        chunks: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_len:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = word[:max_len]
        if current:
            chunks.append(current)
        return chunks or [str(text or "")[:max_len]]

    def _handle_waypoint_command(self, player: str, message: str) -> bool:
        set_match = SET_WP_RE.match(message)
        if set_match:
            name = set_match.group(1)
            threading.Thread(target=self._set_waypoint, args=(player, name), daemon=True).start()
            return True
        del_match = DEL_WP_RE.match(message)
        if del_match:
            name = del_match.group(1)
            threading.Thread(target=self._delete_waypoint, args=(player, name), daemon=True).start()
            return True
        if LIST_WP_RE.match(message):
            threading.Thread(target=self._list_waypoints, args=(player,), daemon=True).start()
            return True
        return False

    def _handle_build_command(self, player: str, message: str) -> bool:
        match = BUILD_RE.match(message)
        if not match:
            return False
        command = re.sub(r"\s+", " ", match.group(1).strip())
        lowered = command.casefold()
        if lowered in {"help", "?", "commands"}:
            threading.Thread(target=self._send_build_help, args=(player,), daemon=True).start()
        elif lowered in {"wand", "stick"}:
            threading.Thread(target=self._give_builder_stick, args=(player,), daemon=True).start()
        elif lowered in {"pos1", "1", "set1"}:
            threading.Thread(target=self._set_build_point, args=(player, "pos1"), daemon=True).start()
        elif lowered in {"pos2", "2", "set2"}:
            threading.Thread(target=self._set_build_point, args=(player, "pos2"), daemon=True).start()
        elif lowered in {"cancel", "clear points", "reset"}:
            threading.Thread(target=self._clear_build_points, args=(player,), daemon=True).start()
        elif lowered in {"fill", "place", "build"} or lowered.startswith(("fill ", "place ", "build ")):
            block = command.split(" ", 1)[1].strip() if " " in command else ""
            threading.Thread(target=self._fill_build_region, args=(player, block or None), daemon=True).start()
        elif lowered in {"clear", "air", "erase"}:
            threading.Thread(target=self._fill_build_region, args=(player, "minecraft:air"), daemon=True).start()
        elif lowered in {"status", "size"}:
            threading.Thread(target=self._build_status, args=(player,), daemon=True).start()
        else:
            threading.Thread(target=self._send_build_help, args=(player,), daemon=True).start()
        return True

    def _send_build_help(self, player: str) -> None:
        assistant = self._assistant_for(player)
        lines = [
            "Build tools: bt pos1 | bt pos2 | bt fill [block] | bt clear | bt status | bt cancel",
            f"Safety: max {MAX_BUILD_BLOCKS} blocks. If [block] is omitted, hotbar slot 1 is used.",
            "Example: put dirt in hotbar slot 1, stand at corner A: bt pos1, corner B: bt pos2, then bt fill.",
        ]
        for line in lines:
            try:
                assistant.send_player_message(line, "aqua")
            except Exception:
                pass

    def _give_builder_stick(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                rcon.command(f"give {player} minecraft:stick 1")
            assistant.send_player_message("Builder stick given. Vanilla cannot detect stick clicks; use bt pos1/pos2/fill.", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Builder stick failed: {exc}", "red")

    def _set_build_point(self, player: str, point_name: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
            if not pos.get("ok"):
                assistant.send_player_message(f"Could not read position for {point_name}.", "red")
                return
            point = {
                "x": math.floor(float(pos["x"])),
                "y": math.floor(float(pos["y"])),
                "z": math.floor(float(pos["z"])),
                "dimension": dimension,
                "updated_at": time.time(),
            }
            state = self._load_buildtools()
            player_state = state.setdefault(player.casefold(), {"player": player})
            player_state[point_name] = point
            self._save_buildtools(state)
            assistant.send_player_message(
                f"{point_name} set to {point['x']} {point['y']} {point['z']} ({dimension}).",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Build point failed: {exc}", "red")

    def _clear_build_points(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            state = self._load_buildtools()
            state.pop(player.casefold(), None)
            self._save_buildtools(state)
            assistant.send_player_message("Build points cleared.", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Could not clear build points: {exc}", "red")

    def _build_status(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            points = self._player_build_points(player)
            if not points:
                assistant.send_player_message("No build points set. Use bt pos1 and bt pos2.", "yellow")
                return
            assistant.send_player_message(self._format_build_status(points), "aqua")
        except Exception as exc:
            assistant.send_player_message(f"Build status failed: {exc}", "red")

    def _fill_build_region(self, player: str, block: str | None) -> None:
        assistant = self._assistant_for(player)
        try:
            points = self._player_build_points(player)
            if not points or "pos1" not in points or "pos2" not in points:
                assistant.send_player_message("Set both points first: bt pos1 and bt pos2.", "yellow")
                return
            p1 = points["pos1"]
            p2 = points["pos2"]
            if p1.get("dimension") != p2.get("dimension"):
                assistant.send_player_message("pos1 and pos2 are in different dimensions.", "red")
                return
            volume = self._region_volume(p1, p2)
            if volume > MAX_BUILD_BLOCKS:
                assistant.send_player_message(f"Region is {volume} blocks; max is {MAX_BUILD_BLOCKS}.", "red")
                return
            block_id = self._safe_block_id(block) if block else None
            with assistant._connect() as rcon:
                if block_id is None:
                    block_id = self._hotbar_slot_zero_item(rcon, player)
                if block_id != "minecraft:air":
                    available = assistant._count_item(rcon, block_id)
                    if available < volume:
                        assistant.send_player_message(
                            f"Need {volume} {block_id}; you have {available}.",
                            "yellow",
                        )
                        return
                dimension = str(p1["dimension"])
                cmd = (
                    f"execute in {dimension} run fill "
                    f"{int(p1['x'])} {int(p1['y'])} {int(p1['z'])} "
                    f"{int(p2['x'])} {int(p2['y'])} {int(p2['z'])} {block_id}"
                )
                response = rcon.command(cmd)
            if "Expected block" in response or "argument.block" in response or "Unknown" in response:
                assistant.send_player_message(f"Fill failed: {block_id} is not a valid block.", "red")
                return
            if block_id != "minecraft:air":
                with assistant._connect() as rcon:
                    rcon.command(f"clear {player} {block_id} {volume}")
            assistant.send_player_message(f"Filled {volume} blocks with {block_id}.", "green")
        except Exception as exc:
            assistant.send_player_message(f"Build fill failed: {exc}", "red")

    def _player_build_points(self, player: str) -> dict | None:
        state = self._load_buildtools()
        data = state.get(player.casefold())
        return data if isinstance(data, dict) else None

    @staticmethod
    def _region_volume(p1: dict, p2: dict) -> int:
        return (
            abs(int(p1["x"]) - int(p2["x"])) + 1
        ) * (
            abs(int(p1["y"]) - int(p2["y"])) + 1
        ) * (
            abs(int(p1["z"]) - int(p2["z"])) + 1
        )

    @staticmethod
    def _format_build_status(points: dict) -> str:
        pos1 = points.get("pos1")
        pos2 = points.get("pos2")
        if pos1 and pos2:
            volume = MinecraftChatBridge._region_volume(pos1, pos2)
            return (
                f"pos1 {pos1['x']} {pos1['y']} {pos1['z']} | "
                f"pos2 {pos2['x']} {pos2['y']} {pos2['z']} | {volume} blocks"
            )
        if pos1:
            return f"pos1 {pos1['x']} {pos1['y']} {pos1['z']} set. Need pos2."
        if pos2:
            return f"pos2 {pos2['x']} {pos2['y']} {pos2['z']} set. Need pos1."
        return "No build points set."

    @staticmethod
    def _safe_block_id(block: str) -> str:
        text = str(block or "").strip().lower().replace(" ", "_")
        if not text:
            raise ValueError("block is empty")
        if not re.fullmatch(r"[a-z0-9_.:-]{1,80}", text):
            raise ValueError("block names can only use letters, numbers, _, ., :, and -")
        return text if ":" in text else f"minecraft:{text}"

    def _hotbar_slot_zero_item(self, rcon, player: str) -> str:
        response = rcon.command(f"data get entity {player} Inventory")
        match = re.search(r'Slot:\s*0b,\s*id:\s*"([^"]+)"', response)
        if not match:
            raise ValueError("hotbar slot 1 is empty; use bt fill dirt or put a block in slot 1")
        return match.group(1)

    def _load_buildtools(self) -> dict:
        with self.buildtools_lock:
            if not self.buildtools_path.exists():
                return {}
            try:
                data = json.loads(self.buildtools_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

    def _save_buildtools(self, state: dict) -> None:
        with self.buildtools_lock:
            self.buildtools_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _set_spawner_keeper(self, player: str, name: str, mob: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        mob_id = self._safe_spawner_mob(mob)
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
            if not pos.get("ok"):
                assistant.send_player_message("Could not read your current position.", "red")
                return
            x = float(pos["x"])
            y = float(pos["y"])
            z = float(pos["z"])
            block_x = math.floor(x)
            block_z = math.floor(z)
            radius = SPAWNER_DEFAULT_RADIUS
            min_x = block_x - radius
            min_z = block_z - radius
            max_x = block_x + radius
            max_z = block_z + radius
            with self.spawner_lock:
                spawners = self._read_spawners_unlocked()
                existing = spawners.get(key)
                if existing is not None:
                    existing_name = str(existing.get("name") or safe_name) if isinstance(existing, dict) else safe_name
                    assistant.send_player_message(f"Spawner keeper {existing_name} already exists. Delete it first.", "yellow")
                    return
                with assistant._connect() as rcon:
                    response = rcon.command(f"execute in {dimension} run forceload add {min_x} {min_z} {max_x} {max_z}")
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "server rejected forceload")
                spawners[key] = {
                    "name": safe_name,
                    "mob": mob_id,
                    "x": x,
                    "y": y,
                    "z": z,
                    "block_x": block_x,
                    "block_z": block_z,
                    "radius": radius,
                    "dimension": dimension,
                    "set_by": player,
                    "created_at": time.time(),
                    "enabled": True,
                }
                try:
                    self._write_spawners_unlocked(spawners)
                except Exception:
                    with assistant._connect() as rcon:
                        rcon.command(f"execute in {dimension} run forceload remove {min_x} {min_z} {max_x} {max_z}")
                    raise
            self._touch_spawner_keeper(key, spawners[key])
            assistant.send_chat_message(
                f"Spawner keeper {safe_name} enabled for {mob_id.replace('minecraft:', '')}. "
                f"It keeps the farm loaded and preserves mobs near it.",
                "all",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Spawner keeper failed: {exc}", "red")

    def _delete_spawner_keeper(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with self.spawner_lock:
                spawners = self._read_spawners_unlocked()
                entry = spawners.get(key)
                if not isinstance(entry, dict):
                    assistant.send_player_message(f"No spawner keeper named {safe_name}.", "yellow")
                    return
                x = math.floor(float(entry.get("x", entry.get("block_x", 0))))
                z = math.floor(float(entry.get("z", entry.get("block_z", 0))))
                radius = int(entry.get("radius") or SPAWNER_DEFAULT_RADIUS)
                dimension = str(entry.get("dimension") or "minecraft:overworld")
                with assistant._connect() as rcon:
                    response = rcon.command(
                        f"execute in {dimension} run forceload remove {x - radius} {z - radius} {x + radius} {z + radius}"
                    )
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "server rejected forceload removal")
                spawners.pop(key, None)
                self._write_spawners_unlocked(spawners)
            assistant.send_chat_message(f"Spawner keeper {entry.get('name', safe_name)} removed.", "all", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Spawner keeper removal failed: {exc}", "red")

    def _list_spawner_keepers(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with self.spawner_lock:
                entries = [entry for entry in self._read_spawners_unlocked().values() if isinstance(entry, dict)]
            if not entries:
                assistant.send_player_message("No spawner keepers. Use spawner set skelfarm skeleton at the farm.", "yellow")
                return
            assistant.send_player_message(f"Spawner keepers ({len(entries)}):", "aqua")
            for entry in sorted(entries, key=lambda item: str(item.get("name") or "").casefold()):
                dimension = str(entry.get("dimension") or "minecraft:overworld").replace("minecraft:", "")
                mob = str(entry.get("mob") or "?").replace("minecraft:", "")
                count = self._count_spawner_mobs(entry)
                count_text = str(count) if count is not None else "?"
                assistant.send_player_message(
                    f"{entry.get('name', '?')}: {mob} {count_text} persistent/tracked, r{entry.get('radius', SPAWNER_DEFAULT_RADIUS)} ({dimension})",
                    "aqua",
                )
        except Exception as exc:
            assistant.send_player_message(f"Spawner keeper list failed: {exc}", "red")

    def _spawner_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.spawner_lock:
                    spawners = self._read_spawners_unlocked()
                for key, entry in list(spawners.items()):
                    if not isinstance(entry, dict) or not entry.get("enabled", True):
                        continue
                    self._touch_spawner_keeper(str(key), entry)
            except Exception as exc:
                bridge_log(f"spawner loop error: {exc}")
            self.stop_event.wait(SPAWNER_TICK_SECONDS)

    def _touch_spawner_keeper(self, key: str, entry: dict) -> None:
        dimension = str(entry.get("dimension") or "minecraft:overworld")
        x = float(entry.get("x", 0.0))
        z = float(entry.get("z", 0.0))
        radius = max(8, min(96, int(entry.get("radius") or SPAWNER_DEFAULT_RADIUS)))
        assistant = self._assistant_for(str(entry.get("set_by") or "drwormbat"))
        with assistant._connect() as rcon:
            rcon.command(
                f"execute in {dimension} run forceload add "
                f"{math.floor(x - radius)} {math.floor(z - radius)} {math.floor(x + radius)} {math.floor(z + radius)}"
            )

    def _count_spawner_mobs(self, entry: dict) -> int | None:
        assistant = self._assistant_for(str(entry.get("set_by") or "drwormbat"))
        key = str(entry.get("name") or "spawner").casefold()
        try:
            with assistant._connect() as rcon:
                self._ensure_spawner_scoreboard(rcon)
                return self._count_spawner_mobs_with_rcon(rcon, key, entry)
        except Exception:
            return None

    def _count_spawner_mobs_with_rcon(self, rcon, key: str, entry: dict) -> int | None:
        mob = self._safe_spawner_mob(str(entry.get("mob") or "skeleton"))
        dimension = str(entry.get("dimension") or "minecraft:overworld")
        x = float(entry.get("x", 0.0))
        y = float(entry.get("y", 64.0))
        z = float(entry.get("z", 0.0))
        radius = max(8, min(96, int(entry.get("radius") or SPAWNER_DEFAULT_RADIUS)))
        score_name = "#" + re.sub(r"[^A-Za-z0-9_]", "_", key)[:32]
        rcon.command(
            f"execute in {dimension} positioned {x:.1f} {y:.1f} {z:.1f} "
            f"store result score {score_name} {SPAWNER_COUNT_OBJECTIVE} "
            f"if entity @e[type={mob},distance=..{radius}]"
        )
        response = rcon.command(f"scoreboard players get {score_name} {SPAWNER_COUNT_OBJECTIVE}")
        match = re.search(r"has\s+(-?\d+)\s+", response)
        return int(match.group(1)) if match else None

    def _ensure_spawner_scoreboard(self, rcon) -> None:
        rcon.command(f"scoreboard objectives add {SPAWNER_COUNT_OBJECTIVE} dummy")

    def _read_spawners_unlocked(self) -> dict:
        if not self.spawners_path.exists():
            return {}
        try:
            data = json.loads(self.spawners_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_spawners_unlocked(self, spawners: dict) -> None:
        temporary = self.spawners_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(spawners, indent=2), encoding="utf-8")
        temporary.replace(self.spawners_path)

    @staticmethod
    def _safe_spawner_mob(mob: str) -> str:
        text = str(mob or "").strip().lower().replace(" ", "_")
        text = text.removeprefix("minecraft:")
        mob_id = SPAWNER_MOBS.get(text)
        if not mob_id:
            allowed = ", ".join(sorted({key for key in SPAWNER_MOBS if key != "skelly"}))
            raise ValueError(f"unknown spawner mob {mob}; use {allowed}")
        return mob_id

    @staticmethod
    def _spawner_tag(key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", str(key or "spawner").casefold())[:40]
        return f"aios_spawner_{safe}"

    def _set_chunkload(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
            if not pos.get("ok"):
                assistant.send_player_message("Could not read your current position.", "red")
                return
            block_x = math.floor(float(pos["x"]))
            block_z = math.floor(float(pos["z"]))
            chunk_x = block_x // 16
            chunk_z = block_z // 16
            with self.chunkload_lock:
                chunkloads = self._read_chunkloads_unlocked()
                existing = chunkloads.get(key)
                if existing is not None:
                    existing_name = str(existing.get("name") or safe_name) if isinstance(existing, dict) else safe_name
                    assistant.send_player_message(f"Chunkload {existing_name} already exists. The old one was kept.", "yellow")
                    return
                same_chunk = next(
                    (
                        entry
                        for entry in chunkloads.values()
                        if isinstance(entry, dict)
                        and str(entry.get("dimension")) == dimension
                        and int(entry.get("chunk_x")) == chunk_x
                        and int(entry.get("chunk_z")) == chunk_z
                    ),
                    None,
                )
                if same_chunk:
                    assistant.send_player_message(
                        f"This chunk is already loaded as {same_chunk.get('name', 'another chunkload')}.",
                        "yellow",
                    )
                    return
                with assistant._connect() as rcon:
                    response = rcon.command(f"execute in {dimension} run forceload add {block_x} {block_z}")
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "server rejected forceload")
                chunkloads[key] = {
                    "name": safe_name,
                    "x": float(pos["x"]),
                    "z": float(pos["z"]),
                    "block_x": block_x,
                    "block_z": block_z,
                    "chunk_x": chunk_x,
                    "chunk_z": chunk_z,
                    "dimension": dimension,
                    "set_by": player,
                    "created_at": time.time(),
                }
                try:
                    self._write_chunkloads_unlocked(chunkloads)
                except Exception:
                    with assistant._connect() as rcon:
                        rcon.command(f"execute in {dimension} run forceload remove {block_x} {block_z}")
                    raise
            assistant.send_chat_message(
                f"Chunkload {safe_name} enabled at chunk {chunk_x} {chunk_z} ({dimension.replace('minecraft:', '')}).",
                "all",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Chunkload failed: {exc}", "red")

    def _delete_chunkload(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        key = safe_name.casefold()
        try:
            with self.chunkload_lock:
                chunkloads = self._read_chunkloads_unlocked()
                entry = chunkloads.get(key)
                if not isinstance(entry, dict):
                    assistant.send_player_message(f"No chunkload named {safe_name}.", "yellow")
                    return
                dimension = str(entry.get("dimension") or "minecraft:overworld")
                block_x = int(entry.get("block_x", int(entry.get("chunk_x", 0)) * 16))
                block_z = int(entry.get("block_z", int(entry.get("chunk_z", 0)) * 16))
                with assistant._connect() as rcon:
                    response = rcon.command(f"execute in {dimension} run forceload remove {block_x} {block_z}")
                if self._rcon_command_failed(response):
                    raise RuntimeError(response.strip() or "server rejected forceload removal")
                chunkloads.pop(key, None)
                try:
                    self._write_chunkloads_unlocked(chunkloads)
                except Exception:
                    with assistant._connect() as rcon:
                        rcon.command(f"execute in {dimension} run forceload add {block_x} {block_z}")
                    raise
            assistant.send_chat_message(f"Chunkload {entry.get('name', safe_name)} removed.", "all", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Chunkload removal failed: {exc}", "red")

    def _list_chunkloads(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            with self.chunkload_lock:
                entries = [entry for entry in self._read_chunkloads_unlocked().values() if isinstance(entry, dict)]
            if not entries:
                assistant.send_player_message("No saved chunkloads. Use chunkload name at the location.", "yellow")
                return
            assistant.send_player_message(f"Chunkloads ({len(entries)}):", "aqua")
            for entry in sorted(entries, key=lambda item: str(item.get("name") or "").casefold()):
                dimension = str(entry.get("dimension") or "minecraft:overworld").replace("minecraft:", "")
                assistant.send_player_message(
                    f"{entry.get('name', '?')}: chunk {entry.get('chunk_x', '?')} {entry.get('chunk_z', '?')} ({dimension})",
                    "aqua",
                )
        except Exception as exc:
            assistant.send_player_message(f"Chunkload list failed: {exc}", "red")

    def _read_chunkloads_unlocked(self) -> dict:
        if not self.chunkloads_path.exists():
            return {}
        try:
            data = json.loads(self.chunkloads_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_chunkloads_unlocked(self, chunkloads: dict) -> None:
        temporary = self.chunkloads_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(chunkloads, indent=2), encoding="utf-8")
        temporary.replace(self.chunkloads_path)

    @staticmethod
    def _rcon_command_failed(response: str) -> bool:
        text = str(response or "").casefold()
        return any(marker in text for marker in ("unknown command", "incorrect argument", "cannot mark", "error"))

    def _set_waypoint(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        waypoint_key = safe_name.casefold()
        try:
            existing = self._load_waypoints().get(waypoint_key)
            if existing:
                existing_name = str(existing.get("name") or safe_name)
                assistant.send_player_message(
                    f"Waypoint {existing_name} already exists. Delete it first if you really want to replace it.",
                    "yellow",
                )
                return
            with assistant._connect() as rcon:
                pos = assistant._read_player_position(rcon, player)
                dimension = self._read_player_dimension(rcon, player)
            if not pos.get("ok"):
                assistant.send_player_message(f"Could not read your position for waypoint {safe_name}.", "red")
                return
            waypoint = {
                "name": safe_name,
                "x": pos["x"],
                "y": pos["y"],
                "z": pos["z"],
                "dimension": dimension,
                "set_by": player,
                "updated_at": time.time(),
            }
            added, existing = self._add_waypoint_if_absent(waypoint_key, waypoint)
            if not added:
                existing_name = str((existing or {}).get("name") or safe_name)
                assistant.send_player_message(
                    f"Waypoint {existing_name} already exists. The old waypoint was kept.",
                    "yellow",
                )
                return
            assistant.send_chat_message(
                f"Waypoint {safe_name} set at {pos['x']} {pos['y']} {pos['z']} ({dimension}).",
                "all",
                "green",
            )
        except Exception as exc:
            assistant.send_player_message(f"Waypoint save failed: {exc}", "red")

    def _teleport_waypoint(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        try:
            waypoint = self._load_waypoints().get(safe_name.casefold())
            if not waypoint:
                assistant.send_player_message(f"No waypoint named {safe_name}. Type list wp.", "yellow")
                return
            dimension = str(waypoint.get("dimension") or "minecraft:overworld")
            x = float(waypoint["x"])
            y = float(waypoint["y"])
            z = float(waypoint["z"])
            with assistant._connect() as rcon:
                rcon.command(f"execute in {dimension} run tp {player} {x:.1f} {y:.1f} {z:.1f}")
            assistant.send_player_message(f"Teleported to {waypoint.get('name', safe_name)}.", "green")
        except Exception as exc:
            assistant.send_player_message(f"Waypoint teleport failed: {exc}", "red")

    def _delete_waypoint(self, player: str, name: str) -> None:
        assistant = self._assistant_for(player)
        safe_name = self._safe_wp_name(name)
        try:
            waypoints = self._load_waypoints()
            removed = waypoints.pop(safe_name.casefold(), None)
            self._save_waypoints(waypoints)
            if removed:
                assistant.send_chat_message(f"Waypoint {safe_name} removed.", "all", "yellow")
            else:
                assistant.send_player_message(f"No waypoint named {safe_name}.", "yellow")
        except Exception as exc:
            assistant.send_player_message(f"Waypoint delete failed: {exc}", "red")

    def _list_waypoints(self, player: str) -> None:
        assistant = self._assistant_for(player)
        try:
            waypoints = list(self._load_waypoints().values())
            if not waypoints:
                assistant.send_player_message("No waypoints saved. Use set wp home.", "yellow")
                return
            names = ", ".join(sorted(str(wp.get("name") or "") for wp in waypoints if wp.get("name")))
            assistant.send_player_message(f"Waypoints: {names}", "aqua")
        except Exception as exc:
            assistant.send_player_message(f"Waypoint list failed: {exc}", "red")

    def _send_help(self, player: str) -> None:
        assistant = self._assistant_for(player)
        lines = [
            "AI chat: ai what do I need for a piston | ai craft a crafting table | ai who is online",
            "Lists: ai add redstone and iron to my list and share it | ai share my list | ai clear my list",
            "Server chat: ai tell everyone meet at spawn | ai say to server we need redstone",
            "Waypoints: set wp home | tp home | list wp | del wp home",
            "Chunkloads: chunkload name | list chunkload | del chunkload name",
            "Spawner farms: spawner set skelfarm skeleton | spawner list | spawner del skelfarm",
            "Players: where Player | tp Player | back | top | eat",
            "Build tools: bt help | bt pos1 | bt pos2 | bt fill [block] | bt clear | bt cancel",
            "Workers: miner help | miner create name | miner chest name | miner start name iron",
            "World/QOL: day | night | ai where am I | ai clear weather",
        ]
        try:
            for line in lines:
                assistant.send_chat_message(line, "all", "aqua")
        except Exception:
            pass

    @staticmethod
    def _is_help_request(request: str) -> bool:
        text = re.sub(r"\s+", " ", str(request or "").strip().casefold())
        return text in {"help", "commands", "what can you do", "what can u do", "show commands", "ai help"}

    def _read_player_dimension(self, rcon, player: str) -> str:
        response = rcon.command(f"data get entity {player} Dimension")
        match = re.search(r'"(minecraft:[^"]+)"', response)
        return match.group(1) if match else "minecraft:overworld"

    @staticmethod
    def _resolve_online_player(rcon, requested: str) -> str | None:
        response = rcon.command("list")
        match = re.search(r"players online:\s*(.*)$", response, re.IGNORECASE)
        if not match:
            return None
        requested_key = str(requested or "").strip().casefold()
        players = [name.strip() for name in match.group(1).split(",") if name.strip()]
        return next((name for name in players if name.casefold() == requested_key), None)

    def _closest_waypoint(self, pos: dict, dimension: str) -> tuple[dict, float] | None:
        closest: tuple[dict, float] | None = None
        px = float(pos["x"])
        py = float(pos["y"])
        pz = float(pos["z"])
        for waypoint in self._load_waypoints().values():
            if not isinstance(waypoint, dict) or str(waypoint.get("dimension")) != dimension:
                continue
            try:
                distance = math.sqrt(
                    (px - float(waypoint["x"])) ** 2
                    + (py - float(waypoint["y"])) ** 2
                    + (pz - float(waypoint["z"])) ** 2
                )
            except (KeyError, TypeError, ValueError):
                continue
            if closest is None or distance < closest[1]:
                closest = (waypoint, distance)
        return closest

    @staticmethod
    def _parse_last_death_location(response: str) -> dict | None:
        pos_match = re.search(r"pos:\s*\[I;\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", response)
        dimension_match = re.search(r'dimension:\s*"(minecraft:[^"]+)"', response)
        if not pos_match or not dimension_match:
            return None
        return {
            "x": int(pos_match.group(1)),
            "y": int(pos_match.group(2)),
            "z": int(pos_match.group(3)),
            "dimension": dimension_match.group(1),
        }

    def _load_waypoints(self) -> dict:
        with self.waypoint_lock:
            if not self.waypoints_path.exists():
                return {}
            try:
                data = json.loads(self.waypoints_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

    def _save_waypoints(self, waypoints: dict) -> None:
        with self.waypoint_lock:
            self.waypoints_path.write_text(json.dumps(waypoints, indent=2), encoding="utf-8")

    def _add_waypoint_if_absent(self, key: str, waypoint: dict) -> tuple[bool, dict | None]:
        with self.waypoint_lock:
            if self.waypoints_path.exists():
                try:
                    data = json.loads(self.waypoints_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
            else:
                data = {}
            waypoints = data if isinstance(data, dict) else {}
            existing = waypoints.get(key)
            if existing is not None:
                return False, existing if isinstance(existing, dict) else None
            waypoints[key] = waypoint
            self.waypoints_path.write_text(json.dumps(waypoints, indent=2), encoding="utf-8")
            return True, None

    @staticmethod
    def _safe_wp_name(name: str) -> str:
        text = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", text):
            raise ValueError("waypoint names can only use letters, numbers, _ and -")
        return text


_bridge: MinecraftChatBridge | None = None


def start_minecraft_chat_bridge() -> MinecraftChatBridge:
    global _bridge
    if _bridge is None:
        _bridge = MinecraftChatBridge()
    _bridge.start()
    return _bridge
