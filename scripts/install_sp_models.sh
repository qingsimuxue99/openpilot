#!/usr/bin/env bash
# ===== 安装后一键下载 5 个 SP 驾驶模型 =====
# 用法: bash /data/openpilot/scripts/install_sp_models.sh
# 下载源: models/sp_models.json (sunnypilot 官方清单, gitlab)
# 特性: 断点续传 (中断后重跑即可继续); 下载完自动显示模型列表
set -e
cd /data/openpilot

# 5 个 SP 模型 (与模型菜单 SP 列表一致): wmiv12 / tr16 / ltr14 / wmiv9 / tr15
SP_MODELS="wmiv12 tr16 ltr14 wmiv9 tr15"

echo "===== 开始下载 SP 模型: $SP_MODELS ====="
for m in $SP_MODELS; do
  echo "===== [$m] 开始 $(date +%H:%M) ====="
  # 900s 超时 + 断点续传 (manager 内部支持 chunk 续传)
  timeout 900 /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py download "$m" \
    && echo "===== [$m] 完成 $(date +%H:%M) =====" \
    || echo "===== [$m] 中断/超时 (重跑本脚本可断点续传) ====="
done

echo
echo "===== 全部下载流程结束, 当前模型列表: ====="
/usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py list
echo
echo "完成! 打开 [萝卜-开始-模型选择] 即可看到并切换 SP 模型。"
