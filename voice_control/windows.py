"""Windows UI Automation adapter. No app-specific selectors."""

from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
import win32api
import win32con
import win32gui
import win32process
import win32ui
from pywinauto import Desktop, keyboard, mouse

from . import sliders
from .core import CHORDS, FIELD_ROLES, SLIDER_ROLES, SURFACE_ID, Control
from .uia_watch import LOADING_NAME

ROLES = {"Button", "Hyperlink", "MenuItem", "ListItem", "TreeItem", "TabItem", "Edit", "CheckBox", "RadioButton", "ComboBox",
         "SplitButton", "Spinner", "Slider"}


def _captionless_popup(hwnd: int) -> bool:
    """Menus, dropdowns, overlays, and tool strips: Windows' own Alt+Tab list leaves these out."""
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    except win32gui.error:
        return True
    if ex_style & win32con.WS_EX_APPWINDOW:
        return False
    return bool(ex_style & win32con.WS_EX_TOOLWINDOW) or bool(style & win32con.WS_POPUP and (style & win32con.WS_CAPTION) != win32con.WS_CAPTION)


def open_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def collect(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd).strip() or _captionless_popup(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process = psutil.Process(pid).name()
        except psutil.Error:
            process = str(pid)
        windows.append({"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "process": process})

    win32gui.EnumWindows(collect, None)
    return windows


def _window_exe(hwnd: int) -> str:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        exe = psutil.Process(pid).exe()
    except psutil.Error:
        return ""
    if Path(exe).name.lower() == "applicationframehost.exe":
        # Store apps are hosted in a frame window; the real app owns a child window.
        children: list[int] = []
        win32gui.EnumChildWindows(hwnd, lambda child, _: children.append(child), None)
        for child in children:
            if win32process.GetWindowThreadProcessId(child)[1] != pid:
                try:
                    return psutil.Process(win32process.GetWindowThreadProcessId(child)[1]).exe()
                except psutil.Error:
                    break
    return exe


def _file_description(exe: str) -> str:
    try:
        lang, codepage = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")[0]
        return str(win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\FileDescription") or "").strip()
    except Exception:
        return ""


def _icon_handle(hwnd: int, exe: str) -> tuple[int, bool]:
    """(HICON, owned): the window's own icon, else its class icon, else the first icon in its exe."""
    generic = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)  # the blank placeholder says nothing about the app
    for kind in (win32con.ICON_BIG, 2, win32con.ICON_SMALL):  # 2 = ICON_SMALL2
        try:
            _, handle = win32gui.SendMessageTimeout(hwnd, win32con.WM_GETICON, kind, 0, win32con.SMTO_ABORTIFHUNG, 100)
        except Exception:
            handle = 0
        if handle and handle != generic:
            return handle, False
    for index in (-14, -34):  # GCL_HICON, GCL_HICONSM
        try:
            handle = win32gui.GetClassLong(hwnd, index)
        except Exception:
            handle = 0
        if handle and handle != generic:
            return handle, False
    if exe:
        try:
            large, small = win32gui.ExtractIconEx(exe, 0, 1)
            for extra in (large + small)[1:]:
                win32gui.DestroyIcon(extra)
            if large or small:
                return (large + small)[0], True
        except Exception:
            pass
    return 0, False


def window_app(hwnd: int, size: int, background: str) -> tuple[str, Any]:
    """Name and icon (a PIL image composited on `background`, or None) of the app that owns any window.

    Everything is read locally from the window and its executable, so it works for any app."""
    from PIL import Image

    exe = _window_exe(hwnd)
    title = win32gui.GetWindowText(hwnd).strip()
    if Path(exe).name.lower() == "applicationframehost.exe":  # a suspended Store app: its title is its name
        exe, name = "", title
    else:
        name = _file_description(exe) or Path(exe).stem or title
    hicon, owned = _icon_handle(hwnd, exe)
    if not hicon:
        return name, None
    screen = win32gui.GetDC(0)
    dc = win32ui.CreateDCFromHandle(screen)
    mem = dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(dc, size, size)
        mem.SelectObject(bitmap)
        r, g, b = (int(background[i:i + 2], 16) for i in (1, 3, 5))
        mem.FillSolidRect((0, 0, size, size), win32api.RGB(r, g, b))
        win32gui.DrawIconEx(mem.GetSafeHdc(), 0, 0, hicon, size, size, 0, None, win32con.DI_NORMAL)
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGBA", (size, size), bits, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        mem.DeleteDC()
        win32gui.ReleaseDC(0, screen)
        win32gui.DeleteObject(bitmap.GetHandle())
        if owned:
            win32gui.DestroyIcon(hicon)
    return name, image


NOT_APPS = re.compile(r"^(uninstall|remove)|\b(help|documentation|manual|readme|release notes|website|web site|"
                      r"samples?|reference|faq|localization|skin format)\b|\.(pdf|hlp|chm|txt|url)$", re.I)


def installed_apps() -> list[dict[str, str]]:
    """Generic Start-menu catalog: desktop shortcuts plus packaged (Store) apps such as Notepad or Calculator.

    Documents and links that happen to live in the Start menu are left out; names and paths stay local until a choice."""
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    by_name: dict[str, dict[str, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.lnk"):
            name = path.stem
            if not NOT_APPS.search(name):
                by_name.setdefault(name.casefold(), {"name": name, "path": str(path)})
    try:  # packaged apps have no .lnk; the shell's AppsFolder lists them by AppUserModelID ("Package!App")
        import win32com.client
        for item in win32com.client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder").Items():
            name, aumid = str(item.Name).strip(), str(item.Path)
            if "!" in aumid and name and not NOT_APPS.search(name):
                by_name.setdefault(name.casefold(), {"name": name, "aumid": aumid})
    except Exception:
        pass
    return sorted(by_name.values(), key=lambda app: app["name"].casefold())


def capture(hwnd: int | None = None, max_nodes: int = 3000) -> tuple[dict[str, Any], list[Control]]:
    hwnd = hwnd or win32gui.GetForegroundWindow()
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError("No foreground window is available")
    windows = open_windows()
    active = next((w for w in windows if w["hwnd"] == hwnd), None)
    if not active:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        active = {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "process": psutil.Process(pid).name()}
    controls: list[Control] = []
    texts: list[str] = []
    loading: list[str] = []
    covered: list[str] = []
    visited = 0
    truncated = False
    # An open context menu or dropdown is a separate top-level window. It is
    # what the user acts on next, so its items come first.
    popups = popup_windows(hwnd)
    for popup in popups:
        visited, cut = _collect(popup, "Open menu", controls, visited, max_nodes, texts=texts, loading=loading,
                                covered=covered)
        truncated |= cut
    visited, cut = _collect(hwnd, "", controls, visited, max_nodes, own=True, texts=texts, loading=loading,
                            covered=covered)
    truncated |= cut
    state = {
        "activeWindow": active,
        "openWindows": windows,
        "openMenus": len(popups),
        "texts": texts,
        "loading": loading,
        "covered": covered,  # controls left out because an in-page dialog lies on top of them
        "controlCount": len(controls),
        "visitedNodes": visited,
        "truncatedTraversal": truncated,
        "controls": [{"id": c.id, "role": c.role, "name": c.name, "rect": c.rect, "path": c.path, "enabled": c.enabled,
                      **({"hwnd": c.hwnd} if c.hwnd else {}), **({"state": c.state} if c.state else {}),
                      **({"detail": c.detail} if c.detail else {}), **({"value": c.value} if c.value else {}),
                      **({"context": c.context} if c.context else {})}
                     for c in controls],
    }
    return state, controls


def popup_windows(hwnd: int) -> list[int]:
    """Visible menus/dropdowns that belong to the window's process, topmost first."""
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return []
    found: list[int] = []
    shell = {ctypes.windll.user32.GetShellWindow()}  # the desktop; taskbars are excluded by class below
    below = []  # EnumWindows walks the z-order from the top; a menu for this window sits above it

    def collect(candidate: int, _: Any) -> None:
        if candidate == hwnd:
            below.append(True)
        if below or candidate in shell or not win32gui.IsWindowVisible(candidate):
            return
        if win32gui.GetClassName(candidate) in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return
        try:
            left, top, right, bottom = win32gui.GetWindowRect(candidate)
            same = win32process.GetWindowThreadProcessId(candidate)[1] == pid
            menu = win32gui.GetClassName(candidate) == "#32768"
        except Exception:
            return
        if right - left < 8 or bottom - top < 8 or right < -1000 or bottom < -1000:
            return  # zero-size helpers and parked off-screen windows
        if menu or (same and _captionless_popup(candidate)):
            found.append(candidate)  # tooltips and shadows qualify too but expose no actionable controls

    win32gui.EnumWindows(collect, None)  # z-order: topmost first
    return found[:4]


# Buttons too: a switch or a pressed/unpressed toggle only shows its effect through this state.
STATEFUL = {"MenuItem", "CheckBox", "RadioButton", "ListItem", "TreeItem", "TabItem", "ComboBox", "SplitButton", "Button"}


def _ui_state(item: Any, role: str) -> str:
    """Checked/selected/expanded state, when the control reports it. Many apps report none; then this is empty."""
    if role not in STATEFUL:
        return ""
    states = []
    try:
        toggle = item.iface_toggle.CurrentToggleState
        states.append({0: "unchecked", 1: "checked", 2: "partly checked"}.get(toggle, ""))
    except Exception:
        pass
    try:
        if item.iface_selection_item.CurrentIsSelected:
            states.append("selected")
    except Exception:
        pass
    try:
        expand = item.iface_expand_collapse.CurrentExpandCollapseState
        states.append({0: "has submenu" if role == "MenuItem" else "collapsed", 1: "expanded", 2: "partly expanded"}.get(expand, ""))
    except Exception:
        pass
    return ", ".join(s for s in states if s)


def _editable(item: Any) -> bool:
    try:
        return not item.iface_value.CurrentIsReadOnly
    except Exception:
        return False


def _field_value(item: Any, name: str) -> str:
    """Text already in a field, so a prefilled value is visible before typing over it. Password fields stay unread."""
    try:
        if item.element_info.element.CurrentIsPassword:
            return ""
        value = str(item.iface_value.CurrentValue or "")
    except Exception:
        return ""
    value = value.strip()[:200]
    return "" if value == name else value


TYPE_GAP = 0.01  # seconds between typed characters; 5 ms was already enough for Notepad


# Typing replaces a single-line field's text; taller fields are documents and message bodies, where it appends.
SINGLE_LINE_HEIGHT = 80


def replaces_text(control: Control) -> bool:
    height = control.rect[3] - control.rect[1]
    return control.role in {"ComboBox", "Spinner"} or (control.role == "Edit" and height <= SINGLE_LINE_HEIGHT
                                                         and "\n" not in control.value)


TEXT_ROLES = {"Text", "Header", "StatusBar", "TitleBar"}
MAX_TEXTS = 60
# Web pages nest controls deeply (a Google Drive file tile sits below depth 20); max_nodes still bounds the walk.
MAX_DEPTH = 45
MAX_DETAIL = 100


def _outside(bounds: tuple[int, int, int, int], area: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        return False  # zero-size containers still hold positioned children
    return right <= area[0] or left >= area[2] or bottom <= area[1] or top >= area[3]


def _focused(item: Any) -> bool:
    try:
        return bool(item.has_keyboard_focus())
    except Exception:
        return False


UIA_IS_DIALOG = 30174  # UIA_IsDialogPropertyId


def _is_dialog(item: Any) -> bool:
    try:
        return bool(item.element_info.element.GetCurrentPropertyValue(UIA_IS_DIALOG))
    except Exception:
        return False


def _covered(controls: list[Control], layers: list[tuple[int, ...]],
             dialogs: list[tuple[tuple[int, int, int, int], set[int]]]) -> set[int]:
    """The controls an in-page dialog lies on top of: a web date picker over the form's Search button.

    The page still lists the controls beneath (Chrome even hit-tests to them), and Invoke still reaches them, so Jev
    kept pressing a Search button it could not see. A control is covered when its centre is inside a dialog it is not
    part of, and that dialog came later in the tree than the control's own dialog, if any: portals append a dialog
    opened from another dialog, so the later one is on top. `dialogs` pairs each dialog's bounds with the controls
    it lies inside (a combo box whose dropdown is a dialog), which it does not cover. An open dropdown's options
    are exempt too, since a list opened from inside a dialog is often portaled outside it while lying on top of it."""
    if not dialogs:
        return set()
    dropdown = any(c.role == "ComboBox" and "expanded" in c.state for c in controls)
    covered = set()
    for index, control in enumerate(controls):
        if dropdown and control.role in {"ListItem", "MenuItem"}:
            continue
        x, y = (control.rect[0] + control.rect[2]) / 2, (control.rect[1] + control.rect[3]) / 2
        own = max(layers[index], default=-1)
        if any(d > own and d not in layers[index] and index not in inside and left <= x < right and top <= y < bottom
               for d, ((left, top, right, bottom), inside) in enumerate(dialogs)):
            covered.add(index)
    return covered


def _collect(hwnd: int, label: str, controls: list[Control], visited: int, max_nodes: int, own: bool = False,
             texts: list[str] | None = None, loading: list[str] | None = None,
             covered: list[str] | None = None) -> tuple[int, bool]:
    """Append the named interactive controls under one top-level window, in UIA pre-order.

    Short read-only text (a result display, a status line, a heading) goes to `texts`: it is
    not actionable, but it often shows whether the goal has been reached. Text inside a
    control that its accessible name leaves out (a card's title under a link named only by
    its tags) becomes that control's detail. Subtrees lying wholly outside the window (the
    scrolled-away part of a long page) are skipped, which keeps long pages fast. Controls
    hidden under an in-page dialog are left out; `covered` gets their descriptions."""
    try:
        root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        frame = root.rectangle()
        area = (int(frame.left), int(frame.top), int(frame.right), int(frame.bottom))
    except Exception:
        return visited, False
    first = len(controls)
    details: dict[int, list[str]] = {}
    near: dict[int, str] = {}  # the last text read before each control within its own neighbourhood
    NEIGHBOURHOOD = 3  # levels of shared ancestry within which a text still labels a control
    # UIA tree pre-order is a useful (though not infallible) reading order for
    # phrases such as "the first link". Breadth-first order is not.
    dialogs: list[tuple[tuple[int, int, int, int], set[int]]] = []  # in-page dialogs in tree order, see _covered
    layers: list[tuple[int, ...]] = []  # per control from `first`: the dialogs it sits inside
    parents: list[int | None] = []  # per control from `first`: the control it sits inside
    queue = [(root, label, 0, None, (), ())]
    seen: set[tuple] = set()
    while queue and visited < max_nodes:
        item, parent_path, depth, owner, scopes, layer = queue.pop()
        visited += 1
        try:
            info = item.element_info
            # Chrome's tree can loop back to the window while a native dropdown is open; walked blindly, the same
            # page repeated until the node cap (949 controls for a 37-control page). Each element is walked once.
            runtime = tuple(getattr(info, "runtime_id", None) or ())
            if runtime:
                if runtime in seen:
                    continue
                seen.add(runtime)
            name = (info.name or "").strip()
            role = info.control_type or ""
            rect = item.rectangle()
            bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            visible = item.is_visible() and rect.right > rect.left and rect.bottom > rect.top
            enabled = item.is_enabled()
        except Exception:
            continue
        if depth and _outside(bounds, area):
            continue
        # A web page's role=dialog (a date picker, a modal) is a Window inside the page's tree.
        if depth and role == "Window" and visible and _is_dialog(item):
            inside, parent = set(), owner if owner is not None and owner >= first else None
            while parent is not None:
                inside.add(parent - first)
                parent = parents[parent - first]
            dialogs.append(((max(bounds[0], area[0]), max(bounds[1], area[1]), min(bounds[2], area[2]),
                             min(bounds[3], area[3])), inside))
            layer = layer + (len(dialogs) - 1,)
        path = (parent_path + " > " + name).strip(" >")[-180:] if name else parent_path
        if visible and role in TEXT_ROLES and name and len(name) <= MAX_DETAIL:
            for scope in scopes[-NEIGHBOURHOOD:]:
                scope["text"] = name
        if texts is not None and visible and role in TEXT_ROLES and name and len(texts) < MAX_TEXTS \
                and len(name) <= 120 and name not in texts:
            texts.append(name)
        # A progress bar or spinner, or anything named like "Loading…": the window is still filling in.
        if loading is not None and visible and (role == "ProgressBar" or (name and len(name) <= 80 and LOADING_NAME.search(name))):
            loading.append(f'{role} "{name}"')
        if owner is not None and visible and role in TEXT_ROLES and name and len(name) <= MAX_DETAIL:
            parts = details.setdefault(owner, [])
            if name.casefold() not in controls[owner].name.casefold() and name not in parts \
                    and sum(len(x) for x in parts) + len(name) <= MAX_DETAIL:
                parts.append(name)
        # An editor's text area (Notepad, a mail body) is a Document that accepts typing; a web page's own
        # Document is read-only and stays out.
        editor = role == "Document" and visible and _editable(item)
        # A Windows combo box exposes its drop-down arrow as a Button named "Open" (or "Close"); next to a
        # dialog's own Open button it is a trap, and the combo box itself already opens the list.
        arrow = role == "Button" and name in {"Open", "Close"} and owner is not None and controls[owner].role == "ComboBox"
        # A slider is useful even unnamed (a player's volume): its value, range and place tell what it is.
        if visible and (role in ROLES or editor) and (name or role in FIELD_ROLES or role in SLIDER_ROLES) and not arrow:
            state = _ui_state(item, role)
            if role in FIELD_ROLES and _focused(item):
                state = ", ".join(x for x in (state, "focused") if x)
            value = _field_value(item, name) if role in FIELD_ROLES else ""
            detail = ""
            if role in SLIDER_ROLES:
                value = sliders.read(item).label()
                detail = "" if name else sliders.unnamed_label(bounds, area)
            controls.append(Control(f"c{len(controls):04d}", name, role, bounds, parent_path[-120:], enabled, 0 if own else hwnd,
                                    state, detail=detail, value=value))
            layers.append(layer)
            parents.append(owner if owner is not None and owner >= first else None)
            owner = len(controls) - 1
            near[owner] = next((scope["text"] for scope in reversed(scopes[-NEIGHBOURHOOD:]) if scope.get("text")), "")
        if depth < MAX_DEPTH:
            try:
                inner = scopes + ({},)
                queue.extend((child, path, depth + 1, owner, inner, layer) for child in reversed(item.children()))
            except Exception:
                pass
    for index, parts in details.items():
        if index >= first and parts:
            controls[index] = replace(controls[index], detail=" | ".join(parts))
    # Four "Rename" buttons, one per file row: the text read just before each one (the file name, a card's
    # heading) is what a sighted person uses to tell them apart. Only repeated names get it.
    repeated: dict[tuple[str, str], list[int]] = {}
    for index in range(first, len(controls)):
        repeated.setdefault((controls[index].role, controls[index].name), []).append(index)
    for indexes in repeated.values():
        if len(indexes) > 1 and len({near.get(i, "") for i in indexes}) > 1:
            for i in indexes:
                if near.get(i) and near[i].casefold() not in controls[i].name.casefold():
                    controls[i] = replace(controls[i], context=near[i])
    hidden = _covered(controls[first:], layers, dialogs)
    if hidden:
        if covered is not None:
            covered.extend(f'{c.role} "{c.name}"' for i, c in enumerate(controls[first:]) if i in hidden)
        controls[first:] = [c for i, c in enumerate(controls[first:]) if i not in hidden]
    return visited, bool(queue)


def _find_fresh_wrapper(hwnd: int, chosen: Control) -> Any:
    """The live element for a captured control: same name and role, at (or near) the same place.

    UIA can report one element more than once, so matches with identical bounds count as one. A control
    that moved (a list re-sorted after typing in its search box) is still accepted when it is the only
    visible control with that name and role in the window."""
    root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    places: dict[tuple, Any] = {}
    in_place: list[Any] = []  # same role at exactly the captured bounds, whatever its name now
    for item in root.descendants():
        try:
            rect = item.rectangle()
            bounds = (rect.left, rect.top, rect.right, rect.bottom)
            if item.element_info.control_type != chosen.role or not item.is_visible() or not item.is_enabled():
                continue
            if (item.element_info.name or "").strip() == chosen.name:
                places.setdefault(bounds, item)
            elif bounds == tuple(chosen.rect):
                in_place.append(item)
        except Exception:
            continue
    if chosen.rect in places:
        return places[chosen.rect]
    # Windows can finish laying out a just-launched app between capture and execution.
    nearby = [bounds for bounds in places if all(abs(a - b) <= 12 for a, b in zip(bounds, chosen.rect))]
    if len(nearby) == 1:
        return places[nearby[0]]
    if not nearby and len(places) == 1:
        return next(iter(places.values()))
    if not places and len(in_place) == 1:
        # Its label changed in place since the capture (a date button whose price finished loading).
        return in_place[0]
    raise RuntimeError(f"Target changed or became ambiguous before execution ({len(nearby) or len(places)} fresh matches)")


def _click_surface(hwnd: int, verb: str) -> str:
    """Click the middle of a window's client area, for content that exposes no controls (a video, a canvas)."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, ((left + right) // 2, (top + bottom) // 2))
    _ensure_uncovered(hwnd, (x, y, x, y))
    if verb == "right_click":
        mouse.right_click(coords=(x, y))
    elif verb == "double_click":
        mouse.double_click(coords=(x, y))
    else:
        mouse.click(coords=(x, y))
    return f"{verb} on the window's content area"


def _ensure_uncovered(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    """Refuse a coordinate click whose point would land on some other app's window (e.g. an always-on-top one)."""
    x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    hit = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2)  # GA_ROOT
    if hit == win32gui.GetAncestor(hwnd, 2):
        return
    if win32process.GetWindowThreadProcessId(hit)[1] == win32process.GetWindowThreadProcessId(hwnd)[1]:
        return  # the app's own popup or child window
    raise RuntimeError(f'Target is covered by "{win32gui.GetWindowText(hit)}"; click cancelled')


def _wait_for_foreground(hwnd: int, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            return
        time.sleep(0.025)
    raise RuntimeError("Target window did not become foreground; action cancelled")


def _bring_to_front(hwnd: int) -> None:
    # Windows refuses SetForegroundWindow from a background process (this app
    # never has focus). A synthetic Alt press makes us the last-input process,
    # which lifts that lock; AttachThreadInput alone was not enough here.
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass
    if win32gui.GetForegroundWindow() == hwnd:
        return
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass
    finally:
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def execute(verb: str, target: dict[str, Any], state: dict[str, Any], controls: list[Control], apps: list[dict[str, str]], utterance: str,
            app_hwnd: int | None = None, text: str | None = None) -> str:
    """Run one planned action. `text` is what type_text enters; without it the words after 'type' are dictated."""
    hwnd = state["activeWindow"]["hwnd"]
    if app_hwnd and win32gui.GetForegroundWindow() == app_hwnd and win32gui.IsWindow(hwnd):
        _bring_to_front(hwnd)
    if verb not in {"switch_window", "launch_app", "alt_tab"}:
        _wait_for_foreground(hwnd)
    if verb in {"left_click", "right_click", "double_click"} and target.get("id") == SURFACE_ID:
        return _click_surface(hwnd, verb)
    if verb in {"left_click", "right_click", "double_click", "hover", "type_text", "select_text", "set_slider"}:
        chosen = next((c for c in controls if c.id == target["id"]), None)
        if chosen is None:
            raise RuntimeError("Selected target is not in captured controls")
        wrapper = _find_fresh_wrapper(chosen.hwnd or hwnd, chosen)
        if verb != "left_click":  # left clicks try UIA Invoke first and check before falling back to the mouse
            _ensure_uncovered(chosen.hwnd or hwnd, chosen.rect)
        if verb == "set_slider":
            change = target.get("change")
            if chosen.role not in SLIDER_ROLES or not sliders.valid_change(change):
                raise RuntimeError("Nothing to move: no slider or no amount")
            name = f'"{chosen.name}"' if chosen.name else "(unnamed)"
            return f"moved Slider {name} {sliders.describe_amount(change)}: " + sliders.move(wrapper, chosen.rect, change)
        if verb == "hover":
            left, top, right, bottom = chosen.rect
            mouse.move(coords=((left + right) // 2, (top + bottom) // 2))
            return f'hovered {chosen.role} "{chosen.name}"'
        if verb == "select_text":
            if not text or chosen.role not in FIELD_ROLES:
                raise RuntimeError("Nothing to select")
            wrapper.click_input()  # the field takes focus, so a following Bold or Copy applies to it
            found = wrapper.iface_text.DocumentRange.FindText(text, False, True)
            if not found:
                raise RuntimeError(f'"{text}" is not in {chosen.name or "the field"}')
            found.Select()
            return f'selected "{text}" in {chosen.name or "the field"}'
        if verb == "type_text":
            from .core import dictation_text
            text = text or dictation_text(utterance)
            if not text or chosen.role not in FIELD_ROLES:
                raise RuntimeError("Say 'type <text>' while an editable field is available")
            wrapper.click_input()
            replaced = replaces_text(chosen) and chosen.value
            if replaces_text(chosen):
                keyboard.send_keys("^a")  # a prefilled "Ahmedabad" would otherwise become "AhmedabadLos Angeles"
            from pynput.keyboard import Controller
            typist = Controller()
            for character in text:  # a burst loses keys in some editors (Notepad dropped "PM" from "4 PM")
                typist.type(character)
                time.sleep(TYPE_GAP)
            where = chosen.name or "edit field"
            if replaced:
                return f'replaced "{chosen.value}" with {len(text)} characters in {where}'
            return f'typed {len(text)} characters into {where}'
        if verb == "left_click":
            try:
                if chosen.role == "MenuItem":
                    raise RuntimeError("menus are clicked like a person would, so they close and apply normally")
                if chosen.role == "ListItem" and any(c.role == "ComboBox" and "expanded" in c.state for c in controls):
                    # Invoke selects a dropdown option but leaves the list open; the next click then lands on it.
                    raise RuntimeError("dropdown options are clicked like a person would, so the list closes")
                if target.get("physical"):
                    raise RuntimeError("the same click changed nothing last time, so this time it is a real click")
                wrapper.invoke()
            except Exception:
                _ensure_uncovered(chosen.hwnd or hwnd, chosen.rect)
                wrapper.click_input(button="left")
        elif verb == "right_click":
            wrapper.click_input(button="right")
        else:
            wrapper.click_input(button="left", double=True)
        return f'{verb} {chosen.role} "{chosen.name}"'
    if verb in {"scroll_up", "scroll_down", "scroll_left", "scroll_right"}:
        bounds = win32gui.GetWindowRect(hwnd)
        center = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
        from pynput.mouse import Controller
        controller = Controller()
        previous = controller.position
        try:
            if verb in {"scroll_up", "scroll_down"}:
                mouse.scroll(coords=center, wheel_dist=3 if verb == "scroll_up" else -3)
            else:
                controller.position = center
                controller.scroll(-3 if verb == "scroll_left" else 3, 0)
        finally:
            controller.position = previous
        return f"{verb} 3 wheel notches"
    if verb in {"zoom_in", "zoom_out", "zoom_reset"}:
        keys = {"zoom_in": "^{ADD}", "zoom_out": "^{SUBTRACT}", "zoom_reset": "^0"}
        keyboard.send_keys(keys[verb])
        return f"{verb} via Ctrl shortcut (app-dependent)"
    if verb == "press_key":
        keys = {"Enter": "{ENTER}", "Escape": "{ESC}", "Tab": "{TAB}", "Shift+Tab": "+{TAB}", "Backspace": "{BACKSPACE}", "Delete": "{DELETE}", "Home": "{HOME}", "End": "{END}", "PageUp": "{PGUP}", "PageDown": "{PGDN}", "Space": "{SPACE}"}
        key = target.get("key")
        if key not in keys:
            raise RuntimeError("Key is outside the allowed catalog")
        keyboard.send_keys(keys[key])
        return f"pressed {key}"
    if verb == "key_chord":
        name = target.get("key")
        if name not in CHORDS:
            raise RuntimeError("Shortcut is outside the allowed catalog")
        keyboard.send_keys(CHORDS[name][1])
        return f"sent {CHORDS[name][0]} ({name})"
    if verb in {"minimize_window", "maximize_window", "close_window"}:
        which = "current window"
        if target["id"] != "current":
            hwnd = int(target["id"][1:])
            if hwnd not in {w["hwnd"] for w in open_windows()}:
                raise RuntimeError("Target window is no longer open")
            which = f'window "{win32gui.GetWindowText(hwnd).strip()}"'
        if verb == "close_window":
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return f"requested {which} close"
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE if verb == "minimize_window" else win32con.SW_MAXIMIZE)
        return f'{"minimized" if verb == "minimize_window" else "maximized"} {which}'
    if verb == "switch_window":
        destination = int(target["id"][1:])
        if destination not in {w["hwnd"] for w in open_windows()}:
            raise RuntimeError("Target window is no longer open")
        _bring_to_front(destination)
        _wait_for_foreground(destination)
        return f"activated window {destination}"
    if verb == "wait":
        time.sleep(1.0)
        return "waited 1 s for the app to respond"
    if verb == "alt_tab":
        keyboard.send_keys("%{TAB}")
        return "sent Alt+Tab (destination not guaranteed)"
    if verb == "launch_app":
        index = int(target["id"][1:])
        if not 0 <= index < len(apps):
            raise RuntimeError("Installed app choice is invalid")
        if apps[index].get("aumid"):
            os.startfile("shell:AppsFolder\\" + apps[index]["aumid"])  # packaged apps start through the shell
            return f'launched "{apps[index]["name"]}"'
        shortcut = Path(apps[index]["path"])
        if not shortcut.is_file() or shortcut.suffix.lower() != ".lnk":
            raise RuntimeError("Installed app shortcut changed")
        os.startfile(shortcut)
        return f'launched "{apps[index]["name"]}"'
    raise RuntimeError(f"No executor for {verb}")


def app_window(hwnd: int) -> int:
    """The window a person is working in: the top-level window of the focus, or, when a menu or dropdown popup
    has focus, the app window that owns it. A dialog (it has a caption) is its own window."""
    if not hwnd:
        return 0
    root = win32gui.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    if _captionless_popup(root):
        return win32gui.GetAncestor(root, 3) or root  # GA_ROOTOWNER
    return root


def _top_level_picture() -> tuple:
    """Cheap snapshot of what is on screen at the window level: the foreground and every visible window's title and bounds."""
    shown: list[tuple] = []

    def collect(hwnd: int, _: Any) -> None:
        if win32gui.IsWindowVisible(hwnd):
            try:
                rect = win32gui.GetWindowRect(hwnd)
                if rect[2] - rect[0] > 1 and rect[3] - rect[1] > 1:
                    shown.append((hwnd, win32gui.GetWindowText(hwnd), rect))
            except win32gui.error:
                pass

    win32gui.EnumWindows(collect, None)
    foreground = win32gui.GetForegroundWindow()
    return foreground, win32gui.GetWindowText(foreground), tuple(shown)


def settle(verb: str, before: tuple, timeout_s: float = 3.0) -> int:
    """Wait for the UI to respond to an action, then to stop changing. Returns the milliseconds waited.

    Launching or switching apps waits (up to 10 s) for a different window to come to the front first."""
    started = time.monotonic()
    if verb in {"launch_app", "switch_window", "alt_tab"}:
        while time.monotonic() - started < 10 and _top_level_picture()[:2] == before[:2]:
            time.sleep(0.1)
    time.sleep(0.3)
    previous = _top_level_picture()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.15)
        current = _top_level_picture()
        if current == previous:
            break
        previous = current
    return round((time.monotonic() - started) * 1000)


# Loading indicators each window showed at its last reading. One that stays (a download bar, a player's buffer
# bar) is part of the window, so it holds up only the first reading that shows it.
_LOADING_SEEN: dict[int, set[str]] = {}


def capture_settled(hwnd: int, sparse: int = 12, timeout_s: float = 4.0) -> tuple[dict[str, Any], list[Control]]:
    """capture(), but a window that is still filling in is read again until it is done or `timeout_s` passes: one
    showing only a few controls (a just-launched app often shows only its title bar for a second or more), or one
    showing a loading indicator it did not show at its previous reading ("Loading…", a progress bar)."""
    state, controls = capture(hwnd)
    known = _LOADING_SEEN.get(hwnd, set())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        appeared = [x for x in state.get("loading", []) if x not in known]
        if len(controls) >= sparse and not appeared:
            break
        time.sleep(0.3 if appeared else 0.4)
        again, more = capture(hwnd)
        if appeared or len(more) >= len(controls):
            state, controls = again, more
    _LOADING_SEEN[hwnd] = set(state.get("loading", []))
    return state, controls
