#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""故事文件：章节提取/替换/禁词扫描/质量门禁/渲染（从 ai_bridge_api.py 拆分，原文未改）。"""
import difflib
import html
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from .config import ANCHOR_KEYWORDS, BANNED_EXEMPT, BANNED_PATTERN, CHATROOM_TEMPLATE, CODE_VOICE_WORDS, C_WARN, HOOK_SIGNAL_WORDS, NOVEL_TEMPLATE, STAGE_ANCHORS, STAGE_ATMOS_WORDS, STAGE_SETTING_WORDS, TERM_LAWS, WASTE_ATMOS_WORDS, _CHAPTER_TITLE_RE, _META_STRONG, log, now_str


def similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def is_cold(text, prev_text):
    """冷场检测：回复太短 或 与上一轮几乎重复"""
    t = re.sub(r"\s", "", text or "")
    if len(t) < 20:
        return True
    if prev_text and similarity(text, prev_text) > 0.85:
        return True
    return False

def default_outdir():
    """输出目录：优先共享存储（手机文件管理器可见），退回脚本目录"""
    candidates = [
        Path("/sdcard/Download/ai-bridge"),
        Path.home() / "storage" / "downloads" / "ai-bridge",
        Path(__file__).resolve().parent / "out",
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except Exception:
            continue
    return Path.cwd() / "out"

def render_chatroom(msgs):
    parts = []
    for m in msgs:
        ts = m.get("ts", "")
        if m["who"] == "sys":
            parts.append(f'<div class="sys">—— {html.escape(m["text"])} ——</div>')
        else:
            cls = "qwen" if m["who"] == "qwen" else "kimi"
            avatar = "🚀" if m["who"] == "qwen" else "🔍"
            name = m.get("name", "AI")
            parts.append(
                f'<div class="bubble {cls}">'
                f'<div class="avatar">{avatar}</div>'
                f'<div class="box">'
                f'<div class="name"><span>{html.escape(name)}</span><span>{ts}</span></div>'
                f'<div class="text">{html.escape(m["text"])}</div>'
                f'</div></div>'
            )
    return CHATROOM_TEMPLATE.replace("__MESSAGES__", "\n".join(parts))

def extract_story_blocks(log_path, max_blocks=60):
    """从 chat.log 提取最近的内容块（跳过'话题：'系统消息），用于旧故事续写"""
    try:
        text = Path(log_path).read_text(encoding="utf-8")
    except Exception:
        return []
    blocks = re.split(r"\n--- .+? ---\n", text)
    blocks = [b.strip() for b in blocks if b.strip() and not b.strip().startswith("话题：")]
    return blocks[-max_blocks:]

def extract_chapter(story_text, n):
    """从故事正文提取第 n 章原文（返回该章文本；找不到返回空）"""
    if not story_text or n < 1:
        return ""
    marker = f"## 第{n}章"
    idx = story_text.find(marker)
    if idx == -1:
        return ""
    start = idx + len(marker)
    # 找下一章起点
    nxt = re.search(r"## 第\d+章", story_text[start:])
    end = start + nxt.start() if nxt else len(story_text)
    return story_text[start:end].strip()

def replace_chapter(story_text, n, new_text):
    """把第 n 章替换为新文本（返回新全文；找不到返回原文本）"""
    if not story_text or n < 1:
        return story_text
    marker = f"## 第{n}章"
    idx = story_text.find(marker)
    if idx == -1:
        return story_text
    start = idx + len(marker)
    nxt = re.search(r"## 第\d+章", story_text[start:])
    end = start + nxt.start() if nxt else len(story_text)
    return story_text[:start] + "\n\n" + new_text.strip() + "\n" + story_text[end:]

def parse_direct_edit(review):
    """从老K评审提取【直接修改】区块：返回 [(章节号, 原文片段, 新文本), ...]，无则 []。
    兼容：多条修改（换行/分号/另一章号分隔）、原文片段：前缀（冒号必选）、中文引号包裹、
    →改为→ / →改为 / 应改为 / 改成 / 改为 分隔符。
    老K摘录允许用……省略中间内容，替换时整段替换（见 apply_direct_edit 的容错匹配）。"""
    if not review:
        return []
    edits = []
    # 提取【直接修改】区块（到下一个【…】区块为止；条目之间可能用空行分隔，不能以双换行终止）
    m = re.search(r"(?:【直接修改】|#{1,4}\s*直接修改)\s*(.*?)(?=(?:【[^】]*】|#{1,4}\s*[^#\n]*:?|\Z))", review, re.S)
    if not m:
        return []
    block = m.group(1).strip()
    if not block or re.fullmatch(r"[无没暂不].{0,5}", block):
        return []
    # 按条目切分：每个新条目以「第X章」或「原文片段/原文：」开头
    # 注意1：lookahead 排除「原标第X章」（「第36章（原标第35章）」里第35章前有「原标」，不能切）
    # 注意2：老K同章多条用「；原文：」分隔（如 原文：A → 改为：B ；原文：C → 改为：D），必须切
    # 注意3：老K多条修改也可能用换行分隔且无「原文：」前缀（如 「A」→改为→「A'」换行「B」→改为→「B'」），
    #        此时没有「第X章/原文：」锚点可切 → 先按「改为」后的引号闭合边界硬切（见下方二次切分）
    items = re.split(r"(?=(?<!原标)第\s*\d+\s*章|(?:原文片段|原文)\s*[:：])", block)
    # 二次切分：把「改完的新文本」后紧跟换行+引号开头的「下一条原文」切开
    # 规则：遇到「」『』或引号包裹的新文本结尾，后面跟换行再跟引号/「开始 → 视为新条目
    _items2 = []
    for it in items:
        # 在 it 内找「X」→改为→「Y」重复模式：每次把 Y 结尾的引号闭合后的换行+新引号 切开
        parts = re.split(r"(?<=[」』”])[\r\n]+(?=[「『“])", it)
        _items2.extend(parts)
    items = _items2
    cur_n, out_seen = None, set()
    for it in items:
        it = it.strip().strip("；;。 ")
        if not it:
            continue
        # 章节号：优先取「原标第Y章」里的 Y（正文真实编号，老K可能写错轮次号）；
        # 没有「原标」才取「第X章」的 X。本条目有则更新，没有则沿用上一个条目
        nm = re.search(r"原标\s*第\s*(\d+)\s*章", it)
        if not nm:
            nm = re.search(r"第\s*(\d+)\s*章", it)
        if nm:
            cur_n = int(nm.group(1))
        n = cur_n
        if n is None:
            continue
        # 剥「第X章」声明、剥「（原标第Y章）」括号、剥「原文片段：」前缀（冒号必选）
        it = re.sub(r"第\s*\d+\s*章\s*[+＋:：]?\s*", "", it, count=1)
        it = re.sub(r"[（(]\s*原标\s*第\s*\d+\s*章\s*[）)]", "", it, count=1)
        it = re.sub(r"(?:原文片段|原文|摘录)\s*[:：]\s*", "", it, count=1)
        # 分隔符拆分
        m2 = re.search(r"(?:→\s*改为\s*→?|→\s*|应改为\s*|改成\s*|改为\s*)(.*)$", it, re.S)
        if not m2:
            continue
        old_text = it[:m2.start()].strip()
        new_text = m2.group(1).strip()
        old_text = old_text.strip(" \n\t，,。；;:：\"'“”‘’「」『』`+＋")
        new_text = new_text.strip(" \n\t，,。；;:：\"'“”‘’「」『』`")
        if len(old_text) >= 4 and new_text and old_text not in out_seen:
            out_seen.add(old_text)
            edits.append((n, old_text, new_text))
    return edits

def _fuzzy_regex(old_text):
    """把原文片段转成容错正则：空白与常见标点可缺失/差异，汉字字母数字必须严格匹配。
    老K摘录用「……」省略中间内容时，省略号转成「跳过任意内容」——保证摘录仍能定位替换。
    用于老K摘录与正文存在标点/空白/省略差异时仍能定位替换。"""
    if not old_text:
        return None
    PUNCT = r"[\s，。：；、“”‘’「」!?！？,.;:()（）\-—…·`]"
    tokens = re.split("(" + PUNCT + "+)", old_text)
    parts = []
    for tk in tokens:
        if not tk:
            continue
        if re.fullmatch(PUNCT + "+", tk):
            # 连续省略号（…… 或 ...）→ 允许跳过任意内容（老K摘录省略中间段）
            if (tk.count("…") >= 2) or (tk.count(".") >= 2 and set(tk) <= {".", " "}):
                parts.append(r"[\s\S]{0,400}?")
            else:
                parts.append(PUNCT + "{0,3}")
        else:
            parts.append(re.escape(tk))
    return re.compile("".join(parts))

def fix_person_to_third(text):
    """第三人称兜底消毒：只动叙述区，保护引号内台词与代码块。
    叙述区替换：我们的→他们的、我们→他们、我的→他的、独立「我」→「他」
    （排除 自我/本我/忘我/无我/你我 等固定词，避免误伤）。
    口诀同清单：叙述永无我，台词我照旧。"""
    if not text:
        return text
    # 用正则切分，保护引号内（“”『』「」）与反引号代码块：奇数段受保护，偶数段是叙述区
    parts = re.split(r'(\"[^\"\n]*\"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|`[^`]*`)', text, flags=re.S)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 1:  # 台词/代码块：原样保留
            out.append(p)
            continue
        p = re.sub(r'我们的', '他们的', p)
        p = re.sub(r'我们', '他们', p)
        p = re.sub(r'我的', '他的', p)
        p = re.sub(r'(?<![自本忘无你我])我', '他', p)
        out.append(p)
    return ''.join(out)

def apply_direct_edit(story_text, chapter_n, old_text, new_text):
    """在指定章节内做定点替换：返回 (新全文, 是否成功)。
    先精确匹配；失败后容错匹配（空白/标点差异）——保证老K认可的修改真正嵌入正文。
    章节号定位失败（老K编号错位/正文缺章）时，降级为全文定位：原文片段通常全篇唯一，
    只要摘录真实存在于正文，就按 章节内→全文、精确→模糊 的顺序兜住。"""
    if not story_text or chapter_n < 1:
        return story_text, False

    # ---- 定位替换区间：优先指定章节 ----
    marker = f"## 第{chapter_n}章"
    idx = story_text.find(marker)
    start, end, chapter = -1, -1, ""
    if idx != -1:
        start = idx + len(marker)
        nxt = re.search(r"## 第\d+章", story_text[start:])
        end = start + nxt.start() if nxt else len(story_text)
        chapter = story_text[start:end]
    # 章节不存在 → 全文作为搜索域（降级）
    if start == -1:
        chapter, start, end = story_text, 0, len(story_text)

    def _try_replace(scope, s0, e0):
        """在 scope（字符串）内定位 old_text（精确→模糊），成功返回 (新全文, True)"""
        if old_text in scope:
            new_scope = scope.replace(old_text, new_text, 1)
            return story_text[:s0] + new_scope + story_text[e0:], True
        pat = _fuzzy_regex(old_text)
        if pat:
            m = pat.search(scope)
            if m:
                new_scope = scope[:m.start()] + new_text + scope[m.end():]
                return story_text[:s0] + new_scope + story_text[e0:], True
        return None, False

    # 1) 指定章节内（精确→模糊）
    r, ok = _try_replace(chapter, start, end)
    if ok:
        return r, True
    # 2) 章节内没找到且章节确实存在 → 全文兜底（精确→模糊）
    if idx != -1:
        r2, ok2 = _try_replace(story_text, 0, len(story_text))
        if ok2:
            return r2, True
    return story_text, False

def extract_rewrite_request(review):
    """从老K评审提取【要求重写】区块：返回 (章节号, 重写要求)，无则 (None, '')。
    理由必须具体（重写不能没有理由）：解析到下一个区块标题【…】或双换行或结尾为止"""
    m = re.search(r"(?:【要求重写】|#{1,4}\s*要求重写)\s*第\s*(\d+)\s*章?\s*\+?\s*(.*?)(?=\n\n|【[^】]*】|#{1,4}\s*[^#\n]*:?|\Z)", review or "", re.S)
    if not m:
        return None, ""
    n = int(m.group(1))
    req = m.group(2).strip().strip("+：:，,。 ").strip()
    return n, req

def clean_story_text(text):
    """把星尘的输出清理成纯小说正文：剥掉应答词、创作说明、与老K对话的痕迹"""
    t = (text or "").strip()
    # 剥掉括号内的创作说明
    t = re.sub(r"（[^）]*?(创作说明|写作思路|本章完|待续|下章|存稿|草稿)[^）]*?）", "", t)
    # 按段处理（空行分段）
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    out = []
    for i, p in enumerate(paras):
        # 开头若干段若是短元话语（强关键词 + 不长）→ 整段丢弃
        if not out and i < 3 and len(p) < 100 and _META_STRONG.search(p):
            continue
        # 剥段内行首的短应答词（如"好的，""收到，"）
        p = re.sub(r"^(好的|好|行|收到|明白|了解|OK|ok|嗯)[，,。！!：:\s]*", "", p)
        # 剥"老K的评审：…"式前缀（本作无老K这个角色，出现即元话语；但"老K巷"等地名不误伤）
        p = re.sub(r"^老[Kk](的)?(评审|意见|批评|建议|说|指出|认为|点评)?[，,。！!：:]\s*", "", p)
        p = p.strip()
        if p:
            out.append(p)
    t = "\n\n".join(out)
    # 去掉结尾独立的"本章完/待续"行
    t = re.sub(r"(^|\n)\s*[（(]?(本章完|待续|未完待续|下章预告)[）)]?[^\n]*", r"\1", t)
    return t.strip()

def _cn_num(s):
    """中文数字/阿拉伯数字 → 阿拉伯数字（第一章/第1章→1；未识别返回None）"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    cn = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    units = {"十":10,"百":100,"千":1000}
    total, cur = 0, 0
    for ch in s:
        if ch in cn:
            cur = cn[ch]
        elif ch in units:
            u = units[ch]
            total += (cur or 1) * u
            cur = 0
        else:
            return None
    return total + cur

def extract_chapter_title(text):
    """从 AI 输出提取章节标题（第X章[ 副标题]，只认第一行）；返回 (章号, 标题, 正文)。无则 (None, None, 原文)"""
    t = (text or "").strip()
    first_line = t.split("\n", 1)[0].strip()
    m = _CHAPTER_TITLE_RE.match(first_line)
    if not m:
        return None, None, t
    n = _cn_num(m.group(1))
    if n is None:
        return None, None, t
    subtitle = (m.group(2) or "").strip()
    title = f"第{n}章" + (f" {subtitle}" if subtitle else "")
    body = t[len(first_line):].strip()
    return n, title, body

def maybe_capture_stray_chapter(outdir, text):
    """兜底：若讨论/答辩输出里意外夹带完整正文（第X章+长文），自动补进正文文件"""
    try:
        n, title, body = extract_chapter_title(text)
        if n is None or not title or len(body) < 300:
            return False
        sf = outdir / "故事正文.md"
        existing = sf.read_text(encoding="utf-8") if sf.exists() else ""
        # 若该章已存在，跳过（不重复）
        if f"## 第{n}章" in existing:
            return False
        with open(sf, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {title}\n\n{body}")
        render_novel(outdir)
        log(f"♻️ 检测到讨论中夹带的正文（{title}），已自动补入正文文件", C_WARN)
        return True
    except Exception:
        return False

def _next_chapter_no(story_text):
    """计算下一章期望号：取现有最大章号+1（防跳号/重号；AI 报错号时用程序号兜底）"""
    nums = [_cn_num(m) for m in re.findall(r"## 第([0-9一二两三四五六七八九十百千]+)章", story_text or "")]
    nums = [x for x in nums if x is not None]
    return (max(nums) + 1) if nums else 1

def scan_banned(text):
    """返回命中的黑名单词列表（去重，豁免语境跳过）"""
    hits = set()
    for w in BANNED_PATTERN.findall(text):
        # 豁免：该命中位置所在行含豁免片段 → 视为合法语境
        idx = text.find(w)
        line = text[max(0, idx-20):idx+20]
        if any(e in line for e in BANNED_EXEMPT):
            continue
        hits.add(w)
    return sorted(hits)

def count_terms(text):
    """统计正文中现实定律/科学术语出现次数（去重词计数）"""
    n = 0
    found = set()
    for t in TERM_LAWS:
        c = text.count(t)
        if c > 0:
            n += c
            found.add(t)
    return n, found

def _stage_by_anchor(anchor_no):
    """按锚号反查阶段名（锚1~8=混沌期，9~18=清理期，19~29=分裂期，30~36=共存期，37~40=终局）"""
    for name, a_lo, a_hi, ch_lo, ch_hi in STAGE_ANCHORS:
        if a_lo <= anchor_no <= a_hi:
            return name
    return "混沌期"

def _quality_gate(outdir, text, story_file, anchor_no=None):
    """B3~B9 质量门禁：落盘前硬校验，返回 (ok, issues)
    B3 文本污染扫描：孤引号/编号残留/常见错字
    B4 时间线核对：异常期第X天单调递增
    B5 标题纪律：章节必须有「第X章」+副标题（缺副标题=标记）
    B6 字数纪律：1500~3000 字区间外=标记（不拦截防误伤）
    B7 反雷同黑名单=熔断（污染词禁现）
    B8 现实定律术语密度（<2 警告不拦截）
    B9 设定/大纲契合度：锚推进+设定运用+时间锚点（按阶段：混沌期=异常期第X天/失控期第X周，清理期=清理期第X年/月，分裂期/共存期=第X年，终局=终局第X月）+柯德声音+末世氛围，缺失≥2项=熔断；结尾钩子=警告
    anchor_no：当前锚号（None=跳过锚推进检查，如非锚驱动流程）
    """
    issues = []

    # B3 文本污染
    if '」' in text and text.count('」') % 2 == 1:
        issues.append("B3 文本污染：孤立的右引号「」不配对")
    if re.search(r'\d+[\.。]\s*[。．]', text):
        issues.append("B3 文本污染：编号残留（如 2.。）")
    for bad, good in [("反映", "反应"), ("在在", "在"), ("的的", "的"), ("，，", "，")]:
        if bad in text:
            # 「反映」在"反映现实"等语境是合法词，只查明显误用（无宾语直接接句号/逗号）
            issues.append(f"B3 疑似错字：「{bad}」（可能应为「{good}」）")
    # 评审注水检测：正文出现（原句…）（与：…）（删掉…）（补足…）这类创作注释
    if re.search(r'[）」]（(?:原句|与：|前一段|删掉|补足|建议|这里|原文)', text):
        issues.append("B3 文本污染：评审注水混入正文（（原句/与：/删掉…）这类创作注释）")

    # B4 时间线核对（只查每章章首的时间标记「异常期第X天」，避免把回忆/插叙当回退）
    if story_file.exists():
        whole = story_file.read_text(encoding="utf-8") + "\n\n" + text
        # 每章开头（## 第X章 标题 后第一处「异常期第X天」）
        days = []
        for ch_m in re.finditer(r'^##\s+第\d+章[^\n]*\n(.*?)(?=^##\s+第\d+章|\Z)', whole, re.M | re.S):
            head = ch_m.group(1)
            dm = re.search(r'异常期第(\d+)天', head[:200])  # 只查章首200字
            if dm:
                days.append(int(dm.group(1)))
        if days:
            prev = days[0]
            for d in days[1:]:
                if d < prev:
                    issues.append(f"B4 时间线回退：第{prev}天 → 第{d}天（时间不能倒退）")
                    break
                prev = d

    # B5 标题纪律
    m = re.search(r'^##\s+第(\d+)章[^\n]*$', text, re.M)
    if m:
        title = m.group(0).strip()
        sub = title.split("章", 1)[1].strip()
        if not sub:
            issues.append("B5 标题纪律：章节缺少副标题（如「第X章 名字」）")
    else:
        issues.append("B5 标题纪律：正文未以「## 第X章」开头")

    # B6 字数纪律
    words = len(re.sub(r'\s', '', text))
    if words < 1500:
        issues.append(f"B6 字数偏少：{words}字（目标1500~3000）")
    elif words > 3200:
        issues.append(f"B6 字数偏多：{words}字（目标1500~3000）")

    # B7 反雷同黑名单熔断（上一轮污染词，命中=必须重写）
    hits = scan_banned(text)
    if hits:
        issues.append(f"B7 反雷同黑名单命中：{'、'.join(hits[:8])}（污染词，禁止出现，必须重写）")

    # B8 现实定律术语密度（硬核特色，每章至少2处定律/科学名词；低于=警告不拦截防硬塞）
    tn, tset = count_terms(text)
    if tn < 2:
        issues.append(f"B8 术语密度不足：仅{tn}处现实定律/科学名词（目标≥2处，如纳维-斯托克斯/麦克斯韦/伯努利/卡诺/潜热/雷诺数）")

    # B9 设定/大纲契合度（v2.8 分级：时间锚点=S级熔断；锚推进/设定运用/声音/氛围/钩子=软指标只提示/警告）
    # 依据：ChatGPT 方案评审共识——"关键词命中"不单独判定文学质量，熔断只用于 S 级错误（时间线/正史）。
    # 写跑题由主编/人工判断，不以"命中几个关键词"代替真审读（deepseek-v4-pro 第247章关键词误杀教训）。
    if anchor_no is not None:
        # ③ 时间锚点：S级硬规则（时间线=正史，缺失=熔断，唯一保留的 B9 熔断项）
        if not re.search(r'(异常期|失控期|崩塌期|剧变期|清理期|分裂期|共存期|终局)第\s*[0-9一二两三四五六七八九十百千]+\s*(天|周|月|年)|第\s*[0-9一二两三四五六七八九十百千]+\s*天\b', text[:300]):
            issues.append("B9 契合度熔断：时间锚点缺失（章首300字内未交代当前时间，如 分裂期第2年/异常期第3天）")
        # ① 锚推进：A级软指标（关键词命中只提示，不熔断——是否真推进由主编/人工判定）
        _kws = ANCHOR_KEYWORDS.get(anchor_no, [])
        if _kws:
            _hits = [w for w in _kws if w in text]
            if len(_hits) < 2:
                issues.append(f"B9 锚推进提示（命中{len(_hits)}个锚{anchor_no}关键词，建议≥2：{'/'.join(_kws[:6])}…；仅提示不拦截，请人工确认本章是否真正推进节点）")
        # ② 设定运用：A级软指标（降级为提示，防为凑关键词硬塞术语）
        _stage = _stage_by_anchor(anchor_no)
        _sw = STAGE_SETTING_WORDS.get(_stage, [])
        if _sw and not any(w in text for w in _sw):
            issues.append(f"B9 设定运用提示（{_stage}核心设定词零命中：{'/'.join(_sw[:5])}…；仅提示不拦截）")
        # ④ 柯德声音：毒舌/吐槽/直率信号≥1（软查：文风问题不参与硬门禁——关键词表抓不到≠没声音，deepseek-r1误杀教训）
        if not any(w in text for w in CODE_VOICE_WORDS):
            issues.append("B9 柯德声音警告（关键词表未命中毒舌/吐槽/直率信号，请人工确认文风；仅警告，不拦截归档）")
        # ⑤ 末世氛围：按阶段取词（混沌期=社会崩塌/生理基线；后期=该阶段氛围词）
        # 先剔除阶段时间标记（"清理期第X年"里的"清理"不能算氛围词），防时间词误命中
        _atm_text = re.sub(r"(混沌期|失控期|崩塌期|剧变期|清理期|分裂期|共存期|终局)第\s*[0-9一二两三四五六七八九十百千]+\s*(天|周|月|年)", "", text)
        _aw = STAGE_ATMOS_WORDS.get(_stage, WASTE_ATMOS_WORDS)
        if not any(w in _atm_text for w in _aw):
            issues.append(f"B9 氛围警告（{_stage}无该阶段氛围信号：{'/'.join(_aw[:6])}…；仅警告不拦截，请人工确认末世重量是否在线）")
        # ⑥ 结尾钩子（宽松信号，只警告不参与熔断计数）
        _tail = text[-200:]
        if not any(w in _tail for w in HOOK_SIGNAL_WORDS):
            issues.append("B9 结尾钩子缺失（末200字无悬念信号：忽然/不对劲/危险/？/轰…）")

    # B13 熵债机制硬查（熔断，v3禁写=错误机制）：编译代价写成"头痛/算力负荷/精神力/能量被抽"
    # 注意排除合法语境：否定表述（"不是头痛"）、生理动作（"揉了揉太阳穴"）、日常生理反应（"太阳穴血管一跳"）
    _suck_bad = [
        r"头痛[，,、\s]{0,4}(?:像|如|欲裂|加剧|回来|还在|欲)",
        r"头痛(?:欲裂|加剧|像|如)",
        r"太阳穴[^。；，]{0,8}(?:痛|胀痛|刺痛|发紧|突突)",
        r"(?:捅|戳|刺|插|扎|钻)进?太阳穴|太阳穴(?:被|挨).{0,4}(?:捅|戳|刺|插|扎)",
        r"算力(?:负荷|发沉|被抽|耗尽)",
        r"(?:消耗|耗掉|抽干).{0,4}(?:精神力|脑力|精力)",
        r"能量(?:从身体|被身体|被抽走|抽干)",
        r"脑(?:子|袋)?(?:沉重|浆|被烧)",
        r"身体过载|凭空疲劳",
    ]
    _suck_hit = None
    for _pat in _suck_bad:
        for _mm in re.finditer(_pat, text):
            _ctx = text[max(0, _mm.start()-12):_mm.end()+12]
            # 合法语境豁免：否定/疑问/动作/生理（血管跳/揉了揉/对准）
            if re.search(r"(不是|不算|没有|哪|谁|怎么|难道|揉|按|摸|指|对准|举起|血管)", _ctx):
                continue
            _suck_hit = _mm.group(0)
            break
        if _suck_hit:
            break
    if _suck_hit:
        issues.append(f"B13 熵债机制熔断：出现旧版抽能写法「{_suck_hit}」（编译代价=头痛/算力负荷/精神力=硬伤，v3禁写；正确=环境落账+推演难度感）")

    # B14 后期概念词熔断（名词时代错位）：当前阶段出现更晚阶段的正式概念词=硬伤
    # 词表按阶段：只禁"当前阶段不该正式出现"的强概念词；传闻/预告/描述性用法由F门禁放行
    if anchor_no is not None:
        _stage_b14 = _stage_by_anchor(anchor_no)
        _late_by_stage = {
            "混沌期": ["复制人", "防火墙", "量子信道", "量子加密", "负熵流配额", "静默区", "元规则", "模因", "锁杀", "信标链", "信标源", "工程战", "悬空城", "巨构", "派系武装", "卡门线", "超级风暴", "断洋流", "大气程序", "宜居带", "积雨云平台", "气候穹顶", "地幔据点", "ZERO", "环境神", "八派", "钟楼", "故土", "复归会", "黎明派", "锚派", "断点派", "新纪元派", "介质层", "环境接口", "纳米探针", "无线电力", "逆向推导"],
            "清理期": ["复制人", "防火墙", "量子信道", "量子加密", "负熵流配额", "静默区", "元规则", "模因", "锁杀", "信标链", "信标源", "工程战", "悬空城", "巨构", "派系武装", "卡门线", "超级风暴", "断洋流", "大气程序", "宜居带", "积雨云平台", "气候穹顶", "地幔据点", "ZERO", "环境神", "八派", "钟楼", "故土", "复归会", "黎明派", "锚派", "断点派", "新纪元派", "介质层", "环境接口", "纳米探针", "无线电力", "逆向推导"],
            "分裂期": ["悬空城", "卡门线", "超级风暴", "断洋流", "模因", "元规则", "锁杀", "环境神", "回滚", "Lv3", "模因战", "大气程序", "积雨云平台", "气候穹顶", "地幔据点", "ZERO"],
            "共存期": ["断洋流", "回滚", "全球性攻击", "卡门线", "锁杀"],
            "终局": [],
        }
        _late_words = _late_by_stage.get(_stage_b14, [])
        # B14 比喻豁免：禁词若出现在「编程/比喻语境」（同句含无日志/无报错/服务器/编译/代码/接口等），
        # 视为柯德的认知比喻（合法），不熔断；作为正式概念/实体/组织名出现（无比喻词）才熔断。
        _metaphor_ctx = {
            "环境接口": ["无日志", "无报错", "无UI", "服务器", "接口", "编译", "代码", "生产环境", "测试环境", "README", "文档", "像", "仿佛", "比喻"],
            "介质层": ["像", "仿佛", "比喻", "内核", "底层"],
            "信标": ["像", "仿佛", "比喻", "信号塔", "灯塔"],
            "主控室": ["像", "仿佛", "比喻", "驾驶舱"],
        }
        _late_hit = []
        for _w in _late_words:
            _meta_kw = _metaphor_ctx.get(_w)
            if _meta_kw:
                _pos = 0
                _is_meta_all = True   # 该禁词所有出现均须命中比喻词才放行
                while True:
                    _i = text.find(_w, _pos)
                    
                    if _i == -1:
                        break
                    _seg = text[max(0, _i - 10): _i + len(_w) + 10]
                    if not any(_k in _seg for _k in _meta_kw):
                        _is_meta_all = False   # 存在非比喻用法 → 正式概念 → 熔断
                        break
                    _pos = _i + len(_w)
                if not _is_meta_all:
                    _late_hit.append(_w)
            else:
                if _w in text:
                    _late_hit.append(_w)
        if _late_hit:
            issues.append(f"B14 {_stage_b14}后期概念词熔断：出现后期概念词「{'、'.join(_late_hit[:5])}」（{_stage_b14}名词=认知上限，后期词=时代错位硬伤；只可作描述性动词或传闻，不可作正式概念）")

    # B12 战斗机动性软查（点名不熔断）：战斗/攻防锚（6/13/15/29/37/38/39）要求动态机动信号
    # 防"站桩对轰"：战斗=移动中完成（追击/缠斗/借地形/借气流），禁止面对面互砸
    _battle_anchors = {6, 13, 15, 29, 37, 38, 39}
    if anchor_no in _battle_anchors:
        _battle_sig = re.findall(r"追击|缠斗|脱战|闪避|俯冲|爬升|滑翔|借风|借气流|热气流|急流|绕到|侧翼|拉开距离|突进|后退|移位|腾空|落地|翻滚|奔跑|冲向|撤离|包抄", text)
        _attack_sig = re.findall(r"热浪|风墙|水刀|应力|裂纹|塌|掀翻|冲击|注入|干扰|补丁|读模型|推演|烧债|烧钱|算账", text)
        if _attack_sig and not _battle_sig:
            issues.append("B12 战斗机动性警告：有攻防动作但无动态机动信号（追击/闪避/借气流/移位）——战斗=移动中完成，禁止站桩对轰，建议补机动维度")

    # B11 尺度震撼检查（软查点名，不熔断）：巨构/灾难/决战锚（25/26/27/34/35/36/37/38）
    # 要求：宏大量级必须给"画面轨换算"（步行/小时/类比/当量/对比）或"宏观感官锚点"（尺寸+声音/温度/光）
    # 防"三百公里长墙"只给概念不给体感（好看五杠杆·画面缺失=点名）
    _scale_anchors = {25, 26, 27, 34, 35, 36, 37, 38}
    if anchor_no in _scale_anchors:
        _scale_conv = re.findall(r"步行|徒步|走.{0,6}(天|小时|分钟|年)|电梯|坐.{0,4}(小时|分钟)|像.{0,4}(山|海|城|墙)|\d+(?:\.\d+)?(?:亿吨|万吨|公里|千米|米高|米长|米深)|三峡|珠峰|原子弹|航母|当量|功率|度", text)
        _scale_sense = re.findall(r"嗡|轰鸣|震颤|震动|阴影|光晕|热浪|低温|寒风|风压|气压|臭氧|金属|锈|雷鸣|轰", text)
        _macro = re.findall(r"公里|千米|万吨|亿吨|千米高|公里长|米高|塔|墙|城|风暴|裂谷|海啸|大陆|垂直|贯穿|遮天", text)
        if not _scale_conv and not _scale_sense:
            issues.append("B11 尺度震撼警告：本锚含巨构/灾难/决战场景，但正文无尺度换算（步行/电梯/类比/当量）也无宏观感官锚点（尺寸+声音/温度/光）——只有概念没画面=点名")
        elif _macro and not _scale_conv:
            issues.append("B11 尺度震撼警告：出现宏大量级（" + "、".join(list(dict.fromkeys(_macro))[:3]) + "）但无画面轨换算——建议补人类经验锚点（步行多久/像什么/当量多少），只给抽象数字=点名")

    # B10 物理自洽熔断（防编程奇幻：无物理定律支撑的超现实创意=废稿，硬核是底线）
    # 视角分级（v2，比喻修辞合法——柯德/角色的台词/内心里的编程比喻=特色放行；
    # 叙述层把环境/存在直接写成运行中的程序=机制落地=熔断）
    def _in_dialog(txt, pos):
        """判断位置是否在台词区（引号内）——台词区=角色说话=比喻/吐槽合法"""
        before = txt[:pos]
        q = before.count('"') + before.count('“') + before.count('「') + before.count('『')
        return q % 2 == 1
    def _is_meta(txt, start, end):
        # 只豁免"奇幻词本身是比喻主体"（前面紧邻像/仿佛/如+该词=本体），
        # 不豁免"像X一样"尾随修饰（那只是形容词性修辞，奇幻词仍是机制落地）
        ctx = txt[max(0,start-25):min(len(txt),end+5)]
        before = txt[max(0,start-25):start]
        return bool(re.search(r"(就像|好像|像是|仿佛|如|打个比方|所谓|称之为|戏称|比方|我管这叫)[^，。；！？]{0,8}$", before))
    _phant_hits = []
    for _m in re.finditer(
            r"格式化工具|系统管理员|管理员权限|无限权限|意识上传|意识外包|外部计算单元|代码成精|"
            r"进化出自我意识|进化出意识|把自己编译成|皮肤表面浮现出.{0,14}代码|皮肤.{0,10}十六进制|被格式化|格式化成|"
            r"身体.{0,6}(JSON|数据库|临时表|数据块|数据结构)", text):
        if _in_dialog(text, _m.start()):
            continue  # 台词区=角色吐槽比喻，合法（柯德是程序员，这是特色）
        if _is_meta(text, _m.start(), _m.end()):
            continue  # 叙述层但有明确比喻标记=合法修辞
        # 硬盘分区比喻豁免（「世界分成几块硬盘/不想被格式化就选个分区」=合法暗喻）
        _ctx = text[max(0,_m.start()-40):min(len(text),_m.end()+40)]
        if re.search(r"硬盘|分区|坏道|重装系统|Ctrl\+Z", _ctx) and _m.group(0) in ("被格式化","格式化成"):
            continue
        _phant_hits.append(_m.group(0))
    if _phant_hits:
        _uniq = list(dict.fromkeys(_phant_hits))[:6]
        issues.append(f"B10 物理自洽熔断：命中编程奇幻词（{'、'.join(_uniq)}）——创意缺物理定律支撑，禁止无物理脚手架的超现实设定，必须重写")
    else:
        # 抽象落地词（v2）：叙述层把环境/存在直接写成程序实体（数据触须/接口/进程/逻辑层面…）
        # 台词区或明确比喻=放行；叙述层直写=点名（软查，防"数据触须"式漏网）
        _abs = re.findall(r"数据触须|数据流.{0,6}扎|接口.{0,6}关闭|进程.{0,4}(?:关闭|拒绝|执行)|逻辑层面.{0,6}(?:拒绝|访问)|"
                          r"大气环流模型.{0,6}(?:数据|触须)|环境.{0,6}对.{0,4}进程.{0,6}关闭", text)
        _abs_real = []
        for a in _abs:
            a = a if isinstance(a, str) else a[0]
            pos = text.find(a)
            if pos >= 0 and not _in_dialog(text, pos) and not _is_meta(text, pos, pos+len(a)):
                _abs_real.append(a)
        if _abs_real:
            issues.append(f"B10 抽象落地警告：叙述层把环境写成程序实体（{'、'.join(list(dict.fromkeys(_abs_real))[:4])}）——编程比喻只许在柯德台词/内心，叙述层机制落地=奇幻倾向，请改回物理机制（量子层/熵债/推演）")
        # 软词：光球/幽灵式拟人化异常体 + 操作语境（有意识/会/进化/学习/情绪…）
        for _m in re.finditer(r"光球|幽灵|递归幽灵", text):
            _s = max(0, _m.start()-20); _e = min(len(text), _m.end()+30)
            _ctx = text[_s:_e]
            if re.search(r"像|仿佛|如|比喻", _ctx):
                continue  # 比喻语境放行
            if re.search(r"意识|会|进化|学习|预测|脉冲|打招呼|情绪|住进|藏在.{0,4}(脑|意识)|斐波那契", _ctx):
                issues.append(f"B10 物理自洽熔断：『{_m.group(0)}』被写成拟人化有意识体（缺物理支撑）——异常体只能是有明确物理机制的现象（介质层量子进程/失稳锚点），禁止代码成精")
                break

    return issues

def append_story(outdir, text, anchor_no=None):
    """把星尘的正文追加到 故事正文.md，按章编号（AI 自带章号则标准化，短回复不编号附到当前章）
    B1 章号守卫：AI 报的章号 ≠ 程序期望号时，强制用期望号+保留副标题落盘，杜绝跳号/重号。
    B2 人称消毒：落盘前强制第三人称（保护台词区），叙述区「我」不落盘。
    B7/B9 熔断：反雷同黑名单或契合度缺失≥2项 = 该章丢弃不落盘（写 .b7_blocked 触发重写轮）"""
    try:
        story_file = outdir / "故事正文.md"
        existing = story_file.read_text(encoding="utf-8") if story_file.exists() else ""
        n = existing.count("## 第")  # 已有章节数（粗略）
        cleaned = clean_story_text(text)
        if not cleaned:
            return
        # B2b 落盘前强制第三人称消毒（ASCII/弯引号台词受保护，不误伤）
        cleaned = fix_person_to_third(cleaned)
        # B3~B9 质量门禁（B7 黑名单 / B9 契合度熔断 = 丢弃；其余=警告不拦截防误伤）
        try:
            _gate_issues = _quality_gate(outdir, cleaned, story_file, anchor_no)
            for issue in _gate_issues:
                log(f"⚠ 质量门禁：{issue}", C_WARN)
            # 真熔断：B7 反雷同黑名单命中 或 B9 契合度缺失≥2项 = 该章丢弃，不落盘（污染/跑偏章绝不能进正文）
            _b_fatal = [i for i in _gate_issues
                        if i.startswith("B7") or (i.startswith("B9") and "熔断" in i)]
            if _b_fatal:
                log(f"🛑 质量门禁熔断：本章不合格（{'；'.join(_b_fatal)}），已丢弃不落盘，需重写。", C_WARN)
                # 把丢弃事件记录到全局（run_loop 可据此要求星尘重写）
                try:
                    _f = outdir / ".b7_blocked"
                    with open(_f, "a", encoding="utf-8") as f:
                        f.write(f"{now_str()} | {'；'.join(_b_fatal)}\n")
                except Exception:
                    pass
                return
        except Exception as ge:
            log(f"⚠ 质量门禁异常：{ge}", C_WARN)
        # 提取 AI 自带的章节标题（有则用它，避免程序再补重复标题）
        ai_n, title, body = extract_chapter_title(cleaned)
        if ai_n is not None and title:
            # B1 章号守卫：期望号 = 现有最大章号 + 1
            expected = _next_chapter_no(existing)
            if ai_n == expected:
                final_title = title
            else:
                # 强制用程序期望号，保留 AI 副标题（如有）
                sub = title.split(" ", 1)[1].strip() if " " in title else ""
                final_title = f"第{expected}章" + (f" {sub}" if sub else "")
                log(f"⚠ 章号守卫：AI 报「{title}」≠期望「第{expected}章」，已用程序号落盘（副标题保留）", C_WARN)
            # AI 标了章号就是新章：即使正文短也单独成章，不并入当前章
            with open(story_file, "a", encoding="utf-8") as f:
                f.write(f"\n\n## {final_title}\n\n{body}")
            render_novel(outdir)  # 同步刷新书本样式的 小说.html
            return
        with open(story_file, "a", encoding="utf-8") as f:
            if len(cleaned) < 120:
                f.write("\n\n" + cleaned)  # 短内容并入当前章
            else:
                f.write(f"\n\n## 第{n+1}章\n\n{cleaned}")
        render_novel(outdir)  # 同步刷新书本样式的 小说.html
    except Exception as e:
        log(f"⚠ 正文写入失败：{e}", C_WARN)

def render_novel(outdir):
    """把 故事正文.md 渲染成书本样式的 小说.html（纯正文，无对话）"""
    try:
        sf = outdir / "故事正文.md"
        if not sf.exists():
            return
        text = sf.read_text(encoding="utf-8")
        parts = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## "):
                parts.append(f'<h2>{html.escape(line[3:])}</h2>')
            elif line.startswith("# "):
                parts.append(f'<h1>{html.escape(line[2:])}</h1>')
            else:
                parts.append(f'<p>{html.escape(line)}</p>')
        body = "\n".join(parts)
        (outdir / "小说.html").write_text(
            NOVEL_TEMPLATE.replace("__TITLE__", "《介质》· 小说正文").replace("__BODY__", body),
            encoding="utf-8")
    except Exception as e:
        log(f"⚠ 小说渲染失败：{e}", C_WARN)

def chapter_count(outdir):
    """当前故事已连载到第几章（读 story.md 统计）"""
    try:
        sf = outdir / "故事正文.md"
        return sf.read_text(encoding="utf-8").count("## 第") if sf.exists() else 0
    except Exception:
        return 0

def read_recent_chapters(outdir, n=3, max_chars=8000):
    """读取故事正文.md 最近 n 章原文，供模型写作/评审前通读。
    返回字符串（含章节标题）；正文不存在或为空时返回空串。
    限制 max_chars 避免超 token（取最近几章靠后的部分）。"""
    try:
        sf = outdir / "故事正文.md"
        if not sf.exists():
            return ""
        text = sf.read_text(encoding="utf-8")
        # 按章节切分
        markers = [m.start() for m in re.finditer(r"## 第\d+章", text)]
        if not markers:
            return text[-max_chars:] if max_chars else text
        # 取最后 n 章的起点
        starts = markers[-n:]
        recent = "\n".join(text[m:].split("\n", 1)[1].strip() for m in starts)
        # 超长则截断（保留尾部，因为最新内容最重要）
        if max_chars and len(recent) > max_chars:
            recent = recent[-max_chars:]
            recent = "…（前文已省略）\n" + recent
        return recent
    except Exception:
        return ""

def extract_story_position(text):
    """P2：从星尘输出中提取【本章定位】区块（仅3行元信息，不进正文）"""
    m = re.search(r"【本章定位】\s*(.*?)(?:\n\n|\Z)", text or "", re.S)
    if not m:
        return ""
    return m.group(1).strip()

def strip_story_position(text):
    """P2：把【本章定位】区块从正文中剥离（程序提取后不落盘）"""
    return re.sub(r"\n?【本章定位】.*?(?:\n\n|\Z)", "\n", text or "", flags=re.S).strip()

def read_story_file(outdir):
    """P10：读取当前正文文件全文（供文风样本抽取）"""
    try:
        p = outdir / "故事正文.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""

