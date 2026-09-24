# Installing Jev Voice Control (for coding agents)

Jev Voice Control is a Windows push-to-talk app: the user holds Right Ctrl, speaks, and it acts in the
foreground window. Everything below is non-interactive and needs no admin rights. Run commands in
PowerShell from the repository root.

## Install

```powershell
.\installer\install.ps1
```

This builds `installer\dist\JevVoiceSetup-<version>.exe` if it isn't there yet (a few minutes; needs
internet and Inno Setup: `winget install JRSoftware.InnoSetup`), installs it silently for the current
user, prints the install status, and starts the app. Exit code 0 means ready; 2 means installed but
there is no API key yet; anything else is a failure, and the script prints the Setup log path.

Options: `-KeyFile <path>` (a `.env` file with the key), `-NoGpu` (skip the ~1.3 GB NVIDIA speech
download, which is otherwise included when the PC has an NVIDIA driver and can take several minutes, so
run the command in the background or with a long timeout), `-NoStartup`, `-NoDesktop`, `-NoLaunch`,
and `-Setup <path>` to use an already-built Setup exe.

## API key

Jev needs an OpenRouter key (`sk-or-...`) or a TypeSafe AI key. The user may already have one: Setup
keeps a key saved earlier, otherwise it uses the first key it finds in `OPENROUTER_API_KEY` /
`TYPESAFE_API_KEY` environment variables or in `.env`, `.env.local`, `.env.openrouter` and
`.env.typesafe` files in the usual project folders (home, Desktop, Documents, `source\repos`,
`projects`, `code`, `dev`, `repos`, ... and one level of subfolders). Any `sk-or-` value counts, even
under another variable name.

After installing, check or change it with the installed `configure.cmd`:

```powershell
$jev = "$env:LOCALAPPDATA\Programs\Jev Voice Control\configure.cmd"
& $jev find-keys                    # keys found on this PC, masked, with where each came from
& $jev set-key --found 2 --check    # save the 2nd one and verify it with OpenRouter
& $jev set-key --from-file C:\path\to\.env
& $jev status --json                # key, folders, GPU libraries, speech models, running (exit 0 = ready)
& $jev test-key                     # 0 = accepted, 3 = rejected, 4 = couldn't check
```

Show the user which file a key came from before saving one they didn't point you to. Never print a
full key; the tool only prints masked keys. Avoid `set-key <KEY>` on the command line when a file or
`--stdin` will do.

## Where things are

- App: `%LOCALAPPDATA%\Programs\Jev Voice Control` (bundled Python in `runtime\`, NVIDIA libraries in `gpu\`)
- Key, settings, logs, speech models: `%LOCALAPPDATA%\Jev Voice Control`
- Start the app: `& "$env:LOCALAPPDATA\Programs\Jev Voice Control\JevVoice.exe"` (only one copy runs)
- Uninstall, keeping the key and models: `& "$env:LOCALAPPDATA\Programs\Jev Voice Control\unins000.exe" /VERYSILENT`

On first start the app downloads its speech model (about 150 MB on CPU, 1.6 GB with the GPU
libraries). It is ready when the pill's dot at the bottom of the screen turns green.

## Working on the code instead

For development, run from source (the key then lives in the repository root, as before):

```powershell
python -m venv .venv-voice
.\.venv-voice\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv-voice\Scripts\python.exe -m voice_control.configure set-key --found 1
.\.venv-voice\Scripts\python.exe -m voice_control.app
```

See `voice_control/README.md` for how the app works and how to run its tests.
