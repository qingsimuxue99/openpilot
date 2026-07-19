# C3 设备网页工具箱 使用教程

## 一、准备

- 设备已刷 carrot / cpv9-dev 分支 openpilot
- 电脑与设备在同一局域网，能 ssh 进设备
- 设备 IP 用 `hostname -I` 查（会随网络变化）

## 二、传文件到设备

在设备 `/data` 下创建 `/data/c3_toolbox/`，把文件放进去：


## 三、启动服务

```bash
cd /data/c3_toolbox
PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3; setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &
```

浏览器打开 `http://设备IP:5588` 即可。连接时会自动创建并扫描备份文件。

## 四、开机自启

让 openpilot 启动时自动拉起工具箱（在设备里执行）：

```bash
grep -q c3_toolbox_autostart /data/openpilot/launch_chffrplus.sh || sed -i '2i bash /data/c3_toolbox/c3_toolbox_autostart.sh &' /data/openpilot/launch_chffrplus.sh
```

重启设备生效：

```bash
sudo reboot
```

撤销自启：

```bash
sed -i '/c3_toolbox_autostart/d' /data/openpilot/launch_chffrplus.sh
```

## 五、更新

本机重新传文件：

```bash
scp c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh comma@设备IP:/data/c3_toolbox/
```

设备里杀旧进程并重启：

```bash
fuser -k 5588/tcp; sleep 1; PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3; cd /data/c3_toolbox; setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &
```

## 六、在线更新

工具箱支持在界面一键在线更新，无需再手动 scp。

**1. 配置更新源（只做一次）**

打开 `c3_toolbox_local.py`，把顶部 `UPDATE_BASE` 改成你自己的仓库地址（GitHub 存放更新文件的 raw 地址）：

```python
UPDATE_BASE = "https://raw.githubusercontent.com/你的用户名/你的仓库/你的分支/"
```

仓库里放这 4 个文件（与本地一致）：`c3_toolbox_local.py`、`c3_toolbox.html`、`c3_toolbox_autostart.sh`、`version.json`（内容如 `{"version":"1.0.1","changelog":"修复xxx"}`）。

**2. 发布新版本**

改完代码推到上面的仓库，并把 `version.json` 里的 `version` 号调高（如 `1.0.0` → `1.0.1`）。

**3. 使用更新**

浏览器打开工具箱 → 「设置」面板底部「在线更新」：点「检查更新」对比版本，有新版点「立即更新」即可自动下载并重启。

> 设备需能访问外网（GitHub）；在线更新会下载并执行代码，请只在信任的仓库下使用。

## 七、常见问题

- 打不开网页：确认 IP 正确；设备里执行 `curl -s http://127.0.0.1:5588/api/backups` 看服务是否在跑。
- 连接不显示备份：设备跑的是旧版，按「五、更新」重部署重启。
- 检查更新失败：确认 `UPDATE_BASE` 已改成正确仓库地址，且设备能联网访问 GitHub；错误信息会显示在更新卡片上。
- flask 缺失：脚本已自动优先用 venv 的 python，回退 python3；若 python3 也没装 flask，先 `pip install flask`。
- 安全：服务无鉴权、终端可执行命令，只在可信局域网内使用，勿暴露公网。

## 八、文件清单

| 文件 | 说明 |
|------|------|
| `c3_toolbox_local.py` | 后端服务（必装） |
| `c3_toolbox.html` | 前端页面（必装） |
| `c3_toolbox_autostart.sh` | 开机自启脚本（必装） |
| `version.json` | 更新源版本清单（随仓库发布） |
| `README.md` | 本教程 |
