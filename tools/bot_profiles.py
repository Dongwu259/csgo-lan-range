#!/usr/bin/env python3
"""botprofile.db reader: what each bot name is *configured* to be.

The file is Turtle Rock's text profile database (not SQLite despite the
extension): one Default block, a set of Template blocks, then ~2200 profile
blocks whose header is "<template+template+...> \"bot name\"". A bot's numbers
are Default overlaid by each template in order, then its own lines, so the
quoted header name is what has to match the client's name for anything to
resolve -- which is exactly why botprofile.db and data/bot_info.json have to
agree (bot_stuff.smx gates its own overrides on the same name).

Only what the engine and bot_stuff actually consume is reported: the numeric
fields, the weapon preference order, and where each value came from.
"""
import os
import re

import gamedir

GAME = gamedir.path()
DB_PATH = os.path.join(GAME, "botprofile.db")

KV_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')
# dDefinder's file quotes handles that contain no space ("ropz") but leaves
# plain ones bare ("Elite+Rifle+RiflePersonality Maverick"), and 615 of the
# 2187 blocks use the bare form, so both have to parse or most names miss.
HEADER_RE = re.compile(r'^([A-Za-z0-9_\-\+]+)\s+(?:"(.*)"|(\S+))\s*$')
FIELD_ORDER = ["Difficulty", "Skill", "ReactionTime", "Aggression", "AttackDelay",
               "Teamwork", "AimFocusInitial", "AimFocusDecay", "AimFocusOffsetScale",
               "AimfocusInterval", "LookAngleMaxAccelNormal", "LookAngleStiffnessNormal",
               "LookAngleDampingNormal", "LookAngleMaxAccelAttacking",
               "LookAngleStiffnessAttacking", "LookAngleDampingAttacking",
               "VoicePitch", "Cost", "Rank", "Skin"]


def _strip_comment(line):
    """Drop a trailing // comment, but never inside the quoted bot name."""
    out, quoted = [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == "/" and not quoted:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _coerce(text):
    t = text.strip()
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?", t, re.I):
        return float(t)
    return t


def load(path=DB_PATH):
    """-> {default, templates, profiles, tiers} with profiles keyed by name."""
    templates = {}
    profiles = []
    default = {}
    cur = None
    cur_meta = None
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    for n, line in enumerate(lines):
        line = _strip_comment(line)
        if not line.strip():
            continue
        s = line.strip()

        if s == "End":
            cur, cur_meta = None, None
            continue

        # body lines are always indented; anything at column 0 starts a block
        if line[:1].isspace() or line.startswith("//"):
            m = KV_RE.match(line)
            if m and cur is not None:
                key, val = m.group(1), _coerce(m.group(2))
                if key == "WeaponPreference":
                    cur.setdefault("WeaponPreference", [])
                    cur["WeaponPreference"].append(val)
                else:
                    cur[key] = val
            elif not line.startswith("//"):
                raise ValueError("line %d is indented but not a key: %r" % (n + 1, s))
            continue

        if s == "Default":
            cur = default
            continue
        if s.startswith("Template "):
            name = s.split(None, 1)[1].strip()
            cur = {}
            templates[name] = cur
            continue
        m = HEADER_RE.match(line)
        if m:
            chain, bot = m.group(1), (m.group(2) if m.group(2) is not None else m.group(3))
            cur_meta = {"name": bot, "chain": chain.split("+")}
            cur = {}
            profiles.append((cur_meta, cur))
            continue
        raise ValueError("line %d is a block start nothing recognises: %r" % (n + 1, s))

    # "ProP" style tiers are the templates that carry a Skill value: that is the
    # number the engine's aim code consumes, so it identifies the strength band.
    tiers = sorted([n for n, b in templates.items() if "Skill" in b],
                   key=lambda n: -templates[n]["Skill"])

    def resolve(meta, own):
        vals, src = {}, {}
        for k, v in default.items():
            if k != "WeaponPreference":
                vals[k] = v
                src[k] = "Default"
        for t in meta["chain"]:
            body = templates.get(t)
            if body is None:
                continue
            for k, v in body.items():
                if k == "WeaponPreference":
                    continue
                vals[k] = v
                src[k] = t
        for k, v in own.items():
            if k != "WeaponPreference":
                vals[k] = v
                src[k] = meta["name"]
        prefs = []
        for t in meta["chain"]:
            prefs += templates.get(t, {}).get("WeaponPreference", [])
        prefs += own.get("WeaponPreference", [])
        tier = next((t for t in meta["chain"] if t in tiers), None)
        return {"name": meta["name"], "chain": meta["chain"], "tier": tier,
                "values": vals, "source": src, "weapons": prefs}

    out = {"default": default, "templates": templates, "tiers": tiers, "by_name": {},
           "count": len(profiles)}
    for meta, own in profiles:
        rec = resolve(meta, own)
        out["by_name"].setdefault(rec["name"].lower(), []).append(rec)
    return out


def get(db, name):
    """Exact match wins; a bot renamed by a user is still findable by prefix,
    and several profiles can share a handle (the engine draws names at random),
    so -> list of records. The last resort ignores spaces entirely, because a
    name that passed through any space-dropping layer on the way back from the
    server ("[RAM]The Admin" -> "[RAM]TheAdmin") is still the same profile."""
    low = name.strip().lower()
    if low in db["by_name"]:
        return db["by_name"][low]
    hits = [r for k, v in db["by_name"].items() if k.startswith(low) for r in v]
    if hits:
        return hits
    flat = low.replace(" ", "")
    return [r for k, v in db["by_name"].items() if k.replace(" ", "") == flat for r in v]


# human reference bands, for the "does this look like a pro" column only.
# reaction time is the one field with a published apples-to-apples number:
# elite CS players land 150-250 ms on a simple visual reaction test, ~0.0 ms
# is not reachable by a human at all.
REF = {
    "ReactionTime": (0.15, 0.25, "秒；职业选手简单反应测试约 0.15-0.25"),
}


if __name__ == "__main__":
    import sys
    db = load()
    print("profiles: %d, templates: %d, tiers: %s" % (db["count"], len(db["templates"]), " ".join(db["tiers"])))
    print("Default: %s" % " ".join("%s=%s" % (k, v) for k, v in sorted(db["default"].items()) if k != "WeaponPreference"))
    for arg in sys.argv[1:] or ["ropz", "saffee", "sh1ro"]:
        for rec in get(db, arg)[:1]:
            print("\n%s  chain=%s  tier=%s" % (rec["name"], "+".join(rec["chain"]), rec["tier"]))
            for k in FIELD_ORDER:
                if k in rec["values"]:
                    print("   %-28s %-14s (from %s)" % (k, rec["values"][k], rec["source"][k]))
            print("   %-28s %s" % ("WeaponPreference", " ".join(map(str, rec["weapons"][:6]))))
