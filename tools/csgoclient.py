#!/usr/bin/env python3
"""CS:GO Legacy 房间客户端 —— 给朋友用，功能被刻意做少。

这个文件不 import pipe，也不 import csgotool：它没有任何通往服务器控制台、
lan_fix 插件、cfg 文件或 SQLite 的路径。所以"客户端不能改 bot 属性、不能改房
间参数、不能生成/执行 cfg、不能开图"不是靠权限检查实现的，而是**这些代码根本
不在里面**——打包成 exe 发给朋友也一样。

它能做的只有四件事：记住房主地址、测一下通不通、用 steam://connect 进房、告
诉你进房后自己在游戏里能敲哪些命令。皮肤是按各人 SteamID 存在房主服务器上的，
所以每个人自己选自己的，互不影响。
"""
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
CONF = os.path.join(HERE, "room.ini")
DEFAULT_PORT = 27015

IN_GAME_HELP = [
    ("!ws", "武器皮肤菜单（也可以按 F1 买枪键打开）"),
    ("!knife", "刀的皮肤菜单（按购买二级刀键也行）"),
    ("!gloves", "手套菜单"),
    ("!seed", "改某把枪的磨损种子 seed"),
    ("!nametag", "给枪刻字（改名牌）"),
    ("!wslang / !gllang", "把菜单切成中文或英文"),
]

FAQ = [
    "进不了房，先确认你和房主在同一个网段。房主工具里 join 会打印类似"
    "「同网段范围 192.168.16.1 - 192.168.31.254」，你的 IP 必须落在这个区间。",
    "在同一个网段还连不上，多半是房主那边的 Windows 防火墙没放行入站 UDP 27015，"
    "让房主在他的管理员终端里加一条放行规则。",
    "服务器列表里看不到这个房间是正常的：这个 build 不应答 A2S 查询，只能直连。"
    "同理，本工具的端口探测也不准——没响应不代表房主没开服，直接按 1 试。",
    "房主设了密码时，本工具会把密码接到链接末尾（steam://connect/地址:端口/密码）。"
    "如果这个链接没反应，就自己在控制台敲一行 connect 地址:端口 密码，"
    "或者把 +connect 那一行填进 Steam 的启动选项，以后开游戏自动进房。",
    "要玩这个房，你装的必须也是 Counter-Strike: Global Offensive (Legacy)"
    "（appid 4465480，同一个 build）。装的是 CS2 连不进来，协议对不上。",
    "想改自己的皮肤：进房以后在游戏控制台/聊天框敲 !ws（详见菜单 4），"
    "选好的皮肤按你的 SteamID 保存，下次进同一个房还在。",
    "皮肤只影响你自己看到的模型，不会改别人的；别人的选择也只属于他们自己。",
]


# ------------------------------------------------------------------ tiny ini
def load_room():
    cfg = {"host": "", "port": str(DEFAULT_PORT), "password": "", "name": ""}
    try:
        with open(CONF, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", ";", "[")):
                    continue
                key, _, val = line.partition("=")
                if key.strip() in cfg:
                    cfg[key.strip()] = val.strip()
    except OSError:
        pass
    return cfg


def save_room(cfg):
    body = ["# 房间地址，由房主生成的分发包带入，也可以在本工具里改",
            "host = %s" % cfg.get("host", ""),
            "port = %s" % cfg.get("port", str(DEFAULT_PORT)),
            "password = %s" % cfg.get("password", ""),
            "name = %s" % cfg.get("name", "")]
    with open(CONF, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(body) + "\n")


# ------------------------------------------------------------------ net probe
def reachable(host, port, timeout=2.0):
    """A TCP connect attempt, kept only as an extra data point.

    This is NOT a reliable "is the room up" test and must not be presented as
    one: the A2S query layer in this build answers nothing, and the game's own
    TCP listener on 27015 comes and goes with its state (it was observed
    listening from the WLAN address at one point and holding nothing later),
    while 127.0.0.1:27015 on the host machine belongs to an unrelated local
    proxy. A failed probe therefore means nothing; a successful one at least
    proves the address is routable."""
    try:
        s = socket.create_connection((host, int(port)), timeout)
        s.close()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def connect_uri(host, port, password=""):
    """Valve's documented forms are connect/IP, connect/IP:PORT and
    connect/IP:PORT/PASSWORD, so a keyed room can still be a one-click link.
    Whether it resolves here is untested: Steam runs a server query to work out
    which app owns that address, and this build answers no A2S at all."""
    uri = "steam://connect/%s:%s" % (host, port)
    if password:
        uri += "/" + password
    return uri


def launch_option(host, port, password=""):
    """The text that goes into Steam 属性 → 启动选项, which the game does receive.

    Same channel the third-party launchers use: the engine defers +connect until
    the main menu is ready, so it lands. Setting it means every start of the game
    auto-joins this room, so it has to be edited when the host's address changes
    and cleared when you want the plain menu back."""
    line = "+connect %s:%s" % (host, port)
    return line + (" " + password if password else "")


def do_connect(cfg):
    host, port = cfg.get("host", ""), cfg.get("port", str(DEFAULT_PORT))
    if not host:
        print("  还没有房主地址。用菜单 3 填，或让房主把他的分发包给你。")
        return
    pw = cfg.get("password", "")
    ok, err = reachable(host, port)
    print("  端口探测：%s（仅供参考，不通也不代表房主没开服）"
          % ("有响应" if ok else "无响应 " + err))
    print("  正在让 Steam 拉起游戏并连进去 …")
    uri = connect_uri(host, port, pw)
    print("  链接: %s" % uri)
    try:
        os.startfile(uri)
    except Exception as e:
        print("  打不开 steam:// 链接：%s" % e)
    print("  没自动进游戏时，自己开控制台敲：connect %s:%s%s"
          % (host, port, (" " + pw) if pw else ""))
    print("  想以后每次开游戏都自动进这个房（朋友端最省事）：")
    print("      Steam 库 → CS:GO Legacy → 右键 属性 → 启动选项，粘贴这一行：")
    print("        " + launch_option(host, port, pw))
    print("      房主地址变了要回来改这一行；不想自动进房就把启动选项清空。")


# ------------------------------------------------------------------ interface
def clear():
    global _VT
    if _VT is None:
        _VT = False
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                _VT = bool(k32.SetConsoleMode(h, ctypes.c_uint32(mode.value | 0x0004)))
        except Exception:
            _VT = False
    sys.stdout.write("\x1b[2J\x1b[H" if _VT else "\n" * 40)
    sys.stdout.flush()


_VT = None


def pause():
    try:
        input("\n按回车返回 … ")
    except (EOFError, KeyboardInterrupt):
        print()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = load_room()
    while True:
        clear()
        print("=== CS:GO 房间客户端 ===")
        print("  房间：%s%s" % (cfg.get("name") or "(未命名)", "  " if cfg.get("name") else ""))
        print("  地址：%s:%s   密码：%s" % (cfg.get("host") or "未设置", cfg.get("port"),
                                     cfg.get("password") or "无"))
        print()
        print("  1  一键进房")
        print("  2  试一下端口有没有响应（仅供参考）")
        print("  3  改房主地址 / 密码")
        print("  4  进房后我自己能敲什么（皮肤菜单等）")
        print("  5  连不上怎么办")
        print("  0  退出")
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if raw in ("0", "q", "quit"):
            return
        if raw == "1":
            do_connect(cfg)
            pause()
        elif raw == "2":
            host = cfg.get("host")
            if not host:
                print("  没有地址")
            else:
                t0 = time.time()
                ok, err = reachable(host, cfg.get("port"))
                print("  %s:%s  %s  (%.0f ms)" % (host, cfg.get("port"),
                      "有响应" if ok else "无响应 " + err, (time.time() - t0) * 1000))
                print("  注意：这个 build 的服务器不应答查询，游戏自己的 TCP 监听也时有时无，")
                print("  所以「无响应」完全不代表房主没开服。要确认就直接按 1 进，或者问房主。")
            pause()
        elif raw == "3":
            try:
                h = input("  房主 IP（回车不改）: ").strip()
                p = input("  端口（回车 %s）: " % cfg.get("port")).strip()
                pw = input("  密码（回车不改，输 - 清空）: ").strip()
                nm = input("  房间名（回车不改）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if h:
                cfg["host"] = h
            if p.isdigit():
                cfg["port"] = p
            if pw:
                cfg["password"] = "" if pw == "-" else pw
            if nm:
                cfg["name"] = nm
            try:
                save_room(cfg)
                print("  已存到 %s" % CONF)
            except OSError as e:
                print("  存不了配置文件：%s" % e)
            pause()
        elif raw == "4":
            print("\n  进房以后，在聊天框或控制台敲这些（都是房主服务器上的 kgns 插件提供的）：")
            for cmd, what in IN_GAME_HELP:
                print("    %-16s %s" % (cmd, what))
            print("\n  选中的皮肤按你的 SteamID 保存，只影响你自己。")
            pause()
        elif raw == "5":
            print()
            for n, line in enumerate(FAQ, 1):
                print("  %d. %s" % (n, line))
            pause()


if __name__ == "__main__":
    main()
