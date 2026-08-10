#pragma once
#include <QWidget>

// 驾驶模型选择器: 全屏 overlay 二级弹窗 (模型列表, SP/CP 前缀, 点击切换→重启设备)
// 入口: 萝卜菜单"开始"标签 (车型选择下方); 弹窗列表 1:1 跟手滚动 (整屏可滑 + 点击生效)
void showModelOverlay(QWidget *parent);
