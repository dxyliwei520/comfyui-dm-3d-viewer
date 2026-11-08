#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
DM 3D 资产自动归档器 —— 让"每次手动点保存"彻底消失

背景：ComfyUI 的 3D 预览节点只给你一个"下载"按钮，文件其实早就写在本机
      (ComfyUI\output\3d\ComfyUI_000xx.glb)。点下载只会多出一份 66 MB 的
      model_<毫秒>.glb 堆在下载目录里，还每次弹"另存为"。

本脚本：盯住这几个目录，**一出现新的 3D 文件就自动归档到**
      E:\\游戏素材库\\3D模型\\<YYYY-MM-DD>\\<HHMMSS>_<原名>
  · 只处理"新出现"的（首次运行会把现有文件记为基线，不复制，避免一次塞几百 MB）
  · **按指纹去重**（大小 + 前 1 MB 的 sha1）：同一个模型下载三次也只留一份
  · 桌面/浏览器下载目录里那种 model_<毫秒>.glb 也认
  · 全程只读源文件（复制不删除），状态存 _archive_state.json

用法：
    python archiver.py                 # 持续盯（每 5 秒扫一次）
    python archiver.py --once          # 只扫一轮
    python archiver.py --import-existing   # 把现在已有的也归档（默认只记基线）
    python archiver.py --interval 10
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

# 归档库：优先环境变量 DM3D_LIB；否则用这台机器惯用的 E:\游戏素材库\3D模型（存在就用）；
# 都没有就落到 ~/DM3D-Assets，保证换台机器也能直接跑
LIB = os.environ.get("DM3D_LIB") or (
    r"E:\游戏素材库\3D模型" if os.path.isdir(r"E:\游戏素材库") else os.path.join(HOME, "DM3D-Assets"))
LIB = os.path.abspath(os.path.expanduser(LIB))

STATE = os.path.join(HERE, "_archive_state.json")
LOG = os.path.join(LIB, "_archive.log")
EXT3D = {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".3mf"}

# 监听目录：环境变量 DM3D_WATCH（分号分隔）可覆盖；否则用这几种常见位置（不存在的自动跳过）
_default_watch = [
    r"D:\ComfyUI_experimental\ComfyUI_windows_portable\ComfyUI\output",
    r"F:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\ComfyUI\output",
    r"E:\WBE下载目录",                      # 这台机器的浏览器下载目录
    os.path.join(HOME, "Downloads"),         # 其它机器默认
]
WATCH = [w for w in (os.environ.get("DM3D_WATCH") or "").split(os.pathsep) if w.strip()] or _default_watch
SKIP_DIR = {"__pycache__", ".git", "node_modules", "input", "temp"}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fingerprint(path):
    """大小 + 前 1 MB 的 sha1（快、够区分）"""
    try:
        st = os.stat(path)
        h = hashlib.sha1(str(st.st_size).encode())
        with open(path, "rb") as f:
            h.update(f.read(1024 * 1024))
        return "%d:%s" % (st.st_size, h.hexdigest()[:16])
    except OSError:
        return ""


def walk_3d(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIR]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in EXT3D:
                yield os.path.join(dirpath, fn)


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("seen", {})
            s.setdefault("lib_fp", [])
            return s
        except Exception:
            pass
    return {"seen": {}, "lib_fp": []}


def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def dest_for(src):
    st = os.stat(src)
    day = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
    stamp = time.strftime("%H%M%S", time.localtime(st.st_mtime))
    name = os.path.basename(src)
    stem, ext = os.path.splitext(name)
    # 下载文件名 model_1794136052939.glb → 用时间戳前缀，别留一坨毫秒
    if stem.startswith("model_") and stem[6:].isdigit():
        stem = "comfy3d"
    d = os.path.join(LIB, day)
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "%s_%s%s" % (stamp, stem, ext))
    n = 2
    while os.path.exists(out):
        out = os.path.join(d, "%s_%s_%d%s" % (stamp, stem, n, ext))
        n += 1
    return out


def scan_once(state, import_existing=False, first_pass=False):
    new_files = 0
    for root in WATCH:
        if not os.path.isdir(root):
            continue
        for fp in walk_3d(root):
            try:
                st = os.stat(fp)
            except OSError:
                continue
            key = os.path.normcase(fp)
            prev = state["seen"].get(key)
            sig = {"size": st.st_size, "mtime": int(st.st_mtime)}
            is_new = prev is None
            changed = prev is not None and (prev.get("size") != sig["size"])
            state["seen"][key] = sig
            if is_new and first_pass and not import_existing:
                continue                      # 首次运行只记基线
            if not (is_new or changed):
                continue
            if st.st_size < 20 * 1024:        # 20 KB 以下多半是缩略图/坏文件
                continue
            fpq = fingerprint(fp)
            if not fpq:
                continue
            if fpq in state["lib_fp"]:
                log("跳过（库里已有同一模型）: %s" % os.path.basename(fp))
                continue
            dst = dest_for(fp)
            try:
                shutil.copy2(fp, dst)
                state["lib_fp"].append(fpq)
                new_files += 1
                log("归档 %6.1f MB  %s  ->  %s" % (st.st_size / 1048576, os.path.basename(fp),
                                                  os.path.relpath(dst, LIB)))
            except Exception as e:
                log("复制失败 %s: %r" % (fp, e))
    return new_files


LOCK = os.path.join(HERE, "_archiver.lock")


def already_running():
    """简单单实例：锁文件里存 PID，进程还在就直接退出"""
    try:
        if os.path.exists(LOCK):
            with open(LOCK) as f:
                pid = int((f.read() or "0").strip() or 0)
            if pid > 0:
                try:
                    os.kill(pid, 0)          # Windows 上也能用，进程不存在会抛错
                    return True
                except OSError:
                    pass
    except Exception:
        pass
    try:
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--import-existing", action="store_true")
    ap.add_argument("--interval", type=int, default=5)
    a = ap.parse_args()

    if already_running():
        print("已有归档器在跑（%s），本次退出" % LOCK)
        return
    os.makedirs(LIB, exist_ok=True)
    state = load_state()
    log("=== 归档器启动  库=%s  监听 %d 个目录%s ===" %
        (LIB, len([w for w in WATCH if os.path.isdir(w)]),
         "（含把现有文件也归档）" if a.import_existing else "（首轮只记基线）"))
    n = scan_once(state, a.import_existing, first_pass=True)
    save_state(state)
    log("首轮完成：新归档 %d 个" % n)
    if a.once:
        return
    while True:
        time.sleep(a.interval)
        n = scan_once(state)
        if n:
            save_state(state)


if __name__ == "__main__":
    main()
