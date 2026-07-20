# C3 设备网页工具箱 · 使用教程（朋友版）

一个跑在 comma 设备（openpilot）上的**网页管理工具箱**，用手机或电脑浏览器就能调参数、做备份、一键更新，**不用连电脑敲命令**。

---

## 这东西能干嘛

- **参数调优**：把设备参数按分类（carParams / fpParams / jsonParams…）清清楚楚列出来，数值直接填、开关直接点，一键写入设备。
- **参数自动备份**：每次连上设备自动备份当前参数，手滑改错了能一键还原。
- **完整备份 / 恢复**：一键把整个 `/data/openpilot` 打包，换设备 / 救砖 / 搬配置都能用；支持「从设备已有备份包恢复」和「上传本地备份包恢复」两种方式。
- **在线更新**：作者发新版后，网页右上角点一下自动更新，**不用再传文件**。
- **实时画面**：「投屏」= 低延迟 MJPEG 流，把设备屏幕显示的内容原样镜像到手机/电脑（不连摄像头，延迟 ~100ms），支持「横屏/竖屏」一键切换。投屏源三路自动兜底：**cereal `uiDebug.frame`**（openpilot 原生屏幕帧广播，最稳）→ **`/dev/shm` 共享内存扫描** → **ffmpeg(fbdev/kmsgrab)** 回退；即使 ffmpeg 不支持 kmsgrab、无 `/dev/fb0` 也能投屏。「数据 HUD」= 纯数据仪表盘（车速/车道/前车/TTC）。缺 ffmpeg 时点「一键安装」即可，不用手动 ssh。

---

## 支持的分支（重要）

工具箱**不绑定任何特定分支**，跑在 comma 硬件（C3 / C3X 等）上的 openpilot 衍生版都能用：

- **openpilot 原版 / carrot（cpv9-dev 等）**
- **dragonpilot（龙领航，dp）**
- **sunnypilot（阳光领航，sp）**
- **frogpilot（青蛙领航，fp）**

工具箱会**自动识别当前分支**（读 `GitBranch` 参数或 git 信息），在「参数控制」标题旁显示分支标识，并**自动加载该分支的自定义参数中文说明与开关类型**——dp 的 `dp_long`、sp 的 `sp_mads_enabled`、fp 的 `FrogTrafficLight` / `FrogStandState` 等都会显示中文名和说明，不再是一堆英文裸参数。

> 部署 / 升级命令对**所有分支完全一致**（见下方），因为工具箱只依赖通用的 `/data/params/d`、`/data/openpilot` 与硬件分区，不读分支特有结构。

---

## 一、第一次部署到设备

### 准备
- 设备已刷 openpilot 衍生版（openpilot 原版 / carrot / dragonpilot / sunnypilot / frogpilot 均可）。
- 手机或电脑与设备在同一 WiFi，能 SSH 进设备（知道设备 IP）。

### 1. 把这三个文件放进设备
```
/data/c3_toolbox/
├── c3_toolbox_local.py      # 后端服务
├── c3_toolbox.html          # 前端页面
└── c3_toolbox_autostart.sh  # 开机自启脚本
```
> 怎么传都行：电脑 `scp`、adb、U 盘、文件管理器，只要把这三个文件丢进 `/data/c3_toolbox/` 即可。

电脑用 `scp` 的示例（把 `设备IP` 换成你真实 IP，用户名默认 `comma`）：
```bash
scp c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh comma@设备IP:/data/c3_toolbox/
```

### 2. 启动服务（在设备终端里执行）
```bash
cd /data/c3_toolbox
PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3
setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &
```

### 3. 打开网页
浏览器访问 `http://设备IP:5588`（端口 `5588`）。
进页面会自动连设备、扫描并创建参数备份。

> 不知道设备 IP？在设备终端执行 `hostname -I` 看。

---

## 二、开机自启（强烈推荐）

让设备开机自动拉起工具箱，不用每次手动起（在设备终端执行一次）：
```bash
grep -q c3_toolbox_autostart /data/openpilot/launch_chffrplus.sh || \
  sed -i '2i bash /data/c3_toolbox/c3_toolbox_autostart.sh &' /data/openpilot/launch_chffrplus.sh
```
重启设备生效：`sudo reboot`

> **dragonpilot / sunnypilot 注意**：自启靠挂载到 openpilot 的启动脚本。原版 / carrot 是 `launch_chffrplus.sh`；dp / sp 若使用了不同的启动脚本（如 `launch_openpilot.sh` 或各自分支的 launch 脚本），请把上面命令里的 `/data/openpilot/launch_chffrplus.sh` 换成你分支实际的启动脚本路径，否则开机不会自动拉起。

**想取消自启**：
```bash
sed -i '/c3_toolbox_autostart/d' /data/openpilot/launch_chffrplus.sh
```

---

## 三、日常怎么用

### 1. 调参数
- 左侧按分类看参数；数值参数直接填数字，开关参数点一下。
- 改完点「应用」即写入设备；**参数要重启 openpilot 才生效**（设备终端 `sudo reboot` 即可）。
- 顶部「刷新信息 / 刷新参数」可手动刷新。

### 2. 参数自动备份（「备份」标签）
- 每次连上设备自动把当前参数存到 `auto_backup`。
- 改错了，在备份列表选对应备份点「恢复到设备」就还原。

### 3. 完整备份 / 恢复（「备份」标签）
> 适合整搬配置、换设备、救砖。

- **创建完整备份**：点「创建完整备份」，后台执行 `tar` 打包 `/data/openpilot`，进度实时显示，可能要几分钟，耐心等。
- **从设备已有备份包恢复**：下拉选 `/data/备份恢复包openpilot_backup_*.tar.gz` → 点「从设备备份包恢复」。
- **上传本地备份包恢复**：选本地 `.tar.gz` → 点「上传并恢复」，适合把另一台设备的备份搬过来。
- 备份包都能在界面「下载」到电脑留存；恢复前自动停 openpilot，恢复后默认重启（可取消）。

> ⚠️ 完整备份通常几百 MB～1GB+，确保设备 `/data` 剩余空间 ≥ openpilot 目录 1.2 倍。
> ⚠️ 恢复会**覆盖**现有 `/data/openpilot`，操作前确认选对包。

### 4. 在线更新（网页右上角）
- 进入页面就自动检查版本，控件与「刷新信息 / 刷新参数」同一排。
- 发现新版本会显示「发现新版本 X.X.X！」+「查看更新内容 ▾」（展开看本次改了啥）+「立即更新」。
- 点「立即更新」→ 自动下载、覆盖、重启，几秒后页面刷新即完成，**全程不用传文件**。

---

## 四、常见问题

- **打不开网页**：确认 IP 对、端口 `5588`；在设备终端 `curl -s http://127.0.0.1:5588/api/version` 看服务是否活着。
- **连上后没参数 / 没备份**：多半是旧版，点右上角「检查更新」升到最新即可。
- **在线更新没反应 / 失败**：确认设备能联网访问 GitHub 与 jsdelivr（国内 CDN 通常可达）；具体错误会显示在更新控件上。
- **启动报 flask 缺失**：脚本已自动优先用 venv 的 python，回退 python3；若 python3 也没 flask，先 `pip install flask`。
- **安全提醒**：服务无登录鉴权、能在设备内执行命令，**只在可信局域网用，千万别暴露公网**。

---

## 五、文件清单（发给朋友就这几个）

| 文件 | 说明 |
|------|------|
| `c3_toolbox_local.py` | 后端服务（必装） |
| `c3_toolbox.html` | 前端页面（必装） |
| `c3_toolbox_autostart.sh` | 开机自启脚本（必装） |
| `README.md` | 本教程 |

> 注意：`version.json` 是作者发版用的，**不用发给朋友**，设备联网会自动去作者仓库取最新版信息。

---

## 附录：作者发版流程（普通用户可忽略）

工具箱用「版本指针机制」：设备端不写死版本号，下载哪个包由 GitHub 仓库里 `version.json` 的 `tag` / `tarball` 动态指定。发新版本后设备联网自动发现、界面一键更新，**设备端代码不用改**。

发版步骤（本机 Git Bash）：
```bash
# 1. 改代码后，把 version.json 的 version 调高（如 1.0.11 -> 1.0.12），
#    并同步把其中 tag / tarball 字段指向上一步要打的“全新” tag 名
# 2. 同步把 c3_toolbox_local.py 里的 VERSION 常量升到同一版本号
# 3. 打包发布包
tar -czf release/c3_toolbox.tar.gz c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh
# 4. 提交推送到 c3-toolbox 分支
git add -A && git commit -m "release: v1.0.12" && git push
# 5. 打“全新” tag 并推送
git tag v1.0.12 && git push origin v1.0.12
```

> ⚠️ **铁律：每次发版必须用「全新」tag 名（如 v1.0.12），绝不复用旧 tag**。
> jsdelivr 对 tag 名做不可变缓存，同名 tag 即使内容变了 CDN 仍可能长期喂旧包，导致“更新了却还是旧版”。
> 另：发现最新版已改为优先直连 GitHub API（推送 tag 后立即可见），jsdelivr 仅作回退，根治了之前“发布后点更新还显示已是最新”的索引延迟问题。
