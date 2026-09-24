"""Where the app keeps its key, settings, logs, and speech models.

A source checkout keeps everything beside the code, as before. The installed
app (no requirements-voice.txt next to the package) keeps it in
%LOCALAPPDATA%\\Jev Voice Control, where the installer also saves the API key.
JEV_VOICE_HOME overrides both.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _home() -> Path:
    if os.environ.get("JEV_VOICE_HOME"):
        return Path(os.environ["JEV_VOICE_HOME"])
    if (ROOT / "requirements-voice.txt").exists():
        return ROOT
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Jev Voice Control"


HOME = _home()
KEY_FILE = HOME / ".env.openrouter"
