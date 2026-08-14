"""The Director runtime: coordinator turns, tool execution, subagent jobs and
the event fan-out that the phone renders.

Shape of one turn:

    user message -> items rebuilt from the thread -> model
      -> text streams out as deltas
      -> tool calls run (approval-gated when destructive)
      -> results go back as items, model runs again
      -> until the model answers with no tool calls

Heavy work (a pixel-operator run, a CODE session on another machine) does not
happen inside the turn. It becomes a job, the turn ends, and when the job
finishes the coordinator is woken with the result so it can report back. That
is what keeps the chat answering while something long is running.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from . import agents as agents_mod
from . import config, models, push, store, tools as tools_mod
from . import routines as routines_mod

MAX_TOOL_ROUNDS = 24
TURN_TIMEOUT = 900.0
SCHEDULER_TICK = 20.0
COMPACTION_IDLE_SECONDS = 60 * 60
COMPACTION_INPUT_CHARS = 120_000
GROUP_TAG_HOPS = 12
GROUP_QUIET_TOOLS = {"start_work", "react"}
INTERRUPTED_TOOL_OUTPUT = (
    "Tool execution was interrupted before completion. Treat this call as cancelled; "
    "do not assume it succeeded."
)


class Runtime:
    """One instance per process. Owns every in-flight conversation and job."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._questions: dict[str, asyncio.Future] = {}
        self._approvals: dict[str, asyncio.Future] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._machines: dict[str, Any] = {}     # machine_id -> MachineLink
        self._pending_calls: dict[str, asyncio.Future] = {}
        self._queued: dict[str, list[dict]] = {}
        self._scheduler: asyncio.Task | None = None
        self._compactions: dict[str, asyncio.Task] = {}
        self._compaction_retry_at: dict[str, float] = {}
        # agent_id -> "yes to everything for the rest of this run"
        self._run_approval: dict[str, bool] = {}
        # Threads a client currently has open, so a reply the user is watching
        # does not also buzz their pocket.
        self._watching: dict[str, float] = {}
        # (group_thread_id, member_agent_id) -> in-flight group reply
        self._group_speaks: dict[tuple[str, str], asyncio.Task] = {}
        # private_thread_id -> work that was started from a group chat
        self._private_from_group: dict[str, dict] = {}
        # group_thread_id -> tag-follow-up hops since the last user message
        self._group_tag_hops: dict[str, int] = {}
        # Relay hops carried by destination thread. Prevents two agents from
        # automatically messaging each other forever.
        self._relay_depth: dict[str, int] = {}

    # ---------------- events ----------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def emit(self, kind: str, payload: dict | None = None, *,
                   thread_id: str = "", agent_id: str = "",
                   persist: bool = True) -> dict:
        """Append to the event log and push to every live listener.

        Deltas are not persisted — they are replaced by the finished message —
        so resuming a thread never replays a token storm.
        """
        if persist:
            event = store.add_event(kind, payload or {}, thread_id=thread_id, agent_id=agent_id)
        else:
            event = {"id": 0, "kind": kind, "payload": payload or {},
                     "thread_id": thread_id, "agent_id": agent_id,
                     "created_at": time.time()}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
        return event

    # ---------------- attention & push ----------------

    def watching(self, thread_id: str, *, active: bool = True) -> None:
        """A client says it has this thread on screen right now."""
        if active:
            self._watching[thread_id] = time.time()
        else:
            self._watching.pop(thread_id, None)

    def is_watched(self, thread_id: str, *, within: float = 45.0) -> bool:
        seen = self._watching.get(thread_id or "")
        return bool(seen and time.time() - seen < within)

    async def notify(self, thread_id: str, agent: dict, title: str, body: str,
                     *, tag: str = "", force: bool = False) -> None:
        """Push to the phone, unless the phone is already looking at it."""
        if not force and self.is_watched(thread_id):
            return
        if agent and not int(agent.get("notify", 1) or 0):
            return
        try:
            await push.send(title, body, url=f"/?agent={agent.get('id', '')}",
                            tag=tag or thread_id)
        except Exception:
            # Notification failure must never break a turn.
            pass

    # ---------------- questions & approvals ----------------

    async def ask_user(self, thread_id: str, agent_id: str, question: str, *,
                       options: list[str] | None = None, kind: str = "question",
                       extra: dict | None = None, timeout: float = 3600.0) -> str:
        qid = store.new_id("qst")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._questions[qid] = future
        payload = {"id": qid, "question": question, "options": list(options or []),
                   "kind": kind, **(extra or {})}
        await self.emit("question", payload, thread_id=thread_id, agent_id=agent_id)
        store.touch_thread(thread_id, status="waiting")
        agent = store.get_agent(agent_id) or {}
        await self.notify(thread_id, agent,
                          str(agent.get("name") or "Director"),
                          question, tag=f"q-{qid}", force=True)
        try:
            answer = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            answer = ""
        finally:
            self._questions.pop(qid, None)
        store.touch_thread(thread_id, status="running")
        await self.emit("question.answered", {"id": qid, "answer": answer},
                        thread_id=thread_id, agent_id=agent_id)
        return str(answer or "")

    def answer_question(self, question_id: str, answer: str) -> bool:
        future = self._questions.get(question_id)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def auto_approved(self, agent: dict, settings: dict) -> bool:
        """Has Calle already said yes to everything?

        Three places can grant it, cheapest first: this run, this agent, or the
        whole box. Any of them means no card is raised.
        """
        if self._run_approval.get(str(agent.get("id") or "")):
            return True
        if int(agent.get("auto_approve", 0) or 0):
            return True
        return bool((settings.get("safety") or {}).get("approve_all"))

    def grant_run_approval(self, agent_id: str) -> None:
        """Approve everything for the rest of this run only."""
        self._run_approval[agent_id] = True

    async def request_approval(self, thread_id: str, agent_id: str, *, tool: str,
                               summary: str, detail: str = "",
                               payload: dict | None = None,
                               timeout: float = 3600.0) -> dict:
        record = store.create_approval(thread_id=thread_id, agent_id=agent_id, tool=tool,
                                       summary=summary, detail=detail, payload=payload or {})
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._approvals[record["id"]] = future
        await self.emit("approval", {"id": record["id"], "tool": tool, "summary": summary,
                                     "detail": detail, "payload": payload or {}},
                        thread_id=thread_id, agent_id=agent_id)
        store.touch_thread(thread_id, status="waiting")
        agent = store.get_agent(agent_id) or {}
        await self.notify(thread_id, agent,
                          str(agent.get("name") or "Director"),
                          summary, tag=f"a-{record['id']}", force=True)
        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            decision = {"status": "declined", "note": "timed out waiting for approval"}
            store.decide_approval(record["id"], "declined", "timed out")
        finally:
            self._approvals.pop(record["id"], None)
        store.touch_thread(thread_id, status="running")
        await self.emit("approval.decided", {"id": record["id"], **decision},
                        thread_id=thread_id, agent_id=agent_id)
        return decision

    def decide_approval(self, approval_id: str, status: str, note: str = "",
                        *, scope: str = "") -> bool:
        """Settle one approval. `scope` widens the yes.

        "run"   approve everything for the rest of this agent's run
        "agent" approve everything this agent ever asks
        "all"   approve everything, everywhere, until turned off again
        """
        record = store.get_approval(approval_id) or {}
        agent_id = str(record.get("agent_id") or "")
        if status == "approved" and scope:
            if scope == "run" and agent_id:
                self.grant_run_approval(agent_id)
            elif scope == "agent" and agent_id:
                store.update_agent(agent_id, {"auto_approve": True})
            elif scope == "all":
                config.update_settings({"safety": {"approve_all": True}})

        store.decide_approval(approval_id, status, note)
        future = self._approvals.get(approval_id)
        if future is None or future.done():
            return bool(record)
        future.set_result({"status": status, "note": note, "scope": scope})
        return True

    # ---------------- machines ----------------

    def attach_machine(self, machine_id: str, link: Any) -> None:
        self._machines[machine_id] = link
        store.set_machine_online(machine_id, True)

    def detach_machine(self, machine_id: str, link: Any | None = None) -> bool:
        """Detach only the socket that is actually closing.

        A reconnect can install a fresh link before aiohttp finishes the old
        socket's ``finally`` block. Letting that stale block pop by id marks a
        live Windows PC offline and makes the phone offer Wake-on-LAN.
        """
        current = self._machines.get(machine_id)
        if current is None or (link is not None and current is not link):
            return False
        self._machines.pop(machine_id, None)
        store.set_machine_online(machine_id, False)
        return True

    def machine_link(self, machine_id: str) -> Any:
        return self._machines.get(machine_id)

    def online_machines(self) -> list[dict]:
        rows = []
        for machine in store.list_machines():
            machine = dict(machine)
            machine["online"] = machine["id"] in self._machines
            rows.append(machine)
        return rows

    async def call_machine(self, machine_id: str, action: str, payload: dict,
                           *, timeout: float = 120.0) -> dict:
        """Ask a connected client (the Windows box) to do something, and wait."""
        link = self._machines.get(machine_id)
        if link is None:
            return {"ok": False, "error": "machine is not connected"}
        call_id = store.new_id("call")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_calls[call_id] = future
        try:
            await link.send({"type": "call", "call_id": call_id,
                             "action": action, "payload": payload})
        except Exception as exc:
            self._pending_calls.pop(call_id, None)
            return {"ok": False, "error": f"could not reach machine: {exc}"}
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"machine did not answer within {timeout:.0f}s"}
        finally:
            self._pending_calls.pop(call_id, None)

    async def cast_machine(self, machine_id: str, action: str, payload: dict) -> bool:
        """Send a latency-sensitive command without waiting for a reply.

        Pointer movement is a stream, not a series of RPCs.  Keeping those
        packets out of ``_pending_calls`` avoids allocating a future and a
        round trip for every accelerometer sample while retaining the same
        authenticated machine link.
        """
        link = self._machines.get(machine_id)
        if link is None:
            return False
        try:
            await link.send({"type": "cast", "action": action,
                             "payload": payload})
        except Exception:
            return False
        return True

    def resolve_machine_call(self, call_id: str, result: dict) -> bool:
        future = self._pending_calls.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    # ---------------- jobs ----------------

    def start_job(self, job: dict, coro_factory: Callable[[], Awaitable[dict]]) -> None:
        """Run a subagent in the background and wake the coordinator after."""
        async def runner() -> dict:
            try:
                result = await coro_factory()
            except asyncio.CancelledError:
                result = {"status": "stopped", "summary": "cancelled"}
                raise
            except Exception as exc:
                result = {"status": "fail", "summary": f"{type(exc).__name__}: {exc}"}
            finally:
                self._jobs.pop(job["id"], None)
            store.update_job(job["id"], status=str(result.get("status") or "done"),
                             result=result)
            await self.emit("job.finished", {"id": job["id"], "kind": job["kind"], **result},
                            thread_id=job.get("thread_id", ""),
                            agent_id=job.get("agent_id", ""))
            await self._report_job(job, result)
            return result

        self._jobs[job["id"]] = asyncio.create_task(runner())

    async def _report_job(self, job: dict, result: dict) -> None:
        """Feed a finished job back into the conversation as a fresh turn."""
        thread_id = job.get("thread_id") or ""
        if not thread_id:
            return
        summary = str(result.get("summary") or "").strip()
        note = (f"[{job['kind']} job {job['id']} finished: {result.get('status')}]\n"
                f"{summary[:4000]}")
        store.add_message(thread_id, "system", note,
                          {"job_id": job["id"], "kind": job["kind"]})
        await self.run_turn(thread_id, trigger="job")

    def stop_job(self, job_id: str) -> bool:
        task = self._jobs.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    # ---------------- routines ----------------

    def start_scheduler(self) -> None:
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self) -> None:
        """Tick, fire what is due, and work out when each thing runs next.

        Deliberately dumb: a wall-clock check every twenty seconds rather than
        sleeping until the next event, so a routine added or changed while the
        loop is asleep is picked up without waking anything.
        """
        while True:
            try:
                await asyncio.sleep(SCHEDULER_TICK)
                for routine in store.due_routines():
                    await self.fire_routine(routine)
                self.start_due_compactions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.emit("routine.error", {"error": f"{type(exc).__name__}: {exc}"})

    def start_due_compactions(self) -> None:
        """Start bounded idle compactions without delaying scheduled routines."""
        available = max(0, 2 - len(self._compactions))
        if not available:
            return
        due = store.threads_due_for_compaction(
            before=time.time() - COMPACTION_IDLE_SECONDS, limit=available)
        for thread in due:
            thread_id = str(thread["id"])
            if (thread_id in self._compactions or self.busy(thread_id)
                    or self._compaction_retry_at.get(thread_id, 0) > time.time()):
                continue

            async def run_compaction(tid: str = thread_id) -> None:
                try:
                    await self.compact_thread(tid)
                    self._compaction_retry_at.pop(tid, None)
                except Exception as exc:
                    self._compaction_retry_at[tid] = time.time() + 300
                    await self.emit("thread.compaction_error",
                                    {"error": f"{type(exc).__name__}: {exc}"},
                                    thread_id=tid)

            task = asyncio.create_task(run_compaction())
            self._compactions[thread_id] = task
            task.add_done_callback(lambda _done, tid=thread_id: self._compactions.pop(tid, None))

    async def compact_due_threads(self, *, force: bool = False) -> dict:
        """Compact every eligible chat once; used by maintenance and tests."""
        rows = store.threads_due_for_compaction(
            before=time.time() - COMPACTION_IDLE_SECONDS, force=force, limit=5000)
        compacted, failed = [], []
        for thread in rows:
            thread_id = str(thread["id"])
            if self.busy(thread_id):
                continue
            try:
                if await self.compact_thread(thread_id):
                    compacted.append(thread_id)
            except Exception as exc:
                failed.append({"thread_id": thread_id,
                               "error": f"{type(exc).__name__}: {exc}"})
        return {"compacted": compacted, "failed": failed}

    async def compact_thread(self, thread_id: str) -> bool:
        """Replace the model-facing past with one durable summary boundary.

        Stored messages are deliberately untouched: the phone can disclose the
        complete transcript, while future model turns see the summary followed
        only by messages written after this exact rowid.
        """
        thread = store.get_thread(thread_id)
        if not thread:
            return False
        through = store.latest_message_sequence(thread_id)
        previous = int(thread.get("compacted_through") or 0)
        if not through or through <= previous:
            return False
        agent = store.get_agent(str(thread.get("agent_id") or ""))
        if not agent:
            return False

        lines = []
        old_summary = str(thread.get("summary") or "").strip()
        for row in store.list_messages(thread_id, after_sequence=previous,
                                       through_sequence=through):
            role = str(row.get("role") or "message").upper()
            meta = row.get("meta") or {}
            content = str(row.get("content") or "")
            if role == "TOOL_CALL":
                content = f"{meta.get('name', 'tool')}({meta.get('arguments', '{}')})"
            elif role == "TOOL_RESULT":
                content = f"{meta.get('name', 'tool')}: {meta.get('output', '')}"
            lines.append(f"{role}: {content[:6000]}")
        fresh = "\n\n".join(lines)
        prefix = "PREVIOUS COMPACTED CONTEXT:\n" + old_summary + "\n\n" if old_summary else ""
        room = max(10_000, COMPACTION_INPUT_CHARS - len(prefix))
        transcript = prefix + fresh[-room:]

        settings = config.load_settings()
        reply = await models.complete(
            backend=str(agent.get("backend") or ""),
            model=str(agent.get("model") or ""),
            reasoning="low",
            instructions=(
                "Compact the conversation into a factual continuation summary. Preserve user goals, "
                "decisions, constraints, names, exact identifiers, unfinished work, results, and errors. "
                "Do not add commentary, greetings, or facts not present. Write concise plain text."),
            items=[models.user_message(transcript)], tools=[], timeout=180.0,
            settings=settings)
        summary = str(reply.get("text") or "").strip()
        if not summary:
            raise models.ModelError("the compaction model returned no summary")
        store.save_compaction(thread_id, summary, through)
        await self.emit("thread.compacted", {"through": through},
                        thread_id=thread_id, agent_id=str(agent["id"]))
        return True

    async def fire_routine(self, routine: dict) -> None:
        """Drop a routine's prompt into its agent's thread and run a turn."""
        agent_id = str(routine.get("agent_id") or "")
        agent = store.get_agent(agent_id)
        if not agent:
            store.delete_routine(routine["id"])
            return

        thread = store.latest_thread(agent_id) or store.create_thread(agent_id)
        schedule = routine.get("schedule") or {}
        now = time.time()

        # Reschedule before running: a turn that throws must not leave the
        # routine due forever, re-firing on every tick.
        if routines_mod.is_recurring(schedule):
            try:
                following = routines_mod.next_run(schedule, after=now)
            except routines_mod.ScheduleError:
                following = 0.0
            store.update_routine(routine["id"], {
                "next_run": following, "last_run": now,
                "runs": int(routine.get("runs") or 0) + 1})
        else:
            store.update_routine(routine["id"], {
                "enabled": False, "next_run": 0, "last_run": now,
                "runs": int(routine.get("runs") or 0) + 1})

        await self.emit("routine.fired", {"id": routine["id"], "name": routine["name"],
                                          "prompt": routine["prompt"]},
                        thread_id=thread["id"], agent_id=agent_id)
        store.add_message(thread["id"], "user", str(routine.get("prompt") or ""),
                          {"routine_id": routine["id"], "routine_name": routine["name"]})
        store.touch_thread(thread["id"], preview=str(routine.get("name") or "")[:280])
        await self.emit("message.user", {"text": str(routine.get("prompt") or ""),
                                         "routine": routine["name"]},
                        thread_id=thread["id"], agent_id=agent_id)
        await self.run_turn(thread["id"], trigger="routine")

    # ---------------- turns ----------------

    def cancel_event(self, thread_id: str) -> asyncio.Event:
        event = self._cancels.get(thread_id)
        if event is None:
            event = asyncio.Event()
            self._cancels[thread_id] = event
        return event

    def stop_thread(self, thread_id: str) -> bool:
        targets = {thread_id}
        targets.update(
            private_id for private_id, origin in self._private_from_group.items()
            if origin.get("group_thread_id") == thread_id
        )
        stopped = False
        for target in targets:
            self.cancel_event(target).set()
            task = self._turn_tasks.get(target)
            if task and not task.done():
                task.cancel()
                stopped = True
            if store.get_thread(target):
                # A task cancelled before its coroutine starts never reaches
                # _turn's finally block. Repair the persisted status here too.
                store.touch_thread(target, status="idle")
        for key, speak in list(self._group_speaks.items()):
            if key[0] == thread_id and speak and not speak.done():
                speak.cancel()
                stopped = True
        return stopped

    def busy(self, thread_id: str) -> bool:
        task = self._turn_tasks.get(thread_id)
        if task and not task.done():
            return True
        if any(not speak.done() for (tid, _), speak in self._group_speaks.items()
               if tid == thread_id):
            return True
        for private_id, origin in self._private_from_group.items():
            if origin.get("group_thread_id") != thread_id:
                continue
            private = self._turn_tasks.get(private_id)
            if private and not private.done():
                return True
        return False

    def working_from_group(self, group_thread_id: str) -> list[dict]:
        rows = []
        for private_id, origin in self._private_from_group.items():
            if origin.get("group_thread_id") != group_thread_id:
                continue
            private = self._turn_tasks.get(private_id)
            if private and not private.done():
                rows.append({**origin, "private_thread_id": private_id})
        return rows

    async def send_message(self, thread_id: str, text: str,
                           attachments: list[dict] | None = None) -> dict:
        """Store a user message and run (or queue) a coordinator turn."""
        thread = store.get_thread(thread_id)
        if not thread:
            raise ValueError("no such thread")
        message = store.add_message(thread_id, "user", text,
                                    {"attachments": attachments or []})
        store.touch_thread(thread_id, preview=text[:280])
        await self.emit("message.user", {"id": message["id"], "text": text,
                                         "attachments": attachments or []},
                        thread_id=thread_id, agent_id=thread["agent_id"])
        owner = store.get_agent(str(thread.get("agent_id") or ""))
        if store.is_group(owner):
            await self.run_group_round(thread_id, trigger="user")
            return message
        if self.busy(thread_id):
            # Mid-run steering: the message is already in the transcript, so the
            # next model round picks it up on its own.
            self._queued.setdefault(thread_id, []).append(message)
            await self.emit("thread.steered", {"text": text}, thread_id=thread_id,
                            agent_id=thread["agent_id"], persist=False)
            return message
        await self.run_turn(thread_id, trigger="user")
        return message

    async def relay_agent_message(self, ctx: tools_mod.ToolContext,
                                  recipient: str, text: str) -> tools_mod.ToolResult:
        """Deliver an attributed message into another chat and wake its owner."""
        target_text = str(recipient or "").strip()
        body = str(text or "").strip()
        if not target_text or not body:
            return tools_mod.ToolResult(error="recipient and message are required")
        if len(body) > 8000:
            return tools_mod.ToolResult(error="agent messages are limited to 8000 characters")

        agents = store.list_agents()
        lowered = target_text.casefold()
        matches = [row for row in agents if str(row.get("id") or "").casefold() == lowered
                   or str(row.get("name") or "").casefold() == lowered]
        if not matches:
            partial = [row for row in agents if lowered in str(row.get("name") or "").casefold()]
            if len(partial) == 1:
                matches = partial
        if len(matches) != 1:
            names = ", ".join(row.get("name") or row.get("id") for row in agents)
            return tools_mod.ToolResult(
                error=("recipient is ambiguous" if matches else "no such agent or group")
                + f". Available: {names}")
        target = matches[0]
        source_id = str(ctx.agent.get("id") or "")
        if str(target.get("id") or "") == source_id:
            return tools_mod.ToolResult(error="that is this same chat")

        source_depth = int(ctx.depth or self._relay_depth.get(ctx.thread_id, 0))
        if source_depth >= 3:
            return tools_mod.ToolResult(
                error="agent relay limit reached; ask Calle before continuing the chain")
        thread = store.latest_thread(target["id"]) or store.create_thread(target["id"])
        meta = {
            "kind": "agent_message",
            "sender_id": source_id,
            "sender_name": str(ctx.agent.get("name") or "Agent"),
            "relay_depth": source_depth + 1,
        }
        message = store.add_message(thread["id"], "user", body, meta)
        store.touch_thread(
            thread["id"], preview=f"{meta['sender_name']}: {body}"[:280])
        self._relay_depth[thread["id"]] = source_depth + 1
        await self.emit("message.user", {
            "id": message["id"], "text": body, "kind": "agent_message",
            "sender_id": source_id, "sender_name": meta["sender_name"],
        }, thread_id=thread["id"], agent_id=target["id"])
        if store.is_group(target):
            await self.run_group_round(thread["id"], trigger="agent_message")
        elif not self.busy(thread["id"]):
            await self.run_turn(thread["id"], trigger="agent_message")
        else:
            self._queued.setdefault(thread["id"], []).append(message)
        await self.notify(
            thread["id"], target,
            str(meta["sender_name"] or "Agent"),
            body, tag=f"relay-{message['id']}")
        return tools_mod.ToolResult(
            output=f"delivered to {target.get('name')}; it is now in that chat",
            card={"title": "message sent", "preview": target.get("name") or target["id"],
                  "meta": "delivered", "tone": "accent", "body": body,
                  "agent_id": target["id"]},
        )

    async def run_group_round(self, thread_id: str, *, trigger: str = "user") -> None:
        """Fan the latest group message out to every member, in parallel.

        Each agent decides whether to speak. In-flight speakers are not
        cancelled — they finish, then pick up a newer user message the same
        way a private chat queues a follow-up.
        """
        thread = store.get_thread(thread_id)
        if not thread:
            return
        group = store.get_agent(str(thread.get("agent_id") or ""))
        if not store.is_group(group):
            return
        members = agents_mod.group_members(group)
        if not members:
            await self.emit("thread.error", {"error": "this group has no members"},
                            thread_id=thread_id, agent_id=group["id"])
            return
        store.touch_thread(thread_id, status="running")
        await self.emit("thread.status", {"status": "running", "trigger": trigger},
                        thread_id=thread_id, agent_id=group["id"])
        if trigger in {"user", "agent_message"}:
            self._group_tag_hops[thread_id] = 0
        await self.emit("group.considering", {
            "agent_ids": [row["id"] for row in members],
            "trigger": trigger,
        }, thread_id=thread_id, agent_id=group["id"], persist=False)
        for member in members:
            self._spawn_group_member(thread_id, group, member)

    def _spawn_group_member(self, thread_id: str, group: dict, member: dict) -> None:
        key = (thread_id, str(member["id"]))
        existing = self._group_speaks.get(key)
        if existing and not existing.done():
            return

        async def runner() -> None:
            started = time.time()
            try:
                await self._group_member_turn(thread_id, group, member)
            except asyncio.CancelledError:
                await self.emit("group.quiet", {"agent_id": member["id"], "reason": "stopped"},
                                thread_id=thread_id, agent_id=member["id"], persist=False)
                raise
            except Exception as exc:
                await self.emit("thread.error",
                                {"error": f"{member.get('name')}: {type(exc).__name__}: {exc}"},
                                thread_id=thread_id, agent_id=member["id"])
            finally:
                self._group_speaks.pop(key, None)
                messages = store.list_messages(thread_id)
                last = messages[-1] if messages else None
                newer_user = (last and last["role"] == "user"
                              and float(last.get("created_at") or 0) > started + 0.05)
                tagged = self._tagged_since(thread_id, member, started)
                if newer_user or tagged:
                    self._spawn_group_member(thread_id, group, member)
                elif not any(not task.done() for (tid, _), task in self._group_speaks.items()
                             if tid == thread_id):
                    if not self.working_from_group(thread_id):
                        store.touch_thread(thread_id, status="idle")
                        await self.emit("thread.status", {"status": "idle"},
                                        thread_id=thread_id, agent_id=group["id"])
                    await self.emit("group.round_done", {}, thread_id=thread_id,
                                    agent_id=group["id"], persist=False)

        self._group_speaks[key] = asyncio.create_task(runner())

    def _group_transcript(self, thread_id: str) -> str:
        names = {row["id"]: row["name"] for row in store.list_agents()}
        lines = []
        for row in store.list_messages(thread_id)[-60:]:
            role = row.get("role")
            meta = row.get("meta") or {}
            content = str(row.get("content") or "").strip()
            if role == "user":
                if meta.get("kind") == "agent_message":
                    lines.append(f"{meta.get('sender_name') or 'Agent'} (relayed): {content}")
                else:
                    lines.append(f"Calle: {content}")
            elif role == "assistant":
                speaker = names.get(str(meta.get("speaker_id") or ""), "Agent")
                kind = str(meta.get("kind") or "")
                if kind == "work_done":
                    lines.append(f"[{speaker} finished private work: {content[:500]}]")
                else:
                    lines.append(f"{speaker}: {content}")
            elif role == "reaction":
                speaker = names.get(str(meta.get("speaker_id") or ""), "Agent")
                lines.append(f"{speaker} reacted {content or meta.get('emoji') or '👍'}")
            elif role == "tool_call" and str(meta.get("name") or "") == "start_work":
                speaker = names.get(str(meta.get("speaker_id") or ""), "Agent")
                lines.append(f"[{speaker} started private work]")
        return "\n".join(lines) or "(empty group chat)"

    def _is_silent_reply(self, text: str) -> bool:
        compact = "".join(ch for ch in str(text or "").strip().upper() if ch.isalnum())
        return compact in {"", "SILENT", "NOREPLY", "PASS", "QUIET"}

    def _nudge_mentions(self, thread_id: str, group: dict, speaker: dict, text: str) -> None:
        """Wake anyone this speaker just @tagged, until the hop cap."""
        hops = int(self._group_tag_hops.get(thread_id) or 0)
        members = agents_mod.group_members(group)
        for member in agents_mod.mentions_in(text, members):
            if str(member.get("id") or "") == str(speaker.get("id") or ""):
                continue
            if hops >= GROUP_TAG_HOPS:
                return
            hops += 1
            self._group_tag_hops[thread_id] = hops
            self._spawn_group_member(thread_id, group, member)

    def _tagged_since(self, thread_id: str, member: dict, started: float) -> bool:
        """True when someone else named this member after this turn began."""
        if int(self._group_tag_hops.get(thread_id) or 0) >= GROUP_TAG_HOPS:
            return False
        member_id = str(member.get("id") or "")
        for row in store.list_messages(thread_id):
            if float(row.get("created_at") or 0) <= started + 0.05:
                continue
            if row.get("role") != "assistant":
                continue
            meta = row.get("meta") or {}
            if str(meta.get("speaker_id") or "") == member_id:
                continue
            if agents_mod.mentions_in(str(row.get("content") or ""), [member]):
                return True
        return False

    async def _group_member_turn(self, thread_id: str, group: dict, member: dict) -> None:
        settings = config.load_settings()
        cancel = self.cancel_event(thread_id)
        members = agents_mod.group_members(group)
        ctx = tools_mod.ToolContext(
            agent=member, thread_id=thread_id, settings=settings,
            emit=lambda kind, payload: self.emit(kind, payload, thread_id=thread_id,
                                                 agent_id=member["id"]),
            request_approval=lambda **kw: self.request_approval(thread_id, member["id"], **kw),
            ask_user=lambda question, **kw: self.ask_user(thread_id, member["id"], question, **kw),
            cancel=cancel, hub=self, source="group")
        schemas = tools_mod.schemas(agents_mod.GROUP_TOOLS)
        instructions = agents_mod.group_system_prompt(member, group, members, settings)
        transcript = self._group_transcript(thread_id)
        items = [
            models.user_message(
                "Recent group chat:\n" + transcript
                + "\n\nReply as yourself. A room message is for you — speak or "
                  "react. SILENT only if Calle named someone else and not you. "
                  "If they asked for a reaction, call react and write nothing."
            )
        ]

        spoke = False
        for _round in range(8):
            if cancel.is_set():
                return
            reply = await models.complete(
                backend=str(member.get("backend") or ""),
                model=str(member.get("model") or ""),
                reasoning=str(member.get("reasoning") or "low"),
                instructions=instructions, items=items, tools=schemas,
                timeout=180.0, settings=settings)
            text = str(reply.get("text") or "").strip()
            calls = reply.get("tool_calls") or []
            silent = self._is_silent_reply(text) and not calls
            if silent:
                await self.emit("group.quiet", {"agent_id": member["id"]},
                                thread_id=thread_id, agent_id=member["id"], persist=False)
                return
            if text and not self._is_silent_reply(text):
                message = store.add_message(thread_id, "assistant", text, {
                    "speaker_id": member["id"],
                    "speaker_name": member.get("name") or "",
                    "backend": reply.get("backend"), "model": reply.get("model"),
                    "usage": reply.get("usage") or {},
                })
                store.touch_thread(thread_id, preview=f"{member.get('name')}: {text}"[:280])
                await self.emit("message.assistant", {
                    "id": message["id"], "text": text,
                    "speaker_id": member["id"],
                    "speaker_name": member.get("name") or "",
                    "usage": reply.get("usage") or {},
                    "model": reply.get("model"), "backend": reply.get("backend"),
                }, thread_id=thread_id, agent_id=member["id"])
                spoke = True
                self._nudge_mentions(thread_id, group, member, text)
                if not calls:
                    await self.notify(thread_id, group,
                                      str(member.get("name") or "Agent"),
                                      text, tag=thread_id)
            if not calls:
                if not spoke and not text:
                    await self.emit("group.quiet", {"agent_id": member["id"]},
                                    thread_id=thread_id, agent_id=member["id"], persist=False)
                return
            for call in calls:
                if cancel.is_set():
                    return
                await self._run_tool_call(thread_id, member, ctx, call)
            items = [
                models.user_message("Recent group chat:\n" + self._group_transcript(thread_id)
                                    + "\n\nContinue as yourself. If you already "
                                      "reacted or answered, stop. Do not narrate a reaction.")
            ]
            member_tool_rows = [
                row for row in store.list_messages(thread_id)
                if row["role"] in {"tool_call", "tool_result"}
                and str((row.get("meta") or {}).get("speaker_id") or "") == member["id"]
            ]
            for row in _complete_tool_rows(member_tool_rows)[-12:]:
                meta = row.get("meta") or {}
                if row["role"] == "tool_call":
                    items.append(models.tool_call(str(meta.get("call_id") or ""),
                                                  str(meta.get("name") or ""),
                                                  str(meta.get("arguments") or "{}")))
                elif row["role"] == "tool_result":
                    items.append(models.tool_result(str(meta.get("call_id") or ""),
                                                    str(meta.get("output") or "")))

    async def start_private_work(self, ctx: tools_mod.ToolContext, task: str) -> tools_mod.ToolResult:
        """Move a group-chat task into that agent's private thread with Calle."""
        agent = ctx.agent
        group_thread = store.get_thread(ctx.thread_id) or {}
        group = store.get_agent(str(group_thread.get("agent_id") or "")) or {}
        private = store.latest_thread(str(agent["id"])) or store.create_thread(str(agent["id"]))
        group_name = str(group.get("name") or "group")
        brief = (
            f"[From the group chat “{group_name}”]\n\n{task.strip()}\n\n"
            "Calle asked this in the group. Do the work in this private thread. "
            "Do not wait for extra confirmation. A short note will go back to the "
            "group when you finish. If you are already working on something related, "
            "fold this in rather than starting over."
        )
        existing = self._private_from_group.get(private["id"])
        since = store.latest_message_sequence(private["id"]) + 1
        if existing:
            existing["jobs"] = int(existing.get("jobs") or 1) + 1
            existing["task"] = task.strip()[:240]
            existing["since_sequence"] = min(int(existing.get("since_sequence") or since), since)
            existing["group_thread_id"] = ctx.thread_id
            existing["group_id"] = str(group.get("id") or "")
        else:
            self._private_from_group[private["id"]] = {
                "group_thread_id": ctx.thread_id,
                "group_id": str(group.get("id") or ""),
                "agent_id": str(agent["id"]),
                "name": str(agent.get("name") or ""),
                "task": task.strip()[:240],
                "jobs": 1,
                "since_sequence": since,
            }
        await self.emit("group.working", {
            "agent_id": agent["id"],
            "name": agent.get("name") or "",
            "private_thread_id": private["id"],
            "task": task.strip()[:120],
        }, thread_id=ctx.thread_id, agent_id=agent["id"])
        await self.send_message(private["id"], brief)
        return tools_mod.ToolResult(
            output=f"Work is running in your private chat ({private['id']}).",
            card={"title": "working", "preview": task.strip()[:90],
                  "meta": str(agent.get("name") or ""), "tone": "accent",
                  "agent_id": agent["id"], "private_thread_id": private["id"]},
        )

    async def post_reaction(self, ctx: tools_mod.ToolContext, emoji: str) -> tools_mod.ToolResult:
        """Drop a reaction chip on the latest user/assistant message."""
        from .tools.group import normalize_react
        chosen = normalize_react(emoji)
        agent = ctx.agent
        target_id = ""
        for row in reversed(store.list_messages(ctx.thread_id)):
            if row.get("role") not in {"user", "assistant"}:
                continue
            if str((row.get("meta") or {}).get("kind") or "") == "work_done":
                continue
            target_id = str(row.get("id") or "")
            break
        store.add_message(ctx.thread_id, "reaction", chosen, {
            "speaker_id": agent["id"],
            "speaker_name": agent.get("name") or "",
            "emoji": chosen,
            "target_id": target_id,
        })
        await self.emit("message.reaction", {
            "emoji": chosen,
            "text": chosen,
            "speaker_id": agent["id"],
            "speaker_name": agent.get("name") or "",
            "target_id": target_id,
        }, thread_id=ctx.thread_id, agent_id=agent["id"])
        return tools_mod.ToolResult(output=f"reacted {chosen}")

    async def _report_group_work(self, thread_id: str, agent: dict) -> None:
        origin = self._private_from_group.get(thread_id)
        if not origin:
            return
        if self.busy(thread_id):
            return
        messages = store.list_messages(thread_id)
        since = int(origin.get("since_sequence") or 0)
        after = [row for row in messages if int(row.get("sequence") or 0) >= since]
        if not after or after[-1]["role"] == "user":
            return
        preview = ""
        for row in reversed(after):
            if row["role"] == "assistant" and str(row.get("content") or "").strip():
                preview = str(row["content"]).strip()
                break
        if not preview:
            return
        if self._private_from_group.get(thread_id) is not origin:
            return
        self._private_from_group.pop(thread_id, None)
        group_thread_id = str(origin.get("group_thread_id") or "")
        if not group_thread_id:
            return
        message = store.add_message(group_thread_id, "assistant", preview[:500], {
            "speaker_id": agent["id"],
            "speaker_name": agent.get("name") or "",
            "kind": "work_done",
            "private_thread_id": thread_id,
        })
        store.touch_thread(group_thread_id,
                           preview=f"{agent.get('name')}: {preview}"[:280])
        await self.emit("message.assistant", {
            "id": message["id"], "text": preview[:500],
            "kind": "work_done",
            "speaker_id": agent["id"],
            "speaker_name": agent.get("name") or "",
            "private_thread_id": thread_id,
        }, thread_id=group_thread_id, agent_id=agent["id"])
        await self.emit("group.idle", {
            "agent_id": agent["id"],
            "private_thread_id": thread_id,
        }, thread_id=group_thread_id, agent_id=agent["id"])
        if not self.busy(group_thread_id):
            store.touch_thread(group_thread_id, status="idle")
            await self.emit("thread.status", {"status": "idle"},
                            thread_id=group_thread_id, agent_id=origin.get("group_id") or "")

    async def run_turn(self, thread_id: str, *, trigger: str = "user") -> None:
        if self.busy(thread_id):
            return
        self._cancels[thread_id] = asyncio.Event()
        task = asyncio.create_task(self._turn(thread_id, trigger))
        self._turn_tasks[thread_id] = task

    async def _turn(self, thread_id: str, trigger: str) -> None:
        thread = store.get_thread(thread_id)
        if not thread:
            return
        agent = store.get_agent(thread["agent_id"])
        if not agent:
            return
        source_note = ""
        messages = store.list_messages(thread_id)
        if messages:
            latest = messages[-1]
            latest_meta = latest.get("meta") or {}
            if latest.get("role") == "user" and latest_meta.get("kind") == "agent_message":
                source_note = (
                    f"This turn was sent internally by {latest_meta.get('sender_name') or 'another agent'}. "
                    "Treat it as an agent-to-agent request, not as words Calle typed. "
                    "Reply in this destination chat so Calle can inspect the exchange."
                )
        if store.is_group(agent):
            self._turn_tasks.pop(thread_id, None)
            await self.run_group_round(thread_id, trigger=trigger)
            return
        settings = config.load_settings()
        cancel = self.cancel_event(thread_id)
        store.touch_thread(thread_id, status="running")
        await self.emit("thread.status", {"status": "running", "trigger": trigger},
                        thread_id=thread_id, agent_id=agent["id"])

        ctx = tools_mod.ToolContext(
            agent=agent, thread_id=thread_id, settings=settings,
            emit=lambda kind, payload: self.emit(kind, payload, thread_id=thread_id,
                                                 agent_id=agent["id"]),
            request_approval=lambda **kw: self.request_approval(thread_id, agent["id"], **kw),
            ask_user=lambda question, **kw: self.ask_user(thread_id, agent["id"], question, **kw),
            cancel=cancel, hub=self,
            depth=int(self._relay_depth.get(thread_id, 0)))

        allowed = agents_mod.tools_for(agent)
        schemas = tools_mod.schemas(allowed)

        try:
            await asyncio.wait_for(
                self._tool_rounds(thread_id, agent, ctx, schemas, settings, cancel,
                                  source_note=source_note),
                timeout=TURN_TIMEOUT)
        except asyncio.TimeoutError:
            await self.emit("thread.error", {"error": "the turn ran past its time limit"},
                            thread_id=thread_id, agent_id=agent["id"])
        except models.ModelError as exc:
            await self.emit("thread.error", {"error": str(exc)},
                            thread_id=thread_id, agent_id=agent["id"])
        except asyncio.CancelledError:
            await self.emit("thread.status", {"status": "stopped"},
                            thread_id=thread_id, agent_id=agent["id"])
            raise
        except Exception as exc:
            await self.emit("thread.error", {"error": f"{type(exc).__name__}: {exc}"},
                            thread_id=thread_id, agent_id=agent["id"])
        finally:
            store.touch_thread(thread_id, status="idle")
            await self.emit("thread.status", {"status": "idle"},
                            thread_id=thread_id, agent_id=agent["id"])
            self._turn_tasks.pop(thread_id, None)
            # "Approve everything" granted for one run expires with that run.
            self._run_approval.pop(agent["id"], None)
            self._relay_depth.pop(thread_id, None)

        # Mid-run messages are already in the transcript, so the loop above
        # may have answered them. Only start a fresh turn if the last thing
        # in the thread is still an unanswered user message.
        self._queued.pop(thread_id, None)
        messages = store.list_messages(thread_id)
        if messages and messages[-1]["role"] == "user" and not self.busy(thread_id):
            await self.run_turn(thread_id, trigger="queued")
        else:
            await self._report_group_work(thread_id, agent)

    async def _tool_rounds(self, thread_id: str, agent: dict, ctx: tools_mod.ToolContext,
                           schemas: list[dict], settings: dict,
                           cancel: asyncio.Event, *, source_note: str = "") -> None:
        for _round in range(MAX_TOOL_ROUNDS):
            if cancel.is_set():
                return
            items = build_items(thread_id)
            instructions = agents_mod.system_prompt(agent, settings)
            if source_note:
                instructions += "\n\n" + source_note

            buffer: list[str] = []

            async def on_delta(delta: str) -> None:
                buffer.append(delta)
                await self.emit("message.delta", {"text": delta}, thread_id=thread_id,
                                agent_id=agent["id"], persist=False)

            async def on_reasoning(delta: str) -> None:
                await self.emit("reasoning.delta", {"text": delta}, thread_id=thread_id,
                                agent_id=agent["id"], persist=False)

            reply = await models.complete(
                backend=str(agent.get("backend") or ""),
                model=str(agent.get("model") or ""),
                reasoning=str(agent.get("reasoning") or ""),
                instructions=instructions, items=items, tools=schemas,
                timeout=240.0, on_delta=on_delta, on_reasoning=on_reasoning,
                settings=settings)

            text = str(reply.get("text") or "").strip()
            calls = reply.get("tool_calls") or []

            if text:
                message = store.add_message(thread_id, "assistant", text, {
                    "backend": reply.get("backend"), "model": reply.get("model"),
                    "usage": reply.get("usage") or {},
                    "reasoning": str(reply.get("reasoning") or "")[:4000],
                })
                store.touch_thread(thread_id, preview=text[:280])
                await self.emit("message.assistant", {
                    "id": message["id"], "text": text,
                    "usage": reply.get("usage") or {},
                    "model": reply.get("model"), "backend": reply.get("backend"),
                    "reasoning": str(reply.get("reasoning") or "")[:4000],
                }, thread_id=thread_id, agent_id=agent["id"])
                if not calls:
                    # Only the finished answer buzzes the phone, not each step.
                    await self.notify(thread_id, agent, str(agent.get("name") or "Director"),
                                      text, tag=thread_id)

            if not calls:
                if not text:
                    await self.emit("thread.error",
                                    {"error": "the model returned nothing"},
                                    thread_id=thread_id, agent_id=agent["id"])
                return

            for call in calls:
                if cancel.is_set():
                    return
                await self._run_tool_call(thread_id, agent, ctx, call)


    async def _run_tool_call(self, thread_id: str, agent: dict,
                             ctx: tools_mod.ToolContext, call: dict) -> None:
        name = str(call.get("name") or "")
        call_id = str(call.get("call_id") or "")
        raw_args = str(call.get("arguments") or "{}")
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}

        store.add_message(thread_id, "tool_call", "", {
            "call_id": call_id, "name": name, "arguments": raw_args,
            **({"speaker_id": agent["id"]} if getattr(ctx, "source", "") == "group" else {}),
        })
        quiet = getattr(ctx, "source", "") == "group" and name in GROUP_QUIET_TOOLS
        if not quiet:
            await self.emit("tool.start", {"call_id": call_id, "name": name, "arguments": args},
                            thread_id=thread_id, agent_id=agent["id"])

        tool = tools_mod.get(name)
        image = ""
        try:
            if tool is None:
                output = f"unknown tool: {name}"
                card = {"title": name, "preview": "unknown tool", "meta": "", "tone": "danger"}
            else:
                gate = (ctx.settings.get("safety", {}) or {}).get("confirm_destructive", True)
                if tool.destructive and gate and not self.auto_approved(agent, ctx.settings):
                    summary = (tool.approval_summary(args) if tool.approval_summary
                               else f"Run {name}")
                    decision = await ctx.request_approval(
                        tool=name, summary=summary, detail=json.dumps(args, indent=2)[:1500],
                        payload=args)
                    if str(decision.get("status")) != "approved":
                        output = f"declined by Calle: {decision.get('note') or 'no reason given'}"
                        card = {"title": name, "preview": summary[:90], "meta": "declined",
                                "tone": "danger"}
                        await self._finish_tool(thread_id, agent, call_id, name, output, card,
                                                source=getattr(ctx, "source", ""))
                        return
                try:
                    result = await tool.run(ctx, **args)
                except TypeError as exc:
                    result = tools_mod.ToolResult(error=f"bad arguments for {name}: {exc}")
                except Exception as exc:
                    result = tools_mod.ToolResult(error=f"{type(exc).__name__}: {exc}")
                output = result.as_output()
                card = result.card or {"title": name, "preview": "", "meta": "", "tone": "ok"}
                if result.error and not card.get("tone"):
                    card["tone"] = "danger"
                image = str(result.image or "")
        except asyncio.CancelledError:
            card = {"title": name, "preview": "interrupted", "meta": "cancelled",
                    "tone": "danger"}
            await self._finish_tool(thread_id, agent, call_id, name,
                                    INTERRUPTED_TOOL_OUTPUT, card,
                                    source=getattr(ctx, "source", ""))
            raise

        await self._finish_tool(thread_id, agent, call_id, name, output, card, image=image,
                                source=getattr(ctx, "source", ""))

    async def _finish_tool(self, thread_id: str, agent: dict, call_id: str, name: str,
                           output: str, card: dict, *, image: str = "", source: str = "") -> None:
        meta = {"call_id": call_id, "name": name, "output": output, "card": card}
        if image:
            meta["image"] = image
        if source == "group":
            meta["speaker_id"] = agent["id"]
        store.add_message(thread_id, "tool_result", "", meta)
        quiet = source == "group" and name in GROUP_QUIET_TOOLS
        if not quiet:
            payload = {"call_id": call_id, "name": name, "card": card}
            if image:
                payload["image"] = image
            await self.emit("tool.done", payload,
                            thread_id=thread_id, agent_id=agent["id"])


def _complete_tool_rows(rows: list[dict]) -> list[dict]:
    """Keep every stored function call and output as one protocol-safe pair.

    A user can send another message while an interactive tool is waiting, and a
    process restart can permanently interrupt that tool. Model APIs reject both
    an interleaved output and a call with no output, so pair known results with
    their calls and synthesize a cancellation result for unfinished calls.
    Result rows whose call is outside this history window are omitted.
    """
    results: dict[str, dict] = {}
    for row in rows:
        if row.get("role") != "tool_result":
            continue
        call_id = str((row.get("meta") or {}).get("call_id") or "")
        if call_id and call_id not in results:
            results[call_id] = row

    completed: list[dict] = []
    for row in rows:
        role = row.get("role")
        if role == "tool_result":
            continue
        if role != "tool_call":
            completed.append(row)
            continue
        meta = row.get("meta") or {}
        call_id = str(meta.get("call_id") or "")
        if not call_id:
            continue
        completed.append(row)
        result = results.get(call_id)
        if result is not None:
            completed.append(result)
        else:
            completed.append({
                "role": "tool_result",
                "content": "",
                "meta": {
                    "call_id": call_id,
                    "name": str(meta.get("name") or ""),
                    "output": INTERRUPTED_TOOL_OUTPUT,
                    **({"speaker_id": meta["speaker_id"]} if meta.get("speaker_id") else {}),
                },
            })
    return completed


def build_items(thread_id: str) -> list[dict]:
    """Rebuild the model's item list from stored messages."""
    items: list[dict] = []
    thread = store.get_thread(thread_id) or {}
    summary = str(thread.get("summary") or "").strip()
    compacted_through = int(thread.get("compacted_through") or 0)
    if summary and compacted_through:
        items.append(models.user_message(
            "[Compacted conversation context]\n" + summary))
    rows = store.list_messages(thread_id, after_sequence=compacted_through)
    for row in _complete_tool_rows(rows):
        role = row["role"]
        meta = row.get("meta") or {}
        if role == "user":
            parts = [models.text_part(row["content"])]
            for attachment in meta.get("attachments") or []:
                url = str(attachment.get("url") or attachment.get("data") or "")
                if url.startswith("data:image") or url.startswith("http"):
                    parts.append(models.image_part(url))
            items.append({"type": "message", "role": "user", "content": parts})
        elif role == "assistant":
            if row["content"]:
                items.append(models.assistant_message(row["content"]))
        elif role == "system":
            # Job results and other out-of-band notes read as user turns; the
            # backends only accept user/assistant roles in the item list.
            items.append(models.user_message(row["content"]))
        elif role == "tool_call":
            items.append(models.tool_call(str(meta.get("call_id") or ""),
                                          str(meta.get("name") or ""),
                                          str(meta.get("arguments") or "{}")))
        elif role == "tool_result":
            items.append(models.tool_result(str(meta.get("call_id") or ""),
                                            str(meta.get("output") or "")))
            image = str(meta.get("image") or "")
            if image.startswith("data:image") or image.startswith("http"):
                items.append({"type": "message", "role": "user", "content": [
                    models.text_part("Operator screenshot:"),
                    models.image_part(image),
                ]})
    return items


RUNTIME = Runtime()


def runtime() -> Runtime:
    return RUNTIME
