#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DM 3D 查看器 · 本地小服务（只用标准库，无第三方依赖）

  python server.py                 # 默认 127.0.0.1:8123，自动打开浏览器
  python server.py --port 8124
  python server.py --dir "E:\\其他模型目录"

它做三件事：
  1. 把本目录的网页（index.html + three.js）当静态文件发出去
  2. /api/list  扫描 ComfyUI 的 output 目录，列出所有 3D 资产（glb/gltf/obj/ply/stl/fbx）
  3. /api/file  带 Range 的流式下载（66 MB 的 glb 也不会爆内存）

安全：只绑 127.0.0.1（本机），只允许读取白名单根目录下的文件，拒绝 ../ 穿越。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EXT3D = {".glb": "GLB", ".gltf": "glTF", ".obj": "OBJ", ".ply": "PLY",
         ".stl": "STL", ".fbx": "FBX", ".splat": "Splat", ".3mf": "3MF"}

DEFAULT_ROOTS = [
    r"D:\ComfyUI_experimental\ComfyUI_windows_portable\ComfyUI\output",
    r"F:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\ComfyUI\output",
    r"E:\游戏素材库",          # 3D 成品放这里（E:\游戏素材库\3D模型）
    os.path.join(HERE, "models"),
]

MIME = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
        ".bin": "application/octet-stream", ".obj": "text/plain; charset=utf-8",
        ".mtl": "text/plain; charset=utf-8", ".ply": "application/octet-stream",
        ".stl": "application/octet-stream", ".fbx": "application/octet-stream",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".ktx2": "image/ktx2", ".hdr": "image/vnd.radiance"}

STATE = {"roots": []}


def norm(p):
    return os.path.normcase(os.path.abspath(p))


def in_roots(path):
    p = norm(path)
    # 网页自己的静态文件（three.js 等）永远允许
    if p == norm(HERE) or p.startswith(norm(HERE) + os.sep):
        return True
    for r in STATE["roots"]:
        root = norm(r)
        if p == root or p.startswith(root + os.sep):
            return True
    return False


def scan(limit=500):
    out = []
    for root in STATE["roots"]:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过明显无关的大目录
            dirnames[:] = [d for d in dirnames if d.lower() not in
                           ("__pycache__", ".git", "node_modules", "input", "temp")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in EXT3D:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append({
                    "name": fn,
                    "path": fp,
                    "rel": os.path.relpath(fp, root),
                    "root": root,
                    "ext": EXT3D.get(ext, ext),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
                if len(out) >= limit * 4:
                    break
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


class Handler(BaseHTTPRequestHandler):
    server_version = "DM3DViewer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("DM3D_VERBOSE"):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    # ---------- GET / HEAD ----------
    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/list":
            return self._json({"roots": STATE["roots"], "items": scan()})

        if path == "/api/roots":
            return self._json({"roots": STATE["roots"]})

        if path == "/api/file":
            fp = (qs.get("p") or [""])[0]
            return self._send_file(fp, head)
        if path == "/api/open":       # 用系统默认程序打开这个文件（方便拖去别的软件）
            fp = (qs.get("p") or [""])[0]
            if not fp or not in_roots(fp) or not os.path.isfile(fp):
                return self._err(400, "路径不在白名单内")
            try:
                os.startfile(fp)      # noqa: S606 (Windows only)
                return self._json({"ok": True})
            except Exception as e:
                return self._err(500, repr(e))

        # 静态文件
        rel = path.lstrip("/") or "index.html"
        if ".." in rel.replace("\\", "/").split("/"):
            return self._err(403, "forbidden")
        fp = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(fp):
            return self._err(404, "not found: " + rel)
        return self._send_file(fp, head)

    # ---------- 文件流（支持 Range）----------
    def _send_file(self, fp, head=False, force_type=None):
        if not fp or not os.path.isfile(fp):
            return self._err(404, "文件不存在")
        if not in_roots(fp):
            return self._err(403, "路径不在白名单内")
        size = os.path.getsize(fp)
        ext = os.path.splitext(fp)[1].lower()
        ctype = force_type or MIME.get(ext) or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except Exception:
                start, end = 0, size - 1
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head:
            return
        with open(fp, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def pick_port(preferred=8123):
    for p in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("没有空闲端口（8123-8152）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--dir", action="append", default=[], help="额外的资产目录，可多次给")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--strict-port", action="store_true",
                    help="端口被占就直接退出（常驻托管用，避免第二个实例跑到 8124）")
    a = ap.parse_args()

    roots = list(DEFAULT_ROOTS) + list(a.dir)
    env = os.environ.get("DM3D_DIRS", "")
    if env:
        roots += [p for p in env.split(";") if p.strip()]
    STATE["roots"] = [r for r in roots if os.path.isdir(r)] or [HERE]

    if a.strict_port:
        # ⚠️ Windows 的 SO_REUSEADDR 允许两个进程绑同一端口（bind 测不出来），
        # 所以用 connect 试探：连得上 = 已经有人在服务
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sk:
            _sk.settimeout(0.6)
            busy = _sk.connect_ex(("127.0.0.1", a.port)) == 0
        if busy:
            print("端口 %d 已经在服务（常驻实例），本实例退出" % a.port)
            return
        port = a.port
    else:
        port = pick_port(a.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    print("=" * 62)
    print("  DM 3D 查看器已启动:  %s" % url)
    for r in STATE["roots"]:
        print("   扫描目录: %s" % r)
    print("  关闭这个窗口 = 停止服务")
    print("=" * 62)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
