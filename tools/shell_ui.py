#!/usr/bin/env python3
"""Interactive front end for csgotool: cleared screens, paged pickers, forms.

Everything here is a thin layer over the same subcommands the one-shot CLI
runs -- a screen either calls csgotool.main() with a command line or talks to
lan_fix through the pipe -- so the menu and the CLI cannot drift apart. It
lives in its own module only to keep csgotool.py from doubling in size.

The import of csgotool is deferred into session() because csgotool imports
this module, and a module-level circular import would break at load time.
"""
import os
import shlex
import sys
import time

PAGE = 15
_VT = None


# ------------------------------------------------------------------ screen io
def clear():
    """Clear the console with ANSI escapes, enabling virtual-terminal mode once.

    Deliberately not `os.system("cls")`: that spawns cmd.exe which inherits our
    stdin (it can swallow typed menu input), and it costs a process per screen.
    If the terminal refuses VT mode we scroll the menu off the top instead,
    which is ugly but never wrong."""
    global _VT
    if _VT is None:
        _VT = False
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)                      # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                _VT = bool(k32.SetConsoleMode(h, ctypes.c_uint32(mode.value | 0x0004)))
        except Exception:
            _VT = False
    if _VT:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    else:
        sys.stdout.write("\n" * 45)


def head(T, title):
    """Banner for every screen. When there is no server the reason has to be on
    screen: a blank or silent terminal reads as a crash, and the actual answer
    ("the game is at its main menu") is something only the user can fix."""
    clear()
    st = T._status_quiet(1.2)
    print("=== %s ===" % title)
    if st:
        print("  %s" % T._fmt_status(st))
    else:
        print("  没检测到运行中的服务器。可能：游戏没开 / 停在主菜单 / ESC 暂停中 / 正在读图。")
        print("  菜单照常可用；涉及服务器的选项会等几秒后报错，不是程序卡死。")
    print()


def ask(text, default=None, kind=None, lo=None, hi=None, empty_ok=None):
    """Prompt with the current value as the default; Enter keeps it.

    kind/lo/hi decide whether the answer is validated as a number -- without
    that a stray "n" from a cancelled prompt ended up inside the command line
    as `--wear n`. empty_ok controls whether Enter is a legal answer; text
    prompts that mean "leave it alone" must pass it, or Enter loops forever."""
    cast = None
    if kind == "int":
        cast = int
    elif kind == "float":
        cast = float
    elif lo is not None or hi is not None:
        cast = int
    ok_empty = (default is not None) if empty_ok is None else empty_ok
    while True:
        suffix = "" if default is None else "  [%s]" % (default,)
        try:
            raw = input("  %s%s: " % (text, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            if ok_empty or default is not None:
                return default
            print("    这里得有值（或 Ctrl+C 放弃）")
            continue
        if raw in ("-", "00"):
            return None                                 # caller reads None as "clear this"
        if cast is None:
            return raw
        try:
            v = cast(raw)
        except ValueError:
            print("    要%s" % ("整数" if cast is int else "数字"))
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            print("    超出范围 %s..%s" % (lo, hi))
            continue
        return v


def confirm(text):
    try:
        return input("  %s [y/N]: " % text).strip().lower() in ("y", "yes", "是")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def pause(text="按回车返回菜单"):
    try:
        input("\n" + text + " … ")
    except (EOFError, KeyboardInterrupt):
        print()


def run(T, line):
    """Hand a command line straight to the real CLI."""
    try:
        argv = shlex.split(line)
    except ValueError as e:
        print("  引号没配对：%s" % e)
        return
    if not argv:
        return
    try:
        rc = T.main(argv)
        if rc:
            print("(返回码 %s)" % rc)
    except SystemExit as e:
        if e.code not in (0, None):
            print("(命令结束，退出码 %s)" % e.code)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print("  出错：%s: %s" % (type(e).__name__, e))


_MOD = None


def _T():
    """The already-loaded csgotool, not a second copy of it.

    When the tool is started as `python csgotool.py shell` the module's real
    name is __main__, so a plain `import csgotool` here would execute the file
    a second time: two module objects, two _DUEL dicts, two caches, and the
    second run's `sys.stdout = TextIOWrapper(sys.stdout.buffer, ...)` steals
    the buffer out from under the first one, which then closes it on garbage
    collection ("ValueError: I/O operation on closed file")."""
    global _MOD
    if _MOD is None:
        main_mod = sys.modules.get("__main__")
        if main_mod is not None and hasattr(main_mod, "_status_quiet"):
            _MOD = main_mod
        else:
            import csgotool
            _MOD = csgotool
    return _MOD


# ------------------------------------------------------------------- pickers
def pick(title, items, note=""):
    """Paged chooser over anything printable. Returns the chosen item, or None
    when you back out. Typing /word filters, which is the difference between a
    usable 1075-entry skin list and a wall of text."""
    filt, page = "", 0
    items = list(items)
    while True:
        pool = [x for x in items if (not filt or filt in str(x).lower())]
        last = max(0, (max(len(pool), 1) - 1) // PAGE)
        page = min(page, last)
        lo = page * PAGE
        clear()
        print(title)
        if note:
            print("  %s" % note)
        if filt:
            print("  筛选 %r  ·  共 %d 条  （回车清空筛选）" % (filt, len(pool)))
        else:
            print("  共 %d 条" % len(pool))
        print("  " + "-" * 72)
        for n, it in enumerate(pool[lo:lo + PAGE], start=lo + 1):
            print("  %-4d %s" % (n, it))
        if not pool[lo:lo + PAGE]:
            print("     （没有匹配的）")
        print("  " + "-" * 72)
        try:
            raw = input("  序号 / n下页 b上页 / 词筛选 / 回车返回: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            if filt:
                filt, page = "", 0
                continue
            return None
        if raw.startswith("/"):
            filt, page = raw[1:].strip(), 0
            continue
        if raw in ("n", "N"):
            page = min(page + 1, last)
            continue
        if raw in ("b", "B"):
            page = max(0, page - 1)
            continue
        if raw.lstrip("-").isdigit():
            n = int(raw)
            if 1 <= n <= len(pool):
                return pool[n - 1]
            print("  序号超出范围")
            time.sleep(0.6)


def pick_map():
    T = _T()
    maps = T.known_maps()
    got = pick("换到哪张图（列表来自 maplist / mapcycle / csgo\\maps\\*.bsp / 道具库）",
               maps, note="也可以直接敲图名，不在列表里的会警告但仍会尝试")
    if got:
        return got
    try:
        raw = input("  图名（回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return raw or None


def weapon_classes():
    T = _T()
    cols, _rows, _n, _g, _gr, _gn = T.db_columns()
    return T.skin_classes(cols)


def pick_weapons(current):
    """Build the --weapon list by picking from the same 53 names kgns' table
    uses, so what the form sends can never be a typo."""
    T = _T()
    chosen = [w.strip() for w in (current or "").split(",") if w.strip()]
    while True:
        clear()
        print("每回合发的武器（当前： %s ）" % (", ".join(chosen) or "空"))
        print("  1  从列表里加一把")
        print("  2  从列表里去掉一把")
        print("  3  直接输入（逗号分隔，如 awp,deagle）")
        print("  4  清空")
        print("  回车  完成")
        try:
            raw = input("  选哪项: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            break
        if raw == "1":
            got = pick("加一把武器", [c for c in weapon_classes() if c not in chosen])
            if got:
                chosen.append(got)
        elif raw == "2":
            got = pick("去掉一把", chosen)
            if got in chosen:
                chosen.remove(got)
        elif raw == "3":
            try:
                raw2 = input("  逗号分隔: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raw2 = ""
            if raw2:
                chosen = [w.strip() for w in raw2.split(",") if w.strip()]
        elif raw == "4":
            chosen = []
    return ",".join(chosen)


# ------------------------------------------------------------------- screens
def screen_bots(T):
    run(T, "bots")
    if not confirm("看某个 bot 的逐字段对照？"):
        return
    rows, _ = T._players()
    names = [r["name"] for r in rows if r["bot"] == "1"]
    got = pick("选一个 bot（--detail 会列出库里值 / 引擎实测 / 结构体偏移 / 判定）", names)
    if got:
        run(T, 'bots "%s" --detail' % got)
        pause()


def screen_skins(T):
    while True:
        head(T, "皮肤")
        print("  1  搜皮肤（中文/英文/号）")
        print("  2  按枪挑皮肤并应用（列表选，不用记 paintkit 号）")
        print("  3  读回我身上现在的真实皮肤")
        print("  4  读回某个 bot 的皮肤")
        print("  回车  返回菜单")
        try:
            raw = input("  选哪项: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        if raw == "1":
            kw = ""
            try:
                kw = input("  关键词: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
            if kw:
                run(T, 'skins search "%s"' % kw)
            pause()
        elif raw == "2":
            skin_flow(T)
        elif raw == "3":
            run(T, "skins dump")
            pause()
        elif raw == "4":
            rows, _ = T._players()
            got = pick("选一个 bot", [r["name"] for r in rows if r["bot"] == "1"])
            if got:
                run(T, 'skins dump --target "%s"' % got)
                pause()


def skin_flow(T):
    """class -> skin -> numbers -> confirm -> apply. The point is you never
    type a paintkit id or a class name by hand."""
    classes = weapon_classes()
    cls = pick("选武器类别", classes)
    if not cls:
        return
    entries = sorted((e for e in T.catalog() if cls in e["columns"]), key=lambda x: x["paintkit"])
    if not entries:
        print("  皮肤表里没有登记给 %s 的皮肤" % cls)
        pause()
        return
    label = ["%6d  %s" % (e["paintkit"], " / ".join(e["names"])) for e in entries]
    got = pick("%s 可用皮肤 %d 个（选一个）" % (cls, len(entries)), label,
               note="回车返回；也可以 /词 按名字筛")
    if not got:
        return
    pk = int(got.strip().split()[0])
    wear = ask("磨损 float（0..1，回车=不动已存的值）", empty_ok=True, kind="float", lo=0.0, hi=1.0)
    seed = ask("seed（-1=随机）", default=-1, kind="int", lo=-1, hi=999999)
    trak = ask("StatTrak 计数（0=不要）", default=0, kind="int", lo=0, hi=999999)
    clear()
    parts = "skins set %s %d --seed %s --trak %s" % (cls, pk, seed, trak)
    if wear is not None:
        parts += " --wear %s" % wear
    print("将执行：  %s" % parts)
    print("（这会写进 kgns 的 SQLite 表，并改你手上实体的 netprop）")
    if confirm("确认？"):
        run(T, parts)
        pause()


def screen_duel(T):
    """A form over the duel flags: shows what is live now, lets you change each
    value with a number or a picker, then applies once."""
    while True:
        cur = {}
        for name in ("mp_maxrounds", "mp_freezetime", "bot_quota"):
            cur[name] = T._safe_get(name)
        autos = autosettings()
        head(T, "单挑设置（duel）")
        print("  r  回合数 mp_maxrounds          现在 %s" % cur["mp_maxrounds"])
        print("  f  冻结秒数 mp_freezetime        现在 %s" % cur["mp_freezetime"])
        print("  b  bot 数量 bot_quota            现在 %s   （1v1 填 0）" % cur["bot_quota"])
        print("  m  每回合补到多少钱              现在 %s" % (autos.get("money", "未设置")))
        print("  w  每回合自动发的武器            现在 %s" % (autos.get("give", "未设置") or "未设置"))
        print("  s  是否先清空原有武器            现在 %s" % ("清空(留刀)" if autos.get("strip") else "不"))
        print("  t  作用对象                      现在 %s" % (autos.get("who", "humans")))
        print("  a  按上面的值执行（写 cvar + 下发插件 + 立刻套用）")
        print("  x  全部关掉（自动发枪/清枪/补钱）")
        print("  回车  返回菜单")
        try:
            raw = input("  改哪项: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        if raw == "r":
            _remember(T, "rounds", lambda: ask("回合数", default=int(cur["mp_maxrounds"]) if cur["mp_maxrounds"].isdigit() else 10, lo=1, hi=99))
        elif raw == "f":
            _remember(T, "freeze", lambda: ask("冻结秒数", default=5, lo=0, hi=60))
        elif raw == "b":
            _remember(T, "bots", lambda: ask("bot 数量（1v1 填 0）", default=0, lo=0, hi=15))
        elif raw == "m":
            v = ask("每回合补到多少钱（想停掉用 x 全部关闭）", default=4750, kind="int", lo=0, hi=32000)
            T._DUEL["money"] = v
        elif raw == "w":
            T._DUEL["weapon"] = pick_weapons(T._DUEL.get("weapon"))
        elif raw == "s":
            T._DUEL["strip"] = not T._DUEL.get("strip", True)
        elif raw == "t":
            got = pick("作用对象", ["humans（双方真人）", "all（含 bot）", "只我自己", "某个 bot"])
            if got:
                T._DUEL["target"] = {"humans（双方真人）": "humans", "all（含 bot）": "all",
                                     "只我自己": None, "某个 bot": None}[got]
                if T._DUEL["target"] is None:
                    if got.startswith("只我自己"):
                        T._DUEL["target"] = T._default_target() or "humans"
                    else:
                        rows, _ = T._players()
                        n = pick("选一个 bot", [r["name"] for r in rows if r["bot"] == "1"])
                        T._DUEL["target"] = n or "humans"
        elif raw == "a":
            apply_duel(T)
            return
        elif raw == "x":
            run(T, "duel --off")
            T._DUEL.clear()
            pause("已关闭，按回车返回")
            return


def _remember(T, key, fn):
    v = fn()
    if v is not None:
        T._DUEL[key] = v


def autosettings():
    """What lan_fix currently has configured, parsed from its own log."""
    T = _T()
    before = os.path.getsize(T.FIXLOG) if os.path.exists(T.FIXLOG) else 0
    T.pipe.send(["sm_lanfix_autoshow"])
    if not os.path.exists(T.FIXLOG):
        return {}
    with open(T.FIXLOG, "rb") as fh:
        fh.seek(before)
        fresh = fh.read().decode("utf-8", "replace")
    for line in fresh.splitlines():
        if "autoset" not in line:
            continue
        m = dict(__import__("re").findall(r"(name|give|strip|money)=(\S*)", line))
        return {"who": m.get("name"), "give": m.get("give"), "money": m.get("money"),
                "strip": m.get("strip") == "1"}
    return {}


def apply_duel(T):
    d = dict(T._DUEL)
    cmd = ["duel"]
    if d.get("rounds") is not None:
        cmd += ["--rounds", str(d["rounds"])]
    if d.get("freeze") is not None:
        cmd += ["--freeze", str(d["freeze"])]
    if d.get("bots") is not None:
        cmd += ["--bots", str(d["bots"])]
    if d.get("money") is not None:
        cmd += ["--money", str(d["money"])]
    if d.get("weapon"):
        cmd += ["--weapon", d["weapon"]]
        if not d.get("strip", True):
            cmd += ["--no-strip"]
    cmd += ["--target", str(d.get("target") or "humans"), "--apply"]
    clear()
    print("将执行：  csgotool " + " ".join('"%s"' % c if " " in c else c for c in cmd))
    if not confirm("确认下发到服务器？"):
        return
    run(T, " ".join('"%s"' % c if " " in c else c for c in cmd))
    pause("已执行，按回车返回")


def screen_lan(T):
    while True:
        head(T, "联机与加入")
        print("  1  看加入信息（地址 / 同网段范围 / 密码）")
        print("  2  生成给朋友双击的 .cmd")
        print("  3  本机代执行连接（自己开第二个客户端时用）")
        print("  4  设房间密码 sv_password")
        print("  5  设房名 hostname")
        print("  6  设 bot 数量 bot_quota")
        print("  7  应用 lan.cfg（保留现有参数）")
        print("  8  生成分发包给朋友（客户端 exe + 写好地址的 room.ini + 说明）")
        print("  回车  返回菜单")
        try:
            raw = input("  选哪项: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        if raw == "1":
            run(T, "join")
        elif raw == "2":
            run(T, "join --write-cmd")
        elif raw == "3":
            run(T, "join --run")
        elif raw == "4":
            pw = ask("房间密码（回车不改；输 0 清空）", empty_ok=True)
            if pw is not None:
                run(T, 'lan --password "%s"' % pw)
        elif raw == "5":
            nm = ask("房名（回车不改）", empty_ok=True)
            if nm is not None:
                run(T, 'lan --hostname "%s"' % nm)
        elif raw == "6":
            n = ask("bot 数量", default=10, lo=0, hi=15)
            run(T, "set bot_quota %s" % n)
        elif raw == "7":
            run(T, "exec lan.cfg")
        elif raw == "8":
            run(T, "kit")
        pause()


def screen_maps(T):
    got = pick_map()
    if not got:
        return
    clear()
    print("将执行：  start %s --now --launch" % got)
    print("（已经在跑就直接 changelevel 重开一局；没在跑则直连 csgo.exe 带上 -insecure 和 +map，")
    print("  不用点菜单。没有 -insecure 的话引擎不加载 addons，皮肤和 bot 强化都不会在）")
    if not confirm("确认？"):
        return
    run(T, "start %s --now --launch" % got)
    pause()


def screen_setup(T):
    while True:
        head(T, "装机与自检（只动文件，不动对局）")
        print("  1  游戏目录解析到哪了")
        print("  2  要装的第三方组件、来源与许可")
        print("  3  核对现场与清单（什么都不写）")
        print("  4  只看安装计划（什么都不写）")
        print("  5  安装")
        print("  6  只装 GPL 那部分，跳过没有 LICENSE 的 bot 栈")
        print("  7  撤销最近一次安装")
        print("  8  mod DLL 进进程了吗（缺 -insecure 就在这露馅）")
        print("  9  静态自检：这套栈自洽吗、bot 数据库够不够强（不用开游戏）")
        print("  10 bot 强度拉平（只看计划，不写）")
        print("  11 bot 强度拉平 + 换名（写本机 botprofile.db，自动备份）")
        print("  12 查看/还原 botprofile.db 的改写备份")
        print("  回车  返回菜单")
        try:
            raw = input("  选哪项: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        cmd = {"1": "gamedir", "2": "install --list", "3": "install --check",
               "4": "install --dry-run", "5": "install",
               "6": "install --skip botpack", "7": "uninstall", "8": "mods",
               "9": "install --audit", "10": "bots patch --dry-run",
               "11": "bots patch --names", "12": "bots patch --backups"}.get(raw)
        if cmd:
            run(T, cmd)
        pause()


def screen_expert(T):
    while True:
        head(T, "专家模式（任意参数与命令）")
        print("  1  读一个参数")
        print("  2  改一个参数（写完自动读回验证）")
        print("  3  执行任意服务器/插件命令")
        print("  4  常用参数一览（挑一个改）")
        print("  5  以 bots_improved.cfg 为底生成新 cfg")
        print("  6  校验一个 cfg 里的参数名是否存在于本 build")
        print("  回车  返回菜单")
        try:
            raw = input("  选哪项: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not raw:
            return
        if raw == "1":
            n = ask("参数名（回车取消）", empty_ok=True)
            if n:
                run(T, "get %s" % n)
        elif raw == "2":
            n = ask("参数名（回车取消）", empty_ok=True)
            if n:
                v = ask("值（回车取消）", empty_ok=True)
                if v:
                    run(T, "set %s %s" % (n, v))
        elif raw == "3":
            c = ask("命令（回车取消）", empty_ok=True)
            if c:
                run(T, "exec %s" % c)
        elif raw == "4":
            got = pick("挑一个参数", COMMON_CVARS)
            if got:
                run(T, "get %s" % got)
                v = ask("新值（回车不改）", empty_ok=True)
                if v:
                    run(T, "set %s %s" % (got.split(" ")[0], v))
        elif raw == "5":
            nm = ask("新 cfg 文件名（不带 .cfg）", default="custom")
            run(T, "cfg gen %s" % nm)
        elif raw == "6":
            got = pick("选一个 cfg", sorted(os.listdir(T.CFG)))
            if got:
                run(T, "verify %s" % got)
        pause()


COMMON_CVARS = [
    "mp_maxrounds 每队回合数", "mp_freezetime 开局冻结秒", "mp_roundtime 回合分钟",
    "mp_startmoney 经济重置时的钱", "mp_maxmoney 钱包上限", "mp_afterroundmoney 每回合固定奖励",
    "mp_buytime 购买时间", "bot_quota bot 总数", "bot_difficulty 0-3",
    "sv_cheats 调试类命令开关", "mp_overtime_enable 加时", "mp_halftime 中场",
    "mp_warmup_pausetimer 热身暂停", "mp_teams_unbalance_limit 人数差上限",
    "sv_password 房间密码", "hostname 房名",
]


# --------------------------------------------------------------------- loop
MAIN = [
    ("1", "对局与名单", "status + who 一屏"),
    ("2", "盯盘 watch", "比分/人数实时刷新，Ctrl+C 回菜单"),
    ("3", "bot 数值对照", "库里配置 vs 引擎实测"),
    ("4", "皮肤", "搜 / 按枪选 / 应用 / 读回"),
    ("5", "单挑设置", "回合数、补钱、自动发枪、清 bot"),
    ("6", "换图开新局", "从列表挑图"),
    ("7", "联机与加入", "地址、.cmd、密码、房名"),
    ("8", "专家模式", "任意参数与命令、cfg 生成与校验"),
    ("9", "体检 doctor", "管道/插件版本/错误日志"),
    ("10", "装机与自检", "install / 核对 / 静态自检 / bots patch / 撤销"),
]


def session(args):
    T = _T()
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    while True:
        head(T, "CS:GO Legacy 工具箱")
        for key, label, hint in MAIN:
            print("  %-3s %-14s %s" % (key, label, hint))
        print("  0   退出")
        print("  也可以直接敲完整命令，如 set mp_freezetime 5")
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("q", "0", "quit", "exit"):
            return
        hit = next((m for m in MAIN if m[0] == line), None)
        try:
            if hit:
                {"1": lambda: (run(T, "status"), run(T, "who"), pause()),
                 "2": lambda: run(T, "watch --players"),
                 "3": lambda: screen_bots(T),
                 "4": lambda: screen_skins(T),
                 "5": lambda: screen_duel(T),
                 "6": lambda: screen_maps(T),
                 "7": lambda: screen_lan(T),
                 "8": lambda: screen_expert(T),
                 "9": lambda: (run(T, "doctor"), pause()),
                 "10": lambda: screen_setup(T)}[line]()
            else:
                run(T, line)
        except KeyboardInterrupt:
            print("\n(已中断)")
            time.sleep(0.3)
