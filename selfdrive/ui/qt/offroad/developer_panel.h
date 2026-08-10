#pragma once

#include "selfdrive/ui/qt/offroad/settings.h"

class DeveloperPanel : public ListWidget {
  Q_OBJECT
public:
  explicit DeveloperPanel(SettingsWindow *parent);
  void showEvent(QShowEvent *event) override;
  // 商用授权: 未激活时 SSH 项隐藏, 点「开发」标签 6 次后调用解锁显示
  void unlockSsh();

private:
  Params params;
  ParamControl* experimentalLongitudinalToggle;
  bool is_release;
  bool offroad = false;

  // 商用授权: 未激活时隐藏「启用 SSH」与「SSH 密钥」
  class SshToggle* ssh_toggle_ = nullptr;
  class SshControl* ssh_control_ = nullptr;
  bool ssh_shown_ = false;   // 是否已显示 SSH 项(激活用户或点「开发」6 次后)

private slots:
  void updateToggles(bool _offroad);
};
