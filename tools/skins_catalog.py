#!/usr/bin/env python3
"""kgns skin catalog: turns weapons_*.cfg / gloves_*.cfg into name lookups.

kgns' own in-game menu shows these strings, so this is the only authoritative
name<->paintkit table in the install. Skin names here are per-language: the
schinese file keeps the English string for skins Valve never translated, so
"血腥运动" and "Fluid Play" can both be looked up in one pass.

The "classes" list matters beyond display: a paintkit id only resolves for the
weapon ids it was authored for, so putting 1255 (an M4A4 skin) on the awp gives
a plain weapon with no error anywhere. Every lookup therefore carries the
classes it is legal on, and csgotool refuses a mismatch.
"""
import os
import re

import gamedir

GAME = gamedir.path()
SM = os.path.join(GAME, "addons", "sourcemod")
WEAPONS_CFG = os.path.join(SM, "configs", "weapons")
GLOVES_CFG = os.path.join(SM, "configs", "gloves")

ENTRY_RE = re.compile(r'^\s*"index"\s+"(-?\d+)"')
CLASS_RE = re.compile(r'^\s*"classes"\s+"([^"]*)"')
TEAM_RE = re.compile(r'^\s*"team"\s+"([^"]*)"')
NAME_RE = re.compile(r'^\s*"([^"]+)"\s*(?://.*)?$')


def _read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def class_to_column(token):
    """kgns' class token in these files is the entity name; the SQLite column
    drops the weapon_ prefix (weapon_m4a1_silencer -> m4a1_silencer,
    weapon_knife_karambit -> knife_karambit, weapon_bayonet -> bayonet)."""
    token = token.strip()
    if token.startswith("weapon_"):
        token = token[len("weapon_"):]
    return token


def load_weapons(lang="schinese"):
    """-> list of {name, paintkit, classes, columns}; columns are kgns' own
    per-weapon family names, empty when the cfg names a weapon this install
    has no column for."""
    path = os.path.join(WEAPONS_CFG, "weapons_%s.cfg" % lang)
    if not os.path.exists(path):
        raise SystemExit("no skin catalog for language %r (%s)" % (lang, path))

    out = []
    name = None
    index = None
    classes = []
    depth = 0                      # 1 = a skin block's own name, 2 = its fields
    for line in _read_text(path).splitlines():
        s = line.strip()
        if s == "{":
            depth += 1
            continue
        if s == "}":
            depth -= 1
            if depth == 1 and name is not None:
                out.append({"name": name, "paintkit": index, "classes": classes,
                            "columns": [class_to_column(c) for c in classes]})
                name, index, classes = None, None, []
            continue
        if depth == 1:
            m = NAME_RE.match(line)
            if m:
                name = m.group(1)
        elif depth == 2:
            m = ENTRY_RE.match(line)
            if m:
                index = int(m.group(1))
                continue
            m = CLASS_RE.match(line)
            if m:
                classes = [c for c in m.group(1).split(";") if c.strip()]
    return out


def load_gloves(lang="schinese"):
    """-> list of {group, group_index, paint, paintkit, teams}. The gloves cfg
    nests paint names inside glove names, which maps onto kgns' gloves table as
    (t_group|ct_group = group_index, t_glove|ct_glove = paintkit)."""
    path = os.path.join(GLOVES_CFG, "gloves_%s.cfg" % lang)
    if not os.path.exists(path):
        raise SystemExit("no glove catalog for language %r (%s)" % (lang, path))

    out = []
    group = None
    group_index = None
    paint = None
    paint_index = None
    teams = []
    depth = 0                      # 1 = glove group, 2 = its fields / paint name, 3 = paint fields
    for line in _read_text(path).splitlines():
        s = line.strip()
        if s == "{":
            depth += 1
            continue
        if s == "}":
            depth -= 1
            if depth == 2 and paint is not None:
                out.append({"group": group, "group_index": group_index, "paint": paint,
                            "paintkit": paint_index, "teams": teams})
                paint, paint_index, teams = None, None, []
            elif depth == 1:
                group, group_index = None, None
            continue
        if depth == 1:
            m = NAME_RE.match(line)
            if m:
                group = m.group(1)
        elif depth == 2:
            m = ENTRY_RE.match(line)
            if m:
                group_index = int(m.group(1))
                continue
            m = NAME_RE.match(line)
            if m:
                paint = m.group(1)
        elif depth == 3:
            m = ENTRY_RE.match(line)
            if m:
                paint_index = int(m.group(1))
                continue
            m = TEAM_RE.match(line)
            if m:
                teams = [t for t in m.group(1).split(";") if t.strip()]
    return out


def display(e):
    return e.get("name") or " / ".join(e.get("names", ["?"]))


def index_by_class(entries):
    """class column -> [(paintkit, name)] so `skins list awp` can show what is
    legal on that weapon."""
    by_class = {}
    for e in entries:
        for col in e["columns"]:
            by_class.setdefault(col, []).append((e["paintkit"], display(e)))
    return by_class


def load_merged(langs=("schinese", "english")):
    """Key on the paintkit id and collect every language's spelling. The id is
    language-independent, so one catalog answers both "血腥运动" and "Bloodsport"
    and the search never has to know which cl_language kgns rendered its menu in."""
    merged = {}
    for lang in langs:
        for e in load_weapons(lang):
            hit = merged.get(e["paintkit"])
            if hit is None:
                merged[e["paintkit"]] = {"paintkit": e["paintkit"], "names": [e["name"]],
                                        "columns": list(e["columns"]), "lang": [lang]}
                continue
            if e["name"] not in hit["names"]:
                hit["names"].append(e["name"])
            for c in e["columns"]:
                if c not in hit["columns"]:
                    hit["columns"].append(c)
            if lang not in hit["lang"]:
                hit["lang"].append(lang)
    return sorted(merged.values(), key=lambda x: -x["paintkit"])


def find(entries, needle, column=None):
    """Exact name first (case-insensitive), then substring, then paintkit id.
    -> (matches, how) where matches keeps order and `how` says what matched."""
    needle = needle.strip()
    low = needle.lower()

    def names(e):
        return e.get("names") or [e.get("name", "?")]

    exact = [e for e in entries if any(n.lower() == low for n in names(e))]
    if exact:
        return (exact if column is None else [e for e in exact if column in e["columns"]], "exact")
    partial = [e for e in entries if any(low in n.lower() for n in names(e))]
    if needle.lstrip("-").isdigit():
        partial = [e for e in entries if e["paintkit"] == int(needle)] + partial
    if column:
        partial = [e for e in partial if column in e["columns"]]
    return (partial, "partial")


if __name__ == "__main__":
    import sys
    w = load_merged()
    g = load_gloves()
    print("weapon skins: %d paintkits, %d distinct classes (schinese+english merged)" %
          (len(w), len(index_by_class(w))))
    print("glove paints: %d entries, %d groups" % (len(g), len({x["group"] for x in g})))
    for arg in sys.argv[1:]:
        m, how = find(w, arg)
        print("\n%r -> %s (%d)" % (arg, how, len(m)))
        for e in m[:12]:
            print("   %6d  %-30s %s" % (e["paintkit"], " / ".join(e["names"]), " ".join(e["columns"])))
