# Scheduled tasks

nanocode can run a prompt by itself on a schedule: a morning summary of new issues, a nightly test run, a reminder in two hours. Each run is a fresh chat in the task's folder, and the answer goes where you want it: a chat on your phone, a desktop notification, or a file.

The code is in [`nanokit/cron.py`](../nanokit/cron.py).

## Add a task

In the app, or in a [gateway](gateway.md) chat:

```
/cron add every day at 8:00 | summarize yesterday's commits and open issues
/cron add weekdays at 18:30 | run the tests and tell me what fails
/cron add in 2 hours | remind me to reply to Anna
```

Or ask: *"check the build every hour and tell me if it breaks"*. The model uses its `cron` tool for that, and asks you first, every time, even in Auto mode. In a gateway chat (where nobody can be asked) the tool is always refused: use `/cron add` there. A scheduled task's own run has no `cron` tool, so tasks can't schedule more tasks.

From a terminal:

```sh
nanocode cron add "every monday at 9am" "plan the week from the open issues" --to telegram:123456789
nanocode cron list
```

## Schedules

| You write | It means |
|---|---|
| `every 15 minutes`, `every hour`, `every 6 hours` | Repeats |
| `every day at 8:00`, `everyday at 8`, `9 am every day`, `every morning`, `every evening` | Daily (morning is 8:00, evening 18:00) |
| `every 2 days at 9am`, `every 2 weeks at 8` | Every few days, at that time (from the next one) |
| `every monday at 9am`, `mondays and fridays at 10`, `mon-fri at 9` | Some days of the week |
| `weekdays at 18:30`, `weekends at 10am` | Monday to Friday, or Saturday and Sunday |
| `every month` | The 1st, at 9:00 |
| `in 2 hours`, `tomorrow at 7`, `at 7pm`, `tonight` | Once |
| `0 9 * * 1-5` | A cron line: minute, hour, day, month, weekday |

Cron lines support `*`, lists (`1,15`), ranges (`1-5`), steps (`*/10`) and names (`jan`, `mon`). Times are in your computer's time zone. With no time, a day-based schedule runs at 9:00. When clocks go back, a time in the repeated hour runs once (frequent tasks keep running through it, never twice for the same moment); when clocks go forward, a time that's skipped (2:30) runs right after the jump (3:30).

## Where answers go

| Target | Where |
|---|---|
| `telegram:<chat id>`, `discord:<channel id>`, `slack:<channel id>`, `whatsapp:<number>` | That chat (the [gateway](gateway.md) must be set up; it doesn't have to be running) |
| `notify` | A desktop notification (macOS, Linux with `notify-send`, Windows) |
| `file` | Only the file |

Every answer is also kept in `~/.nanocode/cron/<id>/<date>.md`. A task made in a gateway chat answers in that chat; one made in the app uses `notify`. In `/cron add`, end the prompt with `-> telegram:123` to choose.

## Manage tasks

| Command | What it does |
|---|---|
| `/cron list` | Tasks, when they run next, how the last run went |
| `/cron pause <id>` / `resume <id>` | Stop and start a task |
| `/cron remove <id>` | Delete it |
| `/cron run <id>` | Run it now |

The same work as `nanocode cron list|pause|resume|remove|run`. One-off tasks delete themselves after they run.

## When they run

Tasks run while the gateway is running (`nanocode gateway start`), and while the app is open. A run takes a lock first (`~/.nanocode/cron/locks/`, holding the pid and the start time), so a task never runs twice when both are running. A repeating task's next time is set before it starts, so a run that dies halfway (nanocode killed, the computer off) isn't started again and again; a lock whose nanocode is gone, or older than the timeout, is ignored. If the computer was off or asleep at the time, the task runs once when nanocode is next running.

A task runs as `nanocode -p "<prompt>"` in its folder: it has your tools, memory and skills, but not the chat it was made in, so write prompts that make sense on their own. By default it's read-only: set `"cron": {"permissions": "auto"}` to let tasks edit files and run commands. A task also remembers what its maker could do (`"permissions"` in the task): one made in a read-only [gateway](gateway.md) chat stays read-only, whatever the `cron` setting. Tasks saved before this have no level and run read-only: add them again to let them edit. A run that takes more than 30 minutes is stopped (`"cron": {"timeout": seconds}`).

Tasks are saved in `~/.nanocode/cron.json`: `id`, `schedule`, `prompt`, `workdir`, `deliver`, `paused`, `permissions`, `last_run`, `next_run`, and optionally `skills` (skills to use).

## From a gateway chat

In a read-only gateway only the owners (`"owners"` in the platform's settings: the ids you gave in setup, or the first person you approved) can add, run, pause or remove tasks; anyone else may `/cron list`. With `"permissions": "auto"` everyone allowed can. A task made in a chat answers only in that chat (or `file`); owners can send it anywhere with `-> telegram:123`.
