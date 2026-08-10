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

  // 驾驶习惯自学习: 行车界面状态框 (紧凑可展开)
  bool learning_enabled_ = false;      // 总开关 CarrotLearningEnabled 是否开启 (节流读取)
  int  learning_param_frame_ = 0;      // 参数节流读取计数
  bool learning_box_expanded_ = false; // 状态框展开/收起
  QRect learning_hit_rect_;            // 当前可点击区域(收起药丸或展开面板), 供 mousePressEvent 命中判定

  // HUD 解析缓存 (来自 Param CarrotLearningHud, '|' 分隔)
  int hud_rate_ = 5;
  struct HudApplied { QString name; int val; int delta; };
  struct HudLearn   { QString name; float cur; float target; };
  QVector<HudApplied> hud_applied_;
  QVector<HudLearn>   hud_learning_;

  void drawLearningBox(QPainter &p, const QRect &surface_rect, bool engaged);
  void parseHud(const QString &raw);

  // STOPPED 计时器 (ported from sunnypilot)
  bool standstill_timer_enabled_ = true;   // TODO: 可接入 Params 开关
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
