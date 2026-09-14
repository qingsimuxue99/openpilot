"""CP 优化移植：纵向加速度变化率限制 + ACC jerk 上限收紧。

这两个优化原本写在 opendbc 子模块的 hyundai/carcontroller.py 里
（设备本地提交 2c30b78）。但子模块改动无法分发：

  * 子模块 HEAD 2c30b78 是设备本地提交，任何公开远程都没有
  * 子模块远程只有 sunnypilot/opendbc（无写权限），qingsimuxue99/opendbc 不存在
  * 提交 gitlink 指针会让 git clone --recursive 报
    "fatal: reference is not a tree: 2c30b78"，全新安装直接失败

所以改由父仓库实现（与自动开启巡航的 CAN 报文同样的思路）：

  ① 加速度变化率限制（一阶斜率限制，减速 0.02 / 加速 0.04 m/s² 每帧 @100 Hz）
     原在 CarController.update() 里，对已被品牌参数 clamp 过的 accel 做限幅。
     搬到 controlsd.py 产生 actuators.accel 的地方（见 limit_accel_rate）：
       - 同样是 100 Hz、同一帧、同一个值
       - 品牌 CarControllerParams.ACCEL_MIN/MAX = -3.5 / 2.0，与 planner 上游的
         ACCEL_MIN/MAX 完全相同 -> 那个 clamp 实际不生效，
         所以「先限幅再 clamp」与「先 clamp 再限幅」等价

  ② ACC jerk 上限收紧（pid 状态 3.0 -> 1.5，其余 1.0 -> 0.5）
     原在 CarController.create_can_msgs() 内部算完就直传给
     hyundaican.create_acc_commands()，父仓库拿不到这个中间变量，
     只能在调用点包一层替换该参数 -> 见 install_jerk_hook()。
     这是猴补丁，因此带启动自检：上游一旦改了签名就大声打日志 + 不安装，
     绝不静默失效。（非 CANFD 走这个函数；CANFD 用 hyundaicanfd.create_acc_control，
     与原实现一样不在本优化范围内。）
"""
import inspect

# 每 100 Hz 帧允许的最大变化量（与 CP 一致）
ACCEL_RATE_DOWN = 0.02    # 减速方向
ACCEL_RATE_UP = 0.04      # 加速方向

# ACC 报文里的 upper_jerk
JERK_PID = 1.5
JERK_OTHER = 0.5


# ============================================================ ① 加速度变化率限制
def limit_accel_rate(accel: float, prev: float | None, a_ego: float, stopping: bool) -> float:
  """一阶斜率限制，与原实现（子模块 CarController.update()）逐行等价。

  prev 为 None 时用 a_ego 作为起点；stopping 时不限幅，但仍把当前值记为 prev
  （原实现同样在限幅分支之外无条件更新 self.accel_smoothed）。

  返回限幅后的加速度；调用方必须把它存回去当作下一帧的 prev。
  """
  if prev is None:
    prev = a_ego
  if stopping:
    return accel
  if accel < prev:
    return max(accel, prev - ACCEL_RATE_DOWN)
  return min(accel, prev + ACCEL_RATE_UP)


# ============================================================ ② jerk 收紧 hook
_STATE = {
  "installed": False,
  "long_control_state": None,
  "pid_value": None,
}


def set_long_control_state(state) -> None:
  """card.py 每帧调用，缓存当前 carControl.actuators.longControlState。

  子模块原本直接读同一个 actuators 对象；包装函数只拿得到位置参数，
  所以由 card.py（同进程、同帧、同线程）把状态递进来。
  """
  _STATE["long_control_state"] = state


def jerk_for_state(long_control_state) -> float:
  if long_control_state is not None and long_control_state == _STATE["pid_value"]:
    return JERK_PID
  return JERK_OTHER


def install_jerk_hook() -> bool:
  """把 hyundaican.create_acc_commands 的 upper_jerk 换成收紧后的值。

  幂等；安装成功返回 True。自检不过（上游签名变了）则打日志并返回 False，
  保持原函数不动 —— 宁可没有这个优化，也不能静默改错东西。
  """
  if _STATE["installed"]:
    return True

  try:
    from opendbc.car.hyundai import hyundaican
    from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
  except Exception as e:
    print(f"[carrot_accel_tuning] import 失败，jerk 收紧未安装: {type(e).__name__}: {e}")
    return False

  orig = getattr(hyundaican, "create_acc_commands", None)
  if orig is None:
    print("[carrot_accel_tuning] hyundaican.create_acc_commands 不存在，jerk 收紧未安装")
    return False

  try:
    params = list(inspect.signature(orig).parameters)
  except (TypeError, ValueError) as e:
    print(f"[carrot_accel_tuning] 取签名失败，jerk 收紧未安装: {type(e).__name__}: {e}")
    return False

  # (packer, enabled, accel, upper_jerk, idx, ...)
  if len(params) < 4 or params[3] != "upper_jerk":
    print(f"[carrot_accel_tuning] create_acc_commands 签名已变，jerk 收紧未安装: {params}")
    return False

  _STATE["pid_value"] = LongCtrlState.pid

  def create_acc_commands(packer, enabled, accel, upper_jerk, *args, **kwargs):
    return orig(packer, enabled, accel, jerk_for_state(_STATE["long_control_state"]), *args, **kwargs)

  create_acc_commands.__wrapped__ = orig
  hyundaican.create_acc_commands = create_acc_commands
  _STATE["installed"] = True
  print(f"[carrot_accel_tuning] ACC jerk 收紧已安装 (pid={JERK_PID} / 其他={JERK_OTHER})")
  return True
