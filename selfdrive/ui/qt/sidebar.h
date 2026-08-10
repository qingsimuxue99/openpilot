#pragma once

#include <memory>

#include <QFrame>
#include <QTimer>
#include <QMap>
#include <QMouseEvent>

#include "selfdrive/ui/ui.h"
#include "selfdrive/ui/qt/network/networking.h"
#include "selfdrive/ui/qt/widgets/input.h"
#include "system/hardware/tici/hardware.h"

class Sidebar : public QFrame {
  Q_OBJECT

public:
  explicit Sidebar(QWidget* parent = 0);

signals:
  void openSettings(int index = 0, const QString &param = "");
  void valueChanged();

public slots:
  void offroadTransition(bool offroad);
  void updateState(const UIState &s);

protected:
  void paintEvent(QPaintEvent *event) override;
  void mousePressEvent(QMouseEvent *event) override;
  void mouseReleaseEvent(QMouseEvent *event) override;
  void mouseDoubleClickEvent(QMouseEvent *event) override;
  void drawMetric(QPainter &p, const QRect &rect, const QString &l1, const QString &l2,
                  QColor border, QColor bg, float borderW,
                  bool secondGreen = false, bool secondBold = false);

  QPixmap home_img, flag_img, settings_img, c3x_img, qr_img;
  QTimer *qrTimer = nullptr;
  bool onroad, flag_pressed, settings_pressed;
  const QMap<cereal::DeviceState::NetworkType, QString> network_type = {
    {cereal::DeviceState::NetworkType::NONE, tr("--")},
    {cereal::DeviceState::NetworkType::WIFI, tr("Wi-Fi")},
    {cereal::DeviceState::NetworkType::ETHERNET, tr("ETH")},
    {cereal::DeviceState::NetworkType::CELL2_G, tr("2G")},
    {cereal::DeviceState::NetworkType::CELL3_G, tr("3G")},
    {cereal::DeviceState::NetworkType::CELL4_G, tr("LTE")},
    {cereal::DeviceState::NetworkType::CELL5_G, tr("5G")}
  };

  const QRect home_btn = QRect(60, 860, 180, 180);
  const QRect settings_btn = QRect(50, 35, 200, 117);
  const QColor white_border = QColor(255, 255, 255, 0x55);
  const QColor warn_color = QColor(0xff, 0xc2, 0x33);
  const QColor danger_color = QColor(0xff, 0x4d, 0x4d);
  const QColor green_color = QColor(0x7e, 0xd9, 0x57);

  QString net_type, ip_addr;
  int net_strength = 0;

  float cpu_temp = 0, cpu_usage = 0;
  int mem_usage = 0;
  float mem_total_gb = 7.4f;
  int fan_rpm = 0;
  float voltage = 0;
  bool fan_full = false;
  bool panda_online = true;

  QRect temp_rect, panda_rect, mem_rect, fan_rect;

private:
  std::unique_ptr<PubMaster> pm;
  Networking *networking = nullptr;
  Params params;
};
