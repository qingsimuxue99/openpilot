#pragma once

#include <QPainter>
#include <QPolygonF>
#include <QTime>          // [DP] 新增：用于彩虹动画计时

#include "selfdrive/ui/ui.h"

class ModelRenderer {
public:
  ModelRenderer();        // [DP] 改动：原来是无参空构造，现在需要实现
  void setTransform(const Eigen::Matrix3f &transform) { car_space_transform = transform; }
  void draw(QPainter &painter, const QRect &surface_rect);

private:
  // [DP] 新增：彩虹路径常量
  static constexpr float DP_RAINBOW_SCROLL_SPEED_FACTOR = 10.0f;
  static constexpr int   DP_RAINBOW_NUM_REPEATS = 1;
  static constexpr int   DP_RAINBOW_ALPHA = 140;      // 透明度，降低更通透
  static constexpr float DP_RAINBOW_BRIGHTNESS = 0.7f;         // [DP] 明度 0~1，越小越暗
  static constexpr int   DP_RAINBOW_GRADIENT_SAMPLES = 80;
  static constexpr int   DP_RAINBOW_HUE_SECTORS = 6;
  static constexpr float DP_RAINBOW_PATH_WIDTH = 1.1f;         // [DP] 默认宽度, 运行时由 dp_ui_rainbow_width 覆盖

public:
  bool mapToScreen(float in_x, float in_y, float in_z, QPointF *out);
  void mapLineToPolygon(const cereal::XYZTData::Reader &line, float y_off, float z_off,
                        QPolygonF *pvd, int max_idx, bool allow_invert = true);
  void drawLead(QPainter &painter, const cereal::RadarState::LeadData::Reader &lead_data, const QPointF &vd, const QRect &surface_rect);
  void update_leads(const cereal::RadarState::Reader &radar_state, const cereal::XYZTData::Reader &line);
  void update_model(const cereal::ModelDataV2::Reader &model, const cereal::RadarState::LeadData::Reader &lead);
  void drawLaneLines(QPainter &painter);
  void drawPath(QPainter &painter, const cereal::ModelDataV2::Reader &model, int height);
  void updatePathGradient(QLinearGradient &bg);
  QColor blendColors(const QColor &start, const QColor &end, float t);

  // [DP] 新增：彩虹路径方法
  void updateRainbowGradient(QLinearGradient &bg);
  static QColor hsvToColor(float h, float s, float v, int alpha);

  bool longitudinal_control = false;
  bool experimental_mode = false;
  float blend_factor = 1.0f;
  bool prev_allow_throttle = true;
  float lane_line_probs[4] = {};
  float road_edge_stds[2] = {};
  float path_offset_z = 1.22f;
  QPolygonF track_vertices;
  QPolygonF lane_line_vertices[4] = {};
  QPolygonF road_edge_vertices[2] = {};
  QPointF lead_vertices[2] = {};
  Eigen::Matrix3f car_space_transform = Eigen::Matrix3f::Zero();
  QRectF clip_region;

  // [DP] 新增：彩虹路径状态变量
  float dp_rainbow_rotation = 0.0f;
  QTime dp_rainbow_timer;
};
