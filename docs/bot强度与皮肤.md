# bot 强度与皮肤

← 回到 [README](../README.md)

## 看 bot 到底什么水平

```
python tools\csgotool.py bots
python tools\csgotool.py bots ropz --detail
python tools\csgotool.py bots --no-live
```

输出把**配置值**（`botprofile.db` 沿 `Default → 模板链 → 自己覆盖` 算出来的应有值）和**实测值**
（从进程里读回的、引擎真正在用的值）并排列出，最后一列告诉你这个 bot 有没有被 `bot_stuff.smx` 接管。

怎么读：

- **档位**：`ProP / Elite / Expert / VeryHard / Hard / Tough / Normal / Fair`，
  对应 `Skill 100→25`、`ReactionTime 0.15→0.50`。
- **接管列**：名字在 `data/bot_info.json`（1409 个职业 ID）里的，插件每回合写
  `反应 0.165–0.325 / 进攻 0–1 / 甩狙加速 4000–7000` 的随机值；五个
  `sh1ro / ZywOo / ropz / donk / m0NESY`（**大小写敏感**）则是 `100000 / 0.0 / 1.0`。
  名字不在表里的，插件不管，库里多少就是多少。
- **实测反应 0.000** 只可能出现在那五个人名上，那不是"职业水平"，是人类不可能。
- **`--detail`** 逐字段给出 `库里值 / 引擎实测 / 结构体偏移 / 判定`。偏移表是实测出来的：
  拿一个不在 bot_info 里的 bot 当对照组，17 个字段全部与配置一致。
- **`bot_difficulty` 基本无效**：这份文件里所有档位都声明支持全部难度，引擎的难度过滤永远不生效，
  强度取决于**抽到哪个名字**。

## 把强度拉平：`bots patch`

抽卡的根子有两条，都在**你本机那份 `botprofile.db`** 里，所以这个命令改写它，
而不是发一份新的给你（Valve 的原文件和 repack 的改写版都不入库）：

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

实现上的边界，都是先验证再动手的：

- **只动 profile 的头行**：换链首的档位 token、或换名字。`Default` / `Template` 块与所有数值行都不碰。
  依据是实测两份 db（Valve 那份 147 个 block、repack 那份 2187 个）里**没有任何 profile 自己覆盖
  `Skill` 或 `ReactionTime`**，所以改头行就够；档位也永远在链首，不需要猜位置。
- 行尾（CRLF/LF）、缩进、行尾注释按原样保留；**计划算完和写盘之间文件被改动过会拒绝落盘**。
- 幂等：拉平之后再跑，报"没有要改的"。
- 换名用**表里的原样大小写**（`bot_stuff` 有些比较是 `strcmp`），并且不会把
  `sh1ro / ZywOo / ropz / donk / m0NESY` 这五个发出去——那五个会拿到 `100000 / 0.0 / 1.0`，
  0 秒反应不是人类水平，拉平强度不该顺带造出这种 bot。
- `--only` 若会把名字池清空（指定档位里一个 block 都没有）直接拒绝，并告诉你现有什么档位。
- 还原会比对 `report.json` 里记录的产物哈希：你自己又动过这份 db 就拒绝还原，
  确认要覆盖才加 `--force`。
- `--names` 依赖 `data/bot_info.json`，那是 `install` 装 `botpack` 时放进去的；没有它会报错
  让你先装，不会静默什么都不做。
- 改完要开新的一局才生效（bot 的名字与数值在生成时定）。
- `install --check` 认得这份改写：哈希在 `backup\bots\*\report.json` 里出现过就算"正常"，
  发布门不会被自己拉平的 db 弄红。

还有一层是数据本身决定的：`botprofile.db` 换成上游那份之后，`bot_stuff` 的开关才会真的亮起
（Valve 默认名字一个都不在 `bot_info.json` 里）。见 [安装与自检](安装与自检.md)。

## 皮肤

### 查

```
python tools\csgotool.py skins search 血腥
python tools\csgotool.py skins search Bloodsport
python tools\csgotool.py skins list awp --limit 60
```

工具把 kgns 的 `weapons_schinese.cfg` 和 `weapons_english.cfg` **按 paintkit 号合并**成一本双语表
（1075 个皮肤，两边 id 集合与适用武器完全一致，只有 22 个未翻译），所以中文名和英文名都能搜，
结果形如 `639 血腥运动 / Bloodsport ak47`。

### 换

```
python tools\csgotool.py skins set awp 血腥运动
python tools\csgotool.py skins set ak47 639 --wear 0.42 --seed 1000 --trak 1
python tools\csgotool.py skins set knife_karambit 415
```

- 第二个参数既可以是 paintkit 号，也可以是皮肤名（中/英）。
- 一个名字可能对应多个号（血腥运动在 ak47/scar20/mp7 上是三个不同的 paintkit），工具会列出来让你选。
- **类别守卫**：皮肤表里没登记给该武器的号，工具会拒绝并告诉你它属于哪些枪
  （引擎不会报错，只会渲染成默认皮肤）。确信要塞就加 `--force`。
- `--wear` 省略时**不动**已存的 float（这是刻意的：早期版本省略它会静默把你的 float 写成 0）。
- 刀要用 `knife_<型号>`（`knife_karambit`、`knife_butterfly`…）。裸 `knife` 列是 kgns 的
  **刀模型选择器**不是 paintkit，写进去会毁掉你的刀型选择，插件直接拒写。
- 改完皮肤只在下次出生时生效，`sv_cheats 1` 时 `kill` 最快。
- 刀的皮肤要先在**单独的刀模型菜单**里选过型号，`!ws` 里那行刀才会解锁（kgns 把刀行设成
  `ITEMDRAW_DISABLED` 直到你选过型号），皮肤本身来自 `configs/weapons/weapons_<lang>.cfg`。

### 看现场

```
python tools\csgotool.py skins dump
python tools\csgotool.py skins dump --target Ro1f
```

读回的是引擎里实体上的真实值（paintkit / wear / seed / stattrak / quality），不是数据库里的期望值。

### 每个人的皮肤互不干扰

kgns 的皮肤表以 **SteamID 为主键**（`weapons.steamid` / `gloves.steamid`），所以每个人在游戏里用
`!ws` / `!knife` / `!gloves` 选的东西存的是自己那一行，只影响自己的模型。房主也可以用
`skins set <类别> <名> --target <对方的 SteamID2 或 32 位账号 ID>` 替别人改，但那是房主端的权力。

给别人定位时用 **Steam 账号 ID**（`userdata\<n>` 那个数字 = SteamID64 的低 32 位），
不要用 `STEAM_1:0:<n>` 尾巴；bot 的 account 是 0，所以**按名字**给 bot 定位。
