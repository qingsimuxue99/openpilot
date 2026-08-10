#include "selfdrive/ui/qt/offroad/driving_model_panel.h"

#include <cstdio>
#include <string>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QSet>
#include <QLabel>
#include <QPushButton>
#include <QButtonGroup>
#include <QHBoxLayout>
#include <QTimer>
#include <QScrollBar>
#include <QRegularExpression>
#include "common/util.h"
#include "selfdrive/ui/qt/widgets/input.h"
#include "selfdrive/ui/qt/widgets/scrollview.h"

// 执行模型管理器命令, 返回 stdout
static QString run_mgr(const QString &args) {
  std::string cmd = "cd /data/openpilot && /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py " + args.toStdString() + " 2>&1";
  FILE *fp = popen(cmd.c_str(), "r");
  std::string out;
  if (fp) { char buf[1024]; while (fgets(buf, sizeof(buf), fp)) out += buf; pclose(fp); }
  return QString::fromStdString(out);
}

// 后台启动下载 (nohup, 不阻塞 UI); 进度写入 /tmp/model_dl_progress
static void startDownload(const QString &name) {
  std::string cmd = "cd /data/openpilot && nohup /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py download "
                  + name.toStdString() + " >> /tmp/sp_dl.log 2>&1 &";
  system(cmd.c_str());
}

// 仅这 5 个验证过的 SP 模型可下载
static const QStringList SP_DL_LIST = {"wmiv12", "tr16", "ltr14", "wmiv9", "tr15"};
static QString g_downloading;   // 当前后台下载中的模型名 (小写), 空=空闲

// ===== 构建弹窗列表: 本地模型(点击切换) + 5 个 SP 可下载(点击下载, 行内进度) =====
static void buildModelList(QWidget *listWidget, QWidget *top) {
  // 清空旧内容
  QLayout *old = listWidget->layout();
  if (old) {
    QLayoutItem *it;
    while ((it = old->takeAt(0)) != nullptr) {
      QWidget *w = it->widget();
      delete it;
      if (w) w->deleteLater();
    }
    delete old;
  }
  QVBoxLayout *listLayout = new QVBoxLayout(listWidget);
  listLayout->setContentsMargins(0, 0, 0, 0);
  listLayout->setSpacing(10);
  listWidget->setStyleSheet(R"(
    QPushButton {
      height: 88px; padding: 0px 20px; text-align: left;
      font-size: 36px; font-weight: 400;
      border-radius: 12px; border: none;
      background-color: #2B2B2B; color: #FFFFFF;
    }
    QPushButton:checked { background-color: #2C2CE2; }
    QPushButton:pressed { background-color: #1F1FD0; }
  )");

  // ---- 本地模型 ----
  QString list_json = run_mgr("list --json");
  QJsonObject lo = QJsonDocument::fromJson(list_json.toUtf8()).object();
  QJsonArray models = lo.value("models").toArray();

  struct Row { QString text; int idx; bool active; };
  QList<Row> rows;
  QMap<QString,int> idx_map;
  QSet<QString> localLower;   // 本地模型名 (小写)
  for (const auto &v : models) {
    QJsonObject m = v.toObject();
    int idx = m.value("idx").toInt();
    QString name = m.value("name").toString().toUpper();
    QString type = m.value("type").toString();
    QString size = m.value("size_str").toString();
    bool active = m.value("active").toBool();
    localLower.insert(name.toLower());
    QString prefix = (type == "SP") ? "SP: " : "CP: ";
    QString text = QString("%1%2 (%3)%4").arg(prefix).arg(name).arg(size).arg(active ? QObject::tr("  ✓") : "");
    if (idx_map.contains(text)) continue;
    idx_map[text] = idx;
    rows.append({text, idx, active});
  }
  std::stable_sort(rows.begin(), rows.end(), [](const Row &a, const Row &b) {
    bool a_sp = a.text.startsWith("SP:");
    bool b_sp = b.text.startsWith("SP:");
    if (a_sp != b_sp) return a_sp;
    if (a.active != b.active) return a.active;
    return a.text < b.text;
  });

  QButtonGroup *group = new QButtonGroup(listWidget);
  group->setExclusive(true);
  int current_idx = -1;

  for (const auto &r : rows) {
    QPushButton *btn = new QPushButton(r.text, listWidget);
    btn->setCheckable(true);
    btn->setChecked(r.active);
    if (r.active) current_idx = r.idx;
    QObject::connect(btn, &QPushButton::clicked, [=](bool checked) {
      if (checked && r.idx != current_idx) {
        if (ConfirmationDialog::confirm(QObject::tr("切换到 %1?\n将重启设备 (约 1-2 分钟, 完整加载新模型), 建议停车时操作").arg(r.text), QObject::tr("CONFIRM"), top)) {
          ConfirmationDialog::alert(run_mgr(QString("switch %1").arg(r.idx)), top);
        } else {
          btn->setChecked(false);
          group->buttons().at(0)->setChecked(true);
        }
      }
    });
    group->addButton(btn);
    listLayout->addWidget(btn);
  }

  // ---- 5 个验证过的 SP 模型: 未下载的可点击下载 (行内实时进度) ----
  QLabel *sep = new QLabel(QObject::tr("—— SP 模型下载 (点击下载) ——"), listWidget);
  sep->setAlignment(Qt::AlignCenter);
  sep->setStyleSheet("font-size:28px; color:#8E8E93; margin:12px 0 4px 0;");
  listLayout->addWidget(sep);

  bool anyToDl = false;
  for (const QString &sn : SP_DL_LIST) {
    if (localLower.contains(sn)) continue;  // 已下载跳过
    anyToDl = true;
    QString disp = sn.toUpper();
    QPushButton *dlBtn = new QPushButton(QString("SP: %1  ⬇ 下载").arg(disp), listWidget);
    dlBtn->setStyleSheet(R"(
      QPushButton {
        height: 88px; padding: 0px 20px; text-align: left;
        font-size: 36px; font-weight: 400;
        border-radius: 12px; border: none;
        background-color: #3D3D5C; color: #FFFFFF;
      }
      QPushButton:disabled { background-color: #2A2A3C; color: #888899; }
      QPushButton:pressed { background-color: #2C2CE2; }
    )");
    if (!g_downloading.isEmpty()) dlBtn->setEnabled(false);  // 已有下载进行中
    QObject::connect(dlBtn, &QPushButton::clicked, [=]() {
      if (!g_downloading.isEmpty()) return;
      if (ConfirmationDialog::confirm(QObject::tr("下载 SP 模型 %1?\n(后台下载, 行内显示进度, 完成后自动更新)").arg(disp), QObject::tr("CONFIRM"), top)) {
        g_downloading = sn;
        startDownload(sn);
        dlBtn->setEnabled(false);
        dlBtn->setText(QString("SP: %1  ⬇ 下载中 0%").arg(disp));
        // 轮询进度: 1.5s 一次, 读 /tmp/model_dl_progress; 完成(本地列表出现)后自动重建
        QTimer *t = new QTimer(listWidget);
        t->setInterval(1500);
        QObject::connect(t, &QTimer::timeout, listWidget, [=]() {
          if (g_downloading != sn) { t->stop(); t->deleteLater(); return; }
          std::string prog = util::read_file("/tmp/model_dl_progress");
          QRegularExpression re("(\\d+)\\s*%");
          QRegularExpressionMatch m = re.match(QString::fromStdString(prog));
          if (m.hasMatch()) {
            int pct = m.captured(1).toInt();
            dlBtn->setText(QString("SP: %1  ⬇ 下载中 %2%").arg(disp).arg(pct));
          }
          // 完成检测: 本地模型列表已出现该模型
          QString lj = run_mgr("list --json");
          if (lj.contains(sn, Qt::CaseInsensitive)) {
            g_downloading.clear();
            t->stop(); t->deleteLater();
            buildModelList(listWidget, top);   // 自动刷新 (该模型进入已安装区)
          }
        });
        t->start();
      }
    });
    listLayout->addWidget(dlBtn);
  }
  if (!anyToDl) {
    QLabel *done = new QLabel(QObject::tr("5 个 SP 模型均已下载"), listWidget);
    done->setAlignment(Qt::AlignCenter);
    done->setStyleSheet("font-size:32px; color:#8BD450; margin:8px 0;");
    listLayout->addWidget(done);
  }
  listLayout->addStretch(1);
}

// ===== 全屏 overlay 二级弹窗 (非 QDialog, 避免 c3 竖屏崩溃) =====
void showModelOverlay(QWidget *top) {
  if (!top) return;
  if (QWidget *old = top->findChild<QWidget*>("model_overlay")) delete old;
  QWidget *overlay = new QWidget(top);
  overlay->setObjectName("model_overlay");
  overlay->setGeometry(top->rect());
  overlay->setStyleSheet(R"(
    QWidget#model_overlay { background-color: rgba(0,0,0,0.85); }
    QWidget#model_panel { background-color: #1e1e1e; border-radius: 28px; }
  )");

  QWidget *panel = new QWidget(overlay);
  panel->setObjectName("model_panel");
  int margin = 60;
  panel->setFixedSize(top->width() - 2 * margin, top->height() - 2 * margin);
  panel->move(margin, margin);

  QVBoxLayout *vbox = new QVBoxLayout(panel);
  vbox->setContentsMargins(36, 36, 36, 36);
  vbox->setSpacing(20);

  QLabel *titleLbl = new QLabel(QObject::tr("模型列表"), panel);
  titleLbl->setAlignment(Qt::AlignCenter);
  titleLbl->setStyleSheet("font-size:36px; font-weight:bold; color:white;");
  vbox->addWidget(titleLbl);

  QWidget *listWidget = new QWidget(panel);
  buildModelList(listWidget, top);
  ScrollView *sv = new ScrollView(listWidget, panel);
  sv->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  sv->setStyleSheet("background-color:#141414; border-radius:12px;");
  vbox->addWidget(sv, 1);

  // 底部: 刷新 + 关闭
  QHBoxLayout *btnRow = new QHBoxLayout();
  btnRow->setSpacing(20);
  QPushButton *refreshBtn = new QPushButton(QObject::tr("刷新"), panel);
  refreshBtn->setStyleSheet("height:100px; font-size:36px; background-color:#2f4f7f; color:white; border-radius:18px; border:none;");
  QObject::connect(refreshBtn, &QPushButton::clicked, top, [top]() { showModelOverlay(top); });
  btnRow->addWidget(refreshBtn, 1);
  QPushButton *closeBtn = new QPushButton(QObject::tr("关闭"), panel);
  closeBtn->setStyleSheet("height:100px; font-size:36px; background-color:#393939; color:white; border-radius:18px; border:none;");
  QObject::connect(closeBtn, &QPushButton::clicked, overlay, [overlay, top](bool) {
    overlay->hide();
    if (top) { top->raise(); top->activateWindow(); }
  });
  btnRow->addWidget(closeBtn, 1);
  vbox->addLayout(btnRow);

  overlay->show();
}
