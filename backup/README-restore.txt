appid=4465480 buildid=22100934 installdir="csgo legacy" date=2026-10-29
<game> below means the game's content folder, <library>\steamapps\common\<installdir>\csgo.
The tool prints it for the current machine: python tools\csgotool.py gamedir
copy botprofile.db.orig -> "<game>/botprofile.db"
copy listenserver.cfg.orig -> "<game>/cfg/listenserver.cfg"
gamemode_competitive_offline.cfg: original content was 3 lines (bot_quota 10 / bot_quota_mode fill)
restore by deleting the trailing exec line
KNOWN-GOOD baseline (validated by user 2026-10-29):
  botprofile.db.tuned + bots_improved.cfg.final + listenserver/gamemode_competitive_offline hooks
copy gameinfo.txt.orig -> "<game>/gameinfo.txt"
  (one-click: backup\restore-gameinfo.cmd "<game>")
copy steam_appid.txt.orig -> "<game parent>/steam_appid.txt"  (730, 5 bytes, sha256 88e2b72514b75000)
