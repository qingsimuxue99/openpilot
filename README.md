# C3 设备网页工具箱 · 使用教程

一个运行在 comma 设备（openpilot）上的网页工具箱，用浏览器就能管理设备，**电脑和手机都能用**。

功能一览：
- **参数调优**：把设备参数按分类（carParams / fpParams / jsonParams 等）清晰展示，可在线编辑并写入设备，重启生效。
- **参数自动备份**：连接设备时自动备份当前参数，误改可一键恢复。
- **完整备份 / 恢复**：一键把 `/data/openpilot` 整体打包成备份包；可**从设备内已有备份包**恢复，也可**上传本地备份包**恢复。
- **在线更新**：作者发新版本后，在网页右上角点一下即可自动更新，无需手动传文件。

---

## 一、部署到设备（第一次用）

### 1. 前提
- 设备已刷 openpilot（carrot / cpv9-dev 等分支）。
- 电脑与设备在同一局域网，能 SSH 进设备（知道设备 IP）。

### 2. 把文件传到设备
在设备里新建目录并把三个文件放进去：

```
/data/c3_toolbox/
├── c3_toolbox_local.py      # 后端服务
├── c3_toolbox.html          # 前端页面
└── c3_toolbox_autostart.sh  # 开机自启脚本
```

> 传文件方式任选：电脑用 `scp`、或 adb、或任意文件管理器把这三个文件拉进 `/data/c3_toolbox/` 即可。

如果用电脑 `scp`（把下面 `设备IP` 换成你设备的真实 IP）：

```bash
scp c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh 用户名@设备IP:/data/c3_toolbox/
```

### 3. 启动服务（在设备终端执行）

```bash
cd /data/c3_toolbox
PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3
setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &
```

### 4. 打开网页
浏览器访问 `http://设备IP:5588`（端口 `5588`）。
进页面后会自动连接设备、扫描并创建参数备份。

> 不知道设备 IP？在设备终端执行 `hostname -I` 或 `ifconfig` 查看。

---

## 二、开机自启（推荐，省得每次手动起）

让 openpilot 开机时自动拉起工具箱（在设备终端执行一次）：

```bash
grep -q c3_toolbox_autostart /data/openpilot/launch_chffrplus.sh || \
  sed -i '2i bash /data/c3_toolbox/c3_toolbox_autostart.sh &' /data/openpilot/launch_chffrplus.sh
```

重启设备生效：`sudo reboot`

**撤销自启**：
```bash
sed -i '/c3_toolbox_autostart/d' /data/openpilot/launch_chffrplus.sh
```

---

## 三、功能使用

### 1. 参数调优
- 左侧按分类查看参数（如 `carParams`、`fpParams`、`jsonParams`…）。
- 改完点「应用」即写入设备；**参数需重启 openpilot 才生效**（可在设备终端 `sudo reboot`，或停掉 manager 再起）。
- 顶部「刷新信息 / 刷新参数」可手动刷新。

### 2. 参数自动备份（「备份」标签内）
- 连接设备时自动把当前参数备份到 `auto_backup` 目录。
- 误改参数后，在「备份」列表里选对应备份点「恢复到设备」即可还原。

### 3. 完整备份 / 恢复（「备份」标签内，重点）
> 用于整体搬迁 / 救砖 / 换设备时保留整套 openpilot 配置。

**① 创建完整备份**
- 点「创建完整备份」，工具箱后台执行：
  `tar -zcvf /data/备份恢复包openpilot_backup_<时间戳>.tar.gz /data/openpilot`
- 打包可能要几分钟，界面会实时显示进度，请耐心等待。

**② 从设备内已有备份包恢复**
- 下拉选择 `/data/备份恢复包openpilot_backup_*.tar.gz` → 点「从设备备份包恢复」。
- 执行：`tar -zxvf <备份包> -C /`

**③ 上传本地备份包恢复**
- 选本地 `.tar.gz` 备份包 → 点「上传并恢复」。
- 适合把另一台设备的备份搬到当前设备。

**④ 其他**
- 备份包可在界面直接「下载」到电脑留存。
- 恢复前会自动停止 openpilot，恢复后默认重启设备（确保干净加载），可在界面取消勾选。

> ⚠️ 完整备份体积较大（通常几百 MB ~ 1GB+），请确保设备 `/data` 剩余空间 ≥ openpilot 目录大小的 1.2 倍。
> ⚠️ 恢复会**覆盖**现有 `/data/openpilot`，操作前确认选对备份包。

### 4. 在线更新（网页右上角）
- 右上角与「刷新信息 / 刷新参数」同排的「在线更新」控件，进入页面即自动检查版本。
- 发现新版本会显示「发现新版本 X.X.X！」+「查看更新内容 ▾」（展开本次更新说明）+「立即更新」。
- 点「立即更新」→ 自动下载、覆盖、重启，几秒后页面刷新即完成，**无需手动传文件**。

---

## 四、常见问题

- **打不开网页**：确认 IP 正确、端口是 5588；在设备终端执行 `curl -s http://127.0.0.1:5588/api/version` 看服务是否在跑。
- **连上后看不到参数 / 备份**：多半设备跑的是旧版，参照「在线更新」点一下更新到最新即可。
- **在线更新失败 / 检查更新无响应**：确认设备能联网访问 jsdelivr（国内 CDN 通常可达）；具体错误会显示在更新控件上。
- **启动报 flask 缺失**：脚本已自动优先用 venv 的 python，回退 python3；若 python3 也没装 flask，先 `pip install flask`。
- **安全提醒**：服务无登录鉴权，且能在设备内执行命令，**仅在可信局域网内使用，切勿暴露到公网**。

---

## 五、文件清单

| 文件 | 说明 |
|------|------|
| `c3_toolbox_local.py` | 后端服务（必装） |
| `c3_toolbox.html` | 前端页面（必装） |
| `c3_toolbox_autostart.sh` | 开机自启脚本（必装） |
| `README.md` | 本教程 |

---

## 附录：开发者自行发布新版本（仅供作者，普通用户可忽略）

工具箱采用**版本指针机制**：设备端不写死任何版本号，下载哪个包由 `version.json` 的 `tag` / `tarball` 字段动态指定。作者发新版本后，设备联网会自动发现并可在界面一键更新，**设备端代码无需改动**。

发布步骤（在本机 Git Bash 执行）：

```bash
# 1. 改完代码后，把 version.json 的 version 调高（如 1.0.9 -> 1.0.10），
#    并同步把其中的 tag / tarball 字段指向上一步要打的“全新” tag 名
# 2. 同步把 c3_toolbox_local.py 里的 VERSION 常量升到同一新版本号
# 3. 重新打包发布包
tar -czf release/c3_toolbox.tar.gz c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh
# 4. 提交并推送到 c3-toolbox 分支
git add -A && git commit -m "release: v1.0.10" && git push
# 5. 打“全新” tag 并推送
git tag v1.0.10 && git push origin v1.0.10
# 6. 验证：数据 API 约 1 分钟内索引到新 tag，设备随后自动发现
curl -s "https://data.jsdelivr.com/v1/package/gh/qingsimuxue99/openpilot" | head -c 160
```

> ⚠️ **关键铁律：每次发版必须用「全新」的 tag 名（如 v1.0.10），绝不复用旧 tag（如再打 v1.0.9）**。
> jsdelivr 对 tag 名做**不可变缓存**：同名 tag 即使内容已变，CDN 仍可能长期返回旧包，导致设备“更新了却还是旧版”。
> 另：分支引用（`@c3-toolbox`）与 `@latest` 浮动引用也有强缓存且 purge 常失效，不要依赖；真正可靠的是“数据 API 实时发现最新 tag + 按具体 tag 下载”，本工具箱已实现该机制。
