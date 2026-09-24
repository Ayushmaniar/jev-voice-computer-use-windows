# Jev Voice Control (Windows prototype)

A small, local push-to-talk app. It transcribes speech with `faster-whisper`,
reads the foreground window through Microsoft UI Automation, and asks Jev via
OpenRouter for the next action and whether a multi-step goal is done. It
re-reads the screen after every action, automatically runs the next safe step,
and records the result in local JSONL. It uses the same
collector for Explorer, browsers, Slack, VLC, and other Windows apps—no
app-name-specific selector rules. UIA quality still varies by app.

## Install and run

**Installer.** `installer\build.ps1` builds `installer\dist\JevVoiceSetup-<version>.exe`
(about 95 MB; needs Inno Setup 6.5+). Setup installs per user, without admin
rights, into `%LOCALAPPDATA%\Programs\Jev Voice Control`: a relocatable CPython
(python-build-standalone) with the app's packages preinstalled, the app, and
`JevVoice.exe`, a small launcher with the app icon. It adds a Start menu entry and,
if chosen, a desktop shortcut and a sign-in start. The bundled Python runs
with `-E -s -P`, so Python installs, variables, and packages elsewhere on the PC
can't affect it. The installed app keeps its key, settings, logs, and speech
models in `%LOCALAPPDATA%\Jev Voice Control` (`paths.py`). Uninstalling asks whether
to delete those too.

The NVIDIA libraries (cuBLAS, cuDNN, NVRTC: about 1.3 GB) are not bundled. When the
PC has an NVIDIA driver, Setup offers to download them. `build.ps1` pins the wheels'
URLs and SHA-256 hashes, Setup checks them, and installs the wheels into `gpu\`,
which is kept across upgrades. If the download fails, the app uses the CPU.

`installer\install.ps1` runs the same Setup silently and prints the status, for
scripts and coding agents (see `AGENTS.md`). Setup's own switches are
`/VERYSILENT /SUPPRESSMSGBOXES /TASKS="desktopicon,startup,gpu" /KEYFILE=<.env> /LOG=<file>`.

**API key.** Put in an OpenRouter key (`sk-or-...`, which calls Jev through
OpenRouter) or a TypeSafe AI key (which calls `api.typesafe.ai/v1/systemone`,
model `jev-latest`, directly). Setup, the app's first-start key window, and
`configure.py` all look for a key already on the PC: `OPENROUTER_API_KEY` /
`TYPESAFE_API_KEY` environment variables, and `.env`, `.env.local`, `.env.openrouter`,
and `.env.typesafe` files in the current folder, the repository, home, Desktop,
Documents, `source\repos`, `projects`, `code`, `dev`, `repos`, `src`, `git`, and
`workspace`, plus one level of subfolders in each. Any `sk-or-` value counts, even
under another variable name. They offer what they find along with the file it
came from; nothing is saved until you confirm (or run Setup silently, which
uses the first key found when no key is saved). Change the key later from the pill's right-click
menu, the tray menu, or the panel's **API key** link. A saved key goes into
`.env.openrouter` or `.env.typesafe`, and the file for the other service is removed. An
`OPENROUTER_API_KEY` or `TYPESAFE_API_KEY` environment variable overrides both
files.

`configure.py` does the same from a terminal (installed: `configure.cmd` in the
install folder):

```powershell
.\.venv-voice\Scripts\python.exe -m voice_control.configure find-keys          # masked keys and where each was found
.\.venv-voice\Scripts\python.exe -m voice_control.configure set-key --found 1 --check
.\.venv-voice\Scripts\python.exe -m voice_control.configure status --json      # exit 0 = ready, 2 = no key
```

**From source.** From the repository root in PowerShell:

```powershell
python -m venv .venv-voice
.\.venv-voice\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv-voice\Scripts\python.exe -m voice_control.app
```

A source checkout (it has `requirements-voice.txt` at the root) keeps the key,
settings, logs, and models in the repository root, as before; `JEV_VOICE_HOME`
overrides the folder. To add a **Jev Voice Control (source)** Start menu shortcut for the
source checkout (separate from the installed app's), run `.\voice_control\install_start_menu.ps1`. The logo and an illustrative app concept
are in `voice_control/assets/`.

On first run the app downloads its speech model to `.voice-model-cache`. Only one
copy runs at a time.

### Using it

The app runs in the background. There is no main window; there are two pieces
of UI:

- **The pill**: a small dark bar at the bottom center of the screen. Idle, it
  is just a status dot (grey while the speech model loads, green when ready)
  and a chevron. Hover it for a hint. It never takes keyboard focus, so the app
  you are talking to stays active. Drag it anywhere; the position is remembered.
- **The activity panel**: click the chevron (or the tray icon) to open a list
  of recent commands: what you said, what ran, how confident Jev was, which
  window it acted on, and a timing bar (heard / screen / Jev / ran). Click a
  card for the full target, result, error, and stage timings. The panel also
  has the settings toggles and a field to type a test command without a
  microphone.

Hold **Right Ctrl alone** while another app is active and speak. The pill
shows live captions while you hold the key (re-transcribed about every 0.7 s
on CPU), then each goal step and the result, and collapses back to the dot after a few
seconds. Pressing any other key during the hold cancels recording silently,
so Right Ctrl shortcuts keep working. Left Ctrl is ignored.

**Multi-step goals** are on by default for spoken and typed commands. The pill shows the current step and a **Stop**
button; the tray menu also has **Stop current goal**. The activity card lists
completed steps and, if the run stops for review, the proposed next step.
The loop ends when Jev reports completion, a step fails, progress stalls, you
stop it, or it reaches 12 actions. Goal mode asks you to approve a step before
clicking a deletion-like control (Delete, Remove, Erase, Discard, Trash, or
Uninstall) or pressing the Delete key. Save, Send, Submit, the Save shortcut,
Enter, paste, and closing a window or tab run automatically
when auto-run is on. Click **Run** to continue the same goal or **✕** to
stop it. Turning off **Run actions automatically** makes every goal step wait
for review. Toggle **Multi-step goals** off to use the original single-step
planner.
A field named Email or Phone is offered only the email addresses and phone
numbers in your request, so goal mode never types a site name into a sign-in
box; with none in the request it stops. Password and verification-code fields
require manual entry.

**Run actions automatically** is on every time the app starts (switching it off lasts until the app is restarted): clicks, typing, shortcuts,
launching, and closing execute as soon as Jev returns a valid plan. Turn it
off (panel, tray menu, or right-click the pill) and the pill instead shows the
planned action with **Run** and **✕** buttons. Auto-run does not bypass
confidence, target, or foreground checks. It can still trigger irreversible
effects in other apps, so turn it off when working with sensitive content.
While a goal action runs the pill becomes briefly click-through, so it does
not intercept a click aimed at a control underneath it.

**Hide pill when idle** removes the pill until you speak; the tray icon then
remains the way to open the panel or quit. Settings live in
`.voice-settings.json` in the data folder (the repository root for a source checkout).

### Speech recognition

On an NVIDIA GPU the app uses `large-v3-turbo` (float16), which transcribes a
typical 2–5 s command in about 0.2 s on an RTX 4070. Without a usable GPU it
falls back to `base.en` on the CPU (about 0.6 s, noticeably less accurate).
Override either with `JEV_VOICE_MODEL` (GPU) or `JEV_VOICE_CPU_MODEL` (CPU).
CTranslate2 needs CUDA 12 cuBLAS and cuDNN 9; the `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12` wheels in the requirements provide them, and the app adds
their `bin` folders to the DLL search path at startup. If there is no CUDA GPU
or those libraries don't load, the app goes straight to the CPU model rather than
downloading the GPU one first. A warm-up pass proves the GPU works before it is used.
The first GPU start downloads about 1.6 GB.

Screen reading starts the moment Right Ctrl goes down, in parallel with your
speech. The names it finds (list items, tabs, links, buttons, window and app
names) plus the command vocabulary are passed to Whisper as `hotwords`, so
"Kavya Rao" or "Anjali Venkatesh" come out spelled the way they are on
screen instead of "Vibamurthy" or "Cerreli Nundukumar". The hint text used is
logged as `asr_hotwords` with `asr_model` and `asr_device`. Goal mode starts a
second capture when you release Right Ctrl, overlapping final transcription.
Its first decision uses that current screen, while the key-press window stays
in the goal's starting context. If the foreground changes again before
planning, goal mode refreshes once more. Single-step mode uses the key-press
capture.

## Commands and action semantics

The table below describes the original single-step mode. Goal mode chooses
from the same primitives, but repeats observe → decide → act until it stops.

| Say | Verb | Target/argument |
| --- | --- | --- |
| “Open the first link” | `left_click` | First UIA hyperlink in tree order |
| “Click the button below Notifications button” | `left_click` | Closest clearly-below control in the same column |
| “Right-click Downloads” | `right_click` | Eligible control |
| “Double-click the folder” | `double_click` | List/tree item or link |
| “Hover over Settings” | `hover` | Observed control and its screen bounds |
| “Scroll down” / “Scroll up” | `scroll_down` / `scroll_up` | Current window, three wheel notches |
| “Scroll left/right” | `scroll_left/right` | Horizontal wheel event at window center |
| “Zoom in/out/reset” | `zoom_in/out/reset` | Current window, Ctrl shortcuts; app-dependent |
| “Type hello world” | `type_text` | An editable field; literal text after “type” |
| “Press Escape” / “Press Enter” | `press_key` | Allowed key from finite catalog |
| “Go back” / “Next tab” / “Undo” | `key_chord` | Allowed shortcut from finite catalog |
| “Switch to Slack” | `switch_window` | A named open window |
| “Go to previous window” | `alt_tab` | Windows' previous window, not a named destination |
| “Launch VLC” | `launch_app` | Installed Start-menu shortcut |
| “Turn the volume down” / “Brightness to 70%” / “Max brightness” | `set_slider` | An exposed Slider, and how far: ±5/10/25% of its range, max, min, middle, or a number the user said |
| “Minimize/maximize/close this window” | matching window verb | Current window |

Jev's prompts say the task is a speech-recognition transcript, so it matches
names by sound ("Cavia Rao" → "Kavya Rao", "Marisal" → "Marisol"). It
acts on a sound-alike only when one option is clearly closest; with two
similar candidates it chooses none. Badly garbled phrases still fail and need
better recognition, not prompting.

`set_slider` works on any UI Automation Slider (a web page's range input or
ARIA slider, Windows Settings' volume and brightness, a player's volume or
seek bar). Jev sees each slider's value and range (`Slider "Brightness" =
"40 (0 to 100)"`; an unnamed one is described by its length and place) and
chooses a categorical change, never a raw number; the change is converted
against the slider's own range. `sliders.py` sets the value through the
RangeValue pattern, else with the keyboard when the slider takes focus, else
by clicking its track and correcting from the value read back (VLC exposes
only a value), and reports the value before and after.

`switch_window` is deterministic only when one observed window matches. `alt_tab`
is a relative history operation: Windows, not Jev, decides where it lands.
Zoom is an intent-level verb implemented with a common Ctrl shortcut; it is
not a universally supported mouse primitive. Crucial future primitives are
container-specific scroll, drag-and-drop, text selection, clipboard inspection,
and app-specific commands such as media seek. These need typed arguments and
their own verification/policy, not just more words in a verb list.

For >254 eligible targets, both planners retain **all** of them and perform a
group-choice Jev call before the final target call. A well-defined ordinal or
spatial reference can identify the group locally, preserving two Jev calls.
No candidate is silently discarded. Very large pages may make group calls
slow. If no unique spatial match exists, or Jev contradicts a computed
relative hint, execution is cancelled.

In goal mode, Jev can choose exact quoted text or a long message after a cue
such as “write this message:” from the user's request. It cannot invent or
rewrite text; the single-step `type <text>` command still enters the literal
dictation after “type”.

## Settling and stale decisions

After each goal step the app waits for the screen to settle before reading it
again (`settle.py`). The old wait only watched top-level windows, so inside an
app it was a fixed ~450 ms: too long when nothing happened, too short while
content was still arriving, and Jev then spent whole steps choosing `wait`.
Now the app watches what the action started, in any app, with nothing to
install (`uia_watch.py`):

- **UI Automation events** from the window being worked on: controls added or
  removed, names, values, toggles and expansion changing, menus and windows the
  app opens. Nothing within the action's short window (0.15 s; 0.5 s after
  typing; 0.6 s after a link or Enter) → the next step starts. Changes → it
  waits until they stop for 0.15 s.
- **Loading indicators**: a progress bar or a control named "Loading…" holds
  the step (up to 15 s), and a screen read that shows a newly appeared one is
  repeated. An indicator that is always there only holds the first reading.
- **The busy cursor** over the window (Windows' wait / working-in-background
  cursor) also counts as loading.
- **Pixels**, for windows that expose fewer than 30 controls (video, canvas,
  games): a change to more than 0.2% of the window counts.

A window that was changing steadily before the action (a clock, a carousel, a
playing video) is recognized, so its own animation does not hold steps.

UI Automation cannot see a request until its result reaches the screen, such as a
slow server behind a link. Two things cover that:

- **Stale decisions.** The window is marked when a screen read starts. If it
  changes before Jev's decision is carried out, the decision was made on an
  old screen, so the loop reads the screen again. Usually the change was
  elsewhere: when the chosen control is still there, in the same place and
  state, with nothing new over it (for keys: the same field has focus), the
  decision stands without asking Jev again (`stale_kept` on the step). A
  `wait`, a scroll, or a target that moved, changed or got covered is decided
  again on the fresh screen (`stale_replans`). At most twice per step.
- **`wait` waits for the change.** Jev's `wait` waits for the window to change
  and then settle (up to 3 s), so one `wait` covers late results instead of
  several in a row.

The step result names the reason, e.g. `(settled in 187 ms: nothing changed)`.
Windows UI Automation cannot follow fall back to the fixed window-level settle.

## Logs, timing, and privacy

Each utterance produces a plan/error record and, if executed or discarded, a final record
with the same `utterance_id` in `logs/voice-actions.jsonl`. The plan/error
record includes recognized words, chosen target, Jev choices/probabilities,
per-stage milliseconds, outcome, and errors, plus everything needed to replay
the command: `inputs` (every captured control with bounds, open windows,
installed app names), `prompts` (the `PROMPTS` dict in force), and `jev_calls`
(each exact Jev request body and response). Final records omit those three
fields. A plan record is roughly 40 KB. `transcribe` runs from
release of Right Ctrl to complete text;
`state` covers active window, window inventory, UIA controls, and a cached
Start-menu app list; `jev_verb`, optional `jev_group`, and `jev_target` cover
network calls; `execute` is reported separately. Every microphone command's
audio is saved as a 16 kHz WAV in `logs/audio/<utterance_id>.wav` (named by
`audio_path` in its plan record) so transcription changes can be tested on
real speech; delete that folder to discard it. API keys and Start-menu
shortcut paths are not logged. Logs and model cache are gitignored,
but logs contain the full on-screen text of each window you command (message
previews, file names, window titles) in plain text. Jev
requests transmit task text, exposed UI labels, open-window titles, and
installed app names to OpenRouter or TypeSafe AI, whichever your key is for.

## Prompt A/B testing

All prompt text lives in `PROMPTS` in `core.py`. `replay.py` re-plans logged
commands with the captured state held fixed, comparing the prompts logged with
each command (A) against the current `PROMPTS` (B):

```powershell
# 1. Prefill logs/replay-labels.json with what originally happened; fix any
#    wrong verb/target (control id, or "none" if nothing should happen) and
#    set "reviewed": true.
.\.venv-voice\Scripts\python.exe -m voice_control.replay --write-labels
# 2. Edit PROMPTS in core.py (or put overrides in a JSON file for --candidate).
# 3. Compare; each variant runs --runs times because Jev is not deterministic.
.\.venv-voice\Scripts\python.exe -m voice_control.replay --runs 3
.\.venv-voice\Scripts\python.exe -m voice_control.replay --candidate my-prompts.json --last 20
```

Before any Jev call, replay rebuilds each logged request offline from `inputs`
and requires it to match the logged request byte for byte; commands that do
not match (for example, after changing how targets are filtered or described)
are skipped, because their state would no longer be what Jev originally saw.
`--check-only` runs just that check. Records written before this logging
existed have no `inputs` and cannot be replayed.

## Current limitations

- The app is a prototype, not an unattended general-purpose agent. Auto-run is
  the default as requested. The goal-mode guard recognizes common action
  labels and keys; it cannot prove that every arbitrary UI action is reversible.
- UIA can omit controls, supply bad bounds, or expose virtualized/offscreen
  items. “First link” means first in UIA tree order, which may differ from
  visual or document reading order. “Below” needs reliable screen rectangles.
- The current scroll action uses the window center and may miss a nested
  pane. Zoom shortcuts do not work in every app.
- Speech is English only. There is no wake word or continuous listening;
  audio is captured only while Right Ctrl is held, and each command's clip is
  kept in `logs/audio/` (see Logs above). Live captions re-transcribe the whole utterance
  so far on the same worker, so the final transcript can wait for up to one
  caption pass after you release the key.
- Microphone → UIA → Jev planning and typed command → reviewed execution have
  been tested live. An uninterrupted microphone → click action still needs
  an interactive check, especially with auto-run enabled.

Run pure tests with:

```powershell
.\.venv-voice\Scripts\python.exe -m unittest voice_control.test_core voice_control.test_goal voice_control.test_settle voice_control.test_sliders -v
```

The live goal harness and its task catalog are in `goal_eval.py` and
`goal_tasks.json`. The `challenge` split has six harder Calculator, VLC,
Task Manager, and Settings chains; no success rate has been measured
for that split yet. Run it from an interactive Windows desktop with
`.\.venv-voice\Scripts\python.exe -m voice_control.goal_eval --split challenge`.

For read-only next-decision checks on synthetic screens, run
`.\.venv-voice\Scripts\python.exe -m voice_control.goal_probe --runs 3`.
Add `--ablate-existing-preference` to compare the target prompt without its
existing-choice preference sentence. These probes do not move the desktop or
measure completed tasks.

For complete chains through deterministic synthetic screens, run
`.\.venv-voice\Scripts\python.exe -m voice_control.goal_sim --runs 3`.
This exercises the real goal loop and its stopping decision, but not Windows
capture or action execution. The five cases include an exact Notepad message
whose text is hidden from the simulated UIA screen after typing.

To re-plan a saved step without moving the desktop, run
`.\.venv-voice\Scripts\python.exe -m voice_control.goal_replay vlc_subs_en --step 2 --runs 3`.
This checks Jev's next decision on a frozen screen; it cannot measure whether
the resulting action works in the app.
