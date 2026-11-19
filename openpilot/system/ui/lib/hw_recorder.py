"""
硬件 H.264 录屏器（高通 Venus V4L2 M2M 编码器 + 纯 Python MP4 封装）

为什么不用 ffmpeg 软编码：
  libx264 编码 2160x1080 实测吃掉 182% CPU（4 秒素材 7.3 CPU 秒），
  加上 GPU 回读 + Python 二次拷贝 + 色彩转换，UI 主线程被压死 → 录屏时整机卡顿。
  Venus 硬编每帧仅 1.7ms（1440x720），CPU 占用可忽略。

数据链路：
  UI 渲染纹理 --GPU缩放--> 1440x720 纹理 --回读--> RGBA
       -> numpy 转 NV12(BT.601 limited) -> Venus 硬编 -> Annex-B 码流
       -> 纯 Python MP4 封装 -> /data/media/0/videos/*.mp4

依赖：
  - system/loggerd/libcluster_h264_encoder_bridge.so（仓库自带，直调 V4L2 API）
  - /dev/v4l/by-path/platform-aa00000.qcom_vidc-video-index1（Venus 编码节点）
  - numpy（venv 自带）
任何一项缺失都会让 available=False，调用方自动退回软编码路径。
"""
import ctypes
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------- MP4 封装
def _box(typ: bytes, payload: bytes) -> bytes:
  return struct.pack(">I", 8 + len(payload)) + typ + payload


def _full(typ: bytes, version: int, flags: int, payload: bytes) -> bytes:
  return _box(typ, struct.pack(">B", version) + flags.to_bytes(3, "big") + payload)


_MATRIX = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def split_annexb(data: bytes):
  """Annex-B（00 00 01 起始码）裸流 -> NAL 列表"""
  n = len(data)
  starts = []
  i = 0
  while i < n - 3:
    if data[i] == 0 and data[i+1] == 0 and data[i+2] == 1:
      starts.append(i + 3)
      i += 3
    else:
      i += 1
  out = []
  for k, s in enumerate(starts):
    e = starts[k+1] - 3 if k + 1 < len(starts) else n
    while e > s and data[e-1] == 0:
      e -= 1
    if e > s:
      out.append(data[s:e])
  return out


def make_avcc(sps: bytes, pps: bytes) -> bytes:
  """AVCDecoderConfigurationRecord（与 ffmpeg/libx264 输出一致）"""
  return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
          + struct.pack(">H", len(sps)) + sps
          + bytes([1]) + struct.pack(">H", len(pps)) + pps)


class _Mp4Writer:
  """顺序写 mdat，最后写 moov（标准 ISO BMFF，mp4 播放器可直接播放）"""

  def __init__(self, path: str, width: int, height: int, fps: int, timescale: int = 90000):
    self.width = width
    self.height = height
    self.fps = fps
    self.timescale = timescale
    self.delta = max(1, timescale // fps)
    self.samples = []          # [(size, is_key, offset)]
    self.sample_ts = []        # 每帧的真实时间戳（微秒），用于算变长帧时长
    self.sps = None
    self.pps = None
    self.f = open(path, "w+b")
    self.f.write(_box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isom" + b"avc1" + b"mp41"))
    self.mdat_size_pos = self.f.tell()
    self.f.write(b"\x00\x00\x00\x00" + b"mdat")

  def add_sample(self, annexb: bytes, is_key: bool, ts_us: int | None = None) -> None:
    if not annexb:
      return
    if self.sps is None or self.pps is None:
      for nal in split_annexb(annexb):
        t = nal[0] & 0x1F
        if t == 7 and self.sps is None:
          self.sps = nal
        elif t == 8 and self.pps is None:
          self.pps = nal
    # Annex-B -> mp4 内部格式（length-prefixed），SPS/PPS 由 avcC 承载
    payload = b""
    for nal in split_annexb(annexb):
      if not nal or (nal[0] & 0x1F) in (7, 8):
        continue
      payload += struct.pack(">I", len(nal)) + nal
    if not payload:
      return
    offset = self.f.tell()
    self.f.write(payload)
    self.samples.append((len(payload), bool(is_key), offset))
    self.sample_ts.append(int(ts_us) if ts_us is not None else None)

  def close(self) -> None:
    end = self.f.tell()
    self.f.seek(self.mdat_size_pos)
    self.f.write(struct.pack(">I", end - self.mdat_size_pos))
    self.f.seek(end)
    self.f.write(self._build_moov())
    self.f.close()

  def _frame_durations(self) -> tuple[list[int], int]:
    """算出每帧时长（timescale 单位）。

    这里做的是「真实时长 + 均摊」：
      - 用首末帧的真实时间戳算出**总时长**（保证播放速度与真实一致，
        UI 帧率波动时也不会偏快/偏慢）；
      - 再把这个总时长**均摊**到每一帧（保证帧间隔均匀，播放不顿）。

    为什么不用逐帧的真实间隔：行驶时 UI 渲染会偶发变慢，某一帧的间隔
    可能被拉到几百毫秒。逐帧照搬的话，mp4 里那一帧的时长就特别长，
    播放时会明显「顿一下」——正是「速度一快就卡顿」的原因。
    均摊之后总时长不变（速度依旧准确），帧间隔又均匀（播放顺滑）。
    """
    n = len(self.samples)
    if n == 0:
      return [], 0
    ts = self.sample_ts
    if len(ts) != n or any(t is None for t in ts):
      # 没有时间戳信息，退回固定 delta
      return [self.delta] * n, self.delta * n
    total_us = ts[-1] - ts[0]
    if total_us <= 0:
      return [self.delta] * n, self.delta * n
    avg = max(1, total_us * self.timescale // (1_000_000 * n))
    return [avg] * n, avg * n

  def _build_stts(self, durations: list[int]) -> bytes:
    """生成变长 stts（时间到样本表），连续相同帧时长合并成一段。"""
    if not durations:
      return _full(b"stts", 0, 0, struct.pack(">I", 0))
    runs: list[list[int]] = []
    for d in durations:
      if runs and runs[-1][1] == d:
        runs[-1][0] += 1
      else:
        runs.append([1, d])
    payload = struct.pack(">I", len(runs)) + b"".join(struct.pack(">II", c, d) for c, d in runs)
    return _full(b"stts", 0, 0, payload)

  def _build_moov(self) -> bytes:
    n = len(self.samples)
    durations, total_dur = self._frame_durations()
    avcc = make_avcc(self.sps or b"\x67\x42\xc0\x1e", self.pps or b"\x68\xce\x38\x80")
    avc1_payload = (b"\x00" * 6 + struct.pack(">H", 1)
                    + struct.pack(">H", 0) + b"\x00" * 2 + b"\x00" * 12
                    + struct.pack(">HH", self.width, self.height)
                    + struct.pack(">II", 0x00480000, 0x00480000)
                    + struct.pack(">I", 0) + struct.pack(">H", 1)
                    + b"\x00" * 32 + struct.pack(">HH", 0x0018, 0xFFFF)
                    + _box(b"avcC", avcc))
    stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1) + _box(b"avc1", avc1_payload))
    stts = self._build_stts(durations)
    keys = [i + 1 for i, (sz, k, off) in enumerate(self.samples) if k]
    stss = _full(b"stss", 0, 0, struct.pack(">I", len(keys)) + b"".join(struct.pack(">I", k) for k in keys)) if keys \
           else _full(b"stss", 0, 0, struct.pack(">I", 0))
    stsc = _full(b"stsc", 0, 0, struct.pack(">I", 1) + struct.pack(">III", 1, max(1, n), 1))
    stsz = _full(b"stsz", 0, 0, struct.pack(">II", 0, n) + b"".join(struct.pack(">I", sz) for sz, k, off in self.samples))
    first_off = self.samples[0][2] if self.samples else self.f.tell()
    stco = _full(b"stco", 0, 0, struct.pack(">I", 1) + struct.pack(">I", first_off))
    stbl = _box(b"stbl", stsd + stts + stss + stsc + stsz + stco)
    vmhd = _full(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    dref = _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b""))
    minf = _box(b"minf", vmhd + _box(b"dinf", dref) + stbl)
    mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, self.timescale, total_dur) + struct.pack(">HH", 0x55C4, 0))
    hdlr = _full(b"hdlr", 0, 0, struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    tkhd = _full(b"tkhd", 0, 7, struct.pack(">IIIII", 0, 0, 1, 0, total_dur)
                 + b"\x00" * 8 + struct.pack(">HHHH", 0, 0, 0, 0)
                 + _MATRIX + struct.pack(">II", self.width << 16, self.height << 16))
    trak = _box(b"trak", tkhd + mdia)
    mvhd = _full(b"mvhd", 0, 0, struct.pack(">IIII", 0, 0, self.timescale, total_dur)
                 + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100) + b"\x00" * 10
                 + _MATRIX + b"\x00" * 24 + struct.pack(">I", 2))
    return _box(b"moov", mvhd + trak)


# ---------------------------------------------------------------- Venus 硬编
_PacketCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                             ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)

VENUS_DEVICE = "/dev/v4l/by-path/platform-aa00000.qcom_vidc-video-index1"

# Venus 编码器的全局互斥锁。
#
# 同一个 V4L2 节点（/dev/video33，和 openpilot 的 encoderd 共用）上，
# 同时存在两个编码器实例会导致驱动状态错乱：
# 旧实例在后台 close/destroy 的同时新实例已经 open，
# 于是新实例的 encode 调用把驱动搞挂，GPU 随之 reset，
# UI 线程下一次 GPU 回读就永久阻塞 —— 表现就是「第二次点录制，画面卡死」。
#
# 锁从 open 一直持有到 close/destroy 结束，保证同一时刻只有一个实例。
_VENUS_LOCK = threading.Lock()

# numpy / PyAV 在模块加载时导入。
#
# 之前是在后台 open 线程里 import 的，而 PyAV 的 import 要几百毫秒且**全程持有 GIL**，
# 于是主线程在 threading.Thread.start() 上被卡住 —— 实测首次 start() 返回要 616ms，
# 表现就是「第一次点录制按钮卡一下」。放到模块级之后，import 发生在 UI 启动阶段，
# 点击路径上就只剩纯 C 的 V4L2 open（约 30-150ms，且在后台线程）。
try:
  import numpy as _np_preload
except Exception:
  _np_preload = None

_AV_PRELOAD = None
for _extra in ("/data/openpilot/pydeps", "/data/openpilot"):
  if _extra not in sys.path:
    sys.path.append(_extra)
try:
  import av as _AV_PRELOAD
except Exception:
  _AV_PRELOAD = None


class VenusH264Recorder:
  """硬件 H.264 编码录屏器。调用方在 UI 线程 submit()，编码在后台线程完成。"""

  def __init__(self, path: str, width: int, height: int, fps: int,
               # 码率：640x360 每秒 30 帧，6Mbps 已经很富余；
               # 行驶时画面变化剧烈（路面纹理、树影、护栏），码率给够才不会发糊。
               bitrate: int = 6_000_000, gop: int = 30, queue_size: int = 4):
    self.path = path
    self.width = width
    self.height = height
    # fps 是「渲染帧率」（UI 的目标帧率，通常 20）。
    # 实际送进编码器的帧只有 fps / capture_every（每 N 渲染帧抓一次），
    # 所以 mp4 的时间戳步长必须按真实采集率算，否则播放时会「快进」。
    # 原版靠 ffmpeg 的 `-r capture_fps` 做这个补偿，硬件路径没有 ffmpeg
    # 这一层，必须在这里自己算。
    self.fps = fps
    self.bitrate = bitrate
    self.gop = gop
    self.queue_size = queue_size
    self.capture_every = 1        # 每 N 个渲染帧抓一帧，由调用方设置
    self._capture_fps = fps

    self._lib = None
    self._handle = None
    self._writer = None
    self._queue = queue.Queue(maxsize=queue_size)
    self._stop = threading.Event()
    # _ready 在后台线程完成 V4L2 open 后置位；UI 线程用它判断能否送帧
    self._ready = threading.Event()
    self._open_thread = None
    self._close_thread = None
    self._thread = None
    self._lock_held = False
    self._packets = []
    self._cb = None
    self._stride = 0
    self._ysl = 0
    self._usl = 0
    self._bytesused = 0
    self._np = None
    self._av = None
    self._layout = None
    self._frame_idx = 0
    self._t0 = None             # 第一帧的真实时间戳基准
    self.dropped = 0
    self.encoded = 0

  # ---------- 依赖探测 ----------
  @staticmethod
  def _bridge_path() -> str:
    """定位 libcluster_h264_encoder_bridge.so（多个候选，按优先级）"""
    name = "libcluster_h264_encoder_bridge.so"
    candidates = []
    # 模块自身位置推导：<root>/system/ui/lib/hw_recorder.py -> <root>/system/loggerd/<name>
    try:
      root = Path(__file__).resolve().parents[3]
      candidates.append(str(root / "system" / "loggerd" / name))
    except Exception:
      pass
    try:
      from openpilot.common.basedir import BASEDIR
      base = Path(BASEDIR)
      candidates.append(str(base / "openpilot" / "system" / "loggerd" / name))
      candidates.append(str(base / "system" / "loggerd" / name))
    except Exception:
      pass
    candidates.append("/data/openpilot/openpilot/system/loggerd/" + name)
    for c in candidates:
      if os.path.exists(c):
        return c
    return candidates[0]

  def available(self) -> bool:
    if not os.path.exists(VENUS_DEVICE):
      return False
    if not os.path.exists(self._bridge_path()):
      return False
    try:
      import numpy  # noqa: F401
    except Exception:
      return False
    return True

  # ---------- 生命周期 ----------
  def start(self) -> bool:
    """启动录屏编码器。

    这里必须「立刻返回」：调用方是 UI 主渲染线程，任何同步耗时都会
    让整屏卡住（用户点一次录屏按钮卡 2 秒就是这么来的）。

    实测耗时构成（设备上）：
      import av (PyAV)  ~80ms
      ctypes.CDLL 加载   ~30ms
      V4L2 open          ~150ms
    三样全部搬到后台线程，主线程只做一次起线程的动作（<1ms）。
    编码器就绪后由 _ready 事件通知；这之前 submit() 会安全丢帧。
    """
    if not self.available():
      return False
    self._stop.clear()
    self._open_thread = threading.Thread(target=self._open_async, name="hw-screenrec-open", daemon=True)
    self._open_thread.start()
    return True

  def _open_async(self) -> None:
    """后台线程：加载依赖 -> 打开 Venus 编码器 -> 启动编码工作线程。"""
    try:
      # 抢 Venus 设备锁：必须等上一个实例彻底 close/destroy 完。
      # 拿不到就放弃这次录制（宁可不出文件，也不要把驱动搞挂）。
      if not _VENUS_LOCK.acquire(timeout=15.0):
        print("[HWREC] Venus 设备仍被占用，放弃本次录制")
        self._ready.set()
        return
      self._lock_held = True
      # 拿到锁的这段时间里可能已经被 stop() 了（用户快速连点）。
      # 这时不能再 open 设备：否则旧实例的 close 与这里的 open 会抢同一个
      # Venus 句柄，把驱动搞挂、GPU reset，UI 线程下一次回读就永久阻塞。
      if self._stop.is_set():
        print("[HWREC] open 前已收到停止指令，放弃")
        self._release_device_lock()
        self._ready.set()
        return
      import numpy as np
      self._np = np
      # PyAV 内部就是 libswscale（C SIMD），比 numpy 定点快一倍以上；
      # 模块加载时已经预导入过了（见文件顶部），这里直接复用。
      self._av = _AV_PRELOAD
      self._lib = ctypes.CDLL(self._bridge_path())
      self._bind()
      handle = self._lib.cluster_h264_encoder_bridge_create(
        self.width, self.height, self.fps, self.bitrate, self.gop,
        VENUS_DEVICE.encode(), 2, 0, 0)
      if not handle:
        print("[HWREC] encoder create failed")
        self._release_device_lock()
        self._ready.set()
        return
      self._handle = handle
      if self._lib.cluster_h264_encoder_bridge_open(handle) != 0:
        self._lib.cluster_h264_encoder_bridge_destroy(handle)
        self._handle = None
        print("[HWREC] encoder open failed")
        self._release_device_lock()
        self._ready.set()
        return
      self._stride = int(self._lib.cluster_h264_encoder_bridge_input_stride(handle))
      self._ysl = int(self._lib.cluster_h264_encoder_bridge_input_y_scanlines(handle))
      self._usl = int(self._lib.cluster_h264_encoder_bridge_input_uv_scanlines(handle))
      self._bytesused = int(self._lib.cluster_h264_encoder_bridge_input_bytesused(handle))
      self._layout = np.zeros((self._ysl + self._usl, self._stride), np.uint8)
      self._writer = _Mp4Writer(self.path, self.width, self.height, self._capture_fps)
      self._thread = threading.Thread(target=self._worker, name="hw-screenrec", daemon=True)
      self._thread.start()
    except Exception as e:
      print(f"[HWREC] open async failed: {type(e).__name__} {e}")
      self._release_device_lock()
    finally:
      self._ready.set()

  def wait_ready(self, timeout: float = 3.0) -> bool:
    """等待编码器就绪（后台线程 open 完成）。"""
    return self._ready.wait(timeout) and self._handle is not None

  def set_capture_rate(self, capture_every: int) -> None:
    """设置「每 N 个渲染帧抓一帧」，并据此修正 mp4 的真实采集帧率。

    调用方必须在 start() 之前设置（start 之后 mp4 writer 已按此建好）。
    """
    self.capture_every = max(1, int(capture_every))
    self._capture_fps = max(1, self.fps // self.capture_every)

  def stop(self) -> None:
    """停止录制。**必须立刻返回**——调用方是 UI 渲染线程。

    收尾工作（等编码线程排空队列 -> Venus drain -> 关闭设备 -> 写 mp4 的 moov）
    实测要几百毫秒，放在 UI 线程里就是「点停止卡一下」。
    这里只发一个停止信号，真正的收尾交给后台线程。
    """
    self._stop.set()
    try:
      self._queue.put_nowait(None)
    except Exception:
      pass
    if self._close_thread is None:
      self._close_thread = threading.Thread(target=self._close_async, name="hw-screenrec-close", daemon=True)
      self._close_thread.start()

  def _close_async(self) -> None:
    """后台线程：等编码线程收尾，然后关闭 Venus 并写完 mp4。"""
    try:
      # 必须等 open 线程彻底结束再 cleanup：否则 _cleanup() 关闭 handle 的
      # 时候 open 线程可能刚拿到 handle，两边抢同一个 Venus 句柄，
      # 驱动会挂掉（表现就是 UI 永久卡死）。
      # 这里等的是后台线程，UI 线程不受影响。
      if self._open_thread is not None:
        self._open_thread.join(timeout=20)
        self._open_thread = None
      if self._thread is not None:
        self._thread.join(timeout=5)
        self._thread = None
      self._cleanup()
    except Exception as e:
      print(f"[HWREC] close async failed: {type(e).__name__} {e}")

  def _release_device_lock(self) -> None:
    """释放 Venus 设备锁（只释放一次）。"""
    if getattr(self, "_lock_held", False):
      self._lock_held = False
      try:
        _VENUS_LOCK.release()
      except RuntimeError:
        pass

  def _cleanup(self) -> None:
    if self._handle is not None and self._lib is not None:
      try:
        self._lib.cluster_h264_encoder_bridge_close(self._handle)
        self._lib.cluster_h264_encoder_bridge_destroy(self._handle)
      except Exception:
        pass
    self._handle = None
    if self._writer is not None:
      try:
        self._writer.close()
      except Exception:
        pass
      self._writer = None
    # 设备已关闭，释放全局锁，让下一次录制可以 open
    self._release_device_lock()

  # ---------- 数据提交（UI 线程调用，必须非阻塞） ----------
  def submit(self, rgba: bytes) -> None:
    if self._handle is None or not rgba:
      return
    # 编码器还没 open 完就丢帧，不排队（避免 UI 线程阻塞）
    if not self._ready.is_set():
      self.dropped += 1
      return
    try:
      self._queue.put_nowait(rgba)
    except queue.Full:
      self.dropped += 1

  # ---------- 后台线程 ----------
  def _worker(self) -> None:
    while True:
      try:
        item = self._queue.get(timeout=0.5)
      except queue.Empty:
        if self._stop.is_set():
          break
        continue
      if item is None:
        break
      try:
        self._encode_one(item)
      except Exception as e:
        print(f"[HWREC] encode error: {type(e).__name__} {e}")
    # 收尾：把剩余码流刷进 mp4
    try:
      self._lib.cluster_h264_encoder_bridge_drain(self._handle, 500, self._cb, None)
    except Exception:
      pass
    self._flush_packets()

  def _flush_packets(self) -> None:
    while self._packets:
      data, is_key, pts = self._packets.pop(0)
      try:
        self._writer.add_sample(data, is_key, pts)
      except Exception:
        pass

  def _on_packet(self, user_data, size, flags, pts, is_key, a6, a7):
    if user_data and size:
      # pts 是编码器回传的时间戳（我们送进去的真实时间），mp4 的帧时长要用它
      self._packets.append((ctypes.string_at(user_data, size), bool(is_key), int(pts)))

  def _encode_one(self, rgba: bytes) -> None:
    np = self._np
    lay = self._layout
    nv12 = self._to_nv12(rgba)
    if nv12 is None:
      return
    # Venus 的 NV12 带 stride 对齐（stride / y_scanlines 由驱动决定），
    # swscale 输出的是紧密排列，需要重排到驱动要求的布局。
    h, w = self.height, self.width
    flat = np.frombuffer(nv12, np.uint8)
    lay[:h, :w] = flat[:h * w].reshape(h, w)
    uv = flat[h * w:].reshape(h // 2, w)
    lay[self._ysl:self._ysl + h // 2, :w] = uv
    size = max(self._bytesused, lay.size)
    buf_arr = np.zeros(size, np.uint8)
    buf_arr[:lay.size] = lay.reshape(-1)
    buf = (ctypes.c_uint8 * size).from_buffer(buf_arr)
    # 时间戳用**真实经过时间**，而不是「名义帧率算出来的步长」。
    #
    # 之前按 fps/capture_every 推算步长，隐含假设「UI 渲染帧率恒定」。
    # 但真机录屏时 UI 帧率会掉到 13~17fps（回读有 GPU 同步开销），
    # 于是每帧实际间隔比假设的长，播放出来就快了 1.2~1.5 倍。
    # 用真实时间戳后，无论 UI 帧率怎么波动，播放速度都正确。
    now = time.monotonic()
    if self._t0 is None:
      self._t0 = now
    ts = int((now - self._t0) * 1_000_000)
    self._frame_idx += 1
    self._lib.cluster_h264_encoder_bridge_encode_nv12(
      self._handle, ctypes.addressof(buf), self._bytesused, ts, self._cb, None)
    self._flush_packets()
    self.encoded += 1

  def _to_nv12(self, rgba) -> bytes:
    """RGBA -> NV12。优先用 PyAV(swscale, C SIMD)，失败回退 numpy 定点实现。"""
    np = self._np
    if self._av is not None:
      try:
        arr = np.frombuffer(rgba, np.uint8).reshape(self.height, self.width, 4)
        frame = self._av.VideoFrame.from_numpy_buffer(arr, format="rgba")
        return frame.reformat(format="nv12").to_ndarray().tobytes()
      except Exception:
        pass
    return self._np_to_nv12(rgba)

  def _np_to_nv12(self, rgba) -> bytes:
    """numpy 定点实现（BT.601 limited range，实测解回 RGB 完全一致）"""
    np = self._np
    a = np.frombuffer(rgba, np.uint8).reshape(self.height, self.width, 4)
    r = a[:, :, 0].astype(np.uint16)
    g = a[:, :, 1].astype(np.uint16)
    b = a[:, :, 2].astype(np.uint16)
    y = ((r * 66 + g * 129 + b * 25) >> 8) + 16
    r2 = r[0::2, 0::2]
    g2 = g[0::2, 0::2]
    b2 = b[0::2, 0::2]
    u = (((b2 * 128 + 32768) - (r2 * 43 + g2 * 85)) >> 8)
    v = (((r2 * 128 + 32768) - (g2 * 107 + b2 * 21)) >> 8)
    out = np.zeros((self.height + self.height // 2, self.width), np.uint8)
    out[:self.height] = y.astype(np.uint8)
    out[self.height:, 0::2] = u.astype(np.uint8)
    out[self.height:, 1::2] = v.astype(np.uint8)
    return out.tobytes()

  def _bind(self) -> None:
    lib = self._lib

    def fn(name, args, restype):
      f = getattr(lib, name)
      f.argtypes = args
      f.restype = restype
      return f

    fn("cluster_h264_encoder_bridge_create",
       [ctypes.c_int] * 5 + [ctypes.c_char_p] + [ctypes.c_int] * 3, ctypes.c_void_p)
    fn("cluster_h264_encoder_bridge_open", [ctypes.c_void_p], ctypes.c_int)
    fn("cluster_h264_encoder_bridge_close", [ctypes.c_void_p], ctypes.c_int)
    fn("cluster_h264_encoder_bridge_destroy", [ctypes.c_void_p], ctypes.c_int)
    fn("cluster_h264_encoder_bridge_encode_nv12",
       [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint64, _PacketCb, ctypes.c_void_p],
       ctypes.c_int)
    fn("cluster_h264_encoder_bridge_drain",
       [ctypes.c_void_p, ctypes.c_int, _PacketCb, ctypes.c_void_p], ctypes.c_int)
    for name in ("cluster_h264_encoder_bridge_input_stride",):
      fn(name, [ctypes.c_void_p], ctypes.c_int)
    for name in ("cluster_h264_encoder_bridge_input_y_scanlines",
                 "cluster_h264_encoder_bridge_input_uv_scanlines",
                 "cluster_h264_encoder_bridge_input_bytesused"):
      fn(name, [ctypes.c_void_p], ctypes.c_int64)
    self._cb = _PacketCb(self._on_packet)
