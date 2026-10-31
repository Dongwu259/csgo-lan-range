"""Install the third-party plugin stack into a CS:GO Legacy game folder.

Everything comes from configs/components.json: pinned version, URL, byte count and
SHA-256 per archive, plus the loose files of the bot pack pinned to a commit.

The order is deliberate -- nothing touches the game folder until every selected
component has been downloaded, size- and hash-checked and staged, and the full
plan (new / replace / identical) has been computed. Replacements are copied into
backup/install/<stamp>/ first and the run is recorded in a report.json, so
install --revert can undo exactly what this did and nothing else.

Integrity uses CRC32 from the archive's own directory (no re-hash of 200 MB) and
downloads only ever go to the hosts the manifest already names.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
import zlib

MANIFEST = "configs/components.json"
ALLOWED_HOSTS = ("github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
                 "cdn.jsdelivr.net")


class InstallError(Exception):
    pass


def root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_manifest(override=None):
    p = override or find_file(MANIFEST)
    if not p:
        raise InstallError("缺少清单 %s（源码树跑法：仓库根；exe 跑法：exe 旁边放一份，"
                           "或打包时 --add-data configs;configs）" % MANIFEST)
    with open(p, "r", encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    if doc.get("schema") != 1:
        raise InstallError("清单 schema=%r 不认识" % doc.get("schema"))
    return doc


def find_file(rel):
    """Locate a repo file: the source tree, next to the exe, or inside the bundle."""
    dirs = [root()]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    for d in dirs:
        p = os.path.join(d, *rel.split("/"))
        if os.path.isfile(p):
            return p
    return None


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def crc32(path):
    c = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            c = zlib.crc32(chunk, c)
    return c & 0xFFFFFFFF


def say(say_, msg):
    (say_ or (lambda t: print(t)))(msg)


# ---------------------------------------------------------------- download
def check_url(url):
    m = re.match(r"^https://([^/]+)/", url)
    if not m or m.group(1) not in ALLOWED_HOSTS:
        raise InstallError("清单里出现不允许的下载主机：%s" % url)
    if "'" in url or '"' in url or " " in url:
        raise InstallError("URL 含可疑字符：%r" % url[:120])
    return url


def alternates(url):
    """raw.githubusercontent is flaky here; github.com/raw and the jsDelivr mirror
    serve the same bytes, and every candidate is checked against the manifest hash."""
    out = [url]
    m = re.match(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.*)$", url)
    if m:
        o, r, ref, p = m.groups()
        out.append("https://github.com/%s/%s/raw/%s/%s" % (o, r, ref, p))
        out.append("https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (o, r, ref, p))
    return out


def _ps_fetch(url, tmp, seconds=900):
    ps = ("$ProgressPreference='SilentlyContinue';"
          "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::SystemDefault;"
          "$ErrorActionPreference='Stop';"
          "Invoke-WebRequest -UseBasicParsing -Uri '%s' -OutFile '%s' -TimeoutSec %d" %
          (url, tmp, seconds))
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def download(url, dst, say_=None, tries=3):
    """Fetch with PowerShell -- curl on this box has no outbound path."""
    check_url(url)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    last = ""
    for attempt in range(1, tries + 1):
        for cand in alternates(url):
            if os.path.exists(tmp):
                os.remove(tmp)
            say(say_, "  下载(第 %d 次) %s" % (attempt, cand))
            r = _ps_fetch(cand, tmp)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dst)
                return dst
            last = r.stderr.decode("gbk", "replace")[:300] if r.stderr else "rc=%d" % r.returncode
            if attempt >= tries:
                break
            time.sleep(3)
    raise InstallError("下载失败：%s\n%s" % (url, last))


def unblock(path, recursive=False):
    """Strip the Mark-of-the-Web: the engine refuses to load DLLs that carry it."""
    args = ["powershell.exe", "-NoProfile", "-Command",
            "Get-ChildItem -LiteralPath '%s' -Recurse -File -Force | Unblock-File" % path
            if recursive else "Unblock-File -LiteralPath '%s'" % path]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- archives
def verify_archive(path, comp):
    size = os.path.getsize(path)
    if size != comp["bytes"]:
        raise InstallError("%s 字节数不对：磁盘 %d，清单 %d" % (comp["id"], size, comp["bytes"]))
    got = sha256(path)
    if got != comp["sha256"]:
        raise InstallError("%s SHA-256 不对\n  清单 %s\n  实际 %s" % (comp["id"], comp["sha256"], got))
    return True


def ensure_release(comp, dl_dir, allow_download=True, say_=None):
    """Return a verified local zip for this component, fetching it if needed."""
    path = os.path.join(dl_dir, comp["cache"])
    if os.path.exists(path):
        try:
            verify_archive(path, comp)
            say(say_, "  缓存命中 %s（%d 字节，SHA-256 相符）" % (comp["cache"], comp["bytes"]))
            return path
        except InstallError as e:
            say(say_, "  缓存不匹配，丢弃后重下：%s" % e)
            os.remove(path)
    if not allow_download:
        raise InstallError("%s 没有可用缓存，且 --no-fetch" % comp["id"])
    download(comp["url"], path, say_)
    verify_archive(path, comp)
    unblock(path)
    return path


def safe_members(zf):
    out = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/") or info.is_dir():
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise InstallError("压缩包里出现不安全路径：%s" % name)
        out.append(info)
    return out


def stage(zip_path, dst_dir, say_=None):
    """Extract (or refresh) the staging tree and return {relpath: ZipInfo}."""
    with zipfile.ZipFile(zip_path) as zf:
        members = safe_members(zf)
        need = []
        for info in members:
            target = os.path.join(dst_dir, *info.filename.split("/"))
            if (not os.path.exists(target) or
                    os.path.getsize(target) != info.file_size or
                    crc32(target) != info.CRC):
                need.append((info, target))
        if need:
            if os.path.isdir(dst_dir):
                shutil.rmtree(dst_dir)
            os.makedirs(dst_dir, exist_ok=True)
            for info, target in need:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            say(say_, "  解压到 staging/：%d 个文件（重下或首次）" % len(members))
        else:
            say(say_, "  staging 已是最新（%d 个文件，按 CRC 复核过）" % len(members))
        return {info.filename.replace("\\", "/"): info for info in members}


# ---------------------------------------------------------------- planning
def _lf(b):
    return b.replace(b"\r\n", b"\n")


def _cmp(src, dst):
    if not os.path.exists(dst):
        return "new", os.path.getsize(src)
    ss = os.path.getsize(src)
    if ss == os.path.getsize(dst) and crc32(src) == crc32(dst):
        return "same", ss
    # KeyValues files are byte-identical to the engine no matter which EOL they
    # carry, and the deployed tree here was once EOL-normalised; treating that as
    # drift would bury the real differences.
    if ss < 8 << 20:
        with open(src, "rb") as a, open(dst, "rb") as b:
            if _lf(a.read()) == _lf(b.read()):
                return "same", ss
    return "replace", ss


def preserve_set(doc):
    return [p.lower() for p in doc.get("preserve", [])]


def exclude_set(doc):
    return {p.lower() for p in doc.get("exclude", [])}


def is_preserved(rel, keep):
    r = rel.lower()
    return any(r == k or (k.endswith("/") and r.startswith(k)) for k in keep)


def plan_release(comp, staged, stage_dir, game, keep=(), ex=()):
    items = []
    for rel, info in staged.items():
        if rel.lower() in ex:
            continue
        sp = os.path.join(stage_dir, *rel.split("/"))
        dp = os.path.join(game, *rel.split("/"))
        action, size = _cmp(sp, dp)
        if action == "replace" and is_preserved(rel, keep):
            action = "keep"
        items.append({"comp": comp["id"], "rel": rel, "src": sp, "dst": dp,
                      "action": action, "bytes": size, "crc": info.CRC})
    return items


def plan_loose(comp, cache_dir, game, allow_download, say_, keep=()):
    items = []
    for ent in comp["files"]:
        rel = ent["path"]
        dst = os.path.join(game, *rel.split("/"))
        local = os.path.join(cache_dir, "loose", comp["id"], *rel.split("/"))
        if not (os.path.exists(local) and sha256(local) == ent["sha256"]):
            if not allow_download:
                raise InstallError("%s 未缓存且 --no-fetch" % rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            download(comp["base"] + rel, local, say_)
            verify_bytes(local, ent)
        action, size = _cmp(local, dst)
        if action == "replace" and is_preserved(rel, keep):
            action = "keep"
        items.append({"comp": comp["id"], "rel": rel, "src": local, "dst": dst,
                      "action": action, "bytes": size, "sha256": ent["sha256"]})
    return items


def verify_bytes(path, ent):
    if os.path.getsize(path) != ent["bytes"]:
        raise InstallError("%s 字节数不对：%d，清单 %d" % (path, os.path.getsize(path), ent["bytes"]))
    if sha256(path) != ent["sha256"]:
        raise InstallError("%s SHA-256 与清单不符" % path)


def plan_local(comp, game, say_, keep=()):
    items = []
    for ent in comp["files"]:
        sp = find_file(ent["from"])
        if not sp:
            raise InstallError("仓库里缺少 %s（清单要求它装到 %s）" % (ent["from"], ent["to"]))
        verify_bytes(sp, ent)
        dp = os.path.join(game, *ent["to"].split("/"))
        action, size = _cmp(sp, dp)
        if action == "replace" and is_preserved(ent["to"], keep):
            action = "keep"
        items.append({"comp": comp["id"], "rel": ent["to"], "src": sp, "dst": dp,
                      "action": action, "bytes": size, "sha256": ent["sha256"]})
    return items


CVLINE = re.compile(r'^(?P<lead>\s*)"(?P<key>[A-Za-z0-9_]+)"(?P<sep>\s+)("(?P<q>[^"]*)"|\S+)'
                    r'(?P<rest>.*)$')


def plan_cvars(doc, game):
    """KeyValues cvar lines the stack needs flipped, e.g. FollowCSGOServerGuidelines."""
    out = []
    for fix in doc.get("cvar_fixes", []):
        path = os.path.join(game, *fix["file"].split("/"))
        entry = {"comp": "cvar", "rel": fix["file"], "dst": path, "cvar": fix,
                 "action": "missing", "fix": fix}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            hit = None
            for i, raw in enumerate(lines):
                m = CVLINE.match(raw)
                if m and m.group("key") == fix["key"]:
                    hit = (i, m)
                    break
            if hit:
                val = (hit[1].group("q") or "").strip()
                entry["action"] = "same" if val == fix["value"] else "edit"
                entry["line"] = hit[0]
                entry["now"] = val
            else:
                entry["action"] = "absent"
        out.append(entry)
    return out


def plan_hooks(doc, game):
    out = []
    for hook in doc.get("hooks", []):
        path = os.path.join(game, *hook["file"].split("/"))
        if not os.path.isfile(path):
            out.append({"comp": "hooks", "rel": hook["file"], "dst": path, "action": "missing"})
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        done = hook["marker"] in body
        out.append({"comp": "hooks", "rel": hook["file"], "dst": path,
                    "action": "same" if done else "append", "hook": hook})
    return out


def summarise(items, say_=None, limit=12):
    tally = {}
    total = 0
    for it in items:
        tally[it["action"]] = tally.get(it["action"], 0) + 1
        if it["action"] in ("new", "replace"):
            total += it.get("bytes", 0)
    say(say_, "计划：%s" % "  ".join("%s=%d" % kv for kv in sorted(tally.items())))
    say(say_, "要写入 %s 字节" % format(total, ","))
    shown = [it for it in items if it["action"] in ("new", "replace", "append", "missing", "edit")]
    for it in shown[:limit]:
        say(say_, "  %-8s %-46s %s" % (it["action"], it["rel"][:46],
                                       format(it.get("bytes", 0), ",")))
    if len(shown) > limit:
        say(say_, "  ... 另有 %d 项要写" % (len(shown) - limit))
    for it in items:
        if it["action"] == "keep":
            say(say_, "  保留     %s（清单里有原版，但这个文件你改过）" % it["rel"])
        elif it["action"] == "absent":
            say(say_, "  找不到键   %s 里没有 %s，得手动加" % (it["rel"], it["cvar"]["key"]))
    return tally, total, shown


# ---------------------------------------------------------------- applying
def stamp_dir(repo, stamp=None):
    return os.path.join(repo, "backup", "install", stamp or time.strftime("%Y%m%d-%H%M%S"))


def apply(items, hooks, cvars, game, repo, backup_root, say_=None):
    os.makedirs(backup_root, exist_ok=True)
    report = {"stamp": os.path.basename(backup_root), "game": game,
              "added": [], "replaced": [], "hooked": [], "cvar_edited": [], "bytes": 0}
    for it in items:
        if it["action"] in ("same", "keep"):
            continue
        dst = it["dst"]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if it["action"] == "replace":
            slot = os.path.join(backup_root, *it["rel"].split("/"))
            os.makedirs(os.path.dirname(slot), exist_ok=True)
            shutil.copy2(dst, slot)
            report["replaced"].append(it["rel"])
        else:
            report["added"].append(it["rel"])
        shutil.copy2(it["src"], dst)
        report["bytes"] += it.get("bytes", 0)
    for hook in hooks:
        if hook["action"] == "append":
            h = hook["hook"]
            slot = os.path.join(backup_root, *hook["rel"].split("/"))
            os.makedirs(os.path.dirname(slot), exist_ok=True)
            shutil.copy2(hook["dst"], slot)
            with open(hook["dst"], "a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n" + "\n".join(h["append"]) + "\n")
            report["hooked"].append(hook["rel"])
        elif hook["action"] == "missing":
            say(say_, "  跳过挂钩：%s 不存在（这份 build 没有它？）" % hook["rel"])
    for c in cvars:
        if c["action"] != "edit":
            continue
        fix, path = c["cvar"], c["dst"]
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        i = c["line"]
        m = CVLINE.match(lines[i])
        lines[i] = '%s"%s"%s"%s"%s' % (m.group("lead"), m.group("key"), m.group("sep"),
                                       fix["value"], m.group("rest"))
        slot = os.path.join(backup_root, *c["rel"].split("/"))
        os.makedirs(os.path.dirname(slot), exist_ok=True)
        shutil.copy2(path, slot)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        report["cvar_edited"].append("%s=%s (原 %s)" % (fix["key"], fix["value"], c["now"]))
        report.setdefault("cvar_paths", []).append(c["rel"])
    # the engine refuses to load DLLs that carry the web marker
    ad = os.path.join(game, "addons")
    if os.path.isdir(ad):
        unblock(ad, recursive=True)
    with open(os.path.join(backup_root, "report.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


def revert(root, game, stamp, say_=None):
    broot = stamp_dir(root, stamp)
    rp = os.path.join(broot, "report.json")
    if not os.path.isfile(rp):
        raise InstallError("没有 %s" % rp)
    with open(rp, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    if os.path.normcase(os.path.normpath(report["game"])) != os.path.normcase(
            os.path.normpath(game)):
        raise InstallError("这份备份的目标是 %s，跟当前游戏目录不一致，不敢还原" % report["game"])
    done = 0
    for rel in report["replaced"] + report.get("cvar_paths", []):
        src = os.path.join(broot, *rel.split("/"))
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(game, *rel.split("/")))
            done += 1
    for rel in report["added"]:
        dst = os.path.join(game, *rel.split("/"))
        if os.path.isfile(dst):
            os.remove(dst)
            done += 1
    for rel in report.get("hooked", []):
        src = os.path.join(broot, *rel.split("/"))
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(game, *rel.split("/")))
            done += 1
    say(say_, "还原了 %d 项（备份仍保留在 %s）" % (done, broot))
    return done


# ---------------------------------------------------------------- top level
def select(doc, only=None, skip=None):
    comps = doc["components"]
    ids = [c["id"] for c in comps]
    out = []
    for c in comps:
        if only and c["id"] not in only:
            continue
        if skip and c["id"] in skip:
            continue
        out.append(c)
    unknown = set(only or []) - set(ids)
    if unknown:
        raise InstallError("清单里没有这些组件：%s" % ", ".join(sorted(unknown)))
    return out


def build_plan(doc, comps, game, repo, dl_dir, allow_download=True, say_=None):
    items, hooks, cvars = [], [], []
    keep = preserve_set(doc)
    ex = exclude_set(doc)
    for comp in comps:
        say(say_, "\n[%s] %s %s" % (comp["id"], comp["title"], comp["version"]))
        say(say_, "  许可：%s" % comp["license"])
        if comp["kind"] == "release":
            zp = ensure_release(comp, dl_dir, allow_download, say_)
            sd = os.path.join(repo, "staging", comp["id"])
            staged = stage(zp, sd, say_)
            items += plan_release(comp, staged, sd, game, keep, ex)
        elif comp["kind"] == "loose":
            items += plan_loose(comp, dl_dir, game, allow_download, say_, keep)
        else:
            items += plan_local(comp, game, say_, keep)
        if comp["kind"] == "local":
            # the cfg hooks and the guideline flip exist to make what we deploy work
            hooks = plan_hooks(doc, game)
            cvars = plan_cvars(doc, game)
    return items, hooks, cvars


def check(doc, game, repo, dl_dir, say_=None, comps=None):
    """Verify what is on disk against the manifest, without writing anything."""
    problems = []
    keep = preserve_set(doc)
    ex = exclude_set(doc)
    for comp in (comps if comps is not None else doc["components"]):
        if comp["kind"] == "release":
            zp = os.path.join(dl_dir, comp["cache"])
            if not os.path.exists(zp):
                say(say_, "%-9s 缓存缺失（装的时候才会下载）" % comp["id"])
                continue
            try:
                verify_archive(zp, comp)
            except InstallError as e:
                problems.append(str(e))
                continue
            with zipfile.ZipFile(zp) as zf:
                members = safe_members(zf)
                bad, eol_only = [], 0
                for info in members:
                    rel = info.filename.replace("\\", "/")
                    if rel.lower() in ex:
                        continue
                    dp = os.path.join(game, *rel.split("/"))
                    if not os.path.exists(dp):
                        bad.append(("缺", rel))
                        continue
                    if os.path.getsize(dp) == info.file_size and crc32(dp) == info.CRC:
                        continue
                    disk = open(dp, "rb").read()
                    if info.file_size < (8 << 20) and _lf(zf.read(info)) == _lf(disk):
                        eol_only += 1
                        continue
                    bad.append((("保留" if is_preserved(rel, keep) else "不同"), rel))
                verdict = "全部一致"
                if bad or eol_only:
                    parts = []
                    if eol_only:
                        parts.append("%d 个只差行尾（引擎不在乎）" % eol_only)
                    kept = [n for k, n in bad if k == "保留"]
                    rest = [(k, n) for k, n in bad if k != "保留"]
                    if kept:
                        parts.append("%d 个你改过、按清单保留" % len(kept))
                    if rest:
                        parts.append("%d 个不一致" % len(rest))
                    verdict = "，".join(parts)
                    bad = rest
                say(say_, "%-9s 归档内 %d 个文件：%s" % (comp["id"], len(members), verdict))
                problems += ["%s %s %s" % (comp["id"], k, n) for k, n in bad]
        else:
            ents = comp["files"]
            to = "to" if comp["kind"] == "local" else "path"
            bad = []
            for ent in ents:
                dp = os.path.join(game, *ent[to].split("/"))
                if not os.path.exists(dp):
                    bad.append(ent[to])
                elif os.path.getsize(dp) != ent["bytes"] or sha256(dp) != ent["sha256"]:
                    if is_preserved(ent[to], keep):
                        say(say_, "  保留 %s（你改过，清单里那份不会覆盖它）" % ent[to])
                    else:
                        bad.append(ent[to] + " (内容不同)")
            say(say_, "%-9s %d 个文件：%s" % (comp["id"], len(ents),
                                             "全部一致" if not bad else "；".join(bad[:6])))
            problems += ["%s %s" % (comp["id"], b) for b in bad]
        for mark in comp.get("marks", []):
            if not os.path.exists(os.path.join(game, *mark.split("/"))):
                problems.append("%s 标志文件缺失 %s" % (comp["id"], mark))
    selected = comps if comps is not None else doc["components"]
    # the hooks and the guideline flip belong to what *we* deploy, so they are
    # only in scope when that component is part of the selection
    if any(c["kind"] == "local" for c in selected):
        for hook in plan_hooks(doc, game):
            say(say_, "%-9s %s: %s" % ("hook", hook["rel"],
                                       "已挂钩" if hook["action"] == "same" else hook["action"]))
            if hook["action"] != "same":
                problems.append("hook %s %s" % (hook["rel"], hook["action"]))
        for c in plan_cvars(doc, game):
            fix = c["cvar"]
            say(say_, "%-9s %s = %s%s" % ("cvar", fix["key"], c["action"],
                                          "" if "why" not in fix else "   （%s）" % fix["why"]))
            if c["action"] != "same":
                problems.append("cvar %s %s" % (fix["key"], c["action"]))
    return problems
