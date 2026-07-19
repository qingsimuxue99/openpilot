# C3 设备网页工具箱 使用教程

## 一、准备

- 设备已刷 carrot / cpv9-dev 分支 openpilot
- 电脑与设备在同一局域网，能 ssh 进设备

## 二、传文件到设备

在c3设备内的data目录下新建/data/c3_toolbox文件夹，然后把四个文件除了README.md，拉进/data/c3_toolbox下

## 三、启动服务

ssh终端输入以下命令 

cd /data/c3_toolbox
PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3; setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &


浏览器打开 `http://设备IP:5588` 即可。连接时会自动创建并扫描备份文件。

## 四、开机自启

让 openpilot 启动时自动拉起工具箱（在设备里执行）：


grep -q c3_toolbox_autostart /data/openpilot/launch_chffrplus.sh || sed -i '2i bash /data/c3_toolbox/c3_toolbox_autostart.sh &' /data/openpilot/launch_chffrplus.sh


重启设备生效：


sudo reboot


撤销自启：


sed -i '/c3_toolbox_autostart/d' /data/openpilot/launch_chffrplus.sh


## 五、更新

在备份菜单内 拉到底在线更新。

设备里杀旧进程并重启：

fuser -k 5588/tcp; sleep 1; PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3; cd /data/c3_toolbox; setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &


## 六、在线更新

工具箱支持在界面一键在线更新（作者已把更新源配好，部署后自动可用）。

**使用者**：浏览器打开工具箱 → 「备份」标签内「在线更新」卡片 → 点「检查更新」对比版本，有新版点「立即更新」即可自动下载、覆盖并重启。设备需能联网访问 jsdelivr（国内可达）。

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


## 七、常见问题

- 打不开网页：确认 IP 正确；设备里执行 `curl -s http://127.0.0.1:5588/api/backups` 看服务是否在跑。
- 连接不显示备份：设备跑的是旧版，按「五、更新」重部署重启。
- 检查更新失败：确认设备能联网访问 jsdelivr（国内 CDN）；错误信息会显示在更新卡片上。
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
