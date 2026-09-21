#include "selfdrive/ui/qt/offroad/screenrecord_manager.h"
#include <cstdlib>
#include <QApplication>
#include <QStorageInfo>
#include "selfdrive/ui/qt/widgets/scrollview.h"

using qrcodegen::QrCode;

// 录像落盘目录, 与 screenrecorder.cc:43 及 fleet.SCREENRECORD_PATH 保持一致
#define REC_DIR "/data/media/0/videos"
// 设备内 python(用于 zip 打包, 设备无 zip 命令但有 python zipfile)
#define DEV_PYTHON "/usr/local/venv/bin/python3"

// 人类可读体积
static QString fmtSize(qint64 b) {
  if (b >= 1024LL * 1024 * 1024) return QString::number(b / 1024.0 / 1024 / 1024, 'f', 1) + " GB";
  if (b >= 1024LL * 1024) return QString::number(b / 1024.0 / 1024, 'f', 1) + " MB";
  return QString::number(b / 1024.0, 'f', 0) + " KB";
}

// 生成黑白二维码位图(文件级, 供两个弹窗复用)
static QPixmap makeQrPixmap(const QString &text) {
  QrCode qr = QrCode::encodeText(text.toUtf8().data(), QrCode::Ecc::LOW);
  int sz = qr.getSize();
  QImage im(sz, sz, QImage::Format_RGB32);
  QRgb black = qRgb(0, 0, 0);
  QRgb white = qRgb(255, 255, 255);
  for (int y = 0; y < sz; y++) {
    for (int x = 0; x < sz; x++) {
      im.setPixel(x, y, qr.getModule(x, y) ? black : white);
    }
  }
  int scale = 8;
  return QPixmap::fromImage(im.scaled(sz * scale, sz * scale, Qt::KeepAspectRatio), Qt::MonoOnly);
}

// ============ RecRow (整行可点击切换 checkbox) ============
RecRow::RecRow(QWidget *parent) : QWidget(parent) {
  setAttribute(Qt::WA_StyledBackground, true);
  setCursor(Qt::PointingHandCursor);
  setFixedHeight(104);
}
void RecRow::bindCheckBox(QCheckBox *cb) {
  cb_ = cb;
  QObject::connect(cb_, &QCheckBox::toggled, this, &RecRow::updateStyle);
  updateStyle();
}
void RecRow::mousePressEvent(QMouseEvent *e) {
  // 仅记录按下位置, 不在此切换勾选; 切换放到 mouseReleaseEvent 并判断移动距离,
  // 避免与 ScrollView 的触摸滚动拖拽冲突(否则手指一拖就误切换勾选)
  pressPos_ = e->pos();
  QWidget::mousePressEvent(e);
}
void RecRow::mouseReleaseEvent(QMouseEvent *e) {
  // 按下到松开未明显移动(视为点击)才切换勾选; 拖动则交给 ScrollView 滚动。
  // 阈值放宽为 30 逻辑像素: c3 触控屏手指按下/松开坐标有噪声抖动, 10 太严易被判成拖动吞掉点击。
  if (cb_ && (e->pos() - pressPos_).manhattanLength() < 30) {
    cb_->toggle();
  }
  QWidget::mouseReleaseEvent(e);
}
void RecRow::updateStyle() {
  if (!cb_) return;
  bool on = cb_->isChecked();
  // 未选中也留 3px 透明描边, 避免选中时内容位移抖动
  setStyleSheet(QString("RecRow { background-color:%1; border-radius:16px; border:3px solid %2; }")
                .arg(on ? "#33506E" : "#2E2E2E")
                .arg(on ? "#7FB3E0" : "transparent"));
}

// ============ ScreenRecordManager ============
ScreenRecordManager::ScreenRecordManager(QWidget *parent) : QWidget(parent) {
  setStyleSheet(R"(
    ScreenRecordManager { background-color: #171717; }
    ScreenRecordManager QLabel { color: #E8E8E8; }
    ScreenRecordManager QPushButton { border-radius: 20px; font-size: 30px; font-weight: 500;
                                      padding: 8px 26px; color:#FFFFFF; background-color:#3A3A3A; min-height:70px; }
    ScreenRecordManager QPushButton:pressed { background-color:#5A5A5A; }
    ScreenRecordManager QPushButton:disabled { color:#8A8A8A; background-color:#2A2A2A; }
    ScreenRecordManager QPushButton#primary { background-color:#2F5C8A; }
    ScreenRecordManager QPushButton#primary:pressed { background-color:#3F72A8; }
    ScreenRecordManager QPushButton#danger { background-color:#7A3B3B; }
    ScreenRecordManager QPushButton#danger:pressed { background-color:#9A4B4B; }
    ScreenRecordManager QPushButton#closeBtn { background-color:#4E4E4E; }
    ScreenRecordManager QPushButton#closeBtn:pressed { background-color:#6E6E6E; }
    ScreenRecordManager QCheckBox { color:#E8E8E8; background:transparent; }
    ScreenRecordManager QCheckBox::indicator { width:44px; height:44px; }
    ScreenRecordManager QFrame#card { background-color:#232323; border-radius:20px; }
  )");

  // 初始化自动清理参数默认值(仅首次)
  Params p;
  if (p.get("CarrotScreenRecAutoClean").empty()) p.put("CarrotScreenRecAutoClean", "0");
  if (p.get("CarrotScreenRecMaxDays").empty()) p.put("CarrotScreenRecMaxDays", "7");
  if (p.get("CarrotScreenRecMinFreeGB").empty()) p.put("CarrotScreenRecMinFreeGB", "2");

  QVBoxLayout *main = new QVBoxLayout(this);
  main->setContentsMargins(0, 0, 0, 0);  // 内嵌进设置页, 边距由 settings 的 panel margin 统一控制(和功能菜单一致)
  main->setSpacing(20);

  // 整个内容区放进一个普通 widget, footer 跟在内容后; 内嵌进设置后由 settings 外层 ScrollView 统一滚动。
  QWidget *content = new QWidget(this);
  QVBoxLayout *contentLay = new QVBoxLayout(content);
  contentLay->setContentsMargins(0, 0, 0, 0);
  contentLay->setSpacing(20);

  // ===== 标题区(不放任何按钮, 避免与开关/关闭互相误触) =====
  QVBoxLayout *head = new QVBoxLayout();
  head->setSpacing(6);
  QLabel *title = new QLabel(tr("录像管理"), this);
  title->setStyleSheet("font-size: 52px; font-weight: 700; color:#FFFFFF; background:transparent;");
  statLabel = new QLabel("", this);
  statLabel->setStyleSheet("font-size: 26px; color:#9E9E9E; background:transparent;");
  head->addWidget(title);
  head->addWidget(statLabel);
  contentLay->addLayout(head);

  // ===== 清理操作条(放在最上方: 清理设置 / 立即清理, 两按钮均匀拉伸) =====
  QFrame *cleanCard = new QFrame(this);
  cleanCard->setObjectName("card");
  QHBoxLayout *cleanBar = new QHBoxLayout(cleanCard);
  cleanBar->setContentsMargins(22, 16, 22, 16);
  cleanBar->setSpacing(18);
  setBtn = new QPushButton(tr("录像设置"), this);
  QPushButton *cleanBtn = new QPushButton(tr("立即清理"), this);
  cleanBtn->setObjectName("primary");
  // 关掉 autoDefault/default, 避免 c3 上默认按钮把点击路由到其它按钮(误触串功能)
  setBtn->setAutoDefault(false); setBtn->setDefault(false);
  cleanBtn->setAutoDefault(false); cleanBtn->setDefault(false);
  QObject::connect(setBtn, &QPushButton::clicked, this, &ScreenRecordManager::toggleSettings);
  QObject::connect(cleanBtn, &QPushButton::clicked, this, &ScreenRecordManager::runAutoClean);
  cleanBar->addWidget(setBtn, 1);
  cleanBar->addWidget(cleanBtn, 1);
  contentLay->addWidget(cleanCard);

  // ===== 操作卡片 =====
  QFrame *opCard = new QFrame(this);
  opCard->setObjectName("card");
  QHBoxLayout *opBar = new QHBoxLayout(opCard);
  opBar->setContentsMargins(22, 16, 22, 16);
  opBar->setSpacing(18);
  selAllBtn = new QPushButton(tr("全选"), this);
  bundleBtn = new QPushButton(tr("打包下载"), this);
  bundleBtn->setObjectName("primary");
  QPushButton *delBtn = new QPushButton(tr("删除选中"), this);
  delBtn->setObjectName("danger");
  QPushButton *refreshBtn = new QPushButton(tr("刷新"), this);
  opBar->addWidget(selAllBtn, 1);
  opBar->addWidget(bundleBtn, 1);
  opBar->addWidget(delBtn, 1);
  opBar->addWidget(refreshBtn, 1);
  QObject::connect(selAllBtn, &QPushButton::clicked, this, &ScreenRecordManager::toggleSelectAll);
  QObject::connect(bundleBtn, &QPushButton::clicked, this, &ScreenRecordManager::downloadSelected);
  QObject::connect(delBtn, &QPushButton::clicked, this, &ScreenRecordManager::deleteSelected);
  QObject::connect(refreshBtn, &QPushButton::clicked, this, &ScreenRecordManager::refreshList);
  contentLay->addWidget(opCard);

  // ===== 清理设置卡片(默认收起, 放在列表上方, 远离底部关闭按钮) =====
  setCard = new QFrame(this);
  setCard->setObjectName("card");
  QVBoxLayout *setLay = new QVBoxLayout(setCard);
  setLay->setContentsMargins(26, 20, 26, 20);
  setLay->setSpacing(10);
  setLay->addWidget(new CValueControl("CarrotScreenRecAutoStart", tr("开机自动录像"),
    tr("滑动设置: 0=关闭, 1=开启。开启后: 进入行车界面即自动开始录制(无需手动点按钮)。"), 0, 1, 1));
  setLay->addWidget(new CValueControl("CarrotScreenRecAutoClean", tr("自动清理录像"),
    tr("滑动设置: 0=关闭, 1=开启。开启后: 超过保留天数的录像, 或设备存储剩余空间低于阈值时, 自动删除最旧的录像(仅删视频, 不影响行车日志)。"), 0, 1, 1));
  setLay->addWidget(new CValueControl("CarrotScreenRecMaxDays", tr("保留天数(天)"),
    tr("自动清理时, 超过该天数的录像将被删除。调大=保留更久。"), 1, 90, 1));
  setLay->addWidget(new CValueControl("CarrotScreenRecMinFreeGB", tr("最小剩余空间(GB)"),
    tr("设备存储剩余空间低于该值时, 从最旧录像开始删除, 直到空间足够。调大=更激进清理。"), 1, 20, 1));
  setCard->setVisible(false);
  contentLay->addWidget(setCard);

  // ===== 列表(直接放进内容区, 由外层 ScrollView 统一滚动) =====
  listWidget = new QWidget();
  listWidget->setStyleSheet("background:transparent;");
  listLayout = new QVBoxLayout(listWidget);
  listLayout->setContentsMargins(0, 0, 0, 0);
  listLayout->setSpacing(12);
  contentLay->addWidget(listWidget, 1);

  // 内嵌进设置后由 settings 外层 ScrollView 统一滚动, 这里不再嵌套 ScrollView(避免双重滚动)
  main->addWidget(content, 1);

  refreshList();
}

void ScreenRecordManager::showEvent(QShowEvent *event) {
  QWidget::showEvent(event);
  runAutoClean();   // 进入页面时跑一次自动清理检查
  refreshList();
}

void ScreenRecordManager::toggleSettings() {
  if (!setCard) return;
  bool show = !setCard->isVisible();
  setCard->setVisible(show);
  setBtn->setText(show ? tr("收起录像设置") : tr("录像设置"));
}

void ScreenRecordManager::refreshList() {
  // 清空(末尾 stretch 由后续重新加)
  QLayoutItem *child;
  while ((child = listLayout->takeAt(0)) != nullptr) {
    if (child->widget()) delete child->widget();
    delete child;
  }
  checkboxes.clear();
  clipNames.clear();

  QDir dir(REC_DIR);
  // 最新优先(按修改时间倒序)
  QStringList files = dir.entryList(QStringList() << "*.mp4", QDir::Files, QDir::Time);
  qint64 totalBytes = 0;

  if (files.isEmpty()) {
    QLabel *empty = new QLabel(tr("暂无录像文件\n\n在行车界面右下角点录像按钮即可录制"), listWidget);
    empty->setAlignment(Qt::AlignCenter);
    empty->setStyleSheet("font-size: 34px; color:#7A7A7A; padding:70px; background:transparent;");
    listLayout->addWidget(empty);
  } else {
    for (const QString &f : files) {
      QFileInfo fi(dir.filePath(f));
      totalBytes += fi.size();
      QString sizeStr = fmtSize(fi.size());
      QString dateStr = fi.lastModified().toString("yyyy-MM-dd  hh:mm");

      RecRow *row = new RecRow(listWidget);
      QHBoxLayout *rl = new QHBoxLayout(row);
      rl->setContentsMargins(24, 12, 20, 12);
      rl->setSpacing(20);

      QCheckBox *cb = new QCheckBox(row);
      rl->addWidget(cb);

      QVBoxLayout *info = new QVBoxLayout();
      info->setSpacing(6);
      QLabel *name = new QLabel(f, row);
      name->setStyleSheet("font-size: 33px; color:#FFFFFF; font-weight:600; background:transparent;");
      QLabel *meta = new QLabel(dateStr + "   ·   " + sizeStr, row);
      meta->setStyleSheet("font-size: 24px; color:#A8A8A8; background:transparent;");
      info->addWidget(name);
      info->addWidget(meta);
      rl->addLayout(info, 1);

      QPushButton *dl = new QPushButton(tr("下载"), row);
      dl->setObjectName("primary");
      dl->setFixedWidth(170);
      dl->setFixedHeight(72);
      dl->setCursor(Qt::PointingHandCursor);
      rl->addWidget(dl);

      listLayout->addWidget(row);

      row->bindCheckBox(cb);
      checkboxes.append(cb);
      clipNames.append(f);
      QObject::connect(dl, &QPushButton::clicked, [=]() { downloadClip(f); });
      QObject::connect(cb, &QCheckBox::toggled, this, &ScreenRecordManager::updateSelectAllBtnText);
    }
  }
  listLayout->addStretch(1);

  // 统计信息
  if (statLabel) {
    QStorageInfo si("/data/media/0");
    qint64 diskTotal = si.bytesTotal();
    qint64 freeBytes = si.bytesFree();
    qint64 usedBytes = diskTotal - freeBytes;
    int usedPct = diskTotal > 0 ? qRound((double)usedBytes / diskTotal * 100.0) : 0;
    statLabel->setText(tr("共 %1 个录像 · 占用 %2 · 存储 已用 %3 / 剩余 %4 / 共 %5 (%6%) · 目录 %7")
                       .arg(files.size()).arg(fmtSize(totalBytes)).arg(fmtSize(usedBytes))
                       .arg(fmtSize(freeBytes)).arg(fmtSize(diskTotal)).arg(usedPct).arg(REC_DIR));
  }

  allSelected = false;
  updateSelectAllBtnText();
}

void ScreenRecordManager::updateSelectAllBtnText() {
  if (updatingAll) return;   // 批量全选/取消中: 不参与, 否则会把 allSelected 改成中间态
  if (!selAllBtn) return;
  bool any = !checkboxes.isEmpty();
  bool allChecked = any;
  for (auto cb : checkboxes) { if (!cb->isChecked()) { allChecked = false; break; } }
  allSelected = allChecked;
  selAllBtn->setText(allSelected ? tr("取消全选") : tr("全选"));
}

void ScreenRecordManager::toggleSelectAll() {
  if (checkboxes.isEmpty()) return;
  const bool target = !allSelected;   // 先定目标态, 循环内不再读成员变量(防被回调改写)
  updatingAll = true;
  for (auto cb : checkboxes) {
    cb->setChecked(target);           // 信号照常发出, 保证每行底色/描边同步刷新
  }
  updatingAll = false;
  allSelected = target;
  if (selAllBtn) selAllBtn->setText(target ? tr("取消全选") : tr("全选"));
}

QStringList ScreenRecordManager::selectedClips() {
  QStringList sel;
  for (int i = 0; i < checkboxes.size(); i++) {
    if (checkboxes[i]->isChecked()) sel << clipNames[i];
  }
  return sel;
}

void ScreenRecordManager::deleteSelected() {
  QStringList sel = selectedClips();
  if (sel.isEmpty()) {
    ConfirmationDialog::alert(tr("请先勾选要删除的录像(点整行即可勾选)"), this);
    return;
  }
  if (!ConfirmationDialog::confirm(tr("确认删除选中的 %1 个录像? 删除后不可恢复。").arg(sel.size()), tr("删除"), this)) {
    return;
  }
  QDir dir(REC_DIR);
  for (const QString &f : sel) QFile::remove(dir.filePath(f));
  refreshList();
}

QString ScreenRecordManager::lanIP() {
  for (const auto &iface : QNetworkInterface::allInterfaces()) {
    if (iface.flags().testFlag(QNetworkInterface::IsLoopBack)) continue;
    if (!iface.flags().testFlag(QNetworkInterface::IsUp)) continue;
    for (const auto &entry : iface.addressEntries()) {
      if (entry.ip().protocol() == QAbstractSocket::IPv4Protocol) {
        return entry.ip().toString();
      }
    }
  }
  return "";
}

void ScreenRecordManager::showQR(const QString &fileOnDisk, const QString &caption) {
  QString ip = lanIP();
  if (ip.isEmpty()) {
    ConfirmationDialog::alert(tr("设备未接入网络, 无法生成下载地址。\n请让设备与手机连接同一 WiFi, 或手机连设备热点。"), this);
    return;
  }
  QString url = QString("http://%1:8082/screenrecords/download/%2").arg(ip).arg(fileOnDisk);
  DownloadQRPopup popup(url, caption, this);
  popup.exec();
}

void ScreenRecordManager::downloadClip(const QString &filename) {
  showQR(filename, filename);
}

// 多选打包下载: 选中多个则打包成 zip(不压缩, 速度=拷贝), 单个则直接下载原文件
void ScreenRecordManager::downloadSelected() {
  QStringList sel = selectedClips();
  if (sel.isEmpty()) {
    ConfirmationDialog::alert(tr("请先勾选要下载的录像(点整行即可勾选, 可多选)"), this);
    return;
  }
  if (sel.size() == 1) {
    downloadClip(sel.first());
    return;
  }

  QDir dir(REC_DIR);
  // 清掉上次的包, 避免占用存储堆积
  for (const QString &old : dir.entryList(QStringList() << "bundle_*.zip" << "bundle_*.tar", QDir::Files)) {
    QFile::remove(dir.filePath(old));
  }

  QString ts = QDateTime::currentDateTime().toString("yyyyMMdd-hhmmss");
  QString zipName = QString("bundle_%1_%2clips.zip").arg(ts).arg(sel.size());
  QString zipPath = dir.filePath(zipName);

  bundleBtn->setText(tr("打包中..."));
  bundleBtn->setEnabled(false);
  QApplication::processEvents();

  // 设备无 zip 命令, 用 python zipfile(ZIP_STORED 不压缩, mp4 本就压不动, 速度≈拷贝)
  QStringList args;
  args << "-c"
       << "import sys,zipfile\n"
          "z=zipfile.ZipFile(sys.argv[1],'w',zipfile.ZIP_STORED,allowZip64=True)\n"
          "[z.write(f,f.split('/')[-1]) for f in sys.argv[2:]]\n"
          "z.close()\n";
  args << zipPath;
  for (const QString &f : sel) args << dir.filePath(f);
  int rc = QProcess::execute(DEV_PYTHON, args);

  QString outName = zipName;
  if (rc != 0 || !QFile::exists(zipPath)) {
    // 兜底: python 不可用时用 tar
    QFile::remove(zipPath);
    QString tarName = QString("bundle_%1_%2clips.tar").arg(ts).arg(sel.size());
    QStringList targs;
    targs << "-cf" << dir.filePath(tarName) << "-C" << REC_DIR;
    for (const QString &f : sel) targs << f;
    rc = QProcess::execute("tar", targs);
    outName = tarName;
  }

  bundleBtn->setText(tr("打包下载"));
  bundleBtn->setEnabled(true);

  if (rc != 0 || !QFile::exists(dir.filePath(outName))) {
    ConfirmationDialog::alert(tr("打包失败, 请改为逐个下载。"), this);
    return;
  }

  qint64 sz = QFileInfo(dir.filePath(outName)).size();
  showQR(outName, tr("%1 个录像已打包 (%2)\n%3").arg(sel.size()).arg(fmtSize(sz)).arg(outName));
}

void ScreenRecordManager::runAutoClean() {
  Params params;
  if (!params.getBool("CarrotScreenRecAutoClean")) return;
  int maxDays = atoi(params.get("CarrotScreenRecMaxDays").c_str());
  if (maxDays <= 0) maxDays = 7;
  int minFreeGB = atoi(params.get("CarrotScreenRecMinFreeGB").c_str());
  if (minFreeGB <= 0) minFreeGB = 2;

  QDir dir(REC_DIR);
  QStringList files = dir.entryList(QStringList() << "*.mp4", QDir::Files, QDir::Name);

  // 条件1: 超过保留天数(文件名格式 YYYYMMDD-HHMMSS.mp4, 见 screenrecorder.cc:118)
  QDate today = QDate::currentDate();
  for (const QString &f : files) {
    QDate d = QDate::fromString(f.left(8), "yyyyMMdd");
    if (d.isValid() && d.daysTo(today) > maxDays) {
      QFile::remove(dir.filePath(f));
    }
  }

  // 重新读取(条件1 可能已删)
  files = dir.entryList(QStringList() << "*.mp4", QDir::Files, QDir::Name);

  // 条件2: 剩余空间不足, 从最旧删起
  qlonglong needBytes = (qlonglong)minFreeGB * 1024LL * 1024LL * 1024LL;
  while (!files.isEmpty()) {
    if (QStorageInfo("/data/media/0").bytesFree() >= needBytes) break;
    QFile::remove(dir.filePath(files.first()));
    files.removeFirst();
  }
}

DownloadQRPopup::DownloadQRPopup(const QString &url, const QString &filename, QWidget *parent) : DialogBase(parent) {
  setStyleSheet("DownloadQRPopup { background-color:#FFFFFF; } DownloadQRPopup QLabel { color:#000000; }");
  QVBoxLayout *main = new QVBoxLayout(this);
  main->setContentsMargins(60, 36, 60, 36);
  main->setSpacing(14);

  QLabel *title = new QLabel(tr("扫码下载"), this);
  title->setStyleSheet("font-size:42px; color:#000000; font-weight:700;");
  title->setAlignment(Qt::AlignCenter);
  main->addWidget(title);

  QLabel *fn = new QLabel(filename, this);
  fn->setStyleSheet("font-size:26px; color:#333333;");
  fn->setAlignment(Qt::AlignCenter);
  fn->setWordWrap(true);
  main->addWidget(fn);

  QLabel *qr = new QLabel(this);
  qr->setPixmap(makeQrPixmap(url));
  qr->setAlignment(Qt::AlignCenter);
  main->addWidget(qr, 1);

  QLabel *hint = new QLabel(tr("手机与设备连同一网络, 扫码即下载\n") + url, this);
  hint->setStyleSheet("font-size:20px; color:#555555;");
  hint->setAlignment(Qt::AlignCenter);
  hint->setWordWrap(true);
  main->addWidget(hint);

  QPushButton *closeBtn = new QPushButton(tr("关闭"), this);
  closeBtn->setStyleSheet("color:#000000; background-color:#E0E0E0; border-radius:20px; font-size:30px; padding:12px 60px;");
  QObject::connect(closeBtn, &QPushButton::clicked, this, &QDialog::reject);
  main->addWidget(closeBtn, 0, Qt::AlignCenter);
}
