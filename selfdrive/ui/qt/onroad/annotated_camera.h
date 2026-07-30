#pragma once

#include <QVBoxLayout>
#include <QPainter>
#include <QRect>
#include <memory>
#include "selfdrive/ui/qt/onroad/hud.h"
#include "selfdrive/ui/qt/onroad/buttons.h"
#include "selfdrive/ui/qt/onroad/driver_monitoring.h"
#include "selfdrive/ui/qt/onroad/model.h"
#include "selfdrive/ui/qt/widgets/cameraview.h"
#include "selfdrive/ui/qt/screenrecorder/screenrecorder.h"

class AnnotatedCameraWidget : public CameraWidget {
  Q_OBJECT

public:
  explicit AnnotatedCameraWidget(VisionStreamType type, QWidget* parent = 0);
  void updateState(const UIState &s);

private:
  QVBoxLayout *main_layout;
  ExperimentalButton *experimental_btn;
  DriverMonitorRenderer dmon;
  HudRenderer hud;
  ModelRenderer model;
  std::unique_ptr<PubMaster> pm;

  int skip_frame_count = 0;
  bool wide_cam_requested = false;

  // 驾驶习惯自学习: 行车界面「学习中」徽标
  bool learning_enabled_ = false;      // 总开关 CarrotLearningEnabled 是否开启 (节流读取)
  int  learning_param_frame_ = 0;      // 参数节流读取计数
  void drawLearningBadge(QPainter &p, const QRect &surface_rect, bool engaged);

protected:
  void paintGL() override;
  void initializeGL() override;
  void showEvent(QShowEvent *event) override;
  mat4 calcFrameMatrix() override;

  double prev_draw_t = 0;
  FirstOrderFilter fps_filter;
  void paintEvent(QPaintEvent *event) override;
private:
  ScreenRecoder* recorder;
  std::shared_ptr<QTimer> record_timer;
};
