# Goal mode on a replication of the Jev computer-use benchmark

Steve Sewell (Builder.io) published a comparison of Jev-based computer use against Luna, a screenshot-based model:
[steve8708/jev-browser-benchmark](https://github.com/steve8708/jev-browser-benchmark) (MIT). His best Jev system
scored 19/51 to 19/57 overall (33–37%); Luna and a Jev-first hybrid scored 51/57 (89.5%).

This document describes how goal mode (`goal.py`, driven by `bench_eval.py`) was run against a rebuilt version of
that benchmark on 2026-09-23, what was changed to improve it, and why the result should be compared with care.

![Goal mode on the replicated benchmark](../logs/bench-plot.png)

## Current results: real accounts, all tasks counted (2026-09-23, afternoon)

The overnight set (below) left out tasks Jev cannot do and used local mocks for every signed-in app. This round
moves closer to the original and reports out of every task:

- **Tasks Jev cannot do are back in the denominator** (`"infeasible"` in `bench_tasks.json`, split `added`): a
  password login, a feedback form, a Gmail reply and a Word note that need text Jev would have to write. Composed text
  passes only if it has enough words and is not just a stretch of the request typed back (`bench_native.composed`).
- **Real apps in place of stand-ins** (the stand-ins are kept with `"retired": true`): Spotify (signed in by the user)
  replaces VLC; the user's own Gmail and Drive replace the mail and drive mocks. These run in the user's everyday
  Chrome, in a window each task opens and closes itself, on test items prefixed "Jev bench". Send is never approved.
  Gmail moves and Drive changes are judged afterwards through the Gmail and Drive APIs (`--verdict`). The reply tasks
  are read from the reply box before the window closes. Notion and Figma stay local mocks (no accounts), and so do the
  calendar and tracker, which were extra stand-ins of ours.

| Two full runs, pooled | Attempts | Success | Cost per success |
| --- | ---: | ---: | ---: |
| **All 61 tasks (headline)** | 2 × 61 | 83/122 · **68.0%** | $0.0032 |
| Only the 57 tasks Jev can do | 2 × 57 | 83/114 · 72.8% | $0.0030 |
| – simple browser (44) | 2 × 44 | 71/88 | |
| – longer workflows (11) | 2 × 11 | 8/22 | |
| – native desktop (6) | 2 × 6 | 4/12 | |
| – infeasible tasks | 2 × 4 | 0/8 | |

Runs: `real-a` 42/61, `real-b` 41/61. On the real accounts:

- **Gmail move: 0/2.** Jev ticks the invoice, opens Move to and clicks the Jev bench Finance item, but the move does
  not take effect. In run A it did take effect minutes later, while later tasks were still using that window, so it was
  counted as a fail. A clean rerun confirmed the click alone does nothing.
- **Gmail reply with given text: 2/2.** Jev opened the thread, clicked Reply and typed the text, then stopped at Send.
- **Drive: 0/2.** Once Jev created the folder but did not move the doc into it. The other time it found no confident
  target.
- **Spotify: 0/2 in the full runs,** though a smoke run passed (search, then Play). In both full runs the first step
  found no confident target.
- **Infeasible tasks: 0/8, as expected.** Jev refuses to type the password, types a stretch of the request instead of
  writing a note, or finds nothing to type.

Harness problems found and fixed during the round (none change goal mode):

- The first offsite email ended with "Safe to delete". The inbox row includes that text, so the review gate blocked
  opening the email. It was replaced, and those runs were redone.
- Gmail asks "Leave site?" when a window with an unsent reply is closed. At first that dialog kept the focus, so later
  tasks ran in the wrong window. Setup now requires the new window to be in front, and closing answers the dialog for
  the task's own window. The affected tasks were rerun.

## Overnight results (2026-09-23, before real accounts)

| | Tasks | Success | Cost per success | Avg time |
| --- | ---: | ---: | ---: | ---: |
| Baseline (goal mode before tonight) | 57 | 33/57 · **57.9%** | $0.0035 | 5.1 s |
| Final (two full runs, pooled) | 2 × 57 | 84/114 · **73.7%** | $0.0031 | 6.2 s |
| – simple browser | 2 × 42 | 70/84 · 83.3% (baseline 29/42) | $0.0020 | 4.5 s |
| – longer workflows | 2 × 10 | 10/20 · 50% (baseline 3/10) | $0.0087 | 12.4 s |
| – native desktop | 2 × 5 | 4/10 · 40% (baseline 1/5) | $0.0078 | 8.5 s |
| – dev split (fixes diagnosed here) | 2 × 29 | 48/58 · 82.8% (baseline 17/29, 58.6%) | | |
| – **holdout split (never tuned on)** | 2 × 28 | 36/56 · **64.3%** (baseline 16/28, 57.1%) | | |

Both final runs scored 42/57 with the same 15 failures. For comparison, the published overall numbers are Jev Browser
19/57 (33.3%, $0.0018 per success), Jev Ultrafast 19/51 (37.3%, $0.0012), and Luna and the Hybrid 51/57 (89.5%,
$0.0128 and $0.0090).

Read the holdout line first. Most of the gain is on the half of the tasks whose failures were looked at (+24 points
on dev against +7 on holdout). The fixes carry over only partly, which is what one would expect from fixes found on a
few examples, and it is the most honest single estimate of what tonight's work adds on unseen tasks. Goal mode also
started higher than Steve's Jev systems (58% against 33–37%). His fixtures were built independently of any harness,
while ours were built by the same person who tuned the harness, so the two numbers are not the same measurement.

## Why this is a replication, not a rerun

The public repository contains results and methodology only. Its README says the runner depends on private test
accounts, local browser sessions and desktop apps, and none of the 57 task fixtures are published. So the three
lanes were rebuilt from their published descriptions:

| Lane | Published description | Rebuilt as |
| --- | --- | --- |
| Simple browser (42) | "navigation, clicking, selecting, scrolling, forms, small edits, and state checks"; the thread names multi-field forms, spreadsheet cell edits, rich-text formatting, drag-and-drop, embedded forms, shadow-DOM controls and file uploads as Jev's failures | 42 local pages in `goal-fixtures/bench/simple`, 6 per category plus 8 for the named hard cases, served by `bench_server.py` |
| Longer browser workflows (10) | flight search and fare selection, a Thai restaurant reservation in SF up to the confirmation boundary, TodoMVC project tracking, and authenticated Google Drive, Notion, Figma and mail checkpoints | the three public sites (Google Flights, OpenTable, TodoMVC), stopping before anything irreversible, plus local stand-ins for the authenticated apps (mail ×2, drive, notes, calendar, issue tracker, design tool) in `goal-fixtures/bench/long` |
| Native desktop (5) | Spotify playback, TextEdit typing, Pages drafting, Keynote slide creation, Numbers spreadsheet entry (macOS) | Windows equivalents: Notepad, Word, PowerPoint, Excel, and VLC for playback (Spotify is installed but signed out, and goal mode never logs in) |

Other differences that matter:

- **Observation.** His Jev systems read the DOM; goal mode reads Windows UI Automation, for the browser and native
  apps alike. Neither sees pixels.
- **Platform.** Windows 11 instead of macOS.
- **Cost.** Ours is Jev's own reported `usage.cost` per request, summed per task. No text model is used (goal mode
  types only literal spans of the request). His Jev costs include whatever text model his harnesses used.
- **Success checks** read the page's own state (`bench_server` records what the page changed), the app itself
  (Office through COM, Notepad's saved file, VLC's remote-control interface), or the window title. They never use
  Jev's done estimate.
- **Approvals.** Goal mode asks before risky steps (Send, Save, Post, Delete…). On local fixture pages the harness
  approves them, standing in for the user; on real sites it never does, so those runs stop at the review gate,
  which is the "safe checkpoint" his long lane also used.

## Exclusions

Following the instruction to skip tasks Jev cannot do at all because the text to type is not in the request:

- No task asks goal mode to compose text (Pages "drafting" became typing two given lines). Every typed value appears
  verbatim in the goal.
- No password login: goal mode refuses to type passwords by design.

## Protocol against overfitting

- Fixtures and checks were written before the first run and not edited afterwards to make a task pass. One goal was
  reworded after the baseline, before any fix: "Play the song Harbor Lights" became "In VLC, play the song Harbor
  Lights", because the original was ambiguous (Jev went to the user's real YouTube Music tab) and Steve's task named
  its app. Its baseline was rerun with the new wording.
- Tasks alternate between a `dev` and a `holdout` split within each category. Failures were diagnosed from `dev`
  traces only. Holdout lines appeared in whole-run printouts, but no fix was designed from a holdout trace; two
  holdout failures whose cause was visible (the "Email" field guard on the contact form, and Excel cells not being
  targets) were deliberately left alone.
- Every change had to be something that helps on any app, and each is listed below with the trace that motivated it.
- The existing suites (`goal_tasks.json`, `goal_sim_cases.json`, unit tests) were rerun as a regression check.

## Changes, and the failure behind each

Code changes (all in the shared goal-mode path, so they apply to live voice use too):

| # | Change | Where | Failure that showed it (task, split) |
| --- | --- | --- | --- |
| 1 | Each UI Automation element is walked once (by runtime id). While a native `<select>` is open, Chrome's tree loops back on itself; the walk repeated a 37-control page into 949 controls and Jev's API rejected the request (`max_tokens_exceeded`). | `windows._collect` | select_country (dev) |
| 2 | An option in an open dropdown is chosen with a real click. UIA Invoke selected it but left the list open, so the next click landed on the list and picked a different option ("Maya" became "Jordan"). | `windows.execute` | tracker_new_issue (dev) |
| 3 | Buttons report their toggle state (`[checked]`/`[unchecked]`). A switch's click showed no change, so Jev clicked it again and turned it back off. | `windows._ui_state` | shadow_notifications (dev) |
| 4 | A click repeated on a control whose first click changed nothing is sent as a real mouse click instead of Invoke. | `goal.run_goal`, `windows.execute` | shadow_notifications (dev) |
| 5 | Ctrl+S ("save") is an available key chord, reviewed like a Save button. | `core.CHORDS`, `goal.action_requires_review` | shortcut_save_doc (dev) |
| 6 | A control whose label changed in place (a date button whose price finished loading) is still found by role and exact position. | `windows._find_fresh_wrapper` | flights_search (dev) |
| 7 | An editable Document (Notepad's text area, a mail body) is a typing target; a web page's read-only Document is not. | `windows._collect`, `core.FIELD_ROLES` | notepad_append (dev) |
| 8 | Characters are typed 10 ms apart. Notepad dropped the keys after "4 " in a burst ("4 PM" arrived as "4 "). | `windows.execute` | notepad_append (dev) |
| 9 | Repeated names carry the text read just before them within their own neighbourhood (three levels of ancestry): `Button "Rename" near "draft.txt"`. The first version used plain reading order, which labelled Chrome's tab-strip Close button with the page's last paragraph; Jev took it for a popup's close button and closed the tab. Scoping fixed that. | `windows._collect`, `core.describe_control` | edit_rename_file, click_like_photo (dev); the tab-closing regression surfaced on click_close_modal (holdout) and was fixed because it was a bug in this change, not to fit that task |
| 10 | A Windows combo box's own drop-down arrow (a Button named "Open"/"Close" inside it) is not offered. In the file dialog it sat next to the real Open button and Jev clicked it ten times. | `windows._collect` | upload_receipt (dev) |
| 11 | New `select_text` action (goal mode only): selects a literal span of the request inside a field through UIA's text pattern, as a person drags across words, e.g. before Bold or Copy. | `goal.py`, `core.eligible_targets`, `windows.execute` | edit_richtext_bold (dev) |
| 12 | Text already typed into a different field is not chosen again (the choice is renormalized over the other candidates); and when the text to type is unclear, the next likeliest action is tried instead of stopping, as with unclear targets. | `goal._unused_text`, `goal.plan_step` | flights_search (dev): "New York" was typed into Departure |
| 13 | While Jev keeps waiting and the latest wait brought nothing new, the last plain button or link click (no toggle state, no risky word) is repeated with the real mouse. | `goal._waiting_on_a_silent_click` | flights_search (dev); on inspection that click had worked, so this rule rarely fires |

One prompt change was kept, one reverted:

- Kept: the text prompt now excludes words that only say where or how to enter text ("at the end", "on a new line",
  "in the subject"). Jev had split between "Meeting moved to 4 PM" and "Meeting moved to 4 PM at the end of this
  note" (notepad_append, dev).
- Reverted: "when the goal lists several items, type only the next one". It made Jev consider "Meeting" on its own
  a candidate and broke notepad_append. TodoMVC (which motivated it) still types all three to-dos as one.

Harness fixes (these make runs independent and fair; none changes goal mode):

- The throwaway Chrome profile starts with its accessibility tree on (`--force-renderer-accessibility`) and waits
  until page controls are exposed, as in a browser the voice app has been querying. Without it the first capture of
  a fresh browser held only the window frame.
- Its autofill data is deleted before each launch (earlier runs' entries popped up over fields).
- Office tasks use their own COM instance, close any leftover copy of the fixture, and suppress save prompts; only
  processes the benchmark started are ever closed. The benchmark's playlist VLC is closed at the end (it held the
  remote-control port the existing VLC checks use).
- Each Notepad run gets a fresh file name (Notepad keeps a reopened file's old text in its tab), and the check is
  exact, so a doubled line fails.

## Still failing, and why

Fifteen tasks failed in both final runs.

Diagnosed from dev traces:

- **flights_search:** the route and date get filled in, but the trip stays "Round trip" although the goal says
  one-way, so Search reopens the date picker asking for a return date and Jev waits.
- **opentable_thai:** the one search box takes "Location, Restaurant or Cuisine". Jev splits between "Thai" and
  "San Francisco", and neither is a confident text choice.
- **todomvc_items:** all three to-dos are typed as one item. The generic prompt fix for this was reverted (above).
- **powerpoint_slide:** PowerPoint does not expose a new slide's title placeholder through UI Automation, so there
  is nothing to type into after New Slide. Seeing it would need another channel (vision or the Office object model).
- **vlc_play_track:** the track is only listed in VLC's playlist, which is hidden until View → Playlist is opened;
  Jev looks under Media instead.

Holdout failures, described from their outcomes and goal mode's known gaps, not debugged:

- **nav_footer_help, scroll_beta_setting:** the target sits below the fold. Capture skips parts of the page outside
  the window, so Jev cannot know a footer link or a bottom setting exists until something scrolls.
- **form_contact:** the credential guard treats a field named "Email" as a sign-in field and blocks when the chosen
  text is not an address.
- **edit_sheet_cell, excel_cells:** spreadsheet cells are not offered as targets (grid cells, double-click to edit).
- **drag_kanban_card:** goal mode has no drag action. Steve's thread lists drag-and-drop as a Jev failure as well.
- **slider_brightness:** range sliders are not a captured role.
- **hover_products_menu:** the menu title is a focusable element without a control role, so it is not a target.
- **drive_new_folder, design_rename_layer:** multi-step menus and a double-click-to-rename that Jev does not find.

Each of these is a candidate for the next round. Off-screen awareness, drag and sliders are the general ones. They
should be developed against new tasks, not these, now that these holdout traces have been read.

## Regression check

The existing live suite (`goal_eval.py`) was rerun after the changes. Holdout: 7/8, matching the best earlier run
(14/16 over two runs). Dev: 9 of 10 tasks passed. The one failure, `gmail`, needs a signed-in Google account that
the throwaway profile does not have; it failed the same way in the last run before these changes. On the first
attempt all five VLC tasks failed, because the benchmark's own VLC still held the remote-control port. All five
passed once it was closed, and the benchmark now closes it itself. `goal_sim` (frozen screens, live Jev): the
five earlier cases completed 14/15 over three runs (saved_template 1/2, then 3/3 on a rerun); the flights
date-picker case added earlier today still fails, as it did before. Unit tests: 92 pass.

## How to run

```powershell
# drives mouse and keyboard; don't use the PC meanwhile
.\.venv-voice\Scripts\python.exe -m voice_control.bench_eval --label mylabel            # all 57
.\.venv-voice\Scripts\python.exe -m voice_control.bench_eval --lane simple --split dev
.\.venv-voice\Scripts\python.exe -m voice_control.bench_plot --label baseline --label mylabel
```

Results go to `logs/bench-eval.jsonl` (one record per task run, with every Jev request and response), the plot
to `logs/bench-plot.png`, and the aggregate to `logs/bench-summary.json`.
