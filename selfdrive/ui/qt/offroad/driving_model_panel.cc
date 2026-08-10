#include "selfdrive/ui/qt/offroad/driving_model_panel.h"

#include <cstdio>
#include <string>
#include <functional>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QMap>
#include <QLabel>
#include <QPushButton>
#include <QHBoxLayout>
#include <QScrollArea>
#include <QScrollBar>
#include <QTouchEvent>
#include <QEvent>
#include <QMouseEvent>
#include <QCoreApplication>
#include "selfdrive/ui/qt/widgets/input.h"

// 执行模型管理器命令, 返回 stdout
static QString run_mgr(const QString &args) {
  std::string cmd = "cd /data/openpilot && /usr/local/venv/bin/python selfdrive/modeld/driving_model_manager.py " + args.toStdString() + " 2>&1";
  FILE *fp = popen(cmd.c_str(), "r");
  std::string out;
  if (fp) { char buf[1024]; while (fgets(buf, sizeof(buf), fp)) out += buf; pclose(fp); }
  return QString::fromStdString(out);
}

// ===== 1:1 跟手滚动区 (二级弹窗内独立使用, 参考自学习记录框架, 增强版) =====
// 整屏可滑: TouchBegin 一律跟踪; 位移 > 12px 判定为滑动并直接驱动滚动条 (跟手、零惯性);
// 未滑动(TouchEnd) 视为点击: 自动把触摸转发为鼠标事件给命中的可点控件 -> 行点击生效。
class FollowScrollArea : public QScrollArea {
  QPoint m_last;
  bool m_tracking = false;
  bool m_dragging = false;
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
        m_last = te->touchPoints().first().pos().toPoint();
        m_tracking = true; m_dragging = false;
        return true;
      }
      case QEvent::TouchUpdate: {
        QTouchEvent *te = static_cast<QTouchEvent*>(ev);
        if (!m_tracking || te->touchPoints().isEmpty()) return QScrollArea::viewportEvent(ev);
        QPoint p = te->touchPoints().first().pos().toPoint();
        int dy = p.y() - m_last.y();
        m_last = p;
        if (!m_dragging) {
          if (qAbs(dy) < 12) return true;   // 阈值内: 点击待定, 继续吞
          m_dragging = true;                 // 超过阈值: 进入滚动
        }
        verticalScrollBar()->setValue(verticalScrollBar()->value() - dy);
        return true;
      }
      case QEvent::TouchEnd: {
        QTouchEvent *te = static_cast<QTouchEvent*>(ev);
        if (m_tracking && !m_dragging && !te->touchPoints().isEmpty()) {
          // 点击: 沿命中控件向上找 clickable 行, 转发鼠标按下/释放
          QPointF gp = te->touchPoints().first().screenPos();
          QWidget *hit = widget() ? widget()->childAt(widget()->mapFromGlobal(gp.toPoint())) : nullptr;
          while (hit && hit != widget() && !hit->property("clickable").toBool()) hit = hit->parentWidget();
          if (hit && hit != widget()) {
            QPoint hp = hit->mapFromGlobal(gp.toPoint());
            QMouseEvent press(QEvent::MouseButtonPress, hp, gp.toPoint(), Qt::LeftButton, Qt::LeftButton, Qt::NoModifier);
            QMouseEvent release(QEvent::MouseButtonRelease, hp, gp.toPoint(), Qt::LeftButton, Qt::NoButton, Qt::NoModifier);
            QCoreApplication::sendEvent(hit, &press);
            QCoreApplication::sendEvent(hit, &release);
          }
        }
        m_tracking = false; m_dragging = false;
        return true;
      }
      case QEvent::TouchCancel:
        m_tracking = false; m_dragging = false;
        return QScrollArea::viewportEvent(ev);
      default:
        return QScrollArea::viewportEvent(ev);
    }
  }
};

// 模型行: 普通 QWidget(非 QAbstractButton) -> 触摸整屏可滑; 点击由 FollowScrollArea 转发 mouse 事件触发
class ModelRow : public QWidget {
public:
  std::function<void(int, const QString&)> on_click;
  ModelRow(const QString &text, int idx, bool active, QWidget *parent = nullptr) : QWidget(parent), idx_(idx), text_(text) {
    setProperty("clickable", true);   // FollowScrollArea 点击转发目标
    setObjectName("model_row");
    setFixedHeight(88);
    setStyleSheet(QString("QWidget#model_row { background-color:%1; border-radius:12px; }")
                  .arg(active ? "#2C2CE2" : "#2B2B2B"));
    QHBoxLayout *h = new QHBoxLayout(this);
    h->setContentsMargins(20, 0, 20, 0);
    QLabel *lbl = new QLabel(text, this);
    lbl->setStyleSheet("color:white; font-size:38px; background:transparent; border:none;");
    h->addWidget(lbl);
  }
protected:
  void mouseReleaseEvent(QMouseEvent *ev) override {
    if (on_click) on_click(idx_, text_);
    QWidget::mouseReleaseEvent(ev);
  }
private:
  int idx_;
  QString text_;
};

DrivingModelPanel::DrivingModelPanel(QWidget *parent) : QWidget(parent) {
  layout_ = new QVBoxLayout(this);
  layout_->setMargin(0);
  layout_->setSpacing(20);

  auto *title = new QLabel(tr("模型选择"), this);
  title->setStyleSheet("font-size:42px; color:#FFFFFF; font-weight:600; margin:12px 8px;");
  layout_->addWidget(title);

  auto *desc = new QLabel(tr("本地模型列表, 点击切换 (SP=独立模型, CP=原版模型)。\n切换后设备自动重启加载新模型。"), this);
  desc->setWordWrap(true);
  desc->setStyleSheet("font-size:32px; color:#9A9A9A; margin:0 8px;");
  layout_->addWidget(desc);

  auto *btn = new QPushButton(tr("打开模型列表"), this);
  btn->setStyleSheet("QPushButton { height:110px; font-size:38px; background-color:#2C2CE2; color:white; border-radius:18px; border:none; }");
  QObject::connect(btn, &QPushButton::clicked, this, [this]() { openOverlay(); });
  layout_->addWidget(btn);
  layout_->addStretch();
}

void DrivingModelPanel::openOverlay() {
  QWidget *top = this->window();
  QWidget *overlay = top->findChild<QWidget*>("model_overlay");
  if (overlay) {
    if (QWidget *listW = overlay->findChild<QWidget*>("model_list")) buildModelList(listW);
    overlay->setGeometry(top->rect());
    overlay->show();
    overlay->raise();
    return;
  }
  // 全屏 overlay (非 QDialog, 避免 c3 竖屏崩溃); 弹窗内列表独立滚动, 不嵌套外层 ScrollView
  overlay = new QWidget(top);
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

  QWidget *listW = new QWidget();
  listW->setObjectName("model_list");
  buildModelList(listW);
  FollowScrollArea *sv = new FollowScrollArea(listW, panel);
  sv->setStyleSheet("background-color:#141414; border-radius:12px;");
  vbox->addWidget(sv, 1);

  QPushButton *closeBtn = new QPushButton(tr("关闭"), panel);
  closeBtn->setStyleSheet("height:100px; font-size:36px; background-color:#393939; color:white; border-radius:18px; border:none;");
  QObject::connect(closeBtn, &QPushButton::clicked, overlay, [overlay, top](bool) {
    overlay->hide();
    if (top) { top->raise(); top->activateWindow(); }
  });
  vbox->addWidget(closeBtn);

  overlay->show();
}

void DrivingModelPanel::buildModelList(QWidget *listW) {
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
  vbox->setContentsMargins(12, 12, 12, 12);
  vbox->setSpacing(8);

  QString list_json = run_mgr("list --json");
  QJsonObject lo = QJsonDocument::fromJson(list_json.toUtf8()).object();
  QJsonArray models = lo.value("models").toArray();

  if (models.isEmpty()) {
    QLabel *empty = new QLabel(tr("暂无本地模型"), listW);
    empty->setStyleSheet("font-size:38px; color:#9A9A9A;");
    vbox->addWidget(empty);
    return;
  }

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
    ModelRow *row = new ModelRow(r.text, r.idx, r.active, listW);
    row->on_click = [this, listW](int idx, const QString &text) {
      if (ConfirmationDialog::confirm(tr("切换到 %1?\n将重启设备 (约 1-2 分钟, 完整加载新模型), 建议停车时操作").arg(text), tr("CONFIRM"), this)) {
        ConfirmationDialog::alert(run_mgr(QString("switch %1").arg(idx)), this);
        buildModelList(listW);
      }
    };
    vbox->addWidget(row);
  }
  vbox->addStretch();
}
