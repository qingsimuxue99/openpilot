#!/usr/bin/env python3
import os
import subprocess
import time
from cereal import car, messaging
from openpilot.common.realtime import Ratekeeper
from openpilot.common.params import Params
import threading

AudibleAlert = car.CarControl.HUDControl.AudibleAlert
BUZZER_GPIO = 42


def buzzer_present():
  # C3XL(阉割版)带蜂鸣器(GPIO42); 标准 C3 无蜂鸣器, 走扬声器(soundd)
  # 用 sudo 主动尝试导出 GPIO42 判断: 能导出=有蜂鸣器(C3XL), 失败=无(C3)
  p = "/sys/class/gpio/gpio%d" % BUZZER_GPIO
  if os.path.exists(p):
    return True
  subprocess.run("echo %d | sudo tee /sys/class/gpio/export" % BUZZER_GPIO,
                 shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, encoding='utf8')
  return os.path.exists(p)


def resolve_beep_mode():
  # BeepMode: 0=自动 1=蜂鸣器 2=扬声器 3=关闭
  bm = Params().get_int("BeepMode")
  if bm == 3:
    return "off"
  if bm == 1:
    return "buzzer"
  if bm == 2:
    return "speaker"
  # 0 自动: 有蜂鸣器用蜂鸣器, 否则交给扬声器(soundd)
  return "buzzer" if buzzer_present() else "speaker"


class Beepd:
  def __init__(self):
    self.current_alert = AudibleAlert.none
    self.active = (resolve_beep_mode() == "buzzer")
    if self.active:
      self.enable_gpio()
      # 开机提示音: 仅 C3XL 蜂鸣器设备; 标准 C3 没有开机提示音逻辑, 此分支不会在 C3 上执行(self.active 为 False)
      if self._sound_on("BeepStartup"):
        self.startup_beep()

  def enable_gpio(self):
    try:
      subprocess.run("echo %d | sudo tee /sys/class/gpio/export" % BUZZER_GPIO,
                     shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, encoding='utf8')
    except Exception:
      pass
    subprocess.run('echo "out" | sudo tee /sys/class/gpio/gpio%d/direction' % BUZZER_GPIO,
                   shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, encoding='utf8')

  def _beep(self, on):
    val = "1" if on else "0"
    subprocess.run('echo "%s" | sudo tee /sys/class/gpio/gpio%d/value' % (val, BUZZER_GPIO),
                   shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, encoding='utf8')

  def engage(self):
    self._beep(True)
    time.sleep(0.05)
    self._beep(False)

  def disengage(self):
    for _ in range(2):
      self._beep(True)
      time.sleep(0.01)
      self._beep(False)
      time.sleep(0.01)

  def warning(self):
    for _ in range(3):
      self._beep(True)
      time.sleep(0.01)
      self._beep(False)
      time.sleep(0.01)

  def startup_beep(self):
    # 开机滴一声: 仅 C3XL(蜂鸣器)有; 标准 C3 无此逻辑
    self._beep(True)
    time.sleep(0.1)
    self._beep(False)

  def dispatch_beep(self, func):
    threading.Thread(target=func, daemon=True).start()

  def _sound_on(self, key):
    # 细分子开关: 未设置或 !=0 视为开启(向后兼容, 默认开)
    v = Params().get(key)
    if v is None or v == b"":
      return True
    try:
      return int(v) != 0
    except ValueError:
      return True

  def update_alert(self, new_alert):
    if new_alert != self.current_alert:
      self.current_alert = new_alert
      print("[BEEP] New alert: %s" % new_alert)
      if new_alert == AudibleAlert.engage:
        if self._sound_on("BeepEngage"):
          self.dispatch_beep(self.engage)
      elif new_alert == AudibleAlert.disengage:
        if self._sound_on("BeepDisengage"):
          self.dispatch_beep(self.disengage)
      elif new_alert in [AudibleAlert.refuse, AudibleAlert.prompt, AudibleAlert.warningSoft, AudibleAlert.promptRepeat]:
        self.dispatch_beep(self.warning)

  def get_audible_alert(self, sm):
    if sm.updated['selfdriveState']:
      new_alert = sm['selfdriveState'].alertSound.raw
      self.update_alert(new_alert)

  def test_beepd_thread(self):
    frame = 0
    rk = Ratekeeper(20)
    pm = messaging.PubMaster(['selfdriveState'])
    while True:
      cs = messaging.new_message('selfdriveState')
      if frame == 20:
        cs.selfdriveState.alertSound = AudibleAlert.engage
      if frame == 40:
        cs.selfdriveState.alertSound = AudibleAlert.disengage
      if frame == 60:
        cs.selfdriveState.alertSound = AudibleAlert.prompt
      if frame == 80:
        cs.selfdriveState.alertSound = AudibleAlert.disengage
      if frame == 85:
        cs.selfdriveState.alertSound = AudibleAlert.prompt
      pm.send("selfdriveState", cs)
      frame += 1
      rk.keep_time()

  def beepd_thread(self, test=False):
    if test:
      threading.Thread(target=self.test_beepd_thread, daemon=True).start()
    sm = messaging.SubMaster(['selfdriveState'])
    rk = Ratekeeper(20)
    while True:
      sm.update(0)
      if self.active:
        self.get_audible_alert(sm)
      rk.keep_time()


def main():
  s = Beepd()
  s.beepd_thread(test=False)  # 改成 True 可启用模拟测试数据 测试后改回 False


if __name__ == "__main__":
  main()
