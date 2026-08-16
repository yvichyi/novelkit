#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""《介质》正史写作脚本（第3章起批量写 · 分层大纲 · 三层开关 · 后期切 pro）

三层开关（默认最保守 = preview + confirm + write 1）：
  --preview    只打印 prompt，不发 API、不写文件（零成本预览）
  --confirm    每章写完后暂停，等你确认才落盘正文
  --write N    一次只写 N 章就停（默认 1）

后期策略：
  --pro-from N 第 N 章起用 deepseek-v4-pro（默认 181=分裂期起），之前用 flash

缓存设计（防重复计费）：
  每章用「干净上下文」= [system persona 固定] + [user 完整 prompt]。
  user 前缀 = 世界观精简版 → 分层大纲 → 正史卡 → 已写前文（累积递增）。
  前文只增不删 → 缓存前缀稳定 → 命中率随连载升高。

数据落盘：01_正史账本/usage.jsonl（命中/未命中/输出/费用，写作仪表盘自动读）

用法：
  python write_chapter.py --preview                  # 预览下一章 prompt
  python write_chapter.py --write 1 --confirm        # 写1章，写后确认
  python write_chapter.py --write 3                  # 连写3章
  python write_chapter.py --write 1 --key sk-xxx     # 指定 key

写新书（通用模式）：
  python new_novel.py                                # 傻瓜式建书（交互问答 + 可选 AI 生成设定）
  NOVEL_DIR=books/新书名 python write_chapter.py --write 1 --confirm   # 开写新书
（NOVEL_DIR 指向新书目录；新书配置放 <新书目录>/novel_config/，引擎从这里读，换书=换目录）
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ⚠️ 时序关键：NOVEL_CONFIG_DIR 必须在 import ai_bridge_api（→ mediakit.config）之前设置，
# 因为 config 模块在 import 顶层就读它（模块级常量）。NOVEL_DIR 相对路径以本脚本目录为基准。
_NOVEL_DIR_ENV = os.environ.get("NOVEL_DIR", "")
if _NOVEL_DIR_ENV:
    _nd0 = Path(_NOVEL_DIR_ENV)
    if not _nd0.is_absolute():
        _nd0 = SCRIPT_DIR / _nd0
    _nd0 = _nd0.resolve()
    if not os.environ.get("NOVEL_CONFIG_DIR"):
        os.environ["NOVEL_CONFIG_DIR"] = str(_nd0 / "novel_config")

sys.path.insert(0, str(SCRIPT_DIR))
import ai_bridge_api as m

# Windows 控制台 GBK 编码兼容：手机端是 UTF-8 终端没问题，电脑端强制 stdout/stderr 走 UTF-8，
# 否则 print 中文/emoji/上标字符（如 m³、℃）会抛 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent          # 默认《介质》数据根（ledger/已发布正文 所在）
# 双模式 BASE 修正（通用仓库布局）：本目录和父目录都没有《介质》数据时，工作根收敛到本目录，
# 避免 clone 到任意位置后误读别人的 01_正史账本/已发布正文
if not (BASE / "已发布正文").exists() and not (Path(__file__).resolve().parent / "已发布正文").exists():
    BASE = Path(__file__).resolve().parent
# 通用模式：NOVEL_DIR 指向新书目录（含 novel_config/ 和运行时目录）。
# 不设置 = 走《介质》默认路径（零影响）。
NOVEL_DIR = os.environ.get("NOVEL_DIR", "")
if NOVEL_DIR:
    _nd = Path(NOVEL_DIR)
    if not _nd.is_absolute():
        _nd = SCRIPT_DIR / _nd
    NOVEL_DIR = str(_nd.resolve())
    LEDGER = _nd / "ledger"                             # 新书账本
    PUBLISHED = _nd / "已发布正文"                       # 新书正文
    REVIEW_DIR = LEDGER / "评审反馈"
    USAGE_JSONL = LEDGER / "usage.jsonl"
    # 新书配置目录（NOVEL_CONFIG_DIR 已在 import 前由环境变量指定，这里仅防御性兜底）
    if not os.environ.get("NOVEL_CONFIG_DIR"):
        os.environ["NOVEL_CONFIG_DIR"] = str(_nd / "novel_config")
else:
    LEDGER = BASE / "01_正史账本"
    PUBLISHED = BASE / "已发布正文"
    REVIEW_DIR = LEDGER / "评审反馈"           # A1 评审闭环：每章写完后 flash 快速评审，下章注入整改清单
    USAGE_JSONL = LEDGER / "usage.jsonl"
FUSED_DIR = (Path(NOVEL_DIR) / "已发布正文_熔断待修") if NOVEL_DIR else (BASE / "已发布正文_熔断待修")  # 熔断正文不丢弃，存这里供手动修复/复用

# 《介质》模式判定：显式 NOVEL_DIR=新书（通用模式）；无 NOVEL_DIR 时只有检测到《介质》数据文件才算《介质》模式。
# 通用仓库（clone 后无《介质》数据）→ 即使不设 NOVEL_DIR 也走通用模板，不会泄漏柯德/熵债等《介质》概念。
IS_MEDIUM_MODE = (not NOVEL_DIR) and (LEDGER / "世界观核心设定·精简版.md").exists()

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
PRO_FROM_DEFAULT = 181   # 分裂期起切 pro
RECENT_FULL = 8          # 前文滚动窗口：只保留最近 N 章全文，更早的靠正史卡（状态卡+事件表+伏笔账本）兜底，防 prompt 无限膨胀


def load_env_key():
    """从项目根 .env 读 DEEPSEEK_API_KEY（傻瓜模式：Key 只填一次）"""
    env_p = BASE / ".env"
    if env_p.exists():
        try:
            for line in env_p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None


def save_env_key(key):
    """把 Key 存到项目根 .env（首次运行时交互询问）"""
    try:
        (BASE / ".env").write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
        return True
    except Exception as e:
        print(f"⚠️ Key 保存失败（不影响本次使用）：{e}")
        return False


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def idea_library():
    """①b 脑洞库·哇点原料（固定前缀，改脑洞库即自动同步）"""
    if NOVEL_DIR:
        # 通用模式：从 novel_config 读 world_idea.md（可选）
        t = read_text(Path(NOVEL_DIR) / "novel_config" / "world_idea.md").strip()
        return f"【脑洞库·哇点原料（每章至少一个「哇点」从这里挑或自创，套进剧情：谁做的/怎么做的/为什么讲得通/代价是什么）】\n{t}" if t else ""
    t = read_text(BASE / "脑洞库.md").strip()
    return f"【脑洞库·哇点原料（最大看点=现实被各种各样的方法改写；知识人人可学，须剧情获取，派系专精≠排他）】\n{t}" if t else ""


def red_line_table():
    """①c 红线对照表（三问判据 + 被否反面教材）"""
    if NOVEL_DIR:
        t = read_text(Path(NOVEL_DIR) / "novel_config" / "redline_table.md").strip()
        return f"【红线对照表（写脑洞前先过三问：尺度对不对/踩没踩红线/付不付得起代价）】\n{t}" if t else ""
    t = read_text(BASE / "红线对照表.md").strip()
    return f"【红线对照表（写脑洞前先过三问：尺度对不对/踩没踩红线/付不付得起债）】\n{t}" if t else ""


def refresh_dashboard():
    """每落盘一章自动重生成仪表盘（手机浏览器 30 秒自动刷新即可看到最新进度）。
    通用模式（NOVEL_DIR）没有《介质》报告目录，跳过刷新。"""
    if NOVEL_DIR:
        return
    try:
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "gen_dashboard.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, cwd=str(SCRIPT_DIR))
        ok = ("总览已更新" in (r.stdout or "")) or ("仪表盘已生成" in (r.stdout or ""))
        print("📊 仪表盘已自动更新" if ok else f"⚠️ 仪表盘更新异常：{(r.stdout or r.stderr or '').strip()[:80]}")
    except Exception as e:
        print(f"⚠️ 仪表盘自动更新失败（不影响正文落盘）：{e}")


def list_chapters():
    """已发布正文 [(章号, 正文)]，按章号升序"""
    out = []
    for p in PUBLISHED.glob("第*章.md"):
        mo = re.search(r"第(\d+)章", p.name)
        if mo:
            out.append((int(mo.group(1)), read_text(p).strip()))
    return sorted(out)


def next_chapter_no():
    chs = list_chapters()
    return (max(c for c, _ in chs) + 1) if chs else 1


def progress_line():
    """傻瓜模式的进度一行：已发布N章 ｜ 当前时代 ｜ 下一章"""
    chs = list_chapters()
    n = next_chapter_no()
    try:
        from ai_bridge_api import stage_of_chapter
        stage, _, _ = stage_of_chapter(n)
    except Exception:
        stage = "?"
    stage_zh = {"混沌期": "混沌", "清理期": "清理", "分裂期": "分裂", "共存期": "共存", "终局": "终局"}.get(stage, stage)
    return f"已发布 {len(chs)} 章 ｜ 当前时代·{stage_zh} ｜ 下一章 = 第{n}章"


def world_prefix():
    """① 世界观核心设定·精简版（固定前缀，永不重排）
    通用模式：优先 novel_config/world_setting.md，退回 ledger/ 下的精简版。"""
    if NOVEL_DIR:
        t = read_text(Path(NOVEL_DIR) / "novel_config" / "world_setting.md").strip()
        if t:
            return t
    return read_text(LEDGER / "世界观核心设定·精简版.md").strip()


def ledger_cards():
    """③ 五张正史卡（当前态）"""
    parts = []
    for name in ["正史状态卡", "剧情推演卡", "大纲审查卡", "章节事件表", "伏笔账本"]:
        t = read_text(LEDGER / f"{name}.md").strip()
        if t:
            parts.append(f"【{name}】\n{t}")
    return "\n\n".join(parts)


def faction_card(ch_no):
    """⑩·五 势力/派系设定（阶段门禁：早期阶段不注入防泄露后期概念）
    通用模式：novel_config/faction.md；《介质》模式：ledger/九派系设定.md"""
    stage, _, _ = m.stage_of_chapter(ch_no)
    if stage in ("混沌期", "清理期"):
        return ""
    if NOVEL_DIR:
        t = read_text(Path(NOVEL_DIR) / "novel_config" / "faction.md").strip()
    else:
        t = read_text(LEDGER / "九派系设定.md")
    label = "当前阶段起可用" if stage == "全篇" else f"{stage}起可用"
    return f"【势力/派系设定（{label}）】\n{t}" if t else ""


def load_chapter_summaries(limit_chapters=None):
    """读 01_正史账本/章节事件表.md，提取每章不可逆事件摘要（B1 混合前文的累积摘要层）。

    只提取「第N章 → 事件摘要」映射，供 prev_context 拼接。
    缺摘要的章用正文标题兜底（返回 None 让调用方处理）。
    返回 {章号: 摘要文本}
    """
    ep = LEDGER / "章节事件表.md"
    if not ep.exists():
        return {}
    try:
        text = ep.read_text(encoding="utf-8")
    except Exception:
        return {}
    out = {}
    # 按 ## 第N章 分段
    sections = re.split(r"(?m)^##\s+第(\d+)章", text)
    # sections[0] 是头部，之后成对：章号、内容
    for i in range(1, len(sections) - 1, 2):
        ch_no = int(sections[i])
        if limit_chapters is not None and ch_no > limit_chapters:
            continue
        body = sections[i + 1]
        # 提取第一个不可逆事件（1. 后面的内容）
        m = re.search(r"1\.\s*(.+)", body)
        if m:
            out[ch_no] = m.group(1).strip()
    return out


def prev_context():
    """⑧ 已写前文（B1 混合模式）：
       [累积摘要层]（第1~N-RECENT_FULL 章，只增不删 → 固定缓存前缀，稳定命中）
       + [最近 RECENT_FULL 章全文]（滚动窗口，控制膨胀）

       早期章靠正史卡+事件表兜底，最近章靠全文保证文风衔接。
    """
    chs = list_chapters()
    if not chs:
        return ""
    recent = chs[-RECENT_FULL:] if len(chs) > RECENT_FULL else chs
    head = ""
    if len(chs) > RECENT_FULL:
        skipped = len(chs) - RECENT_FULL   # 被全文省略的章数 = 需要累积摘要的章数
        summaries = load_chapter_summaries(limit_chapters=skipped)
        # 有摘要的章按序拼；没摘要的章用正文标题兜底
        summ_lines = []
        for no, body in chs[:skipped]:
            if no in summaries:
                summ_lines.append(f"第{no}章：{summaries[no]}")
            else:
                # 兜底：正文标题（或开头第一句）
                title = body.splitlines()[0].strip() if body else ""
                title = re.sub(r"^#*\s*第\d+章\s*", "", title).strip()
                summ_lines.append(f"第{no}章：{title or '（内容见正史状态卡）'}")
        head = (f"【早期章节累积摘要（第1~{skipped}章，只增不删；正文已省略，摘要+正史卡完整覆盖不可逆事件/因果链/伏笔）】\n"
                + "\n".join(summ_lines) + "\n\n")
    return head + "\n\n".join(f"（第{no}章）\n{body}" for no, body in recent)


def anchor_guide(ch_no):
    """按章号返回本章应推进的主线锚点提示（防"原地打转"：每章必须落地锚点的至少一步）"""
    if NOVEL_DIR or not IS_MEDIUM_MODE:
        # 通用模式：从 STAGE_ANCHORS + STAGE_DIMS 生成通用锚点提示（不依赖《介质》硬编码）
        try:
            stage, a_lo, a_hi = m.stage_of_chapter(ch_no)
            dims = m.STAGE_DIMS.get(stage, {})
            guide = dims.get("锚推进", "") or ""
            return f"当前阶段「{stage}」（锚{a_lo}~{a_hi}）" + (f"：{guide}" if guide else "：推进本阶段核心目标")
        except Exception:
            return "推进本阶段核心目标"
    if ch_no == 1:
        return "锚1 觉醒四连：第1章内完成四件事（误触发现→拷贝知识→确认共有）。**故事从白天开始**（柯德白天看云，晚上看不清云=物理硬伤）。**柯德还在城市里**（异常期第1天：政府还在/超市还开/不短缺，零星怪事当怪谈，但恐慌刚开始蔓延）。"
    if ch_no <= 3:
        return "异常期（第0~3天）：政府还在、超市还开、零星怪事当怪谈，但恐慌开始蔓延、首批会算者动手。柯德还在城市，亲眼看到「秩序还没崩、但裂痕已现」（有人囤货/传谣/当街试能力/警察开始疲于应付）。**必写「破坏廉价」的萌芽**：会算的人随便崩一堵承重墙、引爆一罐气、触发一场局地风暴——不是大能者，就是懂一点结构的普通人，点计算轻轻松松。**必写「攻防的萌芽」**：第一批人已经发现这能力能打人——有人用热浪/风压抢东西、吓人、报复，也有人本能地借地形/借环境挡一下；人类学得很快，能力正从「怪谈」迅速变成「武器」。"
    if ch_no <= 10:
        return "失控期（第1~4周）：**死亡高峰**——停水断电、暴乱、踩踏、能力失控、城市崩溃（80亿人恐慌，伤亡最惨烈）。柯德还在城市/城市边缘，**亲眼见证城市一步步崩溃**（超市抢空→停水断电→暴乱踩踏→能力者失控伤人→政府/军队/警察失效），在崩溃中求生。**必写**：①文明崩坏（城市/社会/国家怎么崩，不能突然没了）；②末世重量（压迫/紧张/刺激，柯德真的遇险、命悬一线、受伤）；③**破坏廉价=灾难源头**：普通人（懂一点结构/热力/化学/机械）随便崩楼、引爆、触发风暴——点计算人人能改天换地，城市不是被一个大能者毁的，是被无数普通人「推一把临界点」毁的，要写出这种「谁都能，所以谁都拦不住」的窒息感；④能力重量（大能者改河/改城/改气候的震撼，天价债、蝴蝶效应，柯德感受「改天换地」的力量和自己的渺小）。**必写⑤编译攻防爆发·人类学习能力极强**：能力一旦暴露，人类几天内就学会互相攻击（热浪轰脸/风压掀翻/地面裂口/水淹抢物——热力学/流体/结构/声学攻击，熵债落被改物质）和防御（热盾/风墙/结构硬化/借地形躲环境=近零债）。攻防像病毒一样扩散进化：今天有人用热浪抢超市，明天受害者学会用风墙挡，后天整条街都在攻防——不是只有大能者会打，普通人几天就学会，柯德要亲眼看到「谁都能打、谁都在学防」的残酷，也要看到有人用防御护住亲人。最后柯德被迫逃离城市，进入大迁徙。"
    if ch_no <= 15:
        return "失控期（第2~3周·死亡高峰·柯德在城镇/逃难潮中，**禁止独自进山**！）：城镇正在全面崩坏——停水断电、超市抢空、暴乱踩踏、能力者失控伤人、政府/军队/警察失效。柯德在城镇/人群中求生，**每章必须落地一个强看点（硬）**：①绝境求生（断水断粮/被困/受伤命悬一线/逃命）；②编译攻防（被能力者攻击→柯德被迫用知识反击或借力化解，或近距离目睹攻防）；③文明崩坏现场（暴乱/踩踏/抢掠/互害/军警溃散）；④能力重量（大能者改河/改城/改气候的震撼+天价债蝴蝶效应）；⑤人际冲突（抢劫者/骗子/疯狂者/互助者）。柯德的临界点玩法（种雨/冷凝/借力）穿插但短平快，不占整章。**禁止空转章（硬）**：禁止「独自走路→观察神秘现象→推理→绕开→继续走」；地底悬疑线（拖痕/井/铁片/焦红土）只作背景一笔，绝不当主菜。"
    if ch_no <= 20:
        return "失控期（第3~4周·死亡高峰顶点·柯德仍在城镇/逃难潮，**禁止独自进山**！）：城市崩溃到顶点，柯德被卷入密集冲突。**每章一个强看点（硬）**：①编译攻防升级（普通人几天就学会攻击/防御，柯德被迫真正出手反击，藏拙被识破的危机）；②文明崩坏顶点（政府彻底失效/军队溃散/城市成死亡陷阱/暴民互害/踩踏）；③大能者改天换地的震撼（改一条河/改一座城，天价债+下游断水饥荒暴乱的蝴蝶效应）；④柯德的临界点玩法主动化（主动构造临界点解决危机）；⑤遇到不同领域改写者（声学/电磁/材料/化学的人，各用本行改现实）。**禁止空转章（硬）**，禁止独自安静走路。"
    if ch_no <= 30:
        return "失控期末→崩塌期初：城镇彻底崩溃，柯德被迫大迁徙。**迁徙路上必须有密集冲突和看点（硬）**，禁止独自安静走路：①逃难潮/踩踏/互害/抢掠；②路上各种改写者（各领域）和人性光谱（互助/欺骗/抢劫/残暴）；③死地/废墟/命悬一线；④柯德用能力求生+被迫反击；⑤大能者改天换地的余波（下游断水/饥荒/暴乱/城破）。每章一个强看点。地底悬疑线降为背景一笔。"
    if ch_no <= 40:
        return "锚6 第一次出手+被追踪：被迫用编译防御反击（最小干预）+暴露被目击+被追踪+用知识+移动摆脱。**必写**：柯德选最省债的解法而非最强；心理冲击（他不想当英雄，是被迫出手）；出手后意识到「能力=靶子」、加强藏拙；这是混沌期第一场正面编译攻防，要写细节（推演/代价/痕迹）"
    if ch_no <= 65:
        return "锚7 崩塌期：亲历多重崩塌（气候/地壳/社会）+收音机静默+板块前兆与异动（旧地图作废）+绝望孤独感顶峰。**必写**：①文明崩坏（社会层面）——电网/水厂/医院/学校停摆、逃难潮、暴乱、互害、政府/军队/警察解体，通过柯德见闻（废墟/幸存者口述/收音机残响）正面呈现，不能只写大地翻身；②靠知识+临界点+运气活下来的求生戏（断水断粮有生理反应）；③偶遇幸存者交换信息后分开（不组队，绝对独狼）；死地蔓延、大迁徙"
    return "锚8 剧变期+第一束秩序：亲历板块剧变（地裂/山崩/海陆变迁）+气候失控+人口锐减九成完成+见到「有人在收拾烂摊子」（第一束秩序）。**必写**：①末世重量——逃难潮、暴乱、踩踏、灾难、生死一线的压迫/紧张/刺激，柯德真的遇险、受伤、命悬一线，不是游刃有余地解谜；②文明崩坏的正面现场（城市废墟/旧政权最后遗迹/暴民/互害）；③能力的重量——柯德要亲眼见证「大能者改写现实」的震撼（有人改了一条河/一片气候/一座城），感受「能力改变世界」的重量和自己的渺小；④柯德决定继续独狼但记住「秩序在形成」；钩子=清理期（幸存者聚集）；正文时间线须落在第90天前后（约3个月）"


def build_prompt(ch_no):
    """组装第 ch_no 章 prompt：固定层在前（保缓存前缀）、动态层在后（每章变）。

    【永远固定层】（顺序永不重排 = 缓存前缀）：①世界观 ②脑洞库 ③红线表 ④固定few-shot ⑤固定硬规则 ⑥前文（累积递增）
    （固定写作人格 = system persona「星尘」，由 LLMClient 在 user 之前传入，属固定层最前）
    【动态层】（每章变，放尾部不影响前缀缓存）：⑦当前时间 ⑧当前人物 ⑨当前大纲 ⑩当前评分 ⑪当前状态 ⑫上章整改 ⑬本章任务
    """
    idea = idea_library()
    redline = red_line_table()
    parts = [
        world_prefix(),                                    # ① 世界观（固定）
    ]
    if idea:
        parts.append(idea)                                 # ② 脑洞库（固定）
    if redline:
        parts.append(redline)                              # ③ 红线表（固定）
    few = m.build_fewshot_card(ch_no)                      # ④ 固定 few-shot（阶段内固定）
    if few:
        parts.append(few)
    parts.append("【硬设定红线卡（写作必守）】\n" + m.extract_hard_rules(m.INITIAL_CHECKLIST, limit=12))  # ⑤ 固定硬规则
    # ---- 以上【永远固定层】结束 ----
    # 【缓存优化 B1】前文紧接着固定层放（前文=累积递增，只增不删 → 前缀缓存稳定命中）；
    # 每章变化的动态层全挪到前文之后（只 miss 尾部小段，不再拖垮整块前文缓存）。
    prev = prev_context()
    if prev:
        parts.append("【已写前文（唯一正史，必须衔接，禁止重复/矛盾）】\n" + prev)  # ⑥ 前文（固定前缀尾部，累积）
    # ---- 以下【动态层】开始（每章变，放尾部，不影响前缀缓存）----
    parts += [
        m.build_time_card(ch_no),                          # ⑦ 当前时间（内部罗盘，不印正文）
        m.build_character_card(ch_no),                     # ⑧ 当前人物
        m.build_outline_card(ch_no),                       # ⑨ 当前大纲（当前幕）
        m.build_stage_guide(ch_no),                        # ⑩ 当前评分标准
        ledger_cards(),                                    # ⑪ 当前状态（正史卡）
    ]
    fac = faction_card(ch_no)                              # ⑪·五 九派系设定（分裂期起才注入）
    if fac:
        parts.append(fac)
    review_fb = load_last_review(ch_no)                        # ⑪·六 A1 评审闭环：上章整改清单
    if review_fb:
        parts.append(review_fb)
    task = build_task_instruction(ch_no)                       # ⑬ 本章任务（动态层）
    if task:
        parts.append(task)
    return "\n\n".join(parts)


def build_task_instruction(ch_no):
    """本章任务指令：通用模式用通用模板（可被 novel_config/task_template.md 覆盖），《介质》模式用原硬编码。"""
    if NOVEL_DIR or not IS_MEDIUM_MODE:
        # 通用模式：任务模板（可选覆盖）+ 通用写作要求
        tpl = read_text(Path(NOVEL_DIR) / "novel_config" / "task_template.md").strip()
        base = (
            f"【本章任务】你正在写《{Path(NOVEL_DIR).name}》第{ch_no}章。\n"
            f"【本章主线锚点·必须推进】{anchor_guide(ch_no)}。本章必须落地这个锚的至少一步，禁止原地打转（纯观察/纯心理/纯日常循环=硬伤）。\n"
            "严格延续前文推进，不能重复、不能矛盾。要求：\n"
            f"1. 输出正文的第一行必须是「第{ch_no}章」加副标题，标题独占一行，紧接着空一行再写正文；\n"
            "2. 时间推进用事件/光线/身体状态等暗示标记，不机械堆砌日期；且必须严格延续上一章结尾的时间点，绝不倒退；\n"
            "3. 主角保持他的人设与声音（情绪/吐槽/好奇自然流露，别写成冷静的机器）；\n"
            "4. 每章至少一处「哇点」：让一个设定/能力/玩法以有趣的方式起作用（谁做的/怎么做的/为什么讲得通/代价是什么）；\n"
            "5. 至少1个核心设定在剧情里起作用；\n"
            "6. 结尾留钩子。\n"
            "7. 【禁止空转章·硬】本章必须有「事件推进」：发生了一件事，主角为它付出行动、做出选择或改变处境；每章至少一个「结果落地」。\n"
            "直接输出纯小说正文（1500~3000字），不输出任何说明。"
        )
        if tpl:
            # 模板优先：作者给了 task_template.md 就用它（{ch_no}/{anchor_guide} 会被替换）
            return tpl.replace("{ch_no}", str(ch_no)).replace("{anchor_guide}", anchor_guide(ch_no))
        return base
    return (
        f"【本章任务】你正在写《介质》第{ch_no}章。\n"
        f"【本章主线锚点·必须推进】{anchor_guide(ch_no)}。本章必须落地这个锚的至少一步，禁止原地打转（纯观察/纯心理/纯日常循环=硬伤）。\n"
        "严格延续前文推进，不能重复、不能矛盾。要求：\n"
        f"1. 输出正文的第一行必须是「第{ch_no}章」加副标题（如「第3章 溪边的暗号」），标题独占一行，紧接着空一行再写正文；\n"
        "2. 时间推进用事件/光线/身体状态等暗示标记（如「第二天」「等他醒来」「日头偏西」），**禁止机械堆砌「异常期第X天/第X周」**；且必须严格延续上一章结尾的时间点，绝不倒退；\n"
        "3. 柯德是他本人（情绪化的探索者）：情绪、吐槽、惊叹是自然流露的底色，不是每章的规定动作——不要求每章都有口头禅/「哇」/吐槽，一章没有完全正常。但**在关键节点让他露一点人味**（第一次做成某件事/发现新玩法/世界突然离谱）：他会「卧槽，真的可以？」地兴奋、会有点想笑、会嘴贫吐槽、会好奇想再试一次——世界要完蛋了他当然怕，但「这也太酷了吧」和「要出大事了」是同时存在的。**他其实有点喜欢现实坏掉之后的样子**——不是喜欢灾难，是喜欢「现实突然充满可能性」；发现离谱玩法时，他会有一秒「这也太酷了」，甚至看着成果笑出来、忘了自己还在末世，然后才想起要逃命。**别把他写成太懂事、太冷静、太有效率的灾难求生工程师**；也别为凑人设硬塞情绪，只要「像他本人」就行（坦然接受末世又不麻木、会兴奋上头想上手试、也会冷静算账）。不写成沉默观测者或热血英雄；\n"
        "4. 【爽点/哇点·每章至少一处·硬】从上方【脑洞库】挑一个脑洞当本章「哇点」——柯德本命是气象/流体/热力（A组/F组他直接用），但**世界里的其他改写者用各自领域**（声学/电磁/材料/化学/生物/结构/光学/摩擦），柯德遇到时惊叹+琢磨+偷学（B/C/D/E/X组他要先有剧情获取知识才能用）。**能力 = 「一个人如何理解现实」，不是元素分类**——不同的人理解现实的方式不同，就有不同的玩法；禁止连续多章只在「水/气象/湿度/温度」打转，要主动让不同领域的改写出现。套进剧情（谁改的/怎么改的/为什么讲得通/代价/失效边界），禁止裸用、禁止凭空自编没物理解释的脑洞、禁止旁白甩说明书。**「发明」比「执行」重要**：本章的哇点可以是脑洞库现成的，也可以是柯德/其他角色主动发明的新玩法——发现自然规律→反过来用→「那这个现实是不是还可以这么玩？」（第11章过冷水、第15章推雾就是这个模式）。柯德式原创巧思的精神：柯德的强不在「改得多猛」，在「用最便宜的物理撬动最大局面」「脑子转得比别人快半圈」——别人为水拼命他冷凝出水、别人乱改被反噬他算好账付最小代价；禁止整章只有苟活/被动躲藏/挨标记；前文累积后，优先**组合已有玩法**（把前面出现过的装置/规律两两拼成新玩法，A+B→C），而不是每次都发明全新装置；\n"
        "5. 至少1个核心设定在剧情里起作用（用能力改写现实要付'利息'落环境 / 知识边界 / 质量守恒）；\n"
        "   ⚠️【禁词铁律·混沌期·写错即熔断重写】正文严禁出现这些后期术语：介质、熵债、编译、叠加场、派系、复制人、锁定、巨构、元规则、防火墙、量子信道、脑机接口、算力外挂、悬空城、模因、锁杀、黎明派、环境神、负熵流配额、自我编译、编译工程战。柯德嘴里只能用他自己的叫法：算、改、结账、利息、推演（如「改空气」「算这笔账」「推演一下」「改东西要付账」）；柯德本命编程/气象词可用：root/API/系统盘/进程/临界点(对流临界/过冷)/露点/潜热/凝结核/伯努利/卡诺/比热容。这些后期词柯德这个时代根本不知道，谁写谁熔断（特别注意：**「编译」这个词完全避免**——柯德混沌期根本不知道它，即使指「代码编译」也不行，改用「跑代码/运行/构建/执行」；**「介质」也完全避免**——即使指物理的「传声介质/传播介质」也不行，改用「媒质/传声的东西」）；\n"
        "6. 现实术语≥1处（定律名嵌进感官/动作）；\n"
        "7. 末世氛围+生理基线一笔；\n"
        "8. 结尾留钩子。\n"
        "9. 【禁止空转章·硬】本章必须有「事件推进」：发生了一件事（冲突/危机/攻防/求生/见证震撼/人际交锋），柯德为它付出行动、做出选择或改变处境。禁止「柯德独自走路→观察异常→在心里推理→绕开→继续走」的原地打转（=硬伤）；禁止整章「柯德当观众看别人改现实」；每章至少一个「结果落地」（得到什么/失去什么/被迫做了什么/处境变了）。\n"
        "直接输出纯小说正文（1500~3000字），不输出任何说明。"
    )


def score_quality(text):
    """体检：只答"对/错"（有没有病），不答"好/不好"。返回 checks 字典（人称/元话语/末世有人）。"""
    checks = {}
    # 人称纪律（叙述区禁"我"，台词受保护）
    nar = re.sub(r"[“「『].*?[”」』]", "", text, flags=re.S)
    wo = len(re.findall(r"(?<![自本忘无你我])我", nar))
    checks["人称"] = "FAIL" if wo > 3 else ("WARNING" if wo > 0 else "PASS")
    # 元话语（创作过程字眼）
    meta = re.findall(r"老K|清单第|评审|写作思路|创作说明|修改如下|待续|本章完|（示例|例如", text)
    checks["元话语"] = "FAIL" if meta else "PASS"
    # 末世有人（每章至少一处「人的痕迹」）
    people = re.findall(r"逃难|尸体|尸骸|遗骸|失控者|幸存者|人声|广播|尖叫|火光|炊烟|人潮|人群|路人|小孩|老人|妇女|有人|遗物|血迹", text)
    checks["末世有人"] = "PASS" if people else "WARNING"
    return checks


def sanitize_person(text):
    """人称消毒：叙述区"我"→"他"，台词区（“…”）内受保护原样保留（修复人称漂移）。"""
    def _replace_wo(s):
        return re.sub(r'(?<![自本忘无你我])我', '他', s)
    out = []
    last = 0
    for mm in re.finditer(r'[“「『][^”」』]*[”」』]', text):
        out.append(_replace_wo(text[last:mm.start()]))
        out.append(mm.group(0))
        last = mm.end()
    out.append(_replace_wo(text[last:]))
    return ''.join(out)


def check_chapter(text, ch_no):
    """完整门禁：标题/字数/时间/后期词（语境豁免）/体检/硬核锚点。返回 (fatal, tips)"""
    fatal, tips = [], []
    # 标题（软提示，不熔断——落盘时自动补；容忍 AI 的 markdown # 前缀）
    if not re.match(r"#*\s*第\s*%d\s*章" % ch_no, text.strip().split("\n")[0]):
        tips.append("标题缺失（落盘时自动补「第%d章」）" % ch_no)
    # 字数
    chars = len(re.sub(r"\s", "", text))
    if chars < 1200:
        tips.append(f"字数偏少 {chars} 字（<1200）")
    elif chars > 3500:
        tips.append(f"字数偏多 {chars} 字（>3500）")
    # 时间推进（软提示，不熔断）：按事件分章，用暗示标记，不机械写"第X天"
    head = text[:400]
    if re.search(r"异常期第\s*[0-9一二三四五六七八九十]+\s*天|失控期第\s*[0-9一二三四五六七八九十]+\s*周", head):
        tips.append("时间标注偏机械（写了'第X天'），建议改用事件/光线/身体暗示标记")
    # 后期词黑名单（带语境豁免）
    stage, _, _ = m.stage_of_chapter(ch_no)
    black = m.LATE_STAGE_BLACKLIST.get(stage, [])
    hits = []
    for b in black:
        if b not in text:
            continue
        # 口语豁免：锁定→锁门/上锁 不算术语；断点/防火墙→编程语境豁免
        if b == "锁定" and re.search(r"锁上门|锁好|锁死|锁链|门锁|上锁|锁住", text):
            continue
        if b == "断点" and re.search(r"断点续传|设置断点|调试", text):
            continue
        if b == "防火墙" and re.search(r"防火墙规则|防火墙策略", text):
            continue
        hits.append(b)
    if hits:
        fatal.append(f"后期词泄露（{stage}禁词）：{'、'.join(hits[:6])}")
    # 体检
    checks = score_quality(text)
    for k, v in checks.items():
        if v == "FAIL":
            fatal.append(f"体检FAIL·{k}")
        elif v == "WARNING":
            tips.append(f"体检WARNING·{k}")
    # 硬核锚点（混沌期只查现实物理词+柯德本命词，后期才查后期术语）
    if stage == "混沌期":
        anchors = re.findall(r"推演|潜热|凝结核|露点|比热容|热力学|纳维|麦克斯韦|伯努利|卡诺|相变|压力梯度|凝结|蒸发|临界点|过冷|对流", text)
    else:
        anchors = re.findall(r"熵债|编译|推演|叠加场|临界点|潜热|凝结核|露点|比热容|热力学|纳维|麦克斯韦|伯努利|卡诺|作用范围|排放点|相变|压力梯度", text)
    if not anchors:
        tips.append("硬核锚点缺失（无物理/推演术语，柯德没用知识做事的痕迹）")
    return fatal, tips


def usage_dict(client, model_id):
    u = getattr(client, "last_usage", None) or {}
    hit = u.get("prompt_cache_hit_tokens", 0) or 0
    miss = u.get("prompt_cache_miss_tokens", 0) or 0
    out = u.get("completion_tokens", 0) or 0
    p = m.DEEPSEEK_PRICE.get(model_id)
    cost = 0.0
    if p:
        cost = (hit * p["cache_hit"] + miss * p["cache_miss"] + out * p["output"]) / 1_000_000
    return {"cache_hit": hit, "cache_miss": miss, "output": out, "cost": cost}


def append_usage(ch_no, model_id, u):
    rec = {"ch": ch_no, "model": model_id, "usage": u, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(USAGE_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def review_chapter(ch_no, text, key):
    """A1+A2 评审闭环：落盘后用 flash 快速评审刚写的章，产出：
      A1 【下章改进清单】→ 下一章 prompt 注入整改（连载越写越好）
      A2 【事件】【伏笔】→ update_ledger() 自动更新正史账本（章节事件表/伏笔账本）
    评审失败不阻塞正文（try/except 兜底）。
    """
    try:
        client = m.LLMClient("章评审", "kimi", m.C_KIMI, BASE_URL, key, "deepseek-v4-flash",
                             m.KIMI_PERSONA, max_history=0)
        stage, _, _ = m.stage_of_chapter(ch_no)
        prompt = (
            f"这是刚写好的第{ch_no}章（{stage}时代）：\n\n{text}\n\n"
            + f"【本阶段评分标准（评审按此六维）】\n{m.build_stage_guide(ch_no)}\n\n"
            + "你作为主编「老K」评审这章。只输出固定格式（缺一不可）：\n"
            + "【本章亮点】1~2条，每条30字内\n"
            + "【下章改进清单】3~5条，每条一句话、具体可执行（针对下一章的改进，不是总结本章），"
            + "每条以'- '开头，示例：'- 对话偏平，下章用动作+潜台词替代直白问答'\n"
            + "【事件】本章不可逆事件或关键推进，一句话20~40字（供登记章节事件表；无重大事件写'无'）\n"
            + "【伏笔】本章新埋设的伏笔（写：名称｜级别A/B/C｜预计回收阶段），或已回收的伏笔（写：回收｜名称）；无则写'无'\n"
            + "禁止输出其他内容。"
        )
        reply = await client.chat(prompt, temperature=0.3, max_tokens=800)
        # 落盘评审反馈（下一章读取）
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        (REVIEW_DIR / f"第{ch_no}章.md").write_text(reply, encoding="utf-8")
        # A2 自动更新正史账本
        try:
            update_ledger(ch_no, reply)
        except Exception as e:
            print(f"⚠️ 账本自动更新失败（不影响正文）：{e}")
        # 评审成本记账（ch 标注 review:，与正史章区分）
        try:
            u = usage_dict(client, "deepseek-v4-flash")
            append_usage(f"review:{ch_no}", "deepseek-v4-flash", u)
        except Exception:
            pass
        return reply
    except Exception as e:
        print(f"⚠️ 章节评审失败（不影响正文）：{e}")
        return ""


def update_ledger(ch_no, review_text):
    """A2：从评审【事件】【伏笔】区块自动更新 01_正史账本/。

    章节事件表.md：追加「## 第N章」+ 不可逆事件 + 因果链
    伏笔账本.md：追加埋设/回收记录
    保持与人工登记格式一致。
    """
    from ai_bridge_api import extract_event
    # ---- 事件 → 章节事件表 ----
    ev = extract_event(review_text)
    if ev and ev != "无":
        ep = LEDGER / "章节事件表.md"
        title = ""
        try:
            chs = list_chapters()
            for no, body in chs:
                if no == ch_no:
                    first = body.splitlines()[0] if body else ""
                    title = re.sub(r"^#*\s*第\d+章\s*", "", first).strip()[:20]
                    break
        except Exception:
            pass
        block = f"\n## 第{ch_no}章{('《' + title + '》') if title else ''}\n\n  - **不可逆事件**：\n    1. {ev}\n"
        with open(ep, "a", encoding="utf-8") as f:
            f.write(block)
        print(f"📌 已登记章节事件表：第{ch_no}章 - {ev[:40]}")
    # ---- 伏笔 → 伏笔账本 ----
    vm = re.search(r"【伏笔】([\s\S]*?)(?=\n【|$)", review_text)
    if vm:
        lines = [ln.strip() for ln in vm.group(1).splitlines() if ln.strip() and ln.strip() != "无"]
        if lines:
            vp = LEDGER / "伏笔账本.md"
            vp_text = vp.read_text(encoding="utf-8") if vp.exists() else ""
            entries = []
            for ln in lines:
                ln2 = ln[2:].strip() if ln.startswith("-") else ln  # 去掉 AI 可能带的 "- " 前缀
                if ln2.startswith("回收"):
                    # 回收｜名称
                    nm = ln2.split("｜", 1)[-1].split("|", 1)[-1].strip()
                    entries.append(f"  - 已回收：{nm}")
                else:
                    # 名称｜级别｜阶段
                    parts = re.split(r"[｜|]", ln2)
                    nm = parts[0].strip()
                    rest = "·".join(p.strip() for p in parts[1:] if p.strip())
                    entries.append(f"  - {nm}（{rest}）" if rest else f"  - {nm}")
            anchor = "## 回收记录"
            insert = f"\n## 第{ch_no}章 伏笔登记\n" + "\n".join(entries) + "\n"
            if anchor in vp_text:
                vp_text = vp_text.replace(anchor, insert + "\n" + anchor, 1)
            else:
                vp_text += insert
            vp.write_text(vp_text, encoding="utf-8")
            print(f"📌 已登记伏笔账本：第{ch_no}章 {len(entries)} 条")


def load_last_review(ch_no):
    """读上一章评审反馈的【下章改进清单】，注入本章 prompt（A1 闭环）"""
    if ch_no <= 1:
        return ""
    p = REVIEW_DIR / f"第{ch_no - 1}章.md"
    if not p.exists():
        return ""
    try:
        t = p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    _m = re.search(r"【下章改进清单】([\s\S]*)", t)
    if not _m:
        return ""
    items = [ln.strip() for ln in _m.group(1).splitlines() if ln.strip().startswith("-")]
    if not items:
        return ""
    return ("【上章评审整改清单（本章必须逐条回应，通过剧情自然改进，不推翻已发布内容）】\n"
            + "\n".join(items[:5]))


def pick_model(ch_no, pro_from):
    return "deepseek-v4-pro" if ch_no >= pro_from else "deepseek-v4-flash"


async def polish_chapter(ch_no, draft, key, model_id):
    """B2 草稿-润色：用 pro 润色 flash 草稿（不改变剧情/设定，只提升文笔/节奏/氛围/对话自然度）。

    返回润色后的正文；失败返回原草稿（不阻塞）。
    """
    try:
        client = m.LLMClient("润色", "qwen", m.C_QWEN, BASE_URL, key, model_id, m.QWEN_PERSONA, max_history=0)
        stage, _, _ = m.stage_of_chapter(ch_no)
        book_name = Path(NOVEL_DIR).name if NOVEL_DIR else "介质"
        stage_label = "" if stage == "全篇" else f"（{stage}时代）"
        voice_line = ("保持主角「情绪化的探索者」声音（会惊叹会吐槽会算账，不写成冷静机器）"
                      if NOVEL_DIR and stage != "全篇" else
                      "保持主角的人设声音（情绪/吐槽/好奇自然流露，别写成冷静的机器）")
        prompt = (
            f"你是《{book_name}》的资深文字编辑，润色作者刚写的第{ch_no}章草稿{stage_label}。\n\n"
            f"【草稿】\n{draft}\n\n"
            "【润色要求·铁律】\n"
            "1. 只提升文字质量：语言质感、句子节奏、对话自然度、氛围渲染、细节生动性；\n"
            "2. 绝不改变剧情走向、不新增情节、不删除关键信息、不改变任何设定事实；\n"
            f"3. {voice_line}；\n"
            "4. 保持第一行标题「第X章」不变，正文1500~3000字；\n"
            "5. 直接输出润色后的纯小说正文，不要任何说明、不要复述草稿。"
        )
        polished = await client.chat(prompt, temperature=0.6, max_tokens=m.MAX_TOKENS)
        # 润色成本记账（ch 标注 polish:N，与正史章区分）
        try:
            u = usage_dict(client, model_id)
            append_usage(f"polish:{ch_no}", model_id, u)
        except Exception:
            pass
        # 润色后门禁复查：不合格则回退草稿（pro 不该引入禁词，但防万一）
        fatal, _ = check_chapter(polished, ch_no)
        if fatal:
            print(f"⚠️ 润色后门禁未过（{fatal[0][:40]}…），回退用原草稿")
            return draft
        return polished
    except Exception as e:
        print(f"⚠️ 润色失败（不影响正文）：{e}")
        return draft


async def write_one_chapter(ch_no, args, key, model_id):
    """写一章，返回状态：ok=落盘 / fused=熔断待修 / retry=重写 / skipped=跳过 / failed=生成失败 / preview"""
    prompt = build_prompt(ch_no)
    print(f"\n{'='*64}\n第{ch_no}章 ｜ 模型={model_id} ｜ prompt {len(prompt)}字符\n{'='*64}")
    if args.preview:
        print(prompt)
        print("\n[预览结束] 未发 API、未写文件。")
        return "preview"

    client = m.LLMClient("正史写作", "qwen", m.C_QWEN, BASE_URL, key, model_id, m.QWEN_PERSONA, max_history=0)
    print("⏳ 生成中（思考模型约0.5~2分钟，请勿关闭）…", flush=True)
    t0 = time.time()
    try:
        reply = await client.chat(prompt, temperature=0.8, max_tokens=m.MAX_TOKENS)
    except Exception as e:
        print(f"❌ 生成失败：{e}")
        return "failed"
    dt = time.time() - t0

    fatal, tips = check_chapter(reply, ch_no)
    status = "❌熔断" if fatal else "✅通过"
    chars = len(re.sub(r"\s", "", reply))
    print(f"\n📋 第{ch_no}章 {status}｜{chars}字｜{dt:.0f}s")
    for x in fatal:
        print(f"   🔴 {x[:110]}")
    for x in tips:
        print(f"   🟡 {x[:110]}")

    u = usage_dict(client, model_id)
    append_usage(ch_no, model_id, u)
    total = u["cache_hit"] + u["cache_miss"]
    rate = u["cache_hit"] / total * 100 if total else 0
    print(f"💰 命中率 {rate:.1f}%（命中{u['cache_hit']//1000}K/未{u['cache_miss']//1000}K/出{u['output']//1000}K）｜¥{u['cost']:.4f}")

    if fatal:
        # 熔断正文不丢弃，存到待修目录，供手动修复/复用（省去重新生成的钱）
        FUSED_DIR.mkdir(parents=True, exist_ok=True)
        fused_p = FUSED_DIR / f"第{ch_no}章.md"
        fused_p.write_text(reply, encoding="utf-8")
        print(f"⚠️ 有熔断，正文已存到待修目录（未进正史）：{fused_p}")
        print(f"   熔断原因：{'；'.join(fatal)}")
        print(f"   可手动改词后移到 已发布正文/，或删掉重写。")
        return "fused"

    # ---- B2 草稿-润色：flash 草稿通过门禁后，用 pro 润色提升文字质量 ----
    # 仅当开启 --polish 且当前用 flash（pro 直接写就不必 flash+pro 两遍）
    if getattr(args, "polish", False) and not model_id.endswith("pro"):
        print(f"✨ 草稿通过门禁（{chars}字），pro 润色中…（提升文字质感，不改变剧情）", flush=True)
        t_p = time.time()
        reply = await polish_chapter(ch_no, reply, key, "deepseek-v4-pro")
        print(f"✨ 润色完成（{time.time()-t_p:.0f}s）")

    # 确认环节（--confirm 或傻瓜模式都走这里）：落盘 / 重写 / 丢弃
    if args.confirm:
        while True:
            try:
                ans = input(f"\n✅ 第{ch_no}章已生成，怎么处理？\n  [回车] 落盘保存  [r] 重写  [d] 丢弃跳过\n> ").strip().lower()
            except EOFError:
                ans = ""
            if ans in ("r", "重写"):
                return "retry"
            if ans in ("d", "丢弃", "x"):
                print("已跳过落盘。")
                return "skipped"
            break  # 空/其他 = 落盘

    # 人称消毒：叙述区"我"→"他"（台词区受保护），修复人称漂移
    reply = sanitize_person(reply)
    # 落盘前整理标题：去掉 AI 可能加的 markdown # 前缀；缺标题则自动补
    first_line = reply.strip().split("\n")[0]
    if re.match(r"#*\s*第\s*%d\s*章" % ch_no, first_line):
        reply = re.sub(r"^#+\s*", "", reply, count=1)   # 去掉 ## 前缀
    else:
        reply = f"第{ch_no}章\n\n" + reply
        print(f"🔧 已自动补标题「第{ch_no}章」")
    out_p = PUBLISHED / f"第{ch_no}章.md"
    out_p.write_text(reply, encoding="utf-8")
    print(f"✅ 已落盘：{out_p}")
    refresh_dashboard()
    # A1 评审闭环：落盘后快速评审，生成【下章改进清单】（--no-review 可关）
    if not args.no_review:
        print("🔍 章节评审中（生成下章改进清单，供下一章整改）…", flush=True)
        await review_chapter(ch_no, reply, key)
    return "ok"


async def main():
    ap = argparse.ArgumentParser(description="《介质》正史写作脚本")
    ap.add_argument("--preview", action="store_true", help="只打印 prompt 不发 API")
    ap.add_argument("--confirm", action="store_true", help="每章写完后暂停确认")
    ap.add_argument("--write", type=int, default=1, help="一次写 N 章（默认1）")
    ap.add_argument("--key", default=None, help="DeepSeek API Key（或环境变量 DEEPSEEK_API_KEY / 项目 .env）")
    ap.add_argument("--pro-from", type=int, default=PRO_FROM_DEFAULT, help="第 N 章起切 pro（默认181）")
    ap.add_argument("--start", type=int, default=None, help="起始章号（默认=已发布+1）")
    ap.add_argument("--no-review", action="store_true", help="关闭 A1 章节评审闭环（省评审费）")
    ap.add_argument("--polish", action="store_true", help="B2 草稿-润色：flash 草稿 → pro 润色（提升文字质感，成本约翻倍）")
    args = ap.parse_args()

    # ============ 傻瓜模式：无任何参数 → 交互问答 ============
    smart = len(sys.argv) == 1
    if smart:
        print()
        print("╔══════════════════════════════════════════╗")
        book_name = Path(NOVEL_DIR).name if NOVEL_DIR else "介质"
        print(f"║   《{book_name}》写作助手 · 傻瓜模式        ║")
        print("╚══════════════════════════════════════════╝")
        print(f"📊 {progress_line()}")
        print("    （直接回车 = 写 1 章，最省心）")
        try:
            n = input("\n📝 写几章？[回车=1] ").strip()
            args.write = int(n) if n.isdigit() and int(n) > 0 else 1
        except EOFError:
            args.write = 1
        try:
            c = input("🛡 每章写完后人工确认再落盘？[回车=确认 / n=自动落盘] ").strip().lower()
            args.confirm = c != "n"
        except EOFError:
            args.confirm = True
        try:
            p = input("✨ 开启 pro 润色？（提升文字质感，成本约翻倍）[回车=不开启 / y=开启] ").strip().lower()
            args.polish = (p == "y")
        except EOFError:
            args.polish = False
        print()

    # ============ API Key：--key > 环境变量 > .env > 询问 ============
    key = args.key or os.environ.get("DEEPSEEK_API_KEY", "") or load_env_key() or ""
    if not key and not args.preview:
        if smart:
            print("🔑 第一次使用需要 DeepSeek API Key（platform.deepseek.com 申请，新用户有免费额度）")
            key = input("   粘贴 Key（sk-...）：").strip()
            if not key:
                print("❌ 没有 Key 无法写作。")
                sys.exit(1)
            try:
                save = input("   保存到项目 .env（以后免输）？[回车=保存 / n=不保存] ").strip().lower()
                if save != "n":
                    save_env_key(key)
                    print("   ✅ 已保存，下次不用再输。")
            except EOFError:
                save_env_key(key)
        else:
            print("❌ 缺 API Key：--key sk-xxx 或环境变量 DEEPSEEK_API_KEY 或项目 .env")
            sys.exit(1)

    start = args.start or next_chapter_no()
    ok_cnt = 0
    for i in range(args.write):
        ch_no = start + i
        model_id = pick_model(ch_no, args.pro_from)
        # 支持重写：生成→确认→(重写则再来一轮)
        while True:
            r = await write_one_chapter(ch_no, args, key, model_id)
            if r != "retry":
                break
            print(f"🔄 重写第{ch_no}章…（会再次计费）")
        if r == "ok":
            ok_cnt += 1

    print(f"\n🏁 完成。本次落盘 {ok_cnt} 章。刷新 写作仪表盘.html 查看最新进度。")


if __name__ == "__main__":
    asyncio.run(main())
