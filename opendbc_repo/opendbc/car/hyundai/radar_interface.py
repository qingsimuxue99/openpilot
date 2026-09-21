import math

from opendbc.can import CANParser
from opendbc.car import Bus, structs
from opendbc.car.interfaces import RadarInterfaceBase
from opendbc.car.hyundai.values import DBC, HyundaiFlags, HyundaiExtFlags, HyundaiFlagsSP
from openpilot.common.params import Params
from opendbc.car.hyundai.hyundaicanfd import CanBus
from openpilot.common.filter_simple import MyMovingAverage

ESCC_TID = 1
SCC_TID = 0
RADAR_START_ADDR = 0x500
RADAR_MSG_COUNT = 32
RADAR_START_ADDR_CANFD1 = 0x210
RADAR_MSG_COUNT1 = 16
RADAR_START_ADDR_CANFD2 = 0x3A5 # Group 2, Group 1: 0x210 2개씩있어서 일단 보류.
RADAR_MSG_COUNT2 = 32

# POC for parsing corner radars: https://github.com/commaai/openpilot/pull/24221/

def get_radar_can_parser(CP, radar_tracks, escc, msg_start_addr, msg_count):
  if escc: #没有雷达DBC或者用户关了雷达跟踪
    lead_src, bus = "ESCC", 0
    messages = [(lead_src, 50)]
    print(f"get_radar_can_parser, lead_src={lead_src},bus={bus}")
    return CANParser(DBC[CP.carFingerprint][Bus.pt], messages, bus)

  if not radar_tracks:
    return None
  #if Bus.radar not in DBC[CP.carFingerprint]:
  #  return None
  print("RadarInterface: RadarTracks...")

  if CP.flags & HyundaiFlags.CANFD:
    CAN = CanBus(CP)
    messages = [(f"RADAR_TRACK_{addr:x}", 20) for addr in range(msg_start_addr, msg_start_addr + msg_count)]
    return CANParser('hyundai_canfd_radar_generated', messages, CAN.ACAN)
  else:
    messages = [(f"RADAR_TRACK_{addr:x}", 20) for addr in range(msg_start_addr, msg_start_addr + msg_count)]
  #return CANParser(DBC[CP.carFingerprint][Bus.radar], messages, 1)
    return CANParser('hyundai_kia_mando_front_radar_generated', messages, 1)

class Scc11DualBusParser:
  """非 CANFD 车的 SCC11(0x420) 解析器：同时订阅 ECAN 与 CAM 两条总线。

  为什么不能只挑一条:
  旧写法用 `CP.flags & HyundaiFlags.CAMERA_SCC` 猜总线, 而这个 flag 会被用户参数
  `HyundaiCameraSCC` 打开。一台前雷达发 SCC12 的车(库斯图/伊兰特等)一旦被该参数
  误设成 camera-SCC, 解析器就会去 bus2 找永远不存在的 SCC11, can_valid 恒为 False
    -> RadarData.errors.canError=True
    -> selfdrived 在屏幕上打出「CAN Error: Check Connections!!」。
  改为两条总线都订阅、以真的收到数据的那条为准 —— 与 safety 层
  `hyundai_scc12_seen_on_bus2` 完全同一个原则: 看物理事实, 不看配置推导。
  """

  MESSAGES = (("SCC11", 50),)

  def __init__(self, CP):
    CAN = CanBus(CP)
    dbc = DBC[CP.carFingerprint][Bus.pt]
    self.buses = [CAN.ECAN, CAN.CAM]
    self.parsers = [CANParser(dbc, list(self.MESSAGES), b) for b in self.buses]
    print(f"$$$rcp_scc: 订阅 SCC11 于 bus{self.buses}, 取真有数据者 (不再依赖 HyundaiCameraSCC)")

  def update(self, can_strings):
    updated = set()
    for p in self.parsers:
      updated |= set(p.update(can_strings))
    return updated

  @property
  def can_valid(self):
    return any(p.can_valid for p in self.parsers)

  @property
  def vl(self):
    for p in self.parsers:
      if p.can_valid:
        return p.vl
    return self.parsers[0].vl


def get_radar_can_parser_scc(CP):
  CAN = CanBus(CP)
  if CP.flags & HyundaiFlags.CANFD:
    messages = [("SCC_CONTROL", 50)]
    bus = CAN.CAM if CP.flags & HyundaiFlags.CAMERA_SCC else CAN.ECAN
    print("$$$rcp_scc: CANFD, bus = ", bus)
    return CANParser(DBC[CP.carFingerprint][Bus.pt], messages, bus)

  # 传统 CAN: 本车 SCC11 到底在 ECAN 还是 CAM, 属于物理事实, 由总线数据说了算。
  return Scc11DualBusParser(CP)

class RadarInterface(RadarInterfaceBase):
  def __init__(self, CP):
    super().__init__(CP)

    self.canfd = True if CP.flags & HyundaiFlags.CANFD else False
    self.radar_group1 = False
    if self.canfd:
      if CP.extFlags & HyundaiExtFlags.RADAR_GROUP1.value:
        self.radar_start_addr = RADAR_START_ADDR_CANFD1
        self.radar_msg_count = RADAR_MSG_COUNT1
        self.radar_group1 = True
      else:
        self.radar_start_addr = RADAR_START_ADDR_CANFD2
        self.radar_msg_count = RADAR_MSG_COUNT2
    else:
      self.radar_start_addr = RADAR_START_ADDR
      self.radar_msg_count = RADAR_MSG_COUNT

    self.params = Params()
    self.radar_tracks = self.params.get_int("EnableRadarTracks") >= 1
    #new
    self.showDebugLog = self.params.get_int("ShowDebugLog")
    self.enhanced_scc = (CP.spFlags & HyundaiFlagsSP.SP_ENHANCED_SCC) and (Bus.radar not in DBC[CP.carFingerprint] or not self.radar_tracks)
    print(f"$$$radar_tracks={self.radar_tracks}, enhanced_scc={self.enhanced_scc}")
    #new
    self.updated_tracks = set()
    self.updated_scc = set()
    self.rcp_tracks = get_radar_can_parser(CP, self.radar_tracks, self.enhanced_scc, self.radar_start_addr, self.radar_msg_count)
    # ESCC 模式, 或「雷达轨迹 + 传统CAN + 非 camera-SCC」时, bus0 上不再有车辆侧的
    # SCC11(0x420)(被 OP 自己发出的 SCC11/SCC12, 或雷达轨迹模式取代), 继续订阅会让
    # 该 CANParser 恒 can_valid=False -> RadarData.errors.canError=True
    # -> 屏幕「CAN Error: Check Connections!!」。这类配置不建 SCC parser。
    # (与新版 CP 的 use_scc_parser 防护一致)
    use_scc_parser = (not self.enhanced_scc and
                      not (self.radar_tracks and not self.canfd and not (CP.flags & HyundaiFlags.CAMERA_SCC)))
    self.rcp_scc = get_radar_can_parser_scc(CP) if use_scc_parser else None
    print(f"$$$use_scc_parser={use_scc_parser} (enhanced_scc={self.enhanced_scc}, "
          f"radar_tracks={self.radar_tracks}, canfd={self.canfd}, "
          f"CAMERA_SCC={bool(CP.flags & HyundaiFlags.CAMERA_SCC)})")
    self.trigger_msg_scc = 416 if self.canfd else 0x420

    self.trigger_msg_tracks = self.radar_start_addr + self.radar_msg_count - 1
    self.track_id = 0

    self.radar_off_can = CP.radarUnavailable
    #new
    if self.rcp_tracks is None:
      print("$$$self.rcp_tracks = get_radar_can_parser() is None")
    else:
      print("$$$self.rcp_tracks = get_radar_can_parser() success")
      if self.enhanced_scc:
        self.trigger_msg_tracks = 683
    if self.rcp_scc is None:
      print("$$$self.rcp_scc = get_radar_can_parser_scc() is None")
    else:
      print("$$$self.rcp_scc = get_radar_can_parser_scc() success")
    #new

    self.vRel_last = 0
    self.dRel_last = 0

    # Initialize pts
    total_tracks = self.radar_msg_count * ( 2 if self.radar_group1 else 1)
    for track_id in range(total_tracks):
      t_id = track_id + 32
      self.pts[t_id] = structs.RadarData.RadarPoint()
      self.pts[t_id].measured = False
      self.pts[t_id].trackId = t_id

    self.pts[SCC_TID] = structs.RadarData.RadarPoint()
    self.pts[SCC_TID].trackId = SCC_TID

    self.pts[ESCC_TID] = structs.RadarData.RadarPoint()
    self.pts[ESCC_TID].trackId = ESCC_TID

    self.frame = 0


  def update(self, can_strings):
    self.frame += 1
    if self.radar_off_can or (self.rcp_tracks is None and self.rcp_scc is None):
      return super().update(None)

    if self.rcp_scc is not None:
      vls_s = self.rcp_scc.update(can_strings)
      self.updated_scc.update(vls_s)
      if not self.radar_tracks and not self.enhanced_scc and self.frame % 5 == 0:
        self._update_scc(self.updated_scc)
        self.updated_scc.clear()
        ret = structs.RadarData()
        if not self.rcp_scc.can_valid:
          ret.errors.canError = True
        ret.points = list(self.pts.values())
        return ret
    if (self.radar_tracks or self.enhanced_scc) and self.rcp_tracks is not None:
      vls_t = self.rcp_tracks.update(can_strings)
      self.updated_tracks.update(vls_t)
      if self.trigger_msg_tracks in self.updated_tracks:
        self._update(self.updated_tracks)
        self._update_scc(self.updated_scc)
        self.updated_scc.clear()
        self.updated_tracks.clear()
        ret = structs.RadarData()
        if not self.rcp_tracks.can_valid:
          ret.errors.canError = True
        ret.points = list(self.pts.values())
        return ret

    return None

  def _update(self, updated_messages):
    if self.enhanced_scc:  # 如果检测到ESCC，则使用ESCC的雷达数据
      msg = self.rcp_tracks.vl["ESCC"]
      valid = msg['ACC_ObjStatus'] and msg['ACC_ObjDist'] < 204.6

      ii = ESCC_TID
      if valid:
        self.pts[ii].measured = True
        self.pts[ii].trackId = ESCC_TID
        self.pts[ii].dRel = msg['ACC_ObjDist']
        self.pts[ii].yRel = -msg['ACC_ObjLatPos']
        self.pts[ii].vRel = msg['ACC_ObjRelSpd']
        self.pts[ii].vLead = self.pts[ii].vRel + self.v_ego
        self.pts[ii].aRel = 0.0
        self.pts[ii].yvRel = 0.0

        if (self.showDebugLog & 128) > 0:
          print(f"***update escc: ACC_ObjStatus: {msg['ACC_ObjStatus']}, "
                f"pts[{ii}]: dRel={self.pts[ii].dRel}, yRel={self.pts[ii].yRel}, "
                f"vRel={self.pts[ii].vRel}, aRel={self.pts[ii].aRel}, yvRel={self.pts[ii].yvRel}")
      else:
        # key 已经存在，只需标记为 invalid
        self.pts[ii].measured = False
        self.pts[ii].dRel = 0
        self.pts[ii].yRel = 0
        self.pts[ii].vRel = 0
        self.pts[ii].vLead = 0
        self.pts[ii].aRel = float('nan')
        self.pts[ii].yvRel = 0
        if (self.showDebugLog & 128) > 0:
          print(f"mark pts[{ii}] invalid")

    else: #雷达跟踪数据
      t_id = 32
      for addr in range(self.radar_start_addr, self.radar_start_addr + self.radar_msg_count):

        msg = self.rcp_tracks.vl[f"RADAR_TRACK_{addr:x}"]

        if self.radar_group1:
          valid = msg['VALID_CNT1'] > 10
        elif self.canfd:
          valid = msg['VALID_CNT'] > 10
        else:
          valid = msg['STATE'] in (3, 4)

        self.pts[t_id].measured = bool(valid)
        if not valid:
          self.pts[t_id].dRel = 0
          self.pts[t_id].yRel = 0
          self.pts[t_id].vRel = 0
          self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
          self.pts[t_id].aRel = float('nan')
          self.pts[t_id].yvRel = 0
        elif self.radar_group1:
          self.pts[t_id].dRel = msg['LONG_DIST1']
          self.pts[t_id].yRel = msg['LAT_DIST1']
          self.pts[t_id].vRel = msg['REL_SPEED1']
          self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
          self.pts[t_id].aRel = msg['REL_ACCEL1']
          self.pts[t_id].yvRel = msg['LAT_SPEED1']
        elif self.canfd:
          self.pts[t_id].dRel = msg['LONG_DIST']
          self.pts[t_id].yRel = msg['LAT_DIST']
          self.pts[t_id].vRel = msg['REL_SPEED']
          self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
          self.pts[t_id].aRel = msg['REL_ACCEL']
          self.pts[t_id].yvRel = msg['LAT_SPEED']
        else:
          azimuth = math.radians(msg['AZIMUTH'])
          self.pts[t_id].dRel = math.cos(azimuth) * msg['LONG_DIST']
          self.pts[t_id].yRel = 0.5 * -math.sin(azimuth) * msg['LONG_DIST']
          self.pts[t_id].vRel = msg['REL_SPEED']
          self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
          self.pts[t_id].aRel = msg['REL_ACCEL']
          self.pts[t_id].yvRel = 0.0

        t_id += 1
      # radar group1은 하나의 msg에 2개의 레이더가 들어있음.
      if self.radar_group1:
        for addr in range(self.radar_start_addr, self.radar_start_addr + self.radar_msg_count):
          msg = self.rcp_tracks.vl[f"RADAR_TRACK_{addr:x}"]

          valid = msg['VALID_CNT2'] > 10
          self.pts[t_id].measured = bool(valid)
          if not valid:
            self.pts[t_id].dRel = 0
            self.pts[t_id].yRel = 0
            self.pts[t_id].vRel = 0
            self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
            self.pts[t_id].aRel = float('nan')
            self.pts[t_id].yvRel = 0
          else:
            self.pts[t_id].dRel = msg['LONG_DIST2']
            self.pts[t_id].yRel = msg['LAT_DIST2']
            self.pts[t_id].vRel = msg['REL_SPEED2']
            self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
            self.pts[t_id].aRel = msg['REL_ACCEL2']
            self.pts[t_id].yvRel = msg['LAT_SPEED2']

          t_id += 1

  def _update_scc(self, updated_messages):
    if self.rcp_scc is None:
      # use_scc_parser=False 的配置(ESCC / 雷达轨迹)下没有 SCC parser, 直接跳过
      return
    cpt = self.rcp_scc.vl
    t_id = SCC_TID
    if self.canfd:
      dRel = cpt["SCC_CONTROL"]['ACC_ObjDist']
      vRel = cpt["SCC_CONTROL"]['ACC_ObjRelSpd']
      new_pts = abs(dRel - self.dRel_last) > 3 or abs(vRel - self.vRel_last) > 1
      vLead = vRel + self.v_ego
      valid = 0 < dRel < 150 and not new_pts #cpt["SCC_CONTROL"]['OBJ_STATUS'] and dRel < 150
      self.pts[t_id].measured = bool(valid)
      if not valid:
        self.pts[t_id].dRel = 0
        self.pts[t_id].yRel = 0
        self.pts[t_id].vRel = 0
        self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
        self.pts[t_id].aRel = float('nan')
        self.pts[t_id].yvRel = 0
      else:
        self.pts[t_id].dRel = dRel
        self.pts[t_id].yRel = 0
        self.pts[t_id].vRel = vRel
        self.pts[t_id].vLead = vLead
        self.pts[t_id].aRel = float('nan')
        self.pts[t_id].yvRel = 0 #float('nan')
    else:
      dRel = cpt["SCC11"]['ACC_ObjDist']
      vRel = cpt["SCC11"]['ACC_ObjRelSpd']
      new_pts = abs(dRel - self.dRel_last) > 3 or abs(vRel - self.vRel_last) > 1
      vLead = vRel + self.v_ego
      valid = cpt["SCC11"]['ACC_ObjStatus'] and dRel < 150 and not new_pts
      self.pts[t_id].measured = bool(valid)
      if not valid:
        self.pts[t_id].dRel = 0
        self.pts[t_id].yRel = 0
        self.pts[t_id].vRel = 0
        self.pts[t_id].vLead = self.pts[t_id].vRel + self.v_ego
        self.pts[t_id].aRel = float('nan')
        self.pts[t_id].yvRel = 0
      else:
        self.pts[t_id].dRel = dRel
        self.pts[t_id].yRel = -cpt["SCC11"]['ACC_ObjLatPos']  # in car frame's y axis, left is negative
        self.pts[t_id].vRel = vRel
        self.pts[t_id].vLead = vLead
        self.pts[t_id].aRel = float('nan')
        self.pts[t_id].yvRel = 0 #float('nan')

    self.dRel_last = dRel
    self.vRel_last = vRel
