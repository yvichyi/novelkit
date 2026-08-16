#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置与知识库：persona/红线词/术语/模板/正则/常量（从 ai_bridge_api.py 拆分，原文未改）。"""
import os
import re
from datetime import datetime


C_QWEN = "\033[95m"

C_KIMI = "\033[96m"

C_SYS  = "\033[0m"

C_WARN = "\033[93m"

MAX_TOKENS = 50000

REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "medium")

def log(msg, color=C_SYS):
    print(f"{color}{msg}{C_SYS}", flush=True)

def now_str():
    return datetime.now().strftime("%H:%M:%S")

# ================= 书专属配置加载器 =================
# 本模块从 novel_config/ 目录加载一本书的专属内容（世界观/人格/大纲/红线/锚点）。
# 替换 NOVEL_CONFIG_DIR 指向的目录 = 换一本书，引擎代码零改动。
# 参考模板：templates/novel_config/；《介质》示例：novel_config/。
import json as _json
from pathlib import Path as _Path

NOVEL_CONFIG_DIR = os.environ.get("NOVEL_CONFIG_DIR", "novel_config")
_nc = _Path(NOVEL_CONFIG_DIR)
if not _nc.is_absolute():
    _nc = (_Path(__file__).resolve().parent.parent / _nc).resolve()

def _read_text(name):
    p = _nc / name
    return p.read_text(encoding="utf-8") if p.exists() else ""

def _read_json(name):
    p = _nc / name
    return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

# ---- 文本类 ----
QWEN_PERSONA = _read_text("persona_qwen.md")
KIMI_PERSONA = _read_text("persona_kimi.md")
TOPIC_DEFAULT = _read_text("topic.md")
INITIAL_CHECKLIST = _read_text("checklist.md")
STORY_OUTLINE = _read_text("outline.md")
FORESHADOW_WINDOW = _read_text("foreshadow.md")

# ---- 红线词/风格词库 ----
_redlines = _read_json("redlines.json")
BANNED_WORDS = _redlines.get("BANNED_WORDS", [])
BANNED_EXEMPT = _redlines.get("BANNED_EXEMPT", [])
TERM_LAWS = _redlines.get("TERM_LAWS", [])
STAGE_SETTING_WORDS = _redlines.get("STAGE_SETTING_WORDS", {})
CODE_VOICE_WORDS = _redlines.get("CODE_VOICE_WORDS", [])
WASTE_ATMOS_WORDS = _redlines.get("WASTE_ATMOS_WORDS", [])
STAGE_ATMOS_WORDS = _redlines.get("STAGE_ATMOS_WORDS", {})
HOOK_SIGNAL_WORDS = _redlines.get("HOOK_SIGNAL_WORDS", [])

# ---- 锚点体系 ----
_anchors = _read_json("anchors.json")
ANCHOR_KEYWORDS = _anchors.get("ANCHOR_KEYWORDS", {})

def _intify_keys(d):
    """JSON 的 key 只能是 str；把形如 "1"/"91" 的数字 key 还原为 int（代码用 int 访问锚号）。
    仅当所有 key 都是数字字符串时才转换，兼容未来用字符串 key 的书。"""
    if not isinstance(d, dict):
        return d
    if all(isinstance(k, str) and k.isdigit() for k in d):
        return {int(k): _intify_keys(v) for k, v in d.items()}
    return {k: _intify_keys(v) for k, v in d.items()}

ANCHOR_KEYWORDS = _intify_keys(ANCHOR_KEYWORDS)
STAGE_ANCHORS = [tuple(r) for r in _anchors.get("STAGE_ANCHORS", [])]  # JSON 存 list，还原 tuple 与原格式一致
STAGE_DIMS = _anchors.get("STAGE_DIMS", {})
LATE_STAGE_BLACKLIST = _anchors.get("LATE_STAGE_BLACKLIST", {})

# ---- 人物时间线 / 评分维度 ----
_timeline = _read_json("timeline.json")
CHARACTER_TIMELINE = [tuple(r) for r in _timeline.get("CHARACTER_TIMELINE", [])]  # 还原 tuple
_scores = _read_json("scores.json")
SCORE_DIMS = _scores.get("SCORE_DIMS", [])

# 禁词正则（由 BANNED_WORDS 组装；空词表 = 永不匹配，防止空正则误报全文本踩线）
_banned_pat = "|".join(re.escape(w) for w in BANNED_WORDS)
BANNED_PATTERN = re.compile(_banned_pat) if _banned_pat else re.compile(r"(?!x)x")

CHATROOM_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 互聊室 · 科幻作家 vs 毒舌编辑</title>
<script>
  // 每 5 秒强制重新加载页面（比 meta refresh 可靠，兼容安卓浏览器）
  setTimeout(function(){ location.reload(); }, 5000);
</script>
<style>
  :root { --qwen:#7c3aed; --kimi:#0f766e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;
         background:#0f172a; color:#e2e8f0; max-width:760px; margin:0 auto; padding:12px; }
  header { text-align:center; padding:16px 0 20px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { font-size:12px; color:#94a3b8; }
  .bubble { display:flex; gap:10px; margin:14px 0; align-items:flex-start; }
  .bubble.qwen { flex-direction:row; }
  .bubble.kimi { flex-direction:row-reverse; }
  .avatar { width:38px; height:38px; border-radius:50%; display:flex; align-items:center;
            justify-content:center; font-size:20px; flex-shrink:0; }
  .qwen .avatar { background:var(--qwen); }
  .kimi .avatar { background:var(--kimi); }
  .box { max-width:78%; }
  .name { font-size:12px; color:#94a3b8; margin-bottom:4px; display:flex; gap:8px; }
  .qwen .name { justify-content:flex-start; }
  .kimi .name { justify-content:flex-end; }
  .text { background:#1e293b; border-radius:12px; padding:10px 14px; font-size:15px;
          line-height:1.7; white-space:pre-wrap; word-break:break-word; }
  .qwen .text { border-left:3px solid var(--qwen); }
  .kimi .text { border-right:3px solid var(--kimi); }
  .sys { text-align:center; color:#94a3b8; font-size:12px; margin:18px 0; }
  footer { text-align:center; color:#64748b; font-size:11px; padding:20px 0 40px; }
</style>
</head>
<body>
<header>
  <h1>🤖 AI 互聊室</h1>
  <div class="sub">千问「星尘」写科幻 · Kimi「老K」全方位施压 · 每 5 秒自动刷新</div>
</header>
__MESSAGES__
<footer>每 5 秒自动刷新 · 若长时间不更新，点右上角刷新按钮或关闭重开 · 当前时间 <span id="t"></span></footer>
<script>document.getElementById('t').textContent = new Date().toLocaleTimeString();</script>
</body>
</html>"""

DEEPSEEK_PRICE = {
    # 2026-08-17 起峰谷定价（元/百万 tokens）：高峰=北京时间 9-12点、14-18点；其余空闲=半价
    "deepseek-v4-flash": {
        "peak":   {"cache_hit": 0.10, "cache_miss": 3.0, "output": 9.0},
        "offpeak": {"cache_hit": 0.05, "cache_miss": 1.5, "output": 4.5},
    },
    "deepseek-v4-pro": {
        "peak":   {"cache_hit": 0.30, "cache_miss": 9.0, "output": 27.0},
        "offpeak": {"cache_hit": 0.15, "cache_miss": 4.5, "output": 13.5},
    },
}


def _is_peak_hour(when=None):
    """北京时间高峰：9:00-12:00、14:00-18:00（其余空闲）。固定用 UTC+8，不受服务器时区影响。"""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    dt = when or _dt.now(_tz(_td(hours=8)))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(_td(hours=8)))
    h = dt.hour
    return (9 <= h < 12) or (14 <= h < 18)


def price_for(model, when=None):
    """按当前时段返回模型单价 dict {cache_hit, cache_miss, output}；旧结构（单档）直接返回；无此模型返回 None"""
    p = DEEPSEEK_PRICE.get(model)
    if not p:
        return None
    if "peak" in p and "offpeak" in p:
        return p["peak"] if _is_peak_hour(when) else p["offpeak"]
    return p

STATE_FILE = "ai_bridge_state.json"

_META_STRONG = re.compile(r"(评审|意见|批评|建议|修改如下|创作说明|写作思路|针对|回应|根据|明白了|收到|待续|未完待续|下章)")

_CHAPTER_TITLE_RE = re.compile(r"^\s*(?:#{1,3}\s*)?第([0-9一二两三四五六七八九十百千]+)章\s*[：:、]?\s*([^\n]*?)\s*$")

NOVEL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script>
  // 每 5 秒自动刷新，跟聊天室同步
  setTimeout(function(){ location.reload(); }, 5000);
</script>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:Georgia,"Songti SC","Noto Serif SC",serif;
         background:#f5f0e6; color:#2b2620; }
  .page { max-width:720px; margin:0 auto; padding:32px 20px 80px; }
  h1 { text-align:center; font-size:28px; letter-spacing:6px;
       margin:40px 0 8px; color:#3a2f22; }
  .meta { text-align:center; color:#8a7a63; font-size:13px; margin-bottom:40px;
          letter-spacing:2px; }
  h2 { font-size:20px; margin:48px 0 20px; padding-bottom:10px;
       border-bottom:1px solid #d8cbb8; color:#4a3b28; text-align:center; }
  p { font-size:16.5px; line-height:2.05; margin:0 0 14px; text-align:justify;
      text-indent:2em; }
  .end { text-align:center; color:#a08d6f; margin-top:40px; font-size:13px; }
</style>
</head>
<body>
<div class="page">
  <h1>《介质》</h1>
  <div class="meta">AI 科幻连载 · 星尘 著 · 老K 审校 · 每 5 秒自动更新</div>
  __BODY__
  <div class="end">—— 连载中，更新中 ——</div>
</div>
</body>
</html>"""
