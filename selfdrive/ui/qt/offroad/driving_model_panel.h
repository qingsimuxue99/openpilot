#pragma once
#include <QWidget>
#include <QVBoxLayout>
#include "selfdrive/ui/qt/widgets/controls.h"

// 驾驶模型选择器 (纯本地): 打开显示本地全部模型列表 (SP/CP 前缀标注)
// 点击模型 → 确认 → 写 modelid → 重启设备生效
// 不做在线下载/删除/磁盘占用/进度显示
class DrivingModelPanel : public QWidget {
  Q_OBJECT
public:
  explicit DrivingModelPanel(QWidget *parent = nullptr);
  void showEvent(QShowEvent *event) override;

private:
  void refresh();
  QVBoxLayout *layout_ = nullptr;
};
