#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词卡片：任务卡/设定卡/时间卡/人物卡/fewshot/记忆文档（从 ai_bridge_api.py 拆分，原文未改）。"""
import json
import os
import re
from pathlib import Path
from .config import CHARACTER_TIMELINE, FORESHADOW_WINDOW, LATE_STAGE_BLACKLIST, SCORE_DIMS, STAGE_ANCHORS, STAGE_DIMS, STORY_OUTLINE, TOPIC_DEFAULT
from .story import read_story_file


def extract_unresolved_foreshadow(checklist, limit=8):
    """P3：从清单提取未回收伏笔（兼容两种格式），返回 - 开头列表字符串"""
    if not checklist:
        return ""
    out = []
    for line in checklist.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "已回收@" in line:
            continue
        if line.startswith("- 【伏笔】") or line.startswith("-   ▸ 未回收"):
            item = line.lstrip("- ").strip()
            if item and item not in out:
                out.append(item)
        if len(out) >= limit:
            break
    return "\n".join("- " + x for x in out)

def extract_memory_card(checklist, limit_status=1):
    """P8：从清单提取'记忆快照'——最新【状态】卡 + 硬设定红线 + 主角声音锚点（紧凑，供写作模型读取，防细节漂移）"""
    if not checklist:
        return ""
    parts = []
    # 1) 最新【状态】卡（取最后一条=最新）
    states = [l.strip() for l in checklist.split("\n") if l.strip().startswith("- 【状态】")]
    if states:
        parts.append("【当前状态（最新一条，写作基准）】\n" + states[-1].lstrip("- "))
    # 2) 硬设定红线（防漂移，精炼版）
    hard_rules = []
    for kw in ["红线", "铁律", "硬设定"]:
        for l in checklist.split("\n"):
            if kw in l and l.strip().startswith("-"):
                hard_rules.append(l.strip().lstrip("- ").strip())
                break
    if hard_rules:
        parts.append("【硬设定红线（不可违反）】\n" + "\n".join("- " + r for r in hard_rules[:5]))
    # 3) 主角声音锚点（人物辨识度）
    voice = []
    for l in checklist.split("\n"):
        if any(k in l for k in ["吐槽", "毒舌", "直率", "冷静", "克制", "算账", "声音"]):
            if l.strip().startswith("-"):
                voice.append(l.strip().lstrip("- ").strip())
    if voice:
        parts.append("【主角声音（人物辨识度）】\n" + "\n".join("- " + v for v in voice[:3]))
    return "\n\n".join(parts)

def extract_style_samples(story_text, count=3):
    """P9：从正文抽取高光片段作为文风样本（每段约120字，供写作模型对齐文风）"""
    if not story_text:
        return ""
    # 抽取各章开头段（通常是最精炼的感官锚点）
    import re
    chapters = re.split(r'\n## 第', story_text)
    samples = []
    for ch in chapters[1:]:
        body = ch.split('\n', 1)[1] if '\n' in ch else ch
        paras = [p.strip() for p in body.split('\n\n') if len(p.strip()) > 40]
        if paras:
            samples.append(paras[0])
    if not samples:
        return ""
    # 均匀抽样（取开头/中间/结尾各一段）
    n = len(samples)
    picks = []
    for idx in [0, n//2, n-1]:
        if idx < n and samples[idx] not in picks:
            picks.append(samples[idx])
    return "\n\n".join("【文风样本】\n" + p[:150] for p in picks[:count])

def build_task_card(anchor_state, rhythm, checklist, outline):
    """C1：每章任务卡——从大纲锚定义+节奏计数器+伏笔清单生成本章必办事项（代码强制，聚焦本章，防跑偏）"""
    cur = anchor_state["current"]
    caps = {1:2, 2:6, 3:10, 4:10, 5:10, 6:10, 7:25, 8:25}
    cap = caps.get(cur, 25)
    parts = []
    title_m = re.search(rf"◆\s*锚{cur}\b([^\n]*)", outline)
    title = title_m.group(1).strip() if title_m else ""
    parts.append(f"· 当前锚：锚{cur}「{title}」（本锚已写{anchor_state['chapters_in_anchor']}章 / 上限{cap}章）")
    m = re.search(rf"◆\s*锚{cur}\b[^\n]*\n(.*?)(?=\n◆\s*锚|\Z)", outline, re.S)
    if m:
        block = m.group(1)
        for key in ["必达", "必写", "设定反哺"]:
            km = re.search(rf"- {key}：(.+)", block)
            if km:
                v = km.group(1).strip()
                if key == "设定反哺":
                    v = v[:200] + ("…" if len(v) > 200 else "")
                parts.append(f"· {key}：{v}")
    warns = []
    if rhythm["last_L2"] >= 3:
        warns.append(f"环境冲击已{rhythm['last_L2']}章无L2+（上限3，本章必须有L2以上环境冲击）")
    if rhythm["last_L3L4"] >= 6:
        warns.append(f"生死压力已{rhythm['last_L3L4']}章无L3/L4（上限6，本章必须有生死压力）")
    if rhythm["last_combat"] >= 5:
        warns.append(f"冲突对抗已{rhythm['last_combat']}章无（上限5，本章必须有冲突对抗）")
    if rhythm["last_impact"] >= 3:
        warns.append(f"外部冲击已{rhythm['last_impact']}章无（上限3，本章必须有外部世界冲击：逃难者/尸骸/失控现场/灾变痕迹）")
    parts.append("· 节奏提醒：" + ("；".join(warns) if warns else "正常（按计数器无超限）"))
    un = extract_unresolved_foreshadow(checklist, limit=1)
    parts.append("· 可选回收伏笔：" + un if un else "· 可选回收伏笔：无（本章可不埋伏笔）")
    parts.append("· 硬指标（写完逐条核对）：时间锚点（延续前文时间线，不倒退）｜现实术语≥3处（定律名嵌进感官/动作）｜主角声音2~3处（吐槽/直率/情绪自然）｜结尾钩子｜生理基线一笔（喝水/找食/疲惫）")
    return "\n".join(parts)

def build_setting_card(chapter_no, outline):
    """C3：阶段门禁式设定速查卡——从完整世界观(TOPIC_DEFAULT)按当前阶段抽取：
    ①通用设定（不含任何阶段/后期词）全保留；②当前阶段相关设定（含当前阶段关键词）保留；
    ③后期专属黑名单词强制剔除（只保留预告窗口的提示）；④常驻：物理红线+尺度震撼+当前锚必达。
    返回注入文本。"""
    stage, _, _ = stage_of_chapter(chapter_no)
    blacklist = LATE_STAGE_BLACKLIST.get(stage, [])
    topic = TOPIC_DEFAULT
    import re as _re
    sents = _re.split(r'(?<=[，。；：、\n])', topic)
    keep = []
    for s in sents:
        s = s.strip()
        if len(s) < 6:
            continue
        if any(bw in s for bw in blacklist):
            continue
        _stages = [r[0] for r in STAGE_ANCHORS]
        if stage in s or not any(st in s for st in _stages):
            keep.append(s)
    body = " ".join(keep)
    MAX_CARD = 40000  # 正文质量优先：设定速查卡允许较大（用户确认可翻几倍），上限防无限膨胀
    if len(body) > MAX_CARD:
        body = body[:MAX_CARD] + "…（设定速查卡截断，其余以评审的完整设定为准）"
    discipline = (
        "【设定一致性红线（硬，任何阶段必守）】严格遵循本书 novel_config/ 中的核心设定与红线表；"
        "禁止凭空自编无内在逻辑的设定/能力；任何新能力须有机制与代价（谁做的/怎么做的/为什么讲得通/代价是什么）。\n"
        "【尺度震撼法（宏大场面描写）】双轨震撼=画面轨70%（具体感官细节：步行四十天/影子像移动的山）+数据轨30%（可感知的量化：自重四亿吨/每秒三亿吨）；"
        "换算四法=距离→时间/质量→类比/能量→当量/尺度→人类尺度；三视角=角色局部70%+旁观20%+全局10%；"
        "账本写法=宏大现象是可算的逻辑账本，数字藏在叙事里，写错=硬伤。\n"
        f"【本阶段定位】当前= {stage}。本阶段可用的核心设定已在上方速查卡中，"
        "后期概念只可作预告/伏笔/传闻，不可实际使用=时间线硬伤。\n"
    )
    return f"【设定速查卡（阶段门禁：当前={stage}，后期设定已隔离，只可预告不可使用）】\n{body}\n\n{discipline}{FORESHADOW_WINDOW}"

def stage_of_chapter(chapter_no):
    """按章号返回 (阶段名, 锚区间下限, 锚区间上限)"""
    for name, a_lo, a_hi, ch_lo, ch_hi in STAGE_ANCHORS:
        if ch_lo <= chapter_no <= ch_hi:
            return name, a_lo, a_hi
    # 越界/未配置：有阶段配置时保持原 fallback；零配置（通用新书）→「全篇」不区分阶段
    if STAGE_ANCHORS:
        return STAGE_ANCHORS[-1][0], STAGE_ANCHORS[-1][1], STAGE_ANCHORS[-1][2]
    return "全篇", 1, 999

def build_ending_card(chapter_no):
    """结局收束卡：进度进入全书最后 1/4 时注入，要求收束伏笔/主线、逼近结局方向。

    通用化：从 outline.md 提取「结局方向」段；无结局方向时给通用收束指令。
    """
    import re as _re
    # 全书总章数：取 STAGE_ANCHORS 里最大的 ch_hi（元组结构：name, a_lo, a_hi, ch_lo, ch_hi）
    total = max((r[4] for r in STAGE_ANCHORS), default=999)
    if chapter_no < total - total // 4:
        return ""                                    # 前 3/4 不注入（防提前剧透结局）
    outline = STORY_OUTLINE
    ending = ""
    for _key in ("结局方向", "结尾钩子方向", "结局"):
        _i = outline.find(_key)
        if _i != -1:
            _seg = outline[_i:_i + 300]
            _j = _seg.find("\n\n")
            ending = _seg if _j == -1 else _seg[:_j]
            break
    lines = [f"【结局收束·硬】（当前第{chapter_no}章，全书约{total}章，已进入最后{total - chapter_no + 1}章）",
             "必须开始收束：所有主线/伏笔/人物弧光要在此阶段逐步落地，禁止开新支线、禁止引入新核心设定、禁止无限延展。"]
    if ending.strip():
        lines.append(f"【结局方向（来自大纲，必须朝它推进）】{ending.strip()}")
    else:
        lines.append("【结局方向】按大纲的结局构思推进；若大纲未定，本章起逐步让主线走向一个明确的收束点（不强行圆满，但必须收口）。")
    lines.append("每章结尾都要比上一章更接近结局，最后 1 章必须完成全书收尾（不是悬而未决的续写钩子）。")
    return "\n".join(lines)


def build_stage_guide(chapter_no):
    """生成【本阶段评分标准】注入文本（评审打分用，六维维度固定、要点随阶段变）"""
    name, a_lo, a_hi = stage_of_chapter(chapter_no)
    dims = STAGE_DIMS.get(name)
    if not dims:
        # 通用兜底：六维评分（不依赖书配置，零配置新书也能跑）
        dims = {
            "人物与声音": "主角人设稳定、有情绪有声音，不写成冷静机器",
            "情节推进": "本章有事件发生、主角有行动/选择/处境改变，禁止空转章",
            "世界设定": "核心设定在剧情里起作用，不裸用、不旁白甩说明书",
            "文本质感": "语言流畅、节奏好、对话自然、细节生动",
            "冲突张力": "有冲突/危机/看点，结尾留钩子",
            "一致性": "延续前文、不矛盾、不重复、时间只前进不倒退",
        }
    lines = [f"【本阶段评分标准（当前= {name}，锚{a_lo}~{a_hi}；六维维度固定，检查要点随阶段变化，按本阶段要点打分）】"]
    for dim, desc in dims.items():
        lines.append(f"· {dim}：{desc}")
    return "\n".join(lines)

def build_outline_card(chapter_no):
    """分层大纲注入：只注入「全局骨架 + 当前幕锚段 + 当前幕世界观新要素」，不注入其他幕详纲。

    理由：①防剧透（写前期不读后期锚，避免 AI 提前铺陈后期概念）
          ②省 token（STORY_OUTLINE 全文约 1.5万 token，分层后约 4000 token，省 70%）
          ③缓存稳（全局骨架固定前缀；当前幕锚段每幕才变一次，不是每章变）

    返回注入文本。
    """
    stage, _, _ = stage_of_chapter(chapter_no)
    outline = STORY_OUTLINE
    if not outline.strip():
        return ""                                # 零配置新书：没大纲就不注入
    if "◆ 锚" not in outline:
        return f"【故事大纲】\n{outline}"         # 大纲无锚标记：整体注入（不分层）

    # ① 全局骨架：开头到「◆ 锚1」之前（主题/弧光/结局/写法总纲/角色动机/节点规则/黑名单/求生逻辑/配方表）
    head = outline.split("◆ 锚1", 1)[0].strip()

    # ② 当前幕锚号区间（从 anchors.json 动态取，不硬编码具体作品的阶段）
    _stage_anchors = {r[0]: (r[1], r[2]) for r in STAGE_ANCHORS}
    lo, hi = _stage_anchors.get(stage, (1, 999))
    import re as _re
    start = outline.find(f"◆ 锚{lo} ")
    end = outline.find(f"◆ 锚{hi + 1} ")
    if end == -1:
        end = len(outline)
    anchor_block = outline[start:end].strip()

    # ③ 当前幕的世界观新要素（大纲里若有「【主线·阶段名」段则取，否则跳过）
    world_block = ""
    s2 = outline.find(f"【主线·{stage}")
    if s2 != -1:
        e2 = len(outline)
        for _nxt in [r[0] for r in STAGE_ANCHORS if r[0] != stage]:
            _i = outline.find(f"【主线·{_nxt}", s2 + 1)
            if _i != -1:
                e2 = min(e2, _i)
        world_block = outline[s2:e2].strip()

    parts = [head]
    if world_block:
        parts.append(world_block)
    parts.append(anchor_block)
    return f"【故事大纲·分层注入（当前幕={stage}，锚{lo}~{hi}）】\n" + "\n\n".join(parts)

def time_coordinate(chapter_no):
    """章号→故事时间坐标（内部罗盘，只注入 prompt 作背景，严禁写进正文）。
    返回 (时间标签, 时代阶段, 世界状态, 氛围基调)。
    通用版不内置具体作品的时间表：如需时间轴，在 novel_config/anchors.json 配阶段 + 大纲里写时间设定。
    恒返回空串，由 build_time_card 用通用模板兜底（按大纲设定 + 延续前文）。"""
    return "", "", "", ""

def build_time_card(chapter_no):
    """【时间坐标卡】注入 prompt：告诉模型当前章在时间轴哪个位置、世界该是什么状态。
    只作内部背景，正文用事件/氛围暗示时间推进，不印'第X天/第X周'标签。
    零配置新书：不写死时间轴，要求按大纲设定 + 延续前文。"""
    return (
        f"【时间坐标（内部背景，严禁写进正文）】你正在写第{chapter_no}章。\n"
        "- 故事时间由你的大纲设定；必须严格延续上一章结尾的时间点，只前进不倒退。\n"
        "- 铁律：正文**不要机械堆砌「第X天/第X周」这类标签**，改用事件/光线/身体状态等暗示标记时间推进"
        "（如：天色、灯亮灯灭、季节变化、人物处境变化）。"
    )

def build_character_card(chapter_no):
    """按章号返回「当前可用角色/组织池」注入文本（硬，防时间线回溯错位）。
    零配置（未配 timeline.json）→ 返回空串，不注入。"""
    if not CHARACTER_TIMELINE:
        return ""
    row = CHARACTER_TIMELINE[0]
    for r in CHARACTER_TIMELINE:
        if chapter_no >= r[0]:
            row = r
        else:
            break
    start_ch, stage, people, orgs, forbidden = row
    return (
        f"【当前可用角色/组织池（硬，防时间线回溯错位——本章能出场谁，谁还不该出场）】\n"
        f"当前= {stage}（自第{start_ch}章起）。\n"
        f"✅ 可用人物：{'、'.join(people)}\n"
        f"✅ 可用组织：{'、'.join(orgs)}\n"
        f"❌ 严禁正面登场（越界=时间线硬伤，只可作传闻/埋线，不可实际出场）：{'、'.join(forbidden)}\n"
        f"铁律：角色/组织只能在其登场时代之后出现；禁止把后期事物回溯安到更早时代。"
    )

def build_fewshot_card(chapter_no, sample_file=None):
    """P12：从时代范文库按当前章节所在时代抽取范文+技法拆解（few-shot 参考）

    用途：写作前对齐该时代的语言质感/尺度感/节奏/钩子。
    只注入当前阶段的区块——防止后期范文里的概念提前出现在前期章节。
    范文库文件：few_shot_samples.md（可选，无则跳过）。

    优先注入 sample_library 里该阶段最新归档的【整章样板】（可选），
    加上手动范文，作为写作模型的文风学习材料。
    """
    name, a_lo, a_hi = stage_of_chapter(chapter_no)

    # 通用版不区分子阶段：样板/范文按阶段名直接注入
    cur_stage = 4

    def _sample_stage_no(t):
        """解析样板/范文正文时间锚点 → 子阶段序号（通用版恒 4=不限制）"""
        return 4
    # ① 整章样板（sample_library，最高优先级——真实连载产出，含完整起承转合）
    lib_parts = []
    try:
        stage_dir = Path(__file__).parent / "sample_library" / name
        if stage_dir.exists():
            files = sorted(stage_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[:3]:  # 取最新3篇整章（多了稀释注意力）
                body = f.read_text(encoding="utf-8").strip()
                if not body:
                    continue
                # 阶段匹配：样板阶段不晚于当前章才注入（防后期污染前期）
                if _sample_stage_no(body) > cur_stage:
                    continue
                lib_parts.append(
                    f"【整章样板（{name}阶段，已通过门禁的完整章节；"
                    f"学习它的起承转合/质感/节奏/钩子，严禁照抄情节）】\n{body[:2500]}"
                )
                if len(lib_parts) >= 1:  # 早期章只要1篇匹配的
                    break
    except Exception:
        pass

    # ② 手动范文（few_shot_samples.md）
    if not sample_file:
        sample_file = str(Path(__file__).parent / "few_shot_samples.md")
    p = Path(sample_file)
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
            sections = re.split(r'\n## ', text)
            for sec in sections:
                if sec.startswith(name + "（"):
                    block = sec.split("\n---\n\n## 通用")[0].rstrip()
                    lib_parts.append(
                        f"【时代范文（{name}，从已验证的高质量章节提炼；"
                        f"只对齐质感与技法，严禁照抄句式/情节/比喻）】\n{block}"
                    )
                    break
        except Exception:
            pass

    if not lib_parts:
        return ""
    # 设定免责声明（防样板过时设定污染正文）：样板只学文风/结构/节奏，一切设定以速查卡为准
    _disclaimer = (
        "\n\n【⚠️ 样板免责声明（必读）】样板是从旧批次测试中筛选的，可能含已修订/过时的设定写法。"
        "你只学它的【文风质感/结构节奏/情绪递进】，绝不照抄情节、比喻、术语用法。"
        "一切设定以【设定速查卡】和【硬设定红线卡】为准——样板与设定冲突时，以设定为准，禁止模仿样板的过时写法。"
    )
    return "\n\n".join(lib_parts) + _disclaimer

def extract_hard_rules(checklist, limit=15):
    """红线卡：从清单自动挑含硬性关键词的条目（写作必守，替代全文12k注入防注意力稀释）"""
    kws = ["硬", "必", "禁止", "红线", "铁律", "绝不", "不得", "不许", "致命伤"]
    out = []
    for line in (checklist or "").split("\n"):
        line = line.strip()
        if not line.startswith("-") or "【状态】" in line:
            continue
        if any(k in line for k in kws):
            item = line.lstrip("- ").strip()
            if len(item) > 160:
                item = item[:157] + "…"
            if item and item not in out:
                out.append(item)
        if len(out) >= limit:
            break
    return "\n".join("- " + x for x in out)

def extract_event(review):
    """从评审提取【事件】区块（一句话本章发生了什么，供前情卡引用）"""
    if not review:
        return ""
    m = re.search(r"(?:【事件】|#{1,4}\s*事件)\s*(.+)", review)
    if m:
        return m.group(1).strip()
    return ""

def append_event(outdir, chapter_no, event_text):
    """把评审登记的【事件】追加到 前情事件.md（代码累积，供前情卡引用，零幻觉）"""
    try:
        p = outdir / "前情事件.md"
        line = f"- 第{chapter_no}章：{event_text}"
        if not p.exists():
            p.write_text("# 前情事件表（评审每章登记，前情卡引用）\n\n" + line + "\n", encoding="utf-8")
        else:
            with open(str(p), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

def build_memory_doc(outdir, checklist):
    """前情卡：代码从正文+清单+前情事件.md 提取结构化前情（零幻觉，替代模型摘要）"""
    parts = []
    # 1) 章节轨迹+时间线（最近15章，从正文提取）
    try:
        story = read_story_file(outdir)
        chapters = []
        if story:
            for m in re.finditer(r'^##\s+第(\d+)章\s*([^\n]*)', story, re.M):
                ch_no = m.group(1)
                ch_title = m.group(2).strip()
                seg = story[m.end():m.end() + 200]
                dm = re.search(r'第\d+[天周月]', seg)
                tm = dm.group(1) if dm else "时间未标"
                chapters.append(f"第{ch_no}章 {ch_title}（{tm}）")
        if chapters:
            parts.append("【章节时间线】" + " → ".join(chapters[-15:]))
    except Exception:
        pass
    # 2) 近期事件（评审【事件】区块登记，最近10条）
    try:
        ep = outdir / "前情事件.md"
        if ep.exists():
            evs = [l.strip() for l in ep.read_text(encoding="utf-8").splitlines() if l.strip().startswith("- 第")]
            if evs:
                parts.append("【近期事件（评审登记）】\n" + "\n".join(evs[-10:]))
    except Exception:
        pass
    # 3) 未回收伏笔（前6条）
    un = extract_unresolved_foreshadow(checklist, 6)
    if un:
        parts.append("【未回收伏笔】" + un.replace("\n", "；"))
    # 4) 最新状态卡
    try:
        states = [l.strip().lstrip("- ").replace("【状态】", "") for l in checklist.split("\n") if l.strip().startswith("- 【状态】")]
        if states:
            parts.append("【主角状态（最新）】" + states[-1])
    except Exception:
        pass
    return "\n\n".join(parts)

def prune_checklist(outdir, checklist, keep_status=2):
    """归档旧状态卡+已回收伏笔；返回 (精简清单, 归档文本)。每10章调用，防清单无限膨胀"""
    lines = (checklist or "").split("\n")
    new_lines = []
    archive = []
    status_count = 0
    total_status = sum(1 for l in lines if l.strip().startswith("- 【状态】"))
    for l in lines:
        s = l.strip()
        if s.startswith("- 【状态】"):
            status_count += 1
            if total_status - status_count >= keep_status:
                archive.append(l)
                continue
        if "已回收@" in s:
            archive.append(l)
            continue
        new_lines.append(l)
    return "\n".join(new_lines).strip(), "\n".join(archive).strip()

def extract_scores(text):
    """C2：解析评审【评分】区块：六维各1~5分。返回 dict 或 None（<3维有效视为无效）"""
    if not text:
        return None
    m = re.search(r"(?:【评分】|#{1,4}\s*评分)([\s\S]*?)(?=\n(?:【|#{1,4}\s*[^#\n]*:?|\Z)|$)", text, re.S)
    if not m:
        return None
    block = m.group(1)
    scores = {}
    for dim in SCORE_DIMS:
        dm = re.search(rf"{dim}\s*[:=＝]\s*(\d+)", block)
        if dm:
            v = int(dm.group(1))
            if 1 <= v <= 5:
                scores[dim] = v
    n_valid = len(scores)
    return scores if n_valid >= 3 else None

def append_score_history(outdir, chapter_no, stage_name, scores):
    """C2：评分历史写入 评分卡.md（一行一章，带阶段列，可视化连续低分）"""
    try:
        p = outdir / "评分卡.md"
        line = "| 第{}章 | {} | ".format(chapter_no, stage_name) + " | ".join("{}:{}".format(d, scores.get(d, "-")) for d in SCORE_DIMS) + " |"
        if not p.exists():
            head = "# AI 评分卡（每章六维评分，1~5分；六维维度固定，检查要点随阶段）\n\n"
            head += "| 章节 | 阶段 | " + " | ".join(SCORE_DIMS) + " |\n"
            head += "|---|" + "---|" * (len(SCORE_DIMS) + 1) + "\n"
            p.write_text(head + line + "\n", encoding="utf-8")
        else:
            with open(str(p), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

def score_warning(outdir, min_score=2):
    """C2：读评分卡最近两章，任一维度连续<=2 返回警告文本（注入下一轮写章）"""
    try:
        p = outdir / "评分卡.md"
        if not p.exists():
            return ""
        lines = [l for l in open(str(p), encoding="utf-8").read().splitlines() if l.startswith("| 第")]
        recent = lines[-2:]
        if len(recent) < 2:
            return ""
        low = []
        for dim in SCORE_DIMS:
            vals = []
            for line in recent:
                m = re.search(r"{}:(\d)".format(dim), line)
                if m:
                    vals.append(int(m.group(1)))
            if len(vals) == 2 and all(v <= min_score for v in vals):
                low.append("{}（连续{}/{}分）".format(dim, vals[0], vals[1]))
        if low:
            return "AI 连续两章低分：" + "、".join(low) + "。本章必须重点对治（按低分项：推进锚/加现实术语/找主角声音/加结尾钩子/显化设定/加环境氛围）。"
    except Exception:
        pass
    return ""

def build_progress_card(outdir, anchor_state, rhythm, checklist):
    """从正文+状态机+清单自动提取结构化进展卡（零幻觉零污染，正文=唯一事实源）
    输出：章节轨迹（最近8章+时间）/主线位置/节奏计数器/主角状态"""
    parts = []
    try:
        story = read_story_file(outdir)
        chapters = []
        if story:
            for m in re.finditer(r'^##\s+第(\d+)章\s*([^\n]*)', story, re.M):
                ch_no = m.group(1)
                ch_title = m.group(2).strip()
                seg = story[m.end():m.end() + 200]
                dm = re.search(r'第(\d+)[天周月]', seg)
                tm = f"第{dm.group(1)}天" if dm else "时间未标"
                chapters.append(f"第{ch_no}章 {ch_title}（{tm}）")
        if chapters:
            tail = " → ".join(chapters[-8:])
            parts.append(f"【章节轨迹】{tail}")
    except Exception:
        pass
    parts.append(f"【主线位置】锚{anchor_state['current']}（本锚已写{anchor_state['chapters_in_anchor']}章）")
    parts.append(f"【节奏】距上次L2冲击{rhythm['last_L2']}章 / L3/L4{rhythm['last_L3L4']}章 / 冲突对抗{rhythm['last_combat']}章 / 外部冲击{rhythm['last_impact']}章")
    try:
        states = [l.strip() for l in checklist.split("\n") if l.strip().startswith("- 【状态】")]
        if states:
            parts.append("【主角状态】" + states[-1].lstrip("- 【状态】").strip())
    except Exception:
        pass
    return "\n".join(parts)

def read_progress_card(outdir):
    """P11：读取故事进展.md（截至最新章的状态卡），无则返回空"""
    try:
        p = outdir / "故事进展.md"
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            # 只取最新状态卡（第一行【截至第X章】+ 首段）
            if txt:
                lines = txt.split("\n")
                head = [l for l in lines[:6] if l.strip()]
                return "\n".join(head)[:800]
    except Exception:
        pass
    return ""

