import asyncio
import datetime as dt
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nanocode as nc  # noqa: E402
from nanokit import cron  # noqa: E402

NOW = dt.datetime(2026, 11, 26, 10, 0)  # a Thursday


def when(schedule):
    return dt.datetime.fromtimestamp(cron.next_run(cron.parse_schedule(schedule, NOW), NOW.timestamp()))


def test_cron_expressions():
    assert cron.next_cron("*/15 * * * *", NOW) == dt.datetime(2026, 11, 26, 10, 15)
    assert cron.next_cron("0 9 * * 1-5", NOW) == dt.datetime(2026, 11, 27, 9, 0)
    assert cron.next_cron("30 8 1 jan,jul *", NOW) == dt.datetime(2027, 1, 1, 8, 30)
    assert cron.next_cron("0 0 * * sun", NOW) == dt.datetime(2026, 11, 29, 0, 0)
    assert cron.next_cron("0 0 * * 7", NOW) == dt.datetime(2026, 11, 29, 0, 0)       # 7 is Sunday too
    assert cron.next_cron("0 12 13 * fri", NOW) == dt.datetime(2026, 11, 27, 12, 0)  # day OR weekday, like cron
    assert cron.next_cron("5-10/5 23 * dec *", NOW) == dt.datetime(2026, 12, 1, 23, 5)
    assert cron.next_cron("0 0 29 2 *", NOW) == dt.datetime(2028, 2, 29, 0, 0)
    for bad in ("* * * *", "61 * * * *", "* * * foo *", "*/0 * * * *", "5-1 * * * *"):
        with pytest.raises(cron.CronError):
            cron.parse_cron(bad)


def test_plain_english_schedules():
    assert when("every 15 minutes") == dt.datetime(2026, 11, 26, 10, 15)
    assert when("every day at 8:00") == dt.datetime(2026, 11, 27, 8, 0)
    assert when("every monday at 9am") == dt.datetime(2026, 11, 30, 9, 0)
    assert when("weekdays at 18:30") == dt.datetime(2026, 11, 26, 18, 30)
    assert when("in 2 hours") == dt.datetime(2026, 11, 26, 12, 0)
    assert when("tomorrow at 7") == dt.datetime(2026, 11, 27, 7, 0)
    assert when("at 7pm") == dt.datetime(2026, 11, 26, 19, 0)
    assert when("every morning") == dt.datetime(2026, 11, 27, 8, 0)
    assert when("mondays and fridays at 10") == dt.datetime(2026, 11, 27, 10, 0)
    assert when("every weekend at 10am") == dt.datetime(2026, 11, 28, 10, 0)
    assert when("every 45 minutes") == dt.datetime(2026, 11, 26, 10, 45)
    assert when("every hour") == dt.datetime(2026, 11, 26, 11, 0)
    assert when("every 6 hours") == dt.datetime(2026, 11, 26, 12, 0)
    assert when("every month") == dt.datetime(2026, 12, 1, 9, 0)
    assert when("every monday at 9 am") == dt.datetime(2026, 11, 30, 9, 0)
    assert cron.parse_schedule("in 2 hours", NOW).keys() == {"once"}
    for bad in ("whenever", "every fooday", "today at 9"):
        with pytest.raises(cron.CronError):
            cron.parse_schedule(bad, NOW)


def test_jobs_add_list_pause_resume_remove(sandbox):
    job = cron.add_job("every day at 8:00", "summarize the issues", deliver="telegram:42")
    assert job["workdir"] == str(Path.cwd().resolve()) and job["next_run"] > time.time()
    assert cron.jobs_file().stat().st_mode & 0o077 == 0 or sys.platform == "win32"
    assert "summarize the issues" in cron.list_text() and "telegram:42" in cron.list_text()
    assert cron.set_paused(job["id"][:4], True)["paused"]
    assert "paused" in cron.list_text()
    cron.set_paused(job["id"], False)
    with pytest.raises(cron.CronError):
        cron.add_job("every day", "x", deliver="carrier-pigeon")
    with pytest.raises(cron.CronError):
        cron.add_job("every day", " ")
    cron.remove_job(job["id"])
    assert cron.load_jobs() == []


def test_cron_command_and_cli(sandbox, capsys):
    text = asyncio.run(cron.cron_command("add weekdays at 18:30 | check CI -> slack:C1", deliver="notify"))
    assert text.startswith("Scheduled") and "slack:C1" in text
    assert asyncio.run(cron.cron_command("add weekdays | no bar")).startswith("Scheduled")
    assert asyncio.run(cron.cron_command("add nothing here")).startswith("error")
    assert asyncio.run(cron.cron_command("pause nope")).startswith("error")
    assert "/cron add" in asyncio.run(cron.cron_command("what"))
    assert cron.cron_cli(["add", "every 2 hours", "look", "around", "--to", "file"]) == 0
    assert any(j["prompt"] == "look around" and j["deliver"] == "file" for j in cron.load_jobs())
    assert cron.cron_cli(["list"]) == 0 and "look around" in capsys.readouterr().out


def test_due_jobs_run_once_and_deliver(sandbox, monkeypatch):
    ran, delivered = [], []

    async def execute(job):
        ran.append(job["id"])
        return True, f"answer for {job['prompt']}"

    async def deliver(platform, chat, text):
        delivered.append((platform, chat, text))

    from nanokit import gateway
    monkeypatch.setattr(cron, "execute", execute)
    monkeypatch.setattr(gateway, "deliver", deliver)
    daily = cron.add_job("every day at 8:00", "daily report", deliver="telegram:7")
    once = cron.add_job("in 1 minute", "one off", deliver="file")
    due = int(time.time() - 5)
    cron.update_jobs(lambda jobs: [j.update(next_run=due) for j in jobs])

    async def main():
        first = await cron.tick()
        second = await cron.tick()            # the same runs are claimed already: not again
        await asyncio.gather(*first)
        return first, second

    first, second = asyncio.run(main())
    assert len(first) == 2 and second == [] and sorted(ran) == sorted([daily["id"], once["id"]])
    assert delivered == [("telegram", "7", "⏰ daily report\n\nanswer for daily report")]
    jobs = cron.load_jobs()
    assert [j["id"] for j in jobs] == [daily["id"]]      # the one-off is done
    assert jobs[0]["last_status"] == "ok" and jobs[0]["next_run"] > time.time()
    results = list((cron.runs_dir() / once["id"]).glob("*.md"))
    assert len(results) == 1 and "answer for one off" in results[0].read_text()
    # another nanocode that still sees the old next_run can't claim that run either
    assert not cron.claim({"id": daily["id"], "next_run": due})


def test_execute_runs_a_headless_nanocode(sandbox, monkeypatch):
    seen = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"\x1b[1mThe answer\x1b[0m\n", b""

    async def spawn(*argv, **kw):
        seen.update(argv=argv, **kw)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    job = {"id": "abc", "prompt": "do it", "workdir": str(Path.cwd()), "skills": ["review"]}
    ok, text = asyncio.run(cron.execute(job))
    assert ok and text == "The answer"
    assert seen["argv"][2] == "-p" and "review" in seen["argv"][3] and "--yes" not in seen["argv"]
    assert seen["cwd"] == str(Path.cwd()) and seen["env"]["NANOCODE_CRON"] == "abc"
    nc.st.config["cron"] = {"permissions": "auto"}
    assert cron.job_command({**job, "permissions": "auto"})[-1] == "--yes"
    # a job made from a read-only chat (or an old one without a level) never gets --yes
    assert "--yes" not in cron.job_command({**job, "permissions": "read-only"}) + cron.job_command(job)
    nc.st.config["cron"] = {}
    assert "--yes" not in cron.job_command({**job, "permissions": "auto"})
    assert asyncio.run(cron.execute({**job, "workdir": "/no/such/folder"}))[0] is False


def test_cron_tool_asks_and_delivers_to_the_chat(sandbox, monkeypatch):
    asked = []

    async def ask(tool, preview, dangerous):
        asked.append((tool, dangerous))
        return True
    monkeypatch.setattr(nc, "ask_permission", ask)
    assert "cron" in nc.ALWAYS_ASK
    token = cron.DELIVER_TO.set("discord:55")
    try:
        text = asyncio.run(cron.tool_cron({"action": "add", "schedule": "every monday at 9", "prompt": "plan"}))
    finally:
        cron.DELIVER_TO.reset(token)
    assert "discord:55" in text and cron.load_jobs()[0]["deliver"] == "discord:55"
    assert asked == [("cron", True)]           # asked even in Auto mode
    assert "plan" in asyncio.run(cron.tool_cron({"action": "list"}))
    nc.st.auto_approve, nc.st.headless = False, True       # read-only: adding is refused
    assert asyncio.run(cron.tool_cron({"action": "add", "schedule": "every day", "prompt": "x"})) == nc.DENIED
    nc.st.auto_approve = True                  # headless (a gateway chat): always refused, even in Auto mode
    assert asyncio.run(cron.tool_cron({"action": "remove", "id": cron.load_jobs()[0]["id"]})) == nc.DENIED
    with pytest.raises(nc.ToolError):
        nc.st.headless = False
        asyncio.run(cron.tool_cron({"action": "add", "schedule": "sometime", "prompt": "x"}))


def test_notify_commands(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    argv = cron.notify_command("t", 'say "hi"; rm -rf /')
    assert argv[0] == "osascript" and argv[-1] == 'say "hi"; rm -rf /'   # an argument, never code


def test_dst_fall_back_never_goes_back_in_time(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        first = dt.datetime(2026, 11, 1, 1, 5).timestamp()   # 1:05 EDT; clocks go back at 2:00
        second = first + 3600                                  # 1:05 again, EST
        for expr in ("*/15 * * * *", "30 1 * * *", "* * * * *"):
            after = second
            for _ in range(5):                                 # the scheduler's loop: always forwards
                nxt = cron.next_run({"cron": expr}, after)
                assert nxt > after
                after = nxt
        assert cron.next_run({"cron": "*/15 * * * *"}, second) == second + 600   # the second 1:15
        assert cron.next_run({"cron": "0 2 * * *"}, first) == first + 2 * 3600 - 300    # 2:00 EST
        # spring forward (2026-03-08, 2:00 → 3:00): 2:30 doesn't exist, it runs once, after the jump
        before = dt.datetime(2026, 3, 8, 1, 0).timestamp()
        nxt = cron.next_run({"cron": "30 2 * * *"}, before)
        assert nxt > before and dt.datetime.fromtimestamp(nxt) == dt.datetime(2026, 3, 8, 3, 30)
        assert cron.next_run({"cron": "30 2 * * *"}, nxt) > nxt + 20 * 3600
        days = cron.parse_schedule("every 2 days at 9am", dt.datetime(2026, 3, 6, 10, 0))
        assert dt.datetime.fromtimestamp(cron.next_run(days, before)) == dt.datetime(2026, 3, 9, 9, 0)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_more_schedules():
    assert when("everyday at 8") == dt.datetime(2026, 11, 27, 8, 0)
    assert when("9 am every day") == dt.datetime(2026, 11, 27, 9, 0)
    assert when("at 11 every day") == dt.datetime(2026, 11, 26, 11, 0)
    assert when("mon-fri at 9") == dt.datetime(2026, 11, 27, 9, 0)
    assert cron.parse_schedule("mon-fri at 9", NOW) == {"cron": "0 9 * * 1,2,3,4,5"}
    assert cron.parse_schedule("fri-mon", NOW) == {"cron": "0 9 * * 0,1,5,6"}
    assert cron.parse_schedule("9am weekdays", NOW) == {"cron": "0 9 * * 1-5"}
    spec = cron.parse_schedule("every 2 days at 9am", NOW)       # 10:00 now: the first is tomorrow
    assert spec == {"days": 2, "at": [9, 0], "start": "2026-11-27"} and cron.describe(spec) == "every 2 days at 09:00"
    first = cron.next_run(spec, NOW.timestamp())
    assert dt.datetime.fromtimestamp(first) == dt.datetime(2026, 11, 27, 9, 0)
    assert dt.datetime.fromtimestamp(cron.next_run(spec, first)) == dt.datetime(2026, 11, 29, 9, 0)
    assert dt.datetime.fromtimestamp(cron.next_run(spec, first + 3600)) == dt.datetime(2026, 11, 29, 9, 0)
    assert dt.datetime.fromtimestamp(cron.next_run(cron.parse_schedule("every 2 weeks at 8", NOW),
                                                   NOW.timestamp())) == dt.datetime(2026, 11, 27, 8, 0)
    assert when("every 2 days") == dt.datetime(2026, 11, 28, 10, 0)


def test_a_lock_left_by_a_dead_run_doesnt_block_the_job(sandbox, monkeypatch):
    job = cron.add_job("every hour", "x")
    due = int(time.time() - 5)
    cron.update_jobs(lambda jobs: [j.update(next_run=due) for j in jobs])
    job["next_run"] = due
    path = cron.lock_path(job)
    path.parent.mkdir(parents=True)
    path.write_text('{"pid": 999999999, "started": %f}' % time.time())     # its nanocode died mid-run
    assert cron.claim(job)
    assert not cron.claim(job)                 # ours now, and we're alive
    path.write_text('{"pid": %d, "started": %f}' % (__import__("os").getpid(), time.time() - 86400))
    assert cron.claim(job)                     # older than any run can be
    path.write_text("999999999")               # the old format: just a pid
    assert cron.claim(job)

    async def crash(job):
        raise RuntimeError("the machine went down")
    monkeypatch.setattr(cron, "execute", crash)
    path.unlink()

    async def main():
        tasks = await cron.tick()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(main())
    assert cron.load_jobs()[0]["next_run"] > time.time()   # moved on before the run: not started again and again

    async def cancelled(job):
        raise asyncio.CancelledError
    monkeypatch.setattr(cron, "execute", cancelled)
    once = cron.add_job("in 1 minute", "y")
    once["next_run"] = due
    cron.update_jobs(lambda jobs: [j.update(next_run=due) for j in jobs if j["id"] == once["id"]])
    assert cron.claim(once)
    try:
        asyncio.run(cron.run_job(once))
    except asyncio.CancelledError:
        pass
    assert not cron.lock_path(once).exists()   # a cancelled run lets the next nanocode run it


def test_cron_runs_get_no_cron_tool(monkeypatch):
    monkeypatch.setenv("NANOCODE_CRON", "abc")
    assert not cron.tool_cron_offered()
    with pytest.raises(nc.ToolError):
        asyncio.run(cron.tool_cron({"action": "add", "schedule": "every day", "prompt": "more jobs"}))
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import nanocode as nc; from nanokit import cron; "
            "print('cron' in nc.TOOLS)" % str(Path(__file__).resolve().parent.parent))
    env = {**__import__("os").environ, "NANOCODE_CRON": "abc", "NANOCODE_HOME": str(Path(__file__).parent / "_none")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


def test_read_only_chats_cant_change_jobs(sandbox):
    def run(arg, **kw):
        return asyncio.run(cron.cron_command(arg, deliver="telegram:5", **kw))
    stranger = {"permissions": "read-only", "can_change": False, "targets": ["telegram:5", "file"]}
    assert run("add every day | rm -rf ~", **stranger).startswith("error") and cron.load_jobs() == []
    assert "No scheduled tasks" in run("list", **stranger)
    chat = {"permissions": "auto", "can_change": True, "targets": ["telegram:5", "file"]}
    assert run("add every day | x -> discord:9", **chat).startswith("error")   # only this chat
    assert run("add every day | x", **chat).startswith("Scheduled")
    job = cron.load_jobs()[0]
    assert job["permissions"] == "auto" and job["deliver"] == "telegram:5"
    for word in ("run", "remove", "pause"):
        assert run(f"{word} {job['id']}", **stranger).startswith("error")
    owner = {"permissions": "read-only", "can_change": True, "targets": None}
    assert run("add every day | y -> discord:9", **owner).startswith("Scheduled")
    assert cron.load_jobs()[1]["permissions"] == "read-only"
