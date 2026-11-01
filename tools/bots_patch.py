"""Rewrite the player's own botprofile.db so bot strength is reproducible.

botprofile.db is not ours to ship -- Valve's original and the dDefinder repack
both stay out of the repository. What can be shipped is the *transform*: which
tier token each profile header gets, and which names come from the bot_info.json
the same installer already put on disk. Verified against both files: the tier is
always the first token of the chain and no profile overrides Skill or
ReactionTime itself, so levelling the strength is a one-line-per-block edit.

Every edit is reversible: the pre-patch file is copied to backup/bots/<stamp>/
next to a report.json, and revert refuses to touch a db it did not write.
"""
import json
import os
import re
import shutil
import time

import bot_profiles as profiles
import gamedir
import installer

# strongest first; the patch uses the first one the file actually defines
LADDER = ("ProP", "Elite", "Expert", "VeryHard", "Hard", "Tough", "Normal", "Fair", "Easy")
# column-0 profile header: indent, template chain, separator, then the name part
HEAD_RE = re.compile(r"^([ \t]*)([A-Za-z0-9_\-\+]+)([ \t]+)(.*)$")
NAMEPART_RE = re.compile(r'^(?:"([^"]*)"|(\S+))(.*)$', re.S)
BARE_OK = re.compile(r"^[\w\-]+$")
# bot_stuff.smx matches these five with case-sensitive strcmp and writes
# 100000 accel / 0.0 reaction for them. --rename never hands one out: 0.0 s is
# not reachable by a human, and the point of the patch is a hard lobby, not a
# perfect one.
TOP_BOTS = ("sh1ro", "ZywOo", "ropz", "donk", "m0NESY")
TOP_LOWER = frozenset(t.lower() for t in TOP_BOTS)


def db_path(game=None):
    return os.path.join(game or gamedir.require(), "botprofile.db")


def info_path(game=None):
    return os.path.join(game or gamedir.require(), "addons", "sourcemod",
                        "data", "bot_info.json")


def backup_root(stamp=None):
    return os.path.join(gamedir.tool_root(), "backup", "bots",
                        stamp or time.strftime("%Y%m%d-%H%M%S"))


def _decode(raw):
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise installer.InstallError("botprofile.db 既不是 utf-8 也不是 cp1252，不敢改写")


def scan(path):
    """-> (lines, blocks, codec). Each block: header line i, End line end, chain, name, tail.

    Raises on a column-0 line nothing recognises: a silently skipped line here
    means a profile went unpatched and the strength lottery stays in the file.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    text, codec = _decode(raw)
    lines = text.splitlines(True)
    blocks, open_block, open_kind = [], None, None
    for i, line in enumerate(lines):
        body = profiles._strip_comment(line)
        s = body.strip()
        if not s:
            continue
        if s == "End":
            if open_kind == "profile":
                open_block["end"] = i
                blocks.append(open_block)
            open_block, open_kind = None, None
            continue
        if line[:1].isspace():
            continue
        if s == "Default":
            open_kind = "other"
            continue
        if s.startswith("Template "):
            open_kind = "other"
            continue
        m = HEAD_RE.match(body)
        n = profiles.HEADER_RE.match(body) if m else None
        if not m or not n:
            raise installer.InstallError("第 %d 行是不认识的顶格块开头：%r" % (i + 1, s))
        open_block = {"i": i, "end": None, "chain": m.group(2),
                      "name": n.group(2) if n.group(2) is not None else n.group(3)}
        open_kind = "profile"
    if open_kind == "profile":
        raise installer.InstallError("最后一个 profile 没有 End 行，文件被截断？")
    return lines, blocks, codec


def _splice(raw, chain=None, name=None):
    """Rewrite the chain and/or the name inside a header line.

    The splice works on the raw line, not the comment-stripped one, so a
    trailing // note and the file's own line endings survive the patch."""
    stripped = raw.rstrip("\r\n")
    eol = raw[len(stripped):]
    m = HEAD_RE.match(stripped)
    if not m:
        raise installer.InstallError("这一行不是 profile 头：%r" % raw)
    lead, cur, sep, rest = m.group(1), m.group(2), m.group(3), m.group(4)
    out_chain = cur if chain is None else chain
    if name is None:
        return lead + out_chain + sep + rest + eol
    if BARE_OK.match(name):
        named = name
    else:
        named = '"%s"' % name
    n = NAMEPART_RE.match(rest)
    return lead + out_chain + sep + named + (n.group(3) if n else "") + eol


def tier_of(block, tiers):
    return next((t for t in block["chain"].split("+") if t in tiers), None)


def swap_tier(chain_text, old, new):
    """Rewrite only the token that actually carries the tier -- tier_of() may
    not return chain[0] on a hand-edited file."""
    parts = chain_text.split("+")
    return "+".join([new if p == old else p for p in parts])


def hist_of(blocks, tiers):
    h = {}
    for b in blocks:
        k = tier_of(b, tiers) or "(无档)"
        h[k] = h.get(k, 0) + 1
    return h


def plan(path, tier=None, keep=None, rename=False, info_file=None, game=None):
    """Compute the transform without writing anything.

    path is the botprofile.db to read; game only picks where bot_info.json
    lives, so a fixture can be planned against a synthetic game folder."""
    lines, blocks, codec = scan(path)
    db = profiles.load(path)
    tiers = db["tiers"]
    if not tiers:
        raise installer.InstallError("这份 botprofile.db 里没有带 Skill 的 Template，拉不了档位")
    target = tier or next((t for t in LADDER if t in tiers), tiers[0])
    if tier and tier not in tiers:
        raise installer.InstallError("这份 db 没有档位 %s；可选：%s" % (tier, " ".join(tiers)))
    keep_set = set(keep) if keep else None
    for k in (keep_set or []):
        if k not in tiers:
            raise installer.InstallError("--only 里有未知档位 %s；可选：%s" % (k, " ".join(tiers)))

    covered = {}
    ip = info_file or info_path(game)
    if os.path.isfile(ip):
        with open(ip, encoding="utf-8-sig") as fh:
            # verbatim spelling first: bot_stuff compares some of these with
            # case-sensitive strcmp, so a lower-cased rewrite would silently
            # lose the very profiles the rename is meant to hand over
            for k in json.load(fh):
                covered.setdefault(k.lower(), k)
    elif rename:
        raise installer.InstallError("要用 bot_info.json 换名字，但 %s 不存在；"
                                    "先跑 install（botpack 会装它）" % ip)

    kept = [b for b in blocks if keep_set is None or tier_of(b, tiers) in keep_set]
    if keep_set is not None and blocks and not kept:
        raise installer.InstallError("档位 %s 里一个 profile 都没有，照这个 --only 下去会把名字池清空；"
                                     "可选：%s" % (",".join(sorted(keep_set)), _hist_str(
                                         hist_of(blocks, tiers))))
    used = set(b["name"].lower() for b in kept)
    pool = [covered[k] for k in sorted(covered)
            if k not in used and k not in TOP_LOWER] if rename else []

    edits, drops, renamed, still_out = [], [], [], []
    tier_after, hits = {}, 0
    hist_before = hist_of(blocks, tiers)
    for b in blocks:
        t = tier_of(b, tiers)
        if keep_set is not None and t not in keep_set:
            drops.append({"range": [b["i"], b["end"]], "head": lines[b["i"]],
                          "name": b["name"], "tier": t})
            continue
        name = b["name"]
        if rename:
            want = covered.get(name.lower())
            if want is None:
                if pool:
                    name = pool.pop(0)
                    renamed.append({"line": b["i"] + 1, "was": b["name"], "now": name})
                else:
                    still_out.append(name)
            elif name != want:
                # case-insensitively present but spelled differently: correct it,
                # because a strcmp-shaped lookup would leave this bot unhandled
                name = want
                renamed.append({"line": b["i"] + 1, "was": b["name"], "now": name,
                                "why": "拼写对齐"})
        chain = b["chain"] if t == target else swap_tier(b["chain"], t, target)
        tier_after[b["i"]] = target if chain != b["chain"] else (t or "(无档)")
        if name.lower() in covered:
            hits += 1
        if chain != b["chain"] or name != b["name"]:
            raw = lines[b["i"]]
            edits.append({"i": b["i"], "old": raw,
                          "new": _splice(raw, chain=chain,
                                         name=None if name == b["name"] else name),
                          "name": b["name"], "was_tier": t, "tier": target})

    hist_after = {}
    for b in kept:
        k = tier_after[b["i"]]
        hist_after[k] = hist_after.get(k, 0) + 1

    want = db["templates"].get(target, {})
    return {"target": target, "tiers": tiers, "template": want,
            "edits": edits, "drops": drops, "renamed": renamed,
            "still_out": still_out, "blocks": len(blocks), "kept": len(kept),
            "hist_before": hist_before, "hist_after": hist_after,
            "codec": codec, "lines": len(lines), "pool": len(pool),
            "covered": len(covered), "in_info": hits}


def _hist_str(h):
    return "  ".join("%s=%d" % kv for kv in sorted(h.items(), key=lambda kv: -kv[1]))


def patch(game, tier=None, keep=None, rename=False, dry=False, say_=None):
    """Plan and write the transform in one call. Returns the report (None if nothing to do).

    plan() and the write re-scan the same file and compare every line it is
    about to touch, so a botprofile.db edited in another window between the two
    cannot be patched by stale line numbers."""
    say = installer.say
    path = db_path(game)
    before = installer.sha256(path)
    p = plan(path, tier=tier, keep=keep, rename=rename, game=game)
    if not p["edits"] and not p["drops"]:
        say(say_, "没有要改的：全部 %d 个 profile 已经是档位 %s，接管覆盖 %d/%d" % (
            p["blocks"], p["target"], p["in_info"], p["kept"]))
        return None
    say(say_, "档位 %s：Skill=%s ReactionTime=%s AimFocusInitial=%s" % (
        p["target"], p["template"].get("Skill"), p["template"].get("ReactionTime"),
        p["template"].get("AimFocusInitial")))
    say(say_, "改 %d 个 profile 头，删 %d 个 block，换 %d 个名字" % (
        len(p["edits"]), len(p["drops"]), len(p["renamed"])))
    say(say_, "档位分布：%s  ->  %s" % (_hist_str(p["hist_before"]), _hist_str(p["hist_after"])))
    say(say_, "接管覆盖：%d / %d 个 profile 的名字在 bot_info.json 里"
              "（不在的不会被 bot_stuff 改写，只吃库里的档位数值）" % (p["in_info"], p["kept"]))
    for e in p["edits"][:6]:
        say(say_, "  L%-6d %-44s -> %s" % (e["i"] + 1, e["old"].rstrip(), e["new"].rstrip()))
    if len(p["edits"]) > 6:
        say(say_, "  ... 另有 %d 行同类改写" % (len(p["edits"]) - 6))
    for r in p["renamed"][:6]:
        say(say_, "  换名 L%-6d %-14s -> %s" % (r["line"], r["was"], r["now"]))
    if len(p["renamed"]) > 6:
        say(say_, "  ... 另有 %d 个换名" % (len(p["renamed"]) - 6))
    if p["still_out"]:
        say(say_, "  %d 个名字不在 bot_info.json 里而且没有空闲职业名可用（插件不会接管它们）：%s%s" % (
            len(p["still_out"]), " ".join(p["still_out"][:8]), " ..." if len(p["still_out"]) > 8 else ""))
    if dry:
        say(say_, "（dry-run：没写任何东西）")
        return None

    lines, _, codec = scan(path)
    stale = [e for e in p["edits"] if lines[e["i"]] != e["old"]]
    stale += [d for d in p["drops"] if lines[d["range"][0]] != d["head"]
              or lines[d["range"][1]].strip() != "End"]
    if stale:
        raise installer.InstallError("botprofile.db 在计划之后被改动过（%d 处行号对不上），"
                                     "重新跑一次" % len(stale))
    drop = set()
    for d in p["drops"]:
        drop.update(range(d["range"][0], d["range"][1] + 1))
    by_line = dict((e["i"], e["new"]) for e in p["edits"])
    out = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        out.append(by_line.get(i, line))
    data = "".join(out).encode(codec, errors="strict")

    broot = backup_root()
    os.makedirs(broot, exist_ok=True)
    shutil.copy2(path, os.path.join(broot, "botprofile.db"))
    slot = os.path.join(broot, "bot_info.json")
    ip = info_path(game)
    if os.path.isfile(ip):
        shutil.copy2(ip, slot)
    tmp = path + ".patch.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    if installer.sha256(tmp) == before:
        os.remove(tmp)
        say(say_, "内容与原文件一致，没写")
        return None
    try:
        check = profiles.load(tmp)
    except Exception:
        os.remove(tmp)
        raise installer.InstallError("改写后的文件自己都读不回来，已放弃写入：%s" % tmp)
    os.replace(tmp, path)
    report = {"stamp": os.path.basename(broot), "game": game, "db": "botprofile.db",
              "target": p["target"], "before_sha256": before,
              "after_sha256": installer.sha256(path),
              "before_bytes": os.stat(os.path.join(broot, "botprofile.db")).st_size,
              "after_bytes": os.path.getsize(path), "profiles_before": p["blocks"],
              "profiles_after": check["count"], "edited": len(p["edits"]),
              "dropped": len(p["drops"]), "renamed": len(p["renamed"])}
    with open(os.path.join(broot, "report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    say(say_, "已写入 %s（%s -> %s 字节），原版备份在 %s" % (
        path, format(report["before_bytes"], ","), format(report["after_bytes"], ","), broot))
    say(say_, "profile 数 %d -> %d；还原：csgotool.py bots patch --revert %s" % (
        report["profiles_before"], report["profiles_after"], report["stamp"]))
    return report


def revert(game, stamp=None, force=False, say_=None):
    say = installer.say
    base = os.path.join(gamedir.tool_root(), "backup", "bots")
    if not os.path.isdir(base):
        raise installer.InstallError("没有 %s，本机没跑过 bots patch" % base)
    stamps = sorted(d for d in os.listdir(base) if os.path.isfile(
        os.path.join(base, d, "report.json")))
    if not stamps:
        raise installer.InstallError("%s 里没有可还原的 report.json" % base)
    stamp = stamp or stamps[-1]
    broot = os.path.join(base, stamp)
    rp = os.path.join(broot, "report.json")
    if not os.path.isfile(rp):
        raise installer.InstallError("没有 %s（可用：%s）" % (rp, " ".join(stamps)))
    with open(rp, encoding="utf-8") as fh:
        report = json.load(fh)
    if os.path.normcase(os.path.normpath(report["game"])) != os.path.normcase(
            os.path.normpath(game)):
        raise installer.InstallError("这份备份的目标是 %s，跟当前游戏目录不一致，不敢还原" % report["game"])
    src = os.path.join(broot, "botprofile.db")
    path = db_path(game)
    if not os.path.isfile(src):
        raise installer.InstallError("备份里没有 %s" % src)
    now = installer.sha256(path) if os.path.isfile(path) else None
    if now and not force and now != report.get("after_sha256"):
        raise installer.InstallError("当前 botprofile.db 与这份补丁的产物不一致（你自己改过或后来重打过），"
                                     "要强行还原加 --force")
    shutil.copy2(src, path)
    say(say_, "已还原 %s（来自 %s），现在 sha256 %s" % (path, stamp, installer.sha256(path)))
    return report


def backups():
    base = os.path.join(gamedir.tool_root(), "backup", "bots")
    if not os.path.isdir(base):
        return []
    out = []
    for d in sorted(os.listdir(base)):
        rp = os.path.join(base, d, "report.json")
        if os.path.isfile(rp):
            with open(rp, encoding="utf-8") as fh:
                out.append((d, json.load(fh)))
    return out


def status(game):
    """Read-only view for install --audit: is this db usable as shipped?"""
    path = db_path(game)
    if not os.path.isfile(path):
        return {"ok": False, "why": "没有 botprofile.db"}
    db = profiles.load(path)
    _, blocks, _ = scan(path)
    tiers = db["tiers"]
    ip = info_path(game)
    covered = set()
    if os.path.isfile(ip):
        with open(ip, encoding="utf-8") as fh:
            covered = set(k.lower() for k in json.load(fh))
    hist = {}
    hits = 0
    for b in blocks:
        t = tier_of(b, tiers) or "(无档)"
        hist[t] = hist.get(t, 0) + 1
        if b["name"].lower() in covered:
            hits += 1
    top = next((t for t in LADDER if t in hist), None)
    weak = sum(v for k, v in hist.items() if k != top)
    return {"ok": True, "profiles": db["count"], "tiers": tiers, "hist": hist,
            "bot_info": len(covered), "in_info": hits, "strongest": top,
            "off_strongest": weak}
