#!/usr/bin/env python3
"""Rebuild the two one-file executables and prove they still start.

PyInstaller is only needed here, never at runtime. The output lands in exe\\ and
the run prints SHA-256 per file, which is what a GitHub Release note should say
so a downloader can tell the exe was not touched after this run.

  python tools\\build_exe.py                 # both
  python tools\\build_exe.py --only server
"""
import argparse
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGETS = [
    {"name": "csgo-server-tool", "entry": "tools/csgotool.py",
     "hidden": ["shell_ui", "pipe", "bot_profiles", "skins_catalog", "gamedir",
                "installer", "bots_patch"],
     "data": ["configs", "release", "tools/src"],
     "key": "server",
     # (args, expected rc, a string that must appear) -- proves the frozen build
     # starts and that --add-data actually carried the manifest along
     "checks": [(["--help"], 0, "usage"),
                (["install", "--list"], 0, "botpack"),
                (["install", "--audit", "--into",
                  os.path.join(ROOT, "build", "_not_a_game")], 2, "不像")]},
    {"name": "csgo-room-client", "entry": "tools/csgoclient.py",
     "hidden": [], "data": [], "key": "client",
     # no arguments: the client is a menu loop, and an empty stdin makes it draw
     # the menu once and leave on EOF -- that is the whole smoke test
     "checks": [([], 0, "房间客户端")]},
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def have_pyinstaller():
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").strip()


def build(target, console=True):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--log-level", "WARN",
           "--console" if console else "--windowed",
           "--distpath", os.path.join(ROOT, "exe"),
           "--workpath", os.path.join(ROOT, "build", target["name"]),
           "--specpath", os.path.join(ROOT, "build"),
           "--name", target["name"]]
    for m in target["hidden"]:
        cmd += ["--hidden-import", m]
    for d in target["data"]:
        # absolute source: --specpath moves PyInstaller's relative base
        cmd += ["--add-data", "%s;%s" % (os.path.join(ROOT, *d.split("/")), d)]
    cmd.append(os.path.join(ROOT, *target["entry"].split("/")))
    print("\n$ python -m PyInstaller ... %s" % target["entry"])
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("PyInstaller 失败（rc=%d），没产出可执行的 %s" %
                         (r.returncode, target["name"]))
    return os.path.join(ROOT, "exe", target["name"] + ".exe")


def smoke(exe, checks):
    """Run the frozen build the way a stranger would: no game folder assumed."""
    out = []
    for argv, want, needle in checks:
        r = subprocess.run([exe] + argv, input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=180)
        text = r.stdout.decode("utf-8", "replace")
        ok = r.returncode == want and needle in text
        reason = "" if ok else ("没出现 %r；末行 %s" % (
            needle, (text.strip().splitlines() or ["(空)"])[-1][:60]))
        out.append((" ".join(argv) or "(菜单)", ok, r.returncode, reason))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="重新打包两端 exe")
    p.add_argument("--only", choices=["server", "client"], default=None)
    p.add_argument("--no-smoke", action="store_true", help="只打包，不跑自检")
    args = p.parse_args(argv)

    v = have_pyinstaller()
    if not v:
        print("环境里没有 PyInstaller。装上再跑：\n  python -m pip install pyinstaller")
        return 2
    print("PyInstaller %s" % v)

    chosen = [t for t in TARGETS if not args.only or t["key"] == args.only]
    failed = False
    for t in chosen:
        exe = build(t)
        if not os.path.isfile(exe):
            print("!! 没产出 %s" % exe)
            failed = True
            continue
        print("%-20s %10s 字节  sha256 %s" % (t["name"], format(os.path.getsize(exe), ","),
                                              sha256(exe)))
        if args.no_smoke:
            continue
        for argv, ok, rc, reason in smoke(exe, t["checks"]):
            print("  [%s] %-34s rc=%-3s %s" % ("ok" if ok else "!!", argv, rc, reason))
            failed = failed or not ok
    if failed:
        print("\n自检没全过，别急着发出去。")
        return 1
    print("\n两个 exe 都能跑。要挂到 Release 的话，把上面的 sha256 一起写进说明。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
