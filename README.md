# csgo-lan-range · CS:GO Legacy 本地与局域网工具箱

把一台机器上的 CS:GO Legacy 变成能自己装插件、能拉平 bot 强度、能带朋友开房的本地服务器。
详细操作在 [docs/](docs/)，这一页只讲"clone 之后怎么跑到能玩"。

- 对象：`tools\csgotool.py`（配套 `pipe.py` / `bot_profiles.py` / `bots_patch.py` / `skins_catalog.py`，
  自写插件 `lan_fix.smx` 1.2.3 + `lan_pipe.smx` 1.2.0）
- 环境：CS:GO Legacy（Steam appid 4465480，build 1.38.8.1 / 13881，32 位），本机 listen server。
  与 Valve 无关联，不适用于官方匹配或别人的服务器。
- 依赖：**只有 Python 标准库**。不想装 Python 就用打包好的 exe（[docs/打包与发布](docs/打包与发布.md)）。
- 下面所有命令都在 **clone 目录里**执行。想在任意目录调用，把 `tools\csgotool.py` 换成绝对路径。

## 五分钟上手

```
python tools\csgotool.py gamedir        # 1 游戏目录解析到哪了
python tools\csgotool.py install        # 2 装第三方插件栈（先 --dry-run 看一眼也行）
python tools\csgotool.py bots patch --names   # 3 拉平每局 bot 强度（改本机 db，自动备份）
python tools\csgotool.py mods           # 4 开局之后：8 个 mod DLL 都该在
python tools\csgotool.py doctor         # 5 管道应答、插件版本、错误日志
```

**第 1 步失败就先读它。** 工具里不内置任何人的安装路径，它自己找：注册表
`Software\Valve\Steam` → 各 Steam 库的 `libraryfolders.vdf` → `appmanifest_4465480.acf` 的
`installdir` → `<库>\steamapps\common\<安装名>\csgo`；判定标准是该目录里有 `gameinfo.txt`
且上一级有 `csgo.exe`。没找到就 `gamedir --scan` 看它扫过哪些库，再
`gamedir --set "<那个 csgo 目录>"` 固定（写进 `settings.ini`，也可用环境变量 `CSGO_LEGACY_DIR`）。

**第 2 步之后必须做一次人肉动作**：Steam 里给这个 app 的启动选项写 `-insecure`。
`install` 结束会打印一份带序号的首跑清单，从这一步一路列到"怎么验证"，照着做完就能玩。

## 五条边界（踩过才写下来的）

1. **只用于本地和局域网自娱。** 皮肤靠伪造 netprop、bot 靠改内存偏移，在官方匹配或别人服务器上
   都是找封。UDP 27015 不要往公网映射——文件管道等价于本机的任意命令执行通道。
2. **`-insecure` 是这套东西的存在前提，不是可选项。** VAC 安全会话里引擎**完全不加载** addons
   下的未签名 DLL：对局看起来一切正常，只是没有皮肤、没有 bot 强化、工具连不上。
   判据是 `csgotool.py mods`（不是端口占用，客户端自己就绑 27015）。
3. **自动开局能成立，但开出来的不是竞技局。** `start <图> --launch` 直连起局、mod 全加载，
   可是玩法由主菜单创建 session 时决定，`game_type`/`game_mode` 只是信息位的，写多少遍都改不掉。
   要竞技局就点一次 开始游戏 → 与电脑玩家，之后全自动。判据命令是 `csgotool.py mode`。
   全部实测过程见 [docs/开局与自动启动](docs/开局与自动启动.md)。
4. **不要把 `sv_lan` 设成 1。** 那会让所有人拿不到 SteamID，而皮肤表按 SteamID 存、
   认证失败即返回——所有人的皮肤、手套、刀全部丢失且不会保存。工具会拒绝，除非 `--force`。
5. **游戏目录解析不出来时不要瞎跑。** `gamedir` / `install` / `uninstall` 三条在没目录时也能用，
   其余命令会打印修复方法并以 2 退出，不会拿空路径去拼文件名。

## 常用命令速查

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

## 不想反复敲命令：常驻交互窗口

```
python tools\csgotool.py shell          # 或双击 csgotool.cmd
```

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

菜单不是第二套实现：所有动作最终都交给同一个 `main()`，所以 shell 和命令行不可能行为分叉。
子界面的表单细节见 [docs/交互窗口](docs/交互窗口.md)。

## 文档索引

| 文档 | 讲什么 |
|---|---|
| [docs/安装与自检](docs/安装与自检.md) | `install` 的次序与校验、`--check` / `--audit` / `--dry-run` 的区别、保留名单、同意门、首跑清单、还原 |
| [docs/开局与自动启动](docs/开局与自动启动.md) | 直连启动的两个必需条件、为什么开出来不是竞技局、为什么没有真·后台开服、appid 那道坎 |
| [docs/局域网联机](docs/局域网联机.md) | 开房叫朋友、三种连接写法、连不上的排查顺序、密码房、分发包、房主端与朋友端的权限差别 |
| [docs/bot强度与皮肤](docs/bot强度与皮肤.md) | `bots` 读数怎么解释、`bots patch` 的实现边界、皮肤查/换/读回、刀的特殊性、皮肤为何互不干扰 |
| [docs/规则与配置](docs/规则与配置.md) | `duel` 每一项干什么、cvar 的真实语义陷阱、`verify` 如何从 DLL 里保证参数名可信 |
| [docs/故障排查](docs/故障排查.md) | 症状 → 原因 → 处理 一张表，以及"该按什么顺序排除" |
| [docs/命令参考](docs/命令参考.md) | 全部命令与参数，按主题指回上面的文档 |
| [docs/文件与目录](docs/文件与目录.md) | 仓库里每个路径是什么、哪些只留在本机、游戏目录里的运行时文件 |
| [docs/交互窗口](docs/交互窗口.md) | `shell` 的菜单、表单、分页选择器规则 |
| [docs/打包与发布](docs/打包与发布.md) | `build_exe.py`、发布前四条门、`.secrets.json` 的用意、行尾策略 |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | 哪些东西故意不打包、装的时候从哪来、各自什么许可 |

## 许可

本仓库自己产出的东西按 **GPL-3.0-or-later** 授权，全文见 [LICENSE](LICENSE)。
第三方组件一律保留自己的许可、也不打包进仓库，来源与排除理由见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；发布前的自检门见
[docs/打包与发布](docs/打包与发布.md)。
