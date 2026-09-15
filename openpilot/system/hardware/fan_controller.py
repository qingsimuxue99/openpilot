#!/usr/bin/env python3
import numpy as np

from openpilot.common.pid import PIDController


class FanController:
  def __init__(self, rate: int) -> None:
    self.last_ignition = False
    self.controller = PIDController(k_p=0, k_i=4e-3, rate=rate)

  def update(self, cur_temp: float, ignition: bool) -> int:
    self.controller.pos_limit = 100 if ignition else 30
    self.controller.neg_limit = 20 if ignition else 0  # 最低20%，避免完全停转

    if ignition != self.last_ignition:
      self.controller.reset()
    self.last_ignition = ignition

    # 温度-转速曲线优化：
    # 60°C 以下 → 20%（最低转速，静音）
    # 60-75°C → 20-40%（缓慢上升）
    # 75-85°C → 40-80%（快速上升）
    # 85°C 以上 → 100%（全速散热）
    feedforward = np.interp(cur_temp, [60.0, 75.0, 85.0, 100.0], [20.0, 40.0, 80.0, 100.0])

    return int(self.controller.update(
                 error=(cur_temp - 75),  # 目标温度75°C
                 feedforward=feedforward
              ))
