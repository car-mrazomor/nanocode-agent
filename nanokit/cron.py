"""Scheduled tasks: prompts that run by themselves ("every day at 8:00", "0 9 * * 1-5", "in 2 hours").

Jobs live in ~/.nanocode/cron.json. A job runs as a fresh headless chat (`nanocode -p`) in its folder, and its
answer goes where you asked: a Telegram, Discord, Slack or WhatsApp chat, a desktop notification, or only a
file (~/.nanocode/cron/<id>/<time>.md, where every answer is also kept).

The scheduler runs inside the gateway daemon and inside the app while it's open. A run takes a lock file first,
so two of them never run the same job twice.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import nanocode as nc
from nanokit import cli, command, on

CRON_TICK = 30  # seconds between checks
CRON_TIMEOUT = 1800  # a run that takes longer is stopped
CRON_LOCK_AGE = 2 * 86400  # lock files older than this are cleaned up
DEFAULT_TIME = (9, 0)  # "every monday" with no time

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
DAY_WORDS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6,
             "sun": 0, "mon": 1, "tue": 2, "tues": 2, "wed": 3, "thu": 4, "thur": 4, "thurs": 4, "fri": 5, "sat": 6}
UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400}
TIME_OF_DAY = {"morning": (8, 0), "noon": (12, 0), "midday": (12, 0), "afternoon": (14, 0), "evening": (18, 0),
               "night": (21, 0), "midnight": (0, 0)}
# Where the cron tool delivers by default: the gateway sets it to the chat a message came from.
DELIVER_TO = contextvars.ContextVar("cron_deliver_to", default=None)


class CronError(Exception):
    pass


# --- Cron expressions: minute hour day-of-month month day-of-week ---

def cron_field(text, low, high, names=None):
    values = set()
    for part in text.lower().split(","):
        part, _, step = part.partition("/")
        if step and not step.isdigit() or step == "0":
            raise CronError(f"bad step in '{text}'")
        if part == "*":
            start, end = low, high
        else:
            first, dash, last = part.partition("-")
            start = cron_value(first, low, high, names)
            end = cron_value(last, low, high, names) if dash else (high if step else start)
            if end < start:
                raise CronError(f"bad range '{part}'")
        values.update(range(start, end + 1, int(step or 1)))
    return values


def cron_value(text, low, high, names):
    if names and text[:3] in names and text.isalpha():
        return names[text[:3]]
    if not text.isdigit() or not low <= int(text) <= high:
        raise CronError(f"'{text}' isn't between {low} and {high}")
    return int(text)


def parse_cron(expr):
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("a cron expression has 5 fields: minute hour day month weekday")
    minutes = cron_field(fields[0], 0, 59)
    hours = cron_field(fields[1], 0, 23)
    days = cron_field(fields[2], 1, 31)
    months = cron_field(fields[3], 1, 12, MONTHS)
    weekdays = {d % 7 for d in cron_field(fields[4], 0, 7, DAYS)}   # 7 is Sunday too
    return minutes, hours, days, months, weekdays, fields[2] == "*", fields[4] == "*"


def next_cron(expr, after):
    """The first time after `after` (a datetime) that matches the expression."""
    minutes, hours, days, months, weekdays, any_day, any_weekday = parse_cron(expr)
    start = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    day = start.date()
    for _ in range(366 * 5):
        if day.month in months:
            in_month, in_week = day.day in days, (day.isoweekday() % 7) in weekdays
            # Like cron: with both restricted, either one matches; with one restricted, that one decides.
            matches = in_month and in_week if any_day or any_weekday else in_month or in_week
            if matches:
                for hour in sorted(hours):
                    if day == start.date() and hour < start.hour:
                        continue
                    for minute in sorted(minutes):
                        when = dt.datetime.combine(day, dt.time(hour, minute))
                        if when >= start:
                            return when
        day += dt.timedelta(days=1)
    raise CronError(f"'{expr}' never happens")


# --- Plain English schedules ---

def parse_time(text):
    """(hour, minute) from '8', '8:30', '8am', '6:30 pm', '18:30', 'noon'."""
    text = text.strip().lower().replace(".", "")
    if text in TIME_OF_DAY:
        return TIME_OF_DAY[text]
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|h)?", text)
    if not m:
        raise CronError(f"can't read the time '{text}'")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise CronError(f"'{text}' isn't a time")
    return hour, minute


def split_time(text):
    """('every monday', (9, 0)) from 'every monday at 9am'; the time is None when there's none."""
    m = re.search(r"\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|h)?|noon|midnight)$", text)
    if m and (" at " in text or re.search(r"(am|pm|:\d\d|noon|midnight)$", text)):
        return text[:m.start()].strip(), parse_time(m.group(1))
    m = re.search(r"\s+(?:in the\s+)?(morning|afternoon|evening|night)$", text)
    if m:
        return text[:m.start()].strip(), TIME_OF_DAY[m.group(1)]
    return text.strip(), None


def parse_schedule(text, now=None):
    """What a schedule means: {"cron": expr}, {"every": seconds} or {"once": timestamp}. Raises CronError."""
    now = now or dt.datetime.now()
    raw = " ".join(text.strip().split())
    low = raw.lower()
    if len(low.split()) == 5 and re.fullmatch(r"[\d*/,\-a-z ]+", low) and re.search(r"[\d*]", low):
        try:
            parse_cron(low)
            return {"cron": low}
        except CronError:
            if re.fullmatch(r"[\d*/,\- ]+", low):   # clearly meant as a cron line
                raise
    if low == "tonight":
        low = "tonight at 21:00"
    m = re.fullmatch(r"(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|h)?|noon|midnight)\s+((?:every|each|on\s|daily|"
                     r"weekday|weekend|mon|tue|wed|thu|fri|sat|sun).*)", low)
    if m:                                      # "9 am every day": the time first
        low = f"{m.group(2)} at {m.group(1)}"
    m = re.fullmatch(r"in (\d+|an?|one) (minute|min|hour|hr|day|week)s?", low)
    if m:
        count = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        return {"once": (now + dt.timedelta(seconds=count * UNITS[m.group(2)])).timestamp()}
    m = re.fullmatch(r"(?:(today|tomorrow|tonight)\s+)?(?:at\s+)?(.+?)(?:\s+(today|tomorrow|tonight))?", low)
    if m and (m.group(1) or m.group(3) or low.startswith("at ")):
        hour, minute = TIME_OF_DAY["night"] if m.group(2) == "tonight" else parse_time(m.group(2))
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "tomorrow" in (m.group(1), m.group(3)):
            when += dt.timedelta(days=1)
        elif when <= now:
            if m.group(1) or m.group(3):
                raise CronError(f"{raw} has already passed")
            when += dt.timedelta(days=1)       # "at 7": the next 7 o'clock
        return {"once": when.timestamp()}
    m = re.fullmatch(r"every (\d+ )?(minute|min|hour|hr)s?", low)
    if m:
        count, unit = int(m.group(1) or 1), m.group(2)
        if unit in ("minute", "min") and 60 % count == 0:
            return {"cron": f"*/{count} * * * *" if count > 1 else "* * * * *"}
        if unit in ("hour", "hr") and 24 % count == 0:
            return {"cron": f"0 */{count} * * *" if count > 1 else "0 * * * *"}
        return {"every": count * UNITS[unit]}
    m = re.fullmatch(r"every (\d+) (day|week)s?(?: at .+| in the \w+| \w+)?", low)
    if m:
        at = split_time(low)[1]
        days = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
        if at is None and re.fullmatch(r"every \d+ (day|week)s?", low):
            return {"every": days * 86400}
        if at is None:
            raise CronError(f"can't read the time in '{raw}'")
        first = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if first <= now:
            first += dt.timedelta(days=1)
        return {"days": days, "at": list(at), "start": first.date().isoformat()}
    rest, at = split_time(low)
    hour, minute = at or DEFAULT_TIME
    rest = re.sub(r"^(every|each|on)\b\s*", "", rest)
    if rest in ("day", "daily", "", "everyday", "every day") or (rest in TIME_OF_DAY and at is None):
        if rest in TIME_OF_DAY:
            hour, minute = TIME_OF_DAY[rest]
        return {"cron": f"{minute} {hour} * * *"}
    if rest in ("weekday", "weekdays", "workday", "workdays", "week day", "week days"):
        return {"cron": f"{minute} {hour} * * 1-5"}
    if rest in ("weekend", "weekends"):
        return {"cron": f"{minute} {hour} * * 0,6"}
    if rest in ("week", "weekly"):
        return {"cron": f"{minute} {hour} * * 1"}
    if rest in ("month", "monthly"):
        return {"cron": f"{minute} {hour} 1 * *"}
    m = re.fullmatch(r"([a-z]+)\s*(?:-|to|through|till|until)\s*([a-z]+)", rest)   # "mon-fri"
    if m and all(w.rstrip("s") in DAY_WORDS or w in DAY_WORDS for w in m.groups()):
        first, last = (DAY_WORDS[w if w in DAY_WORDS else w.rstrip("s")] for w in m.groups())
        days = sorted({(first + i) % 7 for i in range((last - first) % 7 + 1)})
        return {"cron": f"{minute} {hour} * * {','.join(map(str, days))}"}
    words = [w for w in re.split(r"[\s,]+", rest) if w and w != "and"]
    names = [re.sub(r"s$", "", w) if w.rstrip("s") in DAY_WORDS else w for w in words]
    if names and all(n in DAY_WORDS for n in names):
        days = sorted({DAY_WORDS[n] for n in names})
        return {"cron": f"{minute} {hour} * * {','.join(map(str, days))}"}
    raise CronError(f"can't read the schedule '{raw}'. Try: every 15 minutes, every day at 8:00, "
                    "every monday at 9am, weekdays at 18:30, in 2 hours, tomorrow at 7, or a cron line like 0 9 * * 1-5")


def local_ts(when):
    """The timestamp of a local wall-clock time. In the hour that repeats when clocks go back, the first one;
    a time skipped when clocks go forward runs at the same time after the jump (2:30 → 3:30)."""
    return when.replace(fold=0).timestamp()


def next_run(spec, after_ts):
    """When a job runs next (a timestamp, always after `after_ts`), or None when it won't again."""
    if "once" in spec:
        return spec["once"] if spec["once"] > after_ts else None
    if "every" in spec:
        return after_ts + spec["every"]
    if "days" in spec:                         # every N days at a time, counted from the first day
        start, step = dt.date.fromisoformat(spec["start"]), max(1, int(spec["days"]))
        hour, minute = spec["at"]
        day = max(start, dt.datetime.fromtimestamp(after_ts).date() - dt.timedelta(days=1))
        day += dt.timedelta(days=-(day - start).days % step)
        while True:
            ts = local_ts(dt.datetime.combine(day, dt.time(hour, minute)))
            if ts > after_ts:
                return ts
            day += dt.timedelta(days=step)
    # A cron line matches local wall-clock times. When clocks go back an hour, those repeat: after a run in the
    # repeated hour, the naive "next" time can be one that has already passed, so step on until one hasn't.
    after = dt.datetime.fromtimestamp(after_ts)
    for _ in range(200):
        when = next_cron(spec["cron"], after)
        for fold in (0, 1):
            ts = when.replace(fold=fold).timestamp()
            if ts > after_ts:
                return ts
        after = when
    raise CronError(f"'{spec['cron']}' never happens")


def describe(spec):
    if "once" in spec:
        return "once, " + fmt_time(spec["once"])
    if "days" in spec:
        n, (hour, minute) = spec["days"], spec["at"]
        every = f"every {n // 7} week{'s' * (n > 7)}" if n % 7 == 0 else f"every {n} day{'s' * (n > 1)}"
        return f"{every} at {hour:02d}:{minute:02d}"
    if "every" in spec:
        secs = spec["every"]
        for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
            if secs % size == 0:
                n = secs // size
                return f"every {n} {name}{'s' * (n > 1)}"
    return f"cron {spec.get('cron')}"


def fmt_time(ts):
    if not ts:
        return "-"
    when = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    day = "today" if when.date() == today else "tomorrow" if when.date() == today + dt.timedelta(days=1) else \
        when.strftime("%a %d %b")
    return f"{day} {when:%H:%M}"


# --- The jobs file ---

def jobs_file():
    return nc.home_dir() / "cron.json"


def runs_dir():
    return nc.home_dir() / "cron"


class file_lock:
    """`with file_lock(path):` one process at a time (nanocode's config lock, for any file)."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        self.locked = False
        try:
            if os.name == "nt":
                import msvcrt
                for _ in range(100):
                    try:
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                        self.locked = True
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt" and self.locked:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()


def load_jobs():
    try:
        data = json.loads(jobs_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [j for j in (data.get("jobs") if isinstance(data, dict) else data) or [] if isinstance(j, dict)]


def save_jobs(jobs):
    path = jobs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # prompts can be private
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"jobs": jobs}, f, indent=2)
    tmp.replace(path)


def update_jobs(change):
    """Reads, changes and writes the jobs under a lock (the app and the gateway may both be running)."""
    with file_lock(jobs_file().with_suffix(".lock")):
        jobs = load_jobs()
        result = change(jobs)
        save_jobs(jobs)
    return result


def find_job(jobs, job_id):
    job_id = (job_id or "").strip()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if job is None and job_id:
        matches = [j for j in jobs if j.get("id", "").startswith(job_id)]
        job = matches[0] if len(matches) == 1 else None
    if job is None:
        raise CronError(f"no job '{job_id}' (/cron list shows them)")
    return job


DELIVER_RE = re.compile(r"^(telegram|discord|slack|whatsapp):\S+$|^(file|notify)$")


def add_job(schedule, prompt, deliver="file", workdir=None, skills=None, now=None, permissions="auto"):
    """A new job. `permissions` is what its maker may do ("auto" or "read-only"): the job never gets more."""
    schedule, prompt = schedule.strip(), prompt.strip()
    if not prompt:
        raise CronError("the job needs a prompt: /cron add <schedule> | <prompt>")
    if not DELIVER_RE.match(deliver or ""):
        raise CronError(f"can't deliver to '{deliver}': use telegram:<chat>, discord:<channel>, slack:<channel>, "
                        "whatsapp:<number>, file or notify")
    spec = parse_schedule(schedule, now)
    now_ts = (now or dt.datetime.now()).timestamp()
    job = {"id": secrets.token_hex(3), "schedule": schedule, "spec": spec, "prompt": prompt,
           "workdir": str(Path(workdir or os.getcwd()).resolve()), "deliver": deliver, "paused": False,
           "created": now_ts, "last_run": None, "last_status": None, "next_run": next_run(spec, now_ts),
           "permissions": "auto" if permissions == "auto" else "read-only"}
    if skills:
        job["skills"] = [s for s in skills if s]
    update_jobs(lambda jobs: jobs.append(job))
    return job


def set_paused(job_id, paused):
    def change(jobs):
        job = find_job(jobs, job_id)
        job["paused"] = paused
        if not paused and (not job.get("next_run") or job["next_run"] < time.time()):
            job["next_run"] = next_run(job["spec"], time.time())
        return job
    return update_jobs(change)


def remove_job(job_id):
    def change(jobs):
        job = find_job(jobs, job_id)
        jobs.remove(job)
        return job
    return update_jobs(change)


def job_line(job):
    state = "paused" if job.get("paused") else f"next {fmt_time(job.get('next_run'))}"
    last = f" · last {fmt_time(job['last_run'])} {job.get('last_status') or ''}".rstrip() if job.get("last_run") else ""
    prompt = " ".join(job.get("prompt", "").split())
    return (f"{job['id']}  {job.get('schedule')}  →  {job.get('deliver')}  ({state}{last})\n"
            f"    {prompt[:100]}{'…' if len(prompt) > 100 else ''}")


def list_text():
    jobs = load_jobs()
    if not jobs:
        return "No scheduled tasks. Add one: /cron add every day at 8:00 | summarize the open issues"
    return "\n".join(job_line(j) for j in jobs)


# --- Running a job ---

def job_command(job):
    """The headless nanocode that runs a job: `nanocode -p <prompt>`, with --yes only when both cron's settings
    and whoever made the job may edit (a job made from a read-only gateway chat stays read-only)."""
    prompt = job["prompt"]
    if job.get("skills"):
        prompt = f"Use these skills (load each with the skill tool): {', '.join(job['skills'])}.\n\n{prompt}"
    auto = (nc.st.config.get("cron") or {}).get("permissions") == "auto" and job.get("permissions") == "auto"
    return [sys.executable, str(Path(nc.__file__).resolve()), "-p", prompt] + (["--yes"] if auto else [])


async def execute(job):
    """Runs a job's prompt. (ok, answer)"""
    workdir = job.get("workdir") or os.getcwd()
    if not Path(workdir).is_dir():
        return False, f"the folder {workdir} doesn't exist any more"
    timeout = int((nc.st.config.get("cron") or {}).get("timeout") or CRON_TIMEOUT)
    env = {**os.environ, "NANOCODE_NO_UPDATE_CHECK": "1", "NANOCODE_CRON": job["id"]}
    try:
        proc = await asyncio.create_subprocess_exec(*job_command(job), cwd=workdir, env=env,
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return False, f"couldn't start nanocode: {e}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"stopped after {timeout // 60} minutes"
    except asyncio.CancelledError:
        proc.kill()
        raise
    answer = nc.ANSI_RE.sub("", stdout.decode(errors="replace")).strip()
    if proc.returncode != 0:
        detail = nc.ANSI_RE.sub("", stderr.decode(errors="replace")).strip().splitlines()[-3:]
        return False, (answer + "\n\n" if answer else "") + "failed: " + (" ".join(detail) or f"exit {proc.returncode}")
    return True, answer or "(no answer)"


def save_result(job, text, started):
    folder = runs_dir() / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y-%m-%d_%H%M%S', time.localtime(started))}.md"
    path.write_text(f"# {job.get('schedule')}: {job['prompt'][:80]}\n\n{text}\n", encoding="utf-8")
    return path


def notify_command(title, text):
    """A desktop notification command for this OS, or None."""
    if sys.platform == "darwin":
        return ["osascript", "-e", "on run argv", "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, text]
    if os.name == "nt":
        script = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                  "$n.ShowBalloonTip(10000, $env:NANOCODE_TITLE, $env:NANOCODE_TEXT, 'Info'); Start-Sleep 10; "
                  "$n.Dispose()")
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
    import shutil
    return ["notify-send", "--app-name=nanocode", title, text] if shutil.which("notify-send") else None


def desktop_notify(title, text):
    argv = notify_command(title, text[:250])
    if argv is None:
        return False
    env = {**os.environ, "NANOCODE_TITLE": title, "NANOCODE_TEXT": text[:250]}
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env)
        return True
    except OSError:
        return False


async def deliver(job, text, path):
    """Sends a job's answer to its target. Returns an error message or None."""
    target = job.get("deliver") or "file"
    if target == "file":
        return None
    title = f"nanocode: {job.get('schedule')}"
    if target == "notify":
        return None if desktop_notify(title, text) else "no desktop notifications here (notify-send?)"
    platform, _, chat = target.partition(":")
    try:
        from nanokit import gateway
        await gateway.deliver(platform, chat, f"⏰ {job['prompt'][:60]}\n\n{text}")
    except Exception as e:
        nc.log_error(e)
        return f"couldn't send to {target}: {e}"
    return None


async def run_job(job, log=None):
    """Runs a job now, keeps and delivers its answer, and records the run. Returns the answer file."""
    log = log or (lambda text: None)
    started = time.time()
    log(f"cron {job['id']}: running '{job['prompt'][:60]}' in {job.get('workdir')}")
    try:
        ok, text = await execute(job)
    except asyncio.CancelledError:             # stopped (nanocode is closing): another one may run it
        release(job)
        raise
    path = save_result(job, text, started)
    problem = await deliver(job, text, path)
    status = ("ok" if ok else "failed") + (f", {problem}" if problem else "")
    log(f"cron {job['id']}: {status} → {nc.tilde(path)}")

    def change(jobs):
        saved = next((j for j in jobs if j.get("id") == job["id"]), None)
        if saved is None:
            return
        saved.update(last_run=started, last_status=status)
        upcoming = None if "once" in saved["spec"] else next_run(saved["spec"], max(time.time(), started))
        if upcoming is None:
            jobs.remove(saved)                 # a one-off ("in 2 hours"): done
        else:
            saved["next_run"] = upcoming
    update_jobs(change)
    return path


def lock_path(job):
    return runs_dir() / "locks" / f"{job['id']}-{int(job['next_run'])}.lock"


def pid_alive(pid):
    from nanokit import gateway
    return gateway.pid_alive(pid)


def lock_stale(path):
    """A lock whose run can't be going on any more: its nanocode is gone, or it's older than any run can be."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    try:
        info = json.loads(text)
        info = info if isinstance(info, dict) else {"pid": int(info)}
    except (ValueError, TypeError):
        info = {}
    started = info.get("started") or (time.time() - age)
    timeout = int((nc.st.config.get("cron") or {}).get("timeout") or CRON_TIMEOUT)
    if time.time() - started > timeout + CRON_TICK * 4:   # a run is stopped after its timeout
        return True
    pid = info.get("pid")
    return isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid)


def claim(job, _again=True):
    """Takes this run of a job (by its scheduled time), so another nanocode doesn't run it too. The lock holds
    our pid and the time: a lock left by a nanocode that died mid-run doesn't block the job."""
    path = lock_path(job)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _again and lock_stale(path):
            try:
                path.unlink()
            except OSError:
                return False
            return claim(job, _again=False)
        return False
    except OSError:
        return False
    os.write(fd, json.dumps({"pid": os.getpid(), "started": time.time()}).encode())
    os.close(fd)
    return True


def release(job):
    try:
        path = lock_path(job)
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def advance(job):
    """Moves a recurring job's next run on before it runs, so a run that dies halfway isn't started again."""
    def change(jobs):
        saved = next((j for j in jobs if j.get("id") == job["id"]), None)
        if saved is not None and "once" not in saved["spec"] and saved.get("next_run") == job["next_run"]:
            saved["next_run"] = next_run(saved["spec"], max(time.time(), job["next_run"]))
    update_jobs(change)


def clean_locks():
    folder = runs_dir() / "locks"
    if not folder.is_dir():
        return
    for path in folder.glob("*.lock"):
        try:
            if time.time() - path.stat().st_mtime > CRON_LOCK_AGE or lock_stale(path):
                path.unlink()
        except OSError:
            pass


def due_jobs(now=None):
    now = now or time.time()
    return [j for j in load_jobs() if not j.get("paused") and j.get("next_run") and j["next_run"] <= now]


RUNNING = {}  # job id -> task


async def tick(log=None):
    """Starts the jobs that are due (each once, across every nanocode running)."""
    started = []
    for job in due_jobs():
        if job["id"] in RUNNING or not claim(job):
            continue
        advance(job)
        task = asyncio.ensure_future(run_job(job, log))
        RUNNING[job["id"]] = task
        task.add_done_callback(lambda t, i=job["id"]: RUNNING.pop(i, None))
        started.append(task)
    return started


async def scheduler(log=None):
    """Checks for due jobs every CRON_TICK seconds until cancelled."""
    clean_locks()
    while True:
        try:
            await tick(log)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            nc.log_error(e)
        await asyncio.sleep(CRON_TICK)


# --- /cron, `nanocode cron`, and the cron tool ---

CRON_HELP = ("/cron add <schedule> | <prompt>   e.g. /cron add every day at 8:00 | summarize yesterday's commits\n"
             "/cron list · /cron pause|resume|remove|run <id>\n"
             "schedules: every 15 minutes, every day at 8:00, every monday at 9am, weekdays at 18:30, in 2 hours, "
             "tomorrow at 7, or a cron line (0 9 * * 1-5)")


async def cron_command(arg, deliver="file", log=None, permissions="auto", can_change=True, targets=None):
    """Handles `/cron ...` (the app, the gateway's chats and the CLI). Returns the text to show.

    From a gateway chat: `permissions` is the chat's ("auto" or "read-only"), stored with a new job; with
    `can_change` False only list works; `targets`, when given, are the only places a new job may answer."""
    word, _, rest = arg.strip().partition(" ")
    rest = rest.strip()
    try:
        if word in ("", "list", "ls"):
            return list_text()
        if word in ("add", "pause", "resume", "remove", "rm", "delete", "run") and not can_change:
            raise CronError("this chat is read-only: only the owner can add, run or change scheduled tasks here "
                            "(/cron list works)")
        if word == "add":
            schedule, bar, prompt = rest.partition("|")
            if not bar:
                raise CronError("separate the schedule and the prompt with |: /cron add every day at 8 | <prompt>")
            target = deliver
            m = re.search(r"\s+(?:->|→)\s*(\S+)\s*$", prompt)   # "... -> telegram:123" picks the target
            if m:
                target, prompt = m.group(1), prompt[:m.start()]
            if targets is not None and target not in targets:
                raise CronError(f"from here a task can only answer in this chat ({', '.join(targets)})")
            job = add_job(schedule, prompt, target, permissions=permissions)
            return f"Scheduled {job['id']}: {describe(job['spec'])} → {job['deliver']} (next {fmt_time(job['next_run'])})"
        if word in ("pause", "resume"):
            job = set_paused(rest, word == "pause")
            return f"{'Paused' if word == 'pause' else 'Resumed'} {job['id']}"
        if word in ("remove", "rm", "delete"):
            job = remove_job(rest)
            return f"Removed {job['id']}: {job['prompt'][:60]}"
        if word == "run":
            job = find_job(load_jobs(), rest)
            path = await run_job(job, log)
            return f"Ran {job['id']}: {nc.tilde(path)}\n\n{path.read_text(encoding='utf-8')[-3000:]}"
    except CronError as e:
        return f"error: {e}"
    return CRON_HELP


@command("/cron", "scheduled tasks: /cron add <schedule> | <prompt>, list, pause, resume, remove, run")
async def cmd_cron(arg):
    nc.out(await cron_command(arg, deliver="notify") + "\n")


@cli("cron", "scheduled tasks: list, add, pause, resume, remove, run")
def cron_cli(argv):
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print('usage: nanocode cron add "<schedule>" "<prompt>" [--to telegram:<chat>|discord:<channel>|'
              'slack:<channel>|whatsapp:<number>|notify|file]\n'
              "       nanocode cron list · nanocode cron pause|resume|remove|run <id>\n"
              + CRON_HELP.split("\n")[-1])
        return 0
    target = "notify"
    if "--to" in argv[:-1]:                    # nanocode cron add ... --to telegram:123
        i = argv.index("--to")
        target, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if argv[:1] == ["add"] and not any("|" in a for a in argv) and len(argv) >= 3:
        argv = ["add", argv[1], "|", " ".join(argv[2:])]   # nanocode cron add "every day at 8" "the prompt"
    text = asyncio.run(cron_command(" ".join(argv), deliver=target, log=print))
    print(text)
    return 1 if text.startswith("error:") else 0


def tool_cron_offered():
    """A job's own run (NANOCODE_CRON is set) gets no cron tool: jobs can't schedule more jobs."""
    return not os.environ.get("NANOCODE_CRON")


nc.ALWAYS_ASK.add("cron")  # adding or removing a scheduled task asks every time, even in Auto mode


def register_tool(*spec):
    return nc.tool(*spec) if tool_cron_offered() else (lambda fn: fn)


@register_tool("cron", "Schedule a task to run later or regularly, by itself, as a new chat in this folder (e.g. the "
         "user asks 'remind me every morning...', 'check the build every hour'). action: add (schedule + prompt), "
         "list, remove (id), pause (id), resume (id). schedule: plain English ('every day at 8:00', 'weekdays at "
         "18:30', 'every 15 minutes', 'in 2 hours', 'tomorrow at 7') or a 5-field cron line. The prompt runs "
         "with no memory of this chat, so make it self-contained. deliver: where the answer goes; leave it out "
         "to answer where the user is talking now.",
         {"action": {"type": "string", "enum": ["add", "list", "remove", "pause", "resume"]},
          "schedule": {"type": "string"}, "prompt": {"type": "string"}, "id": {"type": "string"},
          "deliver": {"type": "string", "description": "telegram:<chat>, discord:<channel>, slack:<channel>, "
                                                       "whatsapp:<number>, notify or file"}},
         ["action"])
async def tool_cron(args):
    action = args.get("action")
    if not tool_cron_offered():
        raise nc.ToolError("a scheduled task can't schedule tasks")
    if action == "list":
        return list_text()
    preview = f"{action} {args.get('id') or ''}{args.get('schedule') or ''}: {args.get('prompt') or ''}".strip()
    if not await nc.allowed("cron", preview):
        return nc.DENIED
    try:
        if action == "add":
            job = add_job(args.get("schedule") or "", args.get("prompt") or "",
                          args.get("deliver") or DELIVER_TO.get() or "notify",
                          permissions="auto" if nc.st.auto_approve or not nc.st.headless else "read-only")
            return f"scheduled {job['id']}: {describe(job['spec'])}, next {fmt_time(job['next_run'])}, " \
                   f"answer → {job['deliver']}"
        if action == "remove":
            return f"removed {remove_job(args.get('id'))['id']}"
        if action in ("pause", "resume"):
            return f"{action}d {set_paused(args.get('id'), action == 'pause')['id']}"
    except CronError as e:
        raise nc.ToolError(str(e))
    raise nc.ToolError(f"unknown action '{action}'")


# --- In the app: the scheduler runs while it's open (the gateway has its own) ---

APP = {"task": None}


@on("startup")
def start_in_app():
    if (nc.st.config.get("cron") or {}).get("in_app", True) is False:
        return

    def log(text):
        if ": running" not in text:
            nc.show_toast(text.split(": ", 1)[-1], seconds=6, title="Scheduled task")

    APP["task"] = asyncio.ensure_future(scheduler(log))


@on("shutdown")
def stop_in_app():
    if APP["task"] is not None:
        APP["task"].cancel()
        APP["task"] = None
