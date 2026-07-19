# C3 设备网页工具箱 使用教程

## 一、准备

- 设备已刷 carrot / cpv9-dev 分支 openpilot
- 电脑与设备在同一局域网，能 ssh 进设备
- 设备 IP 用 `hostname -I` 查（会随网络变化）

## 二、传文件到设备

本机（Windows 用 Git Bash）把文件传到设备：

```bash
scp c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh comma@设备IP:/data/c3_toolbox/
```

> 连不上 ssh 时改 adb：`adb push c3_toolbox_local.py /data/c3_toolbox/`（其余文件同理）。


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

工具箱支持在界面一键在线更新（作者已把更新源配好，部署后自动可用）。

**使用者**：浏览器打开工具箱 → 「设置」面板底部「在线更新」→ 点「检查更新」对比版本，有新版点「立即更新」即可自动下载、覆盖并重启。设备需能联网访问 jsdelivr（国内可达）。

**作者发布新版本**（在本机 Git Bash 执行）：

```bash
# 1. 改完代码后，把 version.json 的 version 调高（如 1.0.0 -> 1.0.1）
# 2. 重新打包发布包
tar -czf release/c3_toolbox.tar.gz c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh
# 3. 提交并推送到 c3-toolbox 分支
git add -A && git commit -m "release: v1.0.1" && git push
# 4. 强制刷新 jsdelivr 缓存（否则设备端短时间可能拉到旧包）
curl -s "https://purge.jsdelivr.net/gh/qingsimuxue99/openpilot@c3-toolbox/release/c3_toolbox.tar.gz"
curl -s "https://purge.jsdelivr.net/gh/qingsimuxue99/openpilot@c3-toolbox/version.json"
```

> 注意：设备端必须先部署含更新接口的最新版（v1.0.0+）；若设备跑的是更早的版本，需先按「五、更新」手动 scp 一次，之后即可在线更新。

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
