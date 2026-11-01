"""Scan the working tree and every git object for identifiers that must not go public.

Run: python tools/secret_scan.py [--all-objects]
Exit code 1 when a hit is found, so it can gate a release.

The blocklist of *your own* values lives in .secrets.json at the repo root, which
is gitignored: a scanner that carried real SteamIDs, IPs and e-mail addresses in
its own source would leak exactly what it exists to catch. The generic patterns
below are format-based (token shapes, MACs, account-id paths) and stay in code.
"""
import io
import json
import os
import re
import subprocess
import sys

if not getattr(sys.stdout, "_wrapped", False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stdout._wrapped = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENT_FILE = ".secrets.json"

PATTERN = {
    "steamid64": re.compile(r"\b7656119[0-9]{10}\b"),
    # the same account in the STEAM_1:x:n form that kgns' tables and this tool
    # itself print, plus the [U:1:account] form
    "steamid2": re.compile(r"\bSTEAM_[0-1]:[0-2]:[0-9]{6,}\b"),
    "steamid32": re.compile(r"\[U:1:[0-9]{6,}\]"),
    "rfc1918": re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)(?:\.\d{1,3}){2,3}\b"),
    "mac_addr": re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "gh_token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "discord_hook": re.compile(r"discordapp\.com/api/webhooks/\d+/[0-9A-Za-z_\-]+"),
    "bearer": re.compile(r"(?i)\b(?:bearer|authorization)\s*[:=]\s*[0-9A-Za-z._\-]{16,}"),
    "cookie": re.compile(r"(?i)set-cookie:\s*\S{16,}"),
}

# Examples in docs are not leaks; these are the only rfc1918-shaped strings the
# README is allowed to carry. Anything else with that shape is a real address.
IP_ALLOW = ("192.168.16.1", "192.168.31.254", "192.168.16.0", "192.168.16", "192.168.1")

TEXT_EXT = (".py", ".md", ".txt", ".cfg", ".cmd", ".ps1", ".sp", ".js", ".ini",
            ".games.txt", ".yaml", ".json", ".vdf", ".manifest")
BINARY_EXT = (".smx", ".dll", ".zip", ".sq3", ".mdmp", ".db", ".tuned",
              ".orig", ".bak", ".upstream", ".pyc", ".mmdb")


def load_literals():
    p = os.path.join(ROOT, IDENT_FILE)
    if not os.path.isfile(p):
        sys.exit("缺少 %s\n  复制 secrets.example.json 为 %s，把里面的值换成你自己的"
                 "（SteamID64、局域网 IP、主机名、提交邮箱等）。\n  这个文件已被 .gitignore 排除，不会入库。"
                 % (IDENT_FILE, IDENT_FILE))
    with open(p, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    out = {}
    for k, v in (data.get("literals") or {}).items():
        if not (isinstance(v, str) and v.strip()):
            continue
        v = v.strip()
        if k == "win_user":
            # bare digits show up in byte sizes, so match it only as an account path
            PATTERN["win_user_path"] = re.compile(r"(?i)(?:users|userdata|home)[\\/]+%s\b"
                                                  % re.escape(v))
            continue
        out[k] = v
    return out


def is_text(name, blob):
    if name.endswith(BINARY_EXT):
        return False
    return b"\x00" not in blob[:4096]


def git_base():
    # the repo may be owned by a different account than the shell running this,
    # so name *this* directory as trusted instead of touching global config.
    # quotepath=false is not cosmetic: with the default C-quoting, a Chinese file
    # name comes back as "docs/\346\225\205..." and every one of them is then
    # reported as "tracked but not on disk".
    return ["git", "-c", "safe.directory=" + ROOT.replace(os.sep, "/"),
            "-c", "core.quotepath=false"]


def git(*args):
    return subprocess.run(git_base() + list(args),
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def tracked_files():
    out = git("ls-files").stdout.decode("utf-8", "replace")
    return [l for l in out.splitlines() if l.strip()]


def sweep(label, where, blob, literals, hits, as_text=None):
    """Literal + format scan of one blob; `where` says which artefact it came from."""
    if as_text is None:
        as_text = is_text(where, blob)
    if not as_text:
        for key, val in literals.items():
            if val.encode() in blob:
                hits.append((key, label, where, 0, val))
        return
    text = blob.decode("utf-8", "replace")
    for i, line in enumerate(text.splitlines(), 1):
        for key, val in literals.items():
            if val in line:
                hits.append((key, label, where, i, line.strip()[:160]))
        for key, rx in PATTERN.items():
            m = rx.search(line)
            if m and not (key == "rfc1918" and any(a in line for a in IP_ALLOW)):
                hits.append((key, label, where, i, line.strip()[:160]))


def main():
    literals = load_literals()
    hits = []
    for rel in tracked_files():
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            hits.append(("missing", "worktree", rel, 0, "tracked but not on disk"))
            continue
        with open(p, "rb") as fh:
            sweep("worktree", rel, fh.read(), literals, hits)

    if "--all-objects" in sys.argv:
        # every object ever written, reachable or not: an amended commit leaves
        # the old commit object (with its e-mail) behind until gc prunes it
        proc = subprocess.run(
            git_base() + ["cat-file", "--batch-all-objects",
                          "--batch-check=%(objectname) %(objecttype)"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        objs = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] in ("blob", "commit"):
                objs.append((parts[0], parts[1]))
        print("objects: %d blob/commit" % len(objs))
        for sha, typ in objs:
            blob = git("cat-file", typ, sha).stdout
            sweep("history", sha[:12], blob, literals, hits, as_text=(typ == "commit"))

    # commit metadata is where the owner's e-mail hides, not in the tree
    identities = git("log", "--format=%h|%an <%ae>|committer %cn <%ce>").stdout
    identities = identities.decode("utf-8", "replace").splitlines()
    for line in identities:
        sweep("commit-meta", line.split("|")[0], line.encode(), literals, hits, as_text=True)

    print("\n提交身份：%s" % ("; ".join(identities) or "（无提交）"))
    by_key = {}
    for h in hits:
        by_key.setdefault(h[0], []).append(h)
    if not by_key:
        print("clean: 工作区%s没有残留这些标识" % ("和历史" if "--all-objects" in sys.argv else ""))
        return 0
    for key in sorted(by_key):
        rows = by_key[key]
        print("\n== %s (%d) ==" % (key, len(rows)))
        for _, label, where, ln, excerpt in rows[:40]:
            print("  %-11s %s:%s  %s" % (label, where, ln or "-", excerpt))
        if len(rows) > 40:
            print("  ... 还有 %d 处" % (len(rows) - 40))
    return 1


if __name__ == "__main__":
    sys.exit(main())
