#pragma once
#include <QWidget>
#include <QVBoxLayout>
#include "selfdrive/ui/qt/widgets/controls.h"

class QTimer;
class QLabel;

// 驾驶模型选择器: 参考「选择车型」框架 — 蓝色大按钮入口 + 弹窗选择
// 1) 当前模型: 点击弹出本地模型列表选择切换
// 2) 在线模型: 点击弹出在线模型库选择下载 (最新在前, 已安装的过滤)
// 3) 删除模型: 点击弹出本地模型列表选择删除 (当前激活项不可删)
// 4) 信息卡: 本地模型/磁盘占用/下载进度, QTimer 每 2s 实时刷新进度
class DrivingModelPanel : public QWidget {
  Q_OBJECT
public:
  explicit DrivingModelPanel(QWidget *parent = nullptr);
  void showEvent(QShowEvent *event) override;

private:
  void refresh();
  QVBoxLayout *layout_ = nullptr;
  QTimer *timer_ = nullptr;
  QLabel *progress_ctl_ = nullptr;
};
