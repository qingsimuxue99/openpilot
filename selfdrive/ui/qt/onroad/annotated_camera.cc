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

  // 驾驶习惯自学习: 每 ~0.5s 节流读取一次总开关状态 (避免每帧读盘)
  if (--learning_param_frame_ <= 0) {
    learning_param_frame_ = std::max(1, (int)(UI_FREQ / 2));
    try {
      learning_enabled_ = Params().getBool("CarrotLearningEnabled");
    } catch (...) {
      learning_enabled_ = false;
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
    int wide_cam_mode = Params().getInt("CarrotWideCamMode"); // 0自动 1仅长焦 2仅广角
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
      int spd_low = Params().getInt("CarrotWideCamSpeedLow");    // km/h, 默认 28
      int spd_high = Params().getInt("CarrotWideCamSpeedHigh");  // km/h, 默认 32
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

  // 驾驶习惯自学习: 绘制紧凑可展开状态框 (总开关开启时显示)
  if (learning_enabled_) {
    bool engaged = sm.alive("selfdriveState") &&
                   sm["selfdriveState"].getSelfdriveState().getEnabled();
    drawLearningBox(painter, rect(), engaged);
  }

  // STOPPED 计时器 (ported from sunnypilot)
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

// 驾驶习惯自学习: 行车界面左下角紧凑状态框 (纯矢量, 无需图片资源)
//   默认收起为小药丸(脉冲点 + "自学习" + 已X·学Y), 点按展开为完整面板。
//   数据来自 Param CarrotLearningHud ('|' 分隔, 由 carrot_learner.py 发布)。
void AnnotatedCameraWidget::parseHud(const QString &raw) {
  hud_applied_.clear();
  hud_learning_.clear();
  hud_rate_ = 5;
  QStringList p = raw.split('|');
  int i = 0;
  if (i >= p.size()) return;
  i++;                                   // [0] enabled (绘制未用)
  if (i < p.size()) { hud_rate_ = p[i].toInt(); i++; }
  if (i < p.size()) {
    int n = p[i].toInt(); i++;
    for (int r = 0; r < n && i + 2 < p.size(); r++) {
      HudApplied a; a.name = p[i]; a.val = p[i + 1].toInt(); a.delta = p[i + 2].toInt(); i += 3;
      hud_applied_.append(a);
    }
  }
  if (i < p.size()) {
    int n = p[i].toInt(); i++;
    for (int r = 0; r < n && i + 2 < p.size(); r++) {
      HudLearn l; l.name = p[i]; l.cur = p[i + 1].toFloat(); l.target = p[i + 2].toFloat(); i += 3;
      hud_learning_.append(l);
    }
  }
}

void AnnotatedCameraWidget::drawLearningBox(QPainter &p, const QRect &surface_rect, bool engaged) {
  p.save();
  p.setRenderHint(QPainter::Antialiasing);

  // 节流读取 HUD 数据 (~0.5s 一次, 避免每帧读盘)
  if (--learning_param_frame_ <= 0) {
    learning_param_frame_ = std::max(1, (int)(UI_FREQ / 2));
    try {
      parseHud(QString::fromStdString(Params().get("CarrotLearningHud")));
    } catch (...) {}
  }

  // 脉搏脉动 (仅激活学习时): 0.55 ~ 1.0
  double pulse = 1.0;
  if (engaged) {
    double t = millis_since_boot() / 1000.0;
    pulse = 0.55 + 0.45 * (0.5 * (1.0 + std::sin(t * 3.2)));
  }
  QColor accent = engaged ? QColor(0x2e, 0xcc, 0x71) : QColor(0x9e, 0xa7, 0xad);

  // 布局: 贴着左侧/底部蓝线边框, 避让底部信息栏(carrot.cc drawBottomBar: bar_h=82 + margin_bottom=3)
  const int margin = 0;                   // 面板左边缘直接贴屏幕左边缘(蓝线内侧)
  const int bottom_bar_h = 85;            // 82 + 3, 信息栏实际占用高度
  const int gap_to_bar = 4;               // 与信息栏顶部留 4px 间隙
  const int x = surface_rect.left() + margin;
  const int bottom_base = surface_rect.bottom() - bottom_bar_h - gap_to_bar;
  // 与弯道预瞄药丸互换位置: 自学习药丸/面板堆叠在弯道药丸上方, 避免遮挡自学习面板
  int stack_base = bottom_base;

  if (!learning_box_expanded_) {
    // ===== 收起态: 大药丸（好点击） =====
    QFont f = p.font();
    f.setPixelSize(36); f.setBold(true); p.setFont(f);
    QFontMetrics fm(f);
    QString title = "自学习";
    int titleW = fm.horizontalAdvance(title);
    QString cnt = QString("已%1·学%2").arg(hud_applied_.size()).arg(hud_learning_.size());
    f.setPixelSize(28); f.setBold(false); p.setFont(f);
    int cntW = fm.horizontalAdvance(cnt);
    int dotR = 10, padX = 20, gap = 16;
    int pillW = padX + dotR * 2 + gap + titleW + 12 + cntW + padX;
    int pillH = 56;
    int y = stack_base - pillH;
    QRect pill(x, y, pillW, pillH);
    learning_hit_rect_ = pill;

    p.setPen(Qt::NoPen); p.setBrush(QColor(0, 0, 0, 175));
    p.drawRoundedRect(pill, pillH / 2, pillH / 2);
    p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), engaged ? (int)(220 * pulse) : 130), 3));
    p.setBrush(Qt::NoBrush); p.drawRoundedRect(pill, pillH / 2, pillH / 2);

    int cx = x + padX + dotR, cy = y + pillH / 2;
    if (engaged) {
      QColor glow = accent; glow.setAlpha((int)(100 * pulse));
      p.setPen(Qt::NoPen); p.setBrush(glow);
      p.drawEllipse(QPoint(cx, cy), (int)(dotR + 10 * pulse), (int)(dotR + 10 * pulse));
    }
    QColor dot = accent; dot.setAlpha(engaged ? (int)(255 * pulse) : 210);
    p.setPen(Qt::NoPen); p.setBrush(dot);
    p.drawEllipse(QPoint(cx, cy), dotR, dotR);

    p.setPen(QColor(255, 255, 255, 245));
    f.setPixelSize(36); f.setBold(true); p.setFont(f);
    p.drawText(QRect(cx + dotR + gap, y, titleW + 4, pillH), Qt::AlignVCenter | Qt::AlignLeft, title);
    f.setPixelSize(28); f.setBold(false); p.setFont(f);
    p.setPen(QColor(180, 190, 200, 230));
    p.drawText(QRect(cx + dotR + gap + titleW + 12, y, cntW + 4, pillH), Qt::AlignVCenter | Qt::AlignLeft, cnt);

  } else {
    // ===== 展开态: 放大面板（提高可读性, 顶部不侵入红绿灯状态灯, 留 10px 间隔） =====
    int padX = 22, padY = 20, headerH = 54, secHeadH = 26, rowH = 40, footH = 34, secGap = 12;
    int nA = hud_applied_.size(), nL = hud_learning_.size();
    int bodyH = (nA > 0 ? (secHeadH + nA * rowH) : 0) + (nL > 0 ? (secHeadH + nL * rowH) : 0);
    int panelW = 420;
    int panelH = padY + headerH + secGap + bodyH + footH + padY;
    int y = stack_base - panelH;
    // 顶部约束: 不侵入顶部红绿灯(状态灯)图标区域, 与其底部保持 10px 间隔
    const int top_limit = surface_rect.top() + 160;  // 红绿灯底部约 +10px
    if (y < top_limit) {
      y = top_limit;
      panelH = stack_base - y;
    }
    QRect panel(x, y, panelW, panelH);
    learning_hit_rect_ = panel;
    p.setClipRect(panel);  // 顶部约束生效时内容裁切到面板内, 不压到红绿灯

    p.setPen(Qt::NoPen); p.setBrush(QColor(8, 10, 14, 208));
    p.drawRoundedRect(panel, 14, 14);
    p.setPen(QPen(QColor(255, 255, 255, 32), 1)); p.setBrush(Qt::NoBrush);
    p.drawRoundedRect(panel, 14, 14);

    int cx0 = x + padX;
    const int iw = panelW - 2 * padX;  // 内部可用宽度
    int yy = y + padY;
    QFont f = p.font();

    // 顶部: 脉冲点 + 标题 + 强度胶囊
    int hcx = cx0 + 6, hcy = yy + headerH / 2;
    QColor dot = accent; dot.setAlpha(engaged ? (int)(255 * pulse) : 210);
    p.setPen(Qt::NoPen); p.setBrush(dot); p.drawEllipse(QPoint(hcx, hcy), 6, 6);
    f.setPixelSize(30); f.setBold(true); p.setFont(f);
    p.setPen(QColor(232, 237, 242, 245));
    p.drawText(QRect(hcx + 16, yy, iw - 130, headerH), Qt::AlignVCenter | Qt::AlignLeft, "驾驶习惯学习");
    QString rateS = QString("强度 %1").arg(hud_rate_);
    f.setPixelSize(20); f.setBold(false); p.setFont(f);
    int rateW = QFontMetrics(f).horizontalAdvance(rateS) + 20;
    QRect rateR(cx0 + iw - rateW, yy + (headerH - 30) / 2, rateW, 30);
    p.setPen(QColor(255, 255, 255, 45)); p.setBrush(QColor(255, 255, 255, 12));
    p.drawRoundedRect(rateR, 15, 15);
    p.setPen(QColor(170, 180, 190, 235));
    p.drawText(rateR, Qt::AlignCenter, rateS);

    yy += headerH + secGap;

    // 已应用参数（标题左右分布: 左名称 / 右项数）
    if (nA > 0) {
      f.setPixelSize(20); f.setBold(false); p.setFont(f);
      p.setPen(QColor(154, 166, 178, 230));
      p.drawText(QRect(cx0, yy, iw * 2 / 3, secHeadH), Qt::AlignVCenter | Qt::AlignLeft, "已应用参数（生效中）");
      p.drawText(QRect(cx0 + iw * 2 / 3, yy, iw / 3, secHeadH), Qt::AlignVCenter | Qt::AlignRight, QString("%1 项").arg(nA));
      yy += secHeadH;
      for (int r = 0; r < nA; r++) {
        const HudApplied &a = hud_applied_[r];
        // 紧凑三列：名称 | 数值(右对齐) | 偏移芯片
        f.setPixelSize(20); f.setBold(true); p.setFont(f);
        QString ds = (a.delta > 0 ? "+" : "") + QString::number(a.delta);
        int chipW = QFontMetrics(f).horizontalAdvance(ds) + 20;
        const int valW = 64, gap = 10;
        int chipX = cx0 + iw - chipW;
        int valX  = chipX - valW - gap;
        int nameW = std::max(20, valX - cx0 - gap);

        f.setPixelSize(24); f.setBold(false); p.setFont(f);
        p.setPen(QColor(232, 237, 242, 240));
        p.drawText(QRect(cx0, yy, nameW, rowH), Qt::AlignVCenter | Qt::AlignLeft, a.name);
        f.setPixelSize(26); f.setBold(true); p.setFont(f);
        p.setPen(QColor(255, 255, 255, 250));
        p.drawText(QRect(valX, yy, valW, rowH), Qt::AlignVCenter | Qt::AlignRight, QString::number(a.val));
        QColor chip = a.delta > 0 ? QColor(0x1f, 0x6f, 0x4a) : QColor(0x9c, 0x5a, 0x16);
        f.setPixelSize(20); f.setBold(true); p.setFont(f);
        QRect cr(chipX, yy + (rowH - 28) / 2, chipW, 28);
        p.setPen(Qt::NoPen); p.setBrush(chip); p.drawRoundedRect(cr, 14, 14);
        p.setPen(QColor(255, 255, 255, 250));
        p.drawText(cr, Qt::AlignCenter, ds);
        yy += rowH;
      }
      yy += 4;
    }

    // 正在学习 / 准备学习（标题左右分布 + 细蓝进度条 + 分式）
    if (nL > 0) {
      f.setPixelSize(20); f.setBold(false); p.setFont(f);
      p.setPen(QColor(154, 166, 178, 230));
      p.drawText(QRect(cx0, yy, iw * 2 / 3, secHeadH), Qt::AlignVCenter | Qt::AlignLeft, "正在学习 / 准备学习");
      p.drawText(QRect(cx0 + iw * 2 / 3, yy, iw / 3, secHeadH), Qt::AlignVCenter | Qt::AlignRight, QString("%1 项").arg(nL));
      yy += secHeadH;
      for (int r = 0; r < nL; r++) {
        const HudLearn &l = hud_learning_[r];
        const int progW = 120, pctW = 64, gap = 10;
        int progX = cx0 + iw - progW;
        int pctX  = progX + progW + gap;
        int nameW = std::max(20, progX - cx0 - gap);

        f.setPixelSize(24); f.setBold(false); p.setFont(f);
        p.setPen(QColor(232, 237, 242, 240));
        p.drawText(QRect(cx0, yy, nameW, rowH), Qt::AlignVCenter | Qt::AlignLeft, l.name);
        // 细蓝进度条
        int barY = yy + rowH / 2 - 4;
        p.setPen(Qt::NoPen); p.setBrush(QColor(255, 255, 255, 26));
        p.drawRoundedRect(QRect(progX, barY, progW, 8), 4, 4);
        float ratio = (l.target > 0) ? std::min(1.0f, l.cur / l.target) : 0.0f;
        p.setBrush(QColor(0x3f, 0xb6, 0xff));
        int fw = std::max(3, (int)(progW * ratio));
        p.drawRoundedRect(QRect(progX, barY, fw, 8), 4, 4);
        // 分式百分比 cur/target
        f.setPixelSize(20); f.setBold(false); p.setFont(f);
        p.setPen(QColor(154, 166, 178, 225));
        QString frac = QString("%1/%2").arg(l.cur, 0, 'f', 1).arg(l.target, 0, 'f', 1);
        p.drawText(QRect(pctX, yy, pctW, rowH), Qt::AlignVCenter | Qt::AlignLeft, frac);
        yy += rowH;
      }
      yy += 4;
    }

    // 底部双段: 左说明 + 右蓝标签（对应设计稿 .lb-foot）
    f.setPixelSize(19); f.setBold(false); p.setFont(f);
    QString footR = "样本达标即微调";
    int footRw = QFontMetrics(f).horizontalAdvance(footR) + 18;
    QRect footRr(cx0 + iw - footRw, yy, footRw, footH);
    p.setPen(QColor(0x3f, 0xb6, 0xff, 170)); p.setBrush(QColor(0x3f, 0xb6, 0xff, 20));
    p.drawRoundedRect(footRr, 10, 10);
    p.setPen(QColor(0x7e, 0xd0, 0xff, 240));
    p.drawText(footRr, Qt::AlignCenter, footR);
    p.setPen(QColor(154, 166, 178, 215));
    p.drawText(QRect(cx0, yy, iw - footRw - 8, footH), Qt::AlignVCenter | Qt::AlignLeft, "每 45s 评估一次");
  }

  p.restore();
}

// STOPPED 计时器绘制 (ported from sunnypilot v2025.002.000 drawE2eAlert)
void AnnotatedCameraWidget::drawStoppedTimer(QPainter &p, const QRect &surface_rect) {
  constexpr int alert_size = 180;  // 放大
  int x = surface_rect.right() - alert_size - UI_BORDER_SIZE * 3;
  int y = surface_rect.center().y() + 20;
  QRect alertRect(x - alert_size, y - alert_size, alert_size * 2, alert_size * 2);
  QPoint center = alertRect.center();

  // 圆环背景 (透灰，很透明但带点灰)
  p.setPen(QPen(QColor(255, 255, 255, 60), 6));
  p.setBrush(QColor(60, 60, 60, 80));  // 灰色底，很透明
  p.drawEllipse(center, alert_size, alert_size);

  // 格式化 mm:ss
  int minute = static_cast<int>(standstill_elapsed_ / 60);
  int second = static_cast<int>(standstill_elapsed_ - (minute * 60));
  QString time_text = QString("%1:%2").arg(minute, 1, 10, QChar('0')).arg(second, 2, 10, QChar('0'));

  // STOPPED 标题 (上方) - 橙色
  p.setFont(InterFont(65, QFont::Bold));
  p.setPen(QColor(255, 140, 0, 255));
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
  if (learning_enabled_ && learning_hit_rect_.contains(event->pos())) {
    learning_box_expanded_ = !learning_box_expanded_;
    event->accept();
    return;
  }
  QWidget::mousePressEvent(event);
}
