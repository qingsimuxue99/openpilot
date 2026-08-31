#include <cassert>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <string>
#include <tuple>
#include <vector>
#include <algorithm>
#include <dirent.h>
#include <sys/stat.h>
#include <thread> //차선캘리

#include <QDebug>
#include <QProcess>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QDialog>
#include <QVBoxLayout>
#include <QPlainTextEdit>
#include <QScrollBar>
#include <QPushButton>
#include <QScrollArea>
#include <QScreen>
#include <QGuiApplication>
#include <QCheckBox>
#include <QTouchEvent>
#include <QHBoxLayout>
#include <QLabel>
#include <QFrame>
#include <QMessageBox>
#include <QFile>
#include <QDir>

#include "common/watchdog.h"
#include "common/util.h"
#include "selfdrive/ui/qt/network/networking.h"
#include "selfdrive/ui/qt/offroad/settings.h"
#include "selfdrive/ui/qt/qt_window.h"
#include "selfdrive/ui/qt/widgets/prime.h"
#include "selfdrive/ui/qt/widgets/scrollview.h"
#include "selfdrive/ui/qt/offroad/developer_panel.h"
#include "selfdrive/ui/qt/widgets/input.h"
#include "selfdrive/ui/qt/offroad/firehose.h"
#include "selfdrive/ui/qt/offroad/screenrecord_manager.h"
#include <QTimer>

// ===== 通用触摸滚动: 直接驱动滚动条, 1:1 跟手(带阻尼), 零惯性; 同时接住 c3 的 QTouchEvent 与合成鼠标 =====
// 用于浮层覆盖层列表(自写滚动, 替代 ScrollView 的 QScroller —— 后者在 c3 eglfs 模态框下会卡死事件循环)。
// 早期曾用官方 ScrollView(含 QScroller, 有惯性) 与自写 TouchListScroller(外部事件过滤器吞触摸, 会黑屏重启);
// 现统一用 FollowScrollArea(见下方类定义), 在 viewportEvent 内 1:1 直接驱动滚动条, 稳定不崩。


// 关键: 在 QScrollArea::viewportEvent 内处理触摸(而非外部 installEventFilter 吞事件), c3 eglfs 下不崩。

// 1:1 跟手滚动区: 仅对落在"非交互控件"区域的触摸做 1:1 平移; 落在复选框/按钮上的触摸放行给子控件(可正常点按)。
class FollowScrollArea : public QScrollArea {
  QPoint m_last;
  bool m_drag = false;
  bool m_pressedInteractive = false;
public:
  explicit FollowScrollArea(QWidget *content, QWidget *parent = nullptr) : QScrollArea(parent) {
    setWidget(content);
    setWidgetResizable(true);
    setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    setFrameShape(QFrame::NoFrame);
    if (viewport()) viewport()->setStyleSheet("background-color:#141414;");
    content->setStyleSheet("background-color:#141414;");
  }
  bool viewportEvent(QEvent *ev) override {
    switch (ev->type()) {
      case QEvent::TouchBegin: {
        QTouchEvent *te = static_cast<QTouchEvent*>(ev);
        if (te->touchPoints().isEmpty()) return QScrollArea::viewportEvent(ev);
        QPointF gp = te->touchPoints().first().screenPos();
        QWidget *hit = widget() ? widget()->childAt(widget()->mapFromGlobal(gp.toPoint())) : nullptr;
        m_pressedInteractive = (hit != nullptr && (hit->inherits("QAbstractButton") || hit->inherits("QAbstractSlider")));
        m_drag = !m_pressedInteractive;
        m_last = te->touchPoints().first().pos().toPoint();
        return m_drag ? true : QScrollArea::viewportEvent(ev);
      }
      case QEvent::TouchUpdate: {
        QTouchEvent *te = static_cast<QTouchEvent*>(ev);
        if (!m_drag || te->touchPoints().isEmpty()) return QScrollArea::viewportEvent(ev);
        QPoint p = te->touchPoints().first().pos().toPoint();
        int dy = p.y() - m_last.y();
        m_last = p;
        verticalScrollBar()->setValue(verticalScrollBar()->value() - dy);
        return true;
      }
      case QEvent::TouchEnd:
      case QEvent::TouchCancel:
        m_drag = false; m_pressedInteractive = false;
        return QScrollArea::viewportEvent(ev);
      default:
        return QScrollArea::viewportEvent(ev);
    }
  }
};

// 功能页分组小标题
static QWidget* sectionHeader(const QString& text) {
  QLabel *h = new QLabel(text);
  h->setStyleSheet("QLabel { color:#7fd0ff; font-size:30px; font-weight:bold; padding:20px 16px 8px 16px; background-color:transparent; }");
  return h;
}

// ============================================================================
TogglesPanel::TogglesPanel(SettingsWindow *parent) : ListWidget(parent) {
  // param, title, desc, icon
  std::vector<std::tuple<QString, QString, QString, QString>> toggle_defs{
    {
      "OpenpilotEnabledToggle",
      tr("Enable openpilot"),
      tr("Use the openpilot system for adaptive cruise control and lane keep driver assistance. Your attention is required at all times to use this feature. Changing this setting takes effect when the car is powered off."),
      "../assets/img_chffr_wheel.png",
    },
    {
      "ExperimentalMode",
      tr("Experimental Mode"),
      "",
      "../assets/img_experimental_white.svg",
    },
    {
      "DisengageOnAccelerator",
      tr("Disengage on Accelerator Pedal"),
      tr("When enabled, pressing the accelerator pedal will disengage openpilot."),
      "../assets/offroad/icon_disengage_on_accelerator.svg",
    },
    {
      "IsLdwEnabled",
      tr("Enable Lane Departure Warnings"),
      tr("Receive alerts to steer back into the lane when your vehicle drifts over a detected lane line without a turn signal activated while driving over 31 mph (50 km/h)."),
      "../assets/offroad/icon_warning.png",
    },
    {
      "AlwaysOnDM",
      tr("Always-On Driver Monitoring"),
      tr("Enable driver monitoring even when openpilot is not engaged."),
      "../assets/offroad/icon_monitoring.png",
    },
    {
      "RecordFront",
      tr("Record and Upload Driver Camera"),
      tr("Upload data from the driver facing camera and help improve the driver monitoring algorithm."),
      "../assets/offroad/icon_monitoring.png",
    },
    {
      "RecordAudio",
      tr("Record and Upload Microphone Audio"),
      tr("Record and store microphone audio while driving. The audio will be included in the dashcam video in comma connect."),
      "../assets/offroad/microphone.png",
    },
    {
      "IsMetric",
      tr("Use Metric System"),
      tr("Display speed in km/h instead of mph."),
      "../assets/offroad/icon_metric.png",
    },
  };


  std::vector<QString> longi_button_texts{tr("Aggressive"), tr("Standard"), tr("Relaxed") , tr("MoreRelaxed") };
  long_personality_setting = new ButtonParamControl("LongitudinalPersonality", tr("Driving Personality"),
                                          tr("Standard is recommended. In aggressive mode, openpilot will follow lead cars closer and be more aggressive with the gas and brake. "
                                             "In relaxed mode openpilot will stay further away from lead cars. On supported cars, you can cycle through these personalities with "
                                             "your steering wheel distance button."),
                                          "../assets/offroad/icon_speed_limit.png",
                                          longi_button_texts);

  // set up uiState update for personality setting
  QObject::connect(uiState(), &UIState::uiUpdate, this, &TogglesPanel::updateState);

  for (auto &[param, title, desc, icon] : toggle_defs) {
    auto toggle = new ParamControl(param, title, desc, icon, this);

    bool locked = params.getBool((param + "Lock").toStdString());
    toggle->setEnabled(!locked);

    addItem(toggle);
    toggles[param.toStdString()] = toggle;

    // insert longitudinal personality after NDOG toggle
    if (param == "DisengageOnAccelerator") {
      addItem(long_personality_setting);
    }
  }

  // Toggles with confirmation dialogs
  toggles["ExperimentalMode"]->setActiveIcon("../assets/img_experimental.svg");
  toggles["ExperimentalMode"]->setConfirmation(true, true);
}

void TogglesPanel::updateState(const UIState &s) {
  const SubMaster &sm = *(s.sm);

  if (sm.updated("selfdriveState")) {
    auto personality = sm["selfdriveState"].getSelfdriveState().getPersonality();
    if (personality != s.scene.personality && s.scene.started && isVisible()) {
      long_personality_setting->setCheckedButton(static_cast<int>(personality));
    }
    uiState()->scene.personality = personality;
  }
}

void TogglesPanel::expandToggleDescription(const QString &param) {
  toggles[param.toStdString()]->showDescription();
}

void TogglesPanel::showEvent(QShowEvent *event) {
  updateToggles();
}

void TogglesPanel::updateToggles() {
  auto experimental_mode_toggle = toggles["ExperimentalMode"];
  const QString e2e_description = QString("%1<br>"
                                          "<h4>%2</h4><br>"
                                          "%3<br>"
                                          "<h4>%4</h4><br>"
                                          "%5<br>")
                                  .arg(tr("openpilot defaults to driving in <b>chill mode</b>. Experimental mode enables <b>alpha-level features</b> that aren't ready for chill mode. Experimental features are listed below:"))
                                  .arg(tr("End-to-End Longitudinal Control"))
                                  .arg(tr("Let the driving model control the gas and brakes. openpilot will drive as it thinks a human would, including stopping for red lights and stop signs. "
                                          "Since the driving model decides the speed to drive, the set speed will only act as an upper bound. This is an alpha quality feature; "
                                          "mistakes should be expected."))
                                  .arg(tr("New Driving Visualization"))
                                  .arg(tr("The driving visualization will transition to the road-facing wide-angle camera at low speeds to better show some turns. The Experimental mode logo will also be shown in the top right corner."));

  const bool is_release = params.getBool("IsReleaseBranch");
  auto cp_bytes = params.get("CarParamsPersistent");
  if (!cp_bytes.empty()) {
    AlignedBuffer aligned_buf;
    capnp::FlatArrayMessageReader cmsg(aligned_buf.align(cp_bytes.data(), cp_bytes.size()));
    cereal::CarParams::Reader CP = cmsg.getRoot<cereal::CarParams>();

    if (hasLongitudinalControl(CP)) {
      // normal description and toggle
      experimental_mode_toggle->setEnabled(true);
      experimental_mode_toggle->setDescription(e2e_description);
      long_personality_setting->setEnabled(true);
    } else {
      // no long for now
      experimental_mode_toggle->setEnabled(false);
      long_personality_setting->setEnabled(false);
      params.remove("ExperimentalMode");

      const QString unavailable = tr("Experimental mode is currently unavailable on this car since the car's stock ACC is used for longitudinal control.");

      QString long_desc = unavailable + " " + \
                          tr("openpilot longitudinal control may come in a future update.");
      if (CP.getAlphaLongitudinalAvailable()) {
        if (is_release) {
          long_desc = unavailable + " " + tr("An alpha version of openpilot longitudinal control can be tested, along with Experimental mode, on non-release branches.");
        } else {
          long_desc = tr("Enable the openpilot longitudinal control (alpha) toggle to allow Experimental mode.");
        }
      }
      experimental_mode_toggle->setDescription("<b>" + long_desc + "</b><br><br>" + e2e_description);
    }

    experimental_mode_toggle->refresh();
  } else {
    experimental_mode_toggle->setDescription(e2e_description);
  }
}

DevicePanel::DevicePanel(SettingsWindow *parent) : ListWidget(parent) {
  setSpacing(50);

  // === 商用授权激活码(设备页第一位; 绑定设备序列号; 复用 InputDialog 屏幕键盘, c3 可用) ===
  ButtonControl *licBtn = new ButtonControl(tr("授权激活码"), tr("输入"),
    "点击[输入]后用屏幕键盘粘贴卖家发给你的激活码。找卖家购买时需提供你的设备序列号(下方 Serial 项)。激活成功后付费功能开启; 到期自动回退基础版, 不影响行车安全。");
  connect(licBtn, &ButtonControl::clicked, [this]() {
    std::string kcur = params.get("CarrotActivationCode");
    QString cur = kcur.empty() ? QString() : QString::fromStdString(kcur);
    QString serial = QString::fromStdString(params.get("HardwareSerial"));
    QString v = InputDialog::getText(QString("输入授权激活码"), this,
      QString("粘贴卖家发给你的激活码(区分大小写)。\n本机序列号: ") + serial + QString("\n留空则清除激活码(退回基础版)。"), false, -1, cur);
    if (v.isNull()) return;
    std::string s = v.trimmed().toStdString();
    while (!s.empty() && (s.back()=='\n' || s.back()=='\r' || s.back()==' ')) s.pop_back();
    while (!s.empty() && (s.front()=='\n' || s.front()=='\r' || s.front()==' ')) s.erase(s.begin());
    params.put("CarrotActivationCode", s);
    if (s.empty()) {
      ConfirmationDialog::alert(tr("激活码已清除, 当前为基础版"), this);
      if (lic_remain_lbl_) lic_remain_lbl_->setText(tr("未激活 / 次数用完，请购买激活码"));
      return;
    }
    // 写临时文件后调用 license.py 校验(避免激活码出现在命令行)
    std::string code_path = "/tmp/carrot_act_code.txt";
    { std::ofstream ofs(code_path); ofs << s; }
    std::string cmd = "cd /data/openpilot && PYTHONPATH=/data/openpilot /usr/local/venv/bin/python selfdrive/carrot/license.py --check-code " + code_path + " 2>&1";
    FILE *fp = popen(cmd.c_str(), "r");
    std::string out;
    if (fp) { char buf[512]; while (fgets(buf, sizeof(buf), fp)) out += buf; pclose(fp); }
    // 实时刷新剩余激活次数显示(输入激活码后立即生效, 无需重进页面/重启)
    if (lic_remain_lbl_) {
      bool act = (params.get("CarrotLicStatus") == "1");
      std::string remain = params.get("CarrotLicRemain");
      if (act) {
        lic_remain_lbl_->setText(tr("已激活，剩余 ") + QString::fromStdString(remain) + tr(" 次"));
      } else {
        lic_remain_lbl_->setText(tr("未激活 / 次数用完，请购买激活码"));
      }
    }
    ConfirmationDialog::alert(QString::fromStdString(out), this);
  });
  addItem(licBtn);

  // === 商用授权: 剩余激活次数显示 (showEvent 刷新) ===
  lic_remain_lbl_ = new LabelControl(tr("剩余激活次数"), tr("读取中"));
  addItem(lic_remain_lbl_);

  // on/off road mode switch: 置于设备页首位, Dongle ID 次之
  onOffRoadBtn = new ButtonControl(tr("Onroad/Offroad Mode"), tr("Go Offroad"));
  connect(onOffRoadBtn, &ButtonControl::clicked, [=]() {
    if (ConfirmationDialog::confirm(tr("Are you sure you want to switch mode?"), tr("CONFIRM"), this)) {
      bool val = params.getBool("device_go_off_road");
      params.putBool("device_go_off_road", !val);
      updateOnOffRoadBtn();
    }
  });
  addItem(onOffRoadBtn);

//   addItem(new LabelControl(tr("Dongle ID"), getDongleId().value_or(tr("N/A"))));

  onoffroad_watch = new ParamWatcher(this);
  onoffroad_watch->addParam("device_go_off_road");
  QObject::connect(onoffroad_watch, &ParamWatcher::paramChanged, [=](const QString &param_name, const QString &param_value) {
    updateOnOffRoadBtn();
  });
  updateOnOffRoadBtn();

  addItem(new LabelControl(tr("Serial"), params.get("HardwareSerial").c_str()));

  // power buttons
  QHBoxLayout* power_layout = new QHBoxLayout();
  power_layout->setSpacing(18);

  QPushButton* reboot_btn = new QPushButton(tr("重启"));
  reboot_btn->setObjectName("reboot_btn");
  power_layout->addWidget(reboot_btn, 1);
  QObject::connect(reboot_btn, &QPushButton::clicked, this, &DevicePanel::reboot);
  //차선캘리
  QPushButton *reset_CalibBtn = new QPushButton(tr("重新校准"));
  reset_CalibBtn->setObjectName("reset_CalibBtn");
  power_layout->addWidget(reset_CalibBtn, 1);
  QObject::connect(reset_CalibBtn, &QPushButton::clicked, this, &DevicePanel::calibration);

  QPushButton* poweroff_btn = new QPushButton(tr("关机"));
  poweroff_btn->setObjectName("poweroff_btn");
  power_layout->addWidget(poweroff_btn, 1);
  QObject::connect(poweroff_btn, &QPushButton::clicked, this, &DevicePanel::poweroff);

  QPushButton* default_btn = new QPushButton(tr("恢复默认"));
  default_btn->setObjectName("default_btn");
  power_layout->addWidget(default_btn, 1);
  QObject::connect(default_btn, &QPushButton::clicked, [&]() {
    if (ConfirmationDialog::confirm(tr("恢复为默认设置？"), tr("是"), this)) {
      QTimer::singleShot(1000, []() {
        Params().putInt("SoftRestartTriggered", 2);
      });
    }
  });

  if (false && !Hardware::PC()) {
      connect(uiState(), &UIState::offroadTransition, poweroff_btn, &QPushButton::setVisible);
  }

  setStyleSheet(R"(
    #reboot_btn { height: 120px; border-radius: 15px; background-color: #2CE22C; }
    #reboot_btn:pressed { background-color: #24FF24; }
    #reset_CalibBtn { height: 120px; border-radius: 15px; background-color: #FFBB00; }
    #reset_CalibBtn:pressed { background-color: #FF2424; }
    #poweroff_btn { height: 120px; border-radius: 15px; background-color: #E22C2C; }
    #poweroff_btn:pressed { background-color: #FF2424; }
    #default_btn { height: 120px; border-radius: 15px; background-color: #BDBDBD; }
    #default_btn:pressed { background-color: #A9A9A9; }
  )");
  addItem(power_layout);

  pair_device = new ButtonControl(tr("Pair Device"), tr("PAIR"),
                                  tr("Pair your device with comma connect (connect.comma.ai) and claim your comma prime offer."));
  connect(pair_device, &ButtonControl::clicked, [=]() {
    PairingPopup popup(this);
    popup.exec();
  });
  addItem(pair_device);

  // offroad-only buttons

  // --- 设备维护开关：禁用更新 ---
  auto disableUpdatesToggle = new ParamControl("DisableUpdates", tr("禁用更新"),
                                   tr("打开后禁止系统自动更新(OTA)，关闭后恢复自动更新。修改后需重启设备生效。"),
                                   "", this);
  addItem(disableUpdatesToggle);

  // --- 设备维护开关：开机编译跳过 ---
  auto skipBuildToggle = new ParamControl("SkipBootBuild", tr("开机编译跳过"),
                                  tr("打开后开机不再重新编译 openpilot，直接跳过编译启动(更省时)。打开会立即重启设备。"),
                                  "", this);
  QObject::connect(skipBuildToggle, &ParamControl::toggleFlipped, [=](bool state) {
    if (state) {
      system("touch /data/openpilot/prebuilt");
      Params().putBool("DoReboot", true);
    } else {
      system("rm -f /data/openpilot/prebuilt");
    }
  });
  addItem(skipBuildToggle);

  auto dcamBtn = new ButtonControl(tr("Driver Camera"), tr("PREVIEW"),
                                   tr("Preview the driver facing camera to ensure that driver monitoring has good visibility. (vehicle must be off)"));
  connect(dcamBtn, &ButtonControl::clicked, [=]() { emit showDriverView(); });
  addItem(dcamBtn);

  auto translateBtn = new ButtonControl(tr("Change Language"), tr("CHANGE"), "");
  connect(translateBtn, &ButtonControl::clicked, [=]() {
    QMap<QString, QString> langs = getSupportedLanguages();
    QString selection = MultiOptionDialog::getSelection(tr("Select a language"), langs.keys(), langs.key(uiState()->language), this);
    if (!selection.isEmpty()) {
      // put language setting, exit Qt UI, and trigger fast restart
      params.put("LanguageSetting", langs[selection].toStdString());
      qApp->exit(18);
      watchdog_kick(0);
    }
  });
  addItem(translateBtn);

  QObject::connect(uiState()->prime_state, &PrimeState::changed, [this] (PrimeState::Type type) {
    pair_device->setVisible(type == PrimeState::PRIME_TYPE_UNPAIRED);
  });
  QObject::connect(uiState(), &UIState::offroadTransition, [=](bool offroad) {
    for (auto btn : findChildren<ButtonControl *>()) {
      if (btn != pair_device && btn != onOffRoadBtn) {
        btn->setEnabled(offroad);
      }
    }
    translateBtn->setEnabled(true);
    // the onroad/offroad switch must stay usable while onroad
    if (onOffRoadBtn != nullptr) {
      onOffRoadBtn->setEnabled(true);
      updateOnOffRoadBtn();
    }
  });

}

void DevicePanel::updateOnOffRoadBtn() {
  if (onOffRoadBtn == nullptr) return;
  if (params.getBool("device_go_off_road")) {
    onOffRoadBtn->setText(tr("Go Onroad"));
  } else {
    onOffRoadBtn->setText(tr("Go Offroad"));
  }
}

void DevicePanel::showEvent(QShowEvent *event) {
  ListWidget::showEvent(event);
  updateOnOffRoadBtn();
  // 商用授权: 刷新剩余激活次数显示
  if (lic_remain_lbl_) {
    bool act = (params.get("CarrotLicStatus") == "1");
    std::string remain = params.get("CarrotLicRemain");
    if (act) {
      lic_remain_lbl_->setText(tr("已激活，剩余 ") + QString::fromStdString(remain) + tr(" 次"));
    } else {
      lic_remain_lbl_->setText(tr("未激活 / 次数用完，请购买激活码"));
    }
  }
}

void DevicePanel::updateCalibDescription() {
  QString desc =
      tr("openpilot requires the device to be mounted within 4° left or right and "
         "within 5° up or 9° down. openpilot is continuously calibrating, resetting is rarely required.");
  std::string calib_bytes = params.get("CalibrationParams");
  if (!calib_bytes.empty()) {
    try {
      AlignedBuffer aligned_buf;
      capnp::FlatArrayMessageReader cmsg(aligned_buf.align(calib_bytes.data(), calib_bytes.size()));
      auto calib = cmsg.getRoot<cereal::Event>().getLiveCalibration();
      if (calib.getCalStatus() != cereal::LiveCalibrationData::Status::UNCALIBRATED) {
        double pitch = calib.getRpyCalib()[1] * (180 / M_PI);
        double yaw = calib.getRpyCalib()[2] * (180 / M_PI);
        desc += tr(" Your device is pointed %1° %2 and %3° %4.")
                    .arg(QString::number(std::abs(pitch), 'g', 1), pitch > 0 ? tr("down") : tr("up"),
                         QString::number(std::abs(yaw), 'g', 1), yaw > 0 ? tr("left") : tr("right"));
      }
    } catch (kj::Exception) {
      qInfo() << "invalid CalibrationParams";
    }
  }
  qobject_cast<ButtonControl *>(sender())->setDescription(desc);
}

void DevicePanel::reboot() {
  if (!uiState()->engaged()) {
    if (ConfirmationDialog::confirm(tr("Are you sure you want to reboot?"), tr("Reboot"), this)) {
      // Check engaged again in case it changed while the dialog was open
      if (!uiState()->engaged()) {
        params.putBool("DoReboot", true);
      }
    }
  } else {
    ConfirmationDialog::alert(tr("Disengage to Reboot"), this);
  }
}

//차선캘리
void execAndReboot(const std::string& cmd) {
    printf("exec cmd: %s\n", cmd.c_str());
    system(cmd.c_str());
    Params().putBool("DoReboot", true);
}

void DevicePanel::calibration() {
  if (!uiState()->engaged()) {
    QStringList calibOptions;
    calibOptions << tr("AllCalibParams")
                 << tr("CalibrationParams")
                 << tr("AllLiveParams");

    QString selectedParam = MultiOptionDialog::getSelection(
      tr("Select calibration parameter to reset"),
      calibOptions,
      "",
      this
    );

    if (selectedParam.isEmpty()) return;

    QString confirmMsg = tr("Are you sure you want to reset %1?").arg(selectedParam);
    if (!ConfirmationDialog::confirm(confirmMsg, tr("ReCalibration"), this)) return;

    if (uiState()->engaged()) {
      ConfirmationDialog::alert(tr("Reboot & Disengage to Calibration"), this);
      return;
    }

    std::thread worker([selectedParam]() {
      std::string base = "/data/params/d_tmp";
      std::string cmd;

      if (selectedParam == "AllCalibParams" || selectedParam == "所有校准参数") {
        cmd = "cd " + base + " && rm -f CalibrationParams LiveParameters LiveParametersV2 LiveTorqueParameters LiveDelay";
      } else {
        if(selectedParam == "CalibrationParams" || selectedParam == "相机校准参数"){
          cmd = "cd " + base + " && rm -f CalibrationParams";
        }else if(selectedParam == "AllLiveParams" || selectedParam == "实时学习参数"){
          cmd = "cd " + base + " && rm -f LiveParameters LiveParametersV2 LiveTorqueParameters LiveDelay";
        }
      }

      execAndReboot(cmd);
    });
    worker.detach();
  } else {
    ConfirmationDialog::alert(tr("Reboot & Disengage to Calibration"), this);
  }
}

void DevicePanel::poweroff() {
  if (!uiState()->engaged()) {
    if (ConfirmationDialog::confirm(tr("Are you sure you want to power off?"), tr("Power Off"), this)) {
      // Check engaged again in case it changed while the dialog was open
      if (!uiState()->engaged()) {
        params.putBool("DoShutdown", true);
      }
    }
  } else {
    ConfirmationDialog::alert(tr("Disengage to Power Off"), this);
  }
}

void SettingsWindow::showEvent(QShowEvent *event) {
  setCurrentPanel(0);
}

void SettingsWindow::setCurrentPanel(int index, const QString &param) {
  if (!param.isEmpty()) {
    // Check if param ends with "Panel" to determine if it's a panel name
    if (param.endsWith("Panel")) {
      QString panelName = param;
      panelName.chop(5); // Remove "Panel" suffix

      // Find the panel by name
      for (int i = 0; i < nav_btns->buttons().size(); i++) {
        if (nav_btns->buttons()[i]->text() == tr(panelName.toStdString().c_str())) {
          index = i;
          break;
        }
      }
    } else {
      emit expandToggleDescription(param);
    }
  }

  panel_widget->setCurrentIndex(index);
  nav_btns->buttons()[index]->setChecked(true);
}

SettingsWindow::SettingsWindow(QWidget *parent) : QFrame(parent) {

  // setup two main layouts
  sidebar_widget = new QWidget;
  QVBoxLayout *sidebar_layout = new QVBoxLayout(sidebar_widget);
  panel_widget = new QStackedWidget();

  // close button
  QPushButton *close_btn = new QPushButton(tr("×"));
  close_btn->setStyleSheet(R"(
    QPushButton {
      font-size: 140px;
      padding-bottom: 20px;
      border-radius: 100px;
      background-color: #292929;
      font-weight: 400;
    }
    QPushButton:pressed {
      background-color: #3B3B3B;
    }
  )");
  close_btn->setFixedSize(200, 200);
  sidebar_layout->addSpacing(45);
  sidebar_layout->addWidget(close_btn, 0, Qt::AlignCenter);
  QObject::connect(close_btn, &QPushButton::clicked, this, &SettingsWindow::closeSettings);

  // setup panels
  DevicePanel *device = new DevicePanel(this);
  QObject::connect(device, &DevicePanel::reviewTrainingGuide, this, &SettingsWindow::reviewTrainingGuide);
  QObject::connect(device, &DevicePanel::showDriverView, this, &SettingsWindow::showDriverView);

  TogglesPanel *toggles = new TogglesPanel(this);
  QObject::connect(this, &SettingsWindow::expandToggleDescription, toggles, &TogglesPanel::expandToggleDescription);

  auto networking = new Networking(this);
  QObject::connect(uiState()->prime_state, &PrimeState::changed, networking, &Networking::setPrimeType);

  QList<QPair<QString, QWidget *>> panels = {
    {tr("Device"), device},
    { tr("功能"), new CarrotPanel(this, 1) },
    { tr("录像"), new ScreenRecordManager(this) },  // 内嵌面板: 点标签即显示, 不弹窗
    {tr("Network"), networking},
    {tr("Toggles"), toggles},
  };
  if(Params().getBool("SoftwareMenu")) {
    panels.append({tr("Software"), new SoftwarePanel(this)});
  }
  if(false) {
    panels.append({tr("Firehose"), new FirehosePanel(this)});
  }
  panels.append({ tr("萝卜"), new CarrotPanel(this) });
  dev_panel_ = new DeveloperPanel(this);
  panels.append({ tr("开发"), dev_panel_ });

  nav_btns = new QButtonGroup(this);
  for (auto &[name, panel] : panels) {
    QPushButton *btn = new QPushButton(name);
    btn->setCheckable(true);
    btn->setChecked(nav_btns->buttons().size() == 0);
    btn->setStyleSheet(R"(
      QPushButton {
        color: grey;
        border: none;
        background: none;
        font-size: 65px;
        font-weight: 500;
      }
      QPushButton:checked {
        color: white;
      }
      QPushButton:pressed {
        color: #ADADAD;
      }
    )");
    btn->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Expanding);
    nav_btns->addButton(btn);
    sidebar_layout->addWidget(btn, 0, Qt::AlignRight);

    const int lr_margin = name != tr("Network") ? 50 : 0;  // Network panel handles its own margins
    panel->setContentsMargins(lr_margin, 25, lr_margin, 25);

    ScrollView *panel_frame = new ScrollView(panel, this);
    panel_widget->addWidget(panel_frame);

    // 录像面板内嵌进 settings(和功能菜单一样), 点标签直接切换显示, 不再弹模态框(避免 eglfs 死锁/卡死)
    // 注意: 必须用 [=] 按值捕获 panel_widget/btn(this 为指针, 拷贝即安全);
    //       若用 [&] 引用捕获, setupSettings 返回后这些局部变量已销毁, 点击时访问悬垂指针 -> SIGSEGV 整个 ui 崩溃
    QObject::connect(btn, &QPushButton::clicked, [=, w = panel_frame]() {
      btn->setChecked(true);
      panel_widget->setCurrentWidget(w);
    });

    // 商用授权: 点「开发」标签文字累计 6 次解锁 SSH 显示; 点其他标签清零(不跨菜单累计)
    if (name == tr("开发")) {
      QObject::connect(btn, &QPushButton::clicked, [this]() {
        dev_click_count_++;
        if (dev_panel_ && dev_click_count_ >= 6) {
          dev_click_count_ = 0;
          dev_panel_->unlockSsh();
        }
      });
    } else {
      QObject::connect(btn, &QPushButton::clicked, [this]() {
        dev_click_count_ = 0;
      });
    }
  }
  sidebar_layout->setContentsMargins(50, 50, 100, 50);

  // main settings layout, sidebar + main panel
  QHBoxLayout *main_layout = new QHBoxLayout(this);

  sidebar_widget->setFixedWidth(500);
  main_layout->addWidget(sidebar_widget);
  main_layout->addWidget(panel_widget);

  setStyleSheet(R"(
    * {
      color: white;
      font-size: 50px;
    }
    SettingsWindow {
      background-color: black;
    }
    QStackedWidget, ScrollView {
      background-color: #292929;
      border-radius: 30px;
    }
  )");
}


#include <QTouchEvent>
#include <QMouseEvent>
#include <QListWidget>

static QStringList get_list(const char* path) {
  QStringList stringList;
  QFile textFile(path);
  if (textFile.open(QIODevice::ReadOnly)) {
    QTextStream textStream(&textFile);
    while (true) {
      QString line = textStream.readLine();
      if (line.isNull()) {
        break;
      } else {
        stringList.append(line);
      }
    }
  }
  return stringList;
}

CarrotPanel::CarrotPanel(QWidget* parent, int mode) : QWidget(parent) {
  panelMode = mode;
  main_layout = new QStackedLayout(this);
  homeScreen = new QWidget(this);
  carrotLayout = new QVBoxLayout(homeScreen);
  carrotLayout->setMargin(10);

  if (panelMode == 0) {   // 一级「功能」面板为单页, 不显示标签按钮栏
  QHBoxLayout* select_layout = new QHBoxLayout();
  select_layout->setSpacing(10);


  QPushButton* start_btn = new QPushButton(tr("开始"));
  start_btn->setObjectName("start_btn");
  QObject::connect(start_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 0;
    this->togglesCarrot(0);
    updateButtonStyles();
  });

  QPushButton* cruise_btn = new QPushButton(tr("巡航"));
  cruise_btn->setObjectName("cruise_btn");
  QObject::connect(cruise_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 1;
    this->togglesCarrot(1);
    updateButtonStyles();
  });

  QPushButton* nav_btn = new QPushButton(tr("导航"));
  nav_btn->setObjectName("nav_btn");
  QObject::connect(nav_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 2;
    this->togglesCarrot(2);
    updateButtonStyles();
  });

  QPushButton* speed_btn = new QPushButton(tr("速度"));
  speed_btn->setObjectName("speed_btn");
  QObject::connect(speed_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 3;
    this->togglesCarrot(3);
    updateButtonStyles();
  });

  QPushButton* latLong_btn = new QPushButton(tr("调节"));
  latLong_btn->setObjectName("latLong_btn");
  QObject::connect(latLong_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 4;
    this->togglesCarrot(4);
    updateButtonStyles();
  });

  QPushButton* disp_btn = new QPushButton(tr("显示"));
  disp_btn->setObjectName("disp_btn");
  QObject::connect(disp_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 5;
    this->togglesCarrot(5);
    updateButtonStyles();
  });

  QPushButton* track_btn = new QPushButton(tr("轨迹"));
  track_btn->setObjectName("track_btn");
  QObject::connect(track_btn, &QPushButton::clicked, this, [this]() {
    this->currentCarrotIndex = 6;
    this->togglesCarrot(6);
    updateButtonStyles();
  });


  updateButtonStyles();

  select_layout->addWidget(start_btn);
  select_layout->addWidget(cruise_btn);
  select_layout->addWidget(nav_btn);
  select_layout->addWidget(speed_btn);
  select_layout->addWidget(latLong_btn);
  select_layout->addWidget(disp_btn);
  select_layout->addWidget(track_btn);
  carrotLayout->addLayout(select_layout, 0);
  }

  QWidget* toggles = new QWidget();
  QVBoxLayout* toggles_layout = new QVBoxLayout(toggles);

  if (panelMode == 0) {
  cruiseToggles = new ListWidget(this);
  cruiseToggles->addItem(new CValueControl("CruiseButtonMode", "按钮：定速巡航模式", "方向盘定速巡航实体按键的功能映射模式。0=普通(原厂按键逻辑);1=用户自定义模式1;2=用户自定义模式2(可重映射按键含义)。调大切换不同映射;0即原厂,改动需按车型适配按键。", 0, 2, 1));
  cruiseToggles->addItem(new CValueControl("CancelButtonMode", "按钮：取消模式", "取消巡航按键方式。0=仅长按取消定速,横向车道保持保留;1=长按取消且同时保留车道保持(只退纵向不退横向)。调大=取消时保留更多辅助,不丢横向。", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("LfaButtonMode", "按钮：LFA模式", "LFA按键触发行为(现代)。0=普通;1=触发减速/停车/前车准备等增强安全动作。开启(1)后按LFA键执行更多主动安全功能。", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("CruiseSpeedUnitBasic", "按钮：定速单位(基础)", "基础定速速度显示单位。1=公里/小时(km/h);2=英里/小时(mph)。仅影响屏幕显示,不改变任何控制逻辑。", 1, 20, 1));
  cruiseToggles->addItem(new CValueControl("CruiseSpeedUnit", "按钮：定速单位(高级)", "高级定速(可超速段)速度显示单位。1=km/h;2=mph。仅影响显示。", 1, 20, 1));
  cruiseToggles->addItem(new CValueControl("CruiseEcoControl", "定速：节能控制(4km/h)", "定速节能控制抬升幅度(单位km/h)。节能模式临时提高设定速度以省油。调大=抬升越多越省油但车速更快;设为0=关闭节能抬升(原厂行为)。", 0, 10, 1));
  cruiseToggles->addItem(new CValueControl("AutoSpeedUptoRoadSpeedLimit", "定速：自动提速至道路限速(0%)", "巡航设定速度自动提到道路限速的百分比(x%)。0=关闭;>0=设定速度自动=道路限速×x%。调大=更贴近限速(开得更快);调小=更保守;0=完全按手动设定。", 0, 200, 10));
  cruiseToggles->addItem(new CValueControl("TFollowGap1", "跟车时间GAP1 x0.01s", "跟车时间档位GAP1对应的时距(秒)=值×0.01。调大=跟车距离更远更保守安全;调小=跟车更近更激进。GAP2/3/4为其余各档,逻辑相同。", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap2", "跟车时间GAP2 x0.01s", "跟车时间档位GAP2对应的时距(秒)=值×0.01。调大=该档跟车更远更保守;调小=更近更激进。", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap3", "跟车时间GAP3 x0.01s", "跟车时间档位GAP3对应的时距(秒)=值×0.01。调大=该档跟车更远更保守;调小=更近更激进。", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap4", "跟车时间GAP4 x0.01s", "跟车时间档位GAP4对应的时距(秒)=值×0.01。调大=该档跟车更远更保守;调小=更近更激进。", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("DynamicTFollow", "动态跟车GAP控制", "动态跟车GAP控制强度(基于前车速度动态调节时距)。调大=动态范围越大(前车慢时拉更远,前车快时收更近);设为0=关闭,使用固定时距。", 0, 100, 5));
  cruiseToggles->addItem(new CValueControl("DynamicTFollowLC", "动态跟车GAP控制(变道)", "变道过程中的动态跟车GAP控制强度。逻辑同动态跟车,但仅作用于变道时段。调大=变道时跟车更动态;设为0=关闭。", 0, 100, 5));
  cruiseToggles->addItem(new CValueControl("MyDrivingMode", "驾驶模式选择", "整体驾驶风格。1=经济(最柔最省油);2=安全(偏保守);3=普通(均衡);4=激进(加速猛/跟车近)。调大=更激进更快;调小=更温和更省。", 1, 4, 1));
  cruiseToggles->addItem(new CValueControl("MyDrivingModeAuto", "驾驶模式自动", "驾驶模式自动切换。0=关闭(固定使用手动选择的模式);1=开启(仅在普通模式3下根据路况自动微调风格)。", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("TrafficLightDetectMode", "红绿灯检测模式", "红绿灯识别。0=关闭;1=仅识别红灯并在停止线前停车;2=停走模式(红灯停、绿灯行,含自动起步)。调大=识别更主动(含起步);0=完全不识别红绿灯。", 0, 2, 1));

  //cruiseToggles->addItem(new CValueControl("CruiseSpeedMin", "CRUISE: Speed Lower limit(10)", "Cruise control MIN speed", 5, 50, 1));
  //cruiseToggles->addItem(new CValueControl("AutoResumeFromGas", "GAS CRUISE ON: Use", "Auto Cruise on when GAS pedal released, 60% Gas Cruise On automatically", 0, 3, 1));
  //cruiseToggles->addItem(new CValueControl("AutoResumeFromGasSpeed", "GAS CRUISE ON: Speed(30)", "Driving speed exceeds the set value, Cruise ON", 20, 140, 5));
  //cruiseToggles->addItem(new CValueControl("TFollowSpeedAddM", "GAP: Additional TFs 40km/h(0)x0.01s", "Speed-dependent additional max(100km/h) TFs", -100, 200, 5));
  //cruiseToggles->addItem(new CValueControl("TFollowSpeedAdd", "GAP: Additional TFs 100Km/h(0)x0.01s", "Speed-dependent additional max(100km/h) TFs", -100, 200, 5));
  //cruiseToggles->addItem(new CValueControl("MyEcoModeFactor", "DRIVEMODE: ECO Accel ratio(80%)", "Acceleration ratio in ECO mode", 10, 95, 5));
  //cruiseToggles->addItem(new CValueControl("MySafeModeFactor", "DRIVEMODE: SAFE ratio(60%)", "Accel/StopDistance/DecelRatio/Gap control ratio", 10, 90, 10));
  //cruiseToggles->addItem(new CValueControl("MyHighModeFactor", "DRIVEMODE: HIGH ratio(100%)", "AccelRatio control ratio", 100, 300, 10));

  latLongToggles = new ListWidget(this);

  // 舒适跟车/加塞 参数(调节菜单最前)
  latLongToggles->addItem(new CValueControl("ComfortLongMode", "舒适纵向模式", "舒适纵向总开关。0=原厂(默认,所有舒适化关闭);1=自定义(读取下方全部参数);2=套用本车型推荐预设(现代/大众/特斯拉各不同,其余9种车仅走通用平滑)。想舒适选2,想微调选1。", 0, 2, 1));
  latLongToggles->addItem(new CValueControl("LongAccelSmoothDown", "减速变化率限制 x0.001", "减速/加塞时的加速度变化率上限(x0.001,即值/1000 为 m/s³)。通用层对所有车型生效。调小=减速更缓更柔(加塞不突兀);调大=减速更干脆。默认20(=0.020),各车型均建议从20起按体感微调。", 1, 200, 1));
  latLongToggles->addItem(new CValueControl("LongAccelSmoothUp", "加速变化率限制 x0.001", "加速/起步时的加速度变化率上限(x0.001)。通用层对所有车型生效。调小=起步加速更柔(不蹿);调大=起步更猛。默认40(=0.040),各车型均建议从40起按体感微调。", 1, 400, 1));
  {
    bool is_jerk_brand = false;
    auto cp_bytes = Params().get("CarParamsPersistent");
    if (!cp_bytes.empty()) {
      AlignedBuffer aligned_buf;
      capnp::FlatArrayMessageReader cmsg(aligned_buf.align(cp_bytes.data(), cp_bytes.size()));
      cereal::CarParams::Reader CP = cmsg.getRoot<cereal::CarParams>();
      std::string brand = CP.getBrand();
      is_jerk_brand = (brand == "hyundai" || brand == "volkswagen" || brand == "tesla");
    }
    if (is_jerk_brand) {
      latLongToggles->addItem(new CValueControl("LongJerkMax", "jerk 上限 x0.1", "jerk上限(x0.1,即值/10 为 m/s³)。仅现代/大众/特斯拉显示生效。现代默认15(=1.5),大众对称1.5,特斯拉49(=4.9,单位m/s³,超限触发ACC故障已clamp)。调大=允许更急的jerk(更跟脚但更颠);调小=更柔。其余9种车无车端jerk字段,不显示此项。", 5, 50, 1));
      latLongToggles->addItem(new CValueControl("LongJerkGain", "jerk 增益 x0.1", "jerk增益(x0.1)。仅现代生效(大众/特斯拉为对称jerk无需)。现代默认15(=1.5)。调大=jerk响应更敏感;调小=更迟钝平顺。", 5, 50, 1));
      latLongToggles->addItem(new CValueControl("LongJerkMinBound", "jerk 下限 x0.1", "jerk下限(x0.1)。仅现代生效。现代默认5(=0.5),保证jerk不低于此值避免过软。调大=下限更高(始终较硬);调小=允许更软。", 0, 20, 1));
    }
  }
  latLongToggles->addItem(new CValueControl("UseLaneLineSpeed", "车道线模式速度(0)", "启用基于车道线(lat_mpc)横向控制的速度阈值(base)。0=关闭(使用模型路径);>0=在设定速度以下启用车道线模式。调大=启用车道线模式的车速上限越高(更晚切回模型路径)。", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("UseLaneLineCurveSpeed", "车道线模式弯道速度(0)", "车道线全模式(更贴车道线)所需的弯道速度阈值,仅高速生效。调大=更高弯道速度才进入车道线全模式;调小=更早进入。", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("AdjustLaneOffset", "车道偏移调整(0)cm", "横向路径相对车道中心的整体偏移(厘米)。调大=车辆整体偏右行驶;调小(含负值)=偏左。用于修正车道居中固定偏差使行驶更居中。", 0, 500, 5));
  latLongToggles->addItem(new CValueControl("AdjustCurveOffset", "弯道偏移调整(0)cm", "弯道中路径偏移(厘米)。调大=弯道中偏右;调小=偏左。修正弯道居中偏差用。", -500, 500, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeNeedTorque", "轻推方向变道", "打灯自动变道是否需要方向盘施力。-1=完全禁用自动变道;0=打灯即变(无需扶盘);1=需轻推方向盘才变道。调大=变道需更多人为参与;设-1=彻底关掉自动变道。", -1, 1, 1));
  latLongToggles->addItem(new CValueControl("AutoLaneChangeMinSpeed", "打灯变道最低速度", "自动变道的最低车速(km/h)。低于此速度打灯不变道。调大=更高速度才允许变道(更保守);设为-1=关闭最低速度限制(谨慎)。", -1, 100, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeDelay", "变道延迟", "打灯到开始变道的延迟(单位x0.1秒)。调大=更晚开始变道(更谨慎,给周围车更多反应);调小=更快变道。", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeBsd", "变道盲区BSD设置", "变道时盲区监测(BSD)处理。-1=无视盲区直接变;0=有盲区时不自动变但轻推可强制;1=有盲区即使轻推也不变道。调大=更保守(有盲区绝不强行变)。", -1, 1, 1));
  latLongToggles->addItem(new CValueControl("CustomSteerOffset", "横向: 自定义方向盘偏移(0)", "启用自定义方向盘零位角度偏移。0=关(用自学习/原厂零位);1=开(使用下方SteerAngleOffset值)。一般车辆无需开启。", 0, 1, 1));
  latLongToggles->addItem(new CValueControl("SteerAngleOffset", "横向: 方向盘偏移角度x0.1(0)", "方向盘零位偏移角度(x0.1度)。车直行时方向盘不正时用。正数=向右偏修正,负数=向左偏修正。需先开CustomSteerOffset。", -100, 100, 1));
  latLongToggles->addItem(new CValueControl("CustomSR", "横向: 自定义方向盘比x0.1(0)", "自定义转向比(x0.1)。0=使用自学习转向比;>0=指定固定转向比(如胜达建议165即16.5)。调大=转向比越大方向越沉稳(小角度转向变化小);调小=更灵敏。", 0, 300, 1));
  latLongToggles->addItem(new CValueControl("SteerRatioRate", "横向: 转向比应用速率x0.01(100)", "实时学习转向比的应用速率(x0.01)。学习到的转向比乘此系数作为最终值。调大=应用越快(紧跟学习值);调小=变化越平缓。默认100(=1.0)。", 30, 170, 1));
  latLongToggles->addItem(new CValueControl("PathOffset", "横向: 路径偏移", "路径整体横向偏移(单位经×0.01)。负数=路径左偏,正数=路径右偏。调大=规划路径整体右移(车随之偏右)。", -150, 150, 1));
  latLongToggles->addItem(new CValueControl("SteerActuatorDelay", "横向: 转向执行器延迟(30)", "转向执行器延迟补偿(x0.01秒)。调大=提前更多量打方向以补偿执行迟滞(转向更跟手);0=使用自学习延迟。默认30(=0.30s)。", 0, 100, 1));
  latLongToggles->addItem(new CValueControl("LateralTorqueCustom", "横向: 自定义扭矩模式(0),", "横向扭矩控制模式。0=使用自学习的扭矩因子与摩擦;1=使用下方自定义值(LateralTorqueAccelFactor/Friction);2=使用openpilot默认值。一般保持0(自学习)。", 0, 2, 1));
  latLongToggles->addItem(new CValueControl("LateralTorqueAccelFactor", "横向: 扭矩加速度因子(2500)", "横向扭矩加速度因子(x0.001)。扭矩=横向加速度×此因子。调大=转向更有力、响应更快(急弯更稳);调小=更柔(过柔会居中无力画龙)。默认2500(=2.5)。", 1000, 6000, 10));
  latLongToggles->addItem(new CValueControl("LateralTorqueFriction", "横向: 扭矩摩擦补偿(100)", "横向扭矩摩擦补偿(x0.001)。抵消轮胎/转向系统摩擦。调大=补偿越强(转向更跟手不打滑);过小=转向发飘需更大输入。默认100(=0.1)。", 0, 1000, 10));
  latLongToggles->addItem(new CValueControl("CustomSteerMax", "横向: 自定义最大转向力(0)", "自定义最大转向力(扭矩上限)。0=使用车型默认;>0=自定义上限(单位原厂扭矩)。调大=允许更大转向力(急弯更稳但可能突兀);调小=限制更死(过弯乏力)。", 0, 30000, 5));
  latLongToggles->addItem(new CValueControl("CustomSteerDeltaUp", "横向: 转向增量上升(0)", "转向指令上升速率限制(增量上限)。调大=转向变化更突兀迅速;调小=更平滑。0=使用默认。", 0, 50, 1));
  latLongToggles->addItem(new CValueControl("CustomSteerDeltaDown", "横向: 转向增量下降(0)", "转向指令下降速率限制(增量下限)。调大=收方向更突兀;调小=更平滑。0=使用默认。", 0, 50, 1));
  latLongToggles->addItem(new CValueControl("LongTuningKpV", "纵向: P增益(100)", "纵向PID比例增益(x0.01)。对速度误差的即时响应。调大=加减速更猛更跟脚;调小=更柔更慢。默认100(=1.0)。", 0, 150, 5));
  latLongToggles->addItem(new CValueControl("LongTuningKiV", "纵向: I增益(0)", "纵向PID积分增益(x0.001)。消除稳态速度误差。调大=更快贴合目标速度但可能抖动;调小=更平顺但响应慢。默认0(用车型值)。", 0, 2000, 5));
  latLongToggles->addItem(new CValueControl("LongTuningKf", "纵向: FF增益(100)", "纵向PID前馈增益(x0.01)。直接按目标加速度前馈。调大=加速更跟脚(少等待反馈);调小=更依赖反馈(略钝)。默认100(=1.0)。", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("LongActuatorDelay", "纵向: 执行器延迟(20)", "纵向执行器(油门/刹车)延迟补偿(x0.01秒)。调大=更早开始加减速以补偿执行迟滞;0=使用车型默认延迟。默认20(=0.20s)。", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("VEgoStopping", "纵向: 车辆停止速度(50)", "判定车辆停止的速度阈值(x0.1 m/s)。调大=更早判定为停车(更提前松油门/准备刹停);调小=更晚。默认50(=0.5m/s)。", 1, 100, 5));
  latLongToggles->addItem(new CValueControl("RadarReactionFactor", "纵向: 雷达反应因子(100)", "对雷达前车目标的反应灵敏度(x0.01)。调大=对前车/雷达目标反应更灵敏(更早减速跟车);调小=更迟钝(更晚反应,可能更激进且减少幽灵刹)。注意:过小有漏刹风险,过大易误刹,需按车型微调。默认100(=1.0)。", 0, 200, 10));
  latLongToggles->addItem(new CValueControl("StoppingAccel", "纵向: 停车时的减速度x0.01(-40)", "停车时的减速度(x0.01 m/s²)。调大(更接近0)=停车过程更缓;调小(更负,如-40)=停车刹得更狠。0=使用车型默认值。", -200, 0, 1));
  latLongToggles->addItem(new CValueControl("StoppingDecelRate", "纵向: 停车时的减速率x0.01(80)", "停车时的减速度变化率(x0.01 m/s²)。调大=减速更平缓到来(末段柔);调小=更突然。0=使用车型默认值。默认80。", 0, 200, 1));
  latLongToggles->addItem(new CValueControl("ComfortBrake", "纵向: 舒适制动减速度x0.01(240)", "舒适制动目标减速度(x0.01 m/s²)。调大=舒适制动更强(刹得更果断但仍舒适);调小=更柔。默认240(=2.4)。", 0, 400, 5));
  latLongToggles->addItem(new CValueControl("StopDistanceCarrot", "纵向: 停车距离 (600)cm", "停稳后距前车的距离(厘米)。调大=停得更远(更安全但占空间);调小=贴更近。默认600(=6米)。", 300, 1000, 10));
  latLongToggles->addItem(new CValueControl("RedLightDistOffset", "纵向: 红灯停车偏移(0)dm", "红灯停止线距离偏移(分米)。调负=停在停止线前(防冲过停止线);调正=停在更远处。默认0(停在线处)。", -150, 150, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitVEgoMax", "减速: 限制减速度的最大车速(20)x0.1", "减速限制功能生效的最高车速(x0.1 m/s)。设0=完全关闭减速限制。调大=在更高速度仍限制最大减速度。", 0, 500, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitAEgoMax", "减速: 限制减速度的最大值(-100)x0.01", "减速限制允许的最大减速度(负值,x0.01 m/s²)。调大(更接近0)=限制更松(允许更急刹);调小(更负)=限制更严(整体更柔不能急刹)。默认-100。", -350, 0, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitVEgoMin", "减速: 限制减速度的最小车速(10)x0.1", "减速限制生效的最低车速(x0.1 m/s)。低于此速度不再限制减速度(保证能停住)。", 0, 500, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitAEgoMin", "减速: 限制减速度的最小值(-25)x0.01", "低速段允许的最大减速度(负值,x0.01 m/s²)。与AEgoMax配合限定低速刹车强度。", -350, 0, 1));
  latLongToggles->addItem(new CValueControl("SmoothStopMode", "纵向: 平滑停车模式(0)", "平滑停车算法模式。0=关闭(原厂停车);1/2=不同平滑策略,数值越大停车越平顺(防点头)。默认0(原厂)。", 0, 5, 1));
  latLongToggles->addItem(new CValueControl("StartAccel", "纵向:起步加速度x0.01(80)", "起步加速度(x0.01 m/s²)。调大=起步更猛;调小=起步更柔(蠕行更顺)。0=使用车型默认值。默认80(=0.8)。", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("JLeadFactor3", "纵向: 加加速度前车因子(0)", "对前车加加速度(jerk)的响应因子(x0.01)。调大=更关注前车急动并提前反应;设为0=关闭此因子。", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("GasSmoothTime", "纵向: 释放油门踏板后平滑加速度的时间(50)x0.1s", "松开油门后最大加速度的平滑时间(x0.1秒)。调大=松油门后加速更平缓过渡(不突兀);调小=更迅速回到目标。默认50(=5s)。", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals0", "加速:0km/h(160)", "0km/h时目标加速度上限(x0.01 m/s²)。各速度点加速能力上限。调大=该速度区间加速更猛;调小=更柔。默认160(=1.6)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals1", "加速:10km/h(160)", "10km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认160(=1.6)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals2", "加速:40km/h(120)", "40km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认120(=1.2)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals3", "加速:60km/h(100)", "60km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认100(=1.0)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals4", "加速:80km/h(80)", "80km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认80(=0.8)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals5", "加速:110km/h(70)", "110km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认70(=0.7)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals6", "加速:140km/h(60)", "140km/h时目标加速度上限(x0.01 m/s²)。调大=该速度区间加速更猛;调小=更柔。默认60(=0.6)。", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("MaxAngleFrames", "最大转角帧数(89)", "最大转向角对应的帧数。默认89;若仪表盘报转向相关错误,调到85~87可规避。一般保持默认。", 80, 100, 1));

  //latLongToggles->addItem(new CValueControl("AutoLaneChangeSpeed", "LaneChangeSpeed(20)", "", 1, 100, 5));
  //latLongToggles->addItem(new CValueControl("JerkStartLimit", "LONG: JERK START(10)x0.1", "Starting Jerk.", 1, 50, 1));
  //latLongToggles->addItem(new CValueControl("LongitudinalTuningApi", "LONG: ControlType", "0:velocity pid, 1:accel pid, 2:accel pid(comma)", 0, 2, 1));
  //latLongToggles->addItem(new CValueControl("StartAccelApply", "LONG: StartingAccel 2.0x(0)%", "정지->출발시 가속도의 가속율을 지정합니다 0: 사용안함.", 0, 100, 10));
  //latLongToggles->addItem(new CValueControl("StopAccelApply", "LONG: StoppingAccel -2.0x(0)%", "정지유지시 브레이크압을 조정합니다. 0: 사용안함. ", 0, 100, 10));
  //latLongToggles->addItem(new CValueControl("TraffStopDistanceAdjust", "LONG: TrafficStopDistance adjust(150)cm", "", -1000, 1000, 10));
  //latLongToggles->addItem(new CValueControl("CruiseMinVals", "DECEL:(120)", "Sets the deceleration rate.(x0.01m/s^2)", 50, 250, 5));

  dispToggles = new ListWidget(this);
  dispToggles->addItem(new CValueControl("ShowDebugLog", "调试日志", "调试日志位掩码: 1=导航信息, 2=变道请求, 4=变道状态机, 8=变道状态信息。需要多项同时记录就把对应数字相加(如想要导航+变道请求填3)。0=不记录。", 0, 255, 1));
  dispToggles->addItem(new CValueControl("ShowDebugUI", "调试信息", "屏上调试信息显示层级。0=关闭;1=显示基础调试;2=显示更详细调试信息。仅调试用,日常驾驶建议0。", 0, 2, 1));
  dispToggles->addItem(new CValueControl("ShowTpms", "胎压信息", "胎压信息显示。0=不显示;1=显示胎压。", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowDateTime", "时间信息", "时间/日期显示。0=不显示;1=显示时间+日期;2=仅时间;3=仅日期。", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowDeviceState", "设备状态", "设备状态显示。0=不显示;1=显示(如温度/网络等状态)。", 0, 1, 1));
  dispToggles->addItem(new CValueControl("ShowRadarInfo", "雷达信息", "雷达目标信息显示。0=不显示;1=显示;2=显示相对位置;3=显示静止车辆。用于排查前车/幽灵目标。", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowRouteInfo", "路线信息", "导航路线信息显示。0=不显示;1=显示。", 0, 1, 1));
  dispToggles->addItem(new CValueControl("ShowPlotMode", "调试图表", "屏上调试图表模式(0-10)。不同数值显示不同的实时曲线图(如加速度/转向)。0=关闭。", 0, 10, 1));
  dispToggles->addItem(new CValueControl("ShowCustomBrightness", "亮度比例", "界面亮度比例(0-100%)。调大=屏幕更亮;调小=更暗。仅影响UI亮度,不影响行车画面。", 0, 100, 10));

  //dispToggles->addItem(new CValueControl("ShowHudMode", "Display Mode", "0:Frog,1:APilot,2:Bottom,3:Top,4:Left,5:Left-Bottom", 0, 5, 1));
  //dispToggles->addItem(new CValueControl("ShowSteerRotate", "Handle rotate", "0:None,1:Rotate", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowAccelRpm", "Accel meter", "0:None,1:Display,1:Accel+RPM", 0, 2, 1));
  //dispToggles->addItem(new CValueControl("ShowTpms", "TPMS", "0:None,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowSteerMode", "Handle Display Mode", "0:Black,1:Color,2:None", 0, 2, 1));
  //dispToggles->addItem(new CValueControl("ShowConnInfo", "APM connection", "0:NOne,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowBlindSpot", "BSD Info", "0:None,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowGapInfo", "GAP Info", "0:None,1:Display", -1, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowDmInfo", "DM Info", "0:None,1:Display,-1:Disable(Reboot)", -1, 1, 1));

  // === 轨迹标签: 由原「显示」标签迁入, 占据原「功能」标签位置 ===
  trackToggles = new ListWidget(this);
  trackToggles->addItem(new CValueControl("ShowPathEnd", "轨迹终点", "轨迹终点标记。0=不显示;1=在规划路径末端显示标记点。", 0, 1, 1));
  trackToggles->addItem(new CValueControl("ShowLaneInfo", "车道信息", "车道信息叠加。-1=无;0=仅轨迹;1=轨迹+车道线;2=轨迹+车道线+路沿。数值越大显示越完整。", -1, 2, 1));
  trackToggles->addItem(new CValueControl("ShowPathColorCruiseOff", "轨迹颜色：未开启巡航", "未开启巡航时的轨迹颜色(0-19, +10为描边)。0红 1橙 2黄 3绿 4蓝 5靛 6紫 7棕 8白 9黑。+10后轨迹带描边。", 0, 19, 1));
  trackToggles->addItem(new CValueControl("ShowPathMode", "轨迹模式：无车道线", "无车道线时的轨迹形状模式(0-15)。0=普通(推荐);1/2=矩形;3/4=^^;5/6=推荐;7/8=^^;9-12=平滑^^。数字越大形状越平滑。", 0, 15, 1));
  trackToggles->addItem(new CValueControl("ShowPathColor", "轨迹颜色：无车道线", "无车道线时的轨迹颜色(0-19, +10为描边)。见ShowPathColorCruiseOff颜色表。", 0, 19, 1));
  trackToggles->addItem(new CValueControl("ShowPathModeLane", "轨迹模式：有车道线", "有车道线时的轨迹形状模式(0-15)。同ShowPathMode。", 0, 15, 1));
  trackToggles->addItem(new CValueControl("ShowPathColorLane", "轨迹颜色：有车道线", "有车道线时的轨迹颜色(0-19, +10为描边)。见ShowPathColorCruiseOff颜色表。", 0, 19, 1));
  trackToggles->addItem(new CValueControl("ShowPathWidth", "轨迹宽度比例(100%)", "轨迹线宽度比例(10-200%)。调大=路径线越宽越明显;调小=越细。", 10, 200, 10));

  startToggles = new ListWidget(this);
  QString selected = QString::fromStdString(Params().get("CarSelected3"));
  QPushButton* selectCarBtn = new QPushButton(selected.length() > 1 ? selected : tr("选择您的车辆"));
  selectCarBtn->setObjectName("selectCarBtn");
  selectCarBtn->setStyleSheet(R"(
    QPushButton {
      margin-top: 20px; margin-bottom: 20px; padding: 10px; height: 120px; border-radius: 15px;
      color: #FFFFFF; background-color: #2C2CE2;
    }
    QPushButton:pressed {
      background-color: #2424FF;
    }
  )");
  //selectCarBtn->setFixedSize(350, 100);
  connect(selectCarBtn, &QPushButton::clicked, [=]() {
    QString selected = QString::fromStdString(Params().get("CarSelected3"));


    QStringList all_items = get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars").toStdString().c_str());
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_gm").toStdString().c_str()));
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_toyota").toStdString().c_str()));
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_mazda").toStdString().c_str()));
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_tesla").toStdString().c_str()));
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_honda").toStdString().c_str()));
    all_items.append(get_list((QString::fromStdString(Params().getParamPath()) + "/SupportedCars_volkswagen").toStdString().c_str()));
    QMap<QString, QStringList> car_groups;
    for (const QString& car : all_items) {
      QStringList parts = car.split(" ", QString::SkipEmptyParts);
      if (!parts.isEmpty()) {
        QString manufacturer = parts.first();
        car_groups[manufacturer].append(car);
      }
    }

        QStringList manufacturers = car_groups.keys();
    QString selectedManufacturer = MultiOptionDialog::getSelection("选择厂商", manufacturers, manufacturers.isEmpty() ? "" : manufacturers.first(), this);

    if (!selectedManufacturer.isEmpty()) {
      QStringList cars = car_groups[selectedManufacturer];
      QString selectedCar = MultiOptionDialog::getSelection("选择您的车辆", cars, selected, this);

      if (!selectedCar.isEmpty()) {
        if (selectedCar == "[ 未选择 ]") {
          Params().remove("CarSelected3");
        } else {
          printf("已选择车辆: %s\n", selectedCar.toStdString().c_str());
          Params().put("CarSelected3", selectedCar.toStdString());
          QTimer::singleShot(1000, []() {
            Params().putInt("SoftRestartTriggered", 1);
          });
          ConfirmationDialog::alert(selectedCar, this);
        }
        selected = QString::fromStdString(Params().get("CarSelected3"));
        selectCarBtn->setText((selected.isEmpty() || selected == "[ 未选择 ]") ? tr("选择您的车辆") : selected);
      }
    }
  });


  startToggles->addItem(selectCarBtn);
  startToggles->addItem(new CValueControl("modelid", "模型选择(-1)", "模型选择。-1=默认模型;0=TR16,1=DTR,2=Firehose,3=GWM,4=PP,5=DS,6=DSv2,7=WMI,8=CD210。修改后重启设备生效。一般保持-1用默认模型。", -1, 100, 1));
  startToggles->addItem(new CValueControl("HyundaiCameraSCC", "现代: 摄像头SCC(0)", "现代车型SCC(自适应巡航)信号来源。1=把SCC的CAN线连到摄像头;2=同步定速状态;3=原厂长控(非摄像头实现SCC)。非摄像头SCC实现的车型(如多数新款)设为0。改后需重启。", -1, 100, 1));
  startToggles->addItem(new CValueControl("CanfdHDA2", "CANFD: HDA2 模式", "CANFD车型HDA2模式。1=HDA2;2=HDA2+盲点监测。非CanFD车型(老款)设为0。错误设置会导致无法启动,按车型资料填。", 0, 2, 1));
  startToggles->addItem(new CValueControl("EnableRadarTracks", "启用雷达追踪(1)", "启用雷达目标追踪。1=启用(用雷达追踪前车);-1或2=禁用(始终用HKG SCC雷达)。胜达等车型设为1。修改后需重启车辆。", -1, 3, 1));
  startToggles->addItem(new CValueControl("EnableEscc", "启用ESCC(1)", "启用ESCC(增强纵向控制)。1=启用;0=禁用。修改后需重启车辆。一般保持1。", 0, 1, 1));
  startToggles->addItem(new CValueControl("AutoCruiseControl", "自动巡航控制(0)", "自动巡航总开关。0=关闭(需手动按按键开启巡航);>0=开启(挂D档自动进入巡航);>1使用softmode1,否则softmode2(不同柔和策略)。", 0, 3, 1));
  startToggles->addItem(new CValueControl("CruiseOnDist", "定速: 自动开启距离(0cm)", "前车靠近时自动开启巡航的距离阈值(厘米)。当前车进入此距离且油门/刹车未踩下时自动开启巡航。调大=更远就自动开;设为0=关闭自动开启。", 0, 2500, 50));
  startToggles->addItem(new CValueControl("AutoEngage", "车辆启动时自动开启的功能", "车辆启动时自动激活的功能。0=都不自动开;1=自动启用车道保持(横向);2=车道保持+定速(横向+纵向)同时启用。", 0, 2, 1));
  startToggles->addItem(new CValueControl("AutoGasTokSpeed", "轻踩油门开启巡航的速度", "轻踩油门开启巡航的速度阈值(km/h)。当车速大于此值时,轻点油门可使巡航速度+10或自动开启巡航(前提是自动巡航控制已打开)。0=关闭此功能。", 0, 200, 5));
  startToggles->addItem(new CValueControl("SpeedFromPCM", "从PCM读取定速速度(2)", "从哪个总线读取设定巡航速度。丰田必须设为1,本田设为3,其他车型默认2。设错会导致设定速度显示/控制异常。", 0, 3, 1));
  startToggles->addItem(new CValueControl("SoundVolumeAdjust", "提示音音量(100%)", "提示音音量(5-200%, 默认100)。调大=提示音更大;调小=更小。", 5, 200, 5));
  startToggles->addItem(new CValueControl("SoundVolumeAdjustEngage", "接管提示音音量(100%)", "接管/脱离等提示音音量(5-200%, 默认100)。调大=更大;调小=更小。", 5, 200, 5));
  startToggles->addItem(new CValueControl("MaxTimeOffroadMin", "熄屏时间 (分钟)", "停车熄屏时间(分钟)。到时未行驶则屏幕熄灭省电。调大=更久才熄屏。", 1, 600, 10));
  startToggles->addItem(new CValueControl("EnableConnect", "启用远程连接", "启用远程连接(如SSH/隧道)。0=关;1/2=开不同方式。注意:开启远程连接可能导致设备被Comma官方封禁,谨慎使用。", 0, 2, 1));
  startToggles->addItem(new CValueControl("MapboxStyle", "地图样式(0)", "地图底图样式(0-2)。不同编号切换地图配色/样式。按喜好选。", 0, 2, 1));
  startToggles->addItem(new CValueControl("RecordRoadCam", "记录前置摄像头(0)", "行车记录摄像头。0=不记录;1=记录前置主摄;2=记录前置主摄+广角前置。开2更全但更占存储。", 0, 2, 1));
  startToggles->addItem(new CValueControl("HDPuse", "使用HDP(CCNC)(0)", "使用HDP(CCNC)相关功能。0=关;1=在使用APN时启用;2=始终启用。按车型网络环境选。", 0, 2, 1));
  startToggles->addItem(new CValueControl("NNFF", "NNFF", "启用神经网络前馈(NNFF, Twilsonco方案)。0=关;1=开。需重启生效。可改善横向扭矩控制精度,部分车型更跟手。", 0, 1, 1));
  startToggles->addItem(new CValueControl("NNFFLite", "NNFF精简版", "NNFF精简版。0=关;1=开。需重启生效。比完整NNFF更轻量。", 0, 1, 1));
  startToggles->addItem(new CValueControl("AutoGasSyncSpeed", "松油门保持巡航速度", "松油门保持巡航速度。0=关闭;1=开启。开启后若踩油门且当前车速高于巡航速度,巡航设定速度自动同步为当前车速(松开后保持)。", 0, 1, 1));
  startToggles->addItem(new CValueControl("DisableMinSteerSpeed", "禁用最小转向速度限制", "禁用最小转向速度限制。0=保持原厂(低于某速度不转向);1=禁用限制(允许极低速也进行转向控制)。仅在特殊场景需要。", 0, 1, 1));
  startToggles->addItem(new CValueControl("DisableDM", "禁用疲劳监测(DM)", "疲劳监测(DM)开关。0=启用疲劳监测(驾驶员分心/闭眼会强制减速并报警);1=禁用疲劳监测(不再因分心强制减速)。注意:禁用会降低安全冗余。", 0, 1, 1));
  startToggles->addItem(new CValueControl("HotspotOnBoot", "开机启用热点", "开机自动启用设备热点。0=关;1=开。开后可手机直连设备WiFi。", 0, 1, 1));
  startToggles->addItem(new CValueControl("SoftwareMenu", "启用软件菜单", "启用软件菜单(开发者/调试入口)。0=关;1=开。", 0, 1, 1));
  startToggles->addItem(new CValueControl("IsLdwsCar", "是否LDWS车型", "是否为LDWS(车道偏离预警)车型。0=否;1=是。按车型实际配置填,影响横向控制初始化。", 0, 1, 1));

  //startToggles->addItem(new CValueControl("CarrotCountDownSpeed", "导航倒计时速度(10)", "", 0, 200, 5));
  //startToggles->addItem(new ParamControl("NoLogging", "禁用日志记录", "", this));
  //startToggles->addItem(new ParamControl("LaneChangeNeedTorque", "变道: 需要方向盘施力", "", this));
  //startToggles->addItem(new CValueControl("LaneChangeLaneCheck", "变道: 检查车道存在", "(0:否,1:车道,2:+路肩)", 0, 2, 1));

  speedToggles = new ListWidget(this);
  speedToggles->addItem(new CValueControl("AutoCurveSpeedLowerLimit", "弯道: 转弯最低降速限制(30)", "转弯自动降速的最低速度限制(默认30)。限制视觉/地图转弯降速不得小于此速度。调大=转弯降速更明显(最低也降到此速);设为0=不限制(可降到更低)。", 0, 200, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedFactor", "弯道: 降速弯道曲率系数(100%)", "弯道降速-曲率系数(%, 默认100)。模型预测横摆角速度×此系数决定降速量。调大=降速越多(过弯越慢越稳);调小=降速越少(过弯更快)。", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedAggressiveness", "弯道: 降速横向加速度系数(100%)", "弯道降速-横向加速度系数(%, 默认100)。目标横向加速度×此系数。调大=允许更大横向加速度(降速越少越快);调小=降速越多(更保守)。", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedFactorH", "高速: 降速弯道曲率系数(100%)", "高速段弯道降速-曲率系数(%, 默认90)。同AutoCurveSpeedFactor,仅作用于高速路段。", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedAggressivenessH", "高速: 降速横向加速度系数(100%)", "高速段弯道降速-横向加速度系数(%, 默认110)。同AutoCurveSpeedAggressiveness,仅作用于高速。", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoRoadSpeedLimitOffset", "道路限速偏移(-1)", "道路限速偏移(默认0, 单位km/h)。巡航限速=道路限速+此偏移;当offset<0时改为道路限速×测速点安全系数(更保守)。调大(正值)=比道路限速更高(开得更快);设为0=严格等于道路限速;调为负值=低于道路限速(更保守)。", -1, 100, 1));
  speedToggles->addItem(new CValueControl("AutoRoadSpeedAdjust", "自动调整道路限速(50%)", "自动调整道路限速的比例(×0.01, 默认-1)。-1:道路限速变化时立即采用新限速(跟随);0:不自动调整;>0:按此比例在新旧限速间平滑过渡(调大=更快贴近新限速)。", -1, 100, 5));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedCtrlEnd", "测速点减速结束点(6秒)", "测速点减速完成点(秒, 默认7)。设定减速应在距测速点多远完成。调大=更早完成减速(更早回到正常速度);调小=更晚。", 3, 20, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedCtrlMode", "导航限速控制模式(3)", "导航限速控制模式(默认3)。0=关闭;1=仅测速摄像头;2=+减速带;3=+移动测速。调大=控制范围越广(更多场景自动减速)。", 0, 3, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedDecelRate", "测速点减速率x0.01m/s²(80)", "测速点减速率(x0.01 m/s², 默认80)。调小=更早开始减速(减速更平缓);调大=更晚更急地减速。", 10, 200, 10));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedSafetyFactor", "测速点安全系数(105%)", "测速点安全系数(%, 默认100)。摄像头限速值×此比例=实际采用限速。调大=限速更高(通过测速点更快);调小=更保守(留余量)。", 80, 120, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedBumpTime", "减速带时间距离(1秒)", "减速带提前量(秒, 默认1)。识别减速带后提前多久开始减速。调大=更早减速。", 1, 50, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedBumpSpeed", "减速带通过速度(35Km/h)", "过减速带速度(km/h, 默认35)。调大=更快通过(更颠簸);调小=更慢更舒适。", 10, 100, 5));
  speedToggles->addItem(new CValueControl("AutoNaviCountDownMode", "导航倒计时模式(0)", "导航倒计时模式(默认0)。0=关闭;1=转向+摄像头倒计时;2=转向+摄像头+减速带倒计时。开启后在屏上显示距下个事件的距离/时间。", 0, 2, 1));
  speedToggles->addItem(new CValueControl("TurnSpeedControlMode", "转弯速度控制模式(1)", "转弯速度控制模式(默认2)。0=关闭;1=基于视觉;2=视觉+路线;3=仅路线。调大=使用更多地图路线数据(弯道识别更准)。", 0, 3, 1));
  speedToggles->addItem(new CValueControl("MapTurnSpeedFactor", "地图转弯速度系数(100%)", "地图转弯速度系数(%, 默认100)。使用地图转弯速度时,实际转弯速度=地图速度×%。仅在转弯速度控制模式为2或3时生效。调大=转弯更快;调小=更慢。", 50, 300, 5));
  speedToggles->addItem(new CValueControl("AutoTurnControl", "ATC: 自动转弯控制(0)", "ATC自动转弯控制(默认2)。0=无;1=变道;2=变道+减速;3=减速。开启后接近弯道自动减速/变道。调大=动作越多。", 0, 3, 1));
  speedToggles->addItem(new CValueControl("AutoTurnControlSpeedTurn", "ATC: 转弯速度(20)", "ATC转弯速度(默认20)。自动转弯时的目标速度。调大=转弯更快;调小=更慢更稳。", 0, 100, 1));
  speedToggles->addItem(new CValueControl("AutoTurnControlTurnEnd", "ATC: 转弯结束时间(6)", "ATC转弯结束时间(默认6, 距离=速度×时间)。识别到弯道结束后再过多久恢复速度。调大=更晚恢复(更稳)。", 0, 30, 1));
  speedToggles->addItem(new CValueControl("AutoTurnMapChange", "ATC 自动地图切换(0)", "ATC自动地图切换(默认0)。0=关;1=开。开启后根据地图自动切换不同限速策略。", 0, 1, 1));

  //new
  navToggles = new ListWidget(this);
  navToggles->addItem(new CValueControl("RoadType", "手动设置道路类型(-1)", "手动设置道路类型(默认-2)。-2=从导航获取(>=85快速路,>=100高速,具体由导航决定);-1=自动(<85公路,>=85快速,>=100高速);0=高速(无应急车道);1=高速(有应急车道);>=2=其它道路。错误设置影响限速/变道策略。", -10, 100, 1));
  navToggles->addItem(new CValueControl("SameSpiCamFilter", "过滤相同测速数据(1)", "过滤相同测速数据。0=关闭;1=打开。开启后合并重复测速摄像头数据,避免频繁重复减速。建议保持1。", 0, 1, 1));
  navToggles->addItem(new CValueControl("StockBlinkerCtrl", "外接控制原车转向拔杆(0)", "外接控制原车转向拨杆。0=关闭;1=打开。开启后用外接硬件控制原车转向灯/拨杆信号。", 0, 1, 1));
  navToggles->addItem(new CValueControl("ExtBlinkerCtrlTest", "外接控制器自检(1)", "外接控制器自检。0=关闭;1=打开。开启后外接控制器上电自检。", 0, 1, 1));
  navToggles->addItem(new CValueControl("BlinkerMode", "手动打灯控制模式(0)", "手动打灯控制模式(默认1)。0=自动(系统自动打灯变道);1=仅变道时打灯(其余场景不自动)。", 0, 1, 1));
  navToggles->addItem(new CValueControl("LaneStabTime", "车道数稳定时间(10x0.1s)", "车道数稳定时间(默认30, ×0.1/DT_MDL,约3秒)。检测到车道数变化后需稳定超过此时长才认定,防抖动误判变道。调大=更稳定才变道(更保守)。", 5, 100, 1));
  navToggles->addItem(new CValueControl("BsdDelayTime", "后盲区有车延时(20x0.1s)", "后盲区有车延时(默认10, ×0.1≈1秒)。后车从盲区消失后,经过此时长才允许变道。调大=更保守(确认安全更久)。", 0, 100, 1));
  navToggles->addItem(new CValueControl("SideBsdDelayTime", "侧前方有车延时(20x0.1s)", "侧前方有车延时(默认10, ×0.1≈1秒)。侧前方车消失后延时,防刚过车就变道。调大=更保守。", 0, 100, 1));
  navToggles->addItem(new CValueControl("SideRelDistTime", "侧前方有车变道相对距离", "侧前方有车变道相对距离阈值(×0.1秒)。与侧前车相对距离小于本车速度×此时长时不允许变道。调大=要求更大安全距离才变道(更保守)。", 0, 50, 1));
  navToggles->addItem(new CValueControl("SidevRelDistTime", "侧前方有车变道等效距离", "侧前方有车变道等效距离阈值(×0.1秒)。侧前车速度×3+相对距离小于本车速度×(时间+3)时不允许变道。调大=更保守。", 0, 50, 1));
  navToggles->addItem(new CValueControl("SideRadarMinDist", "侧面最小雷达距离(0m)", "侧面最小雷达距离(×0.1米, 默认0)。忽略左右侧车道中探测距离小于此值的车辆(视为噪声)。调大=忽略更近的侧车(更敢变道);调小=更谨慎。", -50, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDistOffsetH", "H 提前靠边行驶距离(1000m)", "高速(H)提前靠边行驶距离(米, 默认1500)。距匝道口此距离时开始变到最侧车道。设为0=不提前变道。", 0, 5000, 5));
  navToggles->addItem(new CValueControl("AutoEnTurnNewLaneTimeH", "H 继续变道新车道时间(0s)", "高速(H)继续变道新车道时间(秒, 默认3=关)。已在最侧道,若新车道出现超过此时长允许再次变道(推荐20)。0=关闭。", 0, 120, 1));
  navToggles->addItem(new CValueControl("AutoDoForkDecalDistH", "H 进匝道减速距离偏移(50m)", "高速(H)进匝道减速距离偏移(米, 默认100)。开始减速距离=软件计算距离+此偏移。调大=更早减速。", 0, 500, 5));
  navToggles->addItem(new CValueControl("AutoDoForkBlinkerDistH", "H 进匝道打灯距离偏移(30m)", "高速(H)进匝道打灯距离偏移(米, 默认30)。提前打灯距离=软件计算+此偏移。", 0, 200, 2));
  navToggles->addItem(new CValueControl("AutoDoForkNavDistH", "H 进匝道转向距离(50m)", "高速(H)进匝道转向距离(米, 默认80)。距导航匝道口小于此距离开始变道进入。0=不生效。", 0, 200, 1));
  //navToggles->addItem(new CValueControl("AutoDoForkCheckDistH", "H 高速提前识别出现匝道口的距离(20m)", "在靠近匝道口时提前识别匝道口出现的距离，是在模型预留的轨迹上提前检测的距离", 0, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDecalRateH", "H 进匝道前降速比率(80%)", "高速(H)进匝道前降速比率(%, 默认75)。进匝道时车速降至道路限速的比率。0=关闭此功能(不主动降速)。", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoForkSpeedMinH", "H 进匝道最低速度(60)", "高速(H)进匝道最低速度(默认60)。允许降速到此最低值,低于此不再降。调大=匝道内保持更快。", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoKeepForkSpeedH", "H 进匝道保持速度时间(0s)", "高速(H)进匝道后保持速度时间(秒, 默认5=关)。进匝道后保持当前速度行驶此时长。", 0, 60, 1));

  navToggles->addItem(new CValueControl("AutoForkDistOffset", "L 提前靠边行驶距离(30m)", "公路(L)提前靠边行驶距离(米, 默认30)。同AutoForkDistOffsetH但用于普通公路分叉。0=不提前。", 0, 2000, 5));
  navToggles->addItem(new CValueControl("AutoEnTurnNewLaneTime", "L 继续变道新车道时间(0s)", "公路(L)继续变道新车道时间(秒, 默认1=关)。同AutoEnTurnNewLaneTimeH的公路版(推荐5)。", 0, 120, 1));
  navToggles->addItem(new CValueControl("AutoDoForkDecalDist", "L 进匝道减速距离偏移(20m)", "公路(L)进分叉减速距离偏移(米, 默认20)。", 0, 500, 5));
  navToggles->addItem(new CValueControl("AutoDoForkBlinkerDist", "L 进匝道打灯距离偏移(15m)", "公路(L)进分叉打灯距离偏移(米, 默认15)。", 0, 200, 1));
  navToggles->addItem(new CValueControl("AutoDoForkNavDist", "L 进匝道转向距离(20m)", "公路(L)进分叉转向距离(米, 默认15)。距导航分叉口小于此距离开始变道。0=不生效。", 0, 200, 1));
  //navToggles->addItem(new CValueControl("AutoDoForkCheckDist", "L 公路提前识别出现分叉口的距离(10m)", "在靠近公路分叉口时提前识别分叉口出现的距离，是在模型预留的轨迹上提前检测的距离", 0, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDecalRate", "L 进匝道前降速比率(80%)", "公路(L)进分叉前降速比率(%, 默认80)。同AutoForkDecalRateH的公路版。0=关闭。", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoForkSpeedMin", "L 进匝道最低速度(45)", "公路(L)进分叉最低速度(默认45)。", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoKeepForkSpeed", "L 进匝道保持速度时间(0s)", "公路(L)进分叉后保持速度时间(秒, 默认5=关)。", 0, 60, 1));

  navToggles->addItem(new CValueControl("NewLaneWidthDiff", "ATC 新车道出现标准(0.8m)", "ATC新车道出现判定标准(默认8, ×0.1≈0.8米)。侧车道1秒内宽度增加超过此值才认作新车道出现。调大=需更大变化才认定(更稳)。", 2, 10, 1));
  navToggles->addItem(new CValueControl("AutoTurnDistOffset", "ATC 自动转弯距离偏移(0m)", "ATC自动转弯距离偏移(米, 默认0)。提前自动转弯的距离,一般0,仅针对特定转弯类型微调。", -100, 200, 1));
  navToggles->addItem(new CValueControl("AutoTurnInNotRoadEdge", "ATC 非侧边车道允许变道(0)", "ATC非侧边车道允许变道(默认1)。0=不允许在非最侧边车道自动变道;1=允许。", 0, 1, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChange", "ATC 允许连续变道(0)", "ATC允许连续变道(默认1)。0=关闭(一次变一条);1=允许连续变多条车道。", 0, 1, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChangeCnt", "ATC 允许连续变道次数(x+1)", "ATC允许连续变道次数(默认4, 实际=x+1=5次)。调大=可连续变更多条。", 0, 4, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChangeInterval", "ATC 连续变道间隔(2秒)", "ATC连续变道间隔(秒, 默认2)。两次变道之间的最小间隔。调大=更谨慎(间隔更长)。", 0, 30, 1));
  navToggles->addItem(new CValueControl("AutoTurnLeft", "ATC 允许自动左变道(0)", "ATC允许自动左变道(默认1)。0=需驾驶员打左转向灯才左变;1=允许系统自动左变道。", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpRoadLimit", "提高60以下的公路限速(0)", "提高60以下公路限速(默认0)。0=关闭;1=当普通公路限速<60时,把道路限速+下方提速偏移值。", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpRoadLimit40KMH", "40以下公路提速(15km/h)", "40以下公路提速值(默认0km/h)。配合上方开关,把低限速公路的限速提高此值。", 0, 50, 1));
  navToggles->addItem(new CValueControl("AutoUpHighwayRoadLimit", "提高60以下的高速限速(0)", "提高60以下高速限速(默认0)。0=关闭;1=当高速限速<60时启用提速。", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpHighwayRoadLimit40KMH", "40以下高速提速(20km/h)", "40以下高速提速值(默认0km/h)。配合上方开关使用。", 0, 50, 1));

  toggles_layout->addWidget(cruiseToggles);
  toggles_layout->addWidget(latLongToggles);
  toggles_layout->addWidget(dispToggles);
  toggles_layout->addWidget(trackToggles);
  toggles_layout->addWidget(startToggles);
  toggles_layout->addWidget(speedToggles);
  toggles_layout->addWidget(navToggles);
  } else {
  featToggles = new ListWidget(this);
  featToggles->addItem(sectionHeader("横向与转向辅助"));

  featToggles->addItem(new CValueControl("AutoLaneCorrection", "自动居中纠偏(2)", "0:关闭, 1:实时纠偏, 2:实时纠偏+学习固定偏差(自动保持车道居中)", 0, 2, 1));
  featToggles->addItem(new CValueControl("AutoLaneCorrectionGain", "纠偏强度(40)", "纠偏增益, 越大回中越快; 画龙时调小, 回中慢时调大", 0, 100, 1));
  // === 弯道居中（独立开关，默认关=零影响原逻辑）===
  featToggles->addItem(new CValueControl("CurveCenteringMode", "弯道居中(0)", "弯道中强制沿模型车道线中心行驶, 不外扩不内切, 避免压线; 0:关闭(完全不影响原逻辑), 1:开启", 0, 1, 1));
  featToggles->addItem(new CValueControl("CurveCenteringStrength", "居中强度(60)x0.01", "朝车道中心回正的强度; 越大回正越狠越快, 越小越柔; 仅修正弯道中的偏离分量, 不影响直道与用户偏移", 10, 100, 5));
  featToggles->addItem(new CValueControl("CurveCenteringCurv", "激活曲率(4)x0.001", "触发弯道居中的最小曲率(1/m), 越大只在更急的弯才激活; 直道(曲率≈0)不生效", 1, 10, 1));
  // === 无车道线路沿居中（独立开关, 默认开; 仅无车道线且路沿可见时生效, 不影响有车道线路段）===
  featToggles->addItem(new CValueControl("EdgeCenteringEnabled", "无车道线路沿居中(1)", "无车道线且路沿可见时, 基于两侧路沿几何中心贴道路中心行驶, 避免模型偏左; 0:关闭(完全不影响原逻辑), 1:开启", 0, 1, 1));
  featToggles->addItem(new CValueControl("BlinkerTurnIntent", "转向灯转弯意图(0)", "开启后打转向灯时向模型发送转弯意图，低于设定速度时激活", 0, 1, 1));
  featToggles->addItem(new CValueControl("BlinkerTurnIntentSpeed", "转弯意图激活速度(30)km/h", "低于此速度打转向灯时激活转弯意图", 0, 120, 5));
  // === 弯道预备减速辅助（独立开关，默认关=零影响原逻辑）===
  featToggles->addItem(sectionHeader("弯道预备减速辅助"));
  featToggles->addItem(new CValueControl("CurveAnticipateMode", "入弯预备减速(0)", "接近弯道时提前柔和降到弯道限速, 避免弯中急刹; 0:关闭(完全不影响原逻辑), 1:开启", 0, 1, 1));
  featToggles->addItem(new CValueControl("CurveAnticipateDist", "预备前瞻距离(60)m", "往前看多远开始预备降速, 单位m; 越大越早开始减速越柔, 越小越晚", 20, 150, 5));
  featToggles->addItem(new CValueControl("CurveAnticipateLatA", "预备横向加速度(28)x0.1", "由曲率估算弯道限速时用的目标横向加速度, 单位0.1m/s^2; 越大降速越少越晚, 越小降速越多越早(下限由弯道限速兜底, 不会过慢)", 12, 50, 1));
  // === 幽灵刹车抑制（置信度阻尼，独立开关，默认关=零影响原逻辑）===
  featToggles->addItem(new CValueControl("PhantomBrakeGuardMode", "幽灵刹车抑制(0)", "高速雷达误检假前车导致无故急刹时, 柔和削弱其减速, 绝不删除前车; 0:关闭(完全不影响原逻辑), 1:标准, 2:激进", 0, 2, 1));
  featToggles->addItem(new CValueControl("PhantomBrakeGuardDist", "触发距离(250)x0.1m", "雷达报前车距离小于此值才进入幽灵判定, 单位0.1m; 越大只在更近的假目标才介入", 50, 600, 10));
  featToggles->addItem(new CValueControl("PhantomBrakeGuardConfirm", "多帧确认(10)", "连续命中幽灵判定的帧数才生效, 防单帧抖动误杀真实急刹; 越大越保守", 3, 30, 1));
  // === 急刹自动双闪（独立开关，默认关=零影响原逻辑）===
  featToggles->addItem(sectionHeader("急刹安全警示"));
  featToggles->addItem(new CValueControl("HazardBrakeMode", "急刹自动双闪(0)", "急减速时自动点亮双闪警示后方来车, 降低追尾风险; 0:关闭(完全不影响原逻辑), 1:急刹触发, 2:急刹+FCW碰撞预警触发", 0, 2, 1));
  featToggles->addItem(new CValueControl("HazardBrakeAccel", "触发减速度(-35)x0.1", "触发双闪的减速度阈值, 单位0.1m/s^2; 数值越负越难触发(更急的刹才闪), 建议-30~-45", -60, -15, 1));
  featToggles->addItem(new CValueControl("HazardBrakeRelease", "释放减速度(-15)x0.1", "刹车力度恢复到此值以上才开始计时熄灭(迟滞防抖), 单位0.1m/s^2", -40, -5, 1));
  featToggles->addItem(new CValueControl("HazardBrakeMinSpeed", "最低触发车速(30)km/h", "低于此车速不触发双闪, 避免市区蠕行/泊车误闪", 0, 120, 5));
  featToggles->addItem(new CValueControl("HazardBrakeHold", "熄灭延时(15)x0.1s", "减速恢复后双闪再保持的时间, 单位0.1s; 提示后车前方有情况", 5, 50, 1));
  featToggles->addItem(new CValueControl("HazardBrakeConfirm", "触发确认(3)x0.1s", "持续急刹多久才点亮, 单位0.1s; 防颠簸/单帧噪声误闪", 1, 10, 1));
  featToggles->addItem(sectionHeader("显示与画面"));
  featToggles->addItem(new CValueControl("ShowDrivePanel", "驾驶面板", "驾驶面板显示。0=隐藏;1=显示(行车数据面板)。", 0, 1, 1));
  // === 驾驶优化方案 2026-08-03: 前车切出(方案五) ===
  featToggles->addItem(new CValueControl("DynamicTFollowCutOut", "前车切出跟车释放(100)", "前车切出时释放跟车距离的强度(%)。0=关闭,100=最强。注:当前版本此参数预留,可能未实际生效,以实际表现为准。", 0, 100, 5));
  featToggles->addItem(new ParamControl("dp_ui_rainbow", tr("彩虹路径"), tr("将行驶路径显示为彩虹色动态渐变效果"), "", this));
  featToggles->addItem(new CValueControl("dp_ui_rainbow_speed", "彩虹流动速度(10)", "彩虹色沿路径流动的快慢; 数值越大流动越快, 越小越舒缓; 需先开启[彩虹路径]", 1, 50, 1));
  featToggles->addItem(new CValueControl("dp_ui_rainbow_brightness", "彩虹亮度(70)%", "彩虹路径的颜色亮度百分比; 数值越大越鲜亮, 越小越暗淡通透; 需先开启[彩虹路径]", 10, 100, 5));
  featToggles->addItem(new CValueControl("dp_ui_rainbow_width", "彩虹宽度(110)%", "彩虹路径的显示宽度百分比; 数值越大路径越宽, 越小越细; 需先开启[彩虹路径]", 30, 200, 5));
  featToggles->addItem(new ParamControl("TurnArcEnabled", tr("转向弧"), tr("屏幕底部中央显示弧形转向扭矩指示(移植 sunnypilot Steering Arc); 白色背景弧 + 彩色指示弧随转向摆动、接近最大转向变橙, 反映横向转向强度"), "", this));
  featToggles->addItem(new CValueControl("SteerArcColor", "转向弧颜色(1)", "转向弧配色。0=无色(仅白色背景弧+白点);1=橙色渐变;2=蓝粉红三色渐变。需先开启[转向弧]功能。", 0, 2, 1));

  // === 屏幕智能调光（独立开关，默认关=零影响）===
  featToggles->addItem(new CValueControl("AutoScreenDimMode", "屏幕智能调光(0)", "根据环境光(夜间/隧道)自动降低屏幕亮度与变暗, 减少刺眼; 0:关闭(完全不影响原逻辑), 1:智能降亮, 2:降亮+暗色遮罩", 0, 2, 1));
  featToggles->addItem(new CValueControl("AutoScreenDimLevel", "暗环境亮度(40)%", "暗环境/夜间时屏幕目标亮度百分比(及遮罩强度); 越小越暗越护眼, 越大越亮; 仅[屏幕智能调光]开启时生效", 10, 80, 5));
  featToggles->addItem(new CValueControl("ManualBrightness", "屏幕亮度(手动, 0)", "0=自动跟随环境光(原厂逻辑); 1-100=固定屏幕亮度百分比, 不受环境光与智能调光影响; 暗环境嫌太暗就设此值(如60)", 0, 100, 5));
  featToggles->addItem(new CValueControl("LaneLineColor", "车道线颜色(0=原厂白)", "0:原厂白(默认, 不改原厂逻辑); 1:赤, 2:橙, 3:黄, 4:绿, 5:青, 6:蓝, 7:紫; 行驾界面车道线随之变色", 0, 7, 1));

  // === 画面清爽模式（独立开关，默认关=零影响）===
  featToggles->addItem(new CValueControl("CleanViewMode", "画面清爽模式(0)", "车速超过设定值后自动隐藏画面上的信息图标(速度HUD/时间/胎压/雷达/盲区/转向/实验按钮/录屏按钮等), 只保留干净行驶画面; 0:关闭(完全不影响原逻辑), 1:隐藏图标(保留路径与车道线), 2:极净屏(连路径车道线也隐藏); 报警提示任何情况下都会显示", 0, 2, 1));
  featToggles->addItem(new CValueControl("CleanViewSpeed", "清爽触发车速(55)km/h", "车速达到此值自动进入画面清爽模式, 低于此值5km/h自动恢复正常显示(迟滞防闪烁); 仅[画面清爽模式]开启时生效", 30, 140, 5));

  // === 广角/长焦摄像头切换(模式 + 迟滞速度, 单位 km/h, 可调; 放在功能标签最后) ===
  featToggles->addItem(new CValueControl("CarrotWideCamMode", "广角摄像头模式", "0:自动切换(迟滞速度见下方两项), 1:仅长焦(road主摄), 2:仅广角(wide)", 0, 2, 1));
  featToggles->addItem(new CValueControl("CarrotWideCamSpeedLow", "广角切换速度(28)km/h", "自动模式下, 车速低于此值(单位km/h)时切到广角画面; 需小于[长焦切换速度]以形成迟滞, 防止在边界反复切换", 0, 120, 1));
  featToggles->addItem(new CValueControl("CarrotWideCamSpeedHigh", "长焦切换速度(32)km/h", "自动模式下, 车速高于此值(单位km/h)时切回长焦画面; 需大于[广角切换速度]以形成迟滞", 0, 120, 1));


  toggles_layout->addWidget(featToggles);
  featToggles->setVisible(true);
  applyLicenseLock();
  }
  ScrollView* toggles_view = new ScrollView(toggles, this);
  carrotLayout->addWidget(toggles_view, 1);

  homeScreen->setLayout(carrotLayout);
  main_layout->addWidget(homeScreen);
  main_layout->setCurrentWidget(homeScreen);

  if (panelMode == 0) togglesCarrot(0);
}

void CarrotPanel::togglesCarrot(int widgetIndex) {
  if (panelMode != 0) return;
  startToggles->setVisible(widgetIndex == 0);
  cruiseToggles->setVisible(widgetIndex == 1);
  navToggles->setVisible(widgetIndex == 2);
  speedToggles->setVisible(widgetIndex == 3);
  latLongToggles->setVisible(widgetIndex == 4);
  dispToggles->setVisible(widgetIndex == 5);
  trackToggles->setVisible(widgetIndex == 6);
}

// === 商用授权: 未激活/次数用完时, 设置底部「功能」标签页全部置灰并恢复关闭 ===
void CarrotPanel::applyLicenseLock() {
  // only lock the "功能" tab (featToggles, panelMode==1); never touch "萝卜" tab (mode0) or other menus
  if (panelMode == 0) return;
  std::string st = Params().get("CarrotLicStatus");
  bool activated = (st == "1");
  for (auto* c : featToggles->findChildren<AbstractControl*>()) c->setEnabled(activated);
  if (!activated) {
    for (auto* pc : featToggles->findChildren<ParamControl*>()) { Params().putBool(pc->getKey(), false); pc->refresh(); }
    struct IntSet { const char* key; int val; };
    static const IntSet off_int[] = {
      {"ShowDrivePanel", 0},
      {"CleanViewMode", 0},
      {"AutoScreenDimMode", 0},
      {"AutoScreenDimLevel", 40},
      {"ManualBrightness", 0},
    };
    for (const auto& it : off_int) Params().put(it.key, std::to_string(it.val));
  }
}


void CarrotPanel::showEvent(QShowEvent *event) {
  applyLicenseLock();
  QWidget::showEvent(event);
}

void CarrotPanel::updateButtonStyles() {
  if (panelMode != 0) return;
  QString styleSheet = R"(
      #start_btn, #cruise_btn, #nav_btn, #speed_btn, #latLong_btn ,#disp_btn, #track_btn {
        height: 120px; border-radius: 15px; background-color: #393939;
      }
      #start_btn:pressed, #cruise_btn:pressed, #nav_btn:pressed, #speed_btn:pressed, #latLong_btn:pressed, #disp_btn:pressed, #track_btn:pressed {
        background-color: #4a4a4a;
      }
  )";

  switch (currentCarrotIndex) {
  case 0:
    styleSheet += "#start_btn { background-color: #33ab4c; }";
    break;
  case 1:
    styleSheet += "#cruise_btn { background-color: #33ab4c; }";
    break;
  case 2:
    styleSheet += "#nav_btn { background-color: #33ab4c; }";
    break;
  case 3:
    styleSheet += "#speed_btn { background-color: #33ab4c; }";
    break;
  case 4:
    styleSheet += "#latLong_btn { background-color: #33ab4c; }";
    break;
  case 5:
    styleSheet += "#disp_btn { background-color: #33ab4c; }";
    break;
  case 6:
    styleSheet += "#track_btn { background-color: #33ab4c; }";
    break;
  }

  setStyleSheet(styleSheet);
}


CValueControl::CValueControl(const QString& params, const QString& title, const QString& desc, int min, int max, int unit,
                             const QString& base_key)
  : AbstractControl(title, desc), m_params(params), m_min(min), m_max(max), m_unit(unit), m_base_key(base_key) {

  label.setAlignment(Qt::AlignVCenter | Qt::AlignRight);
  label.setStyleSheet("color: #e0e879");
  hlayout->addWidget(&label);

  QString btnStyle = R"(
    QPushButton {
      padding: 0;
      border-radius: 50px;
      font-size: 20px;
      font-weight: 300;
      color: #E4E4E4;
      background-color: #393939;
    }
    QPushButton:pressed {
      background-color: #4a4a4a;
    }
  )";

  btnminus.setStyleSheet(btnStyle);
  btnplus.setStyleSheet(btnStyle);
  btnminus.setFixedSize(100, 100);
  btnplus.setFixedSize(100, 100);
  btnminus.setText("－");
  btnplus.setText("＋");
  hlayout->addWidget(&btnminus);
  hlayout->addWidget(&btnplus);

  connect(&btnminus, &QPushButton::released, this, &CValueControl::decreaseValue);
  connect(&btnplus, &QPushButton::released, this, &CValueControl::increaseValue);

  refresh();
}

void CValueControl::showEvent(QShowEvent* event) {
  AbstractControl::showEvent(event);
  refresh();
}

void CValueControl::refresh() {
  QString cur = QString::fromStdString(Params().get(m_params.toStdString()));
  if (!m_base_key.isEmpty()) {
    QString base = QString::fromStdString(Params().get(m_base_key.toStdString()));
    if (!base.isEmpty()) {
      label.setText(QString("%1 (基线 %2)").arg(cur).arg(base));
      return;
    }
  }
  label.setText(cur);
}

void CValueControl::adjustValue(int delta) {
  int value = QString::fromStdString(Params().get(m_params.toStdString())).toInt();
  value = qBound(m_min, value + delta, m_max);
  Params().putInt(m_params.toStdString(), value);
  refresh();
}

void CValueControl::increaseValue() {
  adjustValue(m_unit);
}

void CValueControl::decreaseValue() {
  adjustValue(-m_unit);
}
