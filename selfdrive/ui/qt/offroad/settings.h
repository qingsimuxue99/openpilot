#pragma once

#include <map>
#include <string>

#include <QButtonGroup>
#include <QFrame>
#include <QLabel>
#include <QPushButton>
#include <QStackedWidget>
#include <QWidget>

#include "selfdrive/ui/ui.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/qt/widgets/controls.h"

// ********** settings window + top-level panels **********
class DeveloperPanel;   // 商用授权: 点「开发」标签 6 次解锁 SSH 显示
class DrivingModelPanel;  // 驾驶模型选择器 (本地列表切换 + 在线下载)

class SettingsWindow : public QFrame {
  Q_OBJECT

public:
  explicit SettingsWindow(QWidget *parent = 0);
  void setCurrentPanel(int index, const QString &param = "");

protected:
  void showEvent(QShowEvent *event) override;

signals:
  void closeSettings();
  void reviewTrainingGuide();
  void showDriverView();
  void expandToggleDescription(const QString &param);

private:
  QPushButton *sidebar_alert_widget;
  QWidget *sidebar_widget;
  QButtonGroup *nav_btns;
  QStackedWidget *panel_widget;

  // 商用授权: 点「开发」标签文字累计 6 次解锁 SSH 显示; 切换其他菜单清零
  DeveloperPanel *dev_panel_ = nullptr;
  int dev_click_count_ = 0;
};

class DevicePanel : public ListWidget {
  Q_OBJECT
public:
  explicit DevicePanel(SettingsWindow *parent);

signals:
  void reviewTrainingGuide();
  void showDriverView();

protected:
  void showEvent(QShowEvent *event) override;

private slots:
  void poweroff();
  void reboot();
  //re_Calibration
  void calibration();
  void updateCalibDescription();

private:
  // onroad/offroad mode switch (moved here from SoftwarePanel)
  void updateOnOffRoadBtn();

  Params params;
  ButtonControl *pair_device;
  ButtonControl *onOffRoadBtn = nullptr;
  ParamWatcher *onoffroad_watch = nullptr;
  // 商用授权: 剩余激活次数显示(设备菜单, showEvent 刷新)
  class LabelControl* lic_remain_lbl_ = nullptr;
};

class TogglesPanel : public ListWidget {
  Q_OBJECT
public:
  explicit TogglesPanel(SettingsWindow *parent);
  void showEvent(QShowEvent *event) override;

public slots:
  void expandToggleDescription(const QString &param);

private slots:
  void updateState(const UIState &s);

private:
  Params params;
  std::map<std::string, ParamControl*> toggles;
  ButtonParamControl *long_personality_setting;

  void updateToggles();
};

class SoftwarePanel : public ListWidget {
  Q_OBJECT
public:
  explicit SoftwarePanel(QWidget* parent = nullptr);

private:
  void showEvent(QShowEvent *event) override;
  void updateLabels();
  void checkForUpdates();

  bool is_onroad = false;

  QLabel *onroadLbl;
  LabelControl *versionLbl;
  ButtonControl *installBtn;
  ButtonControl *downloadBtn;
  ButtonControl *targetBranchBtn;

  Params params;
  ParamWatcher *fs_watch;
};

// Forward declaration
class FirehosePanel;

class CarrotPanel : public QWidget {
  Q_OBJECT

private:
  QStackedLayout* main_layout = nullptr;
  QWidget* homeScreen = nullptr;
  int currentCarrotIndex = 0;
  int panelMode = 0;  // 0:萝卜面板(多标签)  1:一级「功能」面板(单页)

  QWidget* homeWidget;
  QVBoxLayout* carrotLayout;

  ListWidget* cruiseToggles = nullptr;
  ListWidget* latLongToggles = nullptr;
  ListWidget* featToggles = nullptr;
  ListWidget* dispToggles = nullptr;
  ListWidget* startToggles = nullptr;
  ListWidget* speedToggles = nullptr;
  ListWidget* navToggles = nullptr;
  ListWidget* trackToggles = nullptr;

  void togglesCarrot(int widgetIndex);
  void updateButtonStyles();

public:
  explicit CarrotPanel(QWidget* parent = nullptr, int mode = 0);

  // 商用授权: 未激活/次数用完时锁定全部定制功能项(置灰不可操作)
  void applyLicenseLock();
  void showEvent(QShowEvent *event) override;
};

class CValueControl : public AbstractControl {
  Q_OBJECT

public:
  CValueControl(const QString& params, const QString& title, const QString& desc, int min, int max, int unit = 1,
                 const QString& base_key = QString());

private slots:
  void increaseValue();
  void decreaseValue();

private:
  void showEvent(QShowEvent* event) override;
  void refresh();
  void adjustValue(int delta);

  QPushButton btnplus;
  QPushButton btnminus;
  QLabel label;
  QString m_base_key;   // 可选: 若非空, refresh 时在 label 追加 " (基线 X)"

  QString m_params;
  int m_min;
  int m_max;
  int m_unit;
};
