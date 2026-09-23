# Goal-Jev mode: multi-step goals with Jev

> Benchmark replication and the fixes it motivated (2026-09-23): see [BENCHMARK.md](BENCHMARK.md).

Status as of 2026-09-23. Goal mode is connected to the push-to-talk app and
remains available from the command line and live test harness. The historical
results below were measured before this integration; they are not a new score.

## Live voice session review (2026-09-23, 07:03–07:19 UTC)

26 goal-mode utterances: 12 done, 9 blocked, 2 stuck, 2 errors, 1 step limit.
Worked well: opening sites, Wikipedia search, Calculator 12 x 7 (six steps),
VLC English subtitles through menus, volume up four times, opening a file
from Explorer, switching to a named tab, and Like and Follow. The failures
came from a few general causes, most in capture and execution rather than
Jev's choices:

- Correct targets were rejected at execution (`youtube` link p=0.98; a
  WhatsApp chat that moved after a search). Re-resolution now counts
  duplicate UIA reports of one element as one element, and accepts a moved
  control when it is the only one with that name and role.
- "Go to WhatsApp (tab) and ..." chose switch_window with no matching window
  while the control head chose the tab at p=0.96. switch_window now falls
  back to a confident TabItem click.
- Typing had no field: Google Flights' input is an editable ComboBox, and
  Qt time boxes are Spinners. Both are now typing targets, and focused fields
  are marked `focused`.
- Google Drive tiles were never captured because the UIA walk stopped at
  depth 20; it now goes to depth 45. Subtrees wholly outside the window are
  skipped, which keeps long pages (Wikipedia used the full 3000-node budget,
  about 3.4 s per capture) cheaper.
- Web cards whose link name omits their title (Hugging Face model cards) now
  carry the inner text as `detail`, shown to Jev and added to ASR hotwords.
- A full-screen VLC exposed 0 controls; Jev wanted right_click but had no
  target. Sparse windows now offer their content area to right- and
  double-clicks. "Full screen" had no chord (Jev maximized instead), so there
  is now a Full screen (F11) chord.
- A menu-bar title with at least p=0.4 may be opened on a close call; this
  was View 0.49 vs Video 0.37 in VLC. Opening a menu only shows its items.
- Eleven scrolls in a row on a vague "keep clicking links" goal: six
  identical actions with a flat done estimate now stop the run.

Frozen-screen replays of the logged failures (two or three Jev runs each):
the WhatsApp tab 4/4, typing "Bombay" into "Where to?" 2/2, typing
"Ahmedabad" into the focused "Where else?" 3/3, full screen via F11 on both
steps 6/6, right_click on the full-screen video 3/3, and the Qwen card 2/2
(`detail` added by hand, since those logs predate it). Eight screens from runs
that worked live still made the same choices (each screen replayed 2–3 times),
and goal_sim completed 10/10. These are decision checks. The capture changes
(depth, pruning, detail, focus) have offline tests only; they still need a
live pass on Drive, Hugging Face, and Wikipedia.

Still open: VLC "jump to 25 minutes" needs text Jev cannot produce ("25:00"
is not a span of the request), open-ended goals ("click links until you reach
cars") remain weak, and "search to the last occurrence" cannot see Chrome's
find-bar count.

## Update after the original handoff

- Spoken and typed commands use goal mode by default. A persistent Multi-step
  goals setting selects the original single-step path. With auto-run off,
  every goal step waits for review.
  The pill shows each step and offers Stop; the activity panel records the
  completed steps and the proposed step when a run needs review.
- The voice path uses the same `run_goal` loop as the harness. A capture at
  key-down supplies speech hotwords and the starting context; goal mode now
  starts another capture at key release while final transcription runs. The
  first decision uses that release-time screen, so newly loaded controls in
  the same window are visible. If the foreground changes again before
  planning, it refreshes once more. This extra capture can add latency when
  UIA is slow; release wait time is logged, but live timing is not measured.
  Subsequent actions each capture and settle their resulting screen.
  Cancellation is checked before planning and immediately before execution.
- Goal mode asks the user to approve common irreversible actions (send,
  submit, delete, save, Enter on a send/submit form, paste, close window or
  tab), then resumes the same goal after Run. Cancel stops the goal. This is
  a conservative label-based guard, not a proof that every other UI action is
  harmless.
- Execution errors now stop the loop instead of allowing another Jev step.
  More than 254 candidate controls use the existing group-choice path instead
  of silently dropping candidates beyond the first 254.
- A new menu probe selects an ambiguous visible menu item and presses Right
  to try to expand its submenu without invoking a command. The recorded VLC
  English-subtitle failure showed that hovering `Sub Track` alone exposed
  nothing, then Jev chose `Add Subtitle File`. This is a general menu
  exploration change, but its live effect has not been measured. On the saved
  menu state, seven fresh Jev decisions picked the probe for English (three of
  three, plus an initial run) or clicked `Sub Track` directly for French
  (three of three); this measures decisions on a snapshot, not task success.
- `goal_replay.py` now repeats a saved evaluation step against Jev without
  moving the desktop. It supports a prompt-override JSON file for A/B work.
- Goal-mode typing now keeps exact quoted text and long text after a dictation
  cue or colon ahead of short word spans, preserving punctuation and avoiding
  the 14-word span limit for explicit messages. Jev still chooses only text
  supplied by the user, and uncertain text choices now stop before typing.
  Six fresh decisions on saved Google/YouTube typing
  screens kept their expected text; three decisions on a synthetic Notepad
  edit screen chose a 24-word message exactly. These are decision checks, not
  complete desktop tasks.
- A saved-screen A/B check removed the target-prompt sentence that prefers
  existing choices over add/import/browse. On four Jev runs per menu state,
  both prompts targeted `Sub Track` for English (4/4) and `Speed` for faster
  playback (4/4). For French, the current prompt targeted `Sub Track` 4/4;
  the abridged prompt targeted `Add Subtitle File` 0/4. The sentence stays
  for now, but all three snapshots are from VLC, so this does not establish
  cross-app generalization.
- A frozen synthetic-screen probe set now checks paired/new audio devices and
  saved/imported templates. It runs through the actual goal planner without
  moving the desktop. The first pass exposed a general menu-peek bug: the
  confidence comparison used an unscored target, so it peeked at a runner-up
  even when Jev strongly preferred the right item. Passing the scored target
  fixed that; the regression is covered by an offline test. After the fix,
  both the current prompt and the abridged prompt made the expected direct
  next choice on all 12/12 synthetic runs. Thus these probes do not justify
  the extra prompt sentence; only the VLC frozen-screen A/B favors it so far.
  On three new saved VLC decisions after the bug fix, French chose `Sub Track`
  directly 3/3; English chose a `Sub Track` probe 2/3 and `Add Subtitle File`
  1/3. This remains a next-decision observation, not a subtitle task result.
- Added a deterministic UI-state simulator that drives the same `run_goal`
  loop through changing screens, without mouse or keyboard actions. Five
  new multi-step chains cover Sound output selection, Task Manager Memory,
  opening a saved template, opening a report with a date filter, and typing
  an exact message into Notepad. The
  template chain exposed an overly strict
  stopping decision after the requested window was already in front. A
  general open/show completion cue plus a 0.75 done threshold completed 12/12
  simulated click chains in a fresh three-run check. The report case required four
  actions; its completion probability stayed at or below 0.15 until the
  filter was selected, then reached 0.93–0.94. Three replayed intermediate
  screens still chose actions and a completed YouTube screen still stopped
  (two Jev decisions each). These checks do not prove real app success, and
  the threshold and cue need live testing on unrelated tasks.
- The Notepad simulation verifies the exact 16-word message and punctuation;
  it deliberately exposes no editor value after typing, as can happen with
  UIA. Without a text-entry completion cue, Jev typed correctly then blocked
  with done probability 0.34. The general cue completed 3/3 Notepad runs and
  all five simulated chains 15/15. Saved Google weather and YouTube screens
  after typing still pressed Enter 3/3 each, so the cue did not treat a
  pending search submission as complete in those checks. An offline test
  confirms the simulator rejects wrong text. This is not a live Notepad score.
- Replayed the recorded Gmail failure. On the New Tab screen with an exact
  `Gmail` hyperlink visible, the old verb prompt submitted the typed search
  4/4 times. A general visible-link preference clicked `Gmail` 4/4; saved
  YouTube and weather search steps still submitted their searches 3/3 each.
  The preference is now in the shared goal prompt used by the voice app.
  The sign-in page in that same failed run exposed a separate error: Jev
  proposed typing `Gmail` into `Email or phone`. Goal mode now blocks a
  contact field unless the selected text is an email address or phone number,
  and blocks typing passwords or verification codes. Three fresh saved
  sign-in decisions stopped before typing. This does not establish that a
  signed-out Gmail task succeeds; an account login still needs user input.
- The shared goal loop now checks Stop again after a step leaves the approval
  gate and immediately before execution. An offline regression verifies that
  a Stop at this boundary cancels without sending the action.
- Added offline tests for multi-step observations, cancellation, review gating,
  execution failure, and a target after candidate 254. Added four unmeasured
  `challenge` tasks: a two-operation Calculator chain, VLC subtitles from
  Explorer, Task Manager Performance, and Settings Windows Update.
- The live `challenge` catalog now has six tasks. Two further unmeasured goals
  require Task Manager's Memory graph and the Bluetooth Add a device dialog.
- All 61 offline tests pass. The live harness now marks missing foreground
  windows and shell access denial as `invalid_environment`, and records other
  setup errors separately. It skips them instead of recording
  a model failure. VLC setup now replaces only its fixture process and leaves
  unrelated VLC windows open. An earlier live VLC attempt in this environment was
  **not a model failure**: `GetForegroundWindow()` returned 0 while VLC was
  open, so the harness could not observe the desktop. Interactive voice and
  challenge runs still need to be measured in an interactive desktop session.

## 1. The goal as given

> So currently this app works by taking one step (1/2 step for verb selection, 1/2 step for object candidate selection) right ? I am wondering if you can setup a "goal-jev" mode, so we can give jev tasks which require multi-step tasks, because lets be honest, most of the tasks we want to do are multiple steps.
>
> For example, I can say something like "open gmail for me", and the app should figure out that it should 1) Go to google chrome, 2) Open a new tab, 3) Click on gmail which is a link that shows up beside "Gmail, Images, box with 9 dots, and then the profile icon". The idea I had in my mind was to give Jev the same task with a system prompt telling Jev that it might require multiple steps to complete this task and Jev would get the initial state.
>
> Jev would then take the first turn (open("Google Chrome")) and we won't stop there, we would ask Jev to return something goal_end noul which would have a probability of 0.05 (say) since the goal is not reached.
>
> Then because the goal is not reached, we would automatically start a new Jev series of calls where we would give the system + task + initial_state + (the action taken in order to complete the goal) something like "You took {1} turn(s) till now to reach this goal, the last turn is {open("Google Chrome")}, continue towards the goal" + current_state (current state would show active_window as google chrome, instead of whatever the previous app was), so this would tell the Jev model that is progressing towards the goal, and then the second turn would continue, and then the third turn would take place, but along with the third turn, the model now also outputs goal_end with a noul probability of 0.95 and thus we consider the goal as reached and we stop right there.
>
> So at any point of time, we have
> System prompt
> Task
> initial_state
> {things done till now}
> current_state
>
> Maybe the last operation could be a noop with a goal_end probability of 0.95
>
> This was just one example, another example could be to load english subtitles for a movie that I am watching on VLC media player. Here the chain might look something like: 1) Right click 2) Select subtitles 3) Select english.srt or something like that from the list.
>
> This was the major goal, can you please build something that can solve this. This would be a really major milestone. I would want you to come up with a list of easy, medium, hard tasks and keep on hill climbing towards this as much as possible while making sure that the solution you keep is general (not over-fitted to a specific question). The solution not being overfit a particular set of questions is very very important.
>
> Note: The solution I proposed looks decent to me, but don't be bound by that as the only approach. Think about other ways as well. Keep in mind that we want to get rid of LLMs as much as possible. LLMs can be used for tasks like this where
>
> 1. They can generate a step-by-step very hand holded plan in english text, which we can pass to the Jev model and Jev can execute it
> 2. LLMs can also write their own automation script for every task and then run that code with some harness like claude code (you) and then complete all these tasks
>
> But these are approaches which cost a lot and at the same time are very slow, we want to avoid this by using Jev

## 2. Where we are (summary)

| | Result |
|---|---|
| Dev tasks (tuned against) | **8/10** |
| Held-out tasks (not tuned against, see caveats in §6) | **8/8** |
| LLM calls | **0**. Every decision is a Jev `choice` or `noul` question. |
| Jev requests per step | **1** (a second only if the screen is too large, see §3.3) |
| Typical time | 2–3 step goal: 7–10 s end to end; about 3 s per step |

Both results come from one run of each task in batch `20260923T021220`
(`logs/goal-eval.jsonl`, label `v7-final`). Jev is not deterministic, so the
tasks still need repeated runs (see §5).

Your two examples:

- **"Open Gmail for me"** passes in your signed-in Chrome: switch to the Chrome
  window, click the Gmail link on the New Tab page, done (done probability
  0.96). It fails in the throwaway, signed-out Chrome profile the harness now
  uses (details in §4).
- **"Load the English subtitles for this movie" (VLC)** still fails. Jev
  chooses *Subtitle → Add Subtitle File…* (a file dialog) instead of
  *Subtitle → Sub Track → English*. The held-out variant **"Turn on French
  subtitles"** passes every time: Subtitle → Sub Track → French.

## 3. How it works

### 3.1 The loop (`voice_control/goal.py`)

Your proposal is essentially what was built, with one change: the done check
and the action choice go in the **same** request instead of separate calls.

```
initial_state = read screen
repeat (max 12 actions):
    current_state = read screen   (active window + open menus, controls with UIA state,
                                   visible text, open windows, installed apps)
    ONE Jev request, all about the same state:
        state:     task, startedIn (initial window), stepsTaken ["1. switch_window ... -> active window is now X", ...],
                   activeWindow, exposedControls, visibleText, openWindows, installedApps
        questions: goal_done     noul    "is the whole goal already accomplished?"
                   next_action   choice  22 primitives (click, type, key, switch, launch, wait, no_action, ...)
                   control_target / field_target / window_target / app_target / key_target / chord_target
                                 choice  "assume the next action needs <this kind of target>: which one?"
    if goal_done >= 0.8, or next_action = no_action and goal_done >= 0.5: stop, goal reached
    take the top next_action and read its target head; if that target is not confident,
    try the 2nd/3rd action (no extra request)
    if the action types into a field: a SECOND request, same state plus targetField (the chosen field),
        asks text_to_type  choice  word spans of the goal ("Dubai" vs "Los Angeles" is decided knowing
                                   the field; a value may repeat in two fields when the goal says so)
    execute, wait for the UI to settle, record a summary of the step and what changed
stop as "stuck" if the same action twice in a row changes nothing
```

The five parts you listed map as follows:

- **System prompt:** the instructions on each question (`GOAL_PROMPTS`).
- **Task:** `task`.
- **initial_state:** `startedIn`.
- **Things done till now:** `stepsTaken`, one line per step. Each line gives
  the step's result and whether the active window or screen changed.
- **current_state:** the rest of the state.

Your "noop with goal_end 0.95" ending is the `no_action` + high `goal_done`
case.

### 3.2 Why one request instead of verb, then target

The earlier two-call design sent the target question a thin state (the verb
and the target list only). Asking every target head alongside the verb, each
seeing the full screen, was **faster** (≈0.9 s vs ≈1.2 s for two sequential
calls) and **sharper**. In the calculator case, "One" went from 0.43 to 0.55
against the next-best 0.11. It also makes fallback to a second verb free. The
idea comes from the `jev-ultrafast` browser agent in this folder, which asks
its operation and target heads in one request. Nothing from that project is
run.

### 3.3 Typing without an LLM

Jev cannot generate text. When a field is on screen, `text_candidates()` lists
every run of consecutive words in the goal (up to 14 words), and Jev picks
one. For example, "Search Google for weather in San Diego" → "weather in San
Diego" (0.97). The typed text is therefore always literally from the user; it
can never be invented or reformulated ("12 times 7" can't become `12*7`).

### 3.4 Other Jev findings

- The decisions endpoint supports three question types: `choice`, `noul`
  (returns one probability, exactly what `goal_end` needed) and `score`.
- Several questions can go in one request.
- Large screens can hit `max_tokens_exceeded`. Target heads that share
  candidates are merged (one control head for left/right/double-click and
  hover; one window head for switch, minimize, maximize and close). If a
  request still overflows, the step falls back to two requests: verb first,
  then only the needed heads.

### 3.5 General fixes found while hill-climbing

None of these is specific to a task; each fixes a class of apps or screens.

| Problem seen | Fix |
|---|---|
| Context/drop-down menus are separate top-level windows, invisible to the capture | Capture popup windows of the active app first, labelled "(in open menu)" |
| Popup scan picked up the taskbar and desktop when Explorer was in front (same `explorer.exe`) | Only popups above the active window in z-order; OS shell windows excluded |
| Menus, tool windows and overlays listed as "open windows" to switch to | `openWindows` follows the Alt+Tab rule (no caption-less popups or tool windows) |
| Notepad, Calculator, Settings and other Store apps could not be launched | App catalog adds packaged apps from `shell:AppsFolder`; documents and uninstallers are filtered out (219 apps, under the 255 limit) |
| UIA Invoke on Qt menu items runs the command but leaves the menu open | Menu items get a real mouse click, like a person's |
| An always-on-top window (Claude's own window) swallowed coordinate clicks | Hit-test before every coordinate click; cancel if another app's window covers the target |
| Just-launched apps read before they render (Calculator: 3 controls; Settings: 4 for over a second) | `capture_settled`: re-read a near-empty window for up to 4 s |
| Jev couldn't see results (Calculator display, headings) | Short read-only text (`Text`/`Header`/`StatusBar`) sent as `visibleText`, capped at 60 |
| Jev couldn't tell checked, selected or expanded items | UIA toggle, selection and expand state added to control labels where the app reports it |
| Dialogs were treated as their owner window | Only caption-less popups map back to the owner; a dialog is its own window |
| `wait`, scroll and zoom went through a pointless target call that answered "none" | "Whole-window" verbs need no target |
| Two identical window titles split the choice 50/50, so the gate refused | Only the most recently used of identical-title windows is offered |
| Two close items in an open menu | Hover (harmless) the runner-up first to reveal a submenu, once per item. Didn't help Qt, whose menus don't open submenus on a programmatic hover. |

## 4. The task set and hill-climb history

### 4.1 Tasks (`voice_control/goal_tasks.json`)

Each task has a spoken-style goal, a deterministic setup, and a check that
reads the system itself (window titles, UIA, and VLC's remote-control
interface), never Jev's done probability.

| Split | Level | Task id | Goal | Final result |
|---|---|---|---|---|
| dev | easy | blue_folder | Open the blue folder | PASS, 1 step |
| dev | easy | launch_calculator | Open the calculator | PASS, 1 |
| dev | easy | vlc_pause | Pause the video | PASS, 2 (Playback → Pause) |
| dev | easy | notepad_from_vlc | Open Notepad | PASS, 1 (switched to an open Notepad) |
| dev | medium | gmail | Open Gmail for me | **FAIL**: signed-out profile, typed "Gmail" into the Google sign-in page |
| dev | medium | vlc_subs_en | Load the English subtitles for this movie | **FAIL**: chose Add Subtitle File… |
| dev | medium | red_folder_from_vlc | Go to the Red Folder in File Explorer | PASS, 2 |
| dev | medium | google_weather | Search Google for weather in San Diego | PASS, 3 (switch → type → Enter) |
| dev | hard | calc_multiply | Calculate 12 times 7 | PASS, 6 (launch, 1, 2, ×, 7, =) |
| dev | hard | vlc_faster | Make the video play faster | PASS, 3 (Playback → Speed → Faster; measured 1.40×) |
| holdout | easy | green_folder | open the green folder please | PASS, 1 |
| holdout | easy | downloads | Show me my Downloads folder | PASS, 1 |
| holdout | medium | vlc_subs_fr | Turn on French subtitles | PASS, 3 |
| holdout | medium | youtube | Open YouTube in Chrome | PASS, 5 (type → Enter → click result → wait) |
| holdout | medium | settings_bluetooth | Open the Bluetooth settings | PASS, 2 |
| holdout | hard | calc_add | What is 45 plus 38? Use the calculator | PASS, 7 |
| holdout | hard | vlc_quieter | Turn the volume down in VLC | PASS, 3 (179 → 166) |
| holdout | hard | google_search_turing | Look up Alan Turing on Google | PASS, 3 |

### 4.2 Hill-climb (dev set, one run each)

| Version | Dev | What changed |
|---|---|---|
| v1 | 5/10 | First loop: noul + verb in one call, then a target call; popup capture; Store apps; hit-test |
| v2 | 7/10 | Taskbar-popup fix, real clicks on menu items, sparse-window re-read, UIA state, verb fallback, no target for whole-window verbs |
| v3 | 6/10 | One request with all target heads. One `max_tokens_exceeded` on Gmail; one pass was contaminated (see §6) |
| v4 | 8/10 | Merged heads + two-request fallback, visible text, dialog fix, fixed pause check. `google_weather` still contaminated |
| v5 | 7/10 | Throwaway Chrome profile for the harness: first honest Chrome numbers |
| v6 | 8/10 | Peek-hover, target margin 2.0×, identical-window dedupe. Held-out first run: 6/8 |
| v7 | 8/10 dev, 8/8 held-out | Longer wait for slow-loading windows (found via the held-out Settings task) |

The held-out 6/8 → 8/8 came from one eval-harness bug (VLC's volume reply was
misparsed; the agent had done the right thing) and one real fix (the
slow-loading-window wait).

## 5. Original handoff gaps and current status

Items 1–3 below were addressed in the update above. The remaining capability
and measurement gaps still apply unless explicitly marked otherwise.

**To make it usable:**

1. **Connect goal mode to the push-to-talk app.** The pill should show each
   step as it runs, there should be a stop control mid-goal, and the
   activity panel should show the steps. Also decide when a command is a goal
   and when it is single-step: always goal mode, a setting, or a different
   hotkey.
2. **Safety for auto-run across many steps.** A goal can chain clicks, typing
   and Enter into irreversible effects (sending, deleting, submitting forms).
   Options: confirm before `close_window`, Enter in forms, or buttons named
   Send/Delete/Submit.
3. **Offline unit tests for `goal.py`** (fake Jev and fake screen). The
   existing 31 tests pass, but the new loop has none.
4. **README section and a commit.**

**Known capability gaps:**

5. **Submenu contents are unknown to Jev.** The English-subtitles failure.
   Possible general fixes: keyboard-open a submenu to peek at it (Right arrow)
   instead of hovering; or backtrack (Escape) when a dialog clearly doesn't
   contain the goal.
6. **Text can only be word spans of the goal.** No "seven" → "7", no
   `youtube.com`, no "12*7". Deterministic rewrites could be added as extra
   candidates, still chosen by Jev.
7. **No recovery from a hung or blank window.** Found in the stale Settings
   window; relaunching the app would be the obvious move.
8. **Up to 255 controls per head.** Anything beyond #255 on a huge page is
   silently not offered, unlike the single-step mode, which never discards a
   candidate.
9. **The two-request overflow fallback has not been exercised live** since
   the heads were merged.
10. **Not yet supported:** scrolling to find off-screen or virtualized items,
    drag and drop, file dialogs, and text selection.
11. **Latency.** About 3 s per step: screen read ≈0.5–1 s, Jev ≈0.9 s,
    settle ≈0.45 s. Screen reads could be incremental.

**To trust the numbers more:**

12. **Repeat runs** (`--runs 3`). Every number above is a single run of a
    non-deterministic model.
13. **A broader, freshly written held-out set** in apps never touched during
    tuning: Slack (navigation only), Outlook, Word, Spotify, Paint, Settings
    sub-pages, Task Manager.
14. **A/B-test the task-motivated prompt sentences** in §6, keeping each one
    only if it helps on tasks it was not written for.

## 6. What might be cheating or overfitting

This is the honest audit of where the numbers may look better than the
system really is.

### 6.1 Prompt text written in response to a specific task

| Sentence or knob | Written because of | Risk |
|---|---|---|
| Target prompt: "prefer the entry that lists existing choices (a track, device, mode, or recent item) over one that adds, imports, or browses for a new file" | `vlc_subs_en` | **Highest.** Directly aimed at one failure. It didn't fix it, but it may help `vlc_subs_fr` (held-out), which uses the same menu path, so that pass is not independent evidence. Remove unless it earns its place in an A/B test. |
| Done prompt: effects that cannot be seen, "(playback speed, volume, sound, a keyboard shortcut)" | `vlc_faster` | The examples overlap the held-out `vlc_quieter`, so that pass is partly in-distribution. |
| Target prompt: "several targets in order (digits of a number, parts of a path, a menu and then its submenu)" | `calc_multiply` | The held-out `calc_add` is the same kind of task, so not independent evidence. |
| Target margin 2.5× → 2.0× in goal mode | Calculator: "One" at 0.43 vs 0.18 missed the 2.5× gate | A general knob, tuned on one example. No bad clicks seen in 18 tasks, but the sample is small. |
| Done threshold 0.8 → 0.75 and an open/show completion cue | Synthetic saved-template chain stopped after reaching its window | Both are general, but their measured improvement is from a synthetic case. Saved intermediate states did not stop early in two replays each; unrelated live tasks still need testing. |
| Text-entry completion cue | Synthetic Notepad message was typed exactly but invisible to UIA | 3/3 simulated Notepad runs completed; saved weather and YouTube searches still submitted 3/3 each. Real editor behavior remains unmeasured. |
| Prefer an exact visible destination link over submitting an address-bar search | Recorded Gmail failure searched for Gmail while the link was visible | On saved screens, Gmail clicked the link 4/4 while YouTube and weather still submitted 3/3 each. All are Chrome states, so cross-app behavior is unmeasured. |
| Peek-hover when two open-menu items are within 2× | `vlc_subs_en` | General mechanism, zero measured benefit (Qt ignores the hover). It adds steps, e.g. `vlc_faster` hovered twice before clicking in one run. |
| "Space" added to the key catalog | `vlc_pause` | Harmless and generic, but it was added for one task. |

### 6.2 The held-out set is weaker than it looks

- **Same apps as dev.** Held-out tasks use the same four apps and the same UI
  patterns: Explorer folders, VLC menus, Calculator buttons, Chrome
  address-bar search. They are near-neighbours of dev tasks, not a test in new
  territory. 8/8 overstates how well this generalizes.
- **One fix came from a held-out failure.** The slow-window wait was found and
  fixed after seeing the held-out Settings task fail. The fix is general, but
  the held-out set was used to find it.
- **Pass rates are single runs.**

### 6.3 The harness makes life easier than real use

- **Minimized always-on-top windows.** The harness minimizes always-on-top
  windows (Claude's) during runs. In real use such a window makes coordinate
  clicks cancel (safely, via the hit-test) instead of succeeding.
- **Clean starting states.** Setup gives fixed conditions: VLC started with
  the movie, Explorer at the fixture folder, a fresh Chrome profile, Settings
  and Calculator killed first. Killing a hung Settings window hides the gap
  "no recovery from hung windows".
- **Tidy fixtures.** Three clearly named folders; a movie with neatly labelled
  subtitle tracks. Real libraries are messier.
- **Non-destructive only.** No sending, deleting or form submission was
  tested, so safety under auto-run is unmeasured.

### 6.4 Measurement bugs that happened (all fixed)

- **VLC pause check:** `is_playing` stays 1 while paused, so early runs
  counted a correct pause as a failure.
- **VLC speed check:** the first check asked for an unsupported `rate`
  command. Its replacement passed at 1.09× with no action, so the threshold
  was raised to 1.3×.
- **VLC volume check:** the reply `audio volume: 192 … returned 0` was
  misparsed as 0.
- **Contaminated Chrome state:** early runs navigated **your own Chrome
  window** (a New Tab) to Gmail and then to the weather search. Later
  `google_weather` "passes" just switched to that window. That window was put
  back to a New Tab. The harness now uses a separate throwaway profile and
  refuses to count a task whose goal is already satisfied before it starts.

### 6.5 What is not cheating

- **No per-app selectors, scripts or plans.** The same code runs in Explorer,
  VLC (Qt), Calculator and Settings (UWP) and Chrome.
- **No LLM anywhere.** Every action is Jev choosing among things observed on
  screen. Typed text is always the user's own words.
- **Independent checks.** Success is read from the system, never taken from
  Jev's done probability.

## 7. How to run it

```powershell
# live evaluation (drives mouse and keyboard; don't use the PC meanwhile)
.\.venv-voice\Scripts\python.exe -m voice_control.goal_eval --split dev
.\.venv-voice\Scripts\python.exe -m voice_control.goal_eval --split holdout --runs 3
.\.venv-voice\Scripts\python.exe -m voice_control.goal_eval --ids vlc_subs_en gmail

# per-step Jev distributions for the latest batch (or --batch <id>)
.\.venv-voice\Scripts\python.exe -m voice_control.goal_inspect vlc_subs_en --controls 30
```

Results append to `logs/goal-eval.jsonl`, with the full request, response
and screen for every step. Chrome tasks use a throwaway profile in
`goal-fixtures/chrome-profile`. VLC tasks need the 32-bit VLC in
`Program Files (x86)`; the 64-bit folder on this PC is missing its plugins.

### Files

| File | Change |
|---|---|
| `voice_control/goal.py` | **New.** The goal loop, prompts, heads, peek, text spans |
| `voice_control/goal_eval.py` | **New.** Live harness: setup ops, independent checks, runner |
| `voice_control/goal_tasks.json` | **New.** 10 dev + 8 held-out tasks |
| `voice_control/goal_inspect.py` | **New.** Prints per-step Jev distributions |
| `voice_control/core.py` | `ask_questions` (multi-question and `noul`), `Control.hwnd`/`state`, `wait` verb, Space key, window dedupe, error bodies logged |
| `voice_control/windows.py` | Popup capture, Alt+Tab window rule, packaged apps, UIA state, visible text, hit-test, real menu clicks, `settle`, `capture_settled`, dialog-aware `app_window` |
