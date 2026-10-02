# Fixes — what was broken and what changed

A technical record of the bugs found in the wake/sleep flow and the browser
engine, and how each one was fixed. Written for developers, not end users —
for installation and usage see [README.md](README.md).

The guiding goal throughout: **someone who knows nothing about programming can
press the sleep button and it just works.** No understanding of Selenium,
chromedriver, sudoers, UAC, or scheduled tasks should ever be required.

- Base commit: `fc32938`
- Files changed: `pyqt.py`, `skyroom_core.py`
- Verification: 76 automated checks across 5 offscreen suites, all passing

---

## Table of contents

1. [The critical path — bugs that made the feature unusable](#1-the-critical-path--bugs-that-made-the-feature-unusable)
2. [Permission handling](#2-permission-handling)
3. [Correctness bugs](#3-correctness-bugs)
4. [Browser engine](#4-browser-engine)
5. [GUI robustness](#5-gui-robustness)
6. [Known limitations](#6-known-limitations)

---

## 1. The critical path — bugs that made the feature unusable

### 1.1 The machine slept and never woke up (Windows)

**Where:** `_windows_arm_and_sleep()` — `pyqt.py:492`

`shutdown /h` and `SetSuspendState` return the instant Windows *accepts* the
sleep request. They do not block until the machine resumes. The old code
deleted the scheduled wake task three lines after issuing the sleep command —
so the alarm was destroyed while the machine was on its way down.

```
Press sleep  →  Windows accepts request  →  we delete the wake task  →  ...darkness, forever
```

The task is a one-shot time trigger: once it has fired it cannot fire again,
and the next arm overwrites it via `schtasks /Create /F`. Leaving it
registered is both correct and sufficient.

**Fix:** removed the `schtasks /Delete` on the success path. The two deletions
on the *error* paths stay — there is no wake to preserve if the machine never
went to sleep.

### 1.2 Setup asked for administrator rights forever

**Where:** `_check_wake_permissions()` — `pyqt.py:1720`

The Windows check returned `True` only if the `SkyroomBotWake` task existed.
Deleting it (1.1) guaranteed it was gone next run, so `arm_wake_and_sleep()`
re-ran the entire self-elevating PowerShell setup — a fresh UAC prompt — on
every single press.

**Fix:** resolved by 1.1. The check now tests a persistent config artifact
that actually persists.

### 1.3 Every press froze the UI for 10 seconds

**Where:** `arm_wake_and_sleep()` → `_stop_bot_and_wait()` — `pyqt.py:1576`

```python
self.stop_bot()
for _ in range(40):
    if self.bot is None:
        break
    time.sleep(0.25)
```

`stop_bot()` only *starts* a background thread. `self.bot` is cleared
exclusively in `_on_bot_finished()`, which runs as a queued slot **on the main
thread** — the very thread sitting in `time.sleep`. The event loop cannot
deliver the signal while blocked, so `self.bot is None` was never true. The
loop was decorative: it always ran the full 10 seconds, then hibernated
regardless of whether Chrome had actually closed.

**Fix:** pump a nested `QEventLoop` instead of blocking, so the queued signal
can land. Measured: returns in 0.20s against a 0.2s stop, and Qt events
dispatched during the wait.

### 1.4 "Next wake" was a week wrong for a running class

**Where:** `_compute_next_wake()` — `pyqt.py:1649`

It called `next_occurrence()`, which knows only the class *start* time and
rolls `+7 days` for anything already past. A class currently in progress
therefore reported next week's occurrence — arming an alarm a week out, or
showing a confusing "wake time is in the past" error.

**Fix:** use the class-aware `next_occurrence_range()` from the engine and
skip classes already underway. When a class starts sooner than the configured
wake lead, wake almost immediately rather than showing an error.

### 1.5 The wake time was computed before several minutes of dialogs

**Where:** `arm_wake_and_sleep()` — `pyqt.py:2045`

The target was computed at the top of the function, *then* the code stopped
the bot (up to 10s), *then* possibly ran the whole permission/setup flow,
*then* showed a confirmation box. If the user took their time, the epoch
handed to `rtcwake` was already in the past.

**Fix:** recompute after the permission work and immediately before the
confirmation dialog, then re-validate once more after the user clicks Yes. The
value shown in the dialog is always the value that actually gets armed. The
final one is logged.

---

## 2. Permission handling

### 2.1 The check was reading the wrong `sudo` output

**Where:** `_check_wake_permissions()` — `pyqt.py:1720`

This is the bug behind "endless UAC prompts" on Linux. The check ran:

```python
sudo -n -l /usr/bin/rtcwake      # stdout: "/usr/bin/rtcwake"
```

then tested `if "NOPASSWD" in out`. That output contains only the command
path — the string `NOPASSWD` never appears. Plain `sudo -n -l` (no argument)
is what lists the rules, including the `(ALL) NOPASSWD: /usr/bin/rtcwake`
line.

So the check reported "permission missing" even when the rule was correctly
installed, and the app requested permission on every run, forever. Confirmed
directly on the test machine: the old expression evaluated to `False` with the
rule present; the new one returns `True`.

**Fix:** inspect plain `sudo -n -l` and require a `NOPASSWD` line naming the
exact rtcwake path. A bare `(ALL) ALL` line means sudo would still prompt, so
it correctly does not count.

### 2.2 Permission is now requested automatically

Previously a Linux user was told to copy a command into a terminal. The app now
raises the request itself, exactly like `sudo` or UAC would — the user types a
password, never a command.

| Platform | Mechanism | One-shot? |
|----------|-----------|-----------|
| Linux | `pkexec` (polkit) native password dialog | Yes — installs one `sudoers.d` rule |
| Windows | self-elevating PowerShell → UAC | Yes — task persists (1.1) |
| Neither available | falls back to the copy-command dialog | — |

**Where:** `_linux_elevated_setup()` — `pyqt.py:392`

On first press of the sleep button the app runs `pkexec` on a helper script
that installs `NOPASSWD` for `RTCWAKE_PATH`. Every later arm runs `sudo -n`
silently. Cancellation (polkit 126/127) is distinguished from "no polkit
installed" (rc 2) so the message matches the actual cause.

### 2.3 Hardening the elevation path

A privilege escalation bug here would be far worse than the sleep bug, so the
helper is built defensively:

- **No predictable temp filename.** The helper runs from a private
  `tempfile.mkdtemp()` directory at mode `0700`. A fixed `/tmp` name could be
  swapped by another user between the moment we write it and the moment
  `pkexec` runs it as root — which would mean executing someone else's script
  as root.
- **The rule is validated, never written blind.** A malformed sudoers file can
  lock a user out of `sudo` entirely. The helper runs `visudo -cf` and, if the
  rule is rejected, **deletes the bad file** before exiting non-zero.
- **Correct ownership and mode.** `chown root:root` and `chmod 440` are
  enforced; sudo ignores files that are group/world-writable.
- **Injected content is quoted.** The rule string passes through
  `shlex.quote()`, and the helper runs under `set -e`.
- **`visudo` is resolved explicitly.** It lives in `/usr/sbin`, which is not on
  a GUI application's `PATH` — without `VISUDO_PATH`, validation would silently
  never happen.

### 2.4 The Windows setup blocked on a keypress

**Where:** `_WINDOWS_SETUP_PS1`, end of both branches

Both the success and failure paths ended with `ReadKey("NoEcho,IncludeKeyDown")`
— "Press any key to close". The parent process waits on the child, so the app
sat showing "⏳ در حال اجرا…" until the user happened to find a console window
they were never told about.

**Fix:** removed both pauses; the script exits immediately with its status code.

---

## 3. Correctness bugs

### 3.1 Two emitters for one signal (stale-run corruption)

**Where:** `_on_bot_finished()` — `pyqt.py:1564`

Both `_watch_bot()` and `stop_bot()`'s background thread emitted `finished`.
A late duplicate from an already-finished bot — arriving after the user had
started a new run — would run `self.bot = None` against the *live* bot. Effect:
Start re-enabled while Chrome was running, Stop became a no-op, and
`closeEvent` skipped stopping the live browsers entirely, leaving orphaned
Chrome processes.

**Fix:** `finished` became `pyqtSignal(object)` carrying the bot that ended.
`_on_bot_finished(bot)` returns early when `bot is not self.bot`.

### 3.2 Classes crossing midnight were misdetected

**Where:** `next_occurrence_range()` — `skyroom_core.py:110`

Mid-class detection was gated on `now.weekday() == target_wd`. A 23:00–01:00
class is, for its final two hours, on the *next* weekday — so at 00:30 the
check failed and the function returned a date a week out. Two distinct bugs:

1. The weekday guard rejected the second half of a midnight-crossing class.
2. Even without the guard, the candidate is built from *today's* weekday, so a
   class that started **yesterday** evening and is still running was never
   considered at all.

**Fix:** dropped the weekday guard, and added an explicit previous-occurrence
case so the still-running class is found. Verified at four clock positions
(22:00 before, 00:30 during, 02:00 after, and 00:30 the following day).

### 3.3 Dead code from a since-invalid assumption

**Where:** `_build_tasks()` and `_worker()` — `skyroom_core.py`

The `"already ended — will skip"` branch and the worker's end-of-class guard
were unreachable: `next_occurrence_range()` never returns a past range — it
rolls forward a week instead.

Rather than delete them (they are cheap and correct), they are now documented
as what they actually are — a guard against the wall clock jumping forward
after a task was built — so the next reader does not re-derive the dead-code
puzzle.

---

## 4. Browser engine

### 4.1 Driver discovery ran N×R times and raced

**Where:** `_resolve_driver()` — `skyroom_core.py:623`

`_build_driver()` used to probe every candidate driver on every retry. With N
classes over R restart attempts that is N×R `chromedriver --version`
subprocesses — and several task threads could enter Selenium Manager
simultaneously and race each other's download into the shared cache, producing
a corrupt driver on first run.

**Fix:** the driver binary is decided once per bot run and shared by every task
thread (double-checked locking via `_driver_ready`). Selenium Manager is
serialized behind `_sm_lock`. A driver that fails to launch is dropped from the
cache for the remainder of the run instead of being retried forever.

### 4.2 No preflight — a wall of Selenium errors

**Where:** `preflight()` — `skyroom_core.py:644`, wired at `start_bot()` — `pyqt.py:1493`

A missing Chrome or an unusable driver previously surfaced as a stack of
low-level Selenium messages. `preflight()` now runs before any browser opens and
returns plain-language problems ("Google Chrome is not installed…"). The Start
button shows them as one readable Persian message, and treats a missing browser
or driver as fatal rather than starting doomed work.

### 4.3 Raw driver internals leaked into dialogs

**Where:** `_build_driver()` — `skyroom_core.py:710`

`_capture_service_log()` appends 15 lines of raw chromedriver output to the
exception string. That belongs in the log tab, not in a dialog a user has to
read.

**Fix:** the verbose log is emitted to the log instead; the exception carries a
short sentence pointing at the report tab.

### 4.4 WebSocket counter could go negative

**Where:** `WS_MONITOR_JS` — `skyroom_core.py:215`

A socket that fails before opening fires `error`/`close` without ever firing
`open`, but the old counter incremented on `open` and decremented on `close` —
pushing `__wsActive` negative. The watchdog then read that as a dead connection
while the page was still retrying, triggering a needless full re-login.

**Fix:** only count sockets that actually reached `open`, tracked with a
per-socket `counted` flag.

### 4.5 Silent waiting

**Where:** `_join_room_once()`

When the class page never opened a media socket, the bot spun silently until
end time. A failed login was indistinguishable from a quiet class.

**Fix:** logs a warning naming the elapsed time and suggesting the user check
the browser window.

---

## 5. GUI robustness

### 5.1 Callbacks scheduled from threads with no event loop

**Where:** `_do_arm_and_sleep()` — `pyqt.py:2177`, and the Windows setup flow

Three call sites used `QTimer.singleShot(0, ...)` from a plain
`threading.Thread`. `QTimer` needs the **calling** thread to have a running
event loop; a worker thread has none. These could simply never fire — leaving
the sleep button permanently disabled with no error shown, or the Windows setup
dialog stuck on "in progress" with no way out.

**Fix:** extended `BotBridge` with `arm_result(int, str)` and
`setup_done(int, str, str)` and emit those instead. Verified: a failure emitted
from a worker thread reaches the main thread and re-enables the button.

### 5.2 "Suspend" silently hibernated (Windows)

**Where:** `_windows_suspend_ps()` — `pyqt.py:454`, `_windows_sleep_command()` — `pyqt.py:475`

The old `rundll32 powrprof.dll,SetSuspendState 0,1,0` is undocumented and
hibernates whenever hibernation is enabled system-wide — which the app's own
setup script enables. Two buttons, identical behaviour.

**Fix:** P/Invoke the documented `SetSuspendState(false, ...)` API directly,
falling back to the old `rundll32` line only if `powershell.exe` is
unavailable. The "Power off S5" radio is disabled and relabelled on Windows,
since Windows cannot honour power-off-with-wake — a label that disagrees with
behaviour is worse than no label.

### 5.3 "Start after wake" offered for a mode that cannot honour it

**Where:** `_apply_sleep_mode_effects()` — `pyqt.py:1676`

`rtcwake -m off` powers the machine down. Nothing survives to run the wake
handler, so the checkbox was offered, checked, and meaningless — the warning
was prose in a dialog rather than a disabled control.

**Fix:** selecting "off" disables and unchecks the option, with a tooltip
explaining why. `_current_sleep_mode()` was factored out so the arm flow and
`_save_all_state` no longer re-implement the lookup inline.

### 5.4 `rtcwake` path pinned in one place only

**Where:** `RTCWAKE_PATH` — `pyqt.py` constants

The sudoers rule granted NOPASSWD for the literal `/usr/bin/rtcwake`, but the
command ran bare `rtcwake` and relied on `secure_path` resolution to match.
Works on Fedora/Ubuntu; breaks wherever rtcwake lives elsewhere, and the moment
one of the three strings is edited without the others.

**Fix:** one `RTCWAKE_PATH` constant used in the grant, the probe, and the
execution, plus a clear message when the binary is missing entirely.

---

## 6. Known limitations

Deliberately not addressed, and why:

- **Start builds a task for every class in the weekly file.** Threads then wait
  up to ~7 days, so Start stays disabled for a week and `_on_bot_finished`
  never fires. A real fix needs per-class scheduling with a horizon — a design
  change, not a patch. This is the most significant remaining issue.

- **`wake_scheduler.py` is dead code.** Not in `skyroom_bot.spec`, never invoked
  by the app, and its own docstring says `sudo python wake_scheduler.py` and
  then re-invokes `sudo` internally. It also carries a second, divergent copy of
  the wake-time rule. Delete it or wire it up; do not maintain both.

- **`build.py` hardcodes one chromedriver version** (`153.0.8010.52`) in a
  `version_map` with a single entry and no fallback for other Chrome majors.
  Not a logic bug in the app — the engine falls back to Selenium Manager — but
  releases will ship a driver that is wrong for most users.

- **The UI still rejects `end_time <= start_time`** (`pyqt.py` `ClassDialog`),
  so the midnight-crossing rollover the engine now handles is reachable only via
  a hand-edited JSON file. The two semantics should be reconciled.

- **The password-prompt path is unverified on a clean machine.** The Linux
  elevation code was tested on a host where the sudoers rule already existed,
  so `pkexec` succeeded without prompting. That GNOME Shell hosts the polkit
  agent (so the dialog will appear for an unprivileged user) was confirmed, but
  prompt → success needs a machine without the rule installed.

- **`skyroom_core.py` is CRLF, `pyqt.py` is LF.** Line endings were preserved as
  found in each file to keep diffs reviewable.

---

## Appendix — reproducing the verification

```bash
python -m py_compile pyqt.py skyroom_core.py          # both parse

# core: preflight, driver resolution, thread safety
python -c "
import skyroom_core as sc, threading
b = sc.SkyroomBot(sc.RuntimeConfig(), [], print)
print('preflight:', b.preflight()); print('driver:', b._resolve_driver())
b2 = sc.SkyroomBot(sc.RuntimeConfig(), [], print)
ts = [threading.Thread(target=b2._resolve_driver) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
print('concurrent resolution OK')"

QT_QPA_PLATFORM=offscreen python -c "import pyqt,sys; sys.argv=['x']; pyqt.main()"
```

| Suite | Checks | Covers |
|-------|--------|--------|
| `test_wake_fixes` | 26 | wake computation, stop-wait, signals, mode effects |
| `test_midnight` | 8 | midnight rollover at four clock positions |
| `test_e2e` | 12 | window build, JSON load, arm flow dialogs |
| `test_perm` | 22 | elevation paths, helper hardening, `app_pump` |
| `test_armflow` | 8 | end-to-end arm with captured command |
| **Total** | **76** | all passing |

The suites live in the scratch directory and are not committed; they were
written to verify these specific fixes, not as a permanent test suite.