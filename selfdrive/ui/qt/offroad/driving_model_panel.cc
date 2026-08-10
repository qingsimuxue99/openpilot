#include "selfdrive/ui/qt/offroad/driving_model_panel.h"

#include <cstdio>
#include <string>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QLabel>
#include <QListWidget>
#include <QListWidgetItem>
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

DrivingModelPanel::DrivingModelPanel(QWidget *parent) : QWidget(parent) {
  layout_ = new QVBoxLayout(this);
  layout_->setMargin(0);
  refresh();
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

  QString list_json = run_mgr("list --json");
  QJsonObject lo = QJsonDocument::fromJson(list_json.toUtf8()).object();
  QJsonArray models = lo.value("models").toArray();

  // 标题
  auto *title = new QLabel(tr("模型列表 (%1 个)").arg(models.size()), this);
  title->setStyleSheet("font-size: 42px; color: #FFFFFF; font-weight: 600; margin: 12px 8px;");
  layout_->addWidget(title);

  if (models.isEmpty()) {
    auto *empty = new QLabel(tr("暂无本地模型"), this);
    empty->setStyleSheet("font-size: 38px; color: #9A9A9A;");
    layout_->addWidget(empty);
    return;
  }

  // 模型列表: SP 在前 / CP 在后, 同类型按名称排序; 当前激活项带 ✓ 标记
  QListWidget *list = new QListWidget(this);
  list->setStyleSheet(R"(
    QListWidget { background: transparent; border: none; outline: none; }
    QListWidget::item { padding: 12px 16px; margin: 4px 0px; border-radius: 12px;
                        background-color: #2B2B2B; color: #FFFFFF; font-size: 38px; }
    QListWidget::item:selected { background-color: #2C2CE2; }
  )");
  list->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

  struct Row { QString text; int idx; bool active; };
  QList<Row> rows;
  QMap<QString,int> idx_map;  // text -> idx (去重防同名)
  for (const auto &v : models) {
    QJsonObject m = v.toObject();
    int idx = m.value("idx").toInt();
    QString name = m.value("name").toString().toUpper();
    QString type = m.value("type").toString();
    QString size = m.value("size_str").toString();
    bool active = m.value("active").toBool();
    QString prefix = (type == "SP") ? "SP: " : "CP: ";
    QString text = QString("%1%2 (%3)%4").arg(prefix).arg(name).arg(size).arg(active ? tr("  ✓") : "");
    if (idx_map.contains(text)) continue;  // 同名同型去重
    idx_map[text] = idx;
    rows.append({text, idx, active});
  }
  // 排序: SP 组在前, 组内按名称; 当前激活项提到组内最前
  std::stable_sort(rows.begin(), rows.end(), [](const Row &a, const Row &b) {
    bool a_sp = a.text.startsWith("SP:");
    bool b_sp = b.text.startsWith("SP:");
    if (a_sp != b_sp) return a_sp;
    if (a.active != b.active) return a.active;
    return a.text < b.text;
  });

  for (const auto &r : rows) {
    auto *item = new QListWidgetItem(r.text);
    item->setData(Qt::UserRole, r.idx);
    list->addItem(item);
  }
  list->setMinimumHeight(qMin(rows.size() * 72 + 8, 720));

  QObject::connect(list, &QListWidget::itemClicked, this, [this](QListWidgetItem *item) {
    int idx = item->data(Qt::UserRole).toInt();
    if (idx < 0) return;
    if (ConfirmationDialog::confirm(tr("切换到 %1?\n将重启设备 (约 1-2 分钟, 完整加载新模型), 建议停车时操作").arg(item->text()), tr("CONFIRM"), this)) {
      ConfirmationDialog::alert(run_mgr(QString("switch %1").arg(idx)), this);
      refresh();
    }
  });

  layout_->addWidget(new ScrollView(list, this));
}
