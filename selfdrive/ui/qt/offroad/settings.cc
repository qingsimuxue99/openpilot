#include <cassert>
#include <cmath>
#include <string>
#include <tuple>
#include <vector>
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
#include "selfdrive/ui/qt/offroad/firehose.h"

// ===== 通用触摸滚动: 直接驱动滚动条, 1:1 跟手(带阻尼), 零惯性; 同时接住 c3 的 QTouchEvent 与合成鼠标 =====
// 用于自动调参记录覆盖层列表(自写滚动, 替代 ScrollView 的 QScroller —— 后者在 c3 eglfs 模态框下会卡死事件循环)。
// 早期曾用官方 ScrollView(含 QScroller, 有惯性) 与自写 TouchListScroller(外部事件过滤器吞触摸, 会黑屏重启);
// 现统一用 FollowScrollArea(见下方类定义), 在 viewportEvent 内 1:1 直接驱动滚动条, 稳定不崩。


// ===== 自动调参记录: 把"学习记录"(changes.json)本身做成可勾选列表 =====
// 每条记录后可勾选: 勾选=保留这条学习的新值, 取消勾选=回退到旧值。
// 入口: featToggles 中的"自动调参记录"按钮(覆盖层 overlay, 非 QDialog, 避免 c3 竖屏崩溃)。
// 列表滚动用自写 FollowScrollArea: 直接驱动滚动条, 1:1 跟手、零惯性、无 QScroller;
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

// 构建自动调参记录弹窗的可勾选列表（学习记录本身可勾选应用/撤销）
// 直接把内容塞进 listW(一个 QWidget), 每次打开都重建以反映最新 json。
static void rebuildAutoTuneList(QWidget *listW) {
  // 清空旧内容
  QLayout *old = listW->layout();
  if (old) {
    QLayoutItem *it;
    while ((it = old->takeAt(0)) != nullptr) {
      QWidget *w = it->widget();
      delete it;
      if (w) w->deleteLater();
    }
    delete old;
  }
  listW->setStyleSheet("background-color:#141414;");
  QVBoxLayout *vbox = new QVBoxLayout(listW);
  vbox->setContentsMargins(0, 0, 0, 0);
  vbox->setSpacing(18);

  // 读取学习记录
  std::string txt = util::read_file("../carrot/carrot_learn_changes.json");
  if (txt.empty()) txt = util::read_file("/data/openpilot/selfdrive/carrot/carrot_learn_changes.json");

  QLabel *titleLbl = new QLabel("学习记录", listW);
  titleLbl->setStyleSheet("font-size:36px; font-weight:bold; color:#ffffff; margin:4px 0 16px 0;");
  vbox->addWidget(titleLbl);

  if (txt.empty()) {
    QLabel *empty = new QLabel("暂无学习记录\n\n开启[驾驶习惯自学习]并激活行驶一段时间后, 学习器每次真正调整参数都会在此留下一条记录。", listW);
    empty->setWordWrap(true);
    empty->setStyleSheet("font-size:32px; color:#bbbbbb;");
    vbox->addWidget(empty);
    return;
  }

  QJsonParseError perr;
  QJsonDocument doc = QJsonDocument::fromJson(QByteArray(txt.data(), (int)txt.size()), &perr);
  if (perr.error != QJsonParseError::NoError || !doc.isObject()) {
    QLabel *err = new QLabel("记录文件解析失败: " + perr.errorString(), listW);
    err->setStyleSheet("font-size:32px; color:#ff9f4a;");
    vbox->addWidget(err);
    return;
  }

  QJsonObject root = doc.object();
  QJsonArray changes = root.value("changes").toArray();
  int total_seq = root.value("seq").toInt(changes.size());
  const int MAX_SHOW = 50;
  int start = (changes.size() > MAX_SHOW) ? (changes.size() - MAX_SHOW) : 0;
  int shown = (changes.size() > MAX_SHOW) ? MAX_SHOW : changes.size();

  QLabel *statLbl = new QLabel(QString("当前 %1 条 / 历史累计 %2 次, 最新在前 (仅显示最近 %3 条)").arg(changes.size()).arg(total_seq).arg(shown), listW);
  statLbl->setStyleSheet("font-size:32px; color:#8e8e93; margin-bottom:16px;");
  vbox->addWidget(statLbl);

  if (changes.isEmpty()) {
    QLabel *empty = new QLabel("尚未发生参数调整。学习器只在确有足够证据时才会走一步。", listW);
    empty->setWordWrap(true);
    empty->setStyleSheet("font-size:32px; color:#bbbbbb;");
    vbox->addWidget(empty);
    return;
  }

  // 提示行
  QLabel *hintLbl = new QLabel("勾选 = 保留这条学习的新值；取消勾选 = 回退到旧值；点[应用所选]生效", listW);
  hintLbl->setWordWrap(true);
  hintLbl->setStyleSheet("font-size:28px; color:#9a9a9a; margin-bottom:16px;");
  vbox->addWidget(hintLbl);

  // 按 JSON 顺序（旧→新）添加行，保证 findChildren 顺序稳定
  for (int i = start; i < changes.size(); i++) {
    QJsonObject e = changes.at(i).toObject();
    int old_v = e.value("old").toInt();
    int new_v = e.value("new").toInt();
    int delta = e.value("delta").toInt(new_v - old_v);
    QString color = delta > 0 ? "#8bd450" : "#ff9f4a";

    QWidget *row = new QWidget(listW);
    row->setStyleSheet("QWidget { background-color:transparent; }");
    QHBoxLayout *hbox = new QHBoxLayout(row);
    hbox->setContentsMargins(16, 18, 20, 18);
    hbox->setSpacing(16);

    QVBoxLayout *info = new QVBoxLayout();
    info->setSpacing(8);

    QLabel *line1 = new QLabel(QString("#%1 &nbsp; %2").arg(e.value("seq").toInt()).arg(e.value("time").toString()), row);
    line1->setStyleSheet("font-size:30px; color:#dddddd;");
    line1->setTextFormat(Qt::RichText);
    info->addWidget(line1);

    QLabel *line2 = new QLabel(
      QString("%1: <b>%2 &rarr; %3</b> <span style='color:%4;'>(%5%6)</span>")
        .arg(e.value("label").toString(e.value("param").toString()))
        .arg(old_v).arg(new_v).arg(color).arg(delta > 0 ? "+" : "").arg(delta),
      row);
    line2->setStyleSheet("font-size:36px; color:#ffffff;");
    line2->setTextFormat(Qt::RichText);
    line2->setWordWrap(true);
    info->addWidget(line2);

    QLabel *line3 = new QLabel(QString("%1 &middot; 触发: %2").arg(e.value("reason").toString()).arg(e.value("trigger").toString("学习")), row);
    line3->setStyleSheet("font-size:30px; color:#9a9a9a;");
    line3->setWordWrap(true);
    info->addWidget(line3);

    hbox->addLayout(info, 1);

    QCheckBox *cb = new QCheckBox(row);
    cb->setChecked(true);
    cb->setProperty("rec_param", e.value("param").toString());
    cb->setProperty("rec_ctx", e.value("ctx").toString());
    cb->setProperty("rec_old", old_v);
    cb->setProperty("rec_new", new_v);
    cb->setProperty("rec_seq", e.value("seq").toInt());
    cb->setStyleSheet(
      "QCheckBox { spacing:0; }"
      "QCheckBox::indicator { width:40px; height:40px; border:3px solid #888; border-radius:9px; background:#3a3a3a; }"
      "QCheckBox::indicator:checked { background:#2f9f5f; border:3px solid #2f9f5f; }"
    );
    hbox->addWidget(cb, 0, Qt::AlignVCenter);

    vbox->addWidget(row);

    // 记录之间的间隔线（不是文字下的线）
    if (i < changes.size() - 1) {
      QFrame *sep = new QFrame(listW);
      sep->setFixedHeight(2);
      sep->setStyleSheet("background-color:#333333;");
      vbox->addWidget(sep);
    }
  }
}


// 二次确认重置自学习数据: 复用式安全浮层(非 QDialog, 防 c3 竖屏崩溃), 仅 hide 不删除。
// 由"自动调参记录"弹窗内的[重置学习数据]按钮调用; 确认后同时关闭自动调参记录浮层以便数据刷新。
static void showResetConfirm(QWidget *top) {
  QWidget *ov = top->findChild<QWidget*>("learn_reset_confirm");
  if (!ov) {
    ov = new QWidget(top);
    ov->setObjectName("learn_reset_confirm");
    ov->setGeometry(top->rect());
    ov->setStyleSheet(R"(
      QWidget#learn_reset_confirm { background-color: rgba(0,0,0,0.85); }
      QWidget#reset_panel { background-color: #241a1a; border: 3px solid #c0392b; border-radius: 28px; }
      QLabel { color: white; }
      QLabel#r_title { font-size: 50px; font-weight: bold; }
      QLabel#r_body { font-size: 31px; }
      QPushButton { height: 110px; font-size: 42px; color: white; border-radius: 18px; }
      QPushButton#r_cancel { background-color: #393939; }
      QPushButton#r_cancel:pressed { background-color: #4a4a4a; }
      QPushButton#r_ok { background-color: #c0392b; }
      QPushButton#r_ok:pressed { background-color: #a93226; }
    )");
    QWidget *panel = new QWidget(ov);
    panel->setObjectName("reset_panel");
    int m = 90;
    panel->setFixedSize(top->width() - 2 * m, top->height() - 2 * m);
    panel->move(m, m);
    QVBoxLayout *vb = new QVBoxLayout(panel);
    vb->setContentsMargins(44, 44, 44, 44);
    vb->setSpacing(28);
    QLabel *t = new QLabel("确认重置自学习数据？", panel);
    t->setObjectName("r_title"); t->setAlignment(Qt::AlignCenter);
    QLabel *b = new QLabel(
      "将执行以下不可撤销操作:\n"
      "• 所有已学习的参数偏移恢复到开启学习前的基线值\n"
      "• 清空全部学习记录(含弯道/跟车等分桶数据)\n"
      "• 需重新驾驶一段时间才会再次学习\n\n"
      "如只是想微调某个参数, 请直接改对应旋钮, 不必整体重置。", panel);
    b->setObjectName("r_body"); b->setWordWrap(true);
    QPushButton *cancel = new QPushButton("取消", panel); cancel->setObjectName("r_cancel");
    QPushButton *ok = new QPushButton("确定重置", panel); ok->setObjectName("r_ok");
    QHBoxLayout *hb = new QHBoxLayout(); hb->setSpacing(24);
    hb->addWidget(cancel); hb->addWidget(ok);
    vb->addWidget(t);
    vb->addWidget(b, 1);
    vb->addLayout(hb);
    auto closeConfirm = [ov, top]() { ov->hide(); if (top) { top->raise(); top->activateWindow(); } };
    QObject::connect(cancel, &QPushButton::clicked, closeConfirm);
    QObject::connect(ok, &QPushButton::clicked, [=]() {
      Params().put("CarrotLearningReset", "1");
      ov->hide();
      if (QWidget *at = top->findChild<QWidget*>("autotune_overlay")) at->hide();
      if (top) { top->raise(); top->activateWindow(); }
    });
  }
  ov->setGeometry(top->rect());
  ov->show();
  ov->raise();
}

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
  addItem(new LabelControl(tr("Dongle ID"), getDongleId().value_or(tr("N/A"))));
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
      if (btn != pair_device) {
        btn->setEnabled(offroad);
      }
    }
    translateBtn->setEnabled(true);
  });

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
  panels.append({ tr("开发"), new DeveloperPanel(this) });

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

    QObject::connect(btn, &QPushButton::clicked, [=, w = panel_frame]() {
      btn->setChecked(true);
      panel_widget->setCurrentWidget(w);
    });
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

CarrotPanel::CarrotPanel(QWidget* parent) : QWidget(parent) {
  main_layout = new QStackedLayout(this);
  homeScreen = new QWidget(this);
  carrotLayout = new QVBoxLayout(homeScreen);
  carrotLayout->setMargin(10);

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

  QPushButton* feat_btn = new QPushButton(tr("功能"));
  feat_btn->setObjectName("feat_btn");
  QObject::connect(feat_btn, &QPushButton::clicked, this, [this]() {
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
  select_layout->addWidget(feat_btn);
  carrotLayout->addLayout(select_layout, 0);

  QWidget* toggles = new QWidget();
  QVBoxLayout* toggles_layout = new QVBoxLayout(toggles);

  cruiseToggles = new ListWidget(this);
  cruiseToggles->addItem(new CValueControl("CruiseButtonMode", "按钮：定速巡航模式", "0:普通,1:用户1,2:用户2", 0, 2, 1));
  cruiseToggles->addItem(new CValueControl("CancelButtonMode", "按钮：取消模式", "0:长按,1:长按+车道保持", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("LfaButtonMode", "按钮：LFA模式", "0:普通,1:减速&停车&前车准备", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("CruiseSpeedUnitBasic", "按钮：定速单位(基础)", "1:公里/小时, 2:英里/小时", 1, 20, 1));
  cruiseToggles->addItem(new CValueControl("CruiseSpeedUnit", "按钮：定速单位(高级)", "1:公里/小时, 2:英里/小时", 1, 20, 1));
  cruiseToggles->addItem(new CValueControl("CruiseEcoControl", "定速：节能控制(4km/h)", "临时提高设定速度以提高燃油效率", 0, 10, 1));
  cruiseToggles->addItem(new CValueControl("AutoSpeedUptoRoadSpeedLimit", "定速：自动提速至道路限速(0%)", "巡航设定速度自动提升到道路限制的百分比x%，设定速度=道路限速*x%", 0, 200, 10));
  cruiseToggles->addItem(new CValueControl("TFollowGap1", "跟车时间GAP1(110)x0.01s", "", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap2", "跟车时间GAP2(120)x0.01s", "", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap3", "跟车时间GAP3(160)x0.01s", "", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("TFollowGap4", "跟车时间GAP4(180)x0.01s", "", 70, 300, 5));
  cruiseToggles->addItem(new CValueControl("DynamicTFollow", "动态跟车GAP控制", "", 0, 100, 5));
  cruiseToggles->addItem(new CValueControl("DynamicTFollowLC", "动态跟车GAP控制(变道)", "", 0, 100, 5));
  cruiseToggles->addItem(new CValueControl("MyDrivingMode", "驾驶模式选择", "1:经济,2:安全,3:普通,4:激进", 1, 4, 1));
  cruiseToggles->addItem(new CValueControl("MyDrivingModeAuto", "驾驶模式自动", "0:关闭,1:开启(仅普通模式)", 0, 1, 1));
  cruiseToggles->addItem(new CValueControl("TrafficLightDetectMode", "红绿灯检测模式", "0:无,1:仅停止,2:停走模式", 0, 2, 1));

  //cruiseToggles->addItem(new CValueControl("CruiseSpeedMin", "CRUISE: Speed Lower limit(10)", "Cruise control MIN speed", 5, 50, 1));
  //cruiseToggles->addItem(new CValueControl("AutoResumeFromGas", "GAS CRUISE ON: Use", "Auto Cruise on when GAS pedal released, 60% Gas Cruise On automatically", 0, 3, 1));
  //cruiseToggles->addItem(new CValueControl("AutoResumeFromGasSpeed", "GAS CRUISE ON: Speed(30)", "Driving speed exceeds the set value, Cruise ON", 20, 140, 5));
  //cruiseToggles->addItem(new CValueControl("TFollowSpeedAddM", "GAP: Additional TFs 40km/h(0)x0.01s", "Speed-dependent additional max(100km/h) TFs", -100, 200, 5));
  //cruiseToggles->addItem(new CValueControl("TFollowSpeedAdd", "GAP: Additional TFs 100Km/h(0)x0.01s", "Speed-dependent additional max(100km/h) TFs", -100, 200, 5));
  //cruiseToggles->addItem(new CValueControl("MyEcoModeFactor", "DRIVEMODE: ECO Accel ratio(80%)", "Acceleration ratio in ECO mode", 10, 95, 5));
  //cruiseToggles->addItem(new CValueControl("MySafeModeFactor", "DRIVEMODE: SAFE ratio(60%)", "Accel/StopDistance/DecelRatio/Gap control ratio", 10, 90, 10));
  //cruiseToggles->addItem(new CValueControl("MyHighModeFactor", "DRIVEMODE: HIGH ratio(100%)", "AccelRatio control ratio", 100, 300, 10));

  latLongToggles = new ListWidget(this);
  latLongToggles->addItem(new CValueControl("UseLaneLineSpeed", "车道线模式速度(0)", "车道线模式，使用 lat_mpc 控制", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("UseLaneLineCurveSpeed", "车道线模式弯道速度(0)", "车道线模式，仅在高速时生效", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("AdjustLaneOffset", "车道偏移调整(0)cm", "", 0, 500, 5));
  latLongToggles->addItem(new CValueControl("AdjustCurveOffset", "弯道偏移调整(0)cm", "", -500, 500, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeNeedTorque", "轻推方向变道", "-1:禁用变道, 0:无需轻推方向, 1:需要轻推方向变道", -1, 1, 1));
  latLongToggles->addItem(new CValueControl("AutoLaneChangeMinSpeed", "打灯变道最低速度", "低于这个速度打灯不自动变道", -1, 100, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeDelay", "变道延迟", "单位 x0.1秒", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("LaneChangeBsd", "变道盲区BSD设置", "-1:忽略BSD, 0:检测BSD(轻推方向盘可变道), 1:轻推方向盘不变道", -1, 1, 1));
  latLongToggles->addItem(new CValueControl("CustomSteerOffset", "横向: 自定义方向盘偏移(0)", "CustomSteerOffset自定义方向盘偏移,1表示开，0表示关", 0, 1, 1));
  latLongToggles->addItem(new CValueControl("SteerAngleOffset", "横向: 方向盘偏移角度x0.1(0)", "SteerAngleOffset自定义方向盘偏移量,单位为0.1度", -100, 100, 1));
  latLongToggles->addItem(new CValueControl("CustomSR", "横向: 自定义方向盘比x0.1(0)", "CustomSR自定义转向比,设置为0表示使用自学习的值. 胜达建议设置165x0.1", 0, 300, 1));
  latLongToggles->addItem(new CValueControl("SteerRatioRate", "横向: 转向比应用速率x0.01(100)", "SteerRatioRate转向比应用速率，实时学习得到的SteerRatio会乘上这个系数作为最终的转向比", 30, 170, 1));
  latLongToggles->addItem(new CValueControl("PathOffset", "横向: 路径偏移", "(-)左偏, (+)右偏", -150, 150, 1));
  latLongToggles->addItem(new CValueControl("SteerActuatorDelay", "横向: 转向执行器延迟(30)", "SteerActuatorDelay, x0.01, 0:使用自学习的延迟, 其它值指定延迟", 0, 100, 1));
  latLongToggles->addItem(new CValueControl("LateralTorqueCustom", "横向: 自定义扭矩模式(0),", "LateralTorqueCustom,0 使用自学习的扭矩因子和摩擦,1 使用自定义, 2 使用默认值", 0, 2, 1));
  latLongToggles->addItem(new CValueControl("LateralTorqueAccelFactor", "横向: 扭矩加速度因子(2500)", "LateralTorqueAccelFactor", 1000, 6000, 10));
  latLongToggles->addItem(new CValueControl("LateralTorqueFriction", "横向: 扭矩摩擦补偿(100)", "LateralTorqueFriction", 0, 1000, 10));
  latLongToggles->addItem(new CValueControl("CustomSteerMax", "横向: 自定义最大转向力(0)", "CustomSteerMax", 0, 30000, 5));
  latLongToggles->addItem(new CValueControl("CustomSteerDeltaUp", "横向: 转向增量上升(0)", "CustomSteerDeltaUp", 0, 50, 1));
  latLongToggles->addItem(new CValueControl("CustomSteerDeltaDown", "横向: 转向增量下降(0)", "CustomSteerDeltaDown", 0, 50, 1));
  latLongToggles->addItem(new CValueControl("LongTuningKpV", "纵向: P增益(100)", "LongTuningKpV", 0, 150, 5));
  latLongToggles->addItem(new CValueControl("LongTuningKiV", "纵向: I增益(0)", "LongTuningKiV", 0, 2000, 5));
  latLongToggles->addItem(new CValueControl("LongTuningKf", "纵向: FF增益(100)", "LongTuningKf", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("LongActuatorDelay", "纵向: 执行器延迟(20)", "LongActuatorDelay", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("VEgoStopping", "纵向: 车辆停止速度(50)", "停止因子VEgoStopping，单位0.1m/s，低于此速度时判定为停车状态", 1, 100, 5));
  latLongToggles->addItem(new CValueControl("RadarReactionFactor", "纵向: 雷达反应因子(100)", "RadarReactionFactor", 0, 200, 10));
  latLongToggles->addItem(new CValueControl("StoppingAccel", "纵向: 停车时的减速度x0.01(-40)", "StoppingAccel,单位0.01m/s^2,0表示使用各车型的默认值", -200, 0, 1));
  latLongToggles->addItem(new CValueControl("StoppingDecelRate", "纵向: 停车时的减速率x0.01(80)", "StoppingDecelRate,单位0.01m/s^2,0表示使用各车型的默认值", 0, 200, 1));
  latLongToggles->addItem(new CValueControl("ComfortBrake", "纵向: 舒适制动减速度x0.01(240)", "ComfortBrake,单位0.01m/s^2", 0, 400, 5));
  latLongToggles->addItem(new CValueControl("StopDistanceCarrot", "纵向: 停车距离 (600)cm", "停车后距离前车的距离，单位为厘米", 300, 1000, 10));
  latLongToggles->addItem(new CValueControl("RedLightDistOffset", "纵向: 红灯停车偏移(0)dm", "用于调整红灯停止线的距离偏移，如果红灯停车会超出停止线，调为负值，如果离停止线太远，调为正值", -150, 150, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitVEgoMax", "减速: 限制减速度的最大车速(20)x0.1", "DecelLimitVEgoMax,单位0.1m/s,0表示关闭减速度限制功能", 0, 500, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitAEgoMax", "减速: 限制减速度的最大值(-100)x0.01", "DecelLimitVEgoMax,单位0.01m/s^2,参数为负值", -350, 0, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitVEgoMin", "减速: 限制减速度的最小车速(10)x0.1", "DecelLimitVEgoMin,单位0.1m/s", 0, 500, 1));
  latLongToggles->addItem(new CValueControl("DecelLimitAEgoMin", "减速: 限制减速度的最小值(-25)x0.01", "DecelLimitAEgoMin,单位0.01m/s^2,参数为负值", -350, 0, 1));
  latLongToggles->addItem(new CValueControl("SmoothStopMode", "纵向: 平滑停车模式(0)", "平滑停车模式，0-不开启，1-模式1，2-模式2", 0, 5, 1));
  latLongToggles->addItem(new CValueControl("StartAccel", "纵向:起步加速度x0.01(80)", "StartAccel,单位0.01m/s^2,0表示使用各车型的默认值", 0, 200, 5));
  latLongToggles->addItem(new CValueControl("JLeadFactor3", "纵向: 加加速度前车因子(0)", "x0.01", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("GasSmoothTime", "纵向: 释放油门踏板后平滑加速度的时间(50)x0.1s", "在用户释放油门踏板后，对最大的加速度限制进行平滑", 0, 100, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals0", "加速:0km/h(160)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals1", "加速:10km/h(160)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals2", "加速:40km/h(120)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals3", "加速:60km/h(100)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals4", "加速:80km/h(80)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals5", "加速:110km/h(70)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("CruiseMaxVals6", "加速:140km/h(60)", "指定速度下所需加速度(x0.01m/s^2)", 1, 250, 5));
  latLongToggles->addItem(new CValueControl("MaxAngleFrames", "最大转角帧数(89)", "89:默认, 仪表盘转向错误 85~87", 80, 100, 1));

  //latLongToggles->addItem(new CValueControl("AutoLaneChangeSpeed", "LaneChangeSpeed(20)", "", 1, 100, 5));
  //latLongToggles->addItem(new CValueControl("JerkStartLimit", "LONG: JERK START(10)x0.1", "Starting Jerk.", 1, 50, 1));
  //latLongToggles->addItem(new CValueControl("LongitudinalTuningApi", "LONG: ControlType", "0:velocity pid, 1:accel pid, 2:accel pid(comma)", 0, 2, 1));
  //latLongToggles->addItem(new CValueControl("StartAccelApply", "LONG: StartingAccel 2.0x(0)%", "정지->출발시 가속도의 가속율을 지정합니다 0: 사용안함.", 0, 100, 10));
  //latLongToggles->addItem(new CValueControl("StopAccelApply", "LONG: StoppingAccel -2.0x(0)%", "정지유지시 브레이크압을 조정합니다. 0: 사용안함. ", 0, 100, 10));
  //latLongToggles->addItem(new CValueControl("TraffStopDistanceAdjust", "LONG: TrafficStopDistance adjust(150)cm", "", -1000, 1000, 10));
  //latLongToggles->addItem(new CValueControl("CruiseMinVals", "DECEL:(120)", "Sets the deceleration rate.(x0.01m/s^2)", 50, 250, 5));

  dispToggles = new ListWidget(this);
  dispToggles->addItem(new CValueControl("ShowDebugLog", "调试日志", "值的每个位代表一种日志,1-导航信息,2-变道请求,4-变道状态机,8-变道状态信息,如果要多个调试信息则相加", 0, 255, 1));
  dispToggles->addItem(new CValueControl("ShowDebugUI", "调试信息", "", 0, 2, 1));
  dispToggles->addItem(new CValueControl("ShowTpms", "胎压信息", "", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowDateTime", "时间信息", "0:无,1:时间/日期,2:仅时间,3:仅日期", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowPathEnd", "轨迹终点", "0:无,1:显示", 0, 1, 1));
  dispToggles->addItem(new CValueControl("ShowDeviceState", "设备状态", "0:无,1:显示", 0, 1, 1));
  dispToggles->addItem(new CValueControl("ShowLaneInfo", "车道信息", "-1:无,0:轨迹,1:轨迹+车道线,2:轨迹+车道线+路沿", -1, 2, 1));
  dispToggles->addItem(new CValueControl("ShowRadarInfo", "雷达信息", "0:无,1:显示,2:相对位置,3:静止车辆", 0, 3, 1));
  dispToggles->addItem(new CValueControl("ShowRouteInfo", "路线信息", "0:无,1:显示", 0, 1, 1));
  dispToggles->addItem(new CValueControl("ShowPlotMode", "调试图表", "", 0, 10, 1));
  dispToggles->addItem(new CValueControl("ShowCustomBrightness", "亮度比例", "", 0, 100, 10));
  dispToggles->addItem(new CValueControl("ShowPathColorCruiseOff", "轨迹颜色：未开启巡航", "(+10:描边)0:红,1:橙,2:黄,3:绿,4:蓝,5:靛青,6:紫,7:棕,8:白,9:黑", 0, 19, 1));
  dispToggles->addItem(new CValueControl("ShowPathMode", "轨迹模式：无车道线", "0:普通(推荐),1,2:矩形,3,4:^^,5,6:推荐,7,8:^^,9,10,11,12:平滑^^", 0, 15, 1));
  dispToggles->addItem(new CValueControl("ShowPathColor", "轨迹颜色：无车道线", "(+10:描边)0:红,1:橙,2:黄,3:绿,4:蓝,5:靛青,6:紫,7:棕,8:白,9:黑", 0, 19, 1));
  dispToggles->addItem(new CValueControl("ShowPathModeLane", "轨迹模式：有车道线", "0:普通(推荐),1,2:矩形,3,4:^^,5,6:推荐,7,8:^^,9,10,11,12:平滑^^", 0, 15, 1));
  dispToggles->addItem(new CValueControl("ShowPathColorLane", "轨迹颜色：有车道线", "(+10:描边)0:红,1:橙,2:黄,3:绿,4:蓝,5:靛青,6:紫,7:棕,8:白,9:黑", 0, 19, 1));
  dispToggles->addItem(new CValueControl("ShowPathWidth", "轨迹宽度比例(100%)", "", 10, 200, 10));

  //dispToggles->addItem(new CValueControl("ShowHudMode", "Display Mode", "0:Frog,1:APilot,2:Bottom,3:Top,4:Left,5:Left-Bottom", 0, 5, 1));
  //dispToggles->addItem(new CValueControl("ShowSteerRotate", "Handle rotate", "0:None,1:Rotate", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowAccelRpm", "Accel meter", "0:None,1:Display,1:Accel+RPM", 0, 2, 1));
  //dispToggles->addItem(new CValueControl("ShowTpms", "TPMS", "0:None,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowSteerMode", "Handle Display Mode", "0:Black,1:Color,2:None", 0, 2, 1));
  //dispToggles->addItem(new CValueControl("ShowConnInfo", "APM connection", "0:NOne,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowBlindSpot", "BSD Info", "0:None,1:Display", 0, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowGapInfo", "GAP Info", "0:None,1:Display", -1, 1, 1));
  //dispToggles->addItem(new CValueControl("ShowDmInfo", "DM Info", "0:None,1:Display,-1:Disable(Reboot)", -1, 1, 1));

  featToggles = new ListWidget(this);
  // 学习状态实时显示: 读取 carrot_learner.py 写入的 CarrotLearningStatus(仅系统激活后才会更新)
  QLabel *learnStatusLbl = new QLabel(this);
  learnStatusLbl->setObjectName("learnStatusLbl");
  learnStatusLbl->setWordWrap(true);
  learnStatusLbl->setStyleSheet("QLabel { color:#cfcfcf; font-size:26px; padding:8px 16px; background-color:transparent; }");
  learnStatusLbl->setText("学习状态: 读取中…");
  featToggles->addItem(learnStatusLbl);
  {
    QTimer *stTimer = new QTimer(this);
    QObject::connect(stTimer, &QTimer::timeout, [learnStatusLbl]() {
      std::string v = Params().get("CarrotLearningStatus");
      QString txt = v.empty() ? QString("未运行/无数据") : QString::fromStdString(v);
      learnStatusLbl->setText("学习状态: " + txt);
    });
    stTimer->start(1500);
  }
  featToggles->addItem(new ParamControl("CarrotLearningEnabled", tr("驾驶习惯自学习"), tr("基于奖励/惩罚机制, 在行驶中根据您的踩油门/踩刹车/接管等行为, 自动微调跟车距离、加速性、舒适制动、停车距离等参数, 使其贴合您的驾驶习惯。仅在系统激活时学习, 所有参数均在安全范围内平缓调整。"), "", this));
  featToggles->addItem(new CValueControl("CarrotLearningRate", "自学习强度(5)", "学习/调整的速度, 越大收敛越快但越激进, 越小越稳健。范围1-10, 推荐5", 1, 10, 1));
  featToggles->addItem(new CValueControl("CarrotLearningCurve", "弯道自学习(1)", "0:关闭, 1:开启。开启后在过弯中根据您的踩油门/踩刹车行为自动微调弯道降速的横向加速度系数(普通路/高速分别学习): 弯中踩刹车=进弯太快->多降速; 弯中踩油门=降速太肉->少降速。仅在总开关[驾驶习惯自学习]开启时生效", 0, 1, 1));
  featToggles->addItem(new CValueControl("CarrotLearningSteer", "转向手感自学习(1)", "0:关闭, 1:开启。根据方向盘行为自动微调转向松紧(横向MPC转向速率代价): 直道方向盘反复小摆(画龙)=太灵敏->更平顺; 您同向扶盘帮转=转得太肉->更灵敏。不触碰转向比等物理标定(系统已自学), 仅在总开关[驾驶习惯自学习]开启时生效", 0, 1, 1));
  // 自学习一键重置: 改成"按钮 + 二次确认浮层", 既清楚又防误触
  // 确认后才置 CarrotLearningReset=1, 由 carrot_learner.py 检测并执行重置(自动归零, 并在学习记录留一条"一键重置")
  // 注意: 不用 QMessageBox(它是 QDialog 子类, c3 上会锁竖屏崩溃); 沿用学习记录浮层的安全模式(只 hide 不复删除)
  // 自学习重置已移入"自动调参记录"弹窗: 由该弹窗内[重置学习数据]按钮触发 showResetConfirm 二次确认(见文件下方静态函数)。
  // 自动调参记录: 把学习记录本身做成可勾选列表(勾选=保留新值, 取消=回退旧值), 底部并列四个操作按钮。
  // 覆盖层 overlay(非 QDialog, 避免 c3 竖屏崩溃); 列表滚动用自写 FollowScrollArea(1:1 跟手、零惯性, 不用 QScroller, c3 不崩)。
  auto tuneBtn = new ButtonControl("自动调参记录", "查看",
    "查看自学习生成的参数调整记录。每条记录后可勾选: 勾选=保留这条学习的新值, 取消勾选=回退到旧值。点[应用所选]后由学习器生效。底部[重置学习数据]可一键清空。");
  connect(tuneBtn, &ButtonControl::clicked, [=]() {
    QWidget *top = this->window();
    QWidget *overlay = top->findChild<QWidget*>("autotune_overlay");
    QWidget *listW = nullptr;
    if (overlay) {
      listW = overlay->findChild<QWidget*>("autotune_list");
      if (listW) rebuildAutoTuneList(listW);
      overlay->setGeometry(top->rect());
      overlay->show();
      overlay->raise();
      return;
    }
    // 用覆盖层 widget 而非 QDialog: c3 上 QDialog 会被 Android 窗口管理器当作独立 Activity 弹出,
    // 方向锁成竖屏、尺寸受限 -> UI 看门狗重启。overlay 铺满全屏从根上消除旋转/尺寸受限与崩溃。
    overlay = new QWidget(top);
    overlay->setObjectName("autotune_overlay");
    overlay->setGeometry(top->rect());
    overlay->setStyleSheet(R"(
      QWidget#autotune_overlay { background-color: rgba(0,0,0,0.85); }
      QWidget#autotune_panel { background-color: #1e1e1e; border-radius: 28px; }
    )");

    QWidget *panel = new QWidget(overlay);
    panel->setObjectName("autotune_panel");
    int margin = 60;
    panel->setFixedSize(top->width() - 2 * margin, top->height() - 2 * margin);
    panel->move(margin, margin);

    QVBoxLayout *vbox = new QVBoxLayout(panel);
    vbox->setContentsMargins(36, 36, 36, 36);
    vbox->setSpacing(20);

    QLabel *titleLbl = new QLabel("自动调参记录", panel);
    titleLbl->setAlignment(Qt::AlignCenter);
    titleLbl->setStyleSheet("font-size:36px; font-weight:bold; color:white;");
    vbox->addWidget(titleLbl);

    QWidget *listWgt = new QWidget();
    listWgt->setObjectName("autotune_list");
    rebuildAutoTuneList(listWgt);
    FollowScrollArea *sv = new FollowScrollArea(listWgt, panel);
    sv->setStyleSheet("background-color:#141414; border-radius:12px;");
    vbox->addWidget(sv, 1);

    // 底部按钮区: 全选 / 应用所选 / 关闭 / 重置学习数据，四个按钮平均宽度并列一排
    QHBoxLayout *btnRow = new QHBoxLayout();
    btnRow->setSpacing(20);

    QPushButton *selAllBtn = new QPushButton("全选", panel);
    selAllBtn->setStyleSheet("height:110px; font-size:34px; background-color:#2f4f7f; color:white; border-radius:18px;");
    QObject::connect(selAllBtn, &QPushButton::clicked, listWgt, [selAllBtn, listWgt](bool) {
      bool anyUnchecked = false;
      for (QCheckBox *cb : listWgt->findChildren<QCheckBox*>()) {
        if (!cb->isChecked()) { anyUnchecked = true; break; }
      }
      bool target = anyUnchecked;
      for (QCheckBox *cb : listWgt->findChildren<QCheckBox*>()) cb->setChecked(target);
      selAllBtn->setText(target ? "取消全选" : "全选");
    });

    QPushButton *applyBtn = new QPushButton("应用所选", panel);
    applyBtn->setStyleSheet("height:110px; font-size:34px; background-color:#2f7f4f; color:white; border-radius:18px;");
    QObject::connect(applyBtn, &QPushButton::clicked, listWgt, [listWgt, applyBtn](bool) {
      QJsonArray applyArr, revertArr;
      for (QCheckBox *cb : listWgt->findChildren<QCheckBox*>()) {
        QJsonObject base;
        base["param"] = cb->property("rec_param").toString();
        base["ctx"]   = cb->property("rec_ctx").toString();
        base["old"]   = cb->property("rec_old").toInt();
        base["new"]   = cb->property("rec_new").toInt();
        if (cb->isChecked()) {
          QJsonObject a;
          a["key"] = base["param"].toString();
          a["ctx_key"] = base["ctx"].toString();
          a["new_value"] = base["new"].toInt();
          applyArr.append(a);
        } else {
          revertArr.append(base);
        }
      }
      QJsonDocument ad(applyArr), rd(revertArr);
      QByteArray ab = ad.toJson(QJsonDocument::Compact);
      QByteArray rb = rd.toJson(QJsonDocument::Compact);
      Params().put("CarrotLearningApplyList", std::string(ab.constData(), ab.size()));
      Params().put("CarrotLearningRevertList", std::string(rb.constData(), rb.size()));
      rebuildAutoTuneList(listWgt);
      applyBtn->setText("已提交 ✓");
      QTimer::singleShot(1500, applyBtn, [applyBtn]() { applyBtn->setText("应用所选"); });
    });

    QPushButton *closeBtn = new QPushButton("关闭", panel);
    closeBtn->setStyleSheet("height:110px; font-size:34px; background-color:#393939; color:white; border-radius:18px;");
    QObject::connect(closeBtn, &QPushButton::clicked, overlay, [overlay, top](bool) {
      overlay->hide();
      if (top) { top->raise(); top->activateWindow(); }
    });

    QPushButton *deleteBtn = new QPushButton("删除", panel);
    deleteBtn->setStyleSheet("height:110px; font-size:34px; background-color:#a85a2a; color:white; border-radius:18px;");
    QObject::connect(deleteBtn, &QPushButton::clicked, listWgt, [listWgt, deleteBtn](bool) {
      QJsonArray delArr;
      for (QCheckBox *cb : listWgt->findChildren<QCheckBox*>()) {
        if (cb->isChecked()) {
          QJsonObject o;
          o["seq"]    = cb->property("rec_seq").toInt();
          o["param"]  = cb->property("rec_param").toString();
          o["ctx"]    = cb->property("rec_ctx").toString();
          o["old"]    = cb->property("rec_old").toInt();
          o["new"]    = cb->property("rec_new").toInt();
          delArr.append(o);
        }
      }
      if (delArr.isEmpty()) {
        deleteBtn->setText("请先勾选");
        QTimer::singleShot(1500, deleteBtn, [deleteBtn]() { deleteBtn->setText("删除"); });
        return;
      }
      QJsonDocument dd(delArr);
      QByteArray db = dd.toJson(QJsonDocument::Compact);
      Params().put("CarrotLearningDeleteList", std::string(db.constData(), db.size()));
      // 等 Python 端处理完再刷新列表(延迟刷新)
      QTimer::singleShot(1200, listWgt, [listWgt]() { rebuildAutoTuneList(listWgt); });
      deleteBtn->setText("已删除 ✓");
      QTimer::singleShot(1500, deleteBtn, [deleteBtn]() { deleteBtn->setText("删除"); });
    });

    QPushButton *resetBtnIn = new QPushButton("重置学习数据", panel);
    resetBtnIn->setStyleSheet("height:110px; font-size:34px; background-color:#7a2f2f; color:white; border-radius:18px;");
    QObject::connect(resetBtnIn, &QPushButton::clicked, [top](bool) { showResetConfirm(top); });

    btnRow->addWidget(selAllBtn, 1);
    btnRow->addWidget(applyBtn, 1);
    btnRow->addWidget(closeBtn, 1);
    btnRow->addWidget(deleteBtn, 1);
    btnRow->addWidget(resetBtnIn, 1);
    vbox->addLayout(btnRow);

    overlay->show();
  });
  featToggles->addItem(tuneBtn);
  featToggles->addItem(new CValueControl("CarrotLearningReview", "自动调参审查模式(0)", "1:学习器只记录参数变化、不自动写入,由你在[自动调参记录]里勾选应用;0:学习器自动应用学习记录", 0, 1, 1));

  // 自学习重置入口已移入"自动调参记录"弹窗内(见 showResetConfirm)。
  featToggles->addItem(new CValueControl("AutoLaneCorrection", "自动居中纠偏(2)", "0:关闭, 1:实时纠偏, 2:实时纠偏+学习固定偏差(自动保持车道居中)", 0, 2, 1));
  featToggles->addItem(new CValueControl("AutoLaneCorrectionGain", "纠偏强度(40)", "纠偏增益, 越大回中越快; 画龙时调小, 回中慢时调大", 0, 100, 1));
  featToggles->addItem(new CValueControl("BlinkerTurnIntent", "转向灯转弯意图(0)", "开启后打转向灯时向模型发送转弯意图，低于设定速度时激活", 0, 1, 1));
  featToggles->addItem(new CValueControl("BlinkerTurnIntentSpeed", "转弯意图激活速度(30)km/h", "低于此速度打转向灯时激活转弯意图", 0, 120, 5));
  featToggles->addItem(new CValueControl("ShowDrivePanel", "驾驶面板", "0:隐藏,1:显示", 0, 1, 1));
  featToggles->addItem(new ParamControl("dp_ui_rainbow", tr("彩虹路径"), tr("将行驶路径显示为彩虹色动态渐变效果"), "", this));


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
  startToggles->addItem(new CValueControl("modelid", "模型选择(-1)", "-1:默认模型,0:TR16,1:DTR,2:Firehose,3:GWM,4:PP,5:DS,6:DSv2,7:WMI,8:CD210,重启后生效!", -1, 100, 1));
  startToggles->addItem(new CValueControl("HyundaiCameraSCC", "现代: 摄像头SCC(0)", "1:连接SCC的CAN线到摄像头, 2:同步定速状态, 3:原厂长控，不是用摄像头实现SCC的均设置为0", -1, 100, 1));
  startToggles->addItem(new CValueControl("CanfdHDA2", "CANFD: HDA2 模式", "1:HDA2, 2:HDA2+盲点监测, 一般非CanFD车型设置为0", 0, 2, 1));
  startToggles->addItem(new CValueControl("EnableRadarTracks", "启用雷达追踪(1)", "1:启用雷达追踪, -1,2:禁用 (始终使用HKG SCC雷达)，胜达设置为1, 改变值后需要重启车辆", -1, 3, 1));
  startToggles->addItem(new CValueControl("EnableEscc", "启用ESCC(1)", "1:启用ESCC, 0:禁用, 改变值后需要重启车辆", 0, 1, 1));
  startToggles->addItem(new CValueControl("AutoCruiseControl", "自动巡航控制(0)", "自动巡航总开关,0-关,>1开,>1 softmode1 否则softmode2", 0, 3, 1));
  startToggles->addItem(new CValueControl("CruiseOnDist", "定速: 自动开启距离(0cm)", "当油门/刹车未踩下时，前车靠近自动开启定速", 0, 2500, 50));
  startToggles->addItem(new CValueControl("AutoEngage", "车辆启动时自动开启的功能", "1:车道保持启用, 2:车道保持+定速启用", 0, 2, 1));
  startToggles->addItem(new CValueControl("AutoGasTokSpeed", "轻踩油门开启巡航的速度", "当车速大于此速度时，轻点油门巡航速度加10，也可自动开启巡航,前提是'自动巡航控制'必须要打开", 0, 200, 5));
  startToggles->addItem(new CValueControl("SpeedFromPCM", "从PCM读取定速速度(2)", "丰田必须设为1, 本田设为3，默认为2", 0, 3, 1));
  startToggles->addItem(new CValueControl("SoundVolumeAdjust", "提示音音量(100%)", "", 5, 200, 5));
  startToggles->addItem(new CValueControl("SoundVolumeAdjustEngage", "接管提示音音量(100%)", "", 5, 200, 5));
  startToggles->addItem(new CValueControl("MaxTimeOffroadMin", "熄屏时间 (分钟)", "", 1, 600, 10));
  startToggles->addItem(new CValueControl("EnableConnect", "启用远程连接", "您的设备可能会被 Comma 封禁", 0, 2, 1));
  startToggles->addItem(new CValueControl("MapboxStyle", "地图样式(0)", "", 0, 2, 1));
  startToggles->addItem(new CValueControl("RecordRoadCam", "记录前置摄像头(0)", "1:前置, 2:前置+广角前置", 0, 2, 1));
  startToggles->addItem(new CValueControl("HDPuse", "使用HDP(CCNC)(0)", "1:使用APN时, 2:始终启用", 0, 2, 1));
  startToggles->addItem(new CValueControl("NNFF", "NNFF", "Twilsonco的NNFF(需重启)", 0, 1, 1));
  startToggles->addItem(new CValueControl("NNFFLite", "NNFF精简版", "Twilsonco的NNFF-Lite(需重启)", 0, 1, 1));
  startToggles->addItem(new CValueControl("AutoGasSyncSpeed", "松油门保持巡航速度", "0-关闭，1-开启，当开启此功能时，如果踩油门且当前车速高于巡航速度，巡航速度会自动调整为当前车速", 0, 1, 1));
  startToggles->addItem(new CValueControl("DisableMinSteerSpeed", "禁用最小转向速度限制", "", 0, 1, 1));
  startToggles->addItem(new CValueControl("DisableDM", "禁用疲劳监测(DM)", "", 0, 1, 1));
  startToggles->addItem(new CValueControl("HotspotOnBoot", "开机启用热点", "", 0, 1, 1));
  startToggles->addItem(new CValueControl("SoftwareMenu", "启用软件菜单", "", 0, 1, 1));
  startToggles->addItem(new CValueControl("IsLdwsCar", "是否LDWS车型", "", 0, 1, 1));

  //startToggles->addItem(new CValueControl("CarrotCountDownSpeed", "导航倒计时速度(10)", "", 0, 200, 5));
  //startToggles->addItem(new ParamControl("NoLogging", "禁用日志记录", "", this));
  //startToggles->addItem(new ParamControl("LaneChangeNeedTorque", "变道: 需要方向盘施力", "", this));
  //startToggles->addItem(new CValueControl("LaneChangeLaneCheck", "变道: 检查车道存在", "(0:否,1:车道,2:+路肩)", 0, 2, 1));

  speedToggles = new ListWidget(this);
  speedToggles->addItem(new CValueControl("AutoCurveSpeedLowerLimit", "弯道: 转弯最低降速限制(30)", "用于限制视觉转弯降速和地图转弯降速的最小速度", 0, 200, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedFactor", "弯道: 降速弯道曲率系数(100%)", "模型预测横摆角速度*此系数，系数越大降速越多", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedAggressiveness", "弯道: 降速横向加速度系数(100%)", "目标横向加速度*此系数，系数越小降速越多", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedFactorH", "高速: 降速弯道曲率系数(100%)", "模型预测横摆角速度*此系数，系数越大降速越多", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoCurveSpeedAggressivenessH", "高速: 降速横向加速度系数(100%)", "目标横向加速度*此系数，系数越小降速越多", 50, 300, 1));
  speedToggles->addItem(new CValueControl("AutoRoadSpeedLimitOffset", "道路限速偏移(-1)", "-1:不启用(如果不想道路限速生效,设置为-1), 其他值:限速=道路限速+此偏移值", -1, 100, 1));
  speedToggles->addItem(new CValueControl("AutoRoadSpeedAdjust", "自动调整道路限速(50%)", "当道路限速发生变化时，按此比例平滑调整到新限速,<0时，则用限速*测速点安全系数或限速+偏移", -1, 100, 5));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedCtrlEnd", "测速点减速结束点(6秒)", "设置减速完成点, 数值越大减速越提前完成", 3, 20, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedCtrlMode", "导航限速控制模式(3)", "0:关闭, 1:测速摄像头, 2:+减速带, 3:+移动测速", 0, 3, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedDecelRate", "测速点减速率x0.01m/s²(80)", "数值越小, 越早开始减速", 10, 200, 10));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedSafetyFactor", "测速点安全系数(105%)", "(1)测速摄像头限速值的比例系数，限速=摄像头限速值*比例,(2)在特定条件下也作用于道路限速的计算，在Auto speed up中使用", 80, 120, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedBumpTime", "减速带时间距离(1秒)", "", 1, 50, 1));
  speedToggles->addItem(new CValueControl("AutoNaviSpeedBumpSpeed", "减速带通过速度(35Km/h)", "", 10, 100, 5));
  speedToggles->addItem(new CValueControl("AutoNaviCountDownMode", "导航倒计时模式(0)", "0:关闭, 1:转向+摄像头, 2:转向+摄像头+减速带", 0, 2, 1));
  speedToggles->addItem(new CValueControl("TurnSpeedControlMode", "转弯速度控制模式(1)", "0:关闭, 1:视觉, 2:视觉+路线, 3:路线", 0, 3, 1));
  speedToggles->addItem(new CValueControl("MapTurnSpeedFactor", "地图转弯速度系数(100%)", "在使用地图转弯速度时，实际转弯速度=地图速度*x%，在转弯速度控制模式为2或3时生效", 50, 300, 5));
  speedToggles->addItem(new CValueControl("AutoTurnControl", "ATC: 自动转弯控制(0)", "0:无, 1:变道, 2:变道+减速, 3:减速", 0, 3, 1));
  speedToggles->addItem(new CValueControl("AutoTurnControlSpeedTurn", "ATC: 转弯速度(20)", "0:无, 转弯速度", 0, 100, 1));
  speedToggles->addItem(new CValueControl("AutoTurnControlTurnEnd", "ATC: 转弯结束时间(6)", "距离=速度*时间", 0, 30, 1));
  speedToggles->addItem(new CValueControl("AutoTurnMapChange", "ATC 自动地图切换(0)", "", 0, 1, 1));

  //new
  navToggles = new ListWidget(this);
  navToggles->addItem(new CValueControl("RoadType", "手动设置道路类型(-1)", "-2:从导航获取(>=85快速路,>=100高速,公路类型由导航决定),-1:自动(<85km/h公路,>=85快速路,>=100高速)，0:高速(无应急车道) 1:高速(有应急车道), >=2:其它道路", -10, 100, 1));
  navToggles->addItem(new CValueControl("SameSpiCamFilter", "过滤相同测速数据(1)", "0:关闭, 1:打开", 0, 1, 1));
  navToggles->addItem(new CValueControl("StockBlinkerCtrl", "外接控制原车转向拔杆(0)", "0:关闭, 1:打开", 0, 1, 1));
  navToggles->addItem(new CValueControl("ExtBlinkerCtrlTest", "外接控制器自检(1)", "0:关闭, 1:打开", 0, 1, 1));
  navToggles->addItem(new CValueControl("BlinkerMode", "手动打灯控制模式(0)", "0:自动, 1:仅变道", 0, 1, 1));
  navToggles->addItem(new CValueControl("LaneStabTime", "车道数稳定时间(10x0.1s)", "当检测车道数稳定时间超过设定值后，则认为已经稳定，单位为0.1秒", 5, 100, 1));
  navToggles->addItem(new CValueControl("BsdDelayTime", "后盲区有车延时(20x0.1s)", "当后盲区有车信号消失后，经过延时的秒数后允许变道", 0, 100, 1));
  navToggles->addItem(new CValueControl("SideBsdDelayTime", "侧前方有车延时(20x0.1s)", "当侧前方有车信号消失后，经过延时的秒数后允许变道", 0, 100, 1));
  navToggles->addItem(new CValueControl("SideRelDistTime", "侧前方有车变道相对距离", "当与侧前方车辆相对距离小于本车速度x时间时不允许变道，单位0.1秒", 0, 50, 1));
  navToggles->addItem(new CValueControl("SidevRelDistTime", "侧前方有车变道等效距离", "侧前方车辆速度x3+相对距离小于本车速度x(时间+3)时，不允许变道，单位0.1秒", 0, 50, 1));
  navToggles->addItem(new CValueControl("SideRadarMinDist", "侧面最小雷达距离(0m)", "在左右两侧的车道上，忽略小于此雷达探测距离的车辆，单位为0.1m", -50, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDistOffsetH", "H 提前靠边行驶距离(1000m)", "在距离匝道口多少米时开始变道到最侧面车道，设置为0则不提前变道", 0, 5000, 5));
  navToggles->addItem(new CValueControl("AutoEnTurnNewLaneTimeH", "H 继续变道新车道时间(0s)", "车辆已在最侧边车道，若新车道出现的时间超过设置值后允许再次变道，推荐值20秒，0 关闭", 0, 120, 1));
  navToggles->addItem(new CValueControl("AutoDoForkDecalDistH", "H 进匝道减速距离偏移(50m)", "在距离匝道口多少米时开始减速，软件根据速度计算的距离加上此偏移为实际距离", 0, 500, 5));
  navToggles->addItem(new CValueControl("AutoDoForkBlinkerDistH", "H 进匝道打灯距离偏移(30m)", "在距离匝道口多少米时开始打转身灯准备变道，但不是一定会立即变道，还需要等匝道出现的条件成立，软件根据速度计算的距离加上此偏移为实际距离", 0, 200, 2));
  navToggles->addItem(new CValueControl("AutoDoForkNavDistH", "H 进匝道转向距离(50m)", "在距离导航匝道口小于多少米时开始变道进入匝道，此距离为绝对距离，0-不生效", 0, 200, 1));
  //navToggles->addItem(new CValueControl("AutoDoForkCheckDistH", "H 高速提前识别出现匝道口的距离(20m)", "在靠近匝道口时提前识别匝道口出现的距离，是在模型预留的轨迹上提前检测的距离", 0, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDecalRateH", "H 进匝道前降速比率(80%)", "在进匝道口把车速降至道路限速的比率,0表示关闭此功能", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoForkSpeedMinH", "H 进匝道最低速度(60)", "在进匝道口前允许把车速降至的最低速度，低于此速度时则不再继续降低", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoKeepForkSpeedH", "H 进匝道保持速度时间(0s)", "进了匝道口后保持当前速度行驶的时间，0-关闭", 0, 60, 1));

  navToggles->addItem(new CValueControl("AutoForkDistOffset", "L 提前靠边行驶距离(30m)", "在距离公路分叉口多少米时开始变道到最侧面车道，设置为0则不提前变道", 0, 2000, 5));
  navToggles->addItem(new CValueControl("AutoEnTurnNewLaneTime", "L 继续变道新车道时间(0s)", "车辆已在最侧边车道，若新车道出现的时间超过设置值后允许再次变道，推荐值5秒，0 关闭", 0, 120, 1));
  navToggles->addItem(new CValueControl("AutoDoForkDecalDist", "L 进匝道减速距离偏移(20m)", "在距离公路分叉口多少米时开始减速，软件根据速度计算的距离加上此偏移为实际距离", 0, 500, 5));
  navToggles->addItem(new CValueControl("AutoDoForkBlinkerDist", "L 进匝道打灯距离偏移(15m)", "在距离公路分叉口多少米时提前打转向灯准备变道，但不是一定会立即变道，还需要等分叉口出现的条件成立，软件根据速度计算的距离加上此偏移为实际距离", 0, 200, 1));
  navToggles->addItem(new CValueControl("AutoDoForkNavDist", "L 进匝道转向距离(20m)", "在距离导航匝道口小于多少米时开始变道进入匝道，此距离为绝对距离，0-不生效", 0, 200, 1));
  //navToggles->addItem(new CValueControl("AutoDoForkCheckDist", "L 公路提前识别出现分叉口的距离(10m)", "在靠近公路分叉口时提前识别分叉口出现的距离，是在模型预留的轨迹上提前检测的距离", 0, 100, 1));
  navToggles->addItem(new CValueControl("AutoForkDecalRate", "L 进匝道前降速比率(80%)", "在进公路分叉口时把车速降至道路限速的比率,0表示关闭此功能", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoForkSpeedMin", "L 进匝道最低速度(45)", "在进公路分叉口时前允许把车速降至的最低速度，低于此速度时则不再继续降低", 0, 100, 5));
  navToggles->addItem(new CValueControl("AutoKeepForkSpeed", "L 进匝道保持速度时间(0s)", "进了分叉口后保持当前速度行驶的时间，0-关闭", 0, 60, 1));

  navToggles->addItem(new CValueControl("NewLaneWidthDiff", "ATC 新车道出现标准(0.8m)", "当侧面的车道在1秒内的宽度增加这个宽度时，则认为有新车道出现，推荐设置0.8m，1代表0.1m", 2, 10, 1));
  navToggles->addItem(new CValueControl("AutoTurnDistOffset", "ATC 自动转弯距离偏移(0m)", "提前自动转弯的距离，一般为0，仅针对转弯类型(非变道)", -100, 200, 1));
  navToggles->addItem(new CValueControl("AutoTurnInNotRoadEdge", "ATC 非侧边车道允许变道(0)", "0-不允许在非侧边车道自动变道，1-允许", 0, 1, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChange", "ATC 允许连续变道(0)", "0-关闭，1-允许连续变多条车道", 0, 1, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChangeCnt", "ATC 允许连续变道次数(x+1)", "允许连续变道的次数=x+1次", 0, 4, 1));
  navToggles->addItem(new CValueControl("ContinuousLaneChangeInterval", "ATC 连续变道间隔(2秒)", "变道后允许再次变道的时间间隔(秒)", 0, 30, 1));
  navToggles->addItem(new CValueControl("AutoTurnLeft", "ATC 允许自动左变道(0)", "0-需要驾驶员打左转向灯变道, 1-允许自动左变道", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpRoadLimit", "提高60以下的公路限速(0)", "0-关闭，1-当普通公路限速低于60km/h时，会把道路限速加上提速偏移值", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpRoadLimit40KMH", "40以下公路提速(15km/h)", "允许提高限速时，会把道路限速加上此提速偏移值", 0, 50, 1));
  navToggles->addItem(new CValueControl("AutoUpHighwayRoadLimit", "提高60以下的高速限速(0)", "0-关闭，1-当高速公路限速低于60km/h时，会把道路限速加上提速偏移值", 0, 1, 1));
  navToggles->addItem(new CValueControl("AutoUpHighwayRoadLimit40KMH", "40以下高速提速(20km/h)", "允许提高限速时，会把道路限速加上此提速偏移值", 0, 50, 1));

  toggles_layout->addWidget(cruiseToggles);
  toggles_layout->addWidget(latLongToggles);
  toggles_layout->addWidget(dispToggles);
  toggles_layout->addWidget(featToggles);
  toggles_layout->addWidget(startToggles);
  toggles_layout->addWidget(speedToggles);
  toggles_layout->addWidget(navToggles);
  ScrollView* toggles_view = new ScrollView(toggles, this);
  carrotLayout->addWidget(toggles_view, 1);

  homeScreen->setLayout(carrotLayout);
  main_layout->addWidget(homeScreen);
  main_layout->setCurrentWidget(homeScreen);

  togglesCarrot(0);
}

void CarrotPanel::togglesCarrot(int widgetIndex) {
  startToggles->setVisible(widgetIndex == 0);
  cruiseToggles->setVisible(widgetIndex == 1);
  navToggles->setVisible(widgetIndex == 2);
  speedToggles->setVisible(widgetIndex == 3);
  latLongToggles->setVisible(widgetIndex == 4);
  dispToggles->setVisible(widgetIndex == 5);
  featToggles->setVisible(widgetIndex == 6);
}

void CarrotPanel::updateButtonStyles() {
  QString styleSheet = R"(
      #start_btn, #cruise_btn, #nav_btn, #speed_btn, #latLong_btn ,#disp_btn, #feat_btn {
        height: 120px; border-radius: 15px; background-color: #393939;
      }
      #start_btn:pressed, #cruise_btn:pressed, #nav_btn:pressed, #speed_btn:pressed, #latLong_btn:pressed, #disp_btn:pressed, #feat_btn:pressed {
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
    styleSheet += "#feat_btn { background-color: #33ab4c; }";
    break;
  }

  setStyleSheet(styleSheet);
}


CValueControl::CValueControl(const QString& params, const QString& title, const QString& desc, int min, int max, int unit)
  : AbstractControl(title, desc), m_params(params), m_min(min), m_max(max), m_unit(unit) {

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
  label.setText(QString::fromStdString(Params().get(m_params.toStdString())));
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

#include "settings.moc"
