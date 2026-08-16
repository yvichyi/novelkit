#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伏笔账本管理：解析（表格/登记/支线池 三格式兼容）→ 分级（主线/支线）→ 注入（锚点绑定+主线优先+支线冷冻）

背景：旧版把「伏笔账本.md」全文裸奔注入 prompt，AI 每章看到所有历史伏笔（含支线/噪音），
容易被"好写的支线"带跑主线。本模块把注入端改为分级筛选：

  · 【本章应收网】= 大纲当前锚点标注「- 回收伏笔：…」命中的未回收伏笔（代码强制回收）
  · 【未回收主线】= 主线伏笔按埋设序取前 N 条（可铺垫，禁止展开成支线）
  · 【支线冷冻】  = 支线伏笔只给一条统计，禁止展开

账本支持三种格式（新旧兼容）：
  1) 主表表格：| V-01 | 内容 | 埋设章 | 计划回收 | 状态 | 关联 |   （状态列=已埋/已回收）
  2) 登记区：  ## 第N章 伏笔登记 / - 名称（级别A·主线·预计X回收）  （新格式：名称｜级别A｜主线/支线｜预计X回收）
  3) 支线池：  ## 支线池 / - 名称（级别B·预计X回收）
"""
import re

# 级别标签（登记时按 A/B/C 输出；A=核心必须回收，B=观察可回收，C=噪音不入账）
LEVEL_LABELS = {"A": "核心", "B": "观察", "C": "噪音"}


def _split_blocks(text):
    """按 markdown 标题切块：返回 [(标题, 块内容)]，标题含 '##' 或 '###'。"""
    if not text:
        return []
    # 用 ## 切分（兼容 ## 与 ### 变体）
    m = re.split(r"(?m)^(#{2,3})\s+(.+?)\s*$", text)
    # m[0] = 头部，之后每 3 个一组：('##', 标题, 内容)
    blocks = []
    head = m[0].strip()
    if head:
        blocks.append(("", head))
    for i in range(1, len(m) - 2, 3):
        blocks.append((m[i + 1].strip(), (m[i + 2] or "").strip()))
    return blocks


def _table_rows(text):
    """从文本块提取 markdown 表格行：| a | b | c | → ['a','b','c']（跳过表头/分隔行）"""
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 跳过表头（含 ID/内容 字样的行）和分隔行（---）
        if cells and re.fullmatch(r":?-{2,}:?", cells[0] or ""):
            continue
        joined = " ".join(cells)
        if re.search(r"\b(ID|内容|伏笔)\b", joined) and len(cells) < 3:
            continue
        rows.append(cells)
    return rows


def _split_registered_line(raw):
    """拆解登记区一条（或一行多条的旧格式）→ [(名称, 级别, 主线/支线, 回收计划)]。

    支持：
      新格式：名称｜级别A｜主线/支线｜预计X回收
      旧格式：名称（级别A·预计X回收；名称2（细节）·级别B·预计Y回收；…）
    """
    out = []
    raw = raw.strip()
    if not raw:
        return out
    if "｜" in raw or "|" in raw:
        parts = [p.strip() for p in re.split(r"[｜|]", raw)]
        nm = parts[0]
        level = mainline = plan = ""
        for p in parts[1:]:
            if p.upper() in ("A", "B", "C"):
                level = p.upper()
            elif p in ("主线", "支线"):
                mainline = p
            else:
                plan = p
        out.append((nm, level, mainline or "主线", plan))
        return out
    # 旧格式：提取最外层 （…） 内容（允许内文含嵌套括号，贪婪匹配到最后一个 ））
    lm = re.search(r"（(.*)）\s*$", raw, re.S)
    if not lm:
        out.append((raw, "", "主线", ""))
        return out
    prefix = raw[:lm.start()].strip()
    inner = lm.group(1)
    # 内文按 ； 或 ；（中文分号）拆成多段（旧格式一行多伏笔）
    segs = [s.strip() for s in re.split(r"[；;]", inner) if s.strip()]
    if not segs:
        out.append((prefix or raw, "", "主线", ""))
        return out
    # 第一段的名称 = 前缀 + 段内 `（…）` 之前的文字
    for i, seg in enumerate(segs):
        name = prefix if i == 0 else ""
        # 段内可能带括号细节：名称（细节）·级别X·预计Y
        seg_nm = seg.split("级别")[0].strip("· ")
        # 去掉残留括号（含嵌套）
        seg_nm = re.sub(r"[（(][^）)]*[）)]", "", seg_nm).strip("· ")
        name = (name + seg_nm).strip() if name else seg_nm
        name = name.strip("· ，,。")
        name = re.sub(r"持续$", "", name).strip()   # 旧数据噪声清洗
        level = ""
        lv = re.search(r"级别\s*([ABC])", seg)
        if lv:
            level = lv.group(1).upper()
        plan = ""
        pl = re.search(r"预计\s*([^·；;]+)", seg)
        if pl:
            plan = pl.group(1).strip("· ，,。")
        mainline = "支线" if "支线" in seg else "主线"
        if name:
            out.append((name, level, mainline, plan))
    return out



def parse_ledger(text):
    """解析伏笔账本 → dict：
        main:      未回收主线伏笔  [{id, content, level, plan, chapter}]
        side:      未回收支线伏笔  [{content, level, plan, chapter}]
        recovered: 已回收伏笔      [{content, chapter}]
        discarded: 已舍弃条目（只统计数量用）
    新旧格式全兼容，解析失败也不抛异常。
    """
    result = {"main": [], "side": [], "recovered": [], "discarded": []}
    if not text:
        return result
    blocks = _split_blocks(text)

    # 收集各章登记区与支线池的条目（标题匹配）
    registered = []          # (chapter, content, level, mainline, plan)
    for title, body in blocks:
        t = title or ""
        if "支线" in t or "已舍弃" in t:
            # 支线池 / 已舍弃：记录（支线池条目归 side；已舍弃只计数）
            for cells in _table_rows(body):
                if len(cells) >= 2 and not cells[0].startswith("V-"):
                    # 已舍弃表格：| F-03 | 内容 | ... | -> 内容在 col1
                    result["discarded"].append(cells[1])
                    continue
            for line in body.split("\n"):
                s = line.strip()
                if s.startswith("- ") and "回收" not in s[:6]:
                    if "支线" in t:
                        registered.append((0, s[2:].strip(), "", "支线", ""))
                    else:
                        result["discarded"].append(s[2:].strip())
            continue
        cm = re.match(r"第\s*(\d+)\s*章", t)
        if cm and "登记" in t:
            ch_no = int(cm.group(1))
            for line in body.split("\n"):
                s = line.strip()
                if not s.startswith("- "):
                    continue
                raw = s[2:].strip()
                if not raw:
                    continue
                if raw.startswith("已回收") or raw.startswith("回收"):
                    nm = raw.split("：", 1)[-1].split(":", 1)[-1].strip()
                    result["recovered"].append({"content": nm, "chapter": ch_no})
                    continue
                for nm, level, mainline, plan in _split_registered_line(raw):
                    if nm:
                        registered.append((ch_no, nm, level, mainline, plan))

    # 主表表格（ID 以 V- 开头）→ 主线/回收
    for title, body in blocks:
        if "支线" in title or "已舍弃" in title:
            continue
        for cells in _table_rows(body):
            if not cells:
                continue
            vid = cells[0]
            if not re.match(r"^V-\d+", vid):
                continue
            content = cells[1] if len(cells) > 1 else ""
            buried = cells[2] if len(cells) > 2 else ""
            plan = cells[3] if len(cells) > 3 else ""
            status = cells[4] if len(cells) > 4 else ""
            related = cells[5] if len(cells) > 5 else ""
            level = "A" if "核心" in related or "主线" in related else "B"
            bm = re.search(r"(\d+)", buried)
            ch_no = int(bm.group(1)) if bm else 0
            if "回收" in status or "已回收" in status:
                result["recovered"].append({"content": f"{vid} {content}".strip(), "chapter": ch_no})
            else:
                result["main"].append({"id": vid, "content": content, "level": level,
                                       "plan": plan, "chapter": ch_no, "related": related})

    # 登记区条目：主线 → main，支线 → side（自动生成 id D-章-序号）
    for ch_no, nm, level, mainline, plan in registered:
        if mainline == "支线":
            result["side"].append({"id": "", "content": nm, "level": level,
                                   "plan": plan, "chapter": ch_no})
        else:
            if not any(m["content"] == nm for m in result["main"]):
                result["main"].append({"id": "", "content": nm, "level": level,
                                       "plan": plan, "chapter": ch_no})
    return result



def current_anchor_recovery(outline_text, ch_no):
    """从大纲文本解析「当前章所属锚点」的回收伏笔字段。

    锚定义格式：◆ 锚N 标题（第X~Y章）：…
                - 回收伏笔：V-01, V-04, 哨音人
    章号落入的所有锚区块的「回收伏笔」字段合并去重（锚区间重叠时取并集，更宽容）。
    返回：[(字段原文, 锚号)]；无大纲/无字段 → []。
    """
    if not outline_text:
        return []
    # ① 收集所有锚：锚号 + 章号区间 + 回收伏笔字段
    anchors = []
    heads = list(re.finditer(r"(?m)^◆\s*锚\s*(\d+)\b[^\n]*$", outline_text))
    for i, hm in enumerate(heads):
        a_no = int(hm.group(1))
        title = hm.group(0)
        tm = re.search(r"第\s*(\d+)\s*~\s*(\d+)\s*章", title)
        if tm:
            lo, hi = int(tm.group(1)), int(tm.group(2))
        else:
            tm2 = re.search(r"第\s*(\d+)\s*章", title)
            if tm2:
                lo = hi = int(tm2.group(1))
            else:
                lo = hi = None  # 无章号：按顺序推算
        end = heads[i + 1].start() if i + 1 < len(heads) else len(outline_text)
        block = outline_text[hm.end():end]
        fm = re.search(r"(?m)^-\s*回收伏笔\s*[：:]\s*(.+)$", block)
        names = []
        if fm:
            for x in re.split(r"[，,；;]", fm.group(1)):
                x = x.strip()
                if not x:
                    continue
                x = re.split(r"[（(]", x)[0].strip()   # 剥离注释（V-01（说明）→ V-01）
                if x:
                    names.append(x)
        anchors.append({"no": a_no, "lo": lo, "hi": hi, "names": names})
    # ② 无章号锚按顺序补区间：本锚下限=上一锚上限+1，上限=下一锚下限-1
    for i, a in enumerate(anchors):
        if a["lo"] is None:
            prev_hi = anchors[i - 1]["hi"] if i > 0 and anchors[i - 1]["hi"] else 0
            a["lo"] = prev_hi + 1
            nxt = anchors[i + 1] if i + 1 < len(anchors) else None
            nxt_lo = nxt["lo"] if nxt and nxt["lo"] else 999
            a["hi"] = nxt_lo - 1
    # ③ 命中当前章的锚 → 合并回收字段（同名伏笔多锚标注时合并锚号，去重）
    merged = {}
    for a in anchors:
        if a["lo"] is not None and a["lo"] <= ch_no <= (a["hi"] or 999):
            for n in a["names"]:
                merged.setdefault(n, set()).add(a["no"])
    out = [(n, tuple(sorted(nos))) for n, nos in merged.items()]
    return out


def _match_item(items, name):
    """按 ID（V-01）或内容子串匹配账本条目的未回收项；返回匹配条目列表。"""
    hits = []
    name = name.strip()
    if not name:
        return hits
    for it in items:
        if it.get("id") and it["id"].lower() == name.lower():
            hits.append(it)
        elif name.lower() in (it.get("content") or "").lower():
            hits.append(it)
    return hits


def _fmt_item(it):
    """条目展示：ID + 内容 + （级别X·预计Y回收）；plan 已含'回收'时不重复拼。"""
    s = ((it.get("id") or "") + " " + (it.get("content") or "")).strip()
    bits = []
    if it.get("level"):
        bits.append(f"级别{it['level']}")
    plan = (it.get("plan") or "").strip()
    if plan:
        bits.append(f"预计{plan}" if plan.endswith("回收") else f"预计{plan}回收")
    return s + (f"（{'·'.join(bits)}）" if bits else "")


def build_foreshadow_card(ledger_text, anchor_recovery=None, main_limit=3, side_note=True):
    """生成【伏笔卡】注入文本（分级管控，防止支线带跑主线）。

    anchor_recovery: [(伏笔名, 锚号)] 来自 current_anchor_recovery()
    main_limit:      未回收主线伏笔最多注入 N 条（按埋设章序）
    返回注入文本；账本空/无可注入 → 返回空串。
    """
    if not ledger_text or not ledger_text.strip():
        return ""
    parsed = parse_ledger(ledger_text)
    main, side = parsed["main"], parsed["side"]
    parts = []
    # ① 本章应收网（锚点绑定：命中且未回收）
    due = []
    if anchor_recovery:
        seen = set()
        for name, a_no in anchor_recovery:
            for it in _match_item(main, name):
                key = it.get("id") or it["content"]
                if key in seen:
                    continue
                seen.add(key)
                due.append((a_no, it))
    if due:
        lines = [f"- (锚{'/'.join(map(str, a))}标注) {_fmt_item(it)} —— 本章必须正面落地回收，不许跳过"
                 for a, it in due]
        parts.append("【本章应收网伏笔（大纲锚点指定，必须回收至少1条）】\n" + "\n".join(lines))
    # ② 未回收主线（前 N 条，按埋设章排序）
    main_sorted = sorted(main, key=lambda x: x.get("chapter") or 999)
    due_keys = {(it.get("id") or it["content"]) for _, it in due}
    pool = [it for it in main_sorted if (it.get("id") or it["content"]) not in due_keys]
    if pool:
        lines = [f"- {_fmt_item(it)}" for it in pool[:main_limit]]
        parts.append("【未回收主线伏笔（前{}条，可选铺垫·禁止展开成支线，回收时机由大纲决定）】\n".format(min(main_limit, len(pool))) + "\n".join(lines))
    # ③ 支线冷冻（只给统计）
    if side_note and side:
        parts.append(f"【另有 {len(side)} 条支线伏笔未回收（当前阶段不回收，禁止展开成支线剧情，等大纲锚点标注再收）】")
    if not parts:
        # 无可注入：给极简占位，防止 AI 当"没有伏笔"自由发挥
        if parsed["recovered"]:
            return "【伏笔】本作有已回收伏笔 {} 条（历史完成项，不再展开）。当前无待回收伏笔，禁止新埋与主线无关的支线。".format(len(parsed["recovered"]))
        return ""
    return "【伏笔卡（分级管控：应收网>主线>支线冷冻，禁止支线取代主线）】\n" + "\n\n".join(parts)


def format_review_instruction():
    """评审【伏笔】区块的格式要求（注入评审 prompt 用，强制结构化登记）。"""
    return ("【伏笔】本章新埋设的伏笔：**每条独立一行**，严格格式「名称｜级别A/B/C｜主线/支线｜预计回收阶段」，"
            "级别A=核心必须回收、B=观察、C=噪音不入账；支线（与主线无直接关系）一律标「支线」；"
            "已回收的伏笔写「回收｜名称」；无则写'无'。禁止一行塞多条、禁止省略主线/支线标注。")
