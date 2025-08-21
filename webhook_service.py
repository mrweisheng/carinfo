#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask Webhook 服务：
- POST /webhook/update 触发运行 update_all_transport_purpose.py 脚本（子进程方式）
- 可选鉴权：设置环境变量 WEBHOOK_SECRET 时，需在请求中携带 token（?token= 或 Authorization: Bearer）
- 运行时将脚本的 stdout/stderr 重定向到 log/ 目录下按时间命名的日志文件
- GET /status 查看当前运行状态
- GET /health 健康检查

运行：
  set FLASK_ENV=production
  python webhook_service.py
或：
  python -m flask --app webhook_service:app run --host 0.0.0.0 --port 8080
"""

import os
import sys
import threading
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from flask import Flask, request, jsonify

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 先加载 .env，以便使用其中的 WEBHOOK_* 等配置
load_dotenv()

SCRIPT_PATH = os.path.join(BASE_DIR, "update_all_transport_purpose.py")
LOG_DIR = os.path.join(BASE_DIR, "log")

HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.getenv("WEBHOOK_PORT", "7878"))
SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

app = Flask(__name__)

_state_lock = threading.Lock()
_current: Dict[str, Any] = {
    "running": False,
    "pid": None,
    "log_file": None,
    "start_time": None,
    "returncode": None,
}
_proc: Optional[subprocess.Popen] = None
_log_fp = None  # type: ignore


def _ensure_log_dir():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)


def _verify_secret(req) -> Optional[str]:
    """返回 None 表示验证通过；返回错误字符串表示失败原因。"""
    if not SECRET:
        return None
    # 先查 Header: Authorization: Bearer <token>
    auth = req.headers.get("Authorization", "").strip()
    if auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
        if token == SECRET:
            return None
        return "invalid bearer token"
    # 再查 query 参数 token
    token_q = req.args.get("token", "")
    if token_q == SECRET:
        return None
    return "missing or invalid token"


def _monitor_thread():
    global _proc, _log_fp
    if _proc is None:
        return
    _proc.wait()
    with _state_lock:
        _current["returncode"] = _proc.returncode
        _current["running"] = False
        _current["pid"] = None
    try:
        if _log_fp:
            _log_fp.flush()
            _log_fp.close()
    except Exception:
        pass


@app.route("/webhook/update", methods=["POST"])
def webhook_update():
    global _proc, _log_fp

    auth_err = _verify_secret(request)
    if auth_err:
        return jsonify({"ok": False, "error": auth_err}), 401

    if not os.path.exists(SCRIPT_PATH):
        return jsonify({"ok": False, "error": f"script not found: {SCRIPT_PATH}"}), 500

    with _state_lock:
        if _current["running"]:
            return jsonify({
                "ok": False,
                "error": "update already running",
                "pid": _current["pid"],
                "log_file": _current["log_file"],
                "start_time": _current["start_time"],
            }), 409

        _ensure_log_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(LOG_DIR, f"update_{ts}.log")

        try:
            # 以二进制非缓冲模式打开，避免编码问题并确保实时写入
            _log_fp = open(log_path, "ab", buffering=0)
            # 为子进程强制启用 UTF-8 输出，避免中文日志乱码
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            cmd = [sys.executable, "-X", "utf8", "-u", SCRIPT_PATH]
            _proc = subprocess.Popen(
                cmd,
                cwd=BASE_DIR,
                stdout=_log_fp,
                stderr=subprocess.STDOUT,
                env=child_env,
            )
        except Exception as e:
            try:
                if _log_fp:
                    _log_fp.close()
            except Exception:
                pass
            return jsonify({"ok": False, "error": f"failed to start process: {e}"}), 500

        _current.update({
            "running": True,
            "pid": _proc.pid,
            "log_file": os.path.abspath(log_path),
            "start_time": ts,
            "returncode": None,
        })

        t = threading.Thread(target=_monitor_thread, daemon=True)
        t.start()

        return jsonify({
            "ok": True,
            "message": "update started",
            "pid": _current["pid"],
            "log_file": _current["log_file"],
            "start_time": ts,
        }), 202


@app.route("/status", methods=["GET"])
def status():
    with _state_lock:
        return jsonify({
            "ok": True,
            "running": _current["running"],
            "pid": _current["pid"],
            "log_file": _current["log_file"],
            "start_time": _current["start_time"],
            "returncode": _current["returncode"],
        })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "status": "healthy"})


if __name__ == "__main__":
    # 直接运行该脚本时，启动 Flask 开发服务器
    app.run(host=HOST, port=PORT)