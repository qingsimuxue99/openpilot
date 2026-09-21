from opendbc.car import Bus, get_safety_config, structs
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.values import HyundaiFlags, HyundaiFlagsSP, CAR, DBC, CANFD_RADAR_SCC_CAR, \
                                                   CANFD_UNSUPPORTED_LONGITUDINAL_CAR, \
                                                   UNSUPPORTED_LONGITUDINAL_CAR, HyundaiSafetyFlags, HyundaiExtFlags
from opendbc.car.hyundai.radar_interface import RADAR_START_ADDR
from opendbc.car.interfaces import CarInterfaceBase
from opendbc.car.disable_ecu import disable_ecu
from opendbc.car.hyundai.carcontroller import CarController
from opendbc.car.hyundai.carstate import CarState
from opendbc.car.hyundai.radar_interface import RadarInterface

from openpilot.common.params import Params

ButtonType = structs.CarState.ButtonEvent.Type
Ecu = structs.CarParams.Ecu

# Cancel button can sometimes be ACC pause/resume button, main button can also enable on some cars
ENABLE_BUTTONS = (ButtonType.accelCruise, ButtonType.decelCruise, ButtonType.cancel, ButtonType.mainCruise)

SteerControlType = structs.CarParams.SteerControlType


class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController
  RadarInterface = RadarInterface

  @staticmethod
  def _get_params(ret: structs.CarParams, candidate, fingerprint, car_fw, alpha_long, is_release, docs) -> structs.CarParams:

    params = Params()
    camera_scc = params.get_int("HyundaiCameraSCC")

    # ★ 2026-09-18 物理事实闸门: HyundaiCameraSCC 只对 CANFD 车生效。
    #   该参数三档语义(1 Long-con / 2 Cruise State Sync / 3 Stock Cruise)全部是 CANFD 场景;
    #   传统 CAN 车的纵向由 EnableRadarTracks(>0 或 -2) 负责, 不该再借本参数。
    #   不设闸门时, 一个被污染的参数会同时点着四个症状:
    #     1) ret.flags |= CAMERA_SCC -> panda safetyParam bit8 -> panda RX 表切 camera 分支,
    #        要求 SCC12 出现在 bus2; 传统 CAN 车的 SCC12 在 bus0 => 永久 rxInvalid
    #        => 屏幕 Controls Mismatch
    #     2) panda 侧 stock_ecu_detected 把 bus0 上常驻的原厂 SCC12 当成"原厂 ECU 夺回"
    #        => 屏幕"继电器故障"(relayMalfunction)
    #     3) Python 雷达按 CAMERA_SCC 猜总线, 把 SCC11 订到 bus2 => rcp_scc.can_valid 恒 False
    #        => 屏幕 "CAN Error: Check Connections!!"  (此项已由 2b9cf57 的双总线订阅修复)
    #     4) cam_can 被硬编码成 1(bus1); camera 分支下巡航状态只认 bus2 的 SCC12
    #   覆盖: 全部 63 台未标 CAMERA_SCC 的传统 CAN 车(库斯图 CUSTIN_1ST_GEN / 伊兰特 ELANTRA_2021 等)。
    #   不影响: 33 台 CANFD 车(本参数照旧生效) + 4 台自带 CAMERA_SCC 车型 flag 的传统 CAN 车
    #          (Kona 2022 / Kona EV 2022 / Casper / Casper EV)。
    # 仅本变量会被下面的闸门归零；原值 camera_scc 保留给下方纵向开关判断(:218)，
    # 以免闸门连带改变"非 CANFD 车 + 参数=3(Stock Cruise)"的纵向行为(不该由本修复决定)。
    camera_scc_effective = camera_scc

    if (camera_scc > 0) and not (ret.flags & HyundaiFlags.CANFD):
      print("$$$CAMERA_SCC param IGNORED: non-CANFD car (flags=0x%x), HyundaiCameraSCC=%d"
            " -> CAMERA_SCC flag NOT set, cam_can stays auto; use EnableRadarTracks(>0 or -2) for longitudinal"
            % (ret.flags, camera_scc))
      camera_scc_effective = 0

    if camera_scc_effective > 0:
      ret.flags |= HyundaiFlags.CAMERA_SCC.value
      print(f"$$$CAMERA_SCC toggled by param HyundaiCameraSCC={camera_scc} (car flags=0x{ret.flags:x})")
    else:
      # ★ 2026-09-17: 打印关闭态, 便于现场 grep '$$$CAMERA_SCC' 一条日志判定本车 SCC 来源配置。
      # 注意: 该参数为非 0 时, panda 侧 RX 表会切到 camera 分支并要求 SCC12 出现在 bus2;
      # 若本车实际是 radar-SCC(SCC12 在 bus0), 会永久 rxInvalid => Controls Mismatch(CAN 错误)。
      # 库斯图/伊兰特等雷达 SCC 车必须保持 0。
      print(f"$$$CAMERA_SCC off (param HyundaiCameraSCC={camera_scc}, car flags=0x{ret.flags:x})")

    ret.brand = "hyundai"

    cam_can = CanBus(None, fingerprint).CAM if camera_scc_effective == 0 else 1
    hda2 = False #0x50 in fingerprint[cam_can] or 0x110 in fingerprint[cam_can]
    hda2 = hda2 or params.get_int("CanfdHDA2") > 0
    CAN = CanBus(None, fingerprint, hda2)

    if params.get_int("CanfdDebug") == -1:
      ret.flags |= HyundaiFlags.ANGLE_CONTROL.value

    if ret.flags & HyundaiFlags.CANFD:
      # Shared configuration for CAN-FD cars
      ret.alphaLongitudinalAvailable = True #candidate not in (CANFD_UNSUPPORTED_LONGITUDINAL_CAR | CANFD_RADAR_SCC_CAR)
      #ret.enableBsm = 0x1e5 in fingerprint[CAN.ECAN]
      ret.enableBsm = 0x1ba in fingerprint[CAN.ECAN] # BLINDSPOTS_REAR_CORNERS 0x1ba(442)

      if 0x105 in fingerprint[CAN.ECAN]:
        ret.flags |= HyundaiFlags.HYBRID.value

      if 203 in fingerprint[CAN.CAM]: # LFA_ALT
        print("##### Anglecontrol detected (LFA_ALT)")
        ret.flags |= HyundaiFlags.ANGLE_CONTROL.value

      print("ACAN=", fingerprint[CAN.ACAN])

      if 0x210 in fingerprint[CAN.ACAN]:
        print("##### Radar Group 1 detected (0x210)")
        ret.extFlags |= HyundaiExtFlags.RADAR_GROUP1.value

      # detect HDA2 with ADAS Driving ECU
      if hda2:
        print("$$$CANFD HDA2")
        ret.flags |= HyundaiFlags.CANFD_HDA2.value
        if camera_scc > 0:
          if 0x110 in fingerprint[CAN.ACAN]:
            ret.flags |= HyundaiFlags.CANFD_HDA2_ALT_STEERING.value
            print("$$$CANFD ALT_STEERING1")
        else:
          if 0x110 in fingerprint[CAN.CAM]: # 0x110(272): LKAS_ALT
            ret.flags |= HyundaiFlags.CANFD_HDA2_ALT_STEERING.value
            print("$$$CANFD ALT_STEERING1")
          ## carrot_todo: sorento:
          if 0x2a4 not in fingerprint[CAN.CAM]: # 0x2a4(676): CAM_0x2a4
            ret.flags |= HyundaiFlags.CANFD_HDA2_ALT_STEERING.value
            print("$$$CANFD ALT_STEERING2")

        ## carrot: canival 4th, no 0x1cf
        if 0x1cf not in fingerprint[CAN.ECAN]: # 0x1cf(463): CRUISE_BUTTONS
          ret.flags |= HyundaiFlags.CANFD_ALT_BUTTONS.value
          print("$$$CANFD ALT_BUTTONS")
      else:
        # non-HDA2
        print("$$$CANFD non HDA2")
        if 0x1cf not in fingerprint[CAN.ECAN]:
          ret.flags |= HyundaiFlags.CANFD_ALT_BUTTONS.value
          print("$$$CANFD ALT_BUTTONS")
        #if not ret.flags & HyundaiFlags.RADAR_SCC:
        #  ret.flags |= HyundaiFlags.CANFD_CAMERA_SCC.value
        #  print("$$$CANFD CAMERA_SCC")
      # Some HDA2 cars have alternative messages for gear checks
      # ICE cars do not have 0x130; GEARS message on 0x40 or 0x70 instead
      if 0x40 in fingerprint[CAN.ECAN]:  # 0x40(64): GEAR_ALT
        ret.flags |= HyundaiFlags.CANFD_ALT_GEARS.value
        print("$$$CANFD ALT_GEARS")
      elif 69 in fingerprint[CAN.ECAN]:  # Special case
        ret.extFlags |= HyundaiExtFlags.CANFD_GEARS_69.value
        print("$$$CANFD GEARS_69")
      elif 112 in fingerprint[CAN.ECAN]:  # carrot: eGV70
        ret.flags |= HyundaiFlags.CANFD_ALT_GEARS_2.value
        print("$$$CANFD ALT_GEARS_2")
      elif 0x130 in fingerprint[CAN.ECAN]:  # 0x130(304): GEAR_SHIFTER
        print("$$$CANFD GEAR_SHIFTER present")
      else:
        ret.extFlags |= HyundaiExtFlags.CANFD_GEARS_NONE.value
        print("$$$CANFD GEARS_NONE")

      cfgs = [get_safety_config(structs.CarParams.SafetyModel.hyundaiCanfd), ]
      if CAN.ECAN >= 4:
        cfgs.insert(0, get_safety_config(structs.CarParams.SafetyModel.noOutput))
      ret.safetyConfigs = cfgs

      if ret.flags & HyundaiFlags.CANFD_HDA2:
        ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.CANFD_LKA_STEERING.value
        if ret.flags & HyundaiFlags.CANFD_HDA2_ALT_STEERING:
          ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.CANFD_LKA_STEERING_ALT.value
      if ret.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
        ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.CANFD_ALT_BUTTONS.value
      if ret.flags & HyundaiFlags.CANFD_CAMERA_SCC:
        ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.CAMERA_SCC.value

    else:
      # Shared configuration for non CAN-FD cars
      ret.alphaLongitudinalAvailable = True #candidate not in (UNSUPPORTED_LONGITUDINAL_CAR | CAMERA_SCC_CAR)
      ret.enableBsm = 0x58b in fingerprint[0]
      print(f"$$$ enableBsm = {ret.enableBsm}")

      # Send LFA message on cars with HDA
      if 0x485 in fingerprint[2]:
        ret.flags |= HyundaiFlags.SEND_LFA.value
        print("$$$SEND_LFA")

      # These cars use the FCA11 message for the AEB and FCW signals, all others use SCC12
      if 0x38d in fingerprint[0] or 0x38d in fingerprint[2]:
        ret.flags |= HyundaiFlags.USE_FCA.value
        print("$$$USE_FCA")

      if 0x2AB in fingerprint[0]:
        if params.get_int("EnableEscc") == 1:
          ret.spFlags |= HyundaiFlagsSP.SP_ENHANCED_SCC.value
          print("$$$ESCC")
        else:
          print("$$$User disable ESCC")

      if ret.flags & HyundaiFlags.LEGACY:
        # these cars require a special panda safety mode due to missing counters and checksums in the messages
        ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.hyundaiLegacy)]
        print("$$$Legacy Safety Model")
      else:
        ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.hyundai, 0)]

      if ret.flags & HyundaiFlags.CAMERA_SCC:
        ret.safetyConfigs[0].safetyParam |= HyundaiSafetyFlags.CAMERA_SCC.value
        print("$$$CAMERA_SCC")
      if ret.spFlags & HyundaiFlagsSP.SP_ENHANCED_SCC:
        ret.safetyConfigs[0].safetyParam |= HyundaiSafetyFlags.ESCC.value

    # Common lateral control setup

    ret.centerToFront = ret.wheelbase * 0.4
    ret.steerActuatorDelay = 0.1
    ret.steerLimitTimer = 0.4
    if ret.flags & HyundaiFlags.ANGLE_CONTROL:
      ret.steerControlType = SteerControlType.angle
    else:
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    if ret.flags & HyundaiFlags.ALT_LIMITS:
      ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.ALT_LIMITS.value

    # Common longitudinal control setup

    ret.radarUnavailable = RADAR_START_ADDR not in fingerprint[1] or Bus.radar not in DBC[ret.carFingerprint]
    ret.openpilotLongitudinalControl = alpha_long and ret.alphaLongitudinalAvailable

    # carrot, if camera_scc enabled, enable openpilotLongitudinalControl
    # -2 = VOACC 视觉纵向: 不启用雷达轨迹、不动雷达, 同样接管纵向。
    #      这样 radar-SCC 车(库斯图/伊兰特等)不必再靠把 HyundaiCameraSCC 设成非 0
    #      来换取纵向 —— 那个做法会让本车被误判成 camera-SCC 车, 一次性触发
    #      继电器故障 + Controls Mismatch + CAN Error 三个症状。 (照搬新版 CP 的做法)
    enable_radar_tracks = params.get_int("EnableRadarTracks")
    if ret.flags & HyundaiFlags.CAMERA_SCC.value or enable_radar_tracks > 0 or enable_radar_tracks == -2:
      ret.radarUnavailable = False
      ret.openpilotLongitudinalControl = True if camera_scc != 3 else False
      print(f"$$$OenpilotLongitudinalControl = True, CAMERA_SCC({ret.flags & HyundaiFlags.CAMERA_SCC.value}) or RadarTracks{enable_radar_tracks}")
    else:
      print(f"$$$OenpilotLongitudinalControl = {alpha_long}")

    #ret.radarUnavailable = False  # TODO: canfd... carrot, hyundai cars have radar

    if Bus.radar not in DBC[ret.carFingerprint]:
      if ret.spFlags & (HyundaiFlagsSP.SP_ENHANCED_SCC | HyundaiFlagsSP.SP_CAMERA_SCC_LEAD):
        ret.radarUnavailable = False
        print(f"$$$RadarTracks disable, Escc enable")

    ret.radarTimeStep = 0.05 if enable_radar_tracks > 0 or (ret.spFlags & HyundaiFlagsSP.SP_ENHANCED_SCC) else 0.02

    ret.pcmCruise = not ret.openpilotLongitudinalControl
    ret.startingState = False # True  # carrot
    ret.vEgoStarting = 0.1
    ret.startAccel = 1.0
    ret.longitudinalActuatorDelay = 0.5

    ret.longitudinalTuning.kpBP = [0.]
    ret.longitudinalTuning.kpV = [1.]
    ret.longitudinalTuning.kf = 1.0

    # *** feature detection ***
    if ret.flags & HyundaiFlags.CANFD:
      print(f"$$$$$ CanFD ECAN = {CAN.ECAN}")
      if 0x1fa in fingerprint[CAN.ECAN]:
        ret.extFlags |= HyundaiExtFlags.NAVI_CLUSTER.value
        print("$$$$ NaviCluster = True")
      else:
        print("$$$$ NaviCluster = False")

    else:
      if 1348 in fingerprint[0]:
        ret.extFlags |= HyundaiExtFlags.NAVI_CLUSTER.value
        print("$$$$ NaviCluster = True")
      if 1157 in fingerprint[0] or 1157 in fingerprint[2]:
        ret.extFlags |= HyundaiExtFlags.HAS_LFAHDA.value
        print("$$$$ HasLFAHDA")
      if 1007 in fingerprint[0]:
        print("#### cruiseButtonAlt")

    print(f"$$$$ enableBsm = {ret.enableBsm}")

    if ret.openpilotLongitudinalControl:
      ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.LONG.value
    if ret.flags & HyundaiFlags.HYBRID:
      ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.HYBRID_GAS.value
    elif ret.flags & HyundaiFlags.EV:
      ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.EV_GAS.value
    elif ret.flags & HyundaiFlags.FCEV:
      ret.safetyConfigs[-1].safetyParam |= HyundaiSafetyFlags.FCEV_GAS.value

    # Car specific configuration overrides

    if candidate == CAR.KIA_OPTIMA_G4_FL:
      ret.steerActuatorDelay = 0.2

    # Dashcam cars are missing a test route, or otherwise need validation
    # TODO: Optima Hybrid 2017 uses a different SCC12 checksum
    #ret.dashcamOnly = candidate in {CAR.KIA_OPTIMA_H, }

    return ret

  @staticmethod
  def init(CP, can_recv, can_send):

    Params().put('LongitudinalPersonalityMax', "4")

    #if CP.openpilotLongitudinalControl and not (CP.flags & HyundaiFlags.CANFD_CAMERA_SCC):
    if CP.openpilotLongitudinalControl and not ((CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value) or (CP.spFlags & HyundaiFlagsSP.SP_ENHANCED_SCC)):
      addr, bus = 0x7d0, 0
      if CP.flags & HyundaiFlags.CANFD_HDA2.value:
        addr, bus = 0x730, CanBus(CP).ECAN
      disable_ecu(can_recv, can_send, bus=bus, addr=addr, com_cont_req=b'\x28\x83\x01')

    params = Params()
    if params.get_int("EnableRadarTracks") > 0 and not CP.flags & HyundaiFlags.CANFD:
      result = enable_radar_tracks(CP, can_recv, can_send)
      params.put_bool("EnableRadarTracksResult", result)

    # for blinkers
    if CP.flags & HyundaiFlags.ENABLE_BLINKERS:
      disable_ecu(can_recv, can_send, bus=CanBus(CP).ECAN, addr=0x7B1, com_cont_req=b'\x28\x83\x01')

def enable_radar_tracks(CP, logcan, sendcan):
  from opendbc.car.isotp_parallel_query import IsoTpParallelQuery
  print("################ Try To Enable Radar Tracks ####################")

  ret = False
  sccBus = 2 if CP.flags & HyundaiFlags.CAMERA_SCC.value else 0
  rdr_fw = None
  rdr_fw_address = 0x7d0 #
  try:
    try:
      query = IsoTpParallelQuery(sendcan, logcan, sccBus, [rdr_fw_address], [b'\x10\x07'], [b'\x50\x07'])
      for addr, dat in query.get_data(0.1).items(): # pylint: disable=unused-variable
        print("ecu write data by id ...")
        new_config = b"\x00\x00\x00\x01\x00\x01"
        #new_config = b"\x00\x00\x00\x00\x00\x01"
        dataId = b'\x01\x42'
        WRITE_DAT_REQUEST = b'\x2e'
        WRITE_DAT_RESPONSE = b'\x68'
        query = IsoTpParallelQuery(sendcan, logcan, sccBus, [rdr_fw_address], [WRITE_DAT_REQUEST+dataId+new_config], [WRITE_DAT_RESPONSE])
        result = query.get_data(0)
        print("result=", result)
        ret = True
        break
    except Exception as e:
      print(f"Failed : {e}")
  except Exception as e:
    print("##############  Failed to enable tracks" + str(e))
  print("################ END Try to enable radar tracks")
  return ret
