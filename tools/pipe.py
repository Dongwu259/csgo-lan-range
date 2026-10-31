#!/usr/bin/env python3
"""File command pipe for the CS:GO Legacy listen server (transport for the CLI tool).

The engine in this build has no connectionless RCON, so control goes through
addons/sourcemod/data/lan_pipe.txt, which the lan_pipe.smx plugin polls about
every 0.2s and executes as server-console commands. Results land in
addons/sourcemod/data/lan_exec.log.

  python pipe.py get  mp_freezetime
  python pipe.py set  mp_freezetime 15
  python pipe.py exec "bot_quota 6"
  python pipe.py raw  "?sv_cheats" "?mp_maxrounds"
"""
import os
import sys
import time

import gamedir

GAME = gamedir.path()
SM = os.path.join(GAME, "addons", "sourcemod", "data")
PIPE = os.path.join(SM, "lan_pipe.txt")
LOG = os.path.join(SM, "lan_exec.log")


def _log_size():
    return os.path.getsize(LOG) if os.path.exists(LOG) else 0


def _tail(offset):
    with open(LOG, "rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", "replace")


def send(lines, timeout=8.0, progress=None):
    """Append commands, wait for the plugin to consume them, return new log text.

    progress(elapsed_seconds) is called every couple of seconds while waiting.
    Without it a call against a stopped server is eight silent seconds, which
    from the outside is indistinguishable from a hung program."""
    if isinstance(lines, str):
        lines = [lines]
    payload = "\n".join(lines) + "\n"
    before = _log_size()
    with open(PIPE, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()

    start = time.time()
    deadline = start + timeout
    next_beat = start + 1.5
    consumed = False
    while time.time() < deadline:
        if _pipe_drained(payload):
            consumed = True
            break
        if progress and time.time() >= next_beat:
            progress(time.time() - start)
            next_beat = time.time() + 2.0
        time.sleep(0.1)

    # give the log write a moment after truncation
    time.sleep(0.25)
    out = _tail(before) if os.path.exists(LOG) else ""
    if not consumed:
        if progress:
            progress(-1)
        _unqueue(payload)
        return "[timeout] pipe not consumed within %.1fs\n" % timeout + out
    return out


def _unqueue(payload):
    """Drop our own lines if the server never got to them.

    Only the plugin truncates the pipe file, so a timed-out command sits there
    and runs the instant a map finally loads. An overnight 3 s poller of
    ?mp_maxrounds accumulated 3.2 KB this way and buried the first real
    verification output in lan_exec.log."""
    try:
        with open(PIPE, "r", encoding="utf-8", errors="replace") as fh:
            rest = fh.read()
    except FileNotFoundError:
        return
    drop = {line.strip() for line in payload.splitlines() if line.strip()}
    keep = [line for line in rest.splitlines() if line.strip() not in drop]
    if keep != rest.splitlines():
        with open(PIPE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(keep) + ("\n" if keep else ""))


def _pipe_drained(payload):
    """True when our payload is no longer sitting in the pipe file."""
    try:
        with open(PIPE, "r", encoding="utf-8", errors="replace") as fh:
            rest = fh.read()
    except FileNotFoundError:
        return True
    for line in payload.splitlines():
        if line.strip() and line.strip() in rest:
            return False
    return True


def get_cvar(name, timeout=8.0):
    text = send(["?%s" % name], timeout)
    for line in reversed(text.splitlines()):
        if (" %s = " % name) in line:
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no value returned for %s: %r" % (name, text))


def set_cvar(name, value, timeout=8.0):
    send(["%s %s" % (name, value)], timeout)
    return get_cvar(name, timeout)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    op = sys.argv[1]
    if op == "get":
        print(get_cvar(sys.argv[2]))
    elif op == "set":
        print("%s = %s" % (sys.argv[2], set_cvar(sys.argv[2], sys.argv[3])))
    elif op == "exec":
        print(send(sys.argv[2]).strip())
    elif op == "raw":
        print(send(sys.argv[2:]).strip())
    else:
        print(__doc__)
        raise SystemExit(2)
