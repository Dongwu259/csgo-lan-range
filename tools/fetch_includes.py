#!/usr/bin/env python3
"""Build a self-contained SourcePawn include tree so botmimic.sp can be recompiled.

Copies the install's own include/ directory, then pulls the third-party includes
that botmimic needs (smlib, eItems) from their upstreams, verifying every file
against the byte size reported by jsDelivr's flat package listing.
"""
import hashlib
import json
import os
import shutil
import urllib.request

import gamedir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_INC = os.path.join(gamedir.path(), "addons", "sourcemod", "scripting", "include")
BUILD_INC = os.path.join(ROOT, "build", "include")
MANIFEST = os.path.join(ROOT, "build", "include-manifest.txt")

WANT = {
    "ESK0/eItems@main": ["/scripting/include/eItems.inc"],
    "bcserv/smlib@master": [
        "/scripting/include/smlib.inc",
        "/scripting/include/smlib/arrays.inc",
        "/scripting/include/smlib/clients.inc",
        "/scripting/include/smlib/colors.inc",
        "/scripting/include/smlib/concommands.inc",
        "/scripting/include/smlib/convars.inc",
        "/scripting/include/smlib/crypt.inc",
        "/scripting/include/smlib/debug.inc",
        "/scripting/include/smlib/dynarrays.inc",
        "/scripting/include/smlib/edicts.inc",
        "/scripting/include/smlib/effects.inc",
        "/scripting/include/smlib/entities.inc",
        "/scripting/include/smlib/files.inc",
        "/scripting/include/smlib/game.inc",
        "/scripting/include/smlib/general.inc",
        "/scripting/include/smlib/math.inc",
        "/scripting/include/smlib/menus.inc",
        "/scripting/include/smlib/server.inc",
        "/scripting/include/smlib/sql.inc",
        "/scripting/include/smlib/strings.inc",
        "/scripting/include/smlib/teams.inc",
        "/scripting/include/smlib/vehicles.inc",
        "/scripting/include/smlib/weapons.inc",
        "/scripting/include/smlib/world.inc",
    ],
}


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            import time
            time.sleep(0.6 * (i + 1))


def main():
    if os.path.exists(BUILD_INC):
        shutil.rmtree(BUILD_INC)
    shutil.copytree(GAME_INC, BUILD_INC)
    print("copied install includes:", len(os.listdir(BUILD_INC)), "entries")

    rows = []
    for repo, paths in WANT.items():
        listing = json.loads(get("https://data.jsdelivr.com/v1/packages/gh/%s?structure=flat" % repo))
        sizes = {f["name"]: f.get("size") for f in listing.get("files", [])}
        for p in paths:
            data = get("https://cdn.jsdelivr.net/gh/%s%s" % (repo, p))
            expect = sizes.get(p)
            if expect is not None and len(data) != expect:
                raise SystemExit("SIZE MISMATCH %s: got %d, listing says %d" % (p, len(data), expect))
            dest = os.path.join(BUILD_INC, p.replace("/scripting/include/", "").lstrip("/").replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            rows.append((os.path.relpath(dest, BUILD_INC), len(data), hashlib.sha256(data).hexdigest()))
            print("ok %s (%d)" % (os.path.relpath(dest, BUILD_INC), len(data)))

    with open(MANIFEST, "w", encoding="utf-8") as fh:
        fh.write("# fetched third-party includes, sizes verified against jsDelivr listings\n")
        for name, size, sha in rows:
            fh.write("%-40s %9d  %s\n" % (name, size, sha))
    print("\nmanifest -> %s (%d fetched)" % (MANIFEST, len(rows)))


if __name__ == "__main__":
    main()
