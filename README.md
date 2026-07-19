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

手动更新：把本机最新文件 scp 到设备 `/data/c3_toolbox/` 后，在设备里杀旧进程并重启（见下）。

> 若只是发新版本给使用者，无需手动 scp——使用者在网页右上角「在线更新」控件点「立即更新」即可（见第六章）。

fuser -k 5588/tcp; sleep 1; PYP=/usr/local/venv/bin/python; [ -x "$PYP" ] || PYP=python3; cd /data/c3_toolbox; setsid "$PYP" /data/c3_toolbox/c3_toolbox_local.py > /data/c3_toolbox/server.log 2>&1 < /dev/null &


## 六、在线更新

工具箱支持在界面一键在线更新（**版本指针机制**：设备端不写死任何 tag，下载哪个包由 `version.json` 的 `tag`/`tarball` 字段动态指定，发版后设备自动发现，无需改设备代码）。

**使用者**：浏览器打开工具箱 → 右上角「在线更新」控件（与「刷新信息／刷新参数」同排）→ 进入即自动检查，发现新版本会显示「发现新版本 X.X.X！」+「查看更新内容 ▾」（展开本次更新说明）+「立即更新」按钮，点一下自动下载、覆盖、重启。

**作者发布新版本**（在本机 Git Bash 执行）：

```bash
# 1. 改完代码后，把 version.json 的 version 调高（如 1.0.7 -> 1.0.8），
#    并同步改其中的 tag / tarball 字段指向上一步要打的新 tag 名（务必是“全新”tag）
# 2. 重新打包发布包（VERSION 常量也要升到同一新版本号）
tar -czf release/c3_toolbox.tar.gz c3_toolbox_local.py c3_toolbox.html c3_toolbox_autostart.sh
# 3. 提交并推送到 c3-toolbox 分支
git add -A && git commit -m "release: v1.0.8" && git push
# 4. 打“全新” tag 并推送（⚠️ 见下方警告，绝不复用旧 tag 名）
git tag v1.0.8 && git push origin v1.0.8
# 5. 验证：数据 API 会在约 1 分钟内索引到新 tag，设备随后自动发现
curl -s "https://data.jsdelivr.com/v1/package/gh/qingsimuxue99/openpilot" | head -c 160
```

> ⚠️ **关键铁律：每次发版必须用「全新」的 tag 名（如 v1.0.8），绝不复用旧 tag（如再次打 v1.0.7）**。
> jsdelivr 对 tag 名做**不可变缓存**：同名 tag 即使内容已变，CDN 仍可能长期返回旧包，导致设备"更新了却还是旧版"。
> 另：分支引用（`@c3-toolbox`）和 `@latest` 浮动引用也有强缓存且 purge 常失效，**不要依赖**；
> 真正可靠的是"数据 API 实时发现最新 tag + 按具体 tag 下载"，本工具箱已实现该机制。


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
