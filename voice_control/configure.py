"""Set up Jev Voice from a terminal: find an API key you already have, save it, and check the install.

Meant for people and coding agents alike; every command is non-interactive.

    python -m voice_control.configure status [--json]    ready to use? exit code 0 = yes, 2 = no key
    python -m voice_control.configure find-keys [--json] keys already on this PC (.env files, environment)
    python -m voice_control.configure set-key --found 1  save the first key find-keys listed
    python -m voice_control.configure set-key --from-file PATH | --stdin | KEY
    python -m voice_control.configure test-key           ask OpenRouter whether the saved key works

The installed app has a wrapper that runs this with its bundled Python:
    & "$env:LOCALAPPDATA\\Programs\\Jev Voice Control\\configure.cmd" status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .core import KEY_NAMES, read_key, save_key
from .paths import HOME, KEY_FILE, ROOT

ENV_FILE_NAMES = (".env", ".env.local", ".env.openrouter", ".env.typesafe")
# Folders where people keep projects; each is searched along with its immediate subfolders (one project deep).
PROJECT_FOLDERS = ("", "Desktop", "Documents", "Documents/GitHub", "source/repos", "projects", "code", "dev", "repos",
                   "src", "git", "workspace")
MAX_SUBFOLDERS = 400


def mask(key: str) -> str:
    return key[:6] + "..." + key[-4:] if len(key) > 12 else "..." + key[-2:]


def provider(key: str) -> str:
    return "OpenRouter" if key.startswith("sk-or-") else "TypeSafe AI"


def keys_in_file(path: Path) -> list[str]:
    """OPENROUTER_API_KEY / TYPESAFE_API_KEY values, plus any value that is an OpenRouter key (sk-or-...)."""
    try:
        if path.stat().st_size > 256_000:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.startswith("#"):
            continue
        value = value.strip()
        if value and value[0] in "\"'":
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        if value and " " not in value and (name.strip() in KEY_NAMES or value.startswith("sk-or-")):
            found.append(value)
    return found


def _documents() -> Path:
    try:  # follows a Documents folder that OneDrive or the user moved
        from win32com.shell import shell, shellcon
        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PERSONAL, None, 0))
    except Exception:
        return Path.home() / "Documents"


def search_folders() -> list[Path]:
    home, documents = Path.home(), _documents()
    roots = [Path.cwd(), ROOT, ROOT.parent]
    roots += [documents / name[len("Documents/"):] if name.startswith("Documents") else home / name
              for name in PROJECT_FOLDERS]
    folders: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        folders.append(root)
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("$", "node_modules")))
        except OSError:
            continue
        folders += children[:MAX_SUBFOLDERS]
    unique: dict[str, Path] = {}
    for folder in folders:
        unique.setdefault(os.path.normcase(str(folder.resolve())), folder)
    return list(unique.values())


def find_keys() -> list[dict[str, str]]:
    """Keys already on this PC, best first: the saved one, environment variables, then .env files (newest first)."""
    found: list[dict[str, str]] = []

    def add(key: str, source: str) -> None:
        if key and all(f["key"] != key for f in found):
            found.append({"key": key, "provider": provider(key), "source": source})

    for file in (KEY_FILE, KEY_FILE.with_name(".env.typesafe")):
        for key in keys_in_file(file):
            add(key, f"saved: {file}")
    for name in KEY_NAMES:
        add(os.environ.get(name, "").strip(), f"environment variable {name}")
    files = [folder / name for folder in search_folders() for name in ENV_FILE_NAMES]
    existing = [f for f in files if f.is_file()]
    for file in sorted(existing, key=lambda f: f.stat().st_mtime, reverse=True):
        for key in keys_in_file(file):
            add(key, str(file))
    return found


def check_key(key: str, timeout: float = 10) -> tuple[bool | None, str]:
    """(works?, detail). OpenRouter keys are checked against its key-info endpoint, which costs nothing."""
    if not key.startswith("sk-or-"):
        return None, "TypeSafe AI keys can't be checked without running a command; try one in the app"
    import requests
    try:
        response = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except requests.RequestException as error:
        return None, f"couldn't reach OpenRouter: {type(error).__name__}"
    if response.status_code == 200:
        return True, "OpenRouter accepted the key"
    if response.status_code in (401, 403):
        return False, "OpenRouter rejected the key"
    return None, f"OpenRouter answered HTTP {response.status_code}"


def status() -> dict[str, Any]:
    try:
        key = read_key(KEY_FILE)
    except RuntimeError:
        key = ""
    runtime = ROOT / "runtime" / "python.exe"
    models = HOME / ".voice-model-cache"
    try:
        import nvidia  # noqa: F401  (the optional GPU libraries)
        gpu = True
    except ImportError:
        gpu = False
    try:
        import win32event
        handle = win32event.OpenMutex(0x00100000, False, "Local\\JevVoiceControl")  # SYNCHRONIZE
        running = bool(handle)
    except Exception:
        running = False
    return {
        "ready": bool(key),
        "key": {"set": bool(key), "provider": provider(key) if key else None, "masked": mask(key) if key else None},
        "data_dir": str(HOME),
        "app_dir": str(ROOT),
        "installed_app": runtime.exists(),
        "launcher": str(ROOT / "JevVoice.exe") if (ROOT / "JevVoice.exe").exists() else None,
        "gpu_libraries": gpu,
        "speech_models": sorted(p.name.split("--")[-1] for p in models.glob("models--*")) if models.is_dir() else [],
        "running": running,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m voice_control.configure", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command")
    for name in ("status", "find-keys"):
        commands.add_parser(name).add_argument("--json", action="store_true", help="machine-readable output")
    set_key = commands.add_parser("set-key", help="save an OpenRouter or TypeSafe AI key")
    source = set_key.add_mutually_exclusive_group(required=True)
    source.add_argument("key", nargs="?", help="the key itself (visible in shell history; prefer the other options)")
    source.add_argument("--found", type=int, metavar="N", help="the Nth key listed by find-keys")
    source.add_argument("--from-file", type=Path, metavar="PATH", help="a .env file containing the key")
    source.add_argument("--stdin", action="store_true", help="read the key from standard input")
    set_key.add_argument("--check", action="store_true", help="also check the key with OpenRouter")
    commands.add_parser("test-key", help="check the saved key with OpenRouter")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # a console code page must not crash on a path it can't show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    command = args.command or "status"

    if command == "status":
        info = status()
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2))
        else:
            key = info["key"]
            print(f"API key:        {key['provider'] + ' ' + key['masked'] if key['set'] else 'missing (run: set-key --found 1)'}")
            print(f"Data folder:    {info['data_dir']}")
            print(f"App folder:     {info['app_dir']}{' (installed)' if info['installed_app'] else ' (source checkout)'}")
            print(f"GPU libraries:  {'installed' if info['gpu_libraries'] else 'not installed (speech runs on the CPU)'}")
            print(f"Speech models:  {', '.join(info['speech_models']) or 'none yet (downloaded on first start)'}")
            print(f"Running:        {'yes' if info['running'] else 'no'}")
        return 0 if info["ready"] else 2

    if command == "find-keys":
        found = find_keys()
        if getattr(args, "json", False):
            print(json.dumps([{"index": i, "provider": f["provider"], "masked": mask(f["key"]), "source": f["source"]}
                              for i, f in enumerate(found, 1)], indent=2))
        elif not found:
            print("No OpenRouter or TypeSafe AI keys found in .env files or the environment.")
        else:
            for i, f in enumerate(found, 1):
                print(f"{i}. {f['provider']} key {mask(f['key'])}  ({f['source']})")
        return 0 if found else 1

    if command == "set-key":
        if args.found is not None:
            found = find_keys()
            if not 1 <= args.found <= len(found):
                print(f"There is no key {args.found}; find-keys found {len(found)}.", file=sys.stderr)
                return 1
            key = found[args.found - 1]["key"]
        elif args.from_file is not None:
            keys = keys_in_file(args.from_file)
            if not keys:
                print(f"No OPENROUTER_API_KEY, TYPESAFE_API_KEY or sk-or- key in {args.from_file}.", file=sys.stderr)
                return 1
            key = keys[0]
        else:
            key = (sys.stdin.readline() if args.stdin else args.key or "").strip()
        if not key or any(ch.isspace() for ch in key):
            print("That isn't an API key (it's empty or contains spaces).", file=sys.stderr)
            return 1
        path = save_key(KEY_FILE, key)
        print(f"Saved {provider(key)} key {mask(key)} to {path}")
        if read_key(KEY_FILE) != key:
            print("Note: an OPENROUTER_API_KEY or TYPESAFE_API_KEY environment variable overrides the saved key.")
        if args.check:
            ok, detail = check_key(key)
            print(detail)
            return 3 if ok is False else 0
        return 0

    if command == "test-key":
        try:
            key = read_key(KEY_FILE)
        except RuntimeError:
            print("No key saved yet.", file=sys.stderr)
            return 2
        ok, detail = check_key(key)
        print(f"{provider(key)} key {mask(key)}: {detail}")
        return {True: 0, False: 3, None: 4}[ok]
    return 0


if __name__ == "__main__":
    sys.exit(main())
