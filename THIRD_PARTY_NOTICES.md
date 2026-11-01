# 第三方组件与许可

这份文件说明本仓库**没有**打包哪些东西、装的时候它们从哪来、以及各自是什么许可。
字节数与 SHA-256 不在这里重复一遍——`configs/components.json` 是唯一来源，
`csgotool.py install --check` 会拿它去核对现场。

## 装的时候下载的东西

| 组件 | 版本 | 来源 | 许可 |
|---|---|---|---|
| Metamod: Source | 1.12.0-git1228 | alliedmodders/metamod-source 的 release 资源 | GPL-3.0-or-later |
| SourceMod | 1.12.0-git7259 | alliedmodders/sourcemod 的 release 资源 | GPL-3.0-or-later，带 AlliedModders 的"可与 Source 引擎/Valve 游戏链接"例外条款（见包内 `addons/sourcemod/LICENSE.txt`） |
| PTaH | 1.1.4 | komashchenko/PTaH 的 release 资源 `windows.zip` | GPL-3.0-or-later（仓库根 LICENSE 已核对） |
| kgns weapons | 1.7.8 | kgns/weapons 的 release 资源 | GPL-3.0-or-later（zip 内带 LICENSE） |
| kgns gloves | 1.0.5 | kgns/gloves 的 release 资源 | GPL-3.0-or-later（zip 内带 LICENSE） |
| sm-ripext | 1.3.2 | ErikMinekus/sm-ripext 的 release 资源 | GPL-3.0-or-later（仓库 COPYING） |
| bot 强化栈 | commit `a07119bb7f…` | manicogaming/CSGOBetterBots 的 `raw.githubusercontent`，**逐个文件**按固定 commit 抓 | **上游没有 LICENSE 文件**，见下一节 |

六个 release 包都是 zip，解压后内部路径就是 `<游戏目录>\...`，所以落地规则只有一条。
`install` 对每个包先比字节数再比 SHA-256，两项都对才解压；压缩包根目录的
`README`/`LICENSE` 不往游戏根倒（`components.json` 的 `exclude`）。

## bot 栈：为什么它不在仓库里

`manicogaming/CSGOBetterBots` 这个仓库（以及它打包的 eItems、navmesh、bot_stuff、
SteamIDConverter、bot_info.json、道具线录 `.rec`）**没有声明任何许可**：根目录列表里
没有 LICENSE/COPYING，GitHub 的 license 字段是 null。botmimic 的原作者是
Peace-Maker（wcfan.de），源码里也没有许可头。

没有许可 = 没有再分发权利。所以：

- 本仓库不打包这些文件，也不提交它们的源码副本（`tools/src/botmimic.sp`、
  `botmimic.smx.upstream`、`gamedata/botstuff.games.txt` 都被 .gitignore 排除，只留本机）。
- `install` 在你明确确认（`--accept-upstream-license` 或交互 y/N）之后，按固定 commit
  从上游抓到你机器上。抓取动作是你自己做的，许可责任跟着你走。
- 清单里的 `botpack` 一共 14 个文件，其中两个值得单独说明：
  - `botprofile.db`（装到游戏根目录）——它是整套 bot 栈的开关：`bot_stuff.smx` 按 bot
    名字去 `data/bot_info.json` 查表才决定接管，Valve 的默认名字一个都不在表里。
    替换前原文件进 `backup/install/<时间戳>/`，`uninstall` 能盖回去。
  - `addons/sourcemod/plugins/botmimic.smx`（上游原样二进制）——它的 `gamedata` 本来就在
    清单里，插件不装就是半装状态。它同时列在 `preserve` 里：本机如果是自己重编译的补丁版，
    `install` 不覆盖，`--check` 会明说是"你改过所以保留"。
- `bots patch` 不改任何人的文件：它读**你自己机器上**的 `botprofile.db` 和 `bot_info.json`，
  改写前者并留还原点。所以本仓库既不需要、也没有提交任何一份 db。
- 只要 GPL 那部分的话：`install --skip botpack`。

## 不入库的游戏文件

`botprofile.db`、`gameinfo.txt`、`listenserver.cfg`、`video.txt` 这些是 Valve 的文件。
它们属于安装产物：Valve 原版（`backup/botprofile.db.orig`、`gameinfo.txt.orig`）只在本机
留着用于还原，不进仓库；`install` 装的是**上游仓库那份** `botprofile.db`（按固定 commit 抓取、
字节与 SHA-256 都钉死），由你自己的机器去取。`install` 需要改 `core.cfg` /
`listenserver.cfg` / `gamemode_competitive_offline.cfg` 时是**逐行改或追加带注释的块**，
原版先复制到 `backup/install/<时间戳>/`，`uninstall --revert <时间戳>` 按那份记录还原。

## 本仓库自己写的东西

`tools/*.py`、`tools/src/lan_fix.sp`、`tools/src/lan_pipe.sp`、`release/plugins/*.smx`、
`configs/*.cfg`、`configs/components.json`、文档 —— 按 **GPL-3.0-or-later** 授权，全文见 `LICENSE`。
两个 `.sp` 通过 SourceMod 的 include 链编译，按 SourceMod 的例外条款可与 Source 引擎一起使用；
`release/plugins` 里放的是这两个源码的编译产物，省得用户装 spcomp。
第三方组件保留各自的许可，本仓库不替它们重新授权，也不打包它们。
