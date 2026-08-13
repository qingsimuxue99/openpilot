#include "selfdrive/ui/qt/sidebar.h"

#include <QMouseEvent>
#include <QTimer>
#include <QFile>
#include <QTextStream>
#include <QRegExp>

#include <QNetworkInterface>

#include "selfdrive/ui/qt/util.h"

void Sidebar::drawMetric(QPainter &p, const QRect &rect, const QString &l1, const QString &l2,
                         QColor border, QColor bg, float borderW,
                         bool secondGreen, bool secondBold) {
  p.setPen(Qt::NoPen);
  p.setBrush(bg);
  p.drawRoundedRect(rect, 18, 18);

  QPen pen(border);
  pen.setWidthF(borderW);
  p.setPen(pen);
  p.setBrush(Qt::NoBrush);
  p.drawRoundedRect(rect, 18, 18);

  // 两行均加粗、字号加大；上下留白按卡片高度自动均分（内容垂直居中）
  const int l1Font = 34, l2Font = secondBold ? 40 : 34;
  const int lineH = 46, lineGap = 4;
  const int topPad = qMax(6, (rect.height() - lineH * 2 - lineGap) / 2);

  p.setPen(QColor(0xff, 0xff, 0xff));
  p.setFont(InterFont(l1Font, QFont::Bold));
  p.drawText(QRect(rect.x(), rect.y() + topPad, rect.width(), lineH), Qt::AlignCenter, l1);

  p.setPen(secondGreen ? green_color : QColor(0xff, 0xff, 0xff));
  p.setFont(InterFont(l2Font, QFont::Bold));
  p.drawText(QRect(rect.x(), rect.y() + topPad + lineH + lineGap, rect.width(), lineH), Qt::AlignCenter, l2);
}

Sidebar::Sidebar(QWidget *parent) : QFrame(parent), onroad(false), flag_pressed(false), settings_pressed(false) {
  home_img = loadPixmap("../assets/images/button_home.png", home_btn.size());
  flag_img = loadPixmap("../assets/images/button_flag.png", home_btn.size());
  settings_img = loadPixmap("../assets/images/button_settings.png", settings_btn.size(), Qt::IgnoreAspectRatio);
  c3x_img = loadPixmap("../assets/img_c3x.png", home_btn.size());

  qr_img.load("/data/c3_toolbox/qr.png");
  qrTimer = new QTimer(this);
  QObject::connect(qrTimer, &QTimer::timeout, this, [=]() {
    qr_img.load("/data/c3_toolbox/qr.png");
    update();
  });
  qrTimer->start(15000);

  fan_full = params.getBool("CarrotFanFullSpeed");

  // 总内存只需读一次（/proc/meminfo 的 MemTotal 单位 kB）
  {
    QFile mf("/proc/meminfo");
    if (mf.open(QIODevice::ReadOnly | QIODevice::Text)) {
      QTextStream ts(&mf);
      QString line;
      while (ts.readLineInto(&line)) {
        if (line.startsWith("MemTotal:")) {
          const QStringList parts = line.split(QRegExp("\\s+"), QString::SkipEmptyParts);
          if (parts.size() >= 2) {
            mem_total_gb = parts[1].toFloat() / 1024.0f / 1024.0f;
          }
          break;
        }
      }
      mf.close();
    }
  }

  setAttribute(Qt::WA_OpaquePaintEvent);
  setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Expanding);
  setFixedWidth(300);

  QObject::connect(uiState(), &UIState::uiUpdate, this, &Sidebar::updateState);

  pm = std::make_unique<PubMaster>(std::vector<const char*>{"userFlag"});
}

void Sidebar::mousePressEvent(QMouseEvent *event) {
  if (onroad && home_btn.contains(event->pos())) {
    flag_pressed = true;
    update();
  } else if (settings_btn.contains(event->pos())) {
    settings_pressed = true;
    update();
  }
}

void Sidebar::mouseReleaseEvent(QMouseEvent *event) {
  // 用"按下时已命中"判定：释放时不再二次要求坐标仍在按钮矩形内，
  // 避免触摸抖动 / 主线程繁忙导致轻点落空、需连点多次才弹出设置
  bool was_settings = settings_pressed;
  bool was_flag = flag_pressed;
  flag_pressed = settings_pressed = false;
  update();
  if (was_flag && onroad && home_btn.contains(event->pos())) {
    MessageBuilder msg;
    msg.initEvent().initUserFlag();
    pm->send("userFlag", msg);
  } else if (was_settings) {
    emit openSettings();
  }
}

void Sidebar::mouseDoubleClickEvent(QMouseEvent *event) {
  if (temp_rect.contains(event->pos()) || fan_rect.contains(event->pos())) {
    // 双击温度/风扇卡：切换风扇 自动 <-> 全速
    fan_full = !fan_full;
    params.putBool("CarrotFanFullSpeed", fan_full);
    update();
  } else if (mem_rect.contains(event->pos())) {
    // 双击内存卡：重启设备（先确认，防误触）
    if (ConfirmationDialog::confirm(tr("确定要重启设备吗？"), tr("重启"), this)) {
      Hardware::reboot();
    }
  }
}

void Sidebar::offroadTransition(bool offroad) {
  onroad = !offroad;
  update();
}

void Sidebar::updateState(const UIState &s) {
  if (!isVisible()) return;

  auto &sm = *(s.sm);

  networking = networking ? networking : (window() ? window()->findChild<Networking *>("") : nullptr);
  bool tethering_on = networking && networking->wifi && networking->wifi->tethering_on;
  auto deviceState = sm["deviceState"].getDeviceState();
  net_type = tethering_on ? "Hotspot" : network_type[deviceState.getNetworkType()];
  int strength = tethering_on ? 4 : (int)deviceState.getNetworkStrength();
  net_strength = strength > 0 ? strength + 1 : 0;

  // 温度（多核平均）
  const auto cpuTempC = deviceState.getCpuTempC();
  cpu_temp = 0; int nt = 0;
  for (auto t : cpuTempC) { cpu_temp += t; nt++; }
  if (nt > 0) cpu_temp /= (float)nt;

  // CPU 使用率（多核平均）
  const auto cpuUsagePercent = deviceState.getCpuUsagePercent();
  cpu_usage = 0; int nu = 0;
  for (auto u : cpuUsagePercent) { if (u <= 0) break; cpu_usage += u; nu++; }
  if (nu > 0) cpu_usage /= (float)nu;

  // 内存占用率
  mem_usage = deviceState.getMemoryUsagePercent();

  // 电压 / 风扇转速（peripheralState）
  try {
    auto peripheralState = sm["peripheralState"].getPeripheralState();
    voltage = peripheralState.getVoltage() / 1000.0f;
    fan_rpm = peripheralState.getFanSpeedRpm();
  } catch (...) {
    voltage = 0; fan_rpm = 0;
  }

  // IP 地址
  ip_addr = "--";
  for (const auto &iface : QNetworkInterface::allInterfaces()) {
    if (iface.flags().testFlag(QNetworkInterface::IsLoopBack)) continue;
    if (!iface.flags().testFlag(QNetworkInterface::IsUp)) continue;
    for (const auto &entry : iface.addressEntries()) {
      if (entry.ip().protocol() == QAbstractSocket::IPv4Protocol) {
        ip_addr = entry.ip().toString();
        break;
      }
    }
    if (ip_addr != "--") break;
  }

  // 车辆连接状态
  panda_online = (s.scene.pandaType != cereal::PandaState::PandaType::UNKNOWN);

  // 风扇全速标志（由双击切换，持久于本次 manager 运行）
  fan_full = params.getBool("CarrotFanFullSpeed");

  update();
}

void Sidebar::paintEvent(QPaintEvent *event) {
  QPainter p(this);
  p.setPen(Qt::NoPen);
  p.setRenderHint(QPainter::Antialiasing);

  p.fillRect(rect(), QColor(57, 57, 57));

  QString c3x_position = QString::fromStdString(params.get("DevicePosition"));

  // buttons
  p.setOpacity(settings_pressed ? 0.65 : 1.0);
  p.drawPixmap(settings_btn.x(), settings_btn.y(), settings_img);
  p.setOpacity(onroad && flag_pressed ? 0.65 : 1.0);
  p.drawPixmap(home_btn.x(), home_btn.y(), c3x_img);

  const QRect r3 = QRect(0, 967, event->rect().width(), 50);
  p.setFont(InterFont(30));
  p.setPen(QColor(0xff, 0xff, 0xff));
  p.drawText(r3, Qt::AlignCenter, c3x_position);
  p.setOpacity(1.0);

  // network：5 个白色/灰色圆点（信号强度）
  int x = 58;
  const QColor gray(0x54, 0x54, 0x54);
  for (int i = 0; i < 5; ++i) {
    p.setBrush(i < net_strength ? Qt::white : gray);
    p.drawEllipse(x, 196, 27, 27);
    x += 37;
  }

  // Wi-Fi 名称 + IP（居中、加粗）
  p.setFont(InterFont(40, QFont::Bold));
  p.setPen(QColor(0xff, 0xff, 0xff));
  p.drawText(QRect(15, 234, 270, 50), Qt::AlignCenter, net_type);
  p.setFont(InterFont(32, QFont::Bold));
  p.drawText(QRect(15, 290, 270, 40), Qt::AlignCenter, ip_addr);

  // 状态边框配色（阈值下调：白<55 / 黄55-65 / 红≥65，让常温也能立刻看到变色）
  QColor tempBorder, tempBg, memBorder, memBg;
  float tempW = 2.0f, memW = 2.0f;
  if (cpu_temp >= 85.0f)       { tempBorder = danger_color; tempBg = QColor(255, 77, 77, 0x1f); tempW = 3.0f; }
  else if (cpu_temp >= 78.0f)  { tempBorder = warn_color;   tempBg = QColor(255, 194, 51, 0x1f); tempW = 3.0f; }
  else                         { tempBorder = white_border; tempBg = QColor(255, 255, 255, 0x1a); }
  if (mem_usage >= 85)         { memBorder = danger_color; memBg = QColor(255, 77, 77, 0x1f); memW = 3.0f; }
  else if (mem_usage >= 70)    { memBorder = warn_color;   memBg = QColor(255, 194, 51, 0x1f); memW = 3.0f; }
  else                         { memBorder = white_border; memBg = QColor(255, 255, 255, 0x1a); }

  // ---------- 底部：二维码 + 文字，整体贴到距底边 10px ----------
  const int qrSize = 148;                            // 缩到 148 让出卡片空间
  const int labelH = 24;
  const int bottomPad = 10;                          // 文字底 距 侧边栏底边 10px
  const int labelY = height() - bottomPad - labelH;  // 1080 -> 1046
  const int qrY = labelY - 6 - qrSize;               // 1046 -> 892

  // ---------- 卡片区：WiFi 不动，上距 IP 20px、下距 QR 20px，四卡均分撑满 ----------
  const int cardX = 12, cardW = width() - cardX * 2; // 300 -> 276
  const int cardTop = 350;                           // IP 文字底(330) + 20
  const int cardBottom = qrY - 20;                   // 892 - 20 = 872
  const int cardGap = 14;
  const int cardH = (cardBottom - cardTop - cardGap * 3) / 4;  // (522)/4 = 120
  const int step = cardH + cardGap;

  // 温度卡
  temp_rect = QRect(cardX, cardTop, cardW, cardH);
  drawMetric(p, temp_rect,
                QString("温度 %1°C").arg(cpu_temp, 0, 'f', 0),
                QString("CPU %1%").arg(cpu_usage, 0, 'f', 0),
                tempBorder, tempBg, tempW, false, false);

  // 车辆连接卡（白边框；未连接时第二行标红）
  panda_rect = QRect(cardX, cardTop + step, cardW, cardH);
  drawMetric(p, panda_rect,
                "车辆连接", panda_online ? "在线" : "NO PANDA",
                white_border, QColor(255, 255, 255, 0x1a), 2.0f, false, false);
  if (!panda_online) {
    p.setPen(danger_color);
    p.setFont(InterFont(34, QFont::Bold));
    p.drawText(QRect(cardX, panda_rect.y() + (cardH - 42) / 2, cardW, 42), Qt::AlignCenter, "NO PANDA");
  }

  // 内存卡（总量从 /proc/meminfo 动态读取，避免写死）
  mem_rect = QRect(cardX, cardTop + step * 2, cardW, cardH);
  float used = mem_usage / 100.0f * mem_total_gb;
  drawMetric(p, mem_rect,
                QString("内存 %1%").arg(mem_usage),
                QString("%1G / %2G").arg(used, 0, 'f', 1).arg(mem_total_gb, 0, 'f', 1),
                memBorder, memBg, memW, false, false);

  // 风扇卡（按截图复刻）
  // 自动态: 第一行 "风扇 3380"（白加粗），第二行 "电压 5.12V"（白加粗）
  // 全速态: 第一行 "风扇 全速"，第二行 "3380"（绿加粗大字号）
  fan_rect = QRect(cardX, cardTop + step * 3, cardW, cardH);
  QString fanL1, fanL2;
  if (fan_full) {
    fanL1 = "风扇 全速";
    fanL2 = QString("%1").arg(fan_rpm);
  } else {
    fanL1 = "风扇 自动";
    fanL2 = QString("电压 %1V").arg(voltage, 0, 'f', 2);
  }
  drawMetric(p, fan_rect, fanL1, fanL2,
                tempBorder, tempBg, tempW, fan_full, fan_full);

  // 打开工具箱二维码（贴底，最后绘制，盖住 c3x 图标避免边缘露出）
  if (!qr_img.isNull()) {
    const int qrX = (width() - qrSize) / 2;
    p.fillRect(QRect(0, qrY - 8, width(), height() - qrY + 8), QColor(57, 57, 57));
    p.drawPixmap(qrX, qrY, qrSize, qrSize, qr_img);
    p.setPen(QColor(0xff, 0xff, 0xff));
    p.setFont(InterFont(24, QFont::Bold));
    p.drawText(QRect(0, labelY, width(), labelH), Qt::AlignCenter, tr("扫码打开工具箱"));
  }
}

