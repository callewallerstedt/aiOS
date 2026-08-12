"""Routines: things Director does without being asked.

A routine is a prompt plus a schedule. When it comes due, the prompt is dropped
into that agent's thread exactly as if Calle had typed it, and the agent runs a
normal turn — so a routine can use every tool, ask a question, or dispatch the
operator, and the answer appears in the chat he already reads.

Schedules are stored as small dicts rather than cron strings, because the model
writes them and a dict it can get right beats a five-field string it cannot:

    {"kind": "daily",    "time": "08:00"}
    {"kind": "weekly",   "time": "17:00", "weekday": 4}      # 0=Mon … 6=Sun
    {"kind": "weekdays", "time": "07:30"}                     # Mon-Fri
    {"kind": "interval", "seconds": 3600}
    {"kind": "once",     "at": 1786550000.0}
    {"kind": "once",     "in_seconds": 1800}

Times are local to the box, which is where Calle lives.
"""
from __future__ import annotations

import calendar
import time
from typing import Any

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
KINDS = ("daily", "weekly", "weekdays", "interval", "once")
MIN_INTERVAL = 60.0


class ScheduleError(ValueError):
    """The schedule cannot be understood; the message is shown to the model."""


def parse_time(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("a time like \"08:00\" is required")
    parts = text.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as exc:
        raise ScheduleError(f"could not read the time {text!r}; use \"HH:MM\"") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"{text!r} is not a real time of day")
    return hour, minute


def parse_weekday(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        day = int(value)
        if 0 <= day <= 6:
            return day
        raise ScheduleError("weekday must be 0 (Monday) to 6 (Sunday)")
    text = str(value or "").strip().lower()
    for index, name in enumerate(WEEKDAYS):
        if text == name or text == name[:3]:
            return index
    raise ScheduleError(f"{value!r} is not a weekday")


def normalize(schedule: Any) -> dict:
    """Validate a schedule and put it in canonical form."""
    if not isinstance(schedule, dict):
        raise ScheduleError("schedule must be an object")
    kind = str(schedule.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ScheduleError(f"kind must be one of {', '.join(KINDS)}")

    if kind in ("daily", "weekdays"):
        hour, minute = parse_time(schedule.get("time"))
        return {"kind": kind, "time": f"{hour:02d}:{minute:02d}"}
    if kind == "weekly":
        hour, minute = parse_time(schedule.get("time"))
        return {"kind": kind, "time": f"{hour:02d}:{minute:02d}",
                "weekday": parse_weekday(schedule.get("weekday"))}
    if kind == "interval":
        try:
            seconds = float(schedule.get("seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise ScheduleError("seconds must be a number") from exc
        if seconds < MIN_INTERVAL:
            raise ScheduleError(f"the shortest interval is {int(MIN_INTERVAL)} seconds")
        return {"kind": "interval", "seconds": seconds}

    # once
    if schedule.get("at"):
        try:
            at = float(schedule["at"])
        except (TypeError, ValueError) as exc:
            raise ScheduleError("at must be a unix timestamp") from exc
        return {"kind": "once", "at": at}
    try:
        in_seconds = float(schedule.get("in_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("in_seconds must be a number") from exc
    if in_seconds <= 0:
        raise ScheduleError("a one-off needs either `at` or a positive `in_seconds`")
    return {"kind": "once", "at": time.time() + in_seconds}


def _at_time_on(day: time.struct_time, hour: int, minute: int) -> float:
    return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0,
                        day.tm_wday, day.tm_yday, -1))


def next_run(schedule: dict, *, after: float | None = None) -> float:
    """When this schedule next fires, as a unix timestamp.

    `after` is exclusive, so a routine that just ran never re-fires in the same
    second — which would otherwise loop a daily job forever.
    """
    moment = after if after is not None else time.time()
    kind = str(schedule.get("kind") or "")

    if kind == "once":
        return float(schedule.get("at") or 0)

    if kind == "interval":
        return moment + float(schedule.get("seconds") or MIN_INTERVAL)

    hour, minute = parse_time(schedule.get("time"))

    if kind == "daily":
        for offset in range(0, 3):
            day = time.localtime(moment + offset * 86400)
            candidate = _at_time_on(day, hour, minute)
            if candidate > moment:
                return candidate
        return moment + 86400

    if kind == "weekdays":
        for offset in range(0, 8):
            day = time.localtime(moment + offset * 86400)
            if day.tm_wday > 4:            # Saturday or Sunday
                continue
            candidate = _at_time_on(day, hour, minute)
            if candidate > moment:
                return candidate
        return moment + 86400

    if kind == "weekly":
        target = int(schedule.get("weekday") or 0)
        for offset in range(0, 15):
            day = time.localtime(moment + offset * 86400)
            if day.tm_wday != target:
                continue
            candidate = _at_time_on(day, hour, minute)
            if candidate > moment:
                return candidate
        return moment + 7 * 86400

    raise ScheduleError(f"unknown schedule kind {kind!r}")


def describe(schedule: dict) -> str:
    """A line a human reads on the phone."""
    kind = str(schedule.get("kind") or "")
    if kind == "daily":
        return f"every day at {schedule.get('time')}"
    if kind == "weekdays":
        return f"every weekday at {schedule.get('time')}"
    if kind == "weekly":
        day = WEEKDAYS[int(schedule.get("weekday") or 0)].capitalize()
        return f"every {day} at {schedule.get('time')}"
    if kind == "interval":
        seconds = float(schedule.get("seconds") or 0)
        if seconds >= 3600 and seconds % 3600 == 0:
            hours = int(seconds // 3600)
            return f"every {hours} hour{'s' if hours != 1 else ''}"
        minutes = max(1, int(seconds // 60))
        return f"every {minutes} minute{'s' if minutes != 1 else ''}"
    if kind == "once":
        return "once, at " + time.strftime("%a %d %b %H:%M",
                                           time.localtime(float(schedule.get("at") or 0)))
    return "unscheduled"


def is_recurring(schedule: dict) -> bool:
    return str(schedule.get("kind") or "") != "once"


def humanize_next(timestamp: float) -> str:
    if not timestamp:
        return "never"
    delta = timestamp - time.time()
    if delta < 0:
        return "due now"
    if delta < 3600:
        return f"in {max(1, int(delta // 60))} min"
    if delta < 86400:
        return f"in {int(delta // 3600)} h"
    return time.strftime("%a %d %b %H:%M", time.localtime(timestamp))
