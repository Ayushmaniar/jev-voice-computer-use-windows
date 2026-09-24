# Jev Voice Computer Use for Windows

A push-to-talk Windows app: hold Right Ctrl, say what you want, and Jev reads the
foreground window through UI Automation and performs the action.

## Install

**With the installer.** Run `JevVoiceSetup-<version>.exe`. It needs no admin rights and no Python.
Setup looks for an OpenRouter or TypeSafe AI key you already have (environment variables and `.env`
files in your project folders) and fills it in. You can also paste one, or skip this step and add it
later from the app (right-click the pill, then **API key**). On a PC with an NVIDIA graphics card,
Setup can also download the GPU speech libraries (about 1.3 GB) for faster, more accurate
recognition.

To build the installer yourself (needs Inno Setup: `winget install JRSoftware.InnoSetup`):

```powershell
.\installer\build.ps1        # writes installer\dist\JevVoiceSetup-1.0.0.exe
```

**With a coding agent.** Ask it to install Jev Voice; [AGENTS.md](../AGENTS.md) has everything it
needs. In short, the agent runs:

```powershell
.\installer\install.ps1
```

This builds Setup if needed, installs silently, picks up a key already on the PC, and prints the status.
The installed `configure.cmd` lists the keys it found, saves or checks a key, and reports whether
everything is ready.

**From source**, for development:

```powershell
python -m venv .venv-voice
.\.venv-voice\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv-voice\Scripts\python.exe -m voice_control.app
```

The app asks for a key on first start, offering any it finds. Alternatively put `OPENROUTER_API_KEY=...` in
`.env.openrouter`, or a TypeSafe AI key as `TYPESAFE_API_KEY=...` in `.env.typesafe`, at the
repository root (both are git-ignored).

See [voice_control/README.md](../voice_control/README.md) for usage and design notes.
