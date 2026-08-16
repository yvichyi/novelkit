#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_writer.py —— 手机网页版写作台（零依赖，纯标准库）

打开手机浏览器 → 大按钮点按写作：
  - 写《介质》下一章 / 写新书下一章（后台跑，进度实时滚动）
  - 预览下一章 prompt（零成本）
  - 建新书（表单填写 + 可选 AI 生成设定）
  - 读已写章节 / 看老K评审反馈
  - 局域网内家人朋友手机/电脑也能连（同一 Wi-Fi）

启动：  python3 web_writer.py            # 默认 8080 端口
        python3 web_writer.py --port 9000
访问：  手机浏览器打开 http://127.0.0.1:8080（本机）
       或 http://<局域网IP>:8080（同 Wi-Fi 的家人朋友）
"""
import argparse
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BOOKS_DIR = SCRIPT_DIR / "books"
# 双模式 BASE（数据根判定）：
#  1) 本目录下有 已发布正文/ → 本目录就是《介质》数据根（BASE=本目录）
#  2) 父目录下有 已发布正文/ → 《介质》数据在父目录（原项目布局）
#  3) 都没有 → 通用仓库模式：默认书为空，用户用 books/* 或 new_novel.py 建书
if (SCRIPT_DIR / "已发布正文").exists():
    BASE = SCRIPT_DIR
elif (SCRIPT_DIR.parent / "已发布正文").exists():
    BASE = SCRIPT_DIR.parent
else:
    BASE = SCRIPT_DIR
ENV_FILE = SCRIPT_DIR / ".env"
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ================= 后台任务管理 =================
TASKS = {}
TASK_LOCK = threading.Lock()


class Task:
    def __init__(self, kind, title):
        self.id = f"{int(time.time())}_{len(TASKS)}"
        self.kind = kind
        self.title = title
        self.status = "running"   # running / done / error
        self.log = []
        self.created = time.time()
        self.proc = None

    def add_log(self, line):
        self.log.append(line.rstrip())
        if len(self.log) > 3000:      # 防内存膨胀，只留最近 3000 行
            self.log = self.log[-3000:]

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "status": self.status, "log": self.log}


def new_task(kind, title):
    with TASK_LOCK:
        t = Task(kind, title)
        TASKS[t.id] = t
        return t


def get_task(tid):
    with TASK_LOCK:
        return TASKS.get(tid)


def run_subprocess(tid, cmd, cwd, env=None):
    """后台跑一个子进程，stdout/stderr 实时进任务日志"""
    t = get_task(tid)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        t.proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=full_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        t.status = "error"
        t.add_log(f"❌ 启动失败：{e}")
        return
    for line in t.proc.stdout:
        t.add_log(line.rstrip("\n"))
    rc = t.proc.wait()
    t.status = "done" if rc == 0 else "error"
    if rc != 0:
        t.add_log(f"（退出码 {rc}）")


def run_in_thread(tid, fn):
    """后台跑一个 Python 函数（建书等），print 重定向进任务日志"""
    def _wrap():
        t = get_task(tid)
        buf = io.StringIO()
        class _W:
            def __init__(self, t, buf): self.t, self.buf = t, buf
            def write(self, s):
                self.buf.write(s)
                if "\n" in s:
                    for ln in s.split("\n"):
                        if ln.strip():
                            self.t.add_log(ln.strip())
            def flush(self):
                if self.buf.getvalue():
                    self.t.add_log(self.buf.getvalue().rstrip())
                    self.buf.seek(0); self.buf.truncate(0)
        w = _W(t, buf)
        try:
            with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                fn()
            t.status = "done"
        except Exception as e:
            t.status = "error"
            t.add_log(f"❌ 出错：{e}")
    threading.Thread(target=_wrap, daemon=True).start()


# ================= 书与文件 =================
def list_books():
    """返回 [{id, name, is_default, chapters, next_no, dir}]"""
    out = []
    # 《介质》默认书
    pub = BASE / "已发布正文"
    chs = sorted(pub.glob("第*章.md")) if pub.exists() else []
    out.append({"id": "__default__", "name": "《介质》", "dir": str(SCRIPT_DIR),
                "is_default": True, "chapters": len(chs), "next_no": len(chs) + 1})
    # books/ 下的新书
    if BOOKS_DIR.exists():
        for d in sorted(BOOKS_DIR.iterdir()):
            if not d.is_dir():
                continue
            p2 = d / "已发布正文"
            chs2 = sorted(p2.glob("第*章.md")) if p2.exists() else []
            out.append({"id": d.name, "name": d.name, "dir": str(d),
                        "is_default": False, "chapters": len(chs2), "next_no": len(chs2) + 1})
    return out


def book_dir(bid):
    if bid == "__default__":
        return SCRIPT_DIR
    return BOOKS_DIR / bid


def book_published(bid):
    d = book_dir(bid)
    return (BASE / "已发布正文") if bid == "__default__" else (d / "已发布正文")


def chapter_list(bid):
    pub = book_published(bid)
    out = []
    for p in sorted(pub.glob("第*章.md")):
        mo = re.search(r"第(\d+)章", p.name)
        if not mo:
            continue
        no = int(mo.group(1))
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
        title = ""
        if txt:
            first = txt.splitlines()[0]
            title = re.sub(r"^#*\s*第\d+章\s*", "", first).strip()
        out.append({"no": no, "title": title, "chars": len(re.sub(r"\s", "", txt))})
    return out


def load_env_key():
    for ep in (ENV_FILE, BASE / ".env"):
        if ep.exists():
            try:
                for line in ep.read_text(encoding="utf-8").splitlines():
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return ""


def save_env_key(key):
    (ENV_FILE).write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
    try:
        (BASE / ".env").write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
    except Exception:
        pass


# ================= 引擎参数（novel_config/engine.json，每本书可调）=================
def engine_path(bid):
    return book_dir(bid) / "novel_config" / "engine.json"


def load_engine(bid):
    p = engine_path(bid)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def save_engine(bid, data):
    p = engine_path(bid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# ================= 写章 / 预览 =================
def start_write(bid, count, key, pro_from=None, start=None):
    t = new_task("write", f"写「{'《介质》' if bid=='__default__' else bid}」{count} 章" + (f"（从第{start}章）" if start else ""))
    d = book_dir(bid)
    env = {}
    if key:
        env["DEEPSEEK_API_KEY"] = key
    if bid != "__default__":
        env["NOVEL_DIR"] = str(d)
    cmd = [PY, "-u", "write_chapter.py", "--write", str(count)]
    if start:
        cmd += ["--start", str(start)]
    if pro_from:
        cmd += ["--pro-from", str(pro_from)]
    # cwd 必须永远是脚本目录（write_chapter.py 在这）；新书靠 NOVEL_DIR 环境变量定位
    threading.Thread(target=run_subprocess, args=(t.id, cmd, SCRIPT_DIR, env), daemon=True).start()
    return t


def start_preview(bid):
    t = new_task("preview", f"预览「{'《介质》' if bid=='__default__' else bid}」下一章 prompt")
    d = book_dir(bid)
    env = {}
    if bid != "__default__":
        env["NOVEL_DIR"] = str(d)
    cmd = [PY, "-u", "write_chapter.py", "--preview"]
    threading.Thread(target=run_subprocess, args=(t.id, cmd, SCRIPT_DIR, env), daemon=True).start()
    return t


# ================= 建新书 =================
def start_newbook(name, intro, protagonist, total, use_ai, key, stages=None):
    t = new_task("newbook", f"建新书《{name}》")
    total_n = max(int(total), 1)
    if stages:
        # 前端传来自定义阶段（已校验过）：[{"name","ch_lo","ch_hi","desc"}]
        for s in stages:
            s["ch_lo"] = max(int(s["ch_lo"]), 1)
            s["ch_hi"] = min(max(int(s["ch_hi"]), s["ch_lo"]), total_n)
    else:
        # 生成默认 3 阶段
        if total_n <= 3:
            thirds = [total_n]
        else:
            a = max(1, total_n // 3)
            b = max(a + 1, (total_n * 2) // 3)
            thirds = [a, b - a, total_n - b + 1]
        stages = []
        lo = 1
        for i, ln in enumerate(thirds):
            hi = min(lo + ln - 1, total_n)
            if lo > total_n:
                break
            stages.append({"name": ["开端", "发展", "终局"][i] if i < 3 else f"阶段{i+1}",
                           "ch_lo": lo, "ch_hi": hi, "desc": ""})
            lo = hi + 1
    def _fn():
        sys.path.insert(0, str(SCRIPT_DIR))
        import new_novel
        new_novel.create_book(name, intro, protagonist, stages, use_ai, key)
    run_in_thread(t.id, _fn)
    return t


# ================= HTTP =================
class Handler(BaseHTTPRequestHandler):
    server_version = "WebWriter/1.0"

    def log_message(self, fmt, *args):
        pass   # 静默，避免刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _text(self, s, code=200):
        self._send(code, s.encode("utf-8"), "text/plain; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        q = urllib.parse.parse_qs(url.query)

        if path == "/" or path == "/index.html":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"ok": True, "books": list_books(), "ip": local_ip(),
                        "has_key": bool(load_env_key()), "tasks": len(TASKS)})
        elif path == "/api/books":
            self._json({"books": list_books()})
        elif path == "/api/task":
            t = get_task(q.get("id", [""])[0])
            if not t:
                self._json({"error": "task not found"}, 404)
            else:
                self._json({"task": t.to_dict()})
        elif path == "/api/chapters":
            bid = q.get("book", ["__default__"])[0]
            self._json({"book": bid, "chapters": chapter_list(bid)})
        elif path == "/api/chapter":
            bid = q.get("book", ["__default__"])[0]
            no = q.get("no", ["1"])[0]
            d = book_dir(bid)
            pub = book_published(bid)
            p = pub / f"第{no}章.md"
            if not p.exists():
                self._json({"error": f"第{no}章不存在"}, 404)
            else:
                self._text(p.read_text(encoding="utf-8", errors="replace"))
        elif path == "/api/review":
            bid = q.get("book", ["__default__"])[0]
            no = q.get("no", ["1"])[0]
            d = book_dir(bid)
            p = (d / "ledger" / "评审反馈" / f"第{no}章.md")
            if not p.exists():
                self._json({"error": f"第{no}章暂无评审反馈"}, 404)
            else:
                self._text(p.read_text(encoding="utf-8", errors="replace"))
        elif path == "/api/engine":
            bid = q.get("book", ["__default__"])[0]
            self._json({"book": bid, "engine": load_engine(bid),
                        "defaults": {"write": {"max_tokens": 12000, "thinking": "enabled",
                                               "reasoning_effort": "medium"},
                                     "review": {"max_tokens": 1200, "thinking": "disabled"},
                                     "polish": {"max_tokens": 12000, "thinking": "disabled"}}})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
        except Exception:
            body = {}

        if path == "/api/write":
            bid = body.get("book", "__default__")
            count = max(int(body.get("count", 1)), 1)
            start = body.get("start") or None
            if start is not None:
                start = max(int(start), 1)
            key = body.get("key") or load_env_key()
            t = start_write(bid, count, key, start=start)
            self._json({"task": t.to_dict()})
        elif path == "/api/preview":
            bid = body.get("book", "__default__")
            t = start_preview(bid)
            self._json({"task": t.to_dict()})
        elif path == "/api/newbook":
            name = body.get("name", "").strip()
            if not name:
                self._json({"error": "书名不能为空"}, 400)
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip()
            if (BOOKS_DIR / safe).exists():
                self._json({"error": f"「{name}」已存在，换个书名"}, 400)
            use_ai = bool(body.get("ai", True))
            key = body.get("key") or load_env_key()
            if use_ai and not key:
                self._json({"error": "AI 生成设定需要 API Key，请先在「设置」里填 Key"}, 400)
            total = max(int(body.get("total", 30)), 1)
            stages = None
            raw = body.get("stages")  # 格式："阶段名:起-止" 每行一个，如 "开端:1-10"
            if raw:
                stages = []
                for ln in str(raw).splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    if ":" in ln:
                        nm, rng = ln.split(":", 1)
                    else:
                        nm, rng = f"阶段{len(stages)+1}", ln
                    nm = nm.strip() or f"阶段{len(stages)+1}"
                    rng = rng.strip().replace("第", "").replace("章", "").replace("～", "-").replace("~", "-").replace("—", "-")
                    parts = [p for p in rng.split("-") if p.strip().isdigit()]
                    if len(parts) < 2:
                        self._json({"error": f"阶段「{ln}」格式不对，示例：开端:1-10"}, 400)
                    stages.append({"name": nm, "ch_lo": int(parts[0]), "ch_hi": int(parts[1]), "desc": ""})
            t = start_newbook(name,
                              body.get("intro", "").strip(),
                              body.get("protagonist", "").strip(),
                              total, use_ai, key, stages=stages)
            self._json({"task": t.to_dict()})
        elif path == "/api/savekey":
            key = body.get("key", "").strip()
            if key.startswith("sk-"):
                save_env_key(key)
                self._json({"ok": True})
            else:
                self._json({"error": "Key 格式不对（应以 sk- 开头）"}, 400)
        elif path == "/api/engine":
            bid = body.get("book", "__default__")
            data = body.get("engine")
            if not isinstance(data, dict) or not isinstance(data.get("write"), dict):
                self._json({"error": "参数格式不对（需要 engine.write 对象）"}, 400)
            save_engine(bid, data)
            self._json({"ok": True, "saved": data})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="手机网页版写作台（零依赖）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0", help="默认 0.0.0.0=局域网可访问；本机只用 127.0.0.1")
    args = ap.parse_args()

    print("╔══════════════════════════════════════════╗")
    print("║   📖 网页版写作台 · 手机浏览器打开即可用    ║")
    print("╚══════════════════════════════════════════╝")
    print(f"  本机访问：   http://127.0.0.1:{args.port}")
    print(f"  家人朋友：   http://{local_ip()}:{args.port}  （同一 Wi-Fi）")
    print(f"  已有 Key：   {'✅ 已配置' if load_env_key() else '⚠️ 未配置（进网页点「设置」填）'}")
    print()
    print("  Ctrl+C 停止服务。")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>写作台 · 傻瓜模式</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--sub:#94a3b8;
        --pri:#fbbf24;--ok:#34d399;--err:#f87171;--run:#60a5fa;}
  *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
  body{font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;background:var(--bg);color:var(--txt);
       max-width:560px;margin:0 auto;padding:12px 14px 90px;font-size:16px;}
  h1{font-size:22px;text-align:center;padding:16px 0 4px;}
  .sub{text-align:center;color:var(--sub);font-size:13px;margin-bottom:16px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0;}
  .card h2{font-size:15px;color:var(--sub);margin-bottom:10px;font-weight:600;}
  button{width:100%;border:0;border-radius:14px;padding:16px;font-size:17px;font-weight:700;
         cursor:pointer;color:#0f172a;background:var(--pri);margin:6px 0;}
  button.sec{background:#334155;color:#e2e8f0;}
  button.danger{background:var(--err);color:#fff;}
  button:disabled{opacity:.45;cursor:not-allowed;}
  .row{display:flex;gap:8px;} .row>*{flex:1;}
  input,select,textarea{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);
        background:#0f172a;color:var(--txt);font-size:16px;margin:6px 0;}
  label{font-size:13px;color:var(--sub);}
  .log{background:#0b1120;border:1px solid var(--line);border-radius:12px;padding:12px;
       font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.6;
       white-space:pre-wrap;word-break:break-all;max-height:420px;overflow-y:auto;display:none;}
  .status{font-size:13px;margin-top:8px;min-height:20px;color:var(--sub);}
  .ok{color:var(--ok);} .err{color:var(--err);} .run{color:var(--run);}
  .chip{display:inline-block;background:#334155;border-radius:999px;padding:4px 12px;font-size:12px;margin:2px 4px 2px 0;color:var(--txt);}
  .bookbtn{border:1px solid var(--line);border-radius:14px;padding:14px;margin:6px 0;background:#0f172a;
           cursor:pointer;display:flex;justify-content:space-between;align-items:center;width:100%;text-align:left;font-size:16px;color:var(--txt);}
  .bookbtn.sel{border-color:var(--pri);background:#1a2436;}
  .bookbtn small{color:var(--sub);font-size:12px;}
  .chaps{max-height:340px;overflow-y:auto;}
  .chap{padding:10px 2px;border-bottom:1px dashed var(--line);cursor:pointer;display:flex;justify-content:space-between;}
  .chap small{color:var(--sub);}
  pre.prompt{background:#0b1120;border:1px solid var(--line);border-radius:12px;padding:12px;font-size:12px;
             white-space:pre-wrap;word-break:break-all;max-height:480px;overflow-y:auto;}
  .big{padding:22px;font-size:20px;}
  footer{text-align:center;color:#475569;font-size:12px;padding:20px 0 10px;}
  .hidden{display:none;}
</style>
</head>
<body>
<h1>📖 写作台</h1>
<div class="sub" id="addr"></div>

<!-- 书选择 -->
<div class="card">
  <h2>📚 当前书</h2>
  <div id="booklist"></div>
</div>

<!-- 主操作 -->
<div class="card">
  <button class="big" id="btnWrite" onclick="doWrite()">✍️ 写下一章</button>
  <div class="row">
    <div><label>起始章（留空=下一章）</label>
      <input id="wtStart" type="number" min="1" placeholder="自动"></div>
    <div><label>写几章</label>
      <input id="wtCount" type="number" min="1" value="1"></div>
  </div>
  <div class="row">
    <button class="sec" onclick="doPreview()">👁 预览 prompt</button>
    <button class="sec" onclick="showChaps()">📄 已写章节</button>
  </div>
  <div class="status" id="stWrite"></div>
  <div class="log" id="logWrite"></div>
</div>

<!-- 建新书 -->
<div class="card" id="cardNew">
  <h2>✨ 建一本新书</h2>
  <input id="nbName" placeholder="书名（必填，如：我的小说）">
  <input id="nbIntro" placeholder="一句话简介（可选）">
  <input id="nbProt" placeholder="主角名（可选）">
  <div class="row">
    <input id="nbTotal" type="number" min="1" value="30" placeholder="总章数">
  </div>
  <label>阶段规划（每行一个「阶段名:起-止章」，留空=自动3阶段）</label>
  <textarea id="nbStages" rows="3" placeholder="开端:1-10&#10;发展:11-25&#10;终局:26-30"></textarea>
  <label><input type="checkbox" id="nbAi" checked> 用 AI 生成世界观/大纲/脑洞/红线（需 Key，约1分钟）</label>
  <button class="sec" onclick="doNewBook()">🚀 创建新书</button>
  <div class="status" id="stNew"></div>
  <div class="log" id="logNew"></div>
</div>

<!-- 设置 -->
<div class="card">
  <h2>⚙️ 设置</h2>
  <input id="keyInput" placeholder="DeepSeek API Key（sk-...）" type="password">
  <button class="sec" onclick="saveKey()">💾 保存 Key</button>
  <div class="status" id="stKey"></div>
  <hr style="border:0;border-top:1px solid var(--line);margin:12px 0;">
  <div class="sub" style="text-align:left;font-size:13px;color:var(--sub);">⚡ 引擎参数（只对当前书生效，改完即生效）</div>
  <label>正文写作思考（write.thinking）</label>
  <select id="egThinking">
    <option value="enabled">开 · 先构思再写（质量好，慢）</option>
    <option value="disabled">关 · 直接写（快省，可能降质）</option>
  </select>
  <label>思考强度（write.reasoning_effort）</label>
  <select id="egEffort">
    <option value="low">low · 快省，易写飞</option>
    <option value="medium">medium · 推荐（稳）</option>
    <option value="high">high · 慢贵，易卡</option>
  </select>
  <label>正文 token 上限（write.max_tokens，含思考）</label>
  <input id="egMax" type="number" min="1000" step="500" placeholder="12000">
  <button class="sec" onclick="saveEngine()">💾 保存引擎参数</button>
  <div class="status" id="stEg"></div>
</div>

<footer>写作台 · 同 Wi-Fi 的家人朋友也能连 · 关掉本页 = 停止服务</footer>

<script>
let cur = "__default__";
let pollId = null;

const $ = id => document.getElementById(id);
const esc = s => (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const strip = s => (s||"").replace(/\x1b\[[0-9;]*m/g,"");
const bookName = () => cur === "__default__" ? "《介质》" : cur;

function setSt(id, msg, cls){ $(id).textContent = msg; $(id).className = "status " + (cls||""); }

async function api(path, opt){
  const r = await fetch(path, opt);
  return r.json().catch(()=>({error:"响应异常"}));
}

async function refreshBooks(){
  const d = await api("/api/books");
  const el = $("booklist"); el.innerHTML = "";
  for (const b of d.books){
    const div = document.createElement("div");
    div.className = "bookbtn" + (b.id===cur ? " sel" : "");
    div.innerHTML = `<div><strong>${esc(b.name)}</strong><br><small>已写 ${b.chapters} 章 · 下一章 第${b.next_no}章</small></div>`;
    div.onclick = () => { cur = b.id; refreshBooks(); loadEngine(); };
    el.appendChild(div);
  }
  $("addr").textContent = "局域网地址：http://" + (d.ip||"") + ":" + location.port + "  ·  家人手机同 Wi-Fi 可连";
}

function poll(tid, stEl, logEl, doneMsg){
  clearInterval(pollId);
  pollId = setInterval(async () => {
    const d = await api("/api/task?id=" + tid);
    const t = d.task; if(!t){ clearInterval(pollId); return; }
    const lines = t.log.join("\n");
    $(logEl).style.display = lines ? "block" : "none";
    $(logEl).textContent = strip(lines);
    $(logEl).scrollTop = $(logEl).scrollHeight;
    if (t.status === "running"){
      setSt(stEl, "⏳ 进行中…（写一章约 1~2 分钟，请勿关闭）", "run");
    } else if (t.status === "done"){
      clearInterval(pollId);
      setSt(stEl, "✅ " + (doneMsg||"完成！"), "ok");
      refreshBooks();
    } else {
      clearInterval(pollId);
      setSt(stEl, "❌ 出错了，看下面日志", "err");
    }
  }, 1200);
}

function doWrite(){
  const start = parseInt($("wtStart").value) || undefined;
  const count = parseInt($("wtCount").value) || 1;
  const from = start ? `从第${start}章起` : "下一章";
  if(!confirm(`写《${bookName()}》${from}，共 ${count} 章？\n（约1~2分钟/章，自动落盘）`)) return;
  setSt("stWrite", "⏳ 启动中…", "run");
  api("/api/write", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({book: cur, count, start, key: $("keyInput").value || undefined})})
    .then(d => { if(d.task) poll(d.task.id, "stWrite", "logWrite", `《${bookName()}》已写好！去「已写章节」看吧`); });
}

function doPreview(){
  setSt("stWrite", "⏳ 生成 prompt…", "run");
  api("/api/preview", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({book: cur})})
    .then(d => { if(d.task) poll(d.task.id, "stWrite", "logWrite", "预览完成（下面是完整 prompt）"); });
}

async function showChaps(){
  const d = await api("/api/chapters?book=" + encodeURIComponent(cur));
  const c = d.chapters || [];
  if(!c.length){ setSt("stWrite", "还没有章节，点「✍️ 写下一章」开始吧", "run"); return; }
  let s = "【已写章节】\n";
  for (const ch of c) s += `第${ch.no}章 ${ch.title||""}（${ch.chars}字）\n`;
  setSt("stWrite", "点击下方日志里的章节号可读正文（稍后加）", "run");
  alert(s);
}

function doNewBook(){
  const name = $("nbName").value.trim();
  if(!name){ setSt("stNew", "⚠️ 请填书名", "err"); return; }
  if(!confirm(`创建新书《${name}》？`)) return;
  $("btnNew") && 0;
  setSt("stNew", "⏳ 创建中…（AI 生成约1分钟）", "run");
  api("/api/newbook", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({name, intro:$("nbIntro").value.trim(),
        protagonist:$("nbProt").value.trim(), total:parseInt($("nbTotal").value)||30,
        stages:$("nbStages").value.trim() || undefined,
        ai:$("nbAi").checked, key:$("keyInput").value || undefined})})
    .then(d => {
      if(d.error){ setSt("stNew", "⚠️ " + d.error, "err"); return; }
      poll(d.task.id, "stNew", "logNew", `《${name}》建好了！切到新书开写吧`);
    });
}

async function saveKey(){
  const key = $("keyInput").value.trim();
  if(!key){ setSt("stKey", "⚠️ 先粘贴 Key", "err"); return; }
  const d = await api("/api/savekey", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key})});
  if(d.ok){ setSt("stKey", "✅ Key 已保存（写章/建书不再要）", "ok"); $("keyInput").value=""; }
  else setSt("stKey", "⚠️ " + (d.error||"保存失败"), "err");
}

// ===== 引擎参数（每本书可调）=====
let egDefaults = {};
async function loadEngine(){
  const d = await api("/api/engine?book=" + encodeURIComponent(cur));
  egDefaults = d.defaults || {};
  const e = d.engine || {};
  const w = e.write || {};
  $("egThinking").value = w.thinking || egDefaults.write.thinking || "enabled";
  $("egEffort").value = w.reasoning_effort || egDefaults.write.reasoning_effort || "medium";
  $("egMax").value = w.max_tokens || egDefaults.write.max_tokens || 12000;
}
async function saveEngine(){
  const data = {
    write: {
      thinking: $("egThinking").value,
      reasoning_effort: $("egEffort").value,
      max_tokens: parseInt($("egMax").value) || 12000
    }
  };
  setSt("stEg", "⏳ 保存中…", "run");
  const d = await api("/api/engine", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({book: cur, engine: data})});
  if(d.ok){ setSt("stEg", "✅ 已保存，写下一章即生效", "ok"); }
  else setSt("stEg", "⚠️ " + (d.error||"保存失败"), "err");
}

refreshBooks();
loadEngine();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
