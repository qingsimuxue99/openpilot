#pragma once

#include <QFrame>
#include <QWidget>
#include <QLabel>
#include <QTimer>
#include <QJsonObject>
#include <QPainter>

#include "common/params.h"

class BarChart : public QWidget {
  Q_OBJECT
public:
  explicit BarChart(QWidget* parent = nullptr);
  void setData(const QJsonObject& daily);
protected:
  void paintEvent(QPaintEvent* event) override;
private:
  QJsonObject m_daily;
};

class DriveStatsPanel : public QFrame {
  Q_OBJECT
public:
  explicit DriveStatsPanel(QWidget* parent = nullptr);
private slots:
  void refresh();
private:
  QFrame* card(const QString& title, QWidget* body);
  QString readFile(const QString& path);

  Params params;
  QTimer* timer;

  QLabel *v_today_km, *v_today_sub;
  QLabel *v_week_km, *v_week_sub;
  QLabel *v_total_km, *v_total_sub;
  BarChart* barChart;
  QLabel *s_cpu, *s_mem, *s_store, *s_uptime;
};
