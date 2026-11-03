import os
from pathlib import Path

CHESTNUT_FW_VERSION = "8516b768"
CHESTNUT_USB_IDS = ((0xADD1, 0x0001), (0x3801, 0x0001))
CHESTNUT_ROM_USB_IDS = ((0x174C, 0x2464), (0x174C, 0x2463))
# 认得的固件版本串：8516b768 是 comma 定制版，ed4e39b7 是 ASM2464PDX 原厂（支持热插拔）版。
# 两者都跑 USB4/PCIe 隧道，只是产品描述符里的版本串不同。各处都是拿上报的串和
# CHESTNUT_USB_PRODUCT 直接判等，换固件就会把整条 eGPU 链路判成"不可用"（modeld 忽略底座、
# 退回小模型），所以让 CHESTNUT_USB_PRODUCT 自己同时等于这两个串，判定代码无需改动。
CHESTNUT_FW_VERSIONS = (CHESTNUT_FW_VERSION, "ed4e39b7")
CHESTNUT_USB_PRODUCTS = tuple(f"custom {version}-CLEAN" for version in CHESTNUT_FW_VERSIONS)


class _ChestnutProduct(str):
  """同时等于任一可接受版本产品串的 str（两个比较方向、== 与 != 都成立）。"""

  def __eq__(self, other):
    if isinstance(other, str):
      return other in CHESTNUT_USB_PRODUCTS
    return NotImplemented

  def __ne__(self, other):
    result = self.__eq__(other)
    return result if result is NotImplemented else not result

  __hash__ = str.__hash__


CHESTNUT_USB_PRODUCT = _ChestnutProduct(f"custom {CHESTNUT_FW_VERSION}-CLEAN")
USB_DEVICES_PATH = Path("/sys/bus/usb/devices")
TYPEC_CC_ORIENTATION_PATH = Path("/sys/class/power_supply/usb/typec_cc_orientation")
PRIMARY_USB_CONTROLLER = "a600000.ssusb"


def is_chestnut_usb_id(vendor_id: int, product_id: int, include_bootloader: bool = False) -> bool:
  ids = CHESTNUT_USB_IDS + CHESTNUT_ROM_USB_IDS if include_bootloader else CHESTNUT_USB_IDS
  return (vendor_id, product_id) in ids


def get_usb_topology() -> set[str]:
  try:
    return set(os.listdir(USB_DEVICES_PATH))
  except OSError:
    return set()


def read(path: Path) -> str | None:
  try:
    return path.read_text().strip()
  except OSError:
    return None


def read_int(path: Path, base: int = 10) -> int:
  try:
    return int(path.read_text(), base)
  except (OSError, ValueError, TypeError):
    return 0


def usb_devices() -> list[Path]:
  try:
    devices = (d for d in USB_DEVICES_PATH.glob("*") if (d / "idVendor").exists())
    return sorted(devices, key=lambda p: p.name)
  except OSError:
    return []


def controller(device: Path) -> Path | None:
  try:
    return next((parent for parent in device.resolve().parents if parent.name.endswith(".ssusb")), None)
  except OSError:
    return None


def get_usb_state() -> list[dict]:
  devices = []
  typec_orientation = read_int(TYPEC_CC_ORIENTATION_PATH)
  for device in usb_devices():
    vendor_id = read_int(device / "idVendor", 16)
    product_id = read_int(device / "idProduct", 16)
    ctrl = controller(device)
    devices.append({
      "busnum": read_int(device / "busnum"),
      "devnum": read_int(device / "devnum"),
      "vendorId": vendor_id,
      "productId": product_id,
      "speedMbps": read_int(device / "speed"),
      "manufacturer": read(device / "manufacturer") or "",
      "product": read(device / "product") or "",
      "linkErrorCount": read_int(ctrl / "portli", 0) & 0xFFFF if ctrl is not None else 0,
      "usb3Lane": {1: "a", 2: "b"}.get(typec_orientation, "unknown") if ctrl is not None and ctrl.name == PRIMARY_USB_CONTROLLER else "unknown",
    })
  return devices


def is_chestnut_connected(devices: list[dict] | None = None, include_bootloader: bool = False) -> bool:
  devices = get_usb_state() if devices is None else devices
  return any(is_chestnut_usb_id(d["vendorId"], d["productId"], include_bootloader) for d in devices)


def set_usb_state(device_state, devices: list[dict]) -> None:
  entries = device_state.usbState.init('devices', len(devices))

  chestnut_present = False
  for entry, device in zip(entries, devices, strict=True):
    entry.busnum = device["busnum"]
    entry.devnum = device["devnum"]
    entry.vendorId = device["vendorId"]
    entry.productId = device["productId"]
    entry.speedMbps = device["speedMbps"]
    entry.manufacturer = device["manufacturer"]
    entry.product = device["product"]
    entry.linkErrorCount = device["linkErrorCount"]
    entry.usb3Lane = device.get("usb3Lane", "unknown")

    if is_chestnut_usb_id(entry.vendorId, entry.productId):
      chestnut_present = True

  device_state.chestnutPresent = chestnut_present
