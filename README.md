# qingsimuxue99/openpilot @ CP-DEV

CarrotPilot(c3 设备) 的**设备端修复快照分支**。孤儿分支：只有一个根提交，不含任何历史。

用途：给设备上的一键修复脚本 `fix_boot.sh` 提供下载源（jsdelivr CDN），并存放设备常用的关键源码文件。

## 用法

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/qingsimuxue99/openpilot@CP-DEV/fix_boot.sh | bash
```

⚠️ 分支名是 **CP-DEV**（全大写）。CDN 路径必须大小写完全一致，写成 `@CP-Dev` 会 404。

## 为什么要有这个分支

旧版 `fix_boot.sh` 有两个致命缺陷，直接导致朋友们的设备**每次重启都要重新标定**：

1. `BASE` 指向 `qingsimuxue99/openpilot@CP-Dev` —— 这个大写分支不存在（真实分支名是 `cp-dev`），4 个待恢复文件全部 404。
2. `curl` 没有 `-f`。jsdelivr 对 404 会返回 **82 字节的英文错误页**，且 exit code = 0；脚本的守门条件只有 `wc -c > 50`，82 > 50 校验通过 → **错误页被写进源码**。

由此产生的连锁反应：

```
common/params_keys.h 被写成 82 字节错误页
  → 里面没有 {"CalibrationParams", PERSISTENT}
  → common/params.cc 引入该头文件并把它编进 common/params_pyx.so 的 key 表
  → 每次开机 system/manager/manager.py 调 Params.clearAll(CLEAR_ON_MANAGER_START)
  → common/params.cc:198-217 的 clearAll 会把"不在 key 表里"的参数文件 unlink
  → 标定文件每次开机被删 → 重启后从 0 重新标定
  → 特征：只丢标定，其它所有设置都还在
```

本分支同时补上了旧快照缺失的文件（`common/params_keys.h`、`common/params.h`、`common/params.cc`、`selfdrive/ui/qt/offroad/settings.cc` 等），并把 `fix_boot.sh` 升级到 v2。

## 内容

| 路径 | 说明 |
|---|---|
| `fix_boot.sh` | **v2**：多源回退 / `curl -f` / 内容特征校验 / 强制重建 `params_pyx.so` / key 表校验 |
| `common/params_keys.h` | 参数定义，含 `{"CalibrationParams", PERSISTENT}`（标定能跨开机保留的关键） |
| `common/params.h`、`common/params.cc` | 参数系统实现（`clearAll` 的判据在这里） |
| `common/SConscript`、`cereal/SConscript`、`selfdrive/ui/SConscript` | 构建脚本 |
| `selfdrive/ui/qt/offroad/settings.cc` | 设置/激活界面 |
| `selfdrive/ui/carrot.cc` | CarrotPilot UI 源码 |
| `selfdrive/carrot/license.py` | 激活码校验（Pyarmor 混淆） |
| `selfdrive/carrot/carrot_serv.py`、`config.py`、`curve_anticipate.py` | CarrotPilot 纵向/弯道模块 |
| `selfdrive/locationd/calibrationd.py` | 标定进程 |
| `selfdrive/controls/controlsd.py`、`lib/longitudinal_planner.py` | 控制主循环 |
| `system/manager/manager.py` | 管理器（开机调 `clearAll`） |
| `launch_chffrplus.sh`、`launch_env.sh` | 开机启动脚本 |
| `.gitignore`、`RELEASES.md` | 仓库配置与更新说明 |

## 注意

- 本分支**不是完整的 openpilot 树**，只是修复/取文件的快照。请**不要** `git reset --hard` 到它，否则会删掉设备上的其它源码（v2 脚本已加完整性校验来防这件事）。
- 设备正式安装/更新仍走完整仓库 `qsmx9/openpilot@CP-Dev`。
