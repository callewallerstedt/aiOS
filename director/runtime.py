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
                          f"{agent.get('name', 'Director')} needs you",
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
                          f"{agent.get('name', 'Director')} needs approval",
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

    def detach_machine(self, machine_id: str) -> None:
        self._machines.pop(machine_id, None)
        store.set_machine_online(machine_id, False)

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
        self.cancel_event(thread_id).set()
        task = self._turn_tasks.get(thread_id)
        if task and not task.done():
            return True
        return False

    def busy(self, thread_id: str) -> bool:
        task = self._turn_tasks.get(thread_id)
        return bool(task and not task.done())

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
        if self.busy(thread_id):
            # Mid-run steering: the message is already in the transcript, so the
            # next model round picks it up on its own.
            self._queued.setdefault(thread_id, []).append(message)
            await self.emit("thread.steered", {"text": text}, thread_id=thread_id,
                            agent_id=thread["agent_id"], persist=False)
            return message
        await self.run_turn(thread_id, trigger="user")
        return message

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
            cancel=cancel, hub=self)

        allowed = agents_mod.tools_for(agent)
        schemas = tools_mod.schemas(allowed)

        try:
            await asyncio.wait_for(
                self._tool_rounds(thread_id, agent, ctx, schemas, settings, cancel),
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

        # Mid-run messages are already in the transcript, so the loop above
        # may have answered them. Only start a fresh turn if the last thing
        # in the thread is still an unanswered user message.
        self._queued.pop(thread_id, None)
        messages = store.list_messages(thread_id)
        if messages and messages[-1]["role"] == "user" and not self.busy(thread_id):
            await self.run_turn(thread_id, trigger="queued")

    async def _tool_rounds(self, thread_id: str, agent: dict, ctx: tools_mod.ToolContext,
                           schemas: list[dict], settings: dict,
                           cancel: asyncio.Event) -> None:
        for _round in range(MAX_TOOL_ROUNDS):
            if cancel.is_set():
                return
            items = build_items(thread_id)
            instructions = agents_mod.system_prompt(agent, settings)

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
            "call_id": call_id, "name": name, "arguments": raw_args})
        await self.emit("tool.start", {"call_id": call_id, "name": name, "arguments": args},
                        thread_id=thread_id, agent_id=agent["id"])

        tool = tools_mod.get(name)
        image = ""
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
                    await self._finish_tool(thread_id, agent, call_id, name, output, card)
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

        await self._finish_tool(thread_id, agent, call_id, name, output, card, image=image)

    async def _finish_tool(self, thread_id: str, agent: dict, call_id: str, name: str,
                           output: str, card: dict, *, image: str = "") -> None:
        meta = {"call_id": call_id, "name": name, "output": output, "card": card}
        if image:
            meta["image"] = image
        store.add_message(thread_id, "tool_result", "", meta)
        await self.emit("tool.done", {"call_id": call_id, "name": name, "card": card},
                        thread_id=thread_id, agent_id=agent["id"])


def build_items(thread_id: str) -> list[dict]:
    """Rebuild the model's item list from stored messages."""
    items: list[dict] = []
    thread = store.get_thread(thread_id) or {}
    summary = str(thread.get("summary") or "").strip()
    compacted_through = int(thread.get("compacted_through") or 0)
    if summary and compacted_through:
        items.append(models.user_message(
            "[Compacted conversation context]\n" + summary))
    for row in store.list_messages(thread_id, after_sequence=compacted_through):
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
