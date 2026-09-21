#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
录像下载服务(轻量, 纯 Python 标准库, 不依赖 flask)。

背景:
  「录像管理」里"扫码下载"生成的二维码指向
      http://<设备IP>:8082/screenrecords/download/<file>
  该端点原本由 frogpilot 的 fleet_manager(Flask) 提供。但本设备未安装 flask,
  于是 process_config.check_fleet() 恒为 False -> fleet_manager 从不启动 ->
  8082 端口无监听 -> 手机扫码后浏览器报"网络出错/无法显示该页面"(连接被拒绝)。
  rootfs 只读(/ 为 ro)无法 pip 安装 flask, 因此用标准库 http.server 自建
  同端口、同路径的服务, 与 UI 二维码完全兼容。

路由:
  GET /                                  -> 录像列表页(手机浏览器可读)
  GET /screenrecords                     -> 同上
  GET /screenrecords/download/<file>     -> 附件下载(支持 Range 断点续传)
  GET /screenrecords/play/pipe/<file>    -> 内联播放(不触发下载)
  GET /ping                              -> ok (存活探测)

安全:
  只接受单层文件名(白名单正则) + realpath 前缀校验, 杜绝路径穿越。

注意:
  本模块被 system/manager 以 PythonProcess 方式托管: manager 会先 import 本模块
  (prepare), 再由 launcher 调用 main()。因此顶层不得有副作用, 端口绑定只在 main() 内进行。
"""

import html
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

REC_DIR = "/data/media/0/videos"
HOST = "0.0.0.0"
PORT = 8082

CHUNK = 256 * 1024

# 只允许单层安全文件名: 首字符字母或数字, 其余为字母/数字/点/下划线/减号
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MIME = {
  ".mp4": "video/mp4",
  ".zip": "application/zip",
  ".tar": "application/x-tar",
}

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>录像下载</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;padding:16px;background:#171717;color:#E8E8E8;
      font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 h1{font-size:22px;margin:4px 0 6px}
 .sub{font-size:13px;color:#9E9E9E;margin:0 0 14px}
 ul{list-style:none;margin:0;padding:0}
 li{background:#232323;border-radius:14px;padding:14px 16px;margin-bottom:12px}
 .n{font-size:16px;font-weight:600;word-break:break-all}
 .m{font-size:13px;color:#A8A8A8;margin-top:4px}
 .a{margin-top:10px;display:flex;gap:10px}
 a{flex:1;text-align:center;text-decoration:none;color:#fff;padding:10px 0;
   border-radius:10px;font-size:15px}
 .dl{background:#2F5C8A}
 .pl{background:#3A3A3A}
 .empty{text-align:center;color:#7A7A7A;padding:40px 0;background:transparent}
</style></head>
<body>
<h1>录像下载</h1>
<p class="sub">共 <!--COUNT--> 个文件 · 目录 /data/media/0/videos</p>
<ul><!--ROWS--></ul>
</body></html>
"""


def fmt_size(n):
  if n >= 1024 * 1024 * 1024:
    return "%.2f GB" % (n / 1024.0 / 1024.0 / 1024.0)
  if n >= 1024 * 1024:
    return "%.1f MB" % (n / 1024.0 / 1024.0)
  return "%.0f KB" % (n / 1024.0)


def list_clips():
  """返回 [(name, size, mtime)], 最新优先; 跳过 .lock 残留"""
  try:
    names = os.listdir(REC_DIR)
  except OSError:
    return []
  out = []
  for n in names:
    if n.startswith(".") or n.endswith(".lock"):
      continue
    p = os.path.join(REC_DIR, n)
    try:
      if not os.path.isfile(p):
        continue
      st = os.stat(p)
    except OSError:
      continue
    out.append((n, st.st_size, st.st_mtime))
  out.sort(key=lambda x: x[2], reverse=True)
  return out


class Handler(BaseHTTPRequestHandler):
  server_version = "ScreenRecordServer/1.0"
  protocol_version = "HTTP/1.1"   # 支持 keep-alive, Range 响应也要求 1.1

  # ---------- 基础 ----------
  def log_message(self, fmt, *args):
    print("[%s] %s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                          self.address_string(), fmt % args), flush=True)

  def _head_only(self):
    return getattr(self, "_is_head", False)

  def _send_bytes(self, data, ctype, code=200):
    self.send_response(code)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(data)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    if not self._head_only():
      try:
        self.wfile.write(data)
      except (BrokenPipeError, ConnectionResetError):
        pass

  def _send_text(self, code, msg):
    self._send_bytes(msg.encode("utf-8"), "text/plain; charset=utf-8", code)

  # ---------- 路径/文件名 ----------
  def _req_path(self):
    return unquote(urlparse(self.path).path)

  def _clip_name(self):
    """从 URL 取出并校验文件名; 不合法返回 None"""
    parts = [p for p in self._req_path().split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
      return None
    name = parts[-1]
    return name if SAFE_NAME.match(name) else None

  def _resolve(self, name):
    """拼出真实路径并确认仍在录像目录内"""
    root = os.path.realpath(REC_DIR)
    full = os.path.realpath(os.path.join(root, name))
    if not full.startswith(root + os.sep):
      return None
    return full if os.path.isfile(full) else None

  # ---------- 列表页 ----------
  def _send_index(self):
    clips = list_clips()
    rows = []
    for name, size, mtime in clips:
      ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
      q = html.escape(name, quote=True)
      rows.append(
        '<li><div class="n">%s</div><div class="m">%s · %s</div>'
        '<div class="a"><a class="dl" href="/screenrecords/download/%s">下载</a>'
        '<a class="pl" href="/screenrecords/play/pipe/%s">播放</a></div></li>'
        % (html.escape(name), ts, fmt_size(size), q, q))
    body = PAGE.replace("<!--ROWS-->", "".join(rows) or '<li class="empty">暂无录像文件</li>')
    body = body.replace("<!--COUNT-->", str(len(clips)))
    self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")

  # ---------- 文件发送(支持 Range) ----------
  def _send_file(self, full, name, inline=False):
    size = os.path.getsize(full)
    ext = os.path.splitext(name)[1].lower()
    ctype = MIME.get(ext, "application/octet-stream")

    start, end = 0, size - 1
    partial = False
    rng = self.headers.get("Range")
    if rng:
      m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip())
      if m:
        g1, g2 = m.group(1), m.group(2)
        if g1:
          start = int(g1)
          end = int(g2) if g2 else size - 1
        elif g2:
          start = max(0, size - int(g2))
        if size == 0 or start > end or start >= size:
          self.send_response(416)
          self.send_header("Content-Range", "bytes */%d" % size)
          self.send_header("Content-Length", "0")
          self.end_headers()
          return
        end = min(end, size - 1)
        partial = True

    length = end - start + 1
    self.send_response(206 if partial else 200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(length))
    self.send_header("Accept-Ranges", "bytes")
    self.send_header("Cache-Control", "no-store")
    disp = "inline" if inline else "attachment"
    self.send_header("Content-Disposition",
                     '%s; filename="%s"; filename*=UTF-8\'\'%s'
                     % (disp, name, unquote(name)))
    if partial:
      self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
    self.end_headers()

    if self._head_only():
      return

    remain = length
    try:
      with open(full, "rb") as f:
        f.seek(start)
        while remain > 0:
          buf = f.read(min(CHUNK, remain))
          if not buf:
            break
          self.wfile.write(buf)
          remain -= len(buf)
    except (BrokenPipeError, ConnectionResetError):
      pass   # 客户端(手机)提前断开, 正常现象

  # ---------- HTTP 方法 ----------
  def _dispatch(self):
    path = self._req_path()

    if path in ("/", "/screenrecords", "/screenrecords/"):
      self._send_index()
      return

    if path == "/ping":
      self._send_text(200, "ok")
      return

    if path.startswith("/screenrecords/download/") or path.startswith("/screenrecords/play/pipe/"):
      name = self._clip_name()
      if name is None:
        self._send_text(400, "bad filename")
        return
      full = self._resolve(name)
      if full is None:
        self._send_text(404, "file not found")
        return
      self._send_file(full, name, inline="/play/pipe/" in path)
      return

    self._send_text(404, "not found")

  def do_GET(self):
    self._is_head = False
    self._dispatch()

  def do_HEAD(self):
    self._is_head = True
    self._dispatch()


def main():
  os.makedirs(REC_DIR, exist_ok=True)
  try:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
  except OSError as e:
    print("screenrecord_server: bind %s:%d failed -> %s" % (HOST, PORT, e), flush=True)
    raise
  httpd.daemon_threads = True
  print("screenrecord_server: listening on http://%s:%d/ (dir=%s, pid=%d)"
        % (HOST, PORT, REC_DIR, os.getpid()), flush=True)
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    httpd.server_close()
    print("screenrecord_server: stopped", flush=True)


if __name__ == "__main__":
  main()
