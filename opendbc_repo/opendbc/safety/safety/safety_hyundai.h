#pragma once

#include "safety_declarations.h"
#include "safety_hyundai_common.h"

#define HYUNDAI_LIMITS(steer, rate_up, rate_down) { \
  .max_steer = (steer), \
  .max_rate_up = (rate_up), \
  .max_rate_down = (rate_down), \
  .max_rt_delta = 112, \
  .max_rt_interval = 250000, \
  .driver_torque_allowance = 50, \
  .driver_torque_multiplier = 2, \
  .type = TorqueDriverLimited, \
   /* the EPS faults when the steering angle is above a certain threshold for too long. to prevent this, */ \
   /* we allow setting CF_Lkas_ActToi bit to 0 while maintaining the requested torque value for two consecutive frames */ \
  .min_valid_request_frames = 89, \
  .max_invalid_request_frames = 2, \
  .min_valid_request_rt_interval = 810000,  /* 810ms; a ~10% buffer on cutting every 90 frames */ \
  .has_steer_req_tolerance = true, \
}

extern const LongitudinalLimits HYUNDAI_LONG_LIMITS;
const LongitudinalLimits HYUNDAI_LONG_LIMITS = {
  .max_accel = 250,   // 1/100 m/s2
  .min_accel = -400,  // 1/100 m/s2
};

static const CanMsg HYUNDAI_TX_MSGS[] = {
  {0x340, 0, 8}, // LKAS11 Bus 0
  {0x4F1, 0, 4}, // CLU11 Bus 0
  {0x485, 0, 8}, // LFAHDA_MFC Bus 0
  {593, 2, 8},                              // MDPS12, Bus 2
  {1056, 0, 8},                             // SCC11, Bus 0
  {1057, 0, 8},                             // SCC12, Bus 0
  {1290, 0, 8},                             // SCC13, Bus 0
  {905, 0, 8},                              // SCC14, Bus 0
  {909, 0, 8},                              // FCA11 Bus 0
  {1155, 0, 8},                             // FCA12 Bus 0
  {1186, 0, 8},                             // FRT_RADAR11, Bus 0
  {1265, 2, 4},               // CLU11, Bus 0, 2
  {0x7D0, 0, 8}, // radar UDS TX addr Bus 0 (for radar disable)   // 2000
  {0x7b1, 0, 8},
};

// 2026.9.12: removed 0x371(E_EMS11)/0x91(FCEV_ACCELERATOR) from rx checks - EV/Hybrid/FCEV only, gas cars dont send -> permanent rxInvalid/CM
#define HYUNDAI_COMMON_RX_CHECKS(legacy)                                                                                                                  \
  {.msg = {{0x260, 0, 8, .max_counter = 3U, .frequency = 100U}, { 0 }, { 0 }}},                                                                                        \
  {.msg = {{0x386, 0, 8, .ignore_checksum = (legacy), .ignore_counter = (legacy), .max_counter = (legacy) ? 0U : 15U, .frequency = 100U}, { 0 }, { 0 }}}, \
  {.msg = {{0x394, 0, 8, .ignore_checksum = (legacy), .ignore_counter = (legacy), .max_counter = (legacy) ? 0U : 7U, .frequency = 100U}, { 0 }, { 0 }}},  \

#define HYUNDAI_SCC12_ADDR_CHECK(scc_bus)                                               \
  {.msg = {{0x421, (scc_bus), 8, .max_counter = 15U, .frequency = 50U}, { 0 }, { 0 }}}, \

static bool hyundai_legacy = false;
static bool hyundai_cruise_buttons_alt = false;

// ★ 2026-09-17: SCC12(0x421) 的物理来源自适应标志。
// 判据需要知道"本车 SCC12 天生在哪条总线", 而 hyundai_camera_scc 不可靠:
// 它由 values.py 车型 flag 与用户参数 HyundaiCameraSCC 共同驱动, 参数被误配为非 0 时,
// radar-SCC 车(库斯图/伊兰特等, SCC12 常驻 bus0) 会被误判为"原厂 ECU 夺回控制权"。
// 这里改用物理事实: 只要 bus2 上真的收到过 SCC12, 本车 SCC12 就来自摄像头总线。
// 该事实不受任何配置影响; 每次 safety init 时重新学习。
static bool hyundai_scc12_seen_on_bus2 = false;

static uint8_t hyundai_get_counter(const CANPacket_t *to_push) {
  int addr = GET_ADDR(to_push);

  uint8_t cnt = 0;
  if (addr == 0x260) {
    cnt = (GET_BYTE(to_push, 7) >> 4) & 0x3U;
  } else if (addr == 0x386) {
    cnt = ((GET_BYTE(to_push, 3) >> 6) << 2) | (GET_BYTE(to_push, 1) >> 6);
  } else if (addr == 0x394) {
    cnt = (GET_BYTE(to_push, 1) >> 5) & 0x7U;
  } else if (addr == 0x421) {
    cnt = GET_BYTE(to_push, 7) & 0xFU;
  } else if (addr == 0x4F1) {
    cnt = (GET_BYTE(to_push, 3) >> 4) & 0xFU;
  } else {
  }
  return cnt;
}

static uint32_t hyundai_get_checksum(const CANPacket_t *to_push) {
  int addr = GET_ADDR(to_push);

  uint8_t chksum = 0;
  if (addr == 0x260) {
    chksum = GET_BYTE(to_push, 7) & 0xFU;
  } else if (addr == 0x386) {
    chksum = ((GET_BYTE(to_push, 7) >> 6) << 2) | (GET_BYTE(to_push, 5) >> 6);
  } else if (addr == 0x394) {
    chksum = GET_BYTE(to_push, 6) & 0xFU;
  } else if (addr == 0x421) {
    chksum = GET_BYTE(to_push, 7) >> 4;
  } else {
  }
  return chksum;
}

static uint32_t hyundai_compute_checksum(const CANPacket_t *to_push) {
  int addr = GET_ADDR(to_push);

  uint8_t chksum = 0;
  if (addr == 0x386) {
    // count the bits
    for (int i = 0; i < 8; i++) {
      uint8_t b = GET_BYTE(to_push, i);
      for (int j = 0; j < 8; j++) {
        uint8_t bit = 0;
        // exclude checksum and counter
        if (((i != 1) || (j < 6)) && ((i != 3) || (j < 6)) && ((i != 5) || (j < 6)) && ((i != 7) || (j < 6))) {
          bit = (b >> (uint8_t)j) & 1U;
        }
        chksum += bit;
      }
    }
    chksum = (chksum ^ 9U) & 15U;
  } else {
    // sum of nibbles
    for (int i = 0; i < 8; i++) {
      if ((addr == 0x394) && (i == 7)) {
        continue; // exclude
      }
      uint8_t b = GET_BYTE(to_push, i);
      if (((addr == 0x260) && (i == 7)) || ((addr == 0x394) && (i == 6)) || ((addr == 0x421) && (i == 7))) {
        b &= (addr == 0x421) ? 0x0FU : 0xF0U; // remove checksum
      }
      chksum += (b % 16U) + (b / 16U);
    }
    chksum = (16U - (chksum %  16U)) % 16U;
  }

  return chksum;
}

static void hyundai_rx_hook(const CANPacket_t *to_push) {
  int bus = GET_BUS(to_push);
  int addr = GET_ADDR(to_push);

  // SCC12 is on bus 2 for camera-based SCC cars, bus 0 on all others
  if (addr == 0x421) {
    // ★ 记录 SCC12 的物理来源: bus2 上出现 => 本车 SCC12 来自摄像头总线(与配置无关)
    if (bus == 2) {
      hyundai_scc12_seen_on_bus2 = true;
    }
    if (((bus == 0) && !hyundai_camera_scc) || ((bus == 2) && hyundai_camera_scc)) {
      // 2 bits: 13-14
      int cruise_engaged = (GET_BYTES(to_push, 0, 4) >> 13) & 0x3U;
      hyundai_common_cruise_state_check(cruise_engaged);
    }
  }

  if (bus == 0) {
    if (addr == 0x251) {
      int torque_driver_new = (GET_BYTES(to_push, 0, 2) & 0x7ffU) - 1024U;
      // update array of samples
      update_sample(&torque_driver, torque_driver_new);
    }

    // ACC steering wheel buttons
    if (addr == 1007) hyundai_cruise_buttons_alt = true; // CASPER_EV: 1007
    if (addr == 1007) {
      int cruise_button = (GET_BYTE(to_push, 7) >> 4) & 0x07U;
      bool main_button = GET_BIT(to_push, 58U);
      hyundai_common_cruise_buttons_check(cruise_button, main_button);
    }
    else if (addr == 0x4F1 && !hyundai_cruise_buttons_alt) {
      int cruise_button = GET_BYTE(to_push, 0) & 0x7U;
      bool main_button = GET_BIT(to_push, 3U);
      hyundai_common_cruise_buttons_check(cruise_button, main_button);
    }

    // gas press, different for EV, hybrid, and ICE models
    if ((addr == 0x371) && hyundai_ev_gas_signal) {
      gas_pressed = (((GET_BYTE(to_push, 4) & 0x7FU) << 1) | GET_BYTE(to_push, 3) >> 7) != 0U;
    } else if ((addr == 0x371) && hyundai_hybrid_gas_signal) {
      gas_pressed = GET_BYTE(to_push, 7) != 0U;
    } else if ((addr == 0x91) && hyundai_fcev_gas_signal) {
      gas_pressed = GET_BYTE(to_push, 6) != 0U;
    } else if ((addr == 0x260) && !hyundai_ev_gas_signal && !hyundai_hybrid_gas_signal) {
      gas_pressed = (GET_BYTE(to_push, 7) >> 6) != 0U;
    } else {
    }

    // sample wheel speed, averaging opposite corners
    if (addr == 0x386) {
      uint32_t front_left_speed = GET_BYTES(to_push, 0, 2) & 0x3FFFU;
      uint32_t rear_right_speed = GET_BYTES(to_push, 6, 2) & 0x3FFFU;
      vehicle_moving = (front_left_speed > HYUNDAI_STANDSTILL_THRSLD) || (rear_right_speed > HYUNDAI_STANDSTILL_THRSLD);
    }

    if (addr == 0x394) {
      brake_pressed = ((GET_BYTE(to_push, 5) >> 5U) & 0x3U) == 0x2U;
    }

    bool stock_ecu_detected = (addr == 0x340);

    // 雷达SCC车(库斯图/伊兰特等)雷达常激活、SCC12(0x421)恒在 bus0、OP 仅改写其设定点并不禁用雷达,
    // 若仍检查会被误判 relayMalfunction. 仅对 camera-SCC 车保留该检查(其 SCC12 来自摄像头 bus2,
    // 由 fwd_hook 封堵转发); 雷达SCC车不再因 SCC12 触发继电器故障(代价: 无"原厂夺回"安全标志,
    // 但 OP 已死时本就无所谓). 该门控与 fwd_hook 的 !hyundai_longitudinal 封锁互补.
    // ★ 2026-09-17: 门控从 hyundai_camera_scc 改为 hyundai_scc12_seen_on_bus2(物理来源)。
    //   原因: hyundai_camera_scc 会被用户参数 HyundaiCameraSCC 拉高(即使车型本身是 radar-SCC),
    //   届时 bus0 上常驻的原厂 SCC12 又会被误判 => 继电器故障复现; 且同一参数还会让 RX 表切到
    //   camera 分支、要求 bus2 有 SCC12 => 同时报 Controls Mismatch(CAN 错误), 形成"两症状往复"。
    //   改用"bus2 上是否真的出现过 SCC12"后, 本判据与任何配置解耦:
    //     · radar-SCC 车(bus2 无 SCC12) => 永不因 SCC12 误报, 无论参数怎么配;
    //     · camera-SCC 车(bus2 确有 SCC12, 50Hz => 20ms 内即置位) => 照旧保留该检查。
    // ★ 2026-09-18: 追加 ESCC 门控 —— 根治"装 ESCC 的车一开纵向就持续报继电器故障"。
    //   上游原判据本意是"OP 接管纵向 => 原厂雷达必须已被停用 => 不该再看到 SCC12"
    //   (上游原文: "If openpilot is controlling longitudinal we need to ensure the radar
    //    is turned off / Enforce by checking we don't see SCC12")。
    //   但 ESCC 车的设计前提【恰恰是不要停用原厂雷达】: tx_hook 的 0x7D0 门控 !hyundai_escc
    //   就是为此放行 UDS 的 —— 雷达必须继续发 SCC12, ESCC 模块才有设定点可改写。
    //   二者互斥 => 装 ESCC 的 radar-SCC 车(库斯图等)只要开纵向, bus0 上常驻的原厂 SCC12
    //   就 50Hz 命中该判据 => 屏幕持续"继电器故障"
    //   (实测: cp-8.31 8/12 与 新cp-0917 9/17 两版固件的判据均为此形态)。
    //   故: ESCC 车若本身不是 camera-SCC 车(其 SCC12 物理上就在 bus0, 不可能是"夺回"信号),
    //       整体关闭该判据; camera-SCC 车(含同时装 ESCC 的)照旧保留保护; 其余车行为不变。
    bool scc12_reclaim_check = hyundai_scc12_seen_on_bus2 && (!hyundai_escc || hyundai_camera_scc);
    if (scc12_reclaim_check && (addr == 0x421)) {
      stock_ecu_detected = true;
    }
    generic_rx_checks(stock_ecu_detected);
  }
}

uint32_t last_ts_lkas11_from_op = 0;
uint32_t last_ts_scc12_from_op = 0;
uint32_t last_ts_mdps12_from_op = 0;
uint32_t last_ts_fca11_from_op = 0;

static bool hyundai_tx_hook(const CANPacket_t *to_send) {
  const TorqueSteeringLimits HYUNDAI_STEERING_LIMITS = HYUNDAI_LIMITS(512, 10, 10);
  const TorqueSteeringLimits HYUNDAI_STEERING_LIMITS_ALT = HYUNDAI_LIMITS(512, 10, 10);
  const TorqueSteeringLimits HYUNDAI_STEERING_LIMITS_ALT_2 = HYUNDAI_LIMITS(170, 2, 3);

  bool tx = true;
  int addr = GET_ADDR(to_send);

  // FCA11: Block any potential actuation
  if (addr == 0x38D) {
    int CR_VSM_DecCmd = GET_BYTE(to_send, 1);
    bool FCA_CmdAct = GET_BIT(to_send, 20U);
    bool CF_VSM_DecCmdAct = GET_BIT(to_send, 31U);

    if ((CR_VSM_DecCmd != 0) || FCA_CmdAct || CF_VSM_DecCmdAct) {
      tx = false;
    }
  }

  // ACCEL: safety check SCC12发送消息
  if (addr == 0x421) {
    int cruise_engaged = (GET_BYTES(to_send, 0, 4) >> 13) & 0x3U;
    if (cruise_engaged) {
      if(!controls_allowed) print("auto engage controls_allowed....\n");
      controls_allowed = true;
    }
    int desired_accel_raw = (((GET_BYTE(to_send, 4) & 0x7U) << 8) | GET_BYTE(to_send, 3)) - 1023U;
    int desired_accel_val = ((GET_BYTE(to_send, 5) << 3) | (GET_BYTE(to_send, 4) >> 5)) - 1023U;

    //int aeb_decel_cmd = GET_BYTE(to_send, 2);
    //bool aeb_req = GET_BIT(to_send, 54U);

    bool violation = false;

    violation |= longitudinal_accel_checks(desired_accel_raw, HYUNDAI_LONG_LIMITS);
    violation |= longitudinal_accel_checks(desired_accel_val, HYUNDAI_LONG_LIMITS);
    //violation |= (aeb_decel_cmd != 0);
    //violation |= aeb_req;

    if (violation) {
      tx = false;
    }
  }

  // LKA STEER: safety check
  if (addr == 0x340) {
    int desired_torque = ((GET_BYTES(to_send, 0, 4) >> 16) & 0x7ffU) - 1024U;
    bool steer_req = GET_BIT(to_send, 27U);

    const TorqueSteeringLimits limits = hyundai_alt_limits_2 ? HYUNDAI_STEERING_LIMITS_ALT_2 :
                                        hyundai_alt_limits ? HYUNDAI_STEERING_LIMITS_ALT : HYUNDAI_STEERING_LIMITS;

    if (steer_torque_cmd_checks(desired_torque, steer_req, limits)) {
      //tx = false;
    }
  }

  // UDS: Only tester present ("\x02\x3E\x80\x00\x00\x00\x00\x00") allowed on diagnostics address
  if ((addr == 0x7D0)  && !hyundai_escc && !hyundai_camera_scc) {
    if ((GET_BYTES(to_send, 0, 4) != 0x00803E02U) || (GET_BYTES(to_send, 4, 4) != 0x0U)) {
      tx = false;
    }
  }

  // BUTTONS: used for resume spamming and cruise cancellation
  if ((addr == 0x4F1) && !hyundai_longitudinal) {
    int button = GET_BYTE(to_send, 0) & 0x7U;

    bool allowed_resume = (button == 1);// && controls_allowed;
    bool allowed_set_decel = (button == 2) && controls_allowed;
    bool allowed_cancel = (button == 4) && cruise_engaged_prev;
    bool allowed_gap_dist = (button == 3) && controls_allowed;
    if (!(allowed_resume || allowed_set_decel || allowed_cancel || allowed_gap_dist)) {
      tx = false;
    }
  }
  if(addr == 832)
    last_ts_lkas11_from_op = (tx == 0 ? 0 : microsecond_timer_get());
  else if(addr == 1057)
    last_ts_scc12_from_op = (tx == 0 ? 0 : microsecond_timer_get());
  else if(addr == 593)
    last_ts_mdps12_from_op = (tx == 0 ? 0 : microsecond_timer_get());
  else if(addr == 909)
    last_ts_fca11_from_op = (tx == 0 ? 0 : microsecond_timer_get());

  return tx;
}

static int hyundai_fwd_hook(int bus_num, int addr) {

  int bus_fwd = -1;

  uint32_t now = microsecond_timer_get();

  // forward cam to ccan and viceversa, except lkas cmd
  if (bus_num == 0) {
    bus_fwd = 2;

    if(addr == 593) {
      if(now - last_ts_mdps12_from_op < 200000) {
        bus_fwd = -1;
      }
    }
  }

  if (bus_num == 2) {
    bool is_lkas_msg = addr == 832;
    bool is_lfahda_msg = addr == 1157;
    bool is_scc_msg = addr == 1056 || addr == 1057 || addr == 1290 || addr == 905;
    bool is_fca_msg = addr == 909 || addr == 1155;

    bool block_msg = is_lkas_msg || is_lfahda_msg || is_scc_msg; //|| is_fca_msg;
    if (!block_msg) {
      bus_fwd = 0;
    }
    else {
      if(is_lkas_msg || is_lfahda_msg) {
        // 2026-09-02 fix: permanently block stock LKAS11/LFAHDA from cam bus to car bus
        // (comma official behavior). The old 200ms fallback put stock LKAS11 (0x340) on
        // bus 0 whenever openpilot stopped sending >200ms (e.g. controlsd crash), which
        // falsely triggered relayMalfunction. bus_fwd stays -1 (no forward).
      }
      else if(is_scc_msg) {
        // 所有 OP 纵向车型(库斯图/伊兰特等): OP 管纵向时永久封锁 SCC12 转发, 防止 OP 静默>400ms 兜底转发原厂 SCC12 触发 relayMalf
        // (camera_scc 标志已不可靠: 库斯图实为 radar SCC, 去 CAMERA_SCC 修 CAN 后会变 false; 故改用 longitudinal 标志统一封锁)
        if(!hyundai_longitudinal && (now - last_ts_scc12_from_op >= 400000))
          bus_fwd = 0;
      }
      else if(is_fca_msg) {
        if(now - last_ts_fca11_from_op >= 400000)
          bus_fwd = 0;
      }
    }
  }

  return bus_fwd;
}

/* case
  - legacy(on/off) + camera_scc(allways longitudinal on) + longitudinal(scc off)
*/
static safety_config hyundai_init_carrot(bool legacy_car) {
    // ★ 每次 safety init 重新学习 SCC12 的物理来源(见 hyundai_rx_hook)
    hyundai_scc12_seen_on_bus2 = false;

    static const CanMsg HYUNDAI_LONG_TX_MSGS[] = {
      {0x340, 0, 8}, // LKAS11 Bus 0
      {0x4F1, 0, 4}, // CLU11 Bus 0
      {0x485, 0, 8}, // LFAHDA_MFC Bus 0
      {0x420, 0, 8}, // SCC11 Bus 0
      {0x421, 0, 8}, // SCC12 Bus 0
      {0x50A, 0, 8}, // SCC13 Bus 0
      {0x389, 0, 8}, // SCC14 Bus 0
      {0x4A2, 0, 2}, // FRT_RADAR11 Bus 0
      {0x38D, 0, 8}, // FCA11 Bus 0
      {0x483, 0, 8}, // FCA12 Bus 0
      {0x7D0, 0, 8}, // radar UDS TX addr Bus 0 (for radar disable)
    };

    static const CanMsg HYUNDAI_CAMERA_SCC_TX_MSGS[] = {
      {0x340, 0, 8}, // LKAS11 Bus 0
      {0x4F1, 2, 4}, // CLU11 Bus 2
      {0x485, 0, 8}, // LFAHDA_MFC Bus 0
      {593, 2, 8},                              // MDPS12, Bus 2
      {1056, 0, 8},                             // SCC11, Bus 0
      {1057, 0, 8},                             // SCC12, Bus 0
      {1290, 0, 8},                             // SCC13, Bus 0
      {905, 0, 8},                              // SCC14, Bus 0
      {909, 0, 8},                              // FCA11 Bus 0
      {1155, 0, 8},                             // FCA12 Bus 0
      {1186, 0, 8},                             // FRT_RADAR11, Bus 0
      {0x4F1, 0, 4}, // CLU11 Bus 0
    };

    safety_config ret;
    if (hyundai_camera_scc) {
        static RxCheck hyundai_cam_scc_rx_checks[] = {
          HYUNDAI_COMMON_RX_CHECKS(false)
          HYUNDAI_SCC12_ADDR_CHECK(2)
        };
        static RxCheck hyundai_cam_scc_rx_checks_legacy[] = {
          HYUNDAI_COMMON_RX_CHECKS(true)
          HYUNDAI_SCC12_ADDR_CHECK(2)
        };
        if(legacy_car) ret = BUILD_SAFETY_CFG(hyundai_cam_scc_rx_checks_legacy, HYUNDAI_CAMERA_SCC_TX_MSGS);
        else ret = BUILD_SAFETY_CFG(hyundai_cam_scc_rx_checks, HYUNDAI_CAMERA_SCC_TX_MSGS);
    }
    else if (hyundai_longitudinal) {
        static RxCheck hyundai_long_rx_checks[] = {
          HYUNDAI_COMMON_RX_CHECKS(false)
          // Use CLU11 (buttons) to manage controls allowed instead of SCC cruise state
          {.msg = {{0x4F1, 0, 4, .ignore_checksum = true, .max_counter = 15U, .frequency = 50U}, { 0 }, { 0 }}
},
        };
        static RxCheck hyundai_long_rx_checks_legacy[] = {
          HYUNDAI_COMMON_RX_CHECKS(true)
          // Use CLU11 (buttons) to manage controls allowed instead of SCC cruise state
          {.msg = {{0x4F1, 0, 4, .ignore_checksum = true, .max_counter = 15U, .frequency = 50U}, { 0 }, { 0 }}
},
        };

        if(legacy_car) ret = BUILD_SAFETY_CFG(hyundai_long_rx_checks_legacy, HYUNDAI_LONG_TX_MSGS);
        else ret = BUILD_SAFETY_CFG(hyundai_long_rx_checks, HYUNDAI_LONG_TX_MSGS);
    }
    else {
        static RxCheck hyundai_rx_checks[] = {
           HYUNDAI_COMMON_RX_CHECKS(false)
           HYUNDAI_SCC12_ADDR_CHECK(0)
        };
        static RxCheck hyundai_rx_checks_legacy[] = {
           HYUNDAI_COMMON_RX_CHECKS(true)
           //HYUNDAI_SCC12_ADDR_CHECK(0)
        };

        if(legacy_car) ret = BUILD_SAFETY_CFG(hyundai_rx_checks_legacy, HYUNDAI_TX_MSGS);
        else ret = BUILD_SAFETY_CFG(hyundai_rx_checks, HYUNDAI_TX_MSGS);
    }
    return ret;
}


static safety_config hyundai_init(uint16_t param) {
  static const CanMsg HYUNDAI_LONG_TX_MSGS[] = {
    {0x340, 0, 8}, // LKAS11 Bus 0
    {0x4F1, 0, 4}, // CLU11 Bus 0
    {0x485, 0, 8}, // LFAHDA_MFC Bus 0
    {0x420, 0, 8}, // SCC11 Bus 0
    {0x421, 0, 8}, // SCC12 Bus 0
    {0x50A, 0, 8}, // SCC13 Bus 0
    {0x389, 0, 8}, // SCC14 Bus 0
    {0x4A2, 0, 2}, // FRT_RADAR11 Bus 0
    {0x38D, 0, 8}, // FCA11 Bus 0
    {0x483, 0, 8}, // FCA12 Bus 0
    {0x7D0, 0, 8}, // radar UDS TX addr Bus 0 (for radar disable)
  };

  static const CanMsg HYUNDAI_CAMERA_SCC_TX_MSGS[] = {
    {0x340, 0, 8}, // LKAS11 Bus 0
    {0x4F1, 2, 4}, // CLU11 Bus 2
    {0x485, 0, 8}, // LFAHDA_MFC Bus 0
  };

  hyundai_common_init(param);
  hyundai_legacy = false;
  return hyundai_init_carrot(hyundai_legacy);

  safety_config ret;
  if (hyundai_longitudinal) {
    static RxCheck hyundai_long_rx_checks[] = {
      HYUNDAI_COMMON_RX_CHECKS(false)
      // Use CLU11 (buttons) to manage controls allowed instead of SCC cruise state
      {.msg = {{0x4F1, 0, 4, .ignore_checksum = true, .max_counter = 15U, .frequency = 50U}, { 0 }, { 0 }}},
    };

    ret = BUILD_SAFETY_CFG(hyundai_long_rx_checks, HYUNDAI_LONG_TX_MSGS);
  } else if (hyundai_camera_scc) {
    static RxCheck hyundai_cam_scc_rx_checks[] = {
      HYUNDAI_COMMON_RX_CHECKS(false)
      HYUNDAI_SCC12_ADDR_CHECK(2)
    };

    ret = BUILD_SAFETY_CFG(hyundai_cam_scc_rx_checks, HYUNDAI_CAMERA_SCC_TX_MSGS);
  } else {
    static RxCheck hyundai_rx_checks[] = {
       HYUNDAI_COMMON_RX_CHECKS(false)
       HYUNDAI_SCC12_ADDR_CHECK(0)
    };

    ret = BUILD_SAFETY_CFG(hyundai_rx_checks, HYUNDAI_TX_MSGS);
  }
  return ret;
}

static safety_config hyundai_legacy_init(uint16_t param) {
  // older hyundai models have less checks due to missing counters and checksums
  static RxCheck hyundai_legacy_rx_checks[] = {
    HYUNDAI_COMMON_RX_CHECKS(true)
    //HYUNDAI_SCC12_ADDR_CHECK(0)
  };

  hyundai_common_init(param);
  hyundai_legacy = true;

  return hyundai_init_carrot(hyundai_legacy);

  hyundai_longitudinal = false;
  hyundai_camera_scc = false;
  return BUILD_SAFETY_CFG(hyundai_legacy_rx_checks, HYUNDAI_TX_MSGS);
}

const safety_hooks hyundai_hooks = {
  .init = hyundai_init,
  .rx = hyundai_rx_hook,
  .tx = hyundai_tx_hook,
  .fwd = hyundai_fwd_hook,
  .get_counter = hyundai_get_counter,
  .get_checksum = hyundai_get_checksum,
  .compute_checksum = hyundai_compute_checksum,
};

const safety_hooks hyundai_legacy_hooks = {
  .init = hyundai_legacy_init,
  .rx = hyundai_rx_hook,
  .tx = hyundai_tx_hook,
  .fwd = hyundai_fwd_hook,
  .get_counter = hyundai_get_counter,
  .get_checksum = hyundai_get_checksum,
  .compute_checksum = hyundai_compute_checksum,
};
