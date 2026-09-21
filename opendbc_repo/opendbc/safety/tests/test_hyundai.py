#!/usr/bin/env python3
import random
import unittest

from opendbc.car.hyundai.values import HyundaiSafetyFlags
from opendbc.car.structs import CarParams
from opendbc.safety.tests.libsafety import libsafety_py
import opendbc.safety.tests.common as common
from opendbc.safety.tests.common import CANPackerPanda
from opendbc.safety.tests.hyundai_common import HyundaiButtonBase, HyundaiLongitudinalBase


# 4 bit checkusm used in some hyundai messages
# lives outside the can packer because we never send this msg
def checksum(msg):
  addr, dat, bus = msg

  chksum = 0
  if addr == 0x386:
    for i, b in enumerate(dat):
      for j in range(8):
        # exclude checksum and counter bits
        if (i != 1 or j < 6) and (i != 3 or j < 6) and (i != 5 or j < 6) and (i != 7 or j < 6):
          bit = (b >> j) & 1
        else:
          bit = 0
        chksum += bit
    chksum = (chksum ^ 9) & 0xF
    ret = bytearray(dat)
    ret[5] |= (chksum & 0x3) << 6
    ret[7] |= (chksum & 0xc) << 4
  else:
    for i, b in enumerate(dat):
      if addr in [0x260, 0x421] and i == 7:
        b &= 0x0F if addr == 0x421 else 0xF0
      elif addr == 0x394 and i == 6:
        b &= 0xF0
      elif addr == 0x394 and i == 7:
        continue
      chksum += sum(divmod(b, 16))
    chksum = (16 - chksum) % 16
    ret = bytearray(dat)
    ret[6 if addr == 0x394 else 7] |= chksum << (4 if addr == 0x421 else 0)

  return addr, ret, bus


class TestHyundaiSafety(HyundaiButtonBase, common.PandaCarSafetyTest, common.DriverTorqueSteeringSafetyTest, common.SteerRequestCutSafetyTest):
  TX_MSGS = [[0x340, 0], [0x4F1, 0], [0x485, 0]]
  STANDSTILL_THRESHOLD = 12  # 0.375 kph
  RELAY_MALFUNCTION_ADDRS = {0: (0x340,)}  # LKAS11
  FWD_BLACKLISTED_ADDRS = {2: [0x340, 0x485]}

  MAX_RATE_UP = 3
  MAX_RATE_DOWN = 7
  MAX_TORQUE = 384
  MAX_RT_DELTA = 112
  RT_INTERVAL = 250000
  DRIVER_TORQUE_ALLOWANCE = 50
  DRIVER_TORQUE_FACTOR = 2

  # Safety around steering req bit
  MIN_VALID_STEERING_FRAMES = 89
  MAX_INVALID_STEERING_FRAMES = 2
  MIN_VALID_STEERING_RT_INTERVAL = 810000  # a ~10% buffer, can send steer up to 110Hz

  cnt_gas = 0
  cnt_speed = 0
  cnt_brake = 0
  cnt_cruise = 0
  cnt_button = 0

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, 0)
    self.safety.init_tests()

  def _button_msg(self, buttons, main_button=0, bus=0):
    values = {"CF_Clu_CruiseSwState": buttons, "CF_Clu_CruiseSwMain": main_button, "CF_Clu_AliveCnt1": self.cnt_button}
    self.__class__.cnt_button += 1
    return self.packer.make_can_msg_panda("CLU11", bus, values)

  def _user_gas_msg(self, gas):
    values = {"CF_Ems_AclAct": gas, "AliveCounter": self.cnt_gas % 4}
    self.__class__.cnt_gas += 1
    return self.packer.make_can_msg_panda("EMS16", 0, values, fix_checksum=checksum)

  def _user_brake_msg(self, brake):
    values = {"DriverOverride": 2 if brake else random.choice((0, 1, 3)),
              "AliveCounterTCS": self.cnt_brake % 8}
    self.__class__.cnt_brake += 1
    return self.packer.make_can_msg_panda("TCS13", 0, values, fix_checksum=checksum)

  def _speed_msg(self, speed):
    # panda safety doesn't scale, so undo the scaling
    values = {"WHL_SPD_%s" % s: speed * 0.03125 for s in ["FL", "FR", "RL", "RR"]}
    values["WHL_SPD_AliveCounter_LSB"] = (self.cnt_speed % 16) & 0x3
    values["WHL_SPD_AliveCounter_MSB"] = (self.cnt_speed % 16) >> 2
    self.__class__.cnt_speed += 1
    return self.packer.make_can_msg_panda("WHL_SPD11", 0, values, fix_checksum=checksum)

  def _pcm_status_msg(self, enable):
    values = {"ACCMode": enable, "CR_VSM_Alive": self.cnt_cruise % 16}
    self.__class__.cnt_cruise += 1
    return self.packer.make_can_msg_panda("SCC12", self.SCC_BUS, values, fix_checksum=checksum)

  def _torque_driver_msg(self, torque):
    values = {"CR_Mdps_StrColTq": torque}
    return self.packer.make_can_msg_panda("MDPS12", 0, values)

  def _torque_cmd_msg(self, torque, steer_req=1):
    values = {"CR_Lkas_StrToqReq": torque, "CF_Lkas_ActToi": steer_req}
    return self.packer.make_can_msg_panda("LKAS11", 0, values)


class TestHyundaiSafetyAltLimits(TestHyundaiSafety):
  MAX_RATE_UP = 2
  MAX_RATE_DOWN = 3
  MAX_TORQUE = 270

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.ALT_LIMITS)
    self.safety.init_tests()


class TestHyundaiSafetyAltLimits2(TestHyundaiSafety):
  MAX_RATE_UP = 2
  MAX_RATE_DOWN = 3
  MAX_TORQUE = 170

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.ALT_LIMITS_2)
    self.safety.init_tests()


class TestHyundaiSafetyCameraSCC(TestHyundaiSafety):
  BUTTONS_TX_BUS = 2  # tx on 2, rx on 0
  SCC_BUS = 2  # rx on 2

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.CAMERA_SCC)
    self.safety.init_tests()


class TestHyundaiSafetyFCEV(TestHyundaiSafety):
  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.FCEV_GAS)
    self.safety.init_tests()

  def _user_gas_msg(self, gas):
    values = {"ACCELERATOR_PEDAL": gas}
    return self.packer.make_can_msg_panda("FCEV_ACCELERATOR", 0, values)


class TestHyundaiLegacySafety(TestHyundaiSafety):
  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundaiLegacy, 0)
    self.safety.init_tests()


class TestHyundaiLegacySafetyEV(TestHyundaiSafety):
  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundaiLegacy, 1)
    self.safety.init_tests()

  def _user_gas_msg(self, gas):
    values = {"Accel_Pedal_Pos": gas}
    return self.packer.make_can_msg_panda("E_EMS11", 0, values, fix_checksum=checksum)


class TestHyundaiLegacySafetyHEV(TestHyundaiSafety):
  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundaiLegacy, 2)
    self.safety.init_tests()

  def _user_gas_msg(self, gas):
    values = {"CR_Vcu_AccPedDep_Pos": gas}
    return self.packer.make_can_msg_panda("E_EMS11", 0, values, fix_checksum=checksum)

class TestHyundaiLongitudinalSafety(HyundaiLongitudinalBase, TestHyundaiSafety):
  TX_MSGS = [[0x340, 0], [0x4F1, 0], [0x485, 0], [0x420, 0], [0x421, 0], [0x50A, 0], [0x389, 0], [0x4A2, 0], [0x38D, 0], [0x483, 0], [0x7D0, 0]]

  # ★ 2026-09-18 期望同步: 本 fork 自 8a8c2ce 起有意改变 bus0 SCC12 的语义 ——
  #   判据从"看 hyundai_camera_scc 配置"改为"bus2 上是否真的出现过 SCC12"(物理事实)。
  #   radar-SCC 车(本类模拟的默认场景: 前雷达在 bus0 常驻发 SCC12)不应再因此报继电器故障,
  #   否则库斯图/伊兰特等 63 台传统 CAN 车会误报(屏幕"继电器故障")。
  #   上游此处 (0x340, 0x421) 的期望已不适用于本 fork。
  RELAY_MALFUNCTION_ADDRS = {0: (0x340,)}  # LKAS11 only; SCC12 gated by physical bus-2 detection

  DISABLED_ECU_UDS_MSG = (0x7D0, 0)
  DISABLED_ECU_ACTUATION_MSG = (0x421, 0)

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.LONG)
    self.safety.init_tests()

  def _accel_msg(self, accel, aeb_req=False, aeb_decel=0):
    values = {
      "aReqRaw": accel,
      "aReqValue": accel,
      "AEB_CmdAct": int(aeb_req),
      "CR_VSM_DecCmd": aeb_decel,
    }
    return self.packer.make_can_msg_panda("SCC12", self.SCC_BUS, values)

  def _fca11_msg(self, idx=0, vsm_aeb_req=False, fca_aeb_req=False, aeb_decel=0):
    values = {
      "CR_FCA_Alive": idx % 0xF,
      "FCA_Status": 2,
      "CR_VSM_DecCmd": aeb_decel,
      "CF_VSM_DecCmdAct": int(vsm_aeb_req),
      "FCA_CmdAct": int(fca_aeb_req),
    }
    return self.packer.make_can_msg_panda("FCA11", 0, values)

  def test_no_aeb_fca11(self):
    self.assertTrue(self._tx(self._fca11_msg()))
    self.assertFalse(self._tx(self._fca11_msg(vsm_aeb_req=True)))
    self.assertFalse(self._tx(self._fca11_msg(fca_aeb_req=True)))
    self.assertFalse(self._tx(self._fca11_msg(aeb_decel=1.0)))

  def test_no_aeb_scc12(self):
    self.assertTrue(self._tx(self._accel_msg(0)))
    self.assertFalse(self._tx(self._accel_msg(0, aeb_req=True)))
    self.assertFalse(self._tx(self._accel_msg(0, aeb_decel=1.0)))


  def test_disabled_ecu_alive(self):
    """★ 2026-09-18 期望同步: 本 fork 的 SCC12 判据已改为"物理事实"(bus2 上是否真出现过 SCC12)。

    上游此处期望 bus0 出现 0x421 即报继电器故障; 但那正是本 fork 中
    "radar-SCC 车(库斯图/伊兰特等)开纵向即持续误报" 的来源, 故已废弃。
    新语义: 仅当 bus2 上确实出现过 SCC12(真 camera-SCC 车的物理特征)之后,
    bus0 再出现 0x421 才判为"原厂 ECU 夺回"。
    """
    # 1) 未见过 bus2 的 SCC12 => bus0 的 SCC12 不算夺回(radar-SCC 车常态, 库斯图/伊兰特)
    self.assertFalse(self.safety.get_relay_malfunction())
    for _ in range(10):
      self._rx(common.make_msg(0, 0x421, 8))
    self.assertFalse(self.safety.get_relay_malfunction())

    # 2) 模拟 bus2 上出现过 SCC12(camera-SCC 车特征) => 此时 bus0 的 SCC12 才算夺回
    self._rx(common.make_msg(2, 0x421, 8))
    self.safety.set_relay_malfunction(False)
    self._rx(common.make_msg(0, 0x421, 8))
    self.assertTrue(self.safety.get_relay_malfunction())


class TestHyundaiLongitudinalSafetyCameraSCC(HyundaiLongitudinalBase, TestHyundaiSafety):
  TX_MSGS = [[0x340, 0], [0x4F1, 2], [0x485, 0], [0x420, 0], [0x421, 0], [0x50A, 0], [0x389, 0], [0x4A2, 0]]

  FWD_BLACKLISTED_ADDRS = {2: [0x340, 0x485, 0x420, 0x421, 0x50A, 0x389]}

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, HyundaiSafetyFlags.LONG | HyundaiSafetyFlags.CAMERA_SCC)
    self.safety.init_tests()

  def _accel_msg(self, accel, aeb_req=False, aeb_decel=0):
    values = {
      "aReqRaw": accel,
      "aReqValue": accel,
      "AEB_CmdAct": int(aeb_req),
      "CR_VSM_DecCmd": aeb_decel,
    }
    return self.packer.make_can_msg_panda("SCC12", self.SCC_BUS, values)

  def test_no_aeb_scc12(self):
    self.assertTrue(self._tx(self._accel_msg(0)))
    self.assertFalse(self._tx(self._accel_msg(0, aeb_req=True)))
    self.assertFalse(self._tx(self._accel_msg(0, aeb_decel=1.0)))

  def test_tester_present_allowed(self):
    pass

  def test_disabled_ecu_alive(self):
    pass


if __name__ == "__main__":
  unittest.main()


class TestHyundaiLongitudinalSafetyESCC(common.PandaSafetyTestBase):
  """★ 2026-09-18: 库斯图场景回归 —— 装 ESCC 模块的 radar-SCC 车。

  车辆特征(硬证据):
    · 传统 CAN + 前雷达做 SCC(SCC12 物理在 bus0), 无 CAMERA_SCC 车型 flag;
    · bus0 能检测到 ESCC 报文(0x2AB / BO_ 683 ESCC) 且 EnableEscc=1
      => Python: spFlags |= SP_ENHANCED_SCC => safetyParam |= ESCC(1024)
      => panda: hyundai_escc = true。

  为什么必须单独测:
    上游原判据 "OP 管纵向 => 原厂雷达必须已停用 => 不该再看到 SCC12" 与 ESCC 的
    设计前提互斥 —— tx_hook 的 0x7D0 门控写成 `!hyundai_escc`, 即 hyundai_escc=true
    时【放行 UDS, 不停用原厂雷达】; ESCC 模块正是靠改写原厂雷达的设定点工作,
    雷达被停用就没东西可改。
    => 该车一开纵向, bus0 上常驻的原厂 SCC12 就 50Hz 命中老判据 => 持续"继电器故障"。
    这是库斯图报障、而未装 ESCC 的车不报的差异所在。

  本类只做针对性断言, 不继承 TestHyundaiSafety 的全套用例
  (那套用例因本 fork 的 TX_MSGS 与上游不同步而有既有失败, 与本判据无关)。
  """

  def setUp(self):
    self.packer = CANPackerPanda("hyundai_kia_generic")
    self.safety = libsafety_py.libsafety
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai,
                                 HyundaiSafetyFlags.LONG | HyundaiSafetyFlags.ESCC)
    self.safety.init_tests()

  def _set_hooks(self, param):
    self.safety.set_relay_malfunction(False)
    self.safety.set_safety_hooks(CarParams.SafetyModel.hyundai, param)
    self.safety.init_tests()

  def test_escc_car_scc12_on_bus0_does_not_trigger_relay_malfunction(self):
    """★ 核心回归(库斯图症状): ESCC 车 bus0 常驻原厂 SCC12, 开纵向时不得报继电器故障。

    老判据 `hyundai_longitudinal && addr==0x421` 下, 本断言必然失败(50Hz 误报)。
    """
    self.assertFalse(self.safety.get_relay_malfunction())
    for _ in range(50):
      self._rx(common.make_msg(0, 0x421, 8))
    self.assertFalse(self.safety.get_relay_malfunction())

  def test_escc_car_immune_even_after_bus2_scc12_seen(self):
    """即便 bus2 上出现过 SCC12(异常串扰), "ESCC 且非 camera-SCC" 车仍整体豁免。"""
    self._rx(common.make_msg(2, 0x421, 8))
    self.safety.set_relay_malfunction(False)
    for _ in range(20):
      self._rx(common.make_msg(0, 0x421, 8))
    self.assertFalse(self.safety.get_relay_malfunction())

  def test_escc_car_still_reports_lkas11_reclaim(self):
    """对照(保护未丢): LKAS11(0x340) 的原厂夺回判据对 ESCC 车照旧有效 —— 只摘掉 SCC12 那条。"""
    self.safety.set_relay_malfunction(False)
    for _ in range(20):
      self._rx(common.make_msg(0, 0x340, 8))
    self.assertTrue(self.safety.get_relay_malfunction())

  def test_non_escc_car_behavior_unchanged(self):
    """对照(无回归): 非 ESCC 车 + bus2 见过 SCC12 => bus0 的 SCC12 仍然算"原厂夺回"。"""
    self._set_hooks(HyundaiSafetyFlags.LONG)
    self._rx(common.make_msg(2, 0x421, 8))
    self.safety.set_relay_malfunction(False)
    for _ in range(20):
      self._rx(common.make_msg(0, 0x421, 8))
    self.assertTrue(self.safety.get_relay_malfunction())
