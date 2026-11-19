#!/usr/bin/env python3
import os
import shutil
import threading
import time
from openpilot.system.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 30

# 日志总量达到 MIN_BYTES 时，只保留最新的 KEEP_SEGMENTS 段，其余全部删除。
# 旧逻辑是「空间不够才删最早的」，30G 的盘能攒到 11G 都不动，看着像不会清理；
# 这里改成按数量上限主动维护，日志目录不会无限膨胀。
KEEP_SEGMENTS = 100

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5

# 最近有写入的段视为「正在录制」，不能删。
# 段目录里的 .lock 文件不代表还在写：loggerd 异常退出/重启后不会清锁，
# 历史段会一直带着 0 字节的残留锁。旧逻辑「有 .lock 就跳过」会让日志永远删不掉
# （实测 11GB 日志 94 个段全部因为残留锁被跳过）。
ACTIVE_MINUTES = 5


def is_active_segment(path: str, mtime: float) -> bool:
  """该段是否正在写入。"""
  return (time.time() - mtime) < ACTIVE_MINUTES * 60


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def _segment_dirs(dirs_by_creation: list[str]) -> list[str]:
  """只保留真正的行车记录段目录（boot/crash 等不在其中）。"""
  out = []
  for d in dirs_by_creation:
    if d in DELETE_LAST:
      continue
    date_str, _, seg_str = d.rpartition("--")
    if not date_str:
      continue
    try:
      int(seg_str)
    except ValueError:
      continue
    out.append(d)
  return out


def _log_root_bytes(dirs: list[str]) -> int:
  """统计日志目录占用（只算文件大小）。"""
  total = 0
  root = Paths.log_root()
  for d in dirs:
    p = os.path.join(root, d)
    try:
      with os.scandir(p) as it:
        for e in it:
          try:
            if e.is_file(follow_symlinks=False):
              total += e.stat(follow_symlinks=False).st_size
          except OSError:
            continue
    except OSError:
      continue
  return total


def enforce_segment_cap() -> int:
  """日志总量达到 MIN_BYTES 时，只保留最新 KEEP_SEGMENTS 段，其余全部删除。

  旧逻辑是「空间不够才删最早的」，30G 的盘能攒到 11G 都不动，看着像不会清理；
  这里改成按数量上限主动维护，日志目录不会无限膨胀。

  受保护的目录不会被删：
    - boot / crash（设备故障现场，永远保留）
    - 带 user.preserve 标记的最近若干段及其前两段
    - 正在写入（有 .lock 文件）的段
  """
  dirs_by_creation = listdir_by_creation(Paths.log_root())
  segments = _segment_dirs(dirs_by_creation)
  if len(segments) <= KEEP_SEGMENTS:
    return 0
  if _log_root_bytes(dirs_by_creation) < MIN_BYTES:
    return 0

  preserved = get_preserved_segments(dirs_by_creation)
  keep = set(segments[-KEEP_SEGMENTS:])
  root = Paths.log_root()
  removed = 0
  for d in segments:
    if d in keep or d in preserved:
      continue
    path = os.path.join(root, d)
    try:
      # 正在写入的段跳过（用 mtime 判断，残留 .lock 不再阻挡删除）
      if is_active_segment(path, os.stat(path).st_mtime):
        continue
      cloudlog.info(f"deleting {path} (segment cap: keep newest {KEEP_SEGMENTS})")
      shutil.rmtree(path)
      removed += 1
    except FileNotFoundError:
      continue
    except OSError:
      cloudlog.exception(f"issue deleting {path}")
  if removed:
    cloudlog.info(f"segment cap cleanup removed {removed} segments, kept newest {KEEP_SEGMENTS}")
  return removed


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    # 常态维护：日志总量到 5G 就按数量上限清理（保留最新 100 段）
    enforce_segment_cap()

    out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
    out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT

    if out_of_percent or out_of_bytes:
      dirs = listdir_by_creation(Paths.log_root())
      preserved_dirs = get_preserved_segments(dirs)

      # remove the earliest directory we can
      for delete_dir in sorted(dirs, key=lambda d: (d in DELETE_LAST, d in preserved_dirs)):
        delete_path = os.path.join(Paths.log_root(), delete_dir)

        try:
          # 正在写入的段跳过（残留 .lock 不再阻挡，见 is_active_segment 注释）
          if is_active_segment(delete_path, os.stat(delete_path).st_mtime):
            continue

          cloudlog.info(f"deleting {delete_path}")
          shutil.rmtree(delete_path)
          break
        except FileNotFoundError:
          continue
        except OSError:
          cloudlog.exception(f"issue deleting {delete_path}")
      exit_event.wait(.1)
    else:
      exit_event.wait(30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
