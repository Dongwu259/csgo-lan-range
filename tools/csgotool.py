#!/usr/bin/env python3
"""csgotool -- control the CS:GO Legacy listen server from the command line.

Stdlib only. Control goes through the lan_pipe.smx file pipe (this build has no
connectionless RCON), skin state goes through lan_fix.smx plus kgns' SQLite, and
config writes are checked against this exact build's binaries before they are
ever executed.

  python csgotool.py status
  python csgotool.py shell      # 常驻交互窗口，带主菜单
  python csgotool.py who
  python csgotool.py watch --players
  python csgotool.py bots                  # 对局里 bot 的应有值 vs 引擎里真正生效的值
  python csgotool.py start de_mirage --now # 需要时拉起游戏并开一局，然后打印加入方式
  python csgotool.py join --write-cmd      # 给朋友生成分组双击即连的 .cmd
  python csgotool.py get  mp_freezetime
  python csgotool.py set  bot_quota 6
  python csgotool.py map  de_mirage
  python csgotool.py lan  --bots 6 --password hunter2
  python csgotool.py duel --rounds 10 --money 4750 --weapon awp --bots 0 --apply
  python csgotool.py duel --show
  python csgotool.py join --run
  python csgotool.py cfg  gen practice --set mp_freezetime=5
  python csgotool.py verify lan.cfg
  python csgotool.py skins list
  python csgotool.py skins search 血腥
  python csgotool.py skins set awp 血腥运动 --wear 0.42
  python csgotool.py skins set awp 344 --seed 1000
  python csgotool.py skins dump
  python csgotool.py doctor
"""
import argparse
import io
import json
import os
import re
import shutil
import shlex
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

import pipe  # the transport, same directory
import bot_profiles
import skins_catalog
import gamedir
import installer
import bots_patch

if hasattr(sys.stdout, "buffer") and not getattr(sys.stdout, "_csgotool_utf8", False):
    # this console is cp936 while player and skin names are UTF-8. The guard is
    # not decoration: wrapping twice orphans the first TextIOWrapper, and when
    # it is garbage-collected it closes the shared buffer underneath, so every
    # later print dies with "I/O operation on closed file".
    _OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                            line_buffering=True)
    _OUT._csgotool_utf8 = True
    sys.stdout = _OUT

# Resolved at import by tools/gamedir.py (env var -> settings.ini -> Steam scan).
# It stays "" when nothing was found so the joins below cannot blow up at import;
# main() refuses disk-touching commands with gamedir.how_to_fix() instead.
GAME = gamedir.path()
CFG = os.path.join(GAME, "cfg")
SM = os.path.join(GAME, "addons", "sourcemod")
DB = os.path.join(SM, "data", "sqlite", "sourcemod-local.sq3")
FIXLOG = os.path.join(SM, "data", "lan_fix.log")
EXECLOG = os.path.join(SM, "data", "lan_exec.log")
BINS = {
    "server.dll": os.path.join(GAME, "bin", "server.dll"),
    "engine.dll": os.path.join(GAME, "..", "bin", "engine.dll"),
}

# classes = the per-weapon column families in kgns' own tables
KNIFE_PREFIX = "knife_"
BOTPROFILE = os.path.join(GAME, "botprofile.db")
APPID = gamedir.APPID    # the "csgo legacy" depot entry in Steam
DEFAULT_PORT = 27015
# bot_stuff.smx matches these with case-sensitive strcmp, so a bot only gets
# the 100000 / 0.0 / 1.0 treatment when its name matches this spelling exactly
TOP_BOTS = ("sh1ro", "ZywOo", "ropz", "donk", "m0NESY")
# join-*.cmd files for friends land here. Under PyInstaller __file__ points
# into the temp extraction dir, which would silently throw those files away on
# exit, so the frozen build anchors on the executable instead.
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LAN_DIR = os.path.join(ROOT, "lan")
BACKUP_DIR = os.path.join(ROOT, "backup")


# ------------------------------------------------------------------ helpers
def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def whole_word(buf, token):
    n = token.encode("latin-1")
    ident = set(range(48, 58)) | set(range(65, 91)) | set(range(97, 123)) | {95}
    start = 0
    while True:
        i = buf.find(n, start)
        if i < 0:
            return False
        before = buf[i - 1] if i else 0
        after = buf[i + len(n)] if i + len(n) < len(buf) else 0
        if before not in ident and after not in ident:
            return True
        start = i + 1


def cfg_names(path):
    names = []
    with open(path, encoding="latin-1", errors="replace") as fh:
        for line in fh:
            line = line.split("//", 1)[0].strip()
            if line:
                tok = line.split()[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok):
                    names.append(tok)
    return names


def cmd_verify(args):
    path = args.file
    if not os.path.exists(path):
        # accept "lan", "lan.cfg", or a name living in the game's cfg directory
        for cand in (os.path.join(CFG, path),
                     os.path.join(CFG, path + ".cfg"),
                     os.path.join(CFG, os.path.basename(path))):
            if os.path.exists(cand):
                path = cand
                break
    if not os.path.exists(path):
        sys.exit("no such cfg file: %s (also looked in %s)" % (args.file, CFG))
    blobs = {}
    for label, p in BINS.items():
        if os.path.exists(p):
            blobs[label] = _read_bytes(p)
        else:
            print("note: %s not found, skipped" % p)
    bad = []
    names = cfg_names(path)
    for name in sorted(set(names)):
        if not any(whole_word(b, name) for b in blobs.values()):
            bad.append(name)
    print("checked %d names in %s against %s" % (
        len(set(names)), os.path.basename(path), " + ".join(blobs)))
    if bad:
        print("NOT PRESENT IN THIS BUILD (%d): %s" % (len(bad), ", ".join(bad)))
        return 1
    print("all names exist")
    return 0


def _default_target():
    """The connected human's account id, which ResolveTarget accepts and which is
    stable across rounds; bots report 0 so they are never picked."""
    rows, _ = _players()
    for r in rows:
        if r["bot"] == "0" and r["account"] not in ("0", ""):
            return r["account"]
    return None


PIPE_DEAD = ("管道没有响应：游戏可能暂停（ESC 菜单）、还在主菜单或读图中，"
             "或者 lan_pipe.smx 没加载。用 csgotool.py doctor 复查。")


def _rows(text, pattern):
    out = []
    for line in text.splitlines():
        m = re.search(pattern, line)
        if m:
            out.append(m)
    return out


def _beat(elapsed):
    """One-line progress for a pipe round trip. Overwritten with \\r so a slow
    server shows movement instead of looking like a hung terminal."""
    if elapsed < 0:
        sys.stdout.write("\r" + " " * 60 + "\r")
    else:
        sys.stdout.write("\r  等服务器应答 %.0fs（游戏没在跑 / 停在主菜单 / ESC 暂停时它不会应答）" % elapsed)
    sys.stdout.flush()


def _status():
    """-> dict with map/humans/bots/timeleft/scores, or None when the pipe died."""
    text = pipe.send(["~status"], progress=_beat)
    for m in _rows(text, r"status map=(\S+) humans=(\d+) bots=(\d+) max=(\d+) "
                        r"timeleft=(-?\d+) freeze=(-?\d+) t=(-?\d+) ct=(-?\d+)"):
        return dict(zip(("map", "humans", "bots", "max", "timeleft", "freeze", "t", "ct"),
                        m.groups()))
    print(PIPE_DEAD)
    print(text.strip())
    return None


def _fmt_status(st):
    tl = int(st["timeleft"])
    clock = "%d:%02d" % (tl // 60, tl % 60) if tl >= 0 else "无限"
    return "%-14s 真人 %-2s/%-3s bot %-2s | T %s - %s CT | 剩余 %-6s 冻结 %ss" % (
        st["map"], st["humans"], st["max"], st["bots"], st["t"], st["ct"], clock, st["freeze"])


def cmd_status(_args):
    st = _status()
    if st is None:
        return 1
    print(_fmt_status(st))
    return 0


def cmd_watch(args):
    """Poll status, and optionally the slot table, until Ctrl+C.

    Prints only when something changed so a long session stays readable. The
    interval floors at 1s: every tick is a disk round trip through the plugin's
    0.2s poll, not a cheap local read."""
    interval = max(1.0, args.every)
    last = None
    print("每 %.1fs 刷新一次（Ctrl+C 退出）" % interval)
    try:
        while True:
            st = _status()
            if st is None:
                return 1
            line = _fmt_status(st)
            if args.players:
                rows, _ = _players()
                humans = [r["name"] for r in rows if r["bot"] == "0"]
                alive = sum(1 for r in rows if r["alive"] == "1")
                line += " | 存活 %-2s 真人: %s" % (alive, ", ".join(humans) or "无")
            if line != last:
                print("%s %s" % (time.strftime("%H:%M:%S"), line))
                last = line
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _players():
    """Ask the server for its slot table. Returns dicts with string values.

    lan_pipe 1.3.0 quotes the name because real bot handles contain spaces
    ("[RAM]The Admin"); the unquoted pattern is kept so an older plugin build
    still parses instead of silently reporting nobody in the server."""
    text = pipe.send(["~players"], progress=_beat)
    rows = []
    for line in text.splitlines():
        m = re.search(r'player index=(\d+) userid=(\d+) account=(\d+) name="(.*?)" team=(\d+) alive=(\d+) bot=(\d+)', line)
        if not m:
            m = re.search(r"player index=(\d+) userid=(\d+) account=(\d+) name=(\S*) team=(\d+) alive=(\d+) bot=(\d+)", line)
        if m:
            idx, uid, acc, name, team, alive, bot = m.groups()
            rows.append({"index": idx, "userid": uid, "account": acc,
                         "name": name, "team": team, "alive": alive, "bot": bot})
    return rows, text


def cmd_who(_args):
    rows, text = _players()
    if not rows:
        print(text.strip() or "no players returned")
        return 1
    print("%-6s %-7s %-11s %-20s %-5s %-6s %s" % ("index", "userid", "account", "name", "team", "alive", "kind"))
    for r in rows:
        print("%-6s %-7s %-11s %-20s %-5s %-6s %s" % (
            r["index"], r["userid"], r["account"] or "-", r["name"] or "(unnamed)",
            r["team"], r["alive"], "bot" if r["bot"] == "1" else "HUMAN"))
    return 0


# ------------------------------------------------------------------ bot numbers
def bot_info():
    """data/bot_info.json: the name list bot_stuff.smx gates its overrides on.
    A bot whose name is absent here is left alone by the plugin, so whatever
    botprofile.db said is what the engine runs -- membership is the switch."""
    path = os.path.join(SM, "data", "bot_info.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _floats_from_hex(hexstr):
    out = []
    for i in range(0, len(hexstr) - len(hexstr) % 8, 8):
        word = hexstr[i:i + 8]
        out.append(struct.unpack("<f", bytes.fromhex(word))[0])
    return out


def _ints_from_hex(hexstr):
    return [struct.unpack("<I", bytes.fromhex(hexstr[i:i + 8]))[0]
            for i in range(0, len(hexstr) - len(hexstr) % 8, 8)]


# Offsets inside the engine's BotProfile, measured rather than copied: dumped
# from a live match on 2026-10-31 and matched against botprofile.db. Every row
# below either equalled the configured value (after the scale conversion) or
# held exactly one of bot_stuff's documented random replacements.
# scale = what to divide botprofile.db's number by: the file writes Skill /
# Teamwork / Aggression as 0-100, the struct keeps them as 0..1.
PROFILE_FIELDS = [
    (0x04, "Aggression", "f", 100.0),
    (0x08, "Skill", "f", 100.0),
    (0x0C, "Teamwork", "f", 100.0),
    (0x10, "AimFocusInitial", "f", 1.0),
    (0x14, "AimFocusDecay", "f", 1.0),
    (0x18, "AimFocusOffsetScale", "f", 1.0),
    (0x1C, "AimfocusInterval", "f", 1.0),
    (0x44, "Cost", "i", 1.0),
    (0x50, "VoicePitch", "i", 1.0),
    (0x54, "ReactionTime", "f", 1.0),
    (0x58, "AttackDelay", "f", 1.0),
    (0x68, "LookAngleMaxAccelNormal", "f", 1.0),
    (0x6C, "LookAngleStiffnessNormal", "f", 1.0),
    (0x70, "LookAngleDampingNormal", "f", 1.0),
    (0x74, "LookAngleMaxAccelAttacking", "f", 1.0),
    (0x78, "LookAngleStiffnessAttacking", "f", 1.0),
    (0x7C, "LookAngleDampingAttacking", "f", 1.0),
]
# the four bot_stuff.smx overwrites every tick for any name in bot_info.json
OVERRIDDEN = ("Aggression", "ReactionTime", "LookAngleMaxAccelNormal",
              "LookAngleMaxAccelAttacking")


def _botmem():
    """Read back what the running engine actually holds for each bot.

    Parsed from lan_fix's own tee rather than the pipe echo: the report lines
    carry raw player names and 320 hex characters, and lan_fix.log is a plain
    append nobody truncates, so the record is complete."""
    before = os.path.getsize(FIXLOG) if os.path.exists(FIXLOG) else 0
    out = pipe.send(["sm_lanfix_botmem all"])
    if "[timeout]" in out[:200]:
        print(PIPE_DEAD)
        return None
    text = b""
    if os.path.exists(FIXLOG):
        with open(FIXLOG, "rb") as fh:
            fh.seek(before)
            text = fh.read()
    recs = {}
    for line in text.decode("utf-8", "replace").splitlines():
        m = re.search(r"botmem idx=(\d+) name=(.*?) team=(\d+) alive=(\d+) profile=(0x[0-9a-f]+)", line)
        if not m:
            continue
        idx, name, team, alive, prof = m.groups()
        rec = {"idx": idx, "name": name, "team": team, "alive": alive, "profile": prof,
               "skipped": "SKIPPED" in line}
        for k, v in re.findall(r"(aggression|reactionTime|accelNormal|accelAttacking|task|morale|lookState|nearby|visible|disp|safe|enemy|raw)=([^\s]+)", line):
            rec[k] = v
        if rec.get("raw"):
            rec["words"] = _floats_from_hex(rec["raw"])
            rec["iwords"] = _ints_from_hex(rec["raw"])
        recs[name] = rec
    return recs


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _locate(rec, want):
    """Fallback for the fields with no verified offset yet: find the word whose
    float equals this value. Zero is skipped because a third of the struct is
    padding, so "0 matches" carries no information."""
    if not isinstance(want, (int, float)) or want == 0:
        return []
    return [i * 4 for i, f in enumerate(rec.get("words", []))
            if abs(f - want) <= abs(want) * 1e-4]


def cmd_bots(args):
    if args.name == "patch":
        return cmd_bots_patch(args)
    db = bot_profiles.load()
    info = bot_info()
    rows, _ = _players()
    bots = [r for r in rows if r["bot"] == "1"]
    if args.name:
        bots = [r for r in bots if r["name"].lower() == args.name.lower()]
        if not bots:
            print("对局里没有叫 %s 的 bot（who 看当前名单）" % args.name)
            return 2
    if not bots:
        print("对局里没有 bot。bot_quota 现在是 %s" % _safe_get("bot_quota"))
        return 1

    live = None if args.no_live else _botmem()
    if live is None and not args.no_live:
        print("（读不到实时值，只列 botprofile.db 的配置值）")
        live = {}

    print("%-14s %-4s %-3s %-6s %-8s %-8s %-8s %-9s %s" % (
        "名字", "队", "活", "档位", "反应s", "实测反应", "进攻/100", "实测进攻", "接管"))
    print("-" * 86)
    tiers = {}
    for r in bots:
        recs = bot_profiles.get(db, r["name"])
        rec = recs[0] if recs else None
        tier = rec["tier"] if rec else "无档"
        tiers[tier] = tiers.get(tier, 0) + 1
        want = rec["values"].get("ReactionTime") if rec else None
        agg = rec["values"].get("Aggression") if rec else None
        mem = (live or {}).get(r["name"]) or {}
        lre, lag = _num(mem.get("reactionTime")), _num(mem.get("aggression"))
        if r["name"] in info:
            who = "bot_info 命中"
            if r["name"] in TOP_BOTS:
                who = "bot_info+top 档"
            if lre is not None and want is not None and abs(lre - want) > 1e-6:
                who += "（已覆盖）"
        else:
            who = "未命中：插件不管"
        print("%-14s %-4s %-3s %-6s %-8s %-8s %-8s %-9s %s" % (
            r["name"][:14], r["team"], r["alive"], tier,
            "-" if want is None else ("%g" % want),
            "-" if lre is None else ("%.3f" % lre),
            "-" if agg is None else ("%g" % agg),
            "-" if lag is None else ("%.3f" % lag),
            who))

    print("\n档位分布: " + "  ".join("%s=%d" % (k, v) for k, v in
                               sorted(tiers.items(), key=lambda kv: -kv[1])))
    print("bot_info.json 共 %d 个名字；本局 %d 个 bot 中 %d 个命中" % (
        len(info), len(bots), len([r for r in bots if r["name"] in info])))
    if len(tiers) > 1:
        print("本局档位不统一（强度按名字抽卡）。拉平：python tools\\csgotool.py bots patch --names")
    print("反应时间参考：职业选手简单视觉反应约 0.15-0.25s；实测 0.000 只可能出现在 top 五个人名上（插件写死），不是人类水平")
    if args.detail:
        for r in bots:
            _bot_detail(bot_profiles.get(db, r["name"]), (live or {}).get(r["name"]), info)
    return 0


def _safe_get(name):
    try:
        return pipe.get_cvar(name)
    except Exception:
        return "?"


def _bot_detail(recs, mem, info):
    if not recs:
        print("\n-- 名字不在 botprofile.db 里：引擎随机分配的档位在别处，这里查不到")
        return
    rec = recs[0]
    name = rec["name"]
    taken = name in info
    print("\n== %s  chain=%s  tier=%s  profile=%s%s" % (
        name, "+".join(rec["chain"]), rec["tier"], (mem or {}).get("profile", "-"),
        "" if taken else "   ← 不在 bot_info.json，bot_stuff 不接管"))
    if not mem or not mem.get("words"):
        print("   （没有实时数据：--no-live 或探针没返回）")
        return
    w, iw = mem["words"], mem["iwords"]
    print("   %-26s %-12s %-14s %-8s %s" % ("字段", "库里值", "引擎实测", "偏移", "判定"))
    for off, field, kind, scale in PROFILE_FIELDS:
        if field not in rec["values"]:
            continue
        want = rec["values"][field] / scale
        got = (iw if kind == "i" else w)[off // 4]
        same = abs(got - want) <= max(1e-9, abs(want) * 1e-4)
        verdict = "一致" if same else ("被 bot_stuff 改写" if field in OVERRIDDEN and taken else "不一致")
        print("   %-26s %-12s %-14s +0x%-5X %s" % (field, rec["values"][field],
                                               ("%g" % got), off, verdict))
    for field in sorted(set(rec["values"]) - {f for _, f, _, _ in PROFILE_FIELDS} - {"WeaponPreference", "Difficulty"}):
        hits = _locate(mem, rec["values"][field])
        if len(hits) == 1:
            print("   %-26s %-12s %-14s +0x%-5X 待确认" % (field, rec["values"][field],
                                        ("%g" % rec["values"][field]), hits[0]))
    print("   实时状态 task=%s morale=%s lookState=%s nearby=%s visible=%s disp=%s safe=%s" % (
        mem.get("task"), mem.get("morale"), mem.get("lookState"), mem.get("nearby"),
        mem.get("visible"), mem.get("disp"), mem.get("safe")))
    if rec["tier"] == "ProP":
        print("   注：ProP 档把 AimFocus 四项写成 1e-05 / 1e-07 / 1e-25 / 1e-13，即准星散布与追踪误差被压到 0，"
              "这一项已经超出人类精度，不是'像职业'而是'无抖动'")


def cmd_get(args):
    print(pipe.get_cvar(args.name))
    return 0


def cmd_set(args):
    value = " ".join(args.value)
    if args.name == "sv_lan" and value in ("1", "true"):
        print("REFUSING: sv_lan 1 disables authentication, and kgns keys every skin "
              "row on the SteamID (GetClientAuthId is fail-closed), so everyone would "
              "lose their skins. Pass --force if you really mean it.")
        if not args.force:
            return 2
    readback = pipe.set_cvar(args.name, value)
    ok = readback == value
    print("%s = %s   (%s)" % (args.name, readback, "verified" if ok else "DIFFERS from requested %s" % value))
    return 0 if ok else 1


def cmd_exec(args):
    print(pipe.send([" ".join(args.command)]).strip())
    return 0


MAP_RE = re.compile(r"^(de|cs|ar|ag|mg|dust|train)_?[a-z0-9_]*$")


def known_maps():
    maps = set()
    for candidate in ("maplist.txt", "mapcycle.txt", os.path.join("cfg", "maplist.txt")):
        p = os.path.join(GAME, candidate)
        if os.path.exists(p):
            with open(p, encoding="latin-1", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if MAP_RE.match(line):
                        maps.add(line)
    # whatever the bot nade database covers, plus anything loose on disk
    nades = os.path.join(SM, "configs", "bot_nades.txt")
    if os.path.exists(nades):
        with open(nades, encoding="utf-8", errors="replace") as fh:
            maps.update(re.findall(r'"(de_[a-z0-9_]+)"', fh.read()))
    maps_dir = os.path.join(GAME, "maps")
    if os.path.isdir(maps_dir):
        for d in os.listdir(maps_dir):
            # the loose files carry the .bsp suffix; without stripping it every
            # custom map was invisible to `map`/`start`
            if d.lower().endswith(".bsp"):
                d = d[:-4]
            if MAP_RE.match(d):
                maps.add(d)
    return sorted(maps)


def launch_uri():
    """steam://run/<appid> is the only way to reach this install -- but it takes
    no arguments.

    Running csgo.exe directly fails with "FATAL ERROR: Failed to connect with
    local Steam Client process" even while Steam is up: steam_appid.txt in the
    folder says 730, and 730 is not what is installed here (the legacy build is
    appid 4465480), so Steam refuses the session. Going through the URI makes
    Steam launch the right app.

    Arguments do not survive the URI. Proven 2026-10-31 with a file-based
    probe: -condebug after the "//" never produced the console.log that
    engine.dll really does implement, so the Steam client drops everything after
    the double slash. The game's command line is still injectable -- appinfo
    shows this app launches as "csgo.exe -steam -worldwide" -- but only through
    the launch-options field, see launch_options()."""
    return "steam://run/%s" % APPID


def launch_options(map_name=None, game_type=1, game_mode=0, connect_to=None,
                   password=None, verify=False, insecure=False):
    """Text to paste into Steam's 属性 → 启动选项, which the game does receive.

    insecure=True belongs on the *host* line and is not decoration: while the
    session is VAC-secure the engine refuses to load metamod/SourceMod/PTaH at
    all, so a match with skins and bots needs -insecure. A guest joining that
    room does not need it, which is why connect_to does not imply it.

    This is how the third-party launchers get 一键进房: they hand the engine a
    command line. client.dll carries the string "Executing deferred connect
    command: %s.", which means +connect is deliberately held until the Panorama
    main menu is up instead of being run and lost.

    verify=True appends -condebug: if csgo\\console.log appears, the arguments
    reached the command line, and any behaviour change is then the engine's
    doing rather than the launcher's."""
    parts = ["-insecure"] if insecure else []
    if connect_to:
        parts.append("+connect %s" % connect_to)
        if password:
            parts[-1] += " " + password
    elif map_name:
        parts += ["+game_type %d" % game_type, "+game_mode %d" % game_mode,
                  "+map %s" % map_name]
    if verify:
        parts.append("-condebug")
    return " ".join(parts)


def launch_game(map_name=None, game_type=1, game_mode=0):
    """Open the game through Steam. It lands on the main menu, not on a map.

    The arguments are accepted for call-site compatibility and then dropped,
    because steam://run carries them into the Steam client but no further. Only
    the launch-options field reaches the engine, so an unattended 开局 has to be
    set up there first -- see launch_options()."""
    uri = launch_uri()
    os.startfile(uri)
    return uri


def ensure_appid_file():
    """Point steam_appid.txt at the app that is actually installed.

    The folder ships 730, and csgo.exe authenticates against whatever that file
    says -- 730 is CS2, which is not this install -- so a direct start parks on
    a Launcher Error dialog at ~16 MB. Writing 4465480 makes the very same exe
    start normally, and that is the only way to get a command line into the
    game, since steam://run drops everything after the double slash. The engine
    rewrites this file on a Steam-launched start, so it is asserted before each
    direct launch rather than treated as a one-time fix.
    """
    path = os.path.join(os.path.dirname(GAME), "steam_appid.txt")
    want = (APPID + "\n").encode() + b"\x00"
    try:
        cur = open(path, "rb").read()
    except OSError:
        cur = b""
    if cur == want:
        return path, True
    os.makedirs(BACKUP_DIR, exist_ok=True)
    bak = os.path.join(BACKUP_DIR, "steam_appid.txt.orig")
    if cur and not os.path.exists(bak):
        with open(bak, "wb") as fh:
            fh.write(cur)
    with open(path, "wb") as fh:
        fh.write(want)
    return path, False


def launch_direct(map_name=None, game_type=1, game_mode=0, windowed=False,
                  condebug=False):
    """Start the game ourselves, with a command line -- no menu clicking.

    Proven 2026-10-31: csgo.exe -steam -worldwide -insecure +game_type 1
    +game_mode 0 +map de_nuke reaches INGAME with all eight mod DLLs loaded and
    the pipe answering, provided steam_appid.txt names the installed app.

    Two costs, both measured the hard way: (1) the process gets no Steam user
    session (AuthStatus Failed / Authentication and VAC not requested), so the
    player's binds, sensitivity and video settings look wiped even though
    userdata/.../cfg/config.cfg on disk is untouched; (2) +game_type/+game_mode
    land after Host_NewGame, so the match boots in the engine's default mode and
    needs one changelevel to actually become competitive. Launching from Steam
    with the same text in the launch-options field avoids both, so prefer that
    for playing and keep this for automation."""
    root = os.path.dirname(GAME)
    exe = os.path.join(root, "csgo.exe")
    if not os.path.exists(exe):
        raise RuntimeError("找不到 " + exe)
    path, already = ensure_appid_file()
    args = [exe, "-steam", "-worldwide", "-insecure"]
    if windowed:
        args += ["-novid", "-w", "1280", "-h", "720", "-window"]
    if condebug:
        args.append("-condebug")
    args += launch_options(map_name, game_type, game_mode).split()
    proc = subprocess.Popen(args, cwd=root, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    shown = " ".join(a if " " not in a else '"%s"' % a for a in args[1:])
    return {"pid": proc.pid, "cmd": shown, "appid": os.path.basename(path),
            "appid_was": "已就位" if already else "刚写入"}


def cold_start_hint(map_name=None, game_type=1, game_mode=0):
    """How to get a command line into the game, both ways.

    The tool can already do it itself -- launch_direct starts csgo.exe with
    -insecure +map, and that has been shown to reach INGAME with the mod stack
    loaded. This text is for the other case: starting the game by hand from
    Steam, where the arguments have to live in the launch-options field, because
    steam://run drops everything after the double slash."""
    print("\n免点开局有两条路，第一条是正经玩用的：")
    print("  1) 你自己从 Steam 启动（保留账号会话，键位/灵敏度/视频都在）：")
    print("     库 → CS:GO Legacy → 右键 属性 → 启动选项，粘贴：")
    print("    " + (launch_options(map_name, game_type, game_mode, insecure=True)
                    or launch_options(insecure=True)))
    print("  2) 让工具直连（免点，但拿不到 Steam 用户会话，键位和设置会看起来被重置）：")
    print("     csgotool.py start %s --launch" % (map_name or "de_nuke"))
    print("  两种方式都必须带 -insecure：会话是 VAC 安全状态时引擎拒绝加载 metamod/SourceMod/PTaH，")
    print("  对局看着正常但没有皮肤、没有 bot 强化、没有文件管道（用 csgotool 的 mods 命令查）。")
    print("  直连启动还依赖一件事：安装目录的 steam_appid.txt 必须是 4465480。它出厂写的是 730，")
    print("  而 730 是 CS2、不是本机装的这个 app，于是 Steam 拒绝给会话，进程卡在 Launcher Error。")
    print("  工具会在每次直连前把它对齐（原值备份在 backup/steam_appid.txt.orig）。")
    print("  判定成功与否不用看屏幕：csgotool.py mods 列出 8 个 mod DLL、")
    print("  csgotool.py status 几秒内回数据，就是自动开局成立。")
    print("  改回默认（每次手点）就把启动项清空。注意设成 +connect 会让每次开游戏都往外连。")


GAMEMODE_CFGS = ("gamemode_armsrace", "gamemode_competitive", "gamemode_casual",
                 "gamemode_deathmatch", "gamemode_demolition", "gamemode_retakecasual")
OWN_CFGS = ("bots_improved.cfg", "lan.cfg", "listenserver.cfg",
            "gamemode_competitive_offline.cfg")
CFG_LINE_RE = re.compile(r"([A-Za-z0-9_]+)\s+([^;]+)")


def _cfg_vars(name):
    out = {}
    path = os.path.join(CFG, name)
    if not os.path.exists(path):
        return out
    for line in _read_bytes(path).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = CFG_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def cmd_mode(args):
    """Classify the live match by its rule cvars, not by game_type.

    game_type/game_mode are only informational here: a +map boot leaves
    GameRules in the engine default mode (this build: arms race, logged as
    "couldn't exec gamemode_armsrace_server.cfg") while both cvars still read
    1/0, and neither writing them nor a changelevel converts the running
    session. The cash_* awards distinguish the modes cleanly and none of them
    are touched by our own cfg files, so they are a safe oracle."""
    ours = set()
    for f in OWN_CFGS:
        ours |= set(_cfg_vars(f))
    modes = dict((n, _cfg_vars(n + ".cfg")) for n in GAMEMODE_CFGS if _cfg_vars(n + ".cfg"))
    # A cvar is only useful if the modes disagree on it. Taking the union (not
    # the intersection) keeps mode-specific keys such as mp_respawn_on_death_ct,
    # which is what separates deathmatch from arms race in the first place.
    probes = []
    for cvar in sorted(set.union(*[set(m) for m in modes.values()])):
        values = set(m.get(cvar, "-") for m in modes.values())
        if len(values) > 2 and cvar not in ours and not cvar.startswith(("bot_", "ammo_")):
            probes.append(cvar)
    probes = probes[:16]
    if not probes:
        print("找不到可判别的 cvar")
        return 2
    out = pipe.send(["?%s" % c for c in probes] + ["?game_type", "?game_mode"])
    cur = dict((m.group(1), m.group(2).strip().strip('"'))
               for m in (re.search(r"(\w+) = (.*)", l) for l in out.splitlines()) if m)
    hits = sorted(((sum(1 for c in probes if cur.get(c) == m.get(c)), name)
                   for name, m in modes.items()), key=lambda t: -t[0])
    best = hits[0][0]
    tied = [n.replace("gamemode_", "") for s, n in hits if s == best]
    print("cvar 指纹（只取各模式取值不同、且我们自己的 cfg 没碰过的）:")
    for c in probes:
        print("  %-34s 实测=%-8s %s" % (c, cur.get(c, "?"),
              " ".join("%s=%s" % (n.replace("gamemode_", ""), m.get(c, "-")) for n, m in modes.items())))
    print()
    print("判定: %s   （game_type=%s game_mode=%s，命中 %d/%d）"
          % ("/".join(tied), cur.get("game_type"), cur.get("game_mode"), best, len(probes)))
    if len(tied) > 1:
        print("  这几个模式在这些 cvar 上无法区分；能确定的是它们都属于%s。"
              % ("非竞赛玩法（现金奖励全为 0，即军备竞赛/死斗/爆破抢分一类）"
                 if set(tied) <= {"armsrace", "deathmatch", "demolition"} else "同一类规则"))
    print("  要换模式只能回主菜单重新开一局：这个 build 不能用 cvar 改已创建对局的玩法。")
    return 0


def check_map(name):
    """None when the map is worth trying, else the reason to refuse.

    Only maps that appear in maplist/mapcycle or as a loose .bsp are certain;
    everything shipped lives inside pak01_dir.vpk, which this function does not
    open, so an unknown name is a warning and not a refusal."""
    if not MAP_RE.match(name):
        return "不像是地图名（应类似 de_mirage / cs_agency / ar_baggage）"
    if name not in known_maps():
        print("注意：%s 不在 maplist/mapcycle 里，csgo\\maps 下也没有同名 .bsp；"
              "可能只存在于 vpk 中。仍会尝试切图，切完用 status 核对。" % name)
    return None


def cmd_map(args):
    name = args.name
    why = check_map(name)
    if why:
        print("%s。候选：%s" % (why, ", ".join(known_maps()[:24])))
        return 2
    if args.dry_run:
        print("would run: changelevel %s" % name)
        return 0
    print(pipe.send(["changelevel %s" % name, "~status"]).strip())
    print("note: changelevel re-runs listenserver.cfg and gamemode_competitive_offline.cfg,")
    print("      so bots_improved.cfg is re-applied and a fresh gamedll load happens.")
    return 0


def newest_errors_log():
    """SourceMod names its error log by day, so the file to watch changes at
    midnight and after any gap; take the newest one that exists."""
    d = os.path.join(SM, "logs")
    if not os.path.isdir(d):
        return None
    logs = [os.path.join(d, f) for f in os.listdir(d) if re.fullmatch(r"errors_\d{8}\.log", f)]
    return max(logs, key=os.path.getmtime) if logs else None


def _startup_advice(pids):
    """Say the two things that were each expensive to learn, whenever they apply."""
    if pids:
        mods = [x for x in loaded_modules(pids[0]) if any(d in x for d in ADDON_DLLS)]
        if not mods:
            print("\n建议：这个 app 的 Steam 启动选项里只写 -insecure（不要加 +map）。")
            print("      没有 -insecure 时引擎不加载 addons 里的未签名 DLL，皮肤和 bot 强化都不在。")
    print("\n玩法确认：csgotool.py mode    mod 是否进进程：csgotool.py mods")


def cmd_doctor(_args):
    print("%-12s: %s" % ("game", GAME))
    ok = True

    # Mod state comes first and must print even when the pipe is dead: "no
    # plugins answering" and "the engine never loaded the mods" look identical
    # from outside, and only one of them is fixed by -insecure.
    pids = game_pids()
    if pids:
        mods = [x for x in loaded_modules(pids[0]) if any(d in x for d in ADDON_DLLS)]
        print("%-12s: %s" % ("mods", ("PID %s 已加载 %s" % (pids[0], ", ".join(sorted(set(
            m.split(".")[0] for m in mods))))) if mods else
            "PID %s 进程里没有任何 mod DLL（VAC 安全会话会拒绝未签名 DLL；启动选项加 -insecure）"
            % pids[0]))
    else:
        print("%-12s: csgo.exe 没在跑" % "mods")

    # sm_lanpipe_version goes stale across "sm plugins reload": SM frees a
    # reloaded plugin's convars at end of frame, so the new instance's
    # CreateConVar collides with the old one and keeps reporting the previous
    # value. The boot marker written into the log is the truth.
    try:
        probe = pipe.get_cvar("mp_maxrounds")
        hits = []
        if os.path.exists(EXECLOG):
            hits = re.findall(r"lan_pipe ([0-9.]+) loaded",
                              _read_bytes(EXECLOG).decode("utf-8", "replace"))
        print("%-12s: answering (mp_maxrounds=%s), loaded build %s" % ("pipe",
            probe, hits[-1] if hits else "unknown"))
    except Exception as e:
        ok = False
        print("%-12s: NOT ANSWERING (%s)" % ("pipe", e))
        _startup_advice(game_pids())
        return 1

    # kgns exposes no version cvar, so probe one it definitely creates
    for cvar, label in (("sm_botmimic_version", "botmimic"),
                        ("sm_weapons_db_connection", "kgns weapons"),
                        ("sm_gloves_db_connection", "kgns gloves")):
        try:
            print("%-12s: %s = %s" % (label, cvar, pipe.get_cvar(cvar)))
        except Exception:
            print("%-12s: not readable (plugin absent or cvar renamed)" % label)

    if os.path.exists(FIXLOG):
        text = _read_bytes(FIXLOG).decode("utf-8", "replace")
        loaded = re.findall(r"lan_fix ([0-9.]+) loaded", text)
        tail = text.splitlines()[-1:]
        print("%-12s: loaded %s, last line -> %s" % ("lan_fix", loaded[-1] if loaded else "?",
                                             tail[0] if tail else "(empty log)"))
    else:
        print("%-12s: no data/lan_fix.log yet" % "lan_fix")

    errs = newest_errors_log()
    if errs:
        st = os.stat(errs)
        print("%-12s: %s %d bytes, last write %s  (growth after a reload = a plugin failed)" % (
            "errors", os.path.basename(errs), st.st_size,
            time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime))))
    else:
        print("%-12s: no errors log at all (clean)" % "errors")

    rows, _ = _players()
    print("%-12s: %d connected" % ("humans", len([r for r in rows if r["bot"] == "0"])))

    # Advice that has cost hours to rediscover, stated whenever it applies.
    _startup_advice(pids)
    return 0 if ok else 1


# ------------------------------------------------------------------ skins
def db_columns():
    tmp = tempfile.mktemp(suffix=".sq3")
    shutil.copyfile(DB, tmp)
    con = sqlite3.connect(tmp)
    cols = [r[1] for r in con.execute("PRAGMA table_info(weapons)")]
    glove_cols = [r[1] for r in con.execute("PRAGMA table_info(gloves)")]
    rows = list(con.execute("SELECT * FROM weapons"))
    names = [d[0] for d in con.execute("SELECT * FROM weapons LIMIT 1").description]
    grows = list(con.execute("SELECT * FROM gloves"))
    gnames = [d[0] for d in con.execute("SELECT * FROM gloves LIMIT 1").description]
    con.close()
    os.remove(tmp)
    return cols, rows, names, glove_cols, grows, gnames


def skin_classes(cols):
    out = []
    for c in cols:
        if c in ("steamid", "knife"):
            continue
        if c.endswith(("_float", "_trak", "_trak_count", "_tag", "_seed")):
            continue
        out.append(c)
    return out


_CATALOG = None
_GLOVES = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = skins_catalog.load_merged()
    return _CATALOG


def glove_catalog():
    global _GLOVES
    if _GLOVES is None:
        _GLOVES = skins_catalog.load_gloves()
    return _GLOVES


def resolve_paintkit(needle, cls, force=False):
    """Accept a paintkit number or a skin name in either language.

    Returning the entry too lets the caller warn about a skin that was never
    authored for this weapon: the engine applies whatever m_nFallbackPaintKit it
    is told, it just shows a plain model when the id does not belong."""
    if needle is None:
        print("skins set 需要 paintkit 号或皮肤名，例如 skins set awp 血腥运动")
        return None, None
    needle = needle.strip()
    if re.fullmatch(r"-?\d+", needle):
        # a bare number is an id, never a name: "0" (no skin) and "-1" (kgns'
        # random marker) both substring-match skin names like Mainframe 001
        pk = int(needle)
        entry = next((e for e in catalog() if e["paintkit"] == pk), None)
        if entry and cls not in entry["columns"]:
            print("%d (%s) 在皮肤表里只登记给 %s，不适用于 %s。加 --force 可强写，"
                  "但引擎会显示默认皮肤。" % (pk, " / ".join(entry["names"]),
                                   " ".join(entry["columns"]), cls))
            return (pk, entry) if force else (None, None)
        return pk, entry
    hits, how = skins_catalog.find(catalog(), needle, cls)
    if not hits:
        hits, how = skins_catalog.find(catalog(), needle, None)
        if hits and all(cls not in e["columns"] for e in hits):
            # one name can be several paintkits: kgns keys by id, so "血腥运动"
            # is 639 on the ak47, 597 on the scar20 and 696 on the mp7
            print("%r 在皮肤表里是 %d 个不同的 paintkit，但没有一个登记给 %s：" % (
                needle, len(hits), cls))
            for e in hits:
                print("   %6d  %-34s %s" % (e["paintkit"], " / ".join(e["names"]),
                                            " ".join(e["columns"])))
            print("确认要把其中一个硬塞给 %s 就加 --force（引擎不报错，只会渲染成默认皮肤）" % cls)
            return (hits[0]["paintkit"], hits[0]) if (force and len(hits) == 1) else (None, None)
        print("皮肤表里没有 %r（用 skins search 查名字，或 skins list %s 看这个位置能选什么）" % (needle, cls))
        return None, None
    if len(hits) > 1 and how != "exact":
        print("%r 匹配到 %d 个皮肤，请用更完整的名字或 paintkit 号：" % (needle, len(hits)))
        for e in hits[:15]:
            print("  %6d  %-34s %s" % (e["paintkit"], " / ".join(e["names"]), " ".join(e["columns"])))
        return None, None
    return hits[0]["paintkit"], hits[0]


def cmd_skins(args):
    cols, rows, names, gcols, grows, gnames = db_columns()
    classes = skin_classes(cols)
    cat = catalog()

    if args.what == "search":
        if not args.classname:
            print("skins search 需要一个关键词（皮肤名或 paintkit 号）")
            return 2
        needle = args.classname
        hits, how = skins_catalog.find(cat, needle, args.class_col)
        print("%r -> %s 匹配 %d 个武器皮肤%s" % (needle, how, len(hits),
                                       ("（限定 %s）" % args.class_col) if args.class_col else ""))
        for e in hits[:args.limit]:
            print("  %6d  %-34s %s" % (e["paintkit"], " / ".join(e["names"]),
                                       " ".join(e["columns"])))
        if len(hits) > args.limit:
            print("  （还有 %d 个，用 --limit 或 --class 收窄）" % (len(hits) - args.limit))
        if needle.lstrip("-").isdigit():
            gid = int(needle)
            ghits = [g for g in glove_catalog() if gid in (g["paintkit"], g["group_index"])]
            for g in ghits[:args.limit]:
                print("  手套 %6d 组=%d %s / %s  队伍 %s" % (
                    g["paintkit"], g["group_index"], g["group"], g["paint"], ",".join(g["teams"])))
            if ghits:
                print("  （paintkit 号也命中了手套表：上面两行分别是武器与手套的同名号）")
        return 0

    if args.what == "list":
        if args.classname:
            cls = args.classname
            if cls not in classes:
                print("unknown class %r; valid: %s" % (cls, " ".join(classes)))
                return 2
            legal = sorted((e["paintkit"], " / ".join(e["names"])) for e in cat if cls in e["columns"])
            print("%s 可用皮肤 %d 个（paintkit 号 + 中文名/英文名）:" % (cls, len(legal)))
            for pk, nm in legal[:args.limit]:
                print("  %6d  %s" % (pk, nm))
            if len(legal) > args.limit:
                print("  （还有 %d 个，--limit 提高上限）" % (len(legal) - args.limit))
            return 0
        missing = [c for c in classes if not any(c in e["columns"] for e in cat)]
        extra = [c for c in {x for e in cat for x in e["columns"]} if c not in classes]
        print("classes in kgns' table (%d): %s" % (len(classes), " ".join(classes)))
        print("皮肤表核对: cfg 里 53 类，库表里 %d 类；cfg 缺 %s，库表缺 %s" % (
            len({c for e in cat for c in e["columns"]}), missing or "无", extra or "无"))
        for row in rows:
            d = dict(zip(names, row))
            picked = {k: v for k, v in d.items() if k != "steamid" and not k.endswith(("_float", "_trak", "_trak_count", "_tag", "_seed")) and v}
            print("%s  knife-model-col=%s  selections=%s" % (d.get("steamid"), d.get("knife"), picked))
        for row in grows:
            d = dict(zip(gnames, row))
            print("gloves %s -> %s" % (d.get("steamid"), d))
        return 0

    if args.what == "dump":
        target = args.target or _default_target()
        if not target:
            print("no human connected and no --target given; pass a bot name or an index")
            return 2
        pipe.send(["sm_lanfix_dumpfile %s" % target])
        for line in reversed(_read_bytes(FIXLOG).decode("utf-8", "replace").splitlines()):
            if line.startswith("[") and ("target=%s " % target) in line:
                print(line.split("] ", 1)[-1])
                return 0
        print(pipe.send(["sm_lanfix_dumpfile %s" % target]).strip() or "nothing returned")
        return 1

    if args.what == "set":
        cls = args.classname
        if cls not in classes:
            print("unknown class %r; valid: %s" % (cls, " ".join(classes)))
            return 2
        live = args.target or _default_target()
        if not live:
            print("no human connected and no --target given; pass a bot name or an index")
            return 2

        paintkit, entry = resolve_paintkit(args.paintkit, cls, args.force)
        if paintkit is None:
            return 2
        if entry and cls not in entry["columns"] and not args.force:
            print("%d (%s) 在皮肤表里只登记给 %s，不适用于 %s。加 --force 可强写，"
                  "但引擎会显示默认皮肤。" % (paintkit, " / ".join(entry["names"]),
                                    " ".join(entry["columns"]), cls))
            return 2

        line = "sm_lanfix_set %s %s %d %d %d %s" % (
            live, cls, paintkit, args.seed, args.trak, args.wear)
        pipe.send([line])
        # the authoritative reply is lan_fix's own tee, not the pipe echo
        if os.path.exists(FIXLOG):
            tail = _read_bytes(FIXLOG).decode("utf-8", "replace").splitlines()[-1:]
            print(tail[0] if tail else "(no lan_fix.log yet)")
        else:
            print(pipe.send(["?mp_maxrounds"]).strip() or "(no reply -- is the game running?)")
        return 0


# ------------------------------------------------------------------ lan.cfg
def cmd_lan(args):
    text = open(os.path.join(CFG, "lan.cfg"), encoding="latin-1").read()
    overrides = []
    if args.bots is not None:
        overrides.append(("bot_quota", str(args.bots)))
    if args.password is not None:
        overrides.append(("sv_password", '"%s"' % args.password))
    if args.hostname:
        overrides.append(("hostname", '"%s"' % args.hostname))
    if args.cheats:
        overrides.append(("sv_cheats", "1"))
    for name, value in overrides:
        text = re.sub(r"(?m)^(\s*%s\s+).*$" % re.escape(name), r"\g<1>%s" % value, text)
    out = os.path.join(CFG, "lan.cfg")
    shutil.copyfile(out, out + ".bak")
    with open(out, "w", encoding="latin-1", newline="\n") as fh:
        fh.write(text)
    print("wrote %s (previous saved as %s)" % (out, out + ".bak"))
    for name, value in overrides:
        print("  %s = %s" % (name, value))
    if not args.no_exec:
        print(pipe.send(["exec lan.cfg", "~status"]).strip())
    return 0


def cmd_cfg(args):
    if args.action != "gen":
        sys.exit("only 'cfg gen' is implemented")
    base = os.path.join(CFG, "bots_improved.cfg")
    if not os.path.exists(base):
        sys.exit("no base cfg to start from")
    text = open(base, encoding="latin-1").read()
    for pair in args.set or []:
        name, _, value = pair.partition("=")
        if not value:
            sys.exit("--set wants name=value, got %r" % pair)
        text = re.sub(r"(?m)^(\s*%s\s+).*$" % re.escape(name.strip()), r"\g<1>%s" % value.strip(), text)
    out = os.path.join(CFG, "%s.cfg" % args.name)
    with open(out, "w", encoding="latin-1", newline="\n") as fh:
        fh.write(text)
    print("wrote %s" % out)
    missing = [n for n in cfg_names(out) if not any(whole_word(b, n) for b in map(_read_bytes, [p for p in BINS.values() if os.path.exists(p)]))]
    print("name check: %s" % ("all present in this build" if not missing else "MISSING %s" % ", ".join(missing)))
    if not args.no_exec:
        print(pipe.send(["exec %s.cfg" % args.name, "~status"]).strip())
    return 0


# ------------------------------------------------------------------ match control
def _run(cmd):
    p = subprocess.run(cmd, capture_output=True)
    return p.stdout.decode("gbk", "replace") + p.stderr.decode("gbk", "replace")


def local_interfaces():
    """(adapter, address, mask, gateway) from ipconfig. The UDP-connect trick
    only ever returns the default route's address, and this machine has WLAN, a
    wired NIC and Tailscale at once, so the friend needs to be told which one
    to aim at -- and the netmask is what says whether they are reachable at all."""
    out = _run(["ipconfig"])
    res = []
    adapter = ip = mask = gateway = ""
    def flush():
        if ip:
            res.append((adapter or "?", ip, mask, gateway))
    for line in out.splitlines():
        s = line.strip()
        if s.endswith(":") and ("适配器" in s or "Adapter" in s):
            flush()
            adapter = s.split("适配器")[-1].split("Adapter")[-1].strip().rstrip(":")
            ip = mask = gateway = ""
            continue
        m = re.search(r"(IPv4|IP Address).*?(\d{1,3}(?:\.\d{1,3}){3})", s)
        if m:
            ip = m.group(2)
            continue
        m = re.search(r"(子网掩码|Subnet Mask).*?(\d{1,3}(?:\.\d{1,3}){3})", s)
        if m:
            mask = m.group(2)
            continue
        m = re.search(r"(默认网关|Default Gateway).*?(\d{1,3}(?:\.\d{1,3}){3})", s)
        if m:
            gateway = m.group(2)
    flush()
    return res


def net_range(ip, mask):
    """'192.168.16.1 - 192.168.31.254 (192.168.16.0/20)' -- the one number that
    decides whether a friend is on this LAN or one hop away."""
    if not mask:
        return "掩码未知"
    try:
        a = struct.unpack("!I", socket.inet_aton(ip))[0]
        m = struct.unpack("!I", socket.inet_aton(mask))[0]
    except OSError:
        return "掩码无法解析"
    if m == 0:
        return "点对点/虚拟网卡，没有共享网段这回事"
    net, bcast = a & m, (a & m) | (~m & 0xFFFFFFFF)
    bits = bin(m).count("1")
    f = lambda n: socket.inet_ntoa(struct.pack("!I", n))
    return "%s - %s (%s/%d)" % (f(net + 1 if bcast - net > 2 else net), f(bcast - 1 if bcast - net > 2 else bcast), f(net), bits)


def _iface_kind(ip):
    if ip.startswith("169.254."):
        return "链路本地，别人连不上"
    if ip.startswith("100."):
        return "Tailscale 虚拟网卡，只有装了你 tailscale 的人能用"
    if ip.startswith("127."):
        return "本机"
    if ip.startswith(("10.", "192.168.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
        return "局域网可用"
    return "公网地址，别拿去给局域网朋友"


def port_owner(port):
    out = _run(["netstat", "-ano", "-p", "udp"])
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] == "UDP" and f[1].endswith(":%d" % port):
            pid = f[-1]
            # /FO CSV is not the default, and the table form has no quotes to
            # split on, so name lookup silently returned "?" until it was asked
            # for the same shape every time
            listing = _run(["tasklist", "/FO", "CSV", "/FI", "PID eq " + pid])
            for row in listing.splitlines():
                # the header row is quoted too ("映像名称","PID",...) and would
                # otherwise be returned as the process name
                if row.startswith('"') and ('"%s"' % pid) in row:
                    return pid, row.split('"')[1]
            return pid, "?"
    return None, None


def game_process():
    out = _run(["tasklist", "/FO", "CSV", "/FI", "IMAGENAME eq csgo.exe"])
    for line in out.splitlines():
        if line.lower().startswith('"csgo.exe"'):
            f = [x.strip('"') for x in line.split('","')]
            return f[1] if len(f) > 1 else None
    return None


def game_pids():
    out = _run(["tasklist", "/FO", "CSV", "/FI", "IMAGENAME eq csgo.exe"])
    pids = []
    for line in out.splitlines():
        if line.lower().startswith('"csgo.exe"'):
            f = [x.strip('"') for x in line.split('","')]
            if len(f) > 1 and f[1].isdigit():
                pids.append(int(f[1]))
    return pids


def game_dialogs(pids=None):
    """Read modal dialog text straight off the game's windows.

    Launcher failures only ever appear as a MessageBox on screen, and window text
    is public -- so this saves asking anyone to transcribe an error. It finds the
    dialog regardless of whether it came from a game that started or one that
    died at the Steam session check, which is exactly the case where there is no
    console.log to read."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    want = set(pids) if pids else set(game_pids())
    if not want:
        return []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def text(h):
        n = u.GetWindowTextLengthW(h)
        if not n:
            return ""
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        return b.value

    def klass(h):
        b = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, b, 64)
        return b.value

    found = []

    def on_top(h, _l):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value not in want:
            return True
        kids = []

        def on_child(c, _x):
            t = text(c)
            if t:
                kids.append((klass(c), t))
            return True

        u.EnumChildWindows(h, CB(on_child), 0)
        title = text(h)
        cls = klass(h)
        if cls == "#32770" or kids:
            found.append({"pid": pid.value, "class": cls, "title": title, "texts": kids})
        return True

    u.EnumWindows(CB(on_top), 0)
    return found


def cmd_dialog(args):
    pids = [int(args.pid)] if args.pid else None
    rows = game_dialogs(pids)
    print("csgo.exe 进程: %s" % (", ".join(str(p) for p in sorted(set(game_pids()))) or "没有在跑的"))
    dialogs = [r for r in rows if r["class"] == "#32770"]
    windows = [r for r in rows if r["class"] != "#32770"]
    for w in windows:
        print("  PID %s 窗口: 类 %s 标题 %r" % (w["pid"], w["class"], w["title"]))
    if not dialogs:
        print("没有弹窗。有上面那种 Direct3D 窗口标题就说明游戏真的起来了；"
              "只有 #32770 的 Launcher Error 弹窗才是启动失败。")
        return 0
    for r in dialogs:
        print("\nPID %s 弹窗  标题 %r" % (r["pid"], r["title"]))
        for cls, t in r["texts"]:
            if cls != "Button":
                print("  %s" % t.replace("\n", " / "))
    return 0


ADDON_DLLS = ("metamod", "sourcemod", "sourcepawn", "ptah", "dhooks", "sdktools")


def loaded_modules(pid):
    """Module names of a 32-bit game process, read from a 64-bit Python.

    EnumProcessModulesEx with LIST_MODULES_32BIT is the only reliable way: a
    Toolhelp snapshot from a 64-bit caller returns a 32-bit layout (pointer-
    shifted names), and tasklist /M truncates its output. Needs
    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, not the limited-access flag."""
    import ctypes
    from ctypes import wintypes
    ps = ctypes.windll.psapi
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x0400 | 0x0010, False, int(pid))
    if not h:
        return []
    try:
        arr = (wintypes.HMODULE * 2048)()
        got = wintypes.DWORD()
        if not ps.EnumProcessModulesEx(h, ctypes.byref(arr), ctypes.sizeof(arr),
                                       ctypes.byref(got), 0x01):
            return []
        names = []
        buf = ctypes.create_unicode_buffer(260)
        for i in range(got.value // ctypes.sizeof(wintypes.HMODULE)):
            if ps.GetModuleBaseNameW(h, arr[i], buf, 260):
                names.append(buf.value.lower())
        return names
    finally:
        k.CloseHandle(h)


def cmd_mods(args):
    """Ask the process directly whether the mod stack is inside it.

    The pipe can only say "plugins are not answering", which is also what a
    paused game or a dead plugin looks like. Missing DLLs in the module list is
    a different failure: the engine never loaded them, so no reload or map
    change will help."""
    pids = game_pids()
    if not pids:
        print("csgo.exe 没在跑")
        return 2
    for pid in pids:
        m = loaded_modules(pid)
        print("PID %s  模块 %d 个" % (pid, len(m)))
        if not m:
            print("  读不到模块（权限或进程正在退出）")
            continue
        for core in ("engine.dll", "client.dll", "server.dll", "matchmaking.dll"):
            print("  %-14s %s" % (core, "在" if core in m else "不在"))
        missing = [d for d in ADDON_DLLS if not any(d in x for x in m)]
        present = [x for x in m if any(d in x for d in ADDON_DLLS)]
        for x in present:
            print("  mod 已加载   %s" % x)
        if missing and len(missing) == len(ADDON_DLLS):
            print("  mod 一个都没加载：引擎没有把 addons 目录里的 DLL 装进进程。")
            print("  这与 VAC 安全会话互斥——日志里出现 VAC secure mode is activated 时，")
            print("  未签名的 metamod/sourcemod 会被拒绝。修法是在 Steam 启动选项里加 -insecure。")
        elif missing:
            print("  没看到的模块：%s" % ", ".join(missing))
    return 0


def _wait_pipe(seconds=120, probe=3.0):
    print("  正在等服务器上线，最多 %d 秒。游戏没在跑的话它不会来：需要先在游戏主菜单点一次「开始游戏 → 与电脑玩家」。" % seconds)
    deadline = time.time() + seconds
    next_beat = 0.0
    while time.time() < deadline:
        if "mp_maxrounds =" in pipe.send(["?mp_maxrounds"], timeout=probe):
            print("  上线了")
            return True
        if time.time() >= next_beat:
            sys.stdout.write("  已等 %.0fs" % (time.time() - (deadline - seconds)) + chr(13))
            sys.stdout.flush()
            next_beat = time.time() + 5.0
        time.sleep(1.0)
    print()
    return False


def _server_password():
    try:
        v = pipe.get_cvar("sv_password")
        return v if v and v != "0" else None
    except Exception:
        pass
    path = os.path.join(CFG, "lan.cfg")
    if os.path.exists(path):
        m = re.search(r'(?m)^\s*sv_password\s+"?([^"\s]+)"?', open(path, encoding="latin-1").read())
        if m and m.group(1) not in ("0", ""):
            return m.group(1)
    return None


def _targets(port):
    out = []
    for adapter, ip, mask, gateway in local_interfaces():
        if ip.startswith("127."):
            continue
        out.append({"adapter": adapter, "ip": ip, "mask": mask, "gateway": gateway,
                    "kind": _iface_kind(ip), "range": net_range(ip, mask)})
    return out


def best_target(port):
    """The address a friend on the same Wi-Fi should use: a private range with a
    real gateway beats a link-local or virtual one."""
    cands = [t for t in _targets(port) if t["kind"] == "局域网可用"]
    cands.sort(key=lambda t: (not t["gateway"], t["ip"]))
    return cands[0] if cands else None


def join_block(port, password):
    lines = []
    for t in _targets(port):
        addr = "%s:%d" % (t["ip"], port)
        lines.append("  %-20s %s   %s" % (t["adapter"], addr, t["kind"]))
        lines.append("      同网段范围  %s%s" % (t["range"],
                     ("   网关 " + t["gateway"]) if t["gateway"] else "   无网关"))
        lines.append("      控制台   connect %s%s" % (addr, (" " + password) if password else ""))
        # Valve's documented third segment is the password, so a keyed room can
        # still be a one-click link; whether it resolves at all is the open
        # question, since this build answers no A2S and Steam uses that to find
        # the owning app.
        lines.append("      一键     steam://connect/%s%s" % (addr, ("/" + password) if password else ""))
        lines.append("      启动项   " + launch_options(connect_to=addr, password=password or None))
    return lines


def cmd_join(args):
    port = args.port
    pid, name = port_owner(port)
    print("端口 %d 的 UDP 占用: %s%s" % (port, name or "没人监听（游戏没在对局中）",
                                  ("  PID %s" % pid) if pid else ""))
    if not pid:
        print("→ 先用 csgotool.py start <map> 开一局，房间才存在")
    password = _server_password()
    print("房间密码 sv_password = %s" % (password or "无"))
    print("\n可用的连接地址：")
    for line in join_block(port, password):
        print(line)
    best = best_target(port)
    if best:
        print("给朋友的那个地址: %s:%d（网卡 %s）" % (best["ip"], port, best["adapter"]))

    if args.run:
        if not best:
            print("没有可用的局域网地址，无法代执行")
            return 2
        addr = "%s:%d" % (args.ip or best["ip"], port)
        uri = "steam://connect/%s%s" % (addr, ("/" + password) if password else "")
        print("代执行: %s   （本机 Steam 会拉起游戏并直接连进去）" % uri)
        os.startfile(uri)
        print("没反应就是 steam://connect 解析不出来（它要靠 A2S 判断这台服务器属于哪个 app，"
              "而本 build 不应答 A2S）。改走启动项：")
        print("  " + launch_options(connect_to=addr, password=password or None))
        return 0

    print("\n注意：这台 build 不应答 A2S 查询（真跑图的服务器上实测 A2S_INFO/A2S_PLAYER/"
          "A2S_SPLIT 全部超时），所以朋友的服务器列表里看不到这个房间，只能 connect。")
    print("如果朋友连不上，先确认他在上面那个网段里；再考虑防火墙。入站 UDP %d 的放行命令"
          "需要管理员权限，请你自己在管理员终端里执行：" % port)
    print('  netsh advfirewall firewall add rule name="csgo legacy lan" dir=in action=allow '
          'protocol=UDP localport=%d' % port)
    print("（我不会替你改防火墙。也别忘了：不要把 %d 往公网做端口映射。）" % port)

    if args.write_cmd:
        os.makedirs(LAN_DIR, exist_ok=True)
        wrote = []
        for t in _targets(port):
            if t["kind"] != "局域网可用":
                continue
            path = os.path.join(LAN_DIR, "join-%s.cmd" % t["ip"].replace(".", "-"))
            # batch files are read in the OEM codepage, so keep this pure ASCII;
            # newline="\r\n" does the translation, so the text itself stays \n
            nic = t["adapter"].encode("ascii", "backslashreplace").decode("ascii")
            with open(path, "w", encoding="ascii", newline="\r\n") as fh:
                fh.write("@echo off\n")
                fh.write("rem double-click to join the csgo legacy listen server\n")
                fh.write("rem host NIC: %s   generated %s\n" % (nic, time.strftime("%Y-%m-%d %H:%M")))
                fh.write('start "" "steam://connect/%s:%d%s"\n'
                         % (t["ip"], port, ("/" + password) if password else ""))
            wrote.append(path)
            print("已生成给朋友双击的文件: %s" % path)
        if not wrote:
            print("没有局域网地址可写入，跳过生成")
        else:
            print("提示：DHCP 换地址后这个文件就失效了，重跑一次 join --write-cmd 即可")
            if password:
                print("提示：这个文件里写明了房间密码，别发到公网群里")
            print("备选（steam:// 解析不出来时）：让朋友把下面其中一行填进自己 Steam 里这个 app 的")
            print("启动选项，以后每次开游戏都自动进房：")
            for t in _targets(port):
                if t["kind"] != "局域网可用":
                    continue
                print("  " + launch_options(connect_to="%s:%d" % (t["ip"], port),
                                            password=password or None))
    return 0


DUEL_KEEP_OFF = (("mp_overtime_enable", "0"), ("mp_halftime", "0"),
                 ("mp_teams_unbalance_limit", "0"))


def cmd_duel(args):
    """One-shot 1v1 / small-duel setup: round count, per-round wallet refill and
    a fixed loadout, which is what makes 'same gun, same money, best of N'
    scriptable. The round count and timers are plain cvars; the wallet and the
    loadout have to go through lan_fix because the economy has no cvar that
    refills a specific player to a specific amount every round."""
    target = args.target

    if args.show:
        # read only what this call appends, otherwise a loadout abandoned hours
        # ago keeps showing up in the log as if it were still active
        before = os.path.getsize(FIXLOG) if os.path.exists(FIXLOG) else 0
        pipe.send(["sm_lanfix_autoshow"])
        fresh = b""
        if os.path.exists(FIXLOG):
            with open(FIXLOG, "rb") as fh:
                fh.seek(before)
                fresh = fh.read()
        rows = [l.split("] ", 1)[-1] for l in fresh.decode("utf-8", "replace").splitlines()
                if "autoset" in l]
        print("自动发枪/补钱: " + ("; ".join(rows) if rows else "全部关闭"))
        for name in ("mp_maxrounds", "mp_freezetime", "bot_quota", "mp_overtime_enable",
                     "mp_halftime", "mp_teams_unbalance_limit"):
            try:
                print("%-28s = %s" % (name, pipe.get_cvar(name)))
            except Exception:
                print("%-28s = ?" % name)
        return 0

    if args.off:
        pipe.send(["sm_lanfix_auto %s off" % target,
                   "sm_lanfix_strip %s off" % target,
                   "sm_lanfix_money %s off" % target])
        print("已关闭 %s 的自动发枪/清枪/补钱。回合数等 cvar 保持原值，需要的话：set mp_maxrounds 30" % target)
        return 0

    if args.weapon:
        valid = {c for e in catalog() for c in e["columns"]}
        bad = [w for w in args.weapon.split(",") if w.strip() and w.strip() not in valid]
        if bad:
            print("这些武器名皮肤表里没有，可能不是有效实体名：%s（skins list 看全部 53 个）" % ", ".join(bad))
            return 2

    done = []
    if args.rounds is not None:
        done.append(("mp_maxrounds", pipe.set_cvar("mp_maxrounds", str(args.rounds))))
        for name, val in DUEL_KEEP_OFF:
            pipe.set_cvar(name, val)
            done.append((name, val))
    if args.freeze is not None:
        done.append(("mp_freezetime", pipe.set_cvar("mp_freezetime", str(args.freeze))))
    if args.bots is not None:
        done.append(("bot_quota", pipe.set_cvar("bot_quota", str(args.bots))))

    lines = []
    if args.money is not None:
        lines.append("sm_lanfix_money %s %d" % (target, args.money))
    if args.weapon:
        lines.append("sm_lanfix_auto %s %s" % (target, args.weapon))
        lines.append("sm_lanfix_strip %s %s" % (target, "off" if args.no_strip else "on"))
    if lines:
        if args.apply:
            lines.append("sm_lanfix_autoapply %s" % target)
        print(pipe.send(lines).strip())

    for name, value in done:
        print("%-28s = %s" % (name, value))
    st = _status_quiet()
    if st:
        print("当前: %s" % _fmt_status(st))
    print("下一回合出生起生效；加 --apply 立刻对活着的人套一次；duel --off 还原")
    return 0


DIST_DIR = os.path.join(ROOT, "dist")

KIT_README = """CS:GO 局域网房间 —— 朋友侧使用说明

1. 把这个文件夹整个拷给对方（U 盘、QQ、微信都行）。
2. 对方双击「进房.exe」，进去后按 1 就自动开游戏并连上房主。
3. 进房以后想改自己的皮肤，在游戏里聊天框或控制台敲：
      !ws       武器皮肤
      !knife    刀
      !gloves   手套
      !seed     改磨损种子
      !nametag  给枪刻字
      !wslang   菜单切中文/英文
   选好的皮肤按各自的 SteamID 保存，只影响自己，也不会互相覆盖。
4. 连不上的话，按这个顺序查：
   - 和房主是不是同一个网段（房主工具 join 会打印「同网段范围 …」）；
   - 房主的 Windows 防火墙有没有放行入站 UDP 27015；
   - 服务器列表里看不到房间是正常的，这个版本不应答查询，只能用本工具连；
   - 双击那个链接没反应时，按程序里提示的那行自己在控制台敲 connect。
5. 想以后每次开游戏都自动进这个房：Steam 库 → CS:GO Legacy → 右键 属性 → 启动选项，
   填 +connect 房主IP:27015（有密码就接在后面）。房主换地址了要回来改这一行。
6. 你这边也得装 Counter-Strike: Global Offensive (Legacy)（appid 4465480）。
   装 CS2 是连不进这个房的，协议对不上。
7. 这个程序里没有房主权限：不能改 bot、不能改房间参数、不能开图、
   也碰不到房主的任何文件。它只负责记住地址和把游戏拉起来。
"""


def cmd_kit(args):
    """Build the folder you hand to a friend: client exe + room.ini + readme.

    The address is written in at generation time, so the friend never types an
    IP. It goes stale the moment the host's DHCP address changes, which is why
    this prints the address it baked in."""
    port = args.port
    best = best_target(port)
    host = args.ip or (best["ip"] if best else None)
    if not host:
        print("没找到局域网地址。确认无线/有线网卡已连接，或用 --ip 指定")
        return 2
    folder = os.path.join(DIST_DIR, "房间包-" + host)
    os.makedirs(folder, exist_ok=True)

    exe = os.path.join(ROOT, "exe", "csgo-room-client.exe")
    what = None
    if os.path.exists(exe):
        shutil.copyfile(exe, os.path.join(folder, "进房.exe"))
        what = "进房.exe"
    else:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csgoclient.py")
        shutil.copyfile(src, os.path.join(folder, "csgoclient.py"))
        what = "csgoclient.py（需要对方装了 Python；先打包 exe 再发更好）"

    password = _server_password() or ""
    name = ""
    try:
        name = pipe.get_cvar("hostname")
    except Exception:
        pass
    with open(os.path.join(folder, "room.ini"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 房间地址，由房主生成\nhost = %s\nport = %s\npassword = %s\nname = %s\n"
                 % (host, port, password, name))
    with open(os.path.join(folder, "说明.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(KIT_README)

    print("已生成分发包：%s" % folder)
    print("  可执行文件  %s" % what)
    print("  写入地址    %s:%d   密码 %s   房名 %s" % (host, port, password or "无", name or "无"))
    print("  整个文件夹发给对方即可；你的 IP 变了要重新生成一次")
    return 0


def cmd_start(args):
    name = args.map
    if name:
        why = check_map(name)
        if why:
            print("%s。候选：%s" % (why, ", ".join(known_maps()[:24])))
            return 2

    pid = game_process()
    alive = bool(pid) and _quick_pipe()
    if alive:
        plan = "服务器已在跑：直接改配置/切图"
    elif not pid:
        plan = ("游戏没在跑：会用直连方式带命令行启动（-insecure +map %s）" % (name or "默认")
                if args.launch else "游戏没在跑：需要 --launch 才允许我启动")
    elif args.kill:
        plan = "csgo.exe PID %s 在跑但管道不响应：先 kill 再开" % pid
    else:
        plan = "csgo.exe PID %s 在跑但管道不响应：要么关掉 ESC 暂停菜单，要么加 --kill 重开进程" % pid
    print("计划: %s" % plan)
    if args.dry_run:
        print("dry-run，什么都没做")
        return 0
    if not pid and not args.launch:
        print("游戏没在跑。加 --launch 我会直接起 csgo.exe 并把 +map 带进去（实测可以免点菜单开出一局）；"
              "只想让 Steam 自己启动的话用下面的启动选项写法")
        cold_start_hint(name, args.game_type, args.game_mode)
        return 2
    if pid and not alive and not args.kill:
        return 2

    if not alive:
        if pid and args.kill:
            print("结束 csgo.exe PID %s（当前对局会丢）" % pid)
            print(_run(["taskkill", "/PID", pid, "/F"]).strip())
            time.sleep(3.0)
        if args.launch:
            if args.uri:
                print("启动: %s（URI 方式只能到主菜单，参数会被 Steam 丢掉）" % launch_game())
                cold_start_hint(name, args.game_type, args.game_mode)
            else:
                info = launch_direct(name or "de_dust2", args.game_type, args.game_mode,
                                     windowed=args.windowed, condebug=args.condebug)
                print("启动: pid %s" % info["pid"])
                print("  命令行: %s" % info["cmd"])
                print("  steam_appid.txt: %s（%s）" % (info["appid_was"], info["appid"]))
                print("  没有 -insecure 的话引擎不会加载 addons 里的未签名 DLL，皮肤和 bot 都会消失")
                print("  注意：直连进程拿不到 Steam 用户会话（日志里 AuthStatus Failed），")
                print("  键位/灵敏度/视频会看起来像被重置——磁盘上的 config.cfg 没被动过，")
                print("  但要正经玩请从 Steam 启动（把同一串参数写进启动选项，见 cold_start_hint）")

    if not _wait_pipe(seconds=args.wait):
        print("等了 %d 秒管道仍无响应。没服务器就没有插件，也就没有管道：游戏多半还在读图，"
              "或者正停在主菜单等你点 开始游戏 → 与电脑玩家。doctor 看细节" % args.wait)
        return 1
    print("管道在线")

    if args.cfg:
        print(pipe.send(["exec %s" % args.cfg, "~status"]).strip().splitlines()[-1])
    if args.bots is not None:
        print("bot_quota = %s" % pipe.set_cvar("bot_quota", str(args.bots)))

    # A game created by +map at boot keeps the mode the engine defaulted to
    # (console.log: "GameTypes: missing mapgroupsSP entry for game type/mode
    # (custom/custom)") even though game_type/game_mode already read 1/0 -- the
    # deferred + commands land after Host_NewGame. Writing them here and then
    # changing level is what actually produces a competitive match.
    for cvar, want in (("game_type", str(args.game_type)), ("game_mode", str(args.game_mode))):
        now = pipe.set_cvar(cvar, want)
        print("%s = %s  （写入只为保持一致；已创建的对局玩法改不了，看 csgotool.py mode）" % (cvar, now))

    if name:
        pipe.send(["changelevel %s" % name])
        # during a map change OnMapEnd kills the poll timer, so the pipe goes
        # quiet on purpose; wait for OnMapStart to bring it back
        if not _wait_map(name, args.wait):
            print("切图后 %d 秒内没等到 %s 上线，去游戏里看一眼" % (args.wait, name))
            return 1
        print("地图已是 %s" % name)

    if args.now:
        pipe.send(["mp_warmup_end"])
        print("已 mp_warmup_end（正赛计时开始，朋友可以直接进）")

    port = args.port
    password = _server_password()
    st = _status_quiet()
    print("\n当前: %s" % (_fmt_status(st) if st else "读不到"))
    print("加入方式（发给朋友）：")
    for line in join_block(port, password):
        print(line)
    return 0


def _quick_pipe():
    return "mp_maxrounds =" in pipe.send(["?mp_maxrounds"], timeout=2.0)


def _wait_map(want, seconds):
    """A changelevel onto the map you are already standing in is otherwise
    indistinguishable from a changelevel that never happened, so require the
    quiet window first: OnMapEnd kills the pipe's poll timer, OnMapStart brings
    it back. Only an answer after that gap proves the new match is up."""
    print("  正在等新地图加载完，最多 %d 秒（读图期间插件不轮询，属正常）" % seconds)
    deadline = time.time() + seconds
    quiet = False
    while time.time() < deadline:
        st = _status_quiet(2.0)
        if st is None:
            quiet = True
        elif quiet and st["map"] == want:
            return True
        time.sleep(1.0)
    return False


def _status_quiet(timeout=1.5):
    for m in _rows(pipe.send(["~status"], timeout=timeout),
                   r"status map=(\S+) humans=(\d+) bots=(\d+) max=(\d+) "
                   r"timeleft=(-?\d+) freeze=(-?\d+) t=(-?\d+) ct=(-?\d+)"):
        return dict(zip(("map", "humans", "bots", "max", "timeleft", "freeze", "t", "ct"),
                        m.groups()))
    return None


# ------------------------------------------------------------------ interactive
# The menu itself lives in shell_ui.py. It calls back into main() for every
# action, so the shell can never drift from the one-shot CLI.

_DUEL = {}          # the duel form's pending values, filled in by shell_ui


def cmd_shell(args):
    import shell_ui
    shell_ui.session(args)
    return 0


def _prompt_yes(text):
    try:
        return input(text).strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def cmd_bots_patch(args):
    """Flatten the strength lottery and the name gate inside the player's own botprofile.db.

    The engine draws a random profile per bot, so a lobby can straddle ProP and
    Fair; and bot_stuff.smx only takes a bot over when its name is in
    bot_info.json. Both are properties of the db on this machine, so this edits
    that file in place (backup + report under backup\\bots\\) rather than
    shipping anyone's db for them.
    """
    game = GAME
    if args.backups:
        rows = bots_patch.backups()
        if not rows:
            print("没有备份：本机还没跑过 bots patch")
            return 0
        print("%-17s %-7s %6s %6s %6s %10s" % ("时间戳", "档位", "改写", "删块", "换名", "字节"))
        for stamp, rep in rows:
            print("%-17s %-7s %6s %6s %6s %10s" % (
                stamp, rep.get("target"), rep.get("edited"), rep.get("dropped"),
                rep.get("renamed"), format(rep.get("after_bytes", 0), ",")))
        print("\n还原：python tools\\csgotool.py bots patch --revert <时间戳>")
        return 0
    if args.revert is not None:
        try:
            bots_patch.revert(game, args.revert or None, force=args.force)
        except installer.InstallError as e:
            print(e)
            return 1
        print("下一次开局才生效（bot 的名字与数值在生成时定）")
        return 0
    keep = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    try:
        bots_patch.patch(game, tier=args.tier, keep=keep, rename=args.names,
                         dry=args.dry_run)
    except installer.InstallError as e:
        print("停下来：%s" % e)
        return 1
    if not args.dry_run:
        print("\n下一次开局才生效；核对：python tools\\csgotool.py install --audit")
    return 0


AUDIT_CHAINS = (
    # learned from "sm plugins list" error lines, not from docs: a missing link
    # here means the plugin loads and then refuses to do anything
    ("bot_stuff.smx", ("plugins/bot_steamids.smx", "extensions/SteamIDConverter.ext.dll",
                       "data/bot_info.json")),
    ("botmimic.smx", ("plugins/eitems.smx", "extensions/rip.ext.dll",
                      "gamedata/botmimic.games.txt")),
)
MOTW_PS = ("$ErrorActionPreference='SilentlyContinue';"
           "Get-ChildItem -LiteralPath '%s' -Recurse -Filter *.dll -Force |"
           " ForEach-Object { Get-Item -LiteralPath $_.FullName -Stream * } |"
           " Where-Object { $_.Stream -notmatch 'DATA' } |"
           " Measure-Object | Select-Object -ExpandProperty Count")


def _motw_count(addons_dir):
    """How many DLLs still carry the web marker; the engine refuses those."""
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", MOTW_PS % addons_dir],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or b"").decode("gbk", "replace").strip()
    return int(out) if out.isdigit() else None


def _w(text):
    """Display width, so the two columns line up whether the label is Chinese or ASCII."""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in text)


def _pad(text, width):
    return text + " " * max(0, width - _w(text))


def _audit(doc, target):
    """Static coherence check: does the installed stack actually hang together?

    install --check answers "are the bytes upstream's"; this answers the other
    question -- what a fresh user sees when nothing works -- and needs neither a
    running game nor a download cache."""
    sm = os.path.join(target, "addons", "sourcemod")
    problems, advice = [], []
    print("=== 装机自检（游戏不用在跑）===")
    print("  游戏目录 %s（来源 %s）" % (target, gamedir.source()))

    def _row(tag, label, detail):
        print("  [%s] %s %s" % (_pad(tag, 5), _pad(label, 13), detail))

    def flag(label, detail, ok):
        _row("ok" if ok else "!!", label, detail)
        if not ok:
            problems.append("%s %s" % (label, detail))

    def tip(label, detail):
        _row("建议", label, detail)
        advice.append(label)

    for comp in doc["components"]:
        for mark in comp.get("marks", []):
            flag(comp["id"], mark, os.path.isfile(os.path.join(target, *mark.split("/"))))

    want = []
    for c in doc["components"]:
        if c["kind"] in ("loose", "local"):
            key = "path" if c["kind"] == "loose" else "to"
            want += [e[key] for e in c["files"] if e[key].endswith(".smx")]
    miss = [w for w in want if not os.path.isfile(os.path.join(target, *w.split("/")))]
    flag("清单插件", "%d 个 .smx，缺 %s" % (len(want), ", ".join(miss) or "无"), not miss)

    for head, needs in AUDIT_CHAINS:
        hp = os.path.join(target, "addons", "sourcemod", "plugins", head)
        if not os.path.isfile(hp):
            continue          # the missing-file case is already flagged above
        gap = [n for n in needs if not os.path.isfile(os.path.join(sm, *n.split("/")))]
        flag("依赖链", "%s -> %s%s" % (head, " ".join(needs),
                                 "" if not gap else "（缺 %s）" % ", ".join(gap)), not gap)

    for c in installer.plan_cvars(doc, target):
        fix = c["cvar"]
        flag("参数", "%s 应为 %s，现在 %s" % (fix["key"], fix["value"],
                                      c.get("now") or "读不到"), c["action"] == "same")
    for h in installer.plan_hooks(doc, target):
        flag("挂钩", "%s %s" % (h["rel"], "已挂钩" if h["action"] == "same" else h["action"]),
             h["action"] == "same")

    st = bots_patch.status(target)
    if not st.get("ok"):
        flag("bot 数据库", st.get("why", "读不到"), False)
    else:
        odd = " ".join("%s=%d" % kv for kv in sorted(st["hist"].items(), key=lambda kv: -kv[1]))
        flag("bot 数据库", "profile %d 个，档位 %s；命中 bot_info %d/%d" % (
            st["profiles"], odd, st["in_info"], st["profiles"]), True)
        if not st["bot_info"]:
            tip("bot 数据库", "没有 data/bot_info.json：先跑 install（botpack 会装它），"
                             "否则 bot_stuff 一个 bot 都不接管")
        else:
            if st["off_strongest"]:
                tip("强度抽卡", "bots patch 把所有 profile 拉到 %s（先看计划加 --dry-run）；"
                                "现在每局会混进 %d/%d 个弱档" % (st["strongest"],
                                      st["off_strongest"], st["profiles"]))
            if st["in_info"] < st["profiles"]:
                tip("名字未命中", "bots patch --names 把 %d 个名字换成 bot_info.json 里的空闲职业名" %
                                  (st["profiles"] - st["in_info"]))

    recs = os.path.join(sm, "data", "botmimic", "default")
    if os.path.isdir(recs):
        maps = sorted(n[:-4] for n in os.listdir(os.path.join(target, "maps"))
                      if n.startswith("de_") and n.endswith(".bsp")) if os.path.isdir(
            os.path.join(target, "maps")) else []
        have = set(os.listdir(recs))
        no_rec = [m for m in maps if m not in have]
        if no_rec:
            tip("道具路线", "%d 张本地 de_ 图没有 lineup 数据（上游只做了 %d 张）：%s%s" % (
                len(no_rec), len(have), ", ".join(no_rec[:8]), " ..." if len(no_rec) > 8 else ""))
    else:
        flag("道具路线", "没有 data/botmimic/default（botpack 没装？）", False)

    flag("编译", "spcomp.exe %s（改 .sp 需要它）" % ("在" if os.path.isfile(
        os.path.join(sm, "scripting", "spcomp.exe")) else "缺"), os.path.isfile(
        os.path.join(sm, "scripting", "spcomp.exe")))
    dat = os.path.join(sm, "data")
    writable = os.access(dat if os.path.isdir(dat) else sm, os.W_OK)
    flag("管道", "data 目录%s" % ("可写" if writable else "不可写，管道用不了"), writable)
    n = _motw_count(os.path.join(target, "addons"))
    if n is None:
        _row("??", "Web 标记", "读不到（PowerShell 没答），装的时候清过就没事")
    else:
        flag("Web 标记", "%d 个 DLL 带着 Web 标记（>0 会被引擎拒绝）" % n, n == 0)

    _row("人要做", "启动选项", "Steam 里给 csgo legacy 加 -insecure；这不能由脚本代做")
    if problems:
        print("\n%d 处坏了：%s" % (len(problems), "; ".join(p[:70] for p in problems[:6])))
        return 1
    if advice:
        print("\n没坏，但有 %d 条建议（见上）。" % len(advice))
    else:
        print("\n这套栈是自洽的。实时核对：先开一局，再跑 mods 与 doctor。")
    return 0


def cmd_install(args):
    """按 configs/components.json 把第三方插件栈装进游戏目录。"""
    try:
        doc = installer.load_manifest(args.manifest)
    except installer.InstallError as e:
        print(e)
        return 1
    dl = os.path.join(ROOT, "downloads")

    if args.list:
        print("%-9s %-26s %-16s %s" % ("组件", "名称", "版本", "来源 / 许可"))
        for c in doc["components"]:
            src = c.get("url") or (c.get("base", "") + "（逐个文件）") or "本仓库"
            print("%-9s %-26s %-16s %s" % (c["id"], c["title"], c["version"], src))
            print("%-9s %-26s %-16s 许可：%s" % ("", "", "", c["license"]))
        return 0

    target = os.path.normpath(args.into) if args.into else GAME
    if not gamedir.valid(target):
        print("目标目录不像 CS:GO Legacy 的 csgo 内容目录：%s" % target)
        print(gamedir.how_to_fix())
        return 2

    try:
        only = [x for x in (args.only or "").split(",") if x] or None
        skip = [x for x in (args.skip or "").split(",") if x] or None
        comps = installer.select(doc, only, skip)
    except installer.InstallError as e:
        print(e)
        return 1

    if args.check:
        print("核对目标：%s" % target)
        problems = installer.check(doc, target, ROOT, dl, print, comps)
        if problems:
            print("\n不一致 %d 处：" % len(problems))
            for p in problems[:40]:
                print("  " + p)
            if len(problems) > 40:
                print("  ... 另有 %d 处" % (len(problems) - 40))
            print("\n跑 install 就会把这些补回去。")
            return 1
        print("\n全部一致。")
        return 0

    if args.audit:
        return _audit(doc, target)

    consented = [c for c in comps if c.get("consent")]
    if consented:
        print("\n".join(["", "这几个组件的上游仓库没有 LICENSE 文件，也就是没有给你再分发的许可："]))
        for c in consented:
            print("  %s  <- %s @ %s" % (c["id"], c["repo"], c["version"]))
        print("所以本仓库不打包、不改写它们，只按固定 commit 从上游抓；装不装由你确认。")
        if not args.accept_upstream_license:
            if args.dry_run:
                print("（dry-run：跳过这个确认）")
            elif not _prompt_yes("要继续抓取并安装吗？[y/N] "):
                print("已中止。只要第三方 GPL 部分的话：install --skip %s" %
                      ",".join(c["id"] for c in consented))
                return 2

    pids = game_pids()
    if pids and not args.force:
        print("csgo.exe 正在跑（PID %s）。文件被占着，装到一半会留下半个插件栈。" %
              ",".join(str(p) for p in pids))
        print("先完全退出游戏再跑；确认自担风险就加 --force。")
        return 2

    try:
        items, hooks, cvars = installer.build_plan(doc, comps, target, ROOT, dl,
                                                   allow_download=not args.no_fetch,
                                                   say_=print)
    except installer.InstallError as e:
        print("\n停下来：%s" % e)
        print("没有写入任何文件。")
        return 1

    tally, total, shown = installer.summarise(items + hooks + cvars, print)
    sk = installer.skipped(doc, comps)
    if sk:
        print("\n清单里明确不装的：")
        for line in sk:
            print("  - " + line)
    if args.dry_run:
        print("\ndry-run：只算计划，一个字节都没写。")
        return 0
    if not shown:
        print("\n已经是最新，没什么要写的。")
        return 0
    if not args.yes and not _prompt_yes("\n要写入 %d 项（%s 字节），确认？[y/N] " %
                                         (len(shown), format(total, ","))):
        print("已中止，没写任何东西。")
        return 2

    broot = installer.stamp_dir(ROOT)
    try:
        report = installer.apply(items, hooks, cvars, target, ROOT, broot, print)
    except installer.InstallError as e:
        print("写入失败：%s\n备份与现场记录在 %s" % (e, broot))
        return 1
    print("\n完成：新增 %d，替换 %d，挂钩 %d，共 %s 字节。" %
          (len(report["added"]), len(report["replaced"]), len(report["hooked"]),
           format(report["bytes"], ",")))
    print("\n备份：%s" % broot)
    print("\n接下来（照着做完就能玩）：")
    for i, line in enumerate(doc.get("first_run", []), 1):
        print("  %d. %s" % (i, line))
    print("\n要退回去：python tools\\csgotool.py uninstall --revert %s" %
          os.path.basename(broot))
    return 0


def cmd_uninstall(args):
    """Undo one install run, using only that run's own report."""
    base = os.path.join(ROOT, "backup", "install")
    if not os.path.isdir(base):
        print("没有 install 备份目录：%s" % base)
        return 1
    runs = sorted(d for d in os.listdir(base) if os.path.isfile(
        os.path.join(base, d, "report.json")))
    if not runs:
        print("backup\\install 下没有 report.json，没有可还原的运行。")
        return 1
    stamp = args.revert or runs[-1]
    if stamp not in runs:
        print("没有这次运行：%s（可选：%s）" % (stamp, ", ".join(runs[-8:])))
        return 1
    target = os.path.normpath(args.into) if args.into else GAME
    print("还原 %s -> %s" % (stamp, target))
    pids = game_pids()
    if pids and not args.force:
        print("csgo.exe 还在跑（PID %s），先退出。" % ",".join(str(p) for p in pids))
        return 2
    if not args.yes and not _prompt_yes("会覆盖被换掉的文件、删除本次新增的文件，确认？[y/N] "):
        print("已中止。")
        return 2
    try:
        installer.revert(ROOT, target, stamp, print)
    except installer.InstallError as e:
        print(e)
        return 1
    return 0


def cmd_gamedir(args):
    """Show or pin where the game lives. Everything else depends on this."""
    if args.set:
        want = os.path.normpath(os.path.expandvars(args.set))
        if not gamedir.valid(want):
            print("不符合判定标准：%s" % want)
            print("  需要该目录里有 gameinfo.txt，且它的上一级里有 csgo.exe 或 srcds.exe。")
            print("  要装的是 ...\\steamapps\\common\\<安装名>\\csgo 这一层，不是游戏根目录。")
            return 1
        where = gamedir.write_config(want)
        gamedir.reset()
        print("已写入 %s" % where)
        print("现在解析到：%s  (来源 %s)" % gamedir.resolve())
        print("本条命令之后的其它命令会各自重新解析，无需重开终端。")
        return 0
    p, src = gamedir.resolve()
    print("游戏目录：%s" % (p or "（未找到）"))
    print("来源    ：%s" % src)
    print("Steam 库：%s" % (", ".join(gamedir.libraries()) or "（没找到 Steam）"))
    if args.scan:
        print("\n按探测顺序的候选：")
        for c in gamedir.candidates():
            print("  %-4s %s" % ("[OK]" if gamedir.valid(c) else "[--]", c))
    elif p:
        print("\n跑 --scan 看它还扫过哪些库；用 --set \"<目录>\" 固定下来（写进 %s）。"
              % gamedir.config_path())
    if not p:
        print()
        print(gamedir.how_to_fix())
        return 1
    return 0


# ------------------------------------------------------------------ front end
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # double-clicking the exe should land in the menu instead of printing an
        # argparse "cmd is required" error into a window that then closes
        argv = ["shell"]
    p = argparse.ArgumentParser(prog="csgotool", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("who").set_defaults(fn=cmd_who)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    w = sub.add_parser("watch"); w.add_argument("--every", type=float, default=2.0); w.add_argument("--players", action="store_true"); w.set_defaults(fn=cmd_watch)

    b = sub.add_parser("bots", help="bot 数值对照；bots patch 改写了可在这里看效果")
    b.add_argument("name", nargs="?", help="只看对局里这一个 bot")
    b.add_argument("--no-live", action="store_true", help="只看 botprofile.db，不读内存")
    b.add_argument("--detail", action="store_true", help="逐字段列出来源与结构体偏移")
    b.add_argument("--tier", default=None,
                   help="patch: 把所有 profile 的档位改成这个（默认库里最强的一档）")
    b.add_argument("--only", default=None, help="patch: 只保留这些档位的 block，其余删掉（逗号分隔）")
    b.add_argument("--names", action="store_true",
                   help="patch: 把不命中 bot_info.json 的名字换成里面的空闲职业名")
    b.add_argument("--revert", nargs="?", const="", default=None,
                   help="patch: 还原某次改写（不给时间戳就还原最近一次）")
    b.add_argument("--backups", action="store_true", help="patch: 列出改写备份")
    b.add_argument("--dry-run", action="store_true", help="patch: 只算不写")
    b.add_argument("--force", action="store_true",
                   help="patch --revert: 当前文件不是那次的产物也强行还原")
    b.set_defaults(fn=cmd_bots)

    j = sub.add_parser("join"); j.add_argument("--port", type=int, default=DEFAULT_PORT); j.add_argument("--write-cmd", action="store_true", help="在 <仓库>\\lan 生成给朋友双击的 .cmd")
    j.add_argument("--run", action="store_true", help="本机代执行连接（steam://connect，等于双击 .cmd）"); j.add_argument("--ip", default=None, help="--run 时指定地址，默认挑局域网上最合适的那个"); j.set_defaults(fn=cmd_join)

    d = sub.add_parser("duel")
    d.add_argument("--rounds", type=int, help="mp_maxrounds，同时关掉加时和中场")
    d.add_argument("--money", type=int, help="每回合出生补到这么多钱（lan_fix 直接写 m_iAccount）")
    d.add_argument("--weapon", default=None, help="每回合自动发的武器，逗号分隔，如 awp 或 awp,deagle")
    d.add_argument("--freeze", type=int, help="mp_freezetime 秒数"); d.add_argument("--bots", type=int, help="bot_quota，1v1 用 0")
    d.add_argument("--target", default="humans", help="humans/all/bots 或某个玩家名")
    d.add_argument("--no-strip", action="store_true", help="只加枪，不清空原有武器"); d.add_argument("--apply", action="store_true", help="对当前活着的人立刻套一次，不等下一回合")
    d.add_argument("--off", action="store_true", help="关闭自动发枪/清枪/补钱"); d.add_argument("--show", action="store_true", help="看现在的 duel 状态")
    d.set_defaults(fn=cmd_duel)

    dg = sub.add_parser("dialog")
    dg.add_argument("--pid", default=None, help="只看这个 PID 的窗口，默认看全部 csgo.exe")
    dg.set_defaults(fn=cmd_dialog)

    md = sub.add_parser("mods")
    md.set_defaults(fn=cmd_mods)

    mo = sub.add_parser("mode")
    mo.set_defaults(fn=cmd_mode)

    t = sub.add_parser("start"); t.add_argument("map", nargs="?"); t.add_argument("--bots", type=int); t.add_argument("--cfg", default="lan.cfg"); t.add_argument("--now", action="store_true", help="跳过热身，直接开始正赛")
    t.add_argument("--uri", action="store_true", help="用 steam://run 启动（只能到主菜单，参数会被丢弃）")
    t.add_argument("--windowed", action="store_true", help="直连启动时用 1280x720 小窗，不抢你全屏")
    t.add_argument("--condebug", action="store_true", help="直连启动时带 -condebug，事后能读 csgo/console.log")
    t.add_argument("--launch", action="store_true", help="游戏没跑时直连 csgo.exe：带 -insecure 和 +game_type/+game_mode/+map，免点菜单开出一局（实测有效）"); t.add_argument("--kill", action="store_true", help="进程在跑但没服务器时，允许先结束它（当前对局会丢）")
    t.add_argument("--game-type", type=int, default=1, help="1=竞技（引擎 game_type），启动时用"); t.add_argument("--game-mode", type=int, default=0, help="game_mode，竞技经典=0")
    t.add_argument("--wait", type=int, default=180); t.add_argument("--port", type=int, default=DEFAULT_PORT); t.add_argument("--dry-run", action="store_true"); t.set_defaults(fn=cmd_start)

    k2 = sub.add_parser("kit"); k2.add_argument("--ip", default=None, help="写进分发包的地址，默认自动挑局域网那个"); k2.add_argument("--port", type=int, default=DEFAULT_PORT); k2.set_defaults(fn=cmd_kit)

    g = sub.add_parser("get"); g.add_argument("name"); g.set_defaults(fn=cmd_get)

    s = sub.add_parser("set"); s.add_argument("name"); s.add_argument("value", nargs="+"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_set)

    e = sub.add_parser("exec"); e.add_argument("command", nargs="+"); e.set_defaults(fn=cmd_exec)

    m = sub.add_parser("map"); m.add_argument("name"); m.add_argument("--dry-run", action="store_true"); m.set_defaults(fn=cmd_map)

    v = sub.add_parser("verify"); v.add_argument("file"); v.set_defaults(fn=cmd_verify)

    l = sub.add_parser("lan"); l.add_argument("--bots", type=int); l.add_argument("--password"); l.add_argument("--hostname"); l.add_argument("--cheats", action="store_true"); l.add_argument("--no-exec", action="store_true"); l.set_defaults(fn=cmd_lan)

    c = sub.add_parser("cfg"); c.add_argument("action"); c.add_argument("name", nargs="?", default="custom", help="生成的 cfg 文件名（不带 .cfg）"); c.add_argument("--set", action="append"); c.add_argument("--no-exec", action="store_true"); c.set_defaults(fn=cmd_cfg)

    k = sub.add_parser("skins"); k.add_argument("what", choices=["list", "set", "dump", "search"])
    k.add_argument("classname", nargs="?", help="武器类别（set 时）或搜索词（search 时）")
    k.add_argument("paintkit", nargs="?", help="paintkit 号或皮肤名（中英文都行）")
    k.add_argument("--target", default=None); k.add_argument("--seed", type=int, default=-1); k.add_argument("--trak", type=int, default=0); k.add_argument("--wear", default="-1", help="float 0..1; omit to leave the stored float alone")
    k.add_argument("--class", dest="class_col", default=None, help="把 search/list 限定到某个武器类别")
    k.add_argument("--limit", type=int, default=30); k.add_argument("--force", action="store_true", help="允许写入皮肤表里不适用于该武器的 paintkit")
    k.set_defaults(fn=cmd_skins)

    sh = sub.add_parser("shell"); sh.add_argument("--no-header", action="store_true", help="每轮不刷当前对局状态"); sh.set_defaults(fn=cmd_shell)

    gd = sub.add_parser("gamedir", help="查/固定游戏目录（别人 clone 后第一条要跑的命令）")
    gd.add_argument("--set", default=None, help="把游戏目录写进 settings.ini")
    gd.add_argument("--scan", action="store_true", help="列出探测过的每个候选")
    gd.set_defaults(fn=cmd_gamedir)

    ins = sub.add_parser("install", help="按 configs/components.json 下载并安装开源插件栈")
    ins.add_argument("--list", action="store_true", help="只列组件与来源")
    ins.add_argument("--check", action="store_true", help="核对现场与清单的差异，什么都不写")
    ins.add_argument("--audit", action="store_true",
                     help="静态自检：配对、缺件、bot 数据库覆盖、道具路线（不用开游戏）")
    ins.add_argument("--dry-run", action="store_true", help="算出计划并打印，不写盘")
    ins.add_argument("--only", default=None, help="只装这些组件（逗号分隔）")
    ins.add_argument("--skip", default=None, help="跳过这些组件")
    ins.add_argument("--no-fetch", dest="no_fetch", action="store_true",
                     help="只用 downloads 里的缓存，不联网")
    ins.add_argument("--yes", action="store_true", help="看完计划不再问第二次")
    ins.add_argument("--force", action="store_true", help="游戏在跑也照装（会留半个插件栈）")
    ins.add_argument("--into", default=None, help="装到指定目录（试装/校验用），默认游戏目录")
    ins.add_argument("--manifest", default=None, help="换一份组件清单")
    ins.add_argument("--accept-upstream-license", dest="accept_upstream_license",
                     action="store_true", help="确认接受上游没给许可的那批文件由自己抓取")
    ins.set_defaults(fn=cmd_install)

    un = sub.add_parser("uninstall", help="按某一次 install 的 report.json 还原现场")
    un.add_argument("--revert", default=None, help="时间戳目录名，默认最近一次")
    un.add_argument("--into", default=None)
    un.add_argument("--yes", action="store_true")
    un.add_argument("--force", action="store_true")
    un.set_defaults(fn=cmd_uninstall)

    args = p.parse_args(argv)
    if args.cmd in ("gamedir", "install", "uninstall"):
        # these three have to work before the game directory is known
        return args.fn(args) or 0
    if not GAME:
        print(gamedir.how_to_fix())
        return 2
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
