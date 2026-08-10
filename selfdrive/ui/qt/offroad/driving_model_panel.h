#pragma once
#include <QWidget>
#include <QVBoxLayout>
#include "selfdrive/ui/qt/widgets/controls.h"

// 驾驶模型选择器 (纯本地): 入口页 + 全屏 overlay 二级弹窗 (参考自学习记录弹窗框架)
// 弹窗内列表 1:1 跟手滚动 (FollowScrollArea), 点击模型 → 确认 → 写 modelid → 重启设备生效
// 不做在线下载/删除/磁盘占用/进度显示
class DrivingModelPanel : public QWidget {
  Q_OBJECT
public:
  explicit DrivingModelPanel(QWidget *parent = nullptr);

private:
  void openOverlay();          // 打开全屏二级弹窗 (列表独立滚动, 不嵌套外层 ScrollView)
  void buildModelList(QWidget *listW);  // 重建弹窗内模型列表
  QVBoxLayout *layout_ = nullptr;
};
