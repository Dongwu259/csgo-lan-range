# lan-range · CS:GO Legacy 本地与局域网工具箱

一句话：把一台机器上的 CS:GO Legacy 变成能自己装插件、能拉平 bot 强度、能带朋友开房的本地服务器。仓库名 `lan-range`（局域网靶场）取的是"自己人、离线、练枪"这层意思；名字里不带游戏商标，兼容对象写在下一行。

对象：`tools\csgotool.py`（配套 `pipe.py` / `bot_profiles.py` / `skins_catalog.py`，插件 `lan_fix.smx` 1.2.3 + `lan_pipe.smx` 1.2.0）
环境：CS:GO Legacy（Steam appid 4465480，build 1.38.8.1 / 13881，32 位），本机 listen server。

下面所有命令都在 **clone 目录里**执行（`cd` 进去一次就行）。想在任意目录调用，就把 `tools\csgotool.py` 换成它的绝对路径。

```
python tools\csgotool.py status
```

**第一次先确认游戏目录。** 工具里不内置任何人的安装路径，它自己找：注册表 `Software\Valve\Steam` → 各 Steam 库的 `libraryfolders.vdf` → `appmanifest_4465480.acf` 的 `installdir` → `<库>\steamapps\common\<安装名>\csgo`。判定标准是该目录里有 `gameinfo.txt` 且上一级有 `csgo.exe`。找到了就直接往下用；没找到先用 `python tools\csgotool.py gamedir --scan` 看它扫过哪些库，再 `gamedir --set "<那个 csgo 目录>"` 固定（写进 `settings.ini`，也可用环境变量 `CSGO_LEGACY_DIR`）。

---

## 0. 先记住五条边界

1. **只用于本地和局域网自娱。** 皮肤靠伪造 netprop、bot 靠改内存偏移，这两样在官方匹配或别人服务器上都是找封。这套东西的适用场景只有 `-insecure` 的本机对局和你自己开的局域网房。
2. **自动开局已经成立，工具能自己做，不用点菜单。** 命令是 `csgotool.py start de_nuke --launch`：它直连 `csgo.exe` 并带上 `-insecure +game_type 1 +game_mode 0 +map <图>`，实测日志顺序为 `VAC secure mode disabled` → `Host_NewGame on map de_nuke` → `MAINMENU → LOADINGSCREEN → INGAME`，8 个 mod DLL 全在，`status` 直接回数据。两个前提：**(a)** `-insecure`（见第 5 条，没有它引擎不加载 addons）；**(b)** 安装目录的 `steam_appid.txt` 必须是 4465480——出厂值 730 是 CS2，Steam 因此拒绝给会话，进程只会卡在 Launcher Error 弹窗上；工具在每次直连前把它对齐（原值备份在 `backup/steam_appid.txt.orig`）。走过的弯路记在这里免得重犯：`steam://run/4465480//+map …` 的参数是 **Steam 客户端丢的**（不是引擎不认），而"直连 exe 一定失败"这个旧结论已被推翻——失败的原因是 appid 文件。  **但直连启动有两个实测到的代价**，所以正经玩不要用它的（(b) 仍然成立——Steam 启动选项那条路没有这两个问题）：
  - **拿不到 Steam 用户会话**：日志里是 `AuthStatus ... Failed (No response from server)` 和 `Authentication and VAC not requested`。表现为**键位、灵敏度、视频设置像被清空**（实际磁盘上的 `Steam\userdata\<你的 SteamID64>\4465480\local\cfg\config.cfg` 一字节没动，mtime 也没变，下次正常启动就回来了）。
  - **`+game_type` / `+game_mode` 落得太晚**：`Host_NewGame` 已经按引擎默认模式建好了，于是开出来的是**军备竞赛**这类非竞赛局（日志提示 `GameTypes: missing mapgroupsSP entry for game type/mode (custom/custom)`），即使 `game_type` 读回来是 1。修法是开局后先写 `game_type`/`game_mode` 再 `changelevel` 一次——`start` 已经这么做了。
  **最终结论（2026-10-31 18:0x 实测）**：无论是工具直连还是 Steam 启动选项，`+map` 开出来的对局都是**非竞赛玩法**（日志 `exec: couldn't exec gamemode_armsrace_server.cfg`，现金奖励 cvar 全为 0），而且**已创建的对局无法用 cvar 改玩法**——写 `game_type`/`game_mode` 再 `changelevel` 都不换规则（`game_type` 读回来一直是 1/0，是骗人的）。原因：对局的玩法由 Panorama 主菜单创建 session 时决定（`matchmaking.dll` 里的 `offlineinit` 会话命令），控制台里根本没有创建离线对局的命令（`session_send` / `start_offline` / `create_offline` 在三个 DLL 里都搜不到）。所以判据是 **`csgotool.py mode`**（按 gamemode_*.cfg 的 cvar 指纹识别，不含我们自己 cfg 碰过的键）；要竞技局就必须回主菜单点一次 **开始游戏 → 与电脑玩家**，之后 `start <图>` 全自动。
仍然成立的一条：**主菜单状态下 SourceMod 不加载**（它是 server 侧框架），所以没开局时工具连不上是正常现象。
3. **不要把 `sv_lan` 设成 1。** 那会让所有人拿不到 SteamID，而 kgns 的皮肤表是按 SteamID 存的、`GetClientAuthId` 又是失败即返回且没有 else 分支——结果是所有人的皮肤、手套、刀全部丢失且不会保存。`set sv_lan 1` 会被工具拒绝，除非你加 `--force`。
4. **不要把 UDP 27015 往公网做端口映射。** 文件管道等价于本机的任意命令执行通道，别把它暴露到公网。防火墙放行命令工具只会打印出来给你看，不会替你执行（它也没有管理员权限）。
5. **房主端必须用 `-insecure` 启动，否则整套东西都不存在。** 这是 2026-10-31 实测证实的加载闸门：游戏处在真正的 VAC 安全会话时，引擎**根本不把 addons 里的 DLL 装进进程**——`csgo.exe` 的模块列表里 `engine/client/server/matchmaking.dll` 都在，而 `metamod*`、`sourcemod*`、`sourcepawn*`、`ptah*`、`sdktools*` 一个都没有（磁盘上这些文件全在、大小正确）。加上 `-insecure` 后 8 个 mod DLL 全部进进程，管道立刻应答、皮肤正常应用。症状极具欺骗性：对局看起来完全正常，只是没有皮肤、没有 bot 强化、工具连不上。判据命令：**`python tools\csgotool.py mods`**（`doctor` 也会先打一行 mods）。
   - 启动选项里就写一个词：`-insecure`（要自动开局再加 `+game_type 1 +game_mode 0 +map 图名`）。
   - 以前它"自己就是 insecure"是因为 **Watt Toolkit / Steam++ 拦截了 Steam 会话**，VAC 校验拿不到结果；关掉加速开关并不等于退出该程序（它 3 个进程仍在跑，还占着 `127.0.0.1:27015`）。别把这种意外状态当依赖。

---

## 1. 五分钟上手

**先决条件一次说清**：Steam 库 → 右键这个 app → 属性 → 启动选项里必须有 `-insecure`（见 §0 第 5 条）。没有它，皮肤、bot 强化、文件管道全部不存在，而游戏看起来正常。确认方法：`csgotool.py mods`。

### 新机器上从零装插件

clone 之后先确认游戏目录（§开头那条），然后把第三方栈一次装齐：

```
python tools\csgotool.py install --dry-run    # 只看计划，一个字节都不写
python tools\csgotool.py install              # 真装，落地前会再问一次
```

`install` 的次序是刻意的：按 `configs\components.json` 下载六个 release 包 → **先比字节数再比 SHA-256** → 解压到 `staging\` 并逐文件按 CRC 复核 → 算出计划（新增 / 替换 / 已相同 / 你改过所以保留）→ 要替换的文件先复制进 `backup\install\<时间戳>\` → 落地 → 给两个 Valve cfg 追加 `exec` 行、把 `core.cfg` 的 `FollowCSGOServerGuidelines` 改成 `no` → 解除 MOTW → 写 `report.json`。校验有任何一项不过就停下来，现场不动。

- 你在插件配置里做的改动（`core.cfg`、`cfg\sourcemod\` 下那些）在清单的 `preserve` 名单里：**只跳过，不覆盖**，`--check` 会单独列出来告诉你"这个你改过"。
- bot 强化那批（`botpack`）的上游仓库没有 LICENSE，所以要么 `--accept-upstream-license` 要么交互确认，抓取动作由你的机器对上游做；不想碰它就 `install --skip botpack`。理由与细节见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- `botpack` 会把你游戏根目录的 `botprofile.db` 换成上游那份，**这一步是整套 bot 栈的开关**：`bot_stuff.smx` 是按 bot 名字去 `data/bot_info.json` 查表才决定接不接管，而 Valve 默认名字（`Ron`、`Yahn`…）一个都不在表里——不换文件的话插件加载得干干净净，然后什么都不做。原文件先复制进还原点，`uninstall` 会拿它盖回来。
- 游戏进程在跑就拒绝执行——文件被占着会留下半个插件栈；确认自担风险才用 `--force`。
- 装完要换一次图才加载（metamod 的挂载点在建图时）。装完最后会打印一份带序号的首跑清单，从"Steam 启动选项写 `-insecure`"一路列到"跑 `bots patch`"，照着做完就能玩。
- 退回去：`python tools\csgotool.py uninstall --revert <时间戳>`（不写时间戳就是最近一次），它按那次的 `report.json` 删掉新增的、恢复被换掉的，包括那两行 `exec` 和 `core.cfg` 的那一格。
- 只想知道现场对不对、什么都不改：`install --check`（全部一致时退出码 0，可以直接当 CI 用）。
- 想知道**这套栈自不自洽**（游戏不用在跑，也不用下载缓存）：`install --audit`。它查的是另一类问题——清单声明的 `.smx` 有没有缺、`bot_stuff → bot_steamids → SteamIDConverter` 这类依赖链断没断、`FollowCSGOServerGuidelines` 现在是什么、cfg 挂钩在不在、`botprofile.db` 的档位分布与名字命中率、哪些本地图没有道具路线、DLL 还带不带 Web 标记。坏了退出码 1，只是"可以更好"就打印建议、退出码 0。
- 打包版也能装：清单和两个自写 `.smx` 用 `--add-data` 进了 exe（§9 一条命令重打包），运行时从 `sys._MEIPASS` 里读；区别只有一个——还原点落在 **exe 旁边的** `backup\install\<时间戳>\`，因为写操作一律锚在 `sys.executable` 所在目录。

| 我想干什么 | 命令 |
|---|---|
| 看服务器活着没、插件版本、有没有报错 | `csgotool.py doctor` |
| 看 mod 到底进没进进程（是不是漏了 `-insecure`） | `csgotool.py mods` |
| 不开游戏就知道这套栈缺了什么 | `csgotool.py install --audit` |
| 把每局 bot 的强度抽卡拉平（改本机 db，带备份还原） | `csgotool.py bots patch --names` |
| 看当前对局（地图 / 人数 / 比分 / 剩余时间） | `csgotool.py status` |
| 看每个玩家和 bot 的名字、队伍、生死 | `csgotool.py who` |
| 联机时一直盯着比分和进出的人 | `csgotool.py watch --players` |
| 改一个参数并当场读回验证 | `csgotool.py set mp_freezetime 5` |
| 读一个参数 | `csgotool.py get mp_maxrounds` |
| 换图（= 开新的一局） | `csgotool.py map de_mirage` |
| 给朋友一个双击就能进房的文件 | `csgotool.py join --write-cmd` |
| 单挑：10 回合、每回合 4750、都只拿 AWP | `csgotool.py duel --rounds 10 --money 4750 --weapon awp --bots 0` |
| 看 bot 到底是不是职业选手水平 | `csgotool.py bots` |
| 把某个皮肤换成中文名能搜到的那个 | `csgotool.py skins set awp 血腥运动` |

### 不想反复敲命令：常驻交互窗口

```
python tools\csgotool.py shell
```

界面是**每屏清屏 + 顶部常驻对局行 + 分页选择器 + 表单**，不用背任何参数：

```
=== CS:GO Legacy 工具箱 ===
  de_mirage      真人 1 /64  bot 9  | T 4 - 1 CT | 剩余 无限     冻结 15s

  1   对局与名单          status + who 一屏
  2   盯盘 watch          比分/人数实时刷新，Ctrl+C 回菜单
  3   bot 数值对照         库里配置 vs 引擎实测
  4   皮肤                搜 / 按枪选 / 应用 / 读回
  5   单挑设置            回合数、补钱、自动发枪、清 bot
  6   换图开新局          从列表挑图
  7   联机与加入          地址、.cmd、密码、房名
  8   专家模式            任意参数与命令、cfg 生成与校验
  9   体检 doctor         管道/插件版本/错误日志
  10  装机与自检          install / 核对 / 静态自检 / bots patch / 撤销
```

顶部那行就是你现在最关心的东西，每屏都重画一次；任何一屏返回时都会清屏，历史不会堆成滚不动的墙。

### 三个常用的子界面

**换图（6）**：列出 36 张图（来源是 maplist / mapcycle / `csgo\maps\*.bsp` / 道具库），序号选，`/词` 筛选，`n`/`b` 翻页。选完先打印"将执行 `start xxx --now`"并要你确认。

**皮肤（4 → 2）**：选武器类别（53 项列表）→ 列出该枪**能**用的皮肤（中文名/英文名一起显示，可 `/词` 筛）→ 依次问磨损、seed、StatTrak（**磨损直接回车 = 不动你已存的值**）→ 显示将执行的完整命令 → 确认。你全程不需要知道任何 paintkit 号。

**单挑（5）**：一个表单，每行显示当前真实值，改哪项就敲那个字母：

```
  r  回合数 mp_maxrounds          现在 30
  f  冻结秒数 mp_freezetime        现在 15
  b  bot 数量 bot_quota            现在 10   （1v1 填 0）
  m  每回合补到多少钱              现在 未设置
  w  每回合自动发的武器            现在 未设置
  s  是否先清空原有武器            现在 清空(留刀)
  a  按上面的值执行    x  全部关掉
```

`w` 打开的是从 53 个武器名里挑的子菜单（可加可减），不是让你回忆 `awp,deagle` 怎么写。所有数字都有范围校验（写 `abc` 或 `1.7` 会被拦下重新问，不会像早期版本那样把 `--wear n` 塞进命令）。改完 `a` 会先把拼出来的命令行显示给你看，确认后才下发。

### 通用规则

- **它不是第二套实现。** 所有动作最终都是把命令行交给同一个 `main()`，所以 shell 和命令行不可能行为分叉。
- 也可以**跳过菜单直接敲完整命令**：`set mp_freezetime 5`、`bots ropz --detail`、`join --write-cmd`。
- 提示里出现 `[默认值]` 时，直接回车就用默认值；`-` 表示"清除/不接管"。
- 盯盘（2）会占住界面直到你按 Ctrl+C，之后回到菜单而不是退出程序。
- 服务器没在跑（游戏主菜单/暂停）时，顶部状态行探测两次失败后就不再刷新，避免每屏白等几秒。
- **双击开窗口**：[csgotool.cmd](csgotool.cmd)，等价于 `python tools\csgotool.py shell`；`%*` 会透传参数，所以 `csgotool.cmd doctor` 这种一次性用法也在。出错时窗口 pause 住不闪退。批处理保持纯 ASCII 是刻意的——cmd 用 OEM 代码页读批处理，写中文注释会让它执行到乱码片段。

---

## 2. 叫朋友来打一局

### 步骤

1. 开局（两种方式，都不用手点菜单）：

   ```
   python tools\csgotool.py start de_nuke --launch
   ```

   工具会直连 `csgo.exe` 并带上 `-insecure +game_type 1 +game_mode 0 +map de_nuke`。想让它开在你自己从 Steam 启动的那局上，也可以在主菜单点 **开始游戏 → 与电脑玩家**——但那种情况下必须先在 Steam 属性 → 启动选项里写 `-insecure`，否则插件不会加载（§0 第 5 条）。
2. 应用联机配置并开一局指定地图：

   ```
   python tools\csgotool.py start de_ancient --now
   ```

   `start` 会：检测服务器是否已在跑 → `exec lan.cfg` → `changelevel` 到目标图 → 等图真正加载完 → `--now` 时执行 `mp_warmup_end` 让正赛立刻开始 → 打印加入地址。
3. 生成给朋友的东西：

   ```
   python tools\csgotool.py join --write-cmd
   ```

   输出会告诉你三件事：本机各网卡地址、**同网段范围**、以及 `lan\join-<IP>.cmd`。把那个 `.cmd` 发给朋友，双击即连（有密码时密码就写在链接里，所以这个文件别发到公网群里）。每个地址还会给三种写法：

   | 写法 | 形态 | 说明 |
   | --- | --- | --- |
   | 控制台 | `connect <房主局域网 IP>:27015 <密码>` | 一定有效，但要朋友自己敲 |
   | 一键 | `steam://connect/<房主局域网 IP>:27015/<密码>` | Valve 文档里第三段就是密码。2026-10-31 实测：链接确实送到了游戏，但被 Steam 拒了，客户端弹「无法连接服务器——服务器指定的 app id 无效」——Steam 要靠服务器查询判断这台服务器属于哪个 app，而这台 build 不应答 A2S |
   | 启动项 | `+connect <房主局域网 IP>:27015 <密码>` | 填进朋友自己 Steam 的 属性 → 启动选项，之后每次开游戏自动进房。`client.dll` 里有 `Executing deferred connect command` 这句，说明 `+connect` 会被主菜单延后执行而不是丢掉，这正是第三方平台"一键进房"用的机制 |

   朋友那边还有一个硬前提：他装的必须也是 **Counter-Strike: Global Offensive (Legacy)**（appid 4465480，同一个 build）。装 CS2 连不进来，协议对不上。

### 朋友连不上时按这个顺序查

1. **他和你是不是一个网段。** `join` 会打印类似 `同网段范围 192.168.16.1 - 192.168.31.254 (192.168.16.0/20)`。朋友的 IP 不在这个区间里就不是防火墙问题，是路由/不同 AP 的问题。
2. **服务器在不在。** 别看端口——CS:GO 客户端自己就会绑 UDP 27015，停在主菜单时也一样，`join` 第一行打印的占用信息不能证明有服务器。真正的判据是文件管道有没有被插件吃掉：`python tools\csgotool.py status` 能在几秒内回数据就是在对局中，超时就是还没开局（或 ESC 暂停中）。
3. **防火墙。** 工具会打印需要管理员权限执行的 `netsh advfirewall firewall add rule ... localport=27015`，你自己复制到管理员终端跑。
4. **别去翻服务器列表。** 这个 build 完全不应答 A2S 查询（真跑图的服务器上实测 `A2S_INFO` / `A2S_PLAYER` / `A2S_SPLIT` 全部超时），所以局域网列表里永远看不到这个房间，只能 `connect`。这不是故障，别浪费时间排。

### 密码房

```
python tools\csgotool.py lan --password hunter2
```

改 `lan.cfg` 里的 `sv_password`（旧文件自动备份成 `lan.cfg.bak`）并立即 `exec`。之后 `join` 打印的三种写法都会自动带上密码。

### 自动开局：已打通，靠的是直连启动 + `-insecure`

结论先说：**Steam 的启动选项确实会接到游戏的命令行上**，`+map` 也确实**开出了一局**。`console.log` 里能看到 `---- Host_NewGame ----` / `Host_NewGame on map de_nuke`，UI 状态一路走到 `LOADINGSCREEN → INGAME`，还看到 `Initializing Steam libraries for secure Internet server`、`Logging into anonymous listen server account.`、`VAC secure mode is activated`。

但那局一个插件都没加载。当时我把它归因于"服务器在 addons 挂载之前创建"，**那是错的**：真正的闸门是 §0 第 5 条——VAC 安全会话拒绝加载未签名 DLL。同一次启动里加不加 `-insecure` 才是分水岭，同一次启动里加不加 `-insecure` 才是分水岭，所以那次启动的表现是：

- 插件启动标记停在上一局——`lan_pipe` / `lan_fix` 最后一次 `loaded` 是 15:51:15，重启后没有任何新标记；
- SourceMod 主日志 `L20261031.log` 在重启前就写了 `Log file closed.`，重启后 0 行新增；
- 于是：文件管道死、皮肤不生效、bot 数值插件不生效、`duel` 无效，`status` 一直超时。

**所以流程仍然是：主菜单点一次 开始游戏 → 与电脑玩家**（那条路径下 metamod/SM 正常加载，同一份日志里 15:51 那次就是证据）。想复现这次测试的话：

1. 属性 → 启动选项粘 `+game_type 1 +game_mode 0 +map de_nuke -condebug`，完全退出游戏再启动；
2. `csgo/console.log` 出现 = 参数确实进了引擎（`-condebug` 本身就是探针）；
3. 判断插件有没有加载，看 `addons/sourcemod/data/lan_exec.log` 里有没有**新的** `lan_pipe x.y.z loaded`——不要看端口占用，客户端自己就会绑 27015；
4. 测完把那行清空：`-condebug` 会持续往磁盘写日志，而且 `console.log` 被游戏独占着，运行中删不掉。

**最终结论（2026-10-31 17:4x 实测）**：上面那局之所以是空壳，纯粹是缺 `-insecure`；把 `-insecure` 加进同一条命令行后，`start de_nuke --launch` 开出来的对局 8 个 mod DLL 全在、管道秒回、比分和回合计时正常走。两个必需条件：`-insecure`（§0 第 5 条）和 `steam_appid.txt` = 4465480（出厂是 730，Steam 会因此拒绝给直连进程会话，卡在 Launcher Error 弹窗；工具每次直连前自动对齐，原值备份在 `backup/steam_appid.txt.orig`）。所以"后台开服"现在等于：**一条命令把带插件的对局开起来，朋友直接连**。仍然没解决的只有一件：这台机器的 listen 服在 `-insecure` 下不做玩家认证（日志明写 `Authentication and VAC not requested`，而 `AuthStatus` 这次是 `Failed (No response from server)`），所以**朋友能不能连上、连上后他的 SteamID 还在不在（决定他的皮肤能不能存）**必须等第二台机器才能验。

`csgotool.py start <map> --launch` 只负责把游戏开到主菜单，并把上面这套文本打给你——它不再假装 `steam://run` 能带参数。

### 为什么还没有"真·后台开服"

完美/5E 那类平台能做到点一下就进房，靠的是三件我们都查过的东西：他们**自己拥有服务器**，把 `+connect ip:port 密码` 交给**游戏自己的命令行**，而引擎会把 `+connect` 延后到主菜单就绪再执行（`csgo/bin/client.dll` 里就有 `Executing deferred connect command: %s.` 这句）。顺带一提，这个 build 的 `client.dll` 里还留着 `#SFUI_LoginPerfectWorld_Offline20221206` 这个本地化 token——完美世界登录本来就集成在这份客户端里，所以那是正规通道，不是进程注入。

要真正做到"不用开游戏窗口也有房"，需要的是专用服务器（srcds），而这台 install 给不出来：

- `appmanifest_4465480.acf` 和 Steam 的 appinfo 缓存里，这个 app 的可执行文件只有 `csgo.exe`（启动参数 `-steam -worldwide`），`oslist` 只有 `windows,macos`，安装目录根下也确实没有 `srcds.exe`。
- 当年的 CS:GO 专用服工具 appid 740 已经不在本机 appinfo 缓存里了；acf 里 depot 735/736 有 manifest 但 `size 0`（没下载），它们是不是 dedicated 内容需要用 SteamCMD 的 `app_info_print 4465480`（只读）才能确认。
- 引擎侧并没有排斥它：`engine.dll` 有 dedicated 代码路径，`matchmaking.dll` 里有一整组 `mm_dedicated_allow / mm_dedicated_fake / mm_dedicated_force_servers / mm_dedicated_ip`。所以这是"能不能装到"的问题，不是架构上不可能。
- 就算装上了还有第二道坎：专用服要保持 `sv_lan 0` 才拿得到玩家 SteamID（皮肤就是按 SteamID 存的），而 srcds 做认证需要 `sv_setsteamaccount` 的 GOTL token；legacy 分支还能不能申请到 token 未知。拿不到就只能退回 `sv_lan 1`，那又会丢掉所有人的皮肤保存。

所以现在这台机器的形态是：**一个必须开着游戏窗口的 listen 服**，窗口不需要人盯，但要在对局中。想彻底摆脱窗口，就先花一次成本验证上面两条（SteamCMD 查 depot + GOTL token 是否还能申请），我可以把只读的那步查完再决定要不要下载。

### 2026-10-31 实测：连接的真正拦路石是 appid，而且本机测不了

- `steam://connect/ip:port` **不是没反应**：它确实送到了游戏，但握手被 Steam 拒绝，客户端弹「无法连接服务器——服务器指定的 app id 无效」。所以之前那条"管道连续应答 = URI 没生效"的推理是错的：应答不断只是因为握手早就失败、对局从没重载。
- 原因候选定位到了：安装目录的 `csgo/gameinfo.txt` 里 `FileSystem → SteamAppId` 写的是 **730**，而本机安装/启动的是 **4465480**。曾把它改成 4465480 试过一次（原文件备份在 `backup/gameinfo.txt.orig`，SHA-256 `af6a607e…dbaa`，一键还原 `backup/restore-gameinfo.cmd`）：改完游戏照旧正常启动到主菜单，说明这个值不参与内容挂载，改动本身安全；但因为下面那条，这个改动到底解不解锁**在本机测不出来**，所以已经还原回 730，让 gameinfo 保持只有已证实的变量被改过。
- 同机第二实例曾经被判过死刑，理由是"直接跑 `csgo.exe` 只会停在 `FATAL ERROR: Failed to connect with local Steam Client process!`（进程只占 16 MB）"。**那个结论是错的**：真正的拦路石是安装目录的 `steam_appid.txt`——它是 730 时 Steam 拒绝给会话，改成 4465480 后直连就能起完整对局（见 §0 第 2 条）。第二实例这次实验之所以仍然没有结论，是因为房主本来就不能加入自己开的 listen 服，跟注入通道无关。
- 结论：**"朋友到底能不能连进来"在一台机器 + 一个账号上无法测量**。要答案只能等第二台机器/账号，届时先看他在控制台 `connect <房主局域网 IP>:27015` 是否还报 app id 无效——那才是判定 gameinfo 这一改有没有生效的证据。
- 顺带把这类只有屏幕能看到的信息变成了工具能自己读的：`csgotool.py dialog [--pid N]` 直接枚举 csgo.exe 的窗口，把弹窗原文打出来（窗口类 `#32770` = 错误弹窗，`Valve001` + Direct3D 标题 = 游戏真的起来了）。

---

## 3. 单挑：回合数、每回合补钱、每回合固定发枪

```
python tools\csgotool.py duel --rounds 10 --money 4750 --weapon awp --freeze 5 --bots 0 --apply
```

| 参数 | 作用 |
|---|---|
| `--rounds N` | `mp_maxrounds N`，并顺手关掉 `mp_overtime_enable`、`mp_halftime`、`mp_teams_unbalance_limit`（单挑不该被中场/加时/人数不平衡打断） |
| `--money N` | 每回合出生把钱包写成 N。**为什么不是 cvar**：经济里没有任何变量能"把指定玩家补到指定金额"，`mp_startmoney` 只在经济重置时触发 |
| `--weapon a,b` | 每回合出生发这些枪，逗号分隔；名字会拿皮肤表的 53 个武器 key 校验，写错当场拒绝 |
| `--no-strip` | 只加枪，不清空原有武器（默认会清空主武器/手枪/道具/C4，**但保留刀**，这样发枪失败也不会让人空手） |
| `--freeze N` | `mp_freezetime N` |
| `--bots N` | `bot_quota N`；真 1v1 用 0（实测：这个 build 没有 `sv_hibernate_when_empty`，清空 bot 后管道照样响应） |
| `--target` | `humans`（默认，双方都套）/ `all` / `bots` / 某个玩家名 |
| `--apply` | 对当前活着的人立刻套一次，不用等下一回合 |
| `--show` / `--off` | 看现在套了什么 / 关掉自动发枪+清枪+补钱 |

注意：`--target humans` 对你自己在观战位时**不生效**（出生钩子故意跳过 `team < 2`）。`who` 里 `team=1` 就是观战位。

回合数、冻结时间这类是 cvar，`duel --off` 不会替你改回去，`--show` 会列出当前值，用 `set` 还原。

---

## 4. 皮肤

### 查

```
python tools\csgotool.py skins search 血腥
python tools\csgotool.py skins search Bloodsport
python tools\csgotool.py skins list awp --limit 60
```

工具把 kgns 的 `weapons_schinese.cfg` 和 `weapons_english.cfg` **按 paintkit 号合并**成一本双语表（1075 个皮肤，两边 id 集合与适用武器完全一致，只有 22 个未翻译），所以中文名和英文名都能搜，结果形如 `639 血腥运动 / Bloodsport ak47`。

### 换

```
python tools\csgotool.py skins set awp 血腥运动
python tools\csgotool.py skins set ak47 639 --wear 0.42 --seed 1000 --trak 1
python tools\csgotool.py skins set knife_karambit 415
```

- 第二个参数既可以是 paintkit 号，也可以是皮肤名（中/英）。
- 一个名字可能对应多个号（血腥运动在 ak47/scar20/mp7 上是三个不同的 paintkit），工具会列出来让你选。
- **类别守卫**：皮肤表里没登记给该武器的号，工具会拒绝并告诉你它属于哪些枪（引擎不会报错，只会渲染成默认皮肤）。确信要塞就加 `--force`。
- `--wear` 省略时**不动**已存的 float（这是刻意的：早期版本省略它会静默把你的 float 写成 0）。
- 刀要用 `knife_<型号>`（`knife_karambit`、`knife_butterfly`…）。裸 `knife` 列是 kgns 的**刀模型选择器**不是 paintkit，写进去会毁掉你的刀型选择，插件直接拒写。

### 看现场

```
python tools\csgotool.py skins dump
python tools\csgotool.py skins dump --target Ro1f
```

读回的是引擎里实体上的真实值（paintkit / wear / seed / stattrak / quality），不是数据库里的期望值。

---

## 5. 看 bot 到底什么水平

```
python tools\csgotool.py bots
python tools\csgotool.py bots ropz --detail
python tools\csgotool.py bots --no-live
```

输出把**配置值**（`botprofile.db` 沿 `Default → 模板链 → 自己覆盖` 算出来的应有值）和**实测值**（从进程里读回的、引擎真正在用的值）并排列出，最后一列告诉你这个 bot 有没有被 `bot_stuff.smx` 接管。

怎么读：

- **档位**：`ProP / Elite / Expert / VeryHard / Hard / Tough / Normal / Fair`，对应 `Skill 100→25`、`ReactionTime 0.15→0.50`。
- **接管列**：名字在 `data/bot_info.json`（1409 个职业 ID）里的，插件每回合写死 `反应 0.165–0.325 / 进攻 0–1 / 甩狙加速 4000–7000` 的随机值；五个 `sh1ro / ZywOo / ropz / donk / m0NESY`（**大小写敏感**）则是 `100000 / 0.0 / 1.0`。名字不在表里的，插件不管，库里多少就是多少。
- **实测反应 0.000** 只可能出现在那五个人名上，那不是"职业水平"，是人类不可能。
- **`--detail`** 会逐字段给出 `库里值 / 引擎实测 / 结构体偏移 / 判定`。偏移表是实测出来的：一个不在 bot_info 里的 bot 作为对照组，17 个字段全部与配置一致。
- 已知现象：bot 名字每局随机抽，所以**强度是抽卡**——实测连续两局都是 7 个 ProP + 2 个低档（0.40 / 0.45 秒反应），那两个恰好都不在 bot_info 里，等于每局有两个人慢一倍。调 `bot_difficulty` 没用（这份文件里所有档位都声明支持全部难度，那个 cvar 基本失效）；治它的是下面这一节的 `bots patch`。

### 把强度拉平：`bots patch`

抽卡的根子有两条，都在**你本机那份 `botprofile.db`** 里，所以这个命令改写它，而不是发一份新的给你（Valve 的原文件和 repack 的改写版都不入库）：

| 症状 | 根因 | patch 做的事 |
|---|---|---|
| 每局混进 0.40/0.45 秒反应的 bot | 池子里 781/2187 个 block 挂在弱档模板上 | 把每个 block 链首的档位 token 改成库里最强的一档（默认 `ProP`） |
| 有些 bot 完全不被插件接管 | 名字不在 `bot_info.json` 那 1409 个职业 ID 里 | `--names` 把不命中的名字换成表里还没被占用的职业名 |

```
python tools\csgotool.py bots patch --dry-run     # 只看计划：改多少行、档位分布怎么变
python tools\csgotool.py bots patch --names       # 真写，写之前自动备份
python tools\csgotool.py bots patch --backups     # 有哪些还原点
python tools\csgotool.py bots patch --revert      # 还原最近一次（可给时间戳）
python tools\csgotool.py bots patch --only ProP   # 另一种口味：弱档 block 直接删掉，池子变短
```

- 只动 profile 的头行：换链首的档位 token、或者换名字，`Default` / `Template` 块与所有数值行都不碰。实测两份 db（Valve 那份 147 个 block、repack 那份 2187 个）里**没有任何 profile 自己覆盖 `Skill` 或 `ReactionTime`**，所以改头行就够。
- 行尾（CRLF/LF）、缩进、行尾注释按原样保留；计划算完和写盘之间文件被改动过会拒绝落盘。
- 幂等：拉平之后再跑，报"没有要改的"。
- 换名用**表里的原样大小写**（`bot_stuff` 有些比较是 `strcmp`），并且不会把 `sh1ro / ZywOo / ropz / donk / m0NESY` 这五个发出去——那五个会拿到 `100000 / 0.0 / 1.0`，0 秒反应不是人类水平，拉平强度不该顺带造出这种 bot。
- `--only` 若会把名字池清空（指定档位里一个 block 都没有）直接拒绝，并告诉你现有什么档位。
- 还原会比对 `report.json` 里记录的产物哈希：你自己又动过这份 db 就拒绝还原，确认要覆盖才加 `--force`。
- `--names` 依赖 `data/bot_info.json`，那是 `install` 装 `botpack` 时放进去的；没有它会报错让你先装，不会静默什么都不做。
- 改完要开新的一局才生效（bot 的名字与数值在生成时定）。
- `install --check` 认得这份改写：哈希在 `backup\bots\*\report.json` 里出现过就算"正常"，发布门不会被自己拉平的 db 弄红。

---

## 6. 规则、配置与校验

```
python tools\csgotool.py get  mp_maxrounds
python tools\csgotool.py set  mp_maxrounds 24
python tools\csgotool.py exec mp_warmup_end
python tools\csgotool.py map  de_inferno
python tools\csgotool.py cfg  gen practice --set mp_freezetime=5 --set mp_maxrounds=99
python tools\csgotool.py exec "sm plugins reload lan_fix.smx"
```

- `set` 会**读回验证**，不一致会明确告诉你 `DIFFERS`。
- `exec` 是逃生舱：任何服务器控制台命令和插件命令都能走，包括 `sm_lanfix_*`。
- `map` / `start` 换图前先对 `maplist.txt`、`mapcycle.txt`、`csgo\maps\*.bsp` 和道具数据库核对；只在 vpk 里的图会警告但仍会尝试（切完用 `status` 确认）。
- 记住 `changelevel` 会重跑 `listenserver.cfg` 和 `gamemode_competitive_offline.cfg`，所以 `bots_improved.cfg` 会被重新应用；你的自定义 cfg 要靠 `exec` 排在它后面才生效。
- `verify` 是这套工具里所有参数名的来源可信度保证——它拿 `cfg` 文件里每个标识对 `server.dll` + `engine.dll` 做整词字节搜索：

  ```
  python tools\csgotool.py verify lan.cfg
  ```

  自己写 cfg 时先 `cfg gen`，再 `verify`，不存在的变量名会被点名。

---

## 7. 诊断与故障排查

```
python tools\csgotool.py doctor
```

一次报出：管道是否响应、`lan_pipe` / `lan_fix` 实际加载的版本、botmimic 与 kgns 两个插件的探针 cvar、`lan_fix` 最后一行日志、当天错误日志的大小和时间、在线真人数量。

| 症状 | 原因 | 处理 |
|---|---|---|
| `管道没有响应` | 游戏在 ESC 暂停菜单 / 主菜单 / 读图中；或插件没加载 | 看 27015 是 TCP 还是 UDP 占用：只有 TCP = 还在主菜单；关掉暂停菜单；`doctor` 复查 |
| `sm plugins reload plugins/xxx.smx` 跑了但版本没变 | reload **只认裸文件名**，带 `plugins/` 前缀会被接受且静默什么都不做 | `exec sm plugins reload lan_fix.smx`，再看 `doctor` 里的版本标记 |
| 版本 cvar 和日志不一致 | SourceMod 在帧末才释放被 reload 插件的 cvar，旧 `sm_*_version` 会滞留 | 以插件写进自己日志的 `"<name> <version> loaded"` 为准，`doctor` 读的就是这个 |
| 改完皮肤没变化 | 该玩家当前没带这把枪 | 插件会回 `carries no xxx: persisted, buy it and re-run`，买出来再执行一次 |
| 朋友列表里找不到房间 | 这个 build 不应答 A2S | 只能 `connect`，见第 2 节 |
| bot 数值和 `botprofile.db` 对不上 | `bot_stuff.smx` 每 tick 覆盖四个字段 | 这是设计如此，看 `bots` 的"接管"列 |
| 中文输出乱码 | 控制台代码页 cp936 | 工具内部已强制 UTF-8 输出；若仍乱码，`chcp 65001` 或直接看 `tools` 下重定向的文件 |

---

## 8. 文件在哪

| 路径 | 内容 |
|---|---|
| `tools\csgotool.py` | 主 CLI |
| `tools\pipe.py` | 文件管道传输层（`send` / `get_cvar` / `set_cvar`） |
| `tools\skins_catalog.py` | kgns 双语皮肤表解析 |
| `tools\bot_profiles.py` | `botprofile.db` 文本档解析（2187 个 profile） |
| `tools\gamedir.py` | 游戏目录解析：环境变量 → settings.ini → 扫 Steam 库；不写死任何人的路径 |
| `tools\installer.py` | `install` 的引擎：下载、字节/SHA-256 校验、staging、备份、落地、还原 |
| `tools\bots_patch.py` | `bots patch` 的引擎：扫 `botprofile.db` 的 profile 头行，改档位 / 删弱档 block / 换名，备份与还原都记账 |
| `tools\build_exe.py` | 重打包两端 exe + 冒烟自检 + 打印 SHA-256（§9） |
| `tools\secret_scan.py` | 发布闸门：扫工作区 + 全部 git 对象 + 提交元数据里的标识，命中即非零退出 |
| `tools\src\` | **我们自己写的插件源码**（`lan_pipe.sp` / `lan_fix.sp`）；游戏 `addons\sourcemod\scripting\` 里那份只是部署结果，两边已核对为同一份哈希 |
| `release\plugins\` | 我们这两个插件的编译产物（`.smx`），`install` 直接装它们，用户不需要 spcomp |
| `configs\` | **我们写的 cfg 源文件**（`lan.cfg` / `bots_improved.cfg`）+ `components.json`（第三方组件的版本、URL、字节数、SHA-256） |
| `THIRD_PARTY_NOTICES.md` | 哪些东西故意不打包、装的时候从哪来、各自什么许可 |
| `secrets.example.json` | `secret_scan.py` 要用的"我自己的标识"模板；复制成 `.secrets.json`（已 gitignore） |
| `csgotool.cmd` | 双击开循环版窗口（纯 ASCII，透传参数） |
| `lan\join-<IP>.cmd` | 给朋友双击的加入文件（生成的，不入库） |
| `exe\` `dist\` `build\` | PyInstaller 产物（不入库；一条命令重打包并自检：`python tools\build_exe.py`，见 §9） |
| `...\csgo\addons\sourcemod\scripting\lan_fix.sp` | 皮肤/手套实写 + bot 内存探针 + 自动发枪补钱 |
| `...\csgo\addons\sourcemod\scripting\lan_pipe.sp` | 文件管道插件 |
| `...\csgo\addons\sourcemod\data\lan_pipe.txt` | 命令入口（写一行执行一行，执行完清空） |
| `...\csgo\addons\sourcemod\data\lan_exec.log` | 管道执行结果 |
| `...\csgo\addons\sourcemod\data\lan_fix.log` | 插件机器可读输出（皮肤 dump、botmem、autoshow） |
| `...\csgo\addons\sourcemod\logs\errors_YYYYMMDD.log` | SourceMod 错误日志，按天命名 |
| `...\csgo\cfg\lan.cfg` / `bots_improved.cfg` | 联机配置 / bot 与回合参数 |
| `backup\` | 我们自己插件的旧版本（`lan_fix.smx.1.0.5`）、安装过程的还原点（`backup\install\<时间戳>\`）、`bots patch` 的还原点（`backup\bots\<时间戳>\`，里面是改写前的 `botprofile.db` 和当次的 `report.json`）。**Valve 的游戏文件本体（`botprofile.db.orig`、`gameinfo.txt.orig`）、本机视频设置、以及上游没给许可的文件都只留在本机，被 .gitignore 排除**——清单里写明了原因 |

---

## 9. 两个版本：房主端和朋友端

| | 房主端 | 朋友端 |
|---|---|---|
| 文件 | `exe\csgo-server-tool.exe` | `exe\csgo-room-client.exe`（8.2 MB） |
| 谁用 | 只有你（跑游戏这台机器） | 每个朋友自己拷一份 |
| 能做什么 | 全部：开服换图、规则、bot、皮肤、诊断 | 记地址、试连通、**一键进房**、看自己能敲的命令 |
| 权限 | 全权限 | **结构上没有任何写权限** |

朋友端的"不能改 bot / 不能改房间参数 / 不能碰 cfg / 不能开图"不是靠权限检查实现的——那个程序里**根本没有这些代码**：它不 import 管道模块、不含 lan_fix 命令、不碰 SQLite 和 cfg，只用了 `socket` + `os.startfile`。所以就算对方把 exe 反编译了也拿不到能力。它唯一会写的文件是自己旁边的 `room.ini`。

### 怎么发给朋友

房主端菜单 **7 联机与加入 → 8 生成分发包**，或命令行：

```
python tools\csgotool.py kit
```

生成 `dist\房间包-<你的IP>\`，里面三样：

```
进房.exe      客户端（地址已经写进去了，对方不用输）
room.ini      host / port / password / name
说明.txt      双击→按 1 进房；!ws 选皮肤；连不上怎么查
```

**整个文件夹发给对方就行。** 你的局域网 IP 变了要重新生成一次（`kit` 会把当前地址打印出来）。

### 皮肤为什么天然互不干扰

kgns 的皮肤表以 **SteamID 为主键**（`weapons.steamid` / `gloves.steamid`），所以每个人在游戏里用 `!ws` / `!knife` / `!gloves` 选的东西存的是自己那一行，只影响自己的模型。房主也可以用 `skins set <类别> <名> --target <对方的 SteamID2 或 32 位账号 ID>` 替别人改，但那是房主端的权力。

### 重新打包

改了 `.py` 之后要重出 exe。一条命令，PyInstaller 只是这个脚本的依赖、运行时不需要它：

```
python tools\build_exe.py                # 两端都出，出完自己跑一遍冒烟检查
python tools\build_exe.py --only server  # 只出房主端
```

脚本干三件事：确认环境里有 PyInstaller（没有就打印 `pip` 命令，不替你装）→ 用固定的 `--onefile --console --add-data` 参数打包到 `exe\` → **拿产出的 exe 真的跑几条命令**（`--help`、`install --list`、`install --audit --into <一个不存在的路径>`，客户端则喂空 stdin 让菜单画一遍就退出），每项打印 `[ok]` / `[!!]`，任何一项没过退出码 1。最后附每个文件的字节数与 SHA-256，那串哈希就是该写进 Release 说明的东西——下载方能据此判断 exe 有没有在这次构建之后被改过。

参数都收在 `tools\build_exe.py` 的 `TARGETS` 里，别在 README 里手抄命令行。**两个坑已经踩过并处理了**：**不能用 `--noconsole`**（这类程序要读键盘，去掉控制台后双击既没窗口也读不到输入）；`__file__` 在 onefile 下指向临时解压目录，所以输出目录锚在 `sys.executable` 旁边（否则 `join --write-cmd` 生成的文件会随临时目录一起消失）。`shell_ui` 是在函数里 import 的，必须用 `--hidden-import` 显式告诉打包器。

---

## 10. 命令一览

```
shell [--no-header]      常驻交互窗口，编号主菜单 + 可直接敲完整命令
status                 当前对局概览
who                    玩家与 bot 列表
watch [--every N] [--players]   循环盯盘，只在变化时打印
doctor                 管道 / 插件版本 / 错误日志体检
get <cvar>             读参数
set <cvar> <值>        写参数并读回验证（拒绝 sv_lan 1）
exec <命令...>         任意服务器/插件命令
dialog [--pid N]       读游戏弹窗原文（启动失败的 Launcher Error 不用你抄给我）
mods                   列出 csgo.exe 进程里的 mod DLL；一个都没有 = 缺 -insecure
mode                   按 gamemode_*.cfg 的 cvar 指纹判断这局到底是什么玩法
map <图名> [--dry-run] 换图
start [图名] [--launch] [--now] [--kill] [--bots N] [--cfg x.cfg] [--windowed]
       [--uri] [--condebug] [--dry-run]
                       开新局：--launch 直连 csgo.exe 带 -insecure +map（免点菜单，实测有效）；
                       --uri 只把游戏开到主菜单；--windowed 用 1280x720 小窗不抢全屏
join [--write-cmd] [--run] [--ip A] [--port N]
                       局域网地址与网段识别、生成/代执行加入
kit [--ip A] [--port N]
                       生成发给朋友的分发包（进房.exe + room.ini + 说明.txt）
duel [--rounds N] [--money N] [--weapon a,b] [--freeze N] [--bots N]
     [--target humans] [--apply] [--show] [--off]
                       单挑预设
skins list [类别]      53 个武器类别，或某类别可用皮肤
skins search <词>      中英文皮肤名 / paintkit 号
skins set <类别> <名或号> [--wear] [--seed] [--trak] [--force]
skins dump [目标]      读回引擎实体上的真实皮肤
bots [名字] [--detail] [--no-live]
                       配置值 vs 引擎实测值
bots patch [--tier X] [--only A,B] [--names] [--dry-run]
           [--revert [时间戳]] [--backups] [--force]
                       改写本机 botprofile.db 拉平强度：只动 profile 头行，自动备份，可还原（§5）
lan [--bots N] [--password X] [--hostname X] [--cheats] [--no-exec]
                       改写 lan.cfg 并立即生效
cfg gen <名> [--set k=v] [--no-exec]
                       以 bots_improved.cfg 为底生成新 cfg
verify <cfg文件>       对 server.dll/engine.dll 整词校验参数名
gamedir [--set 目录] [--scan]
                       查/固定游戏目录：环境变量 CSGO_LEGACY_DIR → settings.ini → 扫 Steam 库
install [--list] [--check] [--audit] [--dry-run] [--only a,b] [--skip a,b]
        [--no-fetch] [--yes] [--force] [--into 目录] [--accept-upstream-license]
                       按 configs/components.json 下载并安装第三方插件栈：
                       --check 只核对现场，--dry-run 只出计划，装之前全量校验字节与 SHA-256；
                       --audit 是静态自检（缺件/依赖链/bot 数据库覆盖/道具路线），不用开游戏
uninstall [--revert 时间戳] [--into 目录]
                       按那一次 install 的 report.json 还原：删掉它新增的，恢复它换掉的
```


---

## 11. 许可、再分发与发布前自检

**本仓库自己产出的东西**（`tools/*.py`、`tools/src/*.sp`、`release/plugins/*.smx`、`configs/*.cfg`、
`configs/components.json`、文档）按 **GPL-3.0-or-later** 授权，全文见 [LICENSE](LICENSE)。
选它的理由不是跟风：`lan_fix.sp` / `lan_pipe.sp` 是通过 SourceMod 的 include 链编译的，
而 SourceMod、Metamod、PTaH、kgns weapons/gloves、sm-ripext、eItems 全是 GPL-3.0，
插件产物与它们构成同一套作品，用同一个 copyleft 最省事也最不容易出错。

**第三方组件一律保留自己的许可**，本仓库不重新授权它们，也不打包它们——每一项的来源、
版本、许可和被排除的原因都写在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
两个要点：`manicogaming/CSGOBetterBots`（bot 强化那批）**没有 LICENSE 文件**，所以只允许
你自己在 `install` 显式确认后按固定 commit 从上游抓——`botprofile.db` 就是这么抓下来的上游文件，
仓库里不放它；Valve 自己那份原始 `botprofile.db`、`gameinfo.txt` 属于我们无权分发的事物，
只留本机的 `backup\` 里用于还原。`bots patch` 同理：它改写**你自己机器上**的 db，不带任何人的文件。

**发布前跑这三条，别靠眼睛看：**

```
python tools\secret_scan.py --all-objects   # 扫工作区 + 全部 git 对象 + 提交身份
python tools\csgotool.py install --check     # 核对现场与 components.json（0 = 一致）
python tools\csgotool.py install --audit     # 静态自检：坏东西退出码 1，建议不影响
python tools\build_exe.py                    # 两端 exe 能跑，顺带打印要写进 Release 的 SHA-256
```

`--check` 认得 `bots patch` 的产物（哈希在 `backup\bots\*\report.json` 里出现过就算正常），
所以拉平过强度的机器上这条门仍然是绿的——门不该逼你把自定义改回去。

`secret_scan.py` 要查的"我自己的标识"（SteamID64、局域网 IP、主机名、提交邮箱）放在
**被 gitignore 排除**的 `.secrets.json` 里——把待查的值写进扫描器源码，等于用它要保护的
东西污染仓库，所以仓库里只有 `secrets.example.json` 模板。换机器时先复制模板填自己的值。

行尾由 `.gitattributes` 钉死（批处理 CRLF、`.py/.sp/.cfg/.md` LF、`backup/**` 与 `*.orig`
标 `-text` 禁止任何改写），因为备份文件的用途是按字节还原。
