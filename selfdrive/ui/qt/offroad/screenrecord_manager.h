#pragma once

#include <QDialog>
#include <QWidget>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QCheckBox>
#include <QScrollArea>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QStorageInfo>
#include <QDate>
#include <QDateTime>
#include <QShowEvent>
#include <QMouseEvent>
#include <QFrame>
#include <QProcess>
#include <QNetworkInterface>
#include <QAbstractSocket>

#include <QrCode.hpp>

#include "selfdrive/ui/qt/widgets/input.h"
#include "selfdrive/ui/qt/offroad/settings.h"

// 扫码下载弹窗(白底二维码)
class DownloadQRPopup : public DialogBase {
  Q_OBJECT
public:
  explicit DownloadQRPopup(const QString &url, const QString &filename, QWidget *parent = 0);
};

// 录像列表行: 整行可点击切换 checkbox, 选中状态行底色+描边变化
// 注意: 必须放在顶层(非嵌套类), Qt moc 不支持嵌套类使用 Q_OBJECT
class RecRow : public QWidget {
  Q_OBJECT
public:
  explicit RecRow(QWidget *parent = nullptr);
  void bindCheckBox(QCheckBox *cb);
public slots:
  void updateStyle();
protected:
  void mousePressEvent(QMouseEvent *e) override;
  void mouseReleaseEvent(QMouseEvent *e) override;
private:
  QCheckBox *cb_ = nullptr;
  QPoint pressPos_;
};

// 录像管理面板(内嵌进设置页, 点「录像」标签即显示, 不再是弹窗)
class ScreenRecordManager : public QWidget {
  Q_OBJECT
public:
  explicit ScreenRecordManager(QWidget *parent = 0);
  void showEvent(QShowEvent *event) override;

private slots:
  void refreshList();
  void toggleSelectAll();
  void deleteSelected();
  void downloadClip(const QString &filename);
  void downloadSelected();      // 多选打包下载(zip)
  void runAutoClean();
  void updateSelectAllBtnText();
  void toggleSettings();         // 展开/收起清理设置

private:
  QString lanIP();
  QStringList selectedClips();
  void showQR(const QString &fileOnDisk, const QString &caption);

  QWidget *listWidget = nullptr;
  QVBoxLayout *listLayout = nullptr;
  QList<QCheckBox*> checkboxes;
  QList<QString> clipNames;
  bool allSelected = false;
  bool updatingAll = false;        // 全选/取消全选批量操作中: 屏蔽逐项信号回调改写 allSelected

  QFrame *setCard = nullptr;       // 清理设置卡片(默认收起)
  QPushButton *setBtn = nullptr;   // 展开/收起按钮
  QPushButton *selAllBtn = nullptr;
  QPushButton *bundleBtn = nullptr;
  QLabel *statLabel = nullptr;     // 统计信息(数量/占用/剩余)
};
