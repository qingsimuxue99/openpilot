#include "selfdrive/ui/qt/onroad/annotated_camera.h"

#include <QPainter>
#include <QFontMetrics>
#include <algorithm>
#include <cmath>
#include <unistd.h>
#include <exception>
#include <iostream>
#include <execinfo.h>

#include "common/swaglog.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/carrot.h"

// Window that shows camera view and variety of info drawn on top
AnnotatedCameraWidget::AnnotatedCameraWidget(VisionStreamType type, QWidget *parent)
    : fps_filter(UI_FREQ, 3, 1. / UI_FREQ), CameraWidget("camerad", type, parent) {
  pm = std::make_unique<PubMaster>(std::vector<const char*>{"uiDebug"});

  main_layout = new QVBoxLayout(this);
  main_layout->setMargin(UI_BORDER_SIZE);
  main_layout->setSpacing(0);

  experimental_btn = new ExperimentalButton(this);
  main_layout->addWidget(experimental_btn, 0, Qt::AlignTop | Qt::AlignRight);

  record_timer = std::make_shared<QTimer>();
	QObject::connect(record_timer.get(), &QTimer::timeout, [=]() {
    if(recorder) {
      recorder->update_screen();
    }
  });
	record_timer->start(1000/UI_FREQ);

	recorder = new ScreenRecoder(this);
	main_layout->addWidget(recorder, 0, Qt::AlignBottom | Qt::AlignRight);

}

void AnnotatedCameraWidget::updateState(const UIState &s) {
  // update engageability/experimental mode button
  experimental_btn->updateState(s);
  dmon.updateState(s);

  // 广角切换参数：每 ~20 帧(约1秒)刷新一次缓存，避免 paintEvent 每帧读磁盘阻塞主线程
  static int wide_cam_param_cnt = 0;
  if (wide_cam_param_cnt == 0 || (++wide_cam_param_cnt % 20) == 0) {
    Params p;
    wide_cam_mode_cache_ = p.getInt("CarrotWideCamMode");
    wide_cam_speed_low_ = p.getInt("CarrotWideCamSpeedLow");
    wide_cam_speed_high_ = p.getInt("CarrotWideCamSpeedHigh");
  }

  // === 高速净屏：隐藏 Qt 控件图标（独立开关，默认关=零影响）===
  const bool clean_view = s.clean_view_active;
  if (experimental_btn->isVisible() == clean_view) experimental_btn->setVisible(!clean_view);
  if (recorder != nullptr && recorder->isVisible() == clean_view) recorder->setVisible(!clean_view);

  static int carrot_cmd_index_last = 0;
  SubMaster& sm = *(s.sm);
  if (sm.alive("carrotMan")) {
    const auto& carrot = sm["carrotMan"].getCarrotMan();
    int carrot_cmd_index = carrot.getCarrotCmdIndex();
    if (carrot_cmd_index != carrot_cmd_index_last) {
      carrot_cmd_index_last = carrot_cmd_index;
      QString carrot_cmd = QString::fromStdString(carrot.getCarrotCmd());
      QString carrot_arg = QString::fromStdString(carrot.getCarrotArg());
      if (carrot_cmd == "RECORD") {
        if (carrot_arg == "START") {
          recorder->start();
        }
        else if (carrot_arg == "STOP") {
          recorder->stop();
        }
        else if (carrot_arg == "TOGGLE") {
          recorder->toggle();
        }
      }
    }
  }


  // STOPPED 计时器: 读取车辆静止状态，行程未开始时清零
  is_standstill_ = sm.alive("carState") && sm["carState"].getCarState().getStandstill();
  if (!s.scene.started) {
    standstill_elapsed_ = 0.0f;
  }
}

void AnnotatedCameraWidget::initializeGL() {
  CameraWidget::initializeGL();
  qInfo() << "OpenGL version:" << QString((const char*)glGetString(GL_VERSION));
  qInfo() << "OpenGL vendor:" << QString((const char*)glGetString(GL_VENDOR));
  qInfo() << "OpenGL renderer:" << QString((const char*)glGetString(GL_RENDERER));
  qInfo() << "OpenGL language version:" << QString((const char*)glGetString(GL_SHADING_LANGUAGE_VERSION));

  ui_nvg_init(uiState());
  prev_draw_t = millis_since_boot();
  setBackgroundColor(bg_colors[STATUS_DISENGAGED]);
}

mat4 AnnotatedCameraWidget::calcFrameMatrix() {
  // Project point at "infinity" to compute x and y offsets
  // to ensure this ends up in the middle of the screen
  // for narrow come and a little lower for wide cam.
  // TODO: use proper perspective transform?

  // Select intrinsic matrix and calibration based on camera type
  auto *s = uiState();
  bool wide_cam = active_stream_type == VISION_STREAM_WIDE_ROAD;
  const auto &intrinsic_matrix = wide_cam ? ECAM_INTRINSIC_MATRIX : FCAM_INTRINSIC_MATRIX;
  const auto &calibration = wide_cam ? s->scene.view_from_wide_calib : s->scene.view_from_calib;

   // Compute the calibration transformation matrix
  const auto calib_transform = intrinsic_matrix * calibration;

  float zoom = wide_cam ? 2.0 : 1.1;
  Eigen::Vector3f inf(1000., 0., 0.);
  auto Kep = calib_transform * inf;

  int w = width(), h = height();
  float center_x = intrinsic_matrix(0, 2);
  float center_y = intrinsic_matrix(1, 2);

  float max_x_offset = center_x * zoom - w / 2 - 5;
  float max_y_offset = center_y * zoom - h / 2 - 5;
  float x_offset = std::clamp<float>((Kep.x() / Kep.z() - center_x) * zoom, -max_x_offset, max_x_offset);
  float y_offset = std::clamp<float>((Kep.y() / Kep.z() - center_y) * zoom, -max_y_offset, max_y_offset);

  // Apply transformation such that video pixel coordinates match video
  // 1) Put (0, 0) in the middle of the video
  // 2) Apply same scaling as video
  // 3) Put (0, 0) in top left corner of video
  Eigen::Matrix3f video_transform =(Eigen::Matrix3f() <<
    zoom, 0.0f, (w / 2 - x_offset) - (center_x * zoom),
    0.0f, zoom, (h / 2 - y_offset) - (center_y * zoom),
    0.0f, 0.0f, 1.0f).finished();

  model.setTransform(video_transform * calib_transform);

  float zx = zoom * 2 * center_x / w;
  float zy = zoom * 2 * center_y / h;
  return mat4{{
    zx, 0.0, 0.0, -x_offset / w * 2,
    0.0, zy, 0.0, y_offset / h * 2,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
  }};
}

void AnnotatedCameraWidget::paintGL() {
}

void print_stack_trace() {
    void* buffer[100];
    int nptrs = backtrace(buffer, 100);
    char** symbols = backtrace_symbols(buffer, nptrs);
    if (symbols != nullptr) {
        for (int i = 0; i < nptrs; i++) {
            std::cerr << symbols[i] << std::endl;
        }
        free(symbols);
    }
}

void AnnotatedCameraWidget::paintEvent(QPaintEvent *event) {
  UIState *s = uiState();
  SubMaster &sm = *(s->sm);
  const double start_draw_t = millis_since_boot();

  QPainter painter(this);

  // draw camera frame
  {
    std::lock_guard lk(frame_lock);

    if (frames.empty()) {
      if (skip_frame_count > 0) {
        skip_frame_count--;
        qDebug() << "skipping frame, not ready";
        return;
      }
    } else {
      // skip drawing up to this many frames if we're
      // missing camera frames. this smooths out the
      // transitions from the narrow and wide cameras
      skip_frame_count = 5;
    }

    // >>>>>>>>>>>>>>>>>> 广角/主摄切换逻辑（30km/h 带迟滞，可调） <<<<<<<<<<<<<<<<<<
    int wide_cam_mode = wide_cam_mode_cache_; // 0自动 1仅长焦 2仅广角 (低频缓存,见 updateState)
    bool has_wide_cam = available_streams.count(VISION_STREAM_WIDE_ROAD);
    if (wide_cam_mode == 1) {
      // 仅长焦(road / narrow): 始终使用主摄
      wide_cam_requested = false;
    } else if (wide_cam_mode == 2) {
      // 仅广角(wide): 始终使用广角
      wide_cam_requested = true;
    } else if (has_wide_cam) {
      // 自动: 28/32km/h 带迟滞
      float v_ego = sm["carState"].getCarState().getVEgo(); // in m/s

      // 迟滞速度可调(单位 km/h, 由 UI 设置):
      // - 低于 CarrotWideCamSpeedLow(km/h) 切到广角
      // - 高于 CarrotWideCamSpeedHigh(km/h) 切回长焦
      int spd_low = wide_cam_speed_low_;    // km/h, 默认 28 (低频缓存)
      int spd_high = wide_cam_speed_high_;  // km/h, 默认 32 (低频缓存)
      if (spd_high <= spd_low) spd_high = spd_low + 1;  // 保证迟滞间隙, 防止边界抖动
      const float SWITCH_TO_WIDE_THRESHOLD = (float)spd_low / 3.6f;    // km/h -> m/s
      const float SWITCH_TO_ROAD_THRESHOLD = (float)spd_high / 3.6f;   // km/h -> m/s

      if (wide_cam_requested) {
        // Currently using wide cam: only switch back to road if speed is high enough
        if (v_ego >= SWITCH_TO_ROAD_THRESHOLD) {
          wide_cam_requested = false;
        }
      } else {
        // Currently using road cam: only switch to wide if speed is low enough
        if (v_ego < SWITCH_TO_WIDE_THRESHOLD) {
          wide_cam_requested = true;
        }
      }
      // Optional: keep experimental mode guard if needed (currently commented out)
      // wide_cam_requested = wide_cam_requested && sm["selfdriveState"].getSelfdriveState().getExperimentalMode();
      // wide_cam_requested = wide_cam_requested && s->scene.carrot_experimental_mode;
    } else {
      // No wide camera available, force road cam
      wide_cam_requested = false;
    }
    // >>>>>>>>>>>>>>>>>> END <<<<<<<<<<<<<<<<<<

    painter.beginNativePainting();
    CameraWidget::setStreamType(wide_cam_requested ? VISION_STREAM_WIDE_ROAD : VISION_STREAM_ROAD);
    CameraWidget::setFrameId(sm["modelV2"].getModelV2().getFrameId());
    CameraWidget::paintGL();
    painter.endNativePainting();
  }

  painter.setRenderHint(QPainter::Antialiasing);
  painter.setPen(Qt::NoPen);

  model.draw(painter, rect());
  painter.beginNativePainting();
  try {
      ui_draw(s, &model, width(), height());
  } catch (const std::exception &e) {
	LOGE("ui_nvg_draw failed: %s", e.what());
    print_stack_trace();
    Params params;
    params.putBool("CarrotException", true);
  }
  painter.endNativePainting();
  //dmon.draw(painter, rect());
  //hud.updateState(*s);
  //hud.draw(painter, rect());

  if (standstill_timer_enabled_ && is_standstill_) {
    standstill_elapsed_ += 1.0f / UI_FREQ;
    drawStoppedTimer(painter, rect());
  } else if (!is_standstill_) {
    standstill_elapsed_ = 0.0f;
  }

  double cur_draw_t = millis_since_boot();
  double dt = cur_draw_t - prev_draw_t;
  double fps = fps_filter.update(1. / dt * 1000);
  if (fps < 15) {
    //LOGW("slow frame rate: %.2f fps", fps);
  }
  prev_draw_t = cur_draw_t;

  // publish debug msg
  MessageBuilder msg;
  auto m = msg.initEvent().initUiDebug();
  m.setDrawTimeMillis(cur_draw_t - start_draw_t);
  pm->send("uiDebug", msg);
}

void AnnotatedCameraWidget::showEvent(QShowEvent *event) {
  CameraWidget::showEvent(event);

  ui_update_params(uiState());
  prev_draw_t = millis_since_boot();
}

void AnnotatedCameraWidget::drawStoppedTimer(QPainter &p, const QRect &surface_rect) {
  constexpr int alert_size = 180;  // 放大
  int x = surface_rect.right() - alert_size - UI_BORDER_SIZE * 3;
  int y = surface_rect.center().y() + 20;
  QRect alertRect(x - alert_size, y - alert_size, alert_size * 2, alert_size * 2);
  QPoint center = alertRect.center();

  // 外圈白边 + 深色半透明背景（若隐若现，可透出后方画面）
  p.setPen(QPen(QColor(255, 255, 255, 100), 6));
  p.setBrush(QColor(40, 40, 40, 140));
  p.drawEllipse(center, alert_size, alert_size);

  // 格式化 mm:ss
  int minute = static_cast<int>(standstill_elapsed_ / 60);
  int second = static_cast<int>(standstill_elapsed_ - (minute * 60));
  QString time_text = QString("%1:%2").arg(minute, 1, 10, QChar('0')).arg(second, 2, 10, QChar('0'));

  // STOPPED 标题 (上方) - 橙色
  p.setFont(InterFont(65, QFont::Bold));
  p.setPen(QColor(255, 140, 0));
  QFontMetrics fmt(p.font());
  QRect topTextRect = fmt.boundingRect(alertRect, Qt::TextWordWrap, tr("STOPPED"));
  topTextRect.moveCenter(center);
  topTextRect.moveTop(alertRect.top() + alertRect.height() / 3.5);
  p.drawText(topTextRect, Qt::AlignCenter, tr("STOPPED"));

  // 计时时间 (下方) - 白色
  p.setFont(InterFont(80, QFont::Bold));
  p.setPen(QColor(255, 255, 255, 255));
  QFontMetrics fm(p.font());
  QRect textRect = fm.boundingRect(alertRect, Qt::TextWordWrap, time_text);
  textRect.moveCenter(center);
  textRect.moveBottom(alertRect.bottom() - alertRect.height() / 5);
  p.drawText(textRect, Qt::AlignCenter, time_text);
}

void AnnotatedCameraWidget::mousePressEvent(QMouseEvent *event) {
  // 让点击向上冒泡到 OnroadWindow：恢复行驶界面全屏点击响应
  // (展开侧栏 / HUD区域切换显示)。空 {} 会吞掉事件导致点击无反应。
  QWidget::mousePressEvent(event);
}
