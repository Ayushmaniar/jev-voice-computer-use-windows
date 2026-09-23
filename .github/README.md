# Jev Voice Computer Use for Windows

A push-to-talk Windows app: hold Right Ctrl, say what you want, and Jev reads the
foreground window through UI Automation and performs the action.

See [voice_control/README.md](../voice_control/README.md) for setup, usage, and design notes.

Quick start (PowerShell, from the repository root):

```powershell
python -m venv .venv-voice
.\.venv-voice\Scripts\python.exe -m pip install -r requirements-voice.txt
.\.venv-voice\Scripts\python.exe -m voice_control.app
```

Put `OPENROUTER_API_KEY=...` in `.env.openrouter`, or a TypeSafe AI key as `TYPESAFE_API_KEY=...` in `.env.typesafe`, at the repository root (both are git-ignored).
