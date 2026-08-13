"""Tests for routines, approve-all and push.

The schedule maths is the part worth being strict about: a wrong `next_run`
either fires a reminder at the wrong time or, worse, loops it forever.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


# ---------------- schedule parsing ----------------

def test_daily_normalizes_and_pads_the_time():
    from director import routines

    assert routines.normalize({"kind": "daily", "time": "7:5"}) == {
        "kind": "daily", "time": "07:05"}


def test_weekly_accepts_a_weekday_name():
    from director import routines

    got = routines.normalize({"kind": "weekly", "time": "17:00", "weekday": "friday"})
    assert got["weekday"] == 4
    assert routines.normalize({"kind": "weekly", "time": "17:00", "weekday": "fri"})["weekday"] == 4


def test_bad_schedules_explain_themselves():
    from director import routines

    for bad in ({"kind": "sometimes"}, {"kind": "daily", "time": "25:00"},
                {"kind": "daily"}, {"kind": "interval", "seconds": 5},
                {"kind": "once"}):
        with pytest.raises(routines.ScheduleError):
            routines.normalize(bad)


def test_once_in_seconds_becomes_an_absolute_time():
    from director import routines

    got = routines.normalize({"kind": "once", "in_seconds": 120})
    assert got["kind"] == "once"
    assert 110 < got["at"] - time.time() < 130


def test_daily_next_run_is_always_in_the_future():
    """The bug this guards: computing "today at 08:00" when it is already 09:00
    returns a time in the past, and the scheduler then fires it every tick."""
    from director import routines

    schedule = routines.normalize({"kind": "daily", "time": "08:00"})
    for offset in (0, 3600, 12 * 3600, 23 * 3600):
        moment = time.time() + offset
        when = routines.next_run(schedule, after=moment)
        assert when > moment
        assert when - moment <= 86400 + 3600      # never further than a day out
        assert datetime.fromtimestamp(when, ZoneInfo("Europe/Stockholm")).hour == 8


def test_weekly_lands_on_the_right_weekday():
    from director import routines

    schedule = routines.normalize({"kind": "weekly", "time": "17:00", "weekday": 4})
    when = routines.next_run(schedule)
    local = datetime.fromtimestamp(when, ZoneInfo("Europe/Stockholm"))
    assert local.weekday() == 4
    assert local.hour == 17
    assert when > time.time()


def test_weekdays_never_lands_on_a_weekend():
    from director import routines

    schedule = routines.normalize({"kind": "weekdays", "time": "07:30"})
    moment = time.time()
    for _ in range(10):
        when = routines.next_run(schedule, after=moment)
        assert datetime.fromtimestamp(when, ZoneInfo("Europe/Stockholm")).weekday() <= 4
        moment = when


def test_wall_clock_schedules_ignore_the_server_timezone(monkeypatch):
    from director import routines

    # 06:15 UTC is 08:15 in Stockholm during summer. A daily 09:00 job must
    # therefore be 45 minutes away even if the host process itself uses UTC.
    moment = datetime(2026, 8, 13, 6, 15, tzinfo=ZoneInfo("UTC")).timestamp()
    monkeypatch.setenv("TZ", "UTC")
    when = routines.next_run({"kind": "daily", "time": "09:00"}, after=moment)
    local = datetime.fromtimestamp(when, ZoneInfo("Europe/Stockholm"))
    assert (local.hour, local.minute) == (9, 0)
    assert when - moment == 45 * 60


def test_interval_next_run_is_one_interval_out():
    from director import routines

    schedule = routines.normalize({"kind": "interval", "seconds": 600})
    moment = time.time()
    assert abs(routines.next_run(schedule, after=moment) - (moment + 600)) < 1


def test_one_off_is_not_recurring():
    from director import routines

    assert routines.is_recurring({"kind": "daily"}) is True
    assert routines.is_recurring({"kind": "once"}) is False


def test_describe_reads_like_a_person_wrote_it():
    from director import routines

    assert routines.describe({"kind": "daily", "time": "08:00"}) == "every day at 08:00"
    assert routines.describe({"kind": "weekly", "time": "17:00", "weekday": 4}) \
        == "every Friday at 17:00"
    assert routines.describe({"kind": "interval", "seconds": 7200}) == "every 2 hours"
    assert routines.describe({"kind": "weekdays", "time": "07:30"}) \
        == "every weekday at 07:30"


# ---------------- storage ----------------

def test_routines_round_trip(director):
    from director import routines

    schedule = routines.normalize({"kind": "daily", "time": "09:00"})
    row = director.create_routine(agent_id="agt_director", name="digest",
                                  prompt="tell me the news", schedule=schedule,
                                  next_run=routines.next_run(schedule))
    assert row["schedule"] == schedule
    assert director.get_routine(row["id"])["name"] == "digest"
    assert [r["id"] for r in director.list_routines(agent_id="agt_director")] == [row["id"]]


def test_only_due_routines_come_back(director):
    now = time.time()
    early = director.create_routine(agent_id="a", name="due", prompt="p",
                                    schedule={"kind": "daily"}, next_run=now - 10)
    director.create_routine(agent_id="a", name="later", prompt="p",
                            schedule={"kind": "daily"}, next_run=now + 3600)
    due = director.due_routines(now)
    assert [r["id"] for r in due] == [early["id"]]


def test_a_disabled_routine_is_never_due(director):
    now = time.time()
    row = director.create_routine(agent_id="a", name="paused", prompt="p",
                                  schedule={"kind": "daily"}, next_run=now - 10)
    director.update_routine(row["id"], {"enabled": False})
    assert director.due_routines(now) == []


# ---------------- approve-all ----------------

def test_auto_approve_reads_all_three_grants(director, monkeypatch):
    from director import config
    from director import runtime as runtime_mod

    runtime = runtime_mod.Runtime()
    agent = {"id": "agt_x", "auto_approve": 0}
    settings = config.load_settings()

    assert runtime.auto_approved(agent, settings) is False

    runtime.grant_run_approval("agt_x")
    assert runtime.auto_approved(agent, settings) is True

    runtime = runtime_mod.Runtime()
    assert runtime.auto_approved({"id": "agt_x", "auto_approve": 1}, settings) is True

    settings = {"safety": {"approve_all": True}}
    assert runtime.auto_approved({"id": "agt_y", "auto_approve": 0}, settings) is True


def test_run_scope_grant_is_per_agent(director):
    from director import config
    from director import runtime as runtime_mod

    runtime = runtime_mod.Runtime()
    runtime.grant_run_approval("agt_a")
    settings = config.load_settings()
    assert runtime.auto_approved({"id": "agt_a"}, settings) is True
    assert runtime.auto_approved({"id": "agt_b"}, settings) is False


def test_agent_scope_persists_on_the_agent(director):
    from director import agents, runtime as runtime_mod, store

    agents.ensure_seeded()
    runtime = runtime_mod.Runtime()
    record = store.create_approval(thread_id="t", agent_id="agt_director",
                                   tool="shell", summary="rm")
    runtime.decide_approval(record["id"], "approved", scope="agent")
    assert store.get_agent("agt_director")["auto_approve"] == 1


def test_declining_never_grants_anything(director):
    from director import config, runtime as runtime_mod, store

    runtime = runtime_mod.Runtime()
    record = store.create_approval(thread_id="t", agent_id="agt_z", tool="shell",
                                   summary="rm -rf /")
    runtime.decide_approval(record["id"], "declined", scope="all")
    assert config.load_settings(refresh=True)["safety"]["approve_all"] is False


# ---------------- push ----------------

def test_push_payload_is_trimmed_for_the_service(director, monkeypatch):
    from director import push

    if not push.AVAILABLE:
        pytest.skip("pywebpush is not installed on this machine")

    sent = {}
    monkeypatch.setattr(push, "_send_one",
                        lambda sub, payload, keys: (sent.update(payload) or (True, "")))
    director.add_push_subscription({"endpoint": "https://push.example/abc",
                                    "keys": {"p256dh": "x", "auth": "y"}})
    push.send_sync("a title", "b" * 900)
    assert len(sent["body"]) <= push.MAX_BODY


def test_the_signer_is_a_vapid_object_not_pem_text(director):
    """Regression: pywebpush takes a Vapid instance, a path to a PEM file, or a
    base64url raw key — never PEM contents. Passing the text failed deep inside
    cryptography with "ASN.1 parsing error", which reads like a corrupt key
    rather than the wrong argument type, and every notification silently died."""
    from director import push

    if not push.AVAILABLE:
        pytest.skip("pywebpush is not installed on this machine")

    keys = push.ensure_keys()
    signer = push._signer(keys)
    assert not isinstance(signer, (str, bytes))
    assert hasattr(signer, "sign"), "the signer must be a Vapid object"


def test_a_gone_subscription_is_forgotten(director):
    director.add_push_subscription({"endpoint": "https://push.example/gone"})
    assert len(director.list_push_subscriptions()) == 1
    director.drop_push_subscription("https://push.example/gone")
    assert director.list_push_subscriptions() == []


def test_subscriptions_are_unique_per_endpoint(director):
    director.add_push_subscription({"endpoint": "https://push.example/one", "v": 1})
    director.add_push_subscription({"endpoint": "https://push.example/one", "v": 2})
    rows = director.list_push_subscriptions()
    assert len(rows) == 1 and rows[0]["subscription"]["v"] == 2


def test_the_private_push_key_never_leaves_the_box(director):
    """/api/settings is read by the phone; the VAPID private key must not be
    in what it gets back."""
    import inspect

    from director import server

    source = inspect.getsource(server.get_settings)
    assert 'pop("private_pem"' in source
