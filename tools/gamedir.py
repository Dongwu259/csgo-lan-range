"""Locate the CS:GO Legacy game directory instead of baking one author's path in.

Resolution order, first hit wins:
  1. CSGO_LEGACY_DIR environment variable
  2. settings.ini next to the tool ([game] dir = ...)
  3. Steam itself: registry -> libraryfolders.vdf -> appmanifest_4465480.acf -> <library>/steamapps/common/<installdir>/csgo

path() returns "" when nothing resolved so that os.path.join(GAME, ...) keeps
working at import time; commands that actually touch the disk call require(),
which raises NotFound with the two ways to fix it. Detection is read-only.
"""
import io
import os
import re
import sys

APPID = "4465480"                     # Counter-Strike: Global Offensive (Legacy)
GAME_SUBDIR = "csgo"                  # the folder holding gameinfo.txt
ENV_VAR = "CSGO_LEGACY_DIR"

if not getattr(sys.stdout, "_gamedir_utf8", False) and hasattr(sys.stdout, "buffer"):
    _OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    _OUT._gamedir_utf8 = True
    sys.stdout = _OUT


class NotFound(Exception):
    pass


def _frozen():
    return getattr(sys, "frozen", False)


def tool_root():
    """Directory the user actually installed this tool into."""
    if _frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.path.join(tool_root(), "settings.ini")


def read_config():
    p = config_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line)
        if not m:
            continue
        if section == "game" and m.group(1).lower() == "dir":
            val = m.group(2).strip().strip('"').strip("'")
            # allow %USERPROFILE%-style expansion, then make it absolute
            val = os.path.expandvars(val)
            if val and not os.path.isabs(val):
                val = os.path.normpath(os.path.join(tool_root(), val))
            return val or None
    return None


def write_config(directory):
    """Persist [game] dir, keeping every other line of the file intact."""
    p = config_path()
    lines = []
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    out, in_game, done = [], False, False
    for raw in lines:
        s = raw.strip()
        if s.startswith("["):
            in_game = s.lower() == "[game]"
        if in_game and not done and re.match(r"(?i)dir\s*[=:]", s):
            out.append("dir=%s" % directory)
            done = True
            continue
        out.append(raw)
    blocks = [i for i, l in enumerate(out) if l.strip().lower() == "[game]"]
    if not done and blocks:
        out.insert(blocks[0] + 1, "dir=%s" % directory)
    elif not done:
        out = ["# csgotool settings (written by `csgotool.py gamedir --set`)"] + out
        out += ["", "[game]", "dir=%s" % directory]
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    return p


def valid(dir_path):
    """A usable game dir = gameinfo.txt there and an executable one level up."""
    if not dir_path:
        return False
    g = dir_path.replace("/", os.sep).rstrip("\\/")
    if not os.path.isfile(os.path.join(g, "gameinfo.txt")):
        return False
    root = os.path.dirname(g)
    return (os.path.isfile(os.path.join(root, "csgo.exe")) or
            os.path.isfile(os.path.join(root, "srcds.exe")))


def _regex_first(text, key):
    m = re.search(r'"%s"\s+"([^"]*)"' % re.escape(key), text)
    return m.group(1) if m else None


def steam_paths():
    """Every install location of the Steam client we can find, best guess first."""
    found, seen = [], set()

    def add(p):
        if not p:
            return
        p = os.path.expandvars(p)
        if not os.path.isabs(p):
            return
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen and os.path.isdir(p):
            seen.add(key)
            found.append(os.path.normpath(p))

    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg:
        for hive, view in ((winreg.HKEY_CURRENT_USER, getattr(winreg, "KEY_WOW64_64KEY", 0)),
                           (winreg.HKEY_CURRENT_USER, 0),
                           (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
                           (winreg.HKEY_LOCAL_MACHINE, 0)):
            try:
                with winreg.OpenKey(hive, r"Software\Valve\Steam", 0,
                                    winreg.KEY_READ | view) as k:
                    add(winreg.QueryValueEx(k, "SteamPath")[0])
            except OSError:
                continue
    for cand in (os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                              "Steam"),
                 os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
                 r"C:\Steam", r"D:\Steam", r"E:\Steam"):
        add(cand)
    return found


def libraries():
    """Steam library roots: the client's own libraryfolders.vdf for each install."""
    out, seen = [], set()
    for steam in steam_paths():
        for rel in (os.path.join("config", "libraryfolders.vdf"),
                    os.path.join("steamapps", "libraryfolders.vdf")):
            p = os.path.join(steam, rel)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]*)"', text):
                lib = m.group(1).replace("\\\\", "\\").replace("\\", os.sep)
                key = os.path.normcase(os.path.normpath(lib))
                if key not in seen:
                    seen.add(key)
                    out.append(os.path.normpath(lib))
        key = os.path.normcase(steam)
        if key not in seen:
            seen.add(key)
            out.append(steam)
    return out


def app_manifests(lib):
    """appmanifest_<APPID>.acf in this library -> the installdir it names."""
    p = os.path.join(lib, "steamapps", "appmanifest_%s.acf" % APPID)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return _regex_first(fh.read(), "installdir")
    except OSError:
        return None


def candidates():
    """Every <...>/csgo that could be the game dir, deduped, in probe order."""
    out, seen = [], set()
    for lib in libraries():
        sa = os.path.join(lib, "steamapps", "common")
        inst = app_manifests(lib)
        ordered = []
        if inst:
            ordered.append(os.path.join(sa, inst, GAME_SUBDIR))
        if os.path.isdir(sa):
            try:
                for name in sorted(os.listdir(sa)):
                    if "csgo" not in name.lower():
                        continue
                    ordered.append(os.path.join(sa, name, GAME_SUBDIR))
            except OSError:
                pass
        ordered.append(os.path.join(lib, GAME_SUBDIR))
        for c in ordered:
            k = os.path.normcase(os.path.normpath(c))
            if k not in seen:
                seen.add(k)
                out.append(os.path.normpath(c))
    return out


_state = {}


def resolve():
    """(path, source) with path == "" when nothing was found."""
    if "path" in _state:
        return _state["path"], _state["source"]
    env = os.environ.get(ENV_VAR, "").strip()
    if env and valid(env):
        _state.update(path=os.path.normpath(env), source="env:%s" % ENV_VAR)
        return _state["path"], _state["source"]
    cfg = read_config()
    if cfg and valid(cfg):
        _state.update(path=os.path.normpath(cfg), source="config:%s" % config_path())
        return _state["path"], _state["source"]
    for c in candidates():
        if valid(c):
            _state.update(path=c, source="detect")
            return c, "detect"
    _state.update(path="", source="unresolved")
    return "", "unresolved"


def path():
    return resolve()[0]


def source():
    return resolve()[1]


def require():
    p = path()
    if p:
        return p
    raise NotFound(how_to_fix())


def how_to_fix():
    return "\n".join([
        "找不到 CS:GO Legacy (appid %s) 的安装目录。两种指定方式：" % APPID,
        "  1) 环境变量   set %s=<...\\steamapps\\common\\csgo legacy\\csgo>" % ENV_VAR,
        "  2) 写进配置   python tools/csgotool.py gamedir --set \"<那个 csgo 目录>\"",
        "判定标准：该目录里有 gameinfo.txt，且它的上一级有 csgo.exe 或 srcds.exe。",
        "扫过的库：%s" % (", ".join(libraries()) or "（没找到 Steam）"),
    ])


def reset():
    """Forget cached resolution -- tests and `gamedir --set` use this."""
    _state.clear()
