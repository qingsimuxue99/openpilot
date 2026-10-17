import threading

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.hardware.usbgpu import check_usbgpu, get_usbgpu_device, usbgpu_status
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.list_view import button_item, text_item
from openpilot.system.ui.widgets.scroller_tici import Scroller


# eGPU（外接显卡）诊断界面使用的英文状态 / 检测结果 → 中文映射表。
# 硬件层 usbgpu_status() 与 check_usbgpu() 返回的是英文短语，这里统一翻译成中文展示，
# 保证该界面不出现英文。带动态数值的短语（如 "slow USB (480 Mbps)"）在 _status_to_cn 中按前缀匹配并保留数值。
_STATUS_CN = {
  # usbgpu_status() 可能返回的状态
  "not detected": "未检测到 eGPU 设备",
  "multiple devices": "检测到多个 eGPU 设备",
  "firmware mismatch": "固件版本不匹配",
  "startup failed": "启动失败",
  "loading": "加载中",
  "reboot to compile": "需重启以编译模型",
  "active": "运行中",
  "model not compiled": "模型未编译",
  "ready": "就绪",
  # check_usbgpu() 可能返回的错误
  "USB not connected": "USB 未连接",
  "GPU check timed out": "GPU 检测超时",
  "12V / PCIe not ready": "12V / PCIe 未就绪",
  "GPU incompatible": "GPU 不兼容",
  "USB disconnected during GPU check": "检测过程中 USB 断开",
  "USB link errors": "USB 链路错误",
  # 本界面内部使用的提示语
  "no errors": "无错误",
  "not checked": "尚未检测",
  "checking...": "检测中…",
  "not connected": "未连接",
}


def _status_to_cn(en: str) -> str:
  """将硬件层返回的英文状态翻译成中文。

  先查 _STATUS_CN 静态映射；查不到时按前缀匹配带数值的短语并保留数值；
  仍匹配不到则原样返回，避免界面因未知状态而崩溃。
  """
  if en in _STATUS_CN:
    return _STATUS_CN[en]
  # 含动态数值的短语：slow USB (480 Mbps) -> USB 速率过低（480 Mbps）
  if en.startswith("slow USB"):
    return "USB 速率过低（" + en.split("(", 1)[1].rstrip(")") + "）"
  # USB link 480 Mbps -> USB 链路速率 480 Mbps
  if en.startswith("USB link"):
    return "USB 链路速率 " + en.split(" ", 2)[2]
  # 12V off (8000 mV) -> 12V 供电关闭（8000 mV）
  if en.startswith("12V off"):
    return "12V 供电关闭（" + en.split("(", 1)[1].rstrip(")") + "）"
  # eGPU power fault (...) -> eGPU 供电异常（...）
  if en.startswith("eGPU power fault"):
    return "eGPU 供电异常（" + en.split("(", 1)[1].rstrip(")") + "）"
  # power status unavailable: xxx -> 供电状态读取失败：xxx
  if en.startswith("power status unavailable"):
    return "供电状态读取失败：" + en.split(":", 1)[1].strip()
  return en


class UsbGpuLayout(Widget):
  """eGPU（外接显卡）诊断界面。

  展示 eGPU 的当前运行状态、USB 连接速率，并提供一键检测按钮，
  用于排查 eGPU 的 USB 带宽、固件版本、12V/PCIe 供电以及 GPU 是否能正常运算。
  """

  def __init__(self):
    super().__init__()
    self._checking = False            # 是否正在执行检测，防止重复点击
    self._check_result = "尚未检测"     # 最近一次检测结果的展示文本（中文）

    # 界面条目：eGPU 状态、USB 连接速率、连接检测结果、检测按钮
    self._scroller = Scroller([
      text_item(lambda: tr("eGPU 状态"), self._status),
      text_item(lambda: tr("USB 连接"), self._link),
      text_item(lambda: tr("连接检测"), lambda: self._check_result),
      button_item(lambda: tr("检测 eGPU"), lambda: tr("检测"),
                  lambda: tr("检测 USB 5Gbps 速率、固件版本、12V/PCIe 供电以及 GPU 运算是否正常。"),
                  callback=self._start_check, enabled=ui_state.is_offroad),
    ], line_separator=True, spacing=0)

  @staticmethod
  def _status() -> str:
    """读取并翻译 eGPU 当前运行状态为中文。"""
    return _status_to_cn(usbgpu_status(ui_state.usbgpu_compiled, ui_state.usbgpu_loading,
                                       ui_state.usbgpu_active, ui_state.usbgpu_startup_failed,
                                       compile_pending=ui_state.usbgpu_compile_pending))

  @staticmethod
  def _link() -> str:
    """读取 eGPU 的 USB 连接速率；未连接时返回中文提示。

    速率达到 1Gbps 及以上时以 Gbps 显示，否则以 Mbps 显示。
    """
    device = get_usbgpu_device()
    if device is None:
      return "未连接"
    return f"{device.speed_mbps // 1000} Gbps" if device.speed_mbps >= 1000 and device.speed_mbps % 1000 == 0 \
      else f"{device.speed_mbps} Mbps"

  def _start_check(self):
    """点击“检测 eGPU”后，在后台线程执行检测，结果翻译为中文并显示在“连接检测”一行。"""
    if self._checking or not ui_state.is_offroad():
      return
    self._checking = True
    self._check_result = "检测中…"

    def run():
      try:
        # check_usbgpu 返回 None 表示无错误，否则返回英文错误短语
        result = check_usbgpu()
        self._check_result = "无错误" if result is None else _status_to_cn(result)
      finally:
        self._checking = False

    threading.Thread(target=run, name="usbgpu-check", daemon=True).start()

  def show_event(self):
    super().show_event()
    self._scroller.show_event()

  def _render(self, rect):
    self._scroller.render(rect)
