#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""new_novel.py —— 傻瓜式新书向导：问答式建一本新书的完整骨架

一本新书 = 一个目录（默认 books/<书名>/），包含：
  novel_config/   书专属配置（世界观/人格/大纲/红线/锚点/评分）—— 引擎从这读
  ledger/         运行时账本（正史卡/评审反馈/usage，自动生成）
  已发布正文/     写好的章节

用法：
  python3 new_novel.py                     # 交互问答（推荐，纯傻瓜）
  python3 new_novel.py --name 书名 --ai     # 直接建书 + AI 辅助生成设定
  python3 new_novel.py --no-ai              # 跳过 AI 辅助（生成模板让你自己填）

AI 辅助模式（--ai）：调用 DeepSeek 生成世界观/脑洞库/红线表/大纲/清单，
一次交互问答 + 几十秒等待 = 一本可直接开写的书。
Key 获取顺序：--key > 环境变量 DEEPSEEK_API_KEY > 项目根 .env > 交互询问。

建好后开写：
  NOVEL_DIR=books/书名 python3 write_chapter.py --preview           # 先看第1章 prompt
  NOVEL_DIR=books/书名 python3 write_chapter.py --write 1 --confirm # 写第1章，人工确认
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 模板目录兼容两种布局：
#   本仓库：引擎在 engine/ 子目录 → 模板在仓库根 templates/novel_config
#   单目录部署：脚本和模板同层 → 模板在 BASE/templates/novel_config
TEMPLATE_DIR = BASE.parent / "templates" / "novel_config"
if not TEMPLATE_DIR.exists():
    TEMPLATE_DIR = BASE / "templates" / "novel_config"
BOOKS_DIR = BASE / "books"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

C_G = "\033[92m"; C_Y = "\033[93m"; C_B = "\033[96m"; C_R = "\033[91m"; C_0 = "\033[0m"


def ask(prompt: str, default: str = "", allow_blank: bool = True) -> str:
    """交互一问：显示默认值，回车用默认。EOF（非交互）→ 默认。"""
    hint = f" [回车={default}]" if default else ""
    try:
        ans = input(f"{prompt}{hint} ").strip()
    except EOFError:
        ans = ""
    return ans if ans else default


def load_key(arg_key: str) -> str:
    """Key 获取：--key > 环境变量 > 项目 .env（脚本目录与父目录都试）> 询问"""
    if arg_key:
        return arg_key
    env = os.environ.get("DEEPSEEK_API_KEY", "")
    if env:
        return env
    for env_p in (BASE / ".env", BASE.parent / ".env"):
        if env_p.exists():
            try:
                for line in env_p.read_text(encoding="utf-8").splitlines():
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    # 无 key：交互式（有 tty）→ 询问；非交互（管道/EOF）→ 明确报错并退出（不静默用空 key 全失败）
    try:
        import sys as _sys
        is_tty = _sys.stdin.isatty()
    except Exception:
        is_tty = False
    if not is_tty:
        print(f"{C_R}❌ 没找到 API Key。请先：echo 'DEEPSEEK_API_KEY=sk-xxx' > .env  （或 --key sk-xxx）{C_0}")
        sys.exit(2)
    print(f"{C_Y}🔑 需要 DeepSeek API Key（platform.deepseek.com 申请，新用户有免费额度）{C_0}")
    key = ask("   粘贴 Key（sk-...）：")
    if not key:
        print(f"{C_R}❌ 没有 Key 无法 AI 辅助生成，将退回纯模板模式。{C_0}")
        return ""
    save = ask("   保存到项目 .env（以后免输）？[回车=保存 / n=不保存]", "y")
    if save != "n":
        try:
            (BASE / ".env").write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
            print(f"{C_G}   ✅ 已保存 {BASE / '.env'}{C_0}")
        except Exception as e:
            print(f"{C_Y}⚠️ 保存失败（不影响本次使用）：{e}{C_0}")
    return key


def ask_stage_plan(total_chapters: int):
    """阶段规划交互：返回 [(阶段名, 章起, 章止, 一句话)]"""
    print(f"\n{C_B}📚 阶段规划（每个阶段 = 小说的一个「部」，写章时按阶段注入评分标准与锚点）{C_0}")
    print("   默认 3 阶段：①开端(1~10章) ②发展(11~25章) ③结局(26~{}章)".format(total_chapters))
    custom = ask("   要自定义吗？[回车=用默认 / 输入阶段数]", "").strip()
    n = int(custom) if custom.isdigit() and int(custom) > 0 else 3

    stages = []
    if n == 3 and not custom:
        defaults = [(1, 10), (11, min(25, total_chapters)), (min(26, total_chapters + 1), total_chapters)]
        names = ["开端", "发展", "结局"]
    else:
        # 每个阶段的起止章都手填（傻瓜式：显示可用区间，回车=自动续排）
        defaults = []
        auto_lo = 1
        for i in range(n):
            if i == n - 1:
                lo = auto_lo
                hi = total_chapters
            else:
                q = ask(f"   阶段{i + 1} 从第几章开始？[回车=自动续排]", str(auto_lo)).strip()
                lo = int(q) if q.isdigit() and 1 <= int(q) <= total_chapters else auto_lo
                hi = ask(f"   阶段{i + 1} 到第几章结束？[回车=下一阶段自动续排]", "").strip()
                hi = int(hi) if hi.isdigit() and lo <= int(hi) <= total_chapters else min(lo + max(1, total_chapters // n) - 1, total_chapters)
            if lo > total_chapters:
                break
            defaults.append((lo, hi))
            auto_lo = hi + 1
        names = [f"阶段{i + 1}" for i in range(len(defaults))]
    for i, (lo, hi) in enumerate(defaults):
        if lo > total_chapters:
            break
        nm = ask(f"   阶段{i + 1}名（第{lo}~{hi}章）", names[i])
        desc = ask(f"     一句话（这个阶段写什么）", "")
        stages.append({"name": nm, "ch_lo": lo, "ch_hi": hi, "desc": desc})
    return stages


def gen_config_files(book_dir: Path, name: str, protagonist: str, stages: list):
    """程序化生成 novel_config/ 里不依赖 AI 的文件（锚点/时间线/评分/任务模板等）。"""
    nc = book_dir / "novel_config"
    # ---- anchors.json：阶段锚点（每个阶段一个锚区间）----
    anchors = {
        "ANCHOR_KEYWORDS": {},
        "STAGE_ANCHORS": [],
        "STAGE_DIMS": {},
        "LATE_STAGE_BLACKLIST": {},
    }
    for i, st in enumerate(stages, 1):
        anchors["STAGE_ANCHORS"].append([st["name"], i, i, st["ch_lo"], st["ch_hi"]])
        dims = {
            "人物与声音": f"主角「{protagonist}」人设稳定、有情绪有声音，不写成冷静机器",
            "情节推进": "本章有事件发生、主角有行动/选择/处境改变，禁止空转章",
            "世界设定": "核心设定在剧情里起作用，不裸用、不旁白甩说明书",
            "文本质感": "语言流畅、节奏好、对话自然、细节生动",
            "冲突张力": "有冲突/危机/看点，结尾留钩子",
            "一致性": "延续前文、不矛盾、不重复、时间只前进不倒退",
        }
        if st["desc"]:
            dims["阶段目标"] = f"本阶段（{st['name']}）核心：{st['desc']}"
        anchors["STAGE_DIMS"][st["name"]] = dims
    (nc / "anchors.json").write_text(json.dumps(anchors, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- chapter_events.md：逐章锚点（AI 建书生成；手工建书给占位模板）----
    # 格式：每行「第N章|关键词1、关键词2|本章必须推进的事件一句话」
    # 引擎在 anchors.json 的 ANCHOR_KEYWORDS 为空时解析此文件回填逐章锚点（写章 prompt 的【主线锚点】用）
    (nc / "chapter_events.md").write_text(
        f"《{name}》逐章锚点表（写章 prompt 的【本章主线锚点】来源）\n"
        "格式：第N章|关键词（2~4个，顿号分隔）|本章必须推进的事件一句话\n"
        "========\n"
        f"{chr(10).join(f'第{c}章| |' for c in range(1, (stages[-1]['ch_hi'] if stages else 30) + 1))}\n",
        encoding="utf-8")

    # ---- timeline.json：人物时间线（主角自始至终可用）----
    timeline = {"CHARACTER_TIMELINE": [[1, stages[0]["name"] if stages else "全篇",
                                        [protagonist] if protagonist else ["主角"], [], []]]}
    (nc / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- scores.json：六维评分维度（评审用）----
    scores = {"SCORE_DIMS": ["人物与声音", "情节推进", "世界设定", "文本质感", "冲突张力", "一致性"]}
    (nc / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- task_template.md：章节任务模板（可选；不写则用引擎内置通用模板）----
    tpl = f"""【本章任务】你正在写《{name}》第{{ch_no}}章。
【本章主线锚点·必须推进】{{anchor_guide}}。本章必须落地这个锚的至少一步，禁止原地打转（纯观察/纯心理/纯日常循环=硬伤）。
严格延续前文推进，不能重复、不能矛盾。要求：
1. 输出正文的第一行必须是「第{{ch_no}}章」加副标题，标题独占一行，紧接着空一行再写正文；
2. 时间推进用事件/光线/身体状态等暗示标记，不机械堆砌日期；且必须严格延续上一章结尾的时间点，绝不倒退；
3. 主角保持他的人设与声音（情绪/吐槽/好奇自然流露，别写成冷静的机器）；
4. 每章至少一处「哇点」：让一个设定/能力/玩法以有趣的方式起作用（谁做的/怎么做的/为什么讲得通/代价是什么）；
5. 至少1个核心设定在剧情里起作用；
6. 结尾留钩子；
7. 【禁止空转章·硬】本章必须有「事件推进」：发生了一件事，主角为它付出行动、做出选择或改变处境；每章至少一个「结果落地」。
直接输出纯小说正文（1500~3000字），不输出任何说明。
"""
    (nc / "task_template.md").write_text(tpl, encoding="utf-8")

    # ---- world_setting.md：世界观精简版（作者或 AI 填；先给占位引导）----
    (nc / "world_setting.md").write_text(
        f"《{name}》核心世界观（精简版，注入每章 prompt 的最前面）\n\n"
        f"一句话简介：\n\n核心设定（3~8 条，每条一句话）：\n- \n- \n",
        encoding="utf-8")

    # ---- 其他 md 占位（AI 模式会被覆盖；手工模式当引导）----
    (nc / "world_idea.md").write_text(
        f"《{name}》脑洞库·哇点原料（每章至少一个「哇点」从这里挑或自创，套进剧情）\n\n"
        "- \n", encoding="utf-8")
    (nc / "redline_table.md").write_text(
        f"《{name}》红线对照表（写脑洞前先过三问：尺度对不对/踩没踩红线/付不付得起代价）\n\n"
        "- \n", encoding="utf-8")
    (nc / "faction.md").write_text(
        f"《{name}》势力/派系设定（可选；有则在对应阶段注入）\n\n"
        "- \n", encoding="utf-8")


# ================= AI 辅助生成 =================

async def ai_generate_files(book_dir: Path, name: str, intro: str, protagonist: str, stages: list, key: str,
                          theme: str = "", style: str = "", ideas: str = ""):
    """用 DeepSeek 并行生成 6 个设定文件；单文件失败 → 保留模板占位，不阻塞建书。

    theme/style/ideas：作者自定义（主题/风格/想塞的脑洞），AI 必须基于它们扩充，禁止另起一套。
    """
    from mediakit.llm import LLMClient
    from mediakit.config import QWEN_PERSONA, DEEPSEEK_PRICE

    stage_brief = "\n".join(f"- {s['name']}（第{s['ch_lo']}~{s['ch_hi']}章）：{s['desc'] or '（未填写）'}" for s in stages)
    # 作者自定义（可空）：有就作为 AI 扩充的硬约束
    custom_brief = ""
    if theme.strip():
        custom_brief += f"\n【作者定的主题/题材（必须围绕，禁止偏离）】{theme.strip()}"
    if style.strip():
        custom_brief += f"\n【作者定的风格/基调（写作时保持）】{style.strip()}"
    if ideas.strip():
        custom_brief += f"\n【作者想塞的脑洞/元素（扩充进世界观与大纲，尽量都用上）】{ideas.strip()}"
    base_ctx = (
        f"小说名：《{name}》\n"
        f"一句话简介：{intro or '（未填写，请合理构思）'}\n"
        f"主角：{protagonist or '（未指定，请起一个合适的名字与性格）'}\n"
        f"阶段规划：\n{stage_brief}\n"
        f"{custom_brief}\n"
    )

    def mk_client():
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        return LLMClient("新书设定", "qwen", "\033[95m", base_url, key,
                         "deepseek-v4-flash", QWEN_PERSONA, max_history=0)

    async def gen(filename, instruction, max_tokens=6000, ctx=None, min_len=300):
        prompt = (ctx or base_ctx) + "\n" + instruction
        try:
            client = mk_client()
            reply = await client.chat(prompt, temperature=0.8, max_tokens=max_tokens)
            text = reply.strip()
            # 长度校验：AI 输出过短（网络中断/截断残留）→ 视为失败，保留占位模板并明确提示
            if len(text) < min_len:
                raise RuntimeError(f"输出过短（{len(text)} 字 < {min_len}），疑似网络中断/截断")
            (book_dir / "novel_config" / filename).write_text(text, encoding="utf-8")
            print(f"{C_G}  ✅ 已生成 {filename}（{len(text)} 字）{C_0}")
            return True
        except Exception as e:
            print(f"{C_R}  ❌ {filename} 生成失败（保留占位模板，可稍后重试或手填）：{str(e)[:100]}{C_0}")
            return False

    # 两阶段建书：先 world_setting 打底（世界观底座），其余文件基于它生成，杜绝并行各写各的世界观分裂。
    # （《限制》踩坑：并行生成时 world_setting=壳/文明止损，outline=旷在/形态坍缩，prompt 自相矛盾 → 正文跑偏）
    ws_ok = await gen(
        "world_setting.md",
        "【任务：世界观精简版】为这部小说写核心世界观精简版（400~900字）："
        "一句话简介 + 3~8 条核心设定（每条一句话、信息密度高）。"
        "这是全书的**世界观底座**，之后所有设定文件（脑洞/红线/大纲/清单/逐章锚点）都会以它为准，"
        "所以必须把故事的核心冲突/机制/独特之处写清楚。直接输出 markdown 正文，不要任何说明、不要文件标题注释。",
    )
    ws_text = ""
    _ws_file = book_dir / "novel_config" / "world_setting.md"
    if _ws_file.exists():
        ws_text = _ws_file.read_text(encoding="utf-8").strip()
    ws_ctx = base_ctx + ("\n【已定世界观底座（所有设定必须与此一致，禁止另起一套）】\n" + ws_text if ws_text else "")

    tasks = [
        gen("topic.md",
            "【任务：世界观全文】输出《" + name + "》的完整世界观设定（2000~4000字）：世界运行的底层规则、"
            "核心设定与机制、力量体系（若有）、社会/文明结构、历史背景、本作独特之处。"
            "**必须严格沿用上面【已定世界观底座】的核心设定，只展开细节，禁止另起一套世界观。**"
            "直接输出 markdown 正文，不要任何说明、不要文件标题注释。",
            ctx=ws_ctx),
        gen("world_idea.md",
            "【任务：脑洞库】为这部小说构思 12~20 个「哇点」创意（每个 1~3 句）：本作设定下"
            "可以发生的最有趣/最震撼/最反直觉的场景或玩法。要求可套进剧情（谁做的/怎么做的/为什么讲得通/代价是什么），"
            "**必须基于上面【已定世界观底座】的机制衍生，禁止发明底座没有的新设定。**"
            "直接输出 markdown 列表，不要任何说明。",
            ctx=ws_ctx),
        gen("redline_table.md",
            "【任务：红线对照表】为这部小说写写作红线（300~800字）：三问判据（尺度对不对/踩没踩红线/付不付得起代价）"
            "+ 5~10 条具体红线与反面教材（哪些写法会破坏设定/爽感/逻辑）。"
            "**必须围绕上面【已定世界观底座】的核心设定写红线。**直接输出 markdown 正文，不要任何说明。",
            ctx=ws_ctx),
        gen("outline.md",
            "【任务：故事大纲】按阶段规划写分层大纲：全局骨架（主题/主角弧光/结局方向/写作总纲）+ 每阶段一段"
            "（本阶段要推进什么、关键事件、主角成长、结尾钩子方向）。"
            "**必须严格基于上面【已定世界观底座】，禁止另起一套；结局方向必须来自底座的核心冲突。**"
            "直接输出 markdown 正文，不要任何说明。",
            ctx=ws_ctx),
        gen("checklist.md",
            "【任务：设定一致性清单】把这部小说的关键设定写成「- 【硬】…」「- 【必】…」「- 【伏笔】…」格式的清单"
            "（30~60条），供连载中核对一致性。**必须全部来自上面【已定世界观底座】，禁止凭空发明。**"
            "直接输出 markdown 列表，不要任何说明。",
            ctx=ws_ctx),
        gen("chapter_events.md",
            "【任务：逐章锚点表】基于阶段规划和上面【已定世界观底座】，为每一章写「本章必须推进的事件 + 2~4 个关键词」——"
            "这是连载时每章 prompt 的【主线锚点】，作用是防止 AI 写跑题/原地打转/偏离大纲。"
            "要求：每章的事件必须服务于本阶段目标并逐步逼近结局；后期章节（最后1/4）开始收束伏笔、走向结局方向；"
            "相邻章节事件要有递进，禁止重复。\n"
            "严格按此格式输出（每章一行，用 | 分隔三列）：\n"
            "第1章|关键词1、关键词2、关键词3|本章必须推进的事件一句话\n"
            "…一直输出到第{total}章。只输出表格行，不要任何说明、不要标题。".replace("{total}", str(stages[-1]["ch_hi"] if stages else 30)),
            ctx=ws_ctx),
    ]
    results = await asyncio.gather(*tasks)
    ok_n = sum(1 for r in results if r)
    if ok_n < len(results):
        failed = [f for f, r in zip(
            ["topic.md", "world_idea.md", "redline_table.md", "outline.md", "checklist.md", "chapter_events.md"],
            results) if not r]
        print(f"\n{C_R}⚠️ 有 {len(failed)} 个文件 AI 生成失败（保留占位模板）：{'、'.join(failed)}{C_0}")
        print(f"{C_Y}   → 下次联网稳定后，可手动编辑 novel_config/ 下这些文件，或删除后用向导重建。{C_0}")
    return ok_n


def create_book(name: str, intro: str, protagonist: str, stages: list, use_ai: bool, key: str,
               theme: str = "", style: str = "", ideas: str = ""):
    """建书主体：生成目录骨架 + 全部配置文件。返回新书目录。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip() or "我的新书"
    book_dir = BOOKS_DIR / safe
    if book_dir.exists():
        print(f"{C_Y}⚠️ 目录已存在：{book_dir}{C_0}")
        ans = ask("   覆盖重建？[回车=覆盖 / n=跳过]", "y")
        if ans != "y":
            return None
        shutil.rmtree(book_dir)

    # 目录骨架
    (book_dir / "novel_config").mkdir(parents=True)
    (book_dir / "ledger").mkdir()
    (book_dir / "已发布正文").mkdir()
    print(f"{C_B}📁 创建目录：{book_dir}{C_0}")

    # 复制模板（persona/topic/outline/checklist/redlines/foreshadow/scores/timeline/anchors/README）
    for f in TEMPLATE_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, book_dir / "novel_config" / f.name)
    # 模板里的 persona 是通用人格，AI 模式可保留；手工模式也够用

    # 程序化生成（锚点/时间线/评分/任务模板/占位 md）
    gen_config_files(book_dir, name, protagonist, stages)
    print(f"{C_G}  ✅ novel_config/ 骨架生成（anchors/timeline/scores/task_template/各占位 md）{C_0}")

    # AI 辅助生成设定
    if use_ai:
        if not key:
            print(f"{C_Y}⚠️ 无 API Key，跳过 AI 生成，可稍后手动填模板。{C_0}")
        else:
            print(f"{C_B}🤖 AI 辅助生成设定中（6 个文件并行，约 0.5~2 分钟）…{C_0}")
            ok = asyncio.run(ai_generate_files(book_dir, name, intro, protagonist, stages, key,
                                              theme, style, ideas))
            print(f"{C_G}  ✅ AI 生成完成：{ok}/6 个文件{C_0}")
            if ok < 6:
                print(f"{C_R}  ⚠️ 部分文件未生成（网络中断等），已保留占位模板。写章前请检查：{C_0}")
                print(f"{C_Y}     ls '{book_dir}/novel_config/'  → 看哪个 .md 很小（<300字）就是占位，需补填。{C_0}")

    # 新书 README（使用指引）
    (book_dir / "README.md").write_text(
        f"# 《{name}》· 新书工程\n\n"
        f"- 简介：{intro or '（未填写）'}\n"
        f"- 主角：{protagonist or '（未指定）'}\n"
        f"- 阶段规划：{len(stages)} 阶段\n\n"
        "## 写章（3 条命令）\n"
        "```\n"
        f"NOVEL_DIR=books/{safe} python3 write_chapter.py --preview            # ① 看第1章 prompt（零成本）\n"
        f"NOVEL_DIR=books/{safe} python3 write_chapter.py --write 1 --confirm  # ② 写第1章，人工确认后落盘\n"
        f"NOVEL_DIR=books/{safe} python3 write_chapter.py --write 3            # ③ 连写3章（自动落盘）\n"
        "```\n"
        "## 目录\n"
        "- `novel_config/` 书专属设定（世界观/人格/大纲/红线/锚点），引擎从这里读，改这里 = 改设定\n"
        "- `ledger/` 运行时账本（正史卡/评审反馈/费用），自动生成\n"
        "- `已发布正文/` 写好的章节\n",
        encoding="utf-8")
    print(f"{C_G}  ✅ 已写 README.md{C_0}")
    return book_dir


def main():
    ap = argparse.ArgumentParser(description="傻瓜式新书向导：问答建书 + 可选 AI 辅助生成设定")
    ap.add_argument("--name", default=None, help="书名（跳过问答直接指定）")
    ap.add_argument("--ai", dest="ai", action="store_true", help="开启 AI 辅助生成设定")
    ap.add_argument("--no-ai", dest="ai", action="store_false", help="跳过 AI 辅助")
    ap.set_defaults(ai=None)
    ap.add_argument("--key", default=None, help="DeepSeek API Key（或环境变量/项目 .env）")
    ap.add_argument("--intro", default="", help="一句话简介（--name 模式用）")
    ap.add_argument("--protagonist", default="", help="主角名（--name 模式用）")
    args = ap.parse_args()

    print("╔══════════════════════════════════════════════╗")
    print("║   📚 傻瓜式新书向导 · 问答建书                ║")
    print("╚══════════════════════════════════════════════╝")

    # ---- 书名 ----
    name = args.name or ask("📖 书名", "我的新书")
    intro = args.intro or ask("   一句话简介（可选）", "")
    protagonist = args.protagonist or ask("👤 主角名（可选）", "")
    total = int(ask("📐 预计总章数", "30")) or 30

    # ---- 作者自定义（主题/风格/脑洞，AI 基于此扩充；空=让 AI 自由发挥）----
    theme = ask("🎯 主题/题材（如：反乌托邦/克苏鲁/星际殖民/时间循环…可写自己想法，回车跳过）", "")
    style = ask("🎨 风格/基调（如：冷幽默/沉重史诗/轻快日常/硬核写实…回车跳过）", "")
    ideas = ask("💡 想塞的脑洞/元素（如：会说话的猫/古罗马遗迹/记忆交易…逗号分隔，回车跳过）", "")

    # ---- 阶段规划 ----
    stages = ask_stage_plan(total)

    # ---- AI 辅助？ ----
    use_ai = args.ai
    if use_ai is None:
        ans = ask(f"{C_B}🤖 用 AI 帮你生成设定吗？（世界观/大纲/脑洞/红线/清单，需 DeepSeek key，约几十秒）{C_0}", "y")
        use_ai = (ans != "n")
    key = load_key(args.key) if use_ai else ""

    # ---- 建书 ----
    book_dir = create_book(name, intro, protagonist, stages, use_ai, key, theme, style, ideas)
    if not book_dir:
        print("已取消。")
        return

    print(f"\n{C_G}🎉 新书《{name}》创建完成：{book_dir}{C_0}")
    print(f"\n{C_B}▶ 下一步（3 条命令就能开写）：{C_0}")
    safe = book_dir.name
    print(f"  1. 看第1章 prompt（零成本，确认设定注入效果）：")
    print(f"     {C_Y}NOVEL_DIR=books/{safe} python3 write_chapter.py --preview{C_0}")
    print(f"  2. 写第1章（人工确认后落盘）：")
    print(f"     {C_Y}NOVEL_DIR=books/{safe} python3 write_chapter.py --write 1 --confirm{C_0}")
    print(f"  3. 连写 3 章：")
    print(f"     {C_Y}NOVEL_DIR=books/{safe} python3 write_chapter.py --write 3{C_0}")
    print(f"\n  📝 想改设定？编辑 {book_dir / 'novel_config'}/ 下的文件即可，改完直接生效。")


if __name__ == "__main__":
    main()
