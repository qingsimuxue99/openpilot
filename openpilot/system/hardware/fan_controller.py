#!/usr/bin/env python3
import numpy as np

from openpilot.common.params import Params
from openpilot.common.pid import PIDController

C3_FAN_DEVICE_TYPES = frozenset(("tici", "tizi"))


class FanController:
  def __init__(self, rate: int, device_type: str) -> None:
    self.last_ignition = False
    self.c3_fan_profile = device_type in C3_FAN_DEVICE_TYPES
    self.target_temp = 75 if self.c3_fan_profile else 70
    # C3/C3X use the proven temperature feed-forward from c3-wip. Keep the
    # current C4/mici controller unchanged.
    self.controller = PIDController(k_p=0, k_i=4e-3, k_f=1 if self.c3_fan_profile else 0, rate=rate)
    # 「风扇全速」开关（参数 CarrotFanFullSpeed，由 UI 侧栏点击温度/风扇卡切换）。
    # 老版 Qt sidebar.cc 只写这个参数却无人消费，等于只改显示；这里真正接入
    # 风扇控制链：置位时直接输出 100%，绕过 PID 的温度反馈。
    self._params = Params()
    self._fan_full = False
    self._read_fan_full()

  def _read_fan_full(self):
    try:
      self._fan_full = self._params.get_bool("CarrotFanFullSpeed")
    except Exception:
      self._fan_full = False

  def update(self, cur_temp: float, ignition: bool) -> int:
    # 约每 0.5s 复查一次参数，保证 UI 侧切换后风扇及时跟随
    if int(cur_temp * 2) != getattr(self, "_last_probe", -1):
      self._last_probe = int(cur_temp * 2)
      self._read_fan_full()

    # 全速模式：直接 100%，不受点火状态与温度限制
    if self._fan_full:
      self.controller.reset()
      self.last_ignition = ignition
      return 100

    self.controller.pos_limit = 100 if ignition else 30
    self.controller.neg_limit = 30 if ignition else 0

    if ignition != self.last_ignition:
      self.controller.reset()
    self.last_ignition = ignition

    return int(self.controller.update(
                 error=(cur_temp - self.target_temp),
                 feedforward=np.interp(cur_temp, [60.0, 100.0], [0, 100])
              ))
