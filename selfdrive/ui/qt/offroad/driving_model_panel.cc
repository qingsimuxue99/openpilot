#include "selfdrive/ui/qt/offroad/driving_model_panel.h"

#include <cstdio>
#include <string>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QLabel>
#include <QPushButton>
#include <QScrollArea>
#include <QScrollBar>
#include <QTouchEvent>
#include <QEvent>
#include "selfdrive/ui/qt/widgets/input.h"

// 执行模型管理器命令, 返回 stdout
static QString run_mgr(const QString &args) {
  std::string cmd = "cd /data/openpilot && /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py " + args.toStdString() + " 2>&1";
  FILE *fp = popen(cmd.c_str(), "r");
  std::string out;
  if (fp) { char buf[1024]; while (fgets(buf, sizeof(buf), fp)) out += buf; pclose(fp); }
  return QString::fromStdString(out);
}

// ===== 1:1 跟手滚动区 (参考自学习记录框架): 直接驱动滚动条, 零惯性、无 QScroller;
// 关键: 在 QScrollArea::viewportEvent 内处理触摸(而非外部 installEventFilter 吞事件), c3 eglfs 下不崩。
// 仅对落在"非交互控件"区域的触摸做 1:1 平移; 落在按钮(QAbstractButton)上的触摸放行给子控件(可正常点按)。
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
    if (viewport()) viewport()->setStyleSheet("background-color:transparent;");
    content->setStyleSheet("background-color:transparent;");
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

  // 模型列表: SP 在前 / CP 在后, 同类型按名称排序; 当前激活项高亮 + ✓ 标记
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

  // 内容容器: 按钮行 (参考自学习记录框架, 每行是 QPushButton, 触摸点按正常 + 空白处滑动跟手)
  QWidget *content = new QWidget(this);
  content->setObjectName("model_list_content");
  content->setStyleSheet("QWidget#model_list_content { background-color:transparent; }");
  QVBoxLayout *vbox = new QVBoxLayout(content);
  vbox->setContentsMargins(0, 0, 0, 0);
  vbox->setSpacing(6);

  for (const auto &r : rows) {
    auto *btn = new QPushButton(r.text, content);
    btn->setCursor(Qt::PointingHandCursor);
    btn->setStyleSheet(QString(
      "QPushButton { text-align:left; padding: 14px 16px; margin: 2px 0px; border-radius: 12px;"
      " background-color: %1; color: #FFFFFF; font-size: 38px; border: none; }"
      "QPushButton:pressed { background-color: #1F1FD0; }"
    ).arg(r.active ? "#2C2CE2" : "#2B2B2B"));
    QObject::connect(btn, &QPushButton::clicked, this, [this, r]() {
      if (ConfirmationDialog::confirm(tr("切换到 %1?\n将重启设备 (约 1-2 分钟, 完整加载新模型), 建议停车时操作").arg(r.text), tr("CONFIRM"), this)) {
        ConfirmationDialog::alert(run_mgr(QString("switch %1").arg(r.idx)), this);
        refresh();
      }
    });
    vbox->addWidget(btn);
  }
  vbox->addStretch();

  // 1:1 跟手滚动 (参考自学习记录框架, 不用 ScrollView)
  FollowScrollArea *sv = new FollowScrollArea(content, this);
  layout_->addWidget(sv, 1);
}
