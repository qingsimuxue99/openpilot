#pragma once

#include <QVBoxLayout>
#include <QPainter>
#include <QRect>
#include <QMouseEvent>
#include <QVector>
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

  // STOPPED 静止计时器
  bool standstill_timer_enabled_ = true;
  bool is_standstill_ = false;
  float standstill_elapsed_ = 0.0f;
  void drawStoppedTimer(QPainter &p, const QRect &surface_rect);

protected:
  void paintGL() override;
  void initializeGL() override;
  void showEvent(QShowEvent *event) override;
  void mousePressEvent(QMouseEvent *event) override;
  mat4 calcFrameMatrix() override;

  double prev_draw_t = 0;
  FirstOrderFilter fps_filter;
  void paintEvent(QPaintEvent *event) override;
private:
  ScreenRecoder* recorder;
  std::shared_ptr<QTimer> record_timer;
};
