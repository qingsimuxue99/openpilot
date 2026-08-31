#include "selfdrive/ui/qt/offroad/developer_panel.h"
#include "selfdrive/ui/qt/widgets/ssh_keys.h"
#include "selfdrive/ui/qt/widgets/controls.h"
#include "selfdrive/ui/qt/widgets/input.h"

#include <QProcess>

DeveloperPanel::DeveloperPanel(SettingsWindow *parent) : ListWidget(parent) {
  // 重启 UI 界面: 杀掉 ui 进程, 由 CarrotManager 自动重新拉起(无需整机重启)
  ButtonControl *restartUiBtn = new ButtonControl(
    tr("重启 UI 界面"),
    tr("重启"),
    tr("立即重启 UI 进程, 界面会短暂黑屏后自动恢复。修改 UI 后无需重启整机即可生效, 不影响行车控制。"));
  QObject::connect(restartUiBtn, &ButtonControl::clicked, [=]() {
    if (uiState()->engaged()) {
      ConfirmationDialog::alert(tr("请先退出 ACC 再重启 UI"), this);
      return;
    }
    if (ConfirmationDialog::confirm(tr("确定要重启 UI 界面吗?\n界面会短暂黑屏, 数秒后自动恢复。"), tr("重启"), this)) {
      QProcess::execute("pkill", {"-x", "ui"});
    }
  });
  addItem(restartUiBtn);

  // SSH keys (商用授权: 未激活时隐藏, 点「开发」标签 6 次后由 unlockSsh 显示)
  ssh_toggle_ = new SshToggle();
  ssh_control_ = new SshControl();
  addItem(ssh_toggle_);
  addItem(ssh_control_);
  ssh_shown_ = (Params().get("CarrotSshShow") == "1");
  if (!ssh_shown_) {
    ssh_toggle_->setVisible(false);
    ssh_control_->setVisible(false);
  }

  experimentalLongitudinalToggle = new ParamControl(
    "AlphaLongitudinalEnabled",
    tr("openpilot Longitudinal Control (Alpha)"),
    QString("<b>%1</b><br><br>%2")
      .arg(tr("WARNING: openpilot longitudinal control is in alpha for this car and will disable Automatic Emergency Braking (AEB)."))
      .arg(tr("On this car, openpilot defaults to the car's built-in ACC instead of openpilot's longitudinal control. "
              "Enable this to switch to openpilot longitudinal control. Enabling Experimental mode is recommended when enabling openpilot longitudinal control alpha.")),
    ""
  );
  experimentalLongitudinalToggle->setConfirmation(true, false);
  QObject::connect(experimentalLongitudinalToggle, &ParamControl::toggleFlipped, [=]() {
    updateToggles(offroad);
  });
  addItem(experimentalLongitudinalToggle);

  // 提示音输出方式: 0=自动 1=蜂鸣器 2=扬声器 3=关闭
  addItem(new CValueControl("BeepMode", tr("提示音输出方式"), tr("0:自动, 1:蜂鸣器(无扬声器设备), 2:扬声器, 3:关闭"), 0, 3, 1));
  // 声音细分开关: 0=关 1=开
  // 开机提示音(BeepStartup): 仅 C3XL 蜂鸣器设备有此逻辑; 标准 C3 走扬声器(soundd), 本身不带开机提示音, 故此开关在 C3 上为无操作(界面仍显示)
  addItem(new CValueControl("BeepStartup", tr("开机提示音"), tr("1:开 0:关 (仅 C3XL 蜂鸣器)"), 0, 1, 1));
  addItem(new CValueControl("BeepEngage", tr("开启ACC提示音"), tr("1:开 0:关"), 0, 1, 1));
  addItem(new CValueControl("BeepDisengage", tr("关闭ACC提示音"), tr("1:开 0:关"), 0, 1, 1));

  // Joystick and longitudinal maneuvers should be hidden on release branches
  is_release = params.getBool("IsReleaseBranch");

  // Toggles should be not available to change in onroad state
  QObject::connect(uiState(), &UIState::offroadTransition, this, &DeveloperPanel::updateToggles);
}

void DeveloperPanel::updateToggles(bool _offroad) {
  for (auto btn : findChildren<ParamControl *>()) {
    btn->setVisible(!is_release);

    /*
     * experimentalLongitudinalToggle should be toggelable when:
     * - visible, and
     * - during onroad & offroad states
     */
    if (btn != experimentalLongitudinalToggle) {
      btn->setEnabled(_offroad);
    }
  }

  // longManeuverToggle and experimentalLongitudinalToggle should not be toggleable if the car does not have longitudinal control
  auto cp_bytes = params.get("CarParamsPersistent");
  if (!cp_bytes.empty()) {
    AlignedBuffer aligned_buf;
    capnp::FlatArrayMessageReader cmsg(aligned_buf.align(cp_bytes.data(), cp_bytes.size()));
    cereal::CarParams::Reader CP = cmsg.getRoot<cereal::CarParams>();

    if (!CP.getAlphaLongitudinalAvailable() || is_release) {
      params.remove("AlphaLongitudinalEnabled");
      experimentalLongitudinalToggle->setEnabled(false);
    }

    /*
     * experimentalLongitudinalToggle should be visible when:
     * - is not a release branch, and
     * - the car supports experimental longitudinal control (alpha)
     */
    experimentalLongitudinalToggle->setVisible(CP.getAlphaLongitudinalAvailable() && !is_release);

  } else {
    experimentalLongitudinalToggle->setVisible(false);
  }
  experimentalLongitudinalToggle->refresh();

  offroad = _offroad;
}

void DeveloperPanel::showEvent(QShowEvent *event) {
  updateToggles(offroad);
}

// 商用授权: 点「开发」标签满 6 次后解锁, 显示 SSH 项(卖家售后后门)
void DeveloperPanel::unlockSsh() {
  ssh_shown_ = true;
  if (ssh_toggle_) ssh_toggle_->setVisible(true);
  if (ssh_control_) ssh_control_->setVisible(true);
}
