#!/usr/bin/env python3
"""driving_model_manager.py — 驾驶模型管理器 (扫描/清单/下载/切换/删除/磁盘占用)

用法:
  list                       列出本地模型
  manifest                   列出在线模型清单 (仓库内置 sp_models.json)
  download <name|idx>        下载模型 (进度写 /tmp/model_dl_progress)
  switch <idx>               切换激活模型 (写 modelid + 重启 modeld)
  delete <idx>               删除本地模型
  usage                      磁盘占用统计
"""
import os
import sys
import json
import time
import shutil
import hashlib
import argparse
import urllib.request
from pathlib import Path

sys.path.insert(0, '/data/openpilot')
sys.path.insert(0, '/data/openpilot/opendbc_repo')

MODELS_DIR = Path('/data/openpilot/selfdrive/modeld/models')
MANIFEST = MODELS_DIR / 'sp_models.json'
PROGRESS_FILE = '/tmp/model_dl_progress'
DOWNLOAD_DIR = Path('/data/openpilot/selfdrive/modeld/models/.downloads')


# ---------------- 本地模型扫描 ----------------
def list_local():
  """扫描 models/ 目录, 返回按编号排序的模型列表"""
  models = []
  for d in sorted(MODELS_DIR.iterdir()):
    if not d.is_dir() or not d.name[0].isdigit():
      continue
    # 解析 "N-name"
    try:
      idx = int(d.name.split('-')[0])
    except ValueError:
      continue
    name = d.name.split('-', 1)[1] if '-' in d.name else d.name
    sp_pkl = d / 'driving_tinygrad.pkl'
    cp_v = d / 'driving_vision_tinygrad.pkl'
    cp_p = d / 'driving_policy_tinygrad.pkl'
    if sp_pkl.exists():
      mtype = 'SP'
      size = sp_pkl.stat().st_size
    elif cp_v.exists() and cp_p.exists():
      mtype = 'CP'
      size = cp_v.stat().st_size + cp_p.stat().st_size
    else:
      continue  # 空目录跳过
    models.append({'idx': idx, 'name': name, 'type': mtype, 'size': size, 'dir': str(d)})
  return models


def get_modelid():
  from openpilot.common.params import Params
  mi = Params().get('modelid')
  return int(mi.decode()) if mi and mi.isdigit() else -1


def fmt_size(n):
  for unit in ['B', 'KB', 'MB', 'GB']:
    if n < 1024 or unit == 'GB':
      return f'{n:.1f}{unit}' if unit != 'B' else f'{n}B'
    n /= 1024


def cmd_list(json_out=False):
  models = list_local()
  cur = get_modelid()
  if json_out:
    out = []
    for m in models:
      out.append({'idx': m['idx'], 'name': m['name'], 'type': m['type'],
                  'size': m['size'], 'size_str': fmt_size(m['size']), 'active': m['idx'] == cur})
    du = shutil.disk_usage(MODELS_DIR)
    print(json.dumps({'models': out, 'current': cur,
                      'total': fmt_size(sum(m['size'] for m in models)),
                      'free': fmt_size(du.free)}))
    return 0
  print(f'本地模型 {len(models)} 个, 当前激活 modelid={cur}')
  total = 0
  for m in models:
    total += m['size']
    mark = '▶' if m['idx'] == cur else ' '
    print(f"  {mark} [{m['idx']}] {m['name']:<20} {m['type']:>2} {fmt_size(m['size'])}")
  print(f'总占用: {fmt_size(total)}')


# ---------------- 在线清单 ----------------
def load_manifest():
  if not MANIFEST.exists():
    print('清单文件缺失: %s' % MANIFEST)
    return None
  return json.load(open(MANIFEST, encoding='utf-8'))


def cmd_manifest(json_out=False):
  d = load_manifest()
  if d is None:
    return 1
  bundles = d.get('bundles', [])
  # 最新模型在前: SP 官方 index 递增=模型越新, 倒序排列
  bundles = sorted(bundles, key=lambda b: b.get('index', 0), reverse=True)
  local_names = {m['name'].lower() for m in list_local()}
  if json_out:
    out = []
    for i, b in enumerate(bundles):
      sn = b.get('short_name', '')
      art = b['models'][0]['artifact']
      total = sum(int(ch.get('size', 0)) for ch in art.get('chunks', []))
      out.append({'idx': i, 'short_name': sn, 'display_name': b.get('display_name', ''),
                  'chunks': len(art.get('chunks', [])), 'gen': b.get('generation'),
                  'size_str': fmt_size(total) if total else '', 'installed': sn.lower() in local_names})
    print(json.dumps({'bundles': out, 'count': len(out)}))
    return 0
  print(f'在线模型 {len(bundles)} 个 (清单: {MANIFEST.name}, 最新在前)')
  for i, b in enumerate(bundles):
    sn = b.get('short_name', '')
    art = b['models'][0]['artifact']
    n_chunks = len(art.get('chunks', []))
    have = '✓' if sn.lower() in local_names else ' '
    print(f"  {have} [{i:>2}] {sn:<10} {b.get('display_name','')[:40]:<42} chunks={n_chunks} gen={b.get('generation')}")
  return 0


# ---------------- 下载 ----------------
def write_progress(msg):
  try:
    with open(PROGRESS_FILE, 'w') as f:
      f.write(msg)
  except Exception:
    pass


def chunk_sha256(path):
  """计算 chunk 文件 sha256 (断点完整性校验用)"""
  h = hashlib.sha256()
  with open(path, 'rb') as f:
    while True:
      b = f.read(1 << 20)
      if not b:
        break
      h.update(b)
  return h.hexdigest()


def dl_chunk(url, path, timeout=120, progress_cb=None):
  req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
  with urllib.request.urlopen(req, timeout=timeout) as r, open(path, 'wb') as f:
    total = int(r.headers.get('Content-Length') or 0)
    done = 0
    last_report = 0
    last_t = time.time()
    while True:
      block = r.read(65536)
      if not block:
        break
      f.write(block)
      done += len(block)
      now = time.time()
      # 每 512KB 或每 2 秒汇报一次进度, 让 UI 上的百分比/速度动起来
      if progress_cb and (done - last_report >= 524288 or now - last_t >= 2.0):
        progress_cb(done, total, now)
        last_report = done
        last_t = now
  return done


def cmd_download(target):
  d = load_manifest()
  if d is None:
    return 1
  bundles = d.get('bundles', [])
  # 匹配: 序号或 short_name (忽略大小写)
  b = None
  if target.isdigit() and int(target) < len(bundles):
    b = bundles[int(target)]
  else:
    tl = target.lower()
    b = next((x for x in bundles if x.get('short_name', '').lower() == tl), None)
  if b is None:
    print('未找到模型: %s' % target)
    return 1

  sn = b.get('short_name', '')
  art = b['models'][0]['artifact']
  base_url = art['download_uri']['url']
  chunks = art.get('chunks', [])
  sha256_expected = art['download_uri'].get('sha256', '')
  dl_dir = DOWNLOAD_DIR / sn.lower()
  dl_dir.mkdir(parents=True, exist_ok=True)

  # 计算新编号
  local = list_local()
  next_idx = max([m['idx'] for m in local] + [-1]) + 1
  target_dir = MODELS_DIR / f'{next_idx}-{sn.lower()}'

  print(f'下载模型 [{sn}] -> {target_dir.name} ({len(chunks)} chunks)')
  write_progress(f'{sn} 0% 准备中')

  # 下载 chunks (断点: 仅当已存在且 sha256 与清单一致才算完整, 否则删除重下)
  chunk_paths = []
  ok = True
  for i, ch in enumerate(chunks):
    url = f'{base_url}.chunk{i+1:02d}of{len(chunks):02d}'
    path = dl_dir / f'chunk{i+1:02d}'
    chunk_paths.append(path)
    sha = ch.get('sha256', '') if isinstance(ch, dict) else ''
    if path.exists() and sha and chunk_sha256(path) == sha:
      print(f'  chunk{i+1}: 已存在且校验通过, 跳过')
      continue
    if path.exists():
      print(f'  chunk{i+1}: 存在但校验不通过, 删除重下')
      path.unlink()
    try:
      chunk_start = time.time()
      def cb(done, total, now, _i=i, _s=sn, _n=len(chunks), _st=chunk_start):
        pct = int(done * 100 / total) if total else 0
        spd = done / max(now - _st, 0.001)
        spd_s = f'{spd/1024:.0f}KB/s' if spd < 1024 * 1024 else f'{spd/1024/1024:.1f}MB/s'
        write_progress(f'{_s} 下载中 {_i}/{_n} chunk{_i+1} {pct}% {spd_s}')
      write_progress(f'{sn} 下载中 {i}/{len(chunks)} chunk{i+1} 0%')
      n = dl_chunk(url, path, progress_cb=cb)
      print(f'  chunk{i+1}: {n} bytes OK')
    except Exception as e:
      print(f'  chunk{i+1}: 下载失败 {type(e).__name__}: {str(e)[:80]}')
      ok = False
      break
  if not ok:
    write_progress(f'{sn} 下载失败, 请重试')
    print('下载未完成, 保留已下载分块 (重试会自动续传)')
    return 1

  # 拼接
  write_progress(f'{sn} 拼接中')
  full_path = dl_dir / 'full.pkl'
  with open(full_path, 'wb') as out:
    for p in chunk_paths:
      out.write(p.read_bytes())

  # sha256 校验
  write_progress(f'{sn} 校验中')
  h = hashlib.sha256(full_path.read_bytes()).hexdigest()
  if sha256_expected and h != sha256_expected:
    write_progress(f'{sn} 校验失败')
    print('sha256 不匹配, 下载文件损坏')
    full_path.unlink(missing_ok=True)
    return 1
  print('sha256 校验通过')

  # supercombo 合并类型检测 (当前不支持, 安装前拦截)
  try:
    _sp_tg = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tinygrad_sp')
    if os.path.isdir(_sp_tg) and _sp_tg not in sys.path:
      sys.path.insert(0, _sp_tg)
    if 'DEV' not in os.environ:
      os.environ['DEV'] = 'QCOM'
    with open(full_path, 'rb') as _f:
      _jits = pickle.load(_f)
    if 'model' in _jits.get('metadata', {}):
      write_progress(f'{sn} 类型不支持')
      print(f'[{sn}] supercombo 合并类型模型暂不支持, 已取消安装')
      full_path.unlink(missing_ok=True)
      shutil.rmtree(dl_dir, ignore_errors=True)
      return 1
    del _jits
    print('类型检测通过 (split)')
  except Exception as e:
    print(f'  类型检测跳过: {type(e).__name__}: {str(e)[:60]}')

  # 原子安装
  target_dir.mkdir(parents=True, exist_ok=True)
  shutil.move(str(full_path), str(target_dir / 'driving_tinygrad.pkl'))
  print(f'已安装: {target_dir}')
  write_progress(f'{sn} 完成')

  # 清理临时文件
  shutil.rmtree(dl_dir, ignore_errors=True)
  cmd_list()
  return 0


# ---------------- 切换 / 删除 / 占用 ----------------
def cmd_switch(idx):
  models = list_local()
  m = next((x for x in models if x['idx'] == idx), None)
  if m is None:
    print('无效编号, 可用: %s' % [x['idx'] for x in models])
    return 1
  from openpilot.common.params import Params
  Params().put('modelid', str(idx))
  print(f'已切换 modelid={idx} ({m["name"]}), 重启设备生效 (完整初始化, 约 1-2 分钟)...')
  os.system('sudo -n reboot || sudo reboot || reboot')
  return 0


def cmd_delete(idx):
  models = list_local()
  m = next((x for x in models if x['idx'] == idx), None)
  if m is None:
    print('无效编号')
    return 1
  cur = get_modelid()
  if m['idx'] == cur:
    print('不能删除当前激活模型, 请先切换')
    return 1
  import shutil as _s
  _s.rmtree(m['dir'], ignore_errors=True)
  print(f'已删除 [{m["idx"]}] {m["name"]}')
  return 0


def cmd_usage():
  models = list_local()
  total = 0
  print('磁盘占用:')
  for m in sorted(models, key=lambda x: -x['size']):
    total += m['size']
    print(f"  {fmt_size(m['size']):>9}  [{m['idx']}] {m['name']}")
  print(f'  总计: {fmt_size(total)}')


def main():
  ap = argparse.ArgumentParser(description='驾驶模型管理器')
  ap.add_argument('cmd', choices=['list', 'manifest', 'download', 'switch', 'delete', 'usage'])
  ap.add_argument('arg', nargs='?', default=None)
  ap.add_argument('--json', action='store_true', help='JSON 输出 (UI 用)')
  args = ap.parse_args()
  if args.cmd == 'list':
    return cmd_list(json_out=args.json)
  if args.cmd == 'manifest':
    return cmd_manifest(json_out=args.json)
  if args.cmd == 'download':
    if not args.arg:
      print('用法: download <name|idx>')
      return 1
    return cmd_download(args.arg)
  if args.cmd == 'switch':
    if not args.arg:
      print('用法: switch <idx>')
      return 1
    return cmd_switch(int(args.arg))
  if args.cmd == 'delete':
    if not args.arg:
      print('用法: delete <idx>')
      return 1
    return cmd_delete(int(args.arg))
  if args.cmd == 'usage':
    return cmd_usage()
  return 0


if __name__ == '__main__':
  sys.exit(main())
