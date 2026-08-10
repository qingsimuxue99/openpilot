#include "selfdrive/ui/qt/offroad/driving_model_panel.h"

#include <cstdio>
#include <string>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QFile>
#include <QFrame>
#include <QLabel>
#include <QPushButton>
#include <QTimer>
#include "selfdrive/ui/qt/widgets/input.h"

// 执行模型管理器命令, 返回 stdout
static QString run_mgr(const QString &args) {
  std::string cmd = "cd /data/openpilot && /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py " + args.toStdString() + " 2>&1";
  FILE *fp = popen(cmd.c_str(), "r");
  std::string out;
  if (fp) { char buf[1024]; while (fgets(buf, sizeof(buf), fp)) out += buf; pclose(fp); }
  return QString::fromStdString(out);
}

// 蓝色大按钮样式 (与设置页「选择您的车辆」同款)
static const char *BLUE_BTN_STYLE = R"(
  QPushButton {
    margin-top: 20px; margin-bottom: 20px; padding: 10px; height: 120px; border-radius: 15px;
    color: #FFFFFF; background-color: #2C2CE2;
  }
  QPushButton:pressed {
    background-color: #2424FF;
  }
)";

DrivingModelPanel::DrivingModelPanel(QWidget *parent) : QWidget(parent) {
  layout_ = new QVBoxLayout(this);
  layout_->setMargin(0);
  refresh();
  // 每 2 秒刷新下载进度行 (只改文字, 不重建面板)
  timer_ = new QTimer(this);
  timer_->setInterval(2000);
  QObject::connect(timer_, &QTimer::timeout, this, [this]() {
    if (!progress_ctl_) return;
    QString progress;
    QFile pf("/tmp/model_dl_progress");
    if (pf.open(QIODevice::ReadOnly)) {
      progress = QString::fromUtf8(pf.readAll()).trimmed();
      pf.close();
    }
    progress_ctl_->setText(progress.isEmpty() ? tr("无进行中任务") : progress);
  });
  timer_->start();
}

void DrivingModelPanel::showEvent(QShowEvent *event) {
  QWidget::showEvent(event);
  refresh();
}

void DrivingModelPanel::refresh() {
  // 清空并重建
  while (QLayoutItem *item = layout_->takeAt(0)) {
    if (QWidget *w = item->widget()) { delete w; }
    delete item;
  }
  progress_ctl_ = nullptr;  // 重建后由下方重新赋值

  QString list_json = run_mgr("list --json");
  QString man_json = run_mgr("manifest --json");
  QString progress;
  QFile pf("/tmp/model_dl_progress");
  if (pf.open(QIODevice::ReadOnly)) {
    progress = QString::fromUtf8(pf.readAll()).trimmed();
    pf.close();
  }

  QJsonObject lo = QJsonDocument::fromJson(list_json.toUtf8()).object();
  QString total = lo.value("total").toString();
  QString free = lo.value("free").toString();
  QJsonArray models = lo.value("models").toArray();

  QJsonObject mo = QJsonDocument::fromJson(man_json.toUtf8()).object();
  QJsonArray bundles = mo.value("bundles").toArray();

  // 本地模型弹窗条目: 名字大写 + 去编号前缀 + ABC 字母排序; QMap 记录文本->idx (选中后查表)
  QStringList local_items;
  QMap<QString,int> local_idx;
  QString cur_item, cur_name = tr("默认模型");
  for (const auto &v : models) {
    QJsonObject m = v.toObject();
    int idx = m.value("idx").toInt();
    QString name = m.value("name").toString().toUpper();
    QString type = m.value("type").toString();
    QString size = m.value("size_str").toString();
    bool active = m.value("active").toBool();
    QString item = QString("%1 (%2) %3").arg(name).arg(type).arg(size);
    local_items << item;
    local_idx[item] = idx;
    if (active) { cur_item = item; cur_name = name; }
  }
  local_items.sort(Qt::CaseInsensitive);

  // ===== 当前模型: 蓝色大按钮 + 弹窗选择 (参考「选择车型」框架) =====
  QPushButton *cur_btn = new QPushButton(tr("当前模型: %1").arg(cur_name));
  cur_btn->setObjectName("selectModelBtn");
  cur_btn->setStyleSheet(BLUE_BTN_STYLE);
  QObject::connect(cur_btn, &QPushButton::clicked, this, [this, local_items, local_idx, cur_item]() {
    if (local_items.isEmpty()) {
      ConfirmationDialog::alert(tr("暂无本地模型, 请先到下方下载"), this);
      return;
    }
    QString sel = MultiOptionDialog::getSelection(tr("选择模型 (当前项已选中)"), local_items, cur_item, this);
    if (sel.isEmpty() || sel == cur_item) return;
    int idx = local_idx.value(sel, -1);
    if (idx < 0) return;
    if (ConfirmationDialog::confirm(tr("切换到 %1?\n将重启设备 (约 1-2 分钟, 完整加载新模型), 建议停车时操作").arg(sel), tr("CONFIRM"), this)) {
      ConfirmationDialog::alert(run_mgr(QString("switch %1").arg(idx)), this);
      refresh();
    }
  });
  layout_->addWidget(cur_btn);

  // ===== 在线模型: 蓝色大按钮 + 弹窗选择 (最新在前, 已安装的过滤掉) =====
  QStringList online_items;
  int installed_cnt = 0;
  for (const auto &v : bundles) {
    QJsonObject b = v.toObject();
    QString sn = b.value("short_name").toString();
    QString dn = b.value("display_name").toString();
    QString gen = b.value("gen").toString();
    bool installed = b.value("installed").toBool();
    if (installed) { installed_cnt++; continue; }  // 已安装的不再显示, 防止重复下载
    online_items << QString("%1  gen%2  %3").arg(sn).arg(gen).arg(dn);
  }
  QPushButton *dl_btn = new QPushButton(tr("下载在线模型 (最新在前)"));
  dl_btn->setObjectName("downloadModelBtn");
  dl_btn->setStyleSheet(BLUE_BTN_STYLE);
  QObject::connect(dl_btn, &QPushButton::clicked, this, [this, online_items, installed_cnt]() {
    if (online_items.isEmpty()) {
      ConfirmationDialog::alert(installed_cnt > 0
        ? tr("全部 %1 个在线模型均已安装, 无需下载").arg(installed_cnt)
        : tr("在线模型列表为空, 请稍后重试"), this);
      return;
    }
    QString sel = MultiOptionDialog::getSelection(tr("在线模型 (最新在前)"), online_items, "", this);
    if (sel.isEmpty()) return;
    QString sn = sel.section(' ', 0, 0);
    if (ConfirmationDialog::confirm(tr("下载 %1?\n约几十到几百 MB, 后台下载").arg(sel), tr("CONFIRM"), this)) {
      std::string cmd = "cd /data/openpilot && nohup /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py download " + sn.toStdString() + " > /tmp/model_dl.log 2>&1 &";
      system(cmd.c_str());
      ConfirmationDialog::alert(tr("已开始后台下载 %1, 完成后自动安装, 下方[下载进度]实时更新").arg(sn), this);
      refresh();
    }
  });
  layout_->addWidget(dl_btn);

  // ===== 删除模型: 蓝色大按钮 + 弹窗选择 (当前激活项不可删, 同上 ABC 排序) =====
  QStringList del_items;
  QMap<QString,int> del_idx;
  for (const auto &v : models) {
    QJsonObject m = v.toObject();
    int idx = m.value("idx").toInt();
    QString name = m.value("name").toString().toUpper();
    QString type = m.value("type").toString();
    QString size = m.value("size_str").toString();
    bool active = m.value("active").toBool();
    if (active) continue;  // 当前激活模型不可删除 (manager 也会拦截)
    QString item = QString("%1 (%2) %3").arg(name).arg(type).arg(size);
    del_items << item;
    del_idx[item] = idx;
  }
  del_items.sort(Qt::CaseInsensitive);
  QPushButton *del_btn = new QPushButton(tr("删除模型"));
  del_btn->setObjectName("deleteModelBtn");
  del_btn->setStyleSheet(BLUE_BTN_STYLE);
  QObject::connect(del_btn, &QPushButton::clicked, this, [this, del_items, del_idx]() {
    if (del_items.isEmpty()) {
      ConfirmationDialog::alert(tr("没有可删除的模型 (当前激活模型不可删除)"), this);
      return;
    }
    QString sel = MultiOptionDialog::getSelection(tr("选择要删除的模型 (当前激活项不可删)"), del_items, "", this);
    if (sel.isEmpty()) return;
    int idx = del_idx.value(sel, -1);
    if (idx < 0) return;
    if (ConfirmationDialog::confirm(tr("删除 %1?\n该操作不可恢复!").arg(sel), tr("CONFIRM"), this)) {
      ConfirmationDialog::alert(run_mgr(QString("delete %1").arg(idx)), this);
      refresh();
    }
  });
  layout_->addWidget(del_btn);

  // ===== 信息卡: 与按钮/弹窗同风格 (深色圆角容器, 紧凑三行) =====
  auto *info_card = new QFrame(this);
  info_card->setStyleSheet("QFrame { background-color: #1B1B1B; border-radius: 15px; }");
  auto *il = new QVBoxLayout(info_card);
  il->setContentsMargins(30, 22, 30, 22);
  il->setSpacing(14);
  auto add_row = [&](const QString &t, const QString &v) -> QLabel* {
    auto *row = new QHBoxLayout;
    auto *lt = new QLabel(t, info_card);
    lt->setStyleSheet("font-size: 42px; color: #9A9A9A;");
    auto *lv = new QLabel(v, info_card);
    lv->setStyleSheet("font-size: 42px; color: #FFFFFF; font-weight: 500;");
    lv->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    row->addWidget(lt);
    row->addStretch(1);
    row->addWidget(lv);
    il->addLayout(row);
    return lv;
  };
  add_row(tr("本地模型"), QString::number(models.size()) + tr(" 个"));
  add_row(tr("磁盘占用"), total + tr(" / 剩余 ") + free);
  progress_ctl_ = add_row(tr("下载进度"), progress.isEmpty() ? tr("无进行中任务") : progress);
  layout_->addWidget(info_card);
}
