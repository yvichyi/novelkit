#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流程与质量：多轮讨论主循环 bridge_loop + CLI 入口（从 ai_bridge_api.py 拆分，原文未改）。"""
import argparse
import asyncio
import difflib
import json
import os
import re
import shutil
import sys
from .config import C_KIMI, C_QWEN, C_SYS, C_WARN, INITIAL_CHECKLIST, KIMI_PERSONA, MAX_TOKENS, QWEN_PERSONA, STATE_FILE, STORY_OUTLINE, TOPIC_DEFAULT, log, now_str
from .llm import LLMClient, usage_report
from .state import append_discussion, delete_state, extract_checklist_delta, extract_consensus, load_state, merge_checklist, parse_discuss_signal, parse_summary_package, save_state, write_consistency_file
from .story import append_story, apply_direct_edit, chapter_count, clean_story_text, default_outdir, extract_chapter, extract_rewrite_request, extract_story_blocks, extract_story_position, fix_person_to_third, is_cold, maybe_capture_stray_chapter, parse_direct_edit, read_recent_chapters, read_story_file, render_chatroom, render_novel, replace_chapter, scan_banned, strip_story_position
from .cards import append_event, append_score_history, build_fewshot_card, build_memory_doc, build_progress_card, build_setting_card, build_stage_guide, build_task_card, extract_event, extract_hard_rules, extract_memory_card, extract_scores, extract_style_samples, extract_unresolved_foreshadow, prune_checklist, read_progress_card, score_warning, stage_of_chapter


def write_phase_report(outdir, msgs, round_no, chapter_n):
    """P7：每10轮生成阶段质量报告（纯本地聚合，不加API调用）"""
    try:
        lines = []
        lines.append(f"# 《介质》阶段质量报告（第{round_no}轮）\n")
        lines.append(f"> 生成时间：{now_str()} ｜ 当前进度：第 {chapter_n} 章\n")
        reviews = [m for m in msgs if m.get("who") == "kimi"][-10:]
        lines.append(f"## 老K最近 {len(reviews)} 轮评审\n")
        for m in reviews:
            txt = (m.get("text") or "").strip()
            lines.append(f"### {m.get('ts', '')}\n\n{txt[:600]}\n")
        try:
            cl_path = outdir / "一致性清单.md"
            if cl_path.exists():
                cl = cl_path.read_text(encoding="utf-8")
                un = extract_unresolved_foreshadow(cl, limit=15)
                if un:
                    lines.append(f"## 未回收伏笔（前15条）\n{un}\n")
        except Exception:
            pass
        lines.append("\n---\n> 提示：详细机器体检请运行 python3 check_novel.py（章节/人称/时代词/时间线/专名）。\n")
        (outdir / "阶段质量报告.md").write_text("".join(lines), encoding="utf-8")
        log(f"📊 阶段质量报告已生成（第{round_no}轮）", C_WARN)
    except Exception as e:
        log(f"⚠ 阶段报告生成失败：{e}", C_WARN)

async def bridge_loop(qwen, kimi, args, outdir, msgs):
    # 设定一致性清单（跨函数共享的可变容器；摘要后更新，每轮注入写作/评审 prompt）
    consistency = {"list": ""}
    # 故事大纲（常驻）与本章共识（讨论产出）
    outline = STORY_OUTLINE
    consensus = {"text": ""}
    edit_feedback = {"text": ""}  # B4 定点修改失败反馈（注入下一轮老K评审）
    position_box = {"text": ""}  # P2 星尘【本章定位】声明（注入下一轮老K评审）
    score_state = {"warning": ""}  # C2 老K评分卡：连续低分警告（注入下一轮星尘写章）
    # 主线进度状态机（防跑偏核心：不依赖模型自觉，代码强制推进锚）
    anchor_state = {"current": 1, "chapters_in_anchor": 0}  # current=当前锚(1-8混沌期/9+后续幕)，chapters_in_anchor=本锚已写章数
    rhythm = {"last_L2": 0, "last_L3L4": 0, "last_combat": 0, "last_impact": 0}  # 节奏计数器（记录上次事件的章号）

    def record(who, text, name="系统", skip_story=False):
        msgs.append({"who": who, "text": text, "ts": now_str(), "name": name})
        flush_chatroom()
        if who == "qwen" and not skip_story:
            append_story(outdir, text, anchor_state["current"])
        # 全文已在流式打印时实时显示，这里只打一行状态（避免重复刷屏）
        log(f"\n{'='*40}\n📌 [{name}] 已完成（{len(text)}字），已存档\n{'='*40}",
            qwen.color if who == "qwen" else kimi.color)

    def flush_chatroom():
        try:
            (outdir / "chatroom.html").write_text(render_chatroom(msgs), encoding="utf-8")
            with open(outdir / "chat.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- {now_str()} ---\n")
                f.write(msgs[-1]["text"] + "\n")
        except Exception as e:
            log(f"⚠ 聊天室写入失败：{e}", C_WARN)

    # ---------- 核心循环（存档恢复 / 日志续写 / 新对话 共用） ----------
    async def run_loop(start_round, last_qwen, last_kimi, cold_streak=0):
        i = start_round
        while args.rounds <= 0 or i <= args.rounds:
            log(f"\n{'='*50}\n第 {i} 轮\n{'='*50}", C_SYS)
            # 读取 B7 熔断标记（上章若被丢弃，注入老K评审+星尘写章）
            try:
                _bf = outdir / ".b7_blocked"
                _b7_text = _bf.read_text(encoding="utf-8").strip() if _bf.exists() else ""
                if _b7_text:
                    _bf.unlink()  # 消费标记（熔断信息已读入 _b7_text）
            except Exception:
                _b7_text = ""
            # B7/B9 熔断轮：跳过老K评审，直接要求星尘重写上一章（保持同一章号）
            if _b7_text:
                log(f"🛑 熔断轮：上章质量门禁不合格被丢弃，直接要求星尘重写（{_b7_text[:120]}）", C_WARN)
                _bn_now = chapter_count(outdir) + 1
                _rewrite_prompt = (
                    f"你的上一章（第{_bn_now}章）因质量门禁不合格被程序丢弃，未进入正文。\n"
                    f"丢弃原因：{_b7_text}\n\n"
                    + "重写要求：严格对照丢弃原因逐条整改（B7=彻底避开污染词，换成你自己的表达；"
                    + "B9=必须推进当前锚的必达/必写元素、让核心设定在剧情里起作用、章首交代时间锚点、"
                    + "保留柯德的毒舌吐槽声音、写出末世氛围、结尾留钩子），宁缺毋滥不凑字数。\n\n"
                    + f"【故事大纲（把握走向）】\n{outline}\n\n"
                    + (f"【硬设定红线卡（写作必守）】\n{extract_hard_rules(consistency['list'])}\n\n" if consistency["list"] else "")
                    + f"【前情卡（代码生成的结构化前情）】\n{build_memory_doc(outdir, consistency['list'])}\n\n"
                    + f"【本章任务卡（代码生成，本章存在意义）】\n{build_task_card(anchor_state, rhythm, consistency['list'], outline)}\n\n"
                    + f"【本阶段评分标准（写章对齐）】\n{build_stage_guide(_bn_now)}\n\n"
                    + (f"{build_fewshot_card(_bn_now)}\n\n" if build_fewshot_card(_bn_now) else "")
                    + "请重写第" + str(_bn_now) + "章（1500~3000字），彻底避免黑名单词，直接写纯小说正文，"
                    + "正文第一行以「第" + str(_bn_now) + "章」开头，结尾保留钩子。"
                )
                _rw = await qwen.chat(_rewrite_prompt, temperature=0.8, max_tokens=MAX_TOKENS)
                record("qwen", _rw, qwen.name)
                last_qwen = _rw
                i += 1
                continue

            try:
                # Kimi 挑刺（附带故事大纲+设定一致性清单，供查对；并判断下一章是否需要讨论）
                log(f"🔍 {kimi.name} 收到故事，开始施压…", kimi.color)
                recent_ref = read_recent_chapters(outdir, n=2, max_chars=5000)
                c = await kimi.chat(
                    f"这是作家「星尘」刚写的最新章节：\n\n{last_qwen}\n\n"
                    + (f"【上一轮定点修改失败提示】你上一轮要求的定点修改未能定位到正文原文（摘录不准或该处已改）：{edit_feedback['text']}。请在本次评审重新给出更精确的原文摘录（直接复制正文原句10~50字，不要加引号包装），否则不要再次要求修改同一处。\n\n" if edit_feedback["text"] else "")
                      + (f"【上章B7熔断警示】上章含反雷同黑名单词已被程序丢弃未落盘（{_b7_text}）。请评审时明确要求星尘重写，且警告勿再出现该类词。\n\n" if _b7_text else "")
                    + (f"【星尘本章定位声明（他声明本章要写什么；你对照实际正文评审：声明没做到的要点名）】\n{position_box['text']}\n\n" if position_box["text"] else "")
                    + (f"【未回收伏笔清单（点名悬空最久的一条，要求作者本章推进或回收，不硬凑）】\n{extract_unresolved_foreshadow(consistency['list'], 6)}\n\n" if extract_unresolved_foreshadow(consistency['list'], 6) else "")
                    + f"【故事大纲（把握走向）】\n{outline}\n\n"
                      + f"【主线进度（代码强制状态机，评审必核）】当前应处锚{anchor_state['current']}（本锚已写{anchor_state['chapters_in_anchor']}章）；本章实际是否推进了锚{anchor_state['current']}的必达/必写元素？没推进=点名。\n\n"
                      + f"【节奏计数器（代码统计，超限必点名）】距上次L2以上环境冲击已{rhythm['last_L2']}章（上限3）/ 距上次L3/L4生死压力已{rhythm['last_L3L4']}章（上限6）/ 距上次编译攻防已{rhythm['last_combat']}章（上限5）/ 距上次外部冲击已{rhythm['last_impact']}章（上限3）。超限=点名要求下章爆发。\n\n"
+ f"【本阶段评分标准（六维打分按此，按本阶段要点）】\n{build_stage_guide(chapter_count(outdir))}\n\n"
                    + (f"【设定一致性清单（查对基准）】\n{consistency['list']}\n\n" if consistency["list"] else "")
                      + f"【完整世界观设定（审查对照基准，判定硬伤时以它为准）】\n{TOPIC_DEFAULT}\n\n"
                    + (f"【最近章节原文（对照文风与连续性，供评审参考）】\n{recent_ref}\n\n" if recent_ref else "")
                    + "请按你的评审格式做评审（200~300字）。\n"
                    + "评审末尾必须输出八个区块（缺任一=评审不合格）：\n"
                    + "【讨论建议】需要讨论 / 直接写 + 一句话理由（新派系登场/重大转折/新能力首次使用/上一章致命伤需大调→需要讨论；日常推进/场景过渡/平稳深化→直接写）\n"
                    + "【要求重写】第X章+具体重写要求（写明哪一章哪一处硬伤+为何必须推翻+期望改法；没有充分理由写'无'。重写须经星尘认同才执行）\n"
                    + "【直接修改】第X章+原文片段→改为→新文本（定点修订具体错误：时间线bug/术语误用/物理硬伤；无则写'无'。须经星尘认同才执行）\n"
                    + "【节奏确认】按本章实际内容逐项写：L2=有/无、L3L4=有/无、攻防=有/无、外部冲击=有/无（本章出现了该级别环境冲击/编译攻防/外部事件就写'有'，否则'无'。只用于代码重置节奏计数器）\n"
                    + "【锚确认】当前锚X 是否完成：已完成/未完成（对照锚的必达元素逐条核对，未完成=即使章数超也留在本锚）\n"
                    + "【文学评审四问+硬核审查】不打分，只答+理由：①我为什么要翻下一章（具体原因）；②这章留下什么画面/情绪/想法；③柯德有没有微小变化；④删掉这章作品失去什么（不可删除性）；⑤硬核审查（物理自洽/知识锚点/熵债）\n"
                    + "【清单更新】本章新增或修正的设定（- 开头；无则写'无'）\n"
                      + "【事件】第X章一句话总结本章发生了什么（20~40字，供代码提取进前情卡；无重大事件写'无'）", max_tokens=MAX_TOKENS)
                record("kimi", c, kimi.name)
                edit_feedback["text"] = ""  # 本轮反馈已注入，清空
                # 解析老K【节奏确认】→ 重置对应节奏计数器（超限点名→下章补上→归零→重新计时）
                _rc = re.search(r"(?:【节奏确认】|#{1,4}\s*节奏确认)[\s\S]*?(?:L2=有|L2=无)", c)
                if _rc:
                    _rc_text = _rc.group(0)
                    _map = {"L2": "last_L2", "L3L4": "last_L3L4", "攻防": "last_combat", "外部冲击": "last_impact"}
                    for _k, _rk in _map.items():
                        if re.search(rf"{_k}\s*=\s*有", _rc_text):
                            rhythm[_rk] = 0
                            log(f"📊 节奏确认：{_k}=有，计数器归零", C_WARN)
                # 解析老K【锚确认】→ 内容驱动切锚（未完成=留在本锚）
                _ac = re.search(r"(?:【锚确认】|#{1,4}\s*锚确认)[\s\S]*?(?:已完成|未完成)", c)
                if _ac:
                    _ac_text = _ac.group(0)
                    _cur_a = anchor_state["current"]
                    if "已完成" in _ac_text and _cur_a < 8:
                        anchor_state["current"] = _cur_a + 1
                        anchor_state["chapters_in_anchor"] = 0
                        log(f"📌 老K确认锚{_cur_a}已完成，推进到锚{anchor_state['current']}", C_WARN)
                    elif "未完成" in _ac_text:
                        log(f"📌 老K确认锚{_cur_a}未完成，留在本锚（即使章数超上限）", C_WARN)
                # ---- 老K评分卡（C2）：解析六维评分写入 评分卡.md，连续低分生成警告 ----
                _scores = extract_scores(c)
                if _scores:
                    _stage_name, _, _ = stage_of_chapter(chapter_count(outdir))
                    append_score_history(outdir, chapter_count(outdir), _stage_name, _scores)
                    _sw = score_warning(outdir)
                    score_state["warning"] = _sw
                    if _sw:
                        log(f"⚠ 评分提醒：{_sw}", C_WARN)
                else:
                    log("⚠ 老K本轮未输出【评分】区块（或格式不符），评分卡本轮空缺", C_WARN)
                # ---- 老K【事件】区块 → 前情事件.md（供前情卡引用，代码累积零幻觉） ----
                _ev = extract_event(c)
                if _ev and _ev != "无":
                    append_event(outdir, chapter_count(outdir), _ev)
                # 从老K评审中提取【清单更新】区块，合并进主清单（老K是清单共同维护者）
                checklist_delta = extract_checklist_delta(c)
                if checklist_delta:
                    consistency["list"] = merge_checklist(consistency["list"], checklist_delta)
                    write_consistency_file(outdir, consistency["list"])
                    log(f"📋 老K清单更新已合并（当前清单 {len(consistency['list'])} 字）", C_WARN)
                # 解析老K的讨论建议：下一章是否需要先讨论
                need_discuss = parse_discuss_signal(c)
                if is_cold(c, last_kimi):
                    cold_streak += 1
                    log(f"⚠ {kimi.name} 回复疑似冷场（{cold_streak}/2）", C_WARN)
                else:
                    cold_streak = 0
                last_kimi = c

                # ---- 老K【直接修改】定点修订？须星尘认同才执行 ----
                # 流程：解析【直接修改】第X章+原文→改为→新文 → 星尘裁决 → 认同则定点替换
                direct_edits = parse_direct_edit(c)
                if direct_edits:
                    sf2 = outdir / "故事正文.md"
                    if sf2.exists():
                        for (ed_n, ed_old, ed_new) in direct_edits:
                            # 征求星尘意见
                            verdict_d = await qwen.chat(
                                f"主编「老K」要求对第{ed_n}章做定点修改：\n原文：{ed_old}\n改为：{ed_new}\n\n"
                                "你作为作者有一票否决权。认同输出【同意修改】；不认同输出【拒绝修改】+一句话理由。",
                                max_tokens=MAX_TOKENS)
                            record("sys", f"🔧 星尘对定点修改第{ed_n}章的裁决：\n{verdict_d}", "星尘·修改裁决")
                            if "同意修改" in verdict_d or "认同" in verdict_d:
                                st2 = sf2.read_text(encoding="utf-8")
                                new_st, ok = apply_direct_edit(st2, ed_n, ed_old, ed_new)
                                if ok:
                                    sf2.write_text(new_st, encoding="utf-8")
                                    render_novel(outdir)
                                    # 定位替换位置，打印上下文预览（确认新文本真的嵌入正文）
                                    ctx = ""
                                    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, st2, new_st).get_opcodes():
                                        if tag in ("replace", "delete", "insert"):
                                            ctx = new_st[max(0, j1 - 30):j2 + 30].replace("\n", " ")
                                            break
                                    log(f"🔧 第{ed_n}章已定点修改并嵌入正文：\n"
                                        f"  「{ed_old[:40]}…」→「{ed_new[:40]}…」\n"
                                        f"  📍 嵌入位置：…{ctx}…", C_WARN)
                                    append_discussion(outdir, f"定点修改-第{ed_n}章",
                                        f"原文：{ed_old}\n改为：{ed_new}\n\n（已嵌入《故事正文.md》并重新渲染，星尘认同执行）")
                                else:
                                    log(f"⚠ 定点修改失败：第{ed_n}章未找到原文片段「{ed_old[:30]}…」（可能已改或摘录不准），跳过。", C_WARN)
                                    edit_feedback["text"] += f"\n- 第{ed_n}章：{ed_old[:60]}"
                            else:
                                log(f"🔧 星尘拒绝定点修改第{ed_n}章（{verdict_d[:60]}…）", C_WARN)
                    else:
                        log("⚠ 故事正文.md 不存在，无法定点修改。", C_WARN)

                # ---- 老K要求重写某章？须有具体理由 + 星尘认同才执行 ----
                # 流程：解析【要求重写】第X章+理由 → 征求星尘意见（一票否决权）→
                #       认同→重写并替换正文（本轮不写新章，下轮老K复审重写结果）
                #       拒绝→按正常流程继续写新章
                rewrote = False
                rewrite_n, rewrite_req = extract_rewrite_request(c)
                if rewrite_n and rewrite_req:
                    sf = outdir / "故事正文.md"
                    if sf.exists():
                        old_chapter = extract_chapter(sf.read_text(encoding="utf-8"), rewrite_n)
                        if old_chapter:
                            log(f"♻️ 老K要求重写第{rewrite_n}章（理由：{rewrite_req[:60]}…），征求星尘意见…", C_WARN)
                            verdict_w = await qwen.chat(
                                f"主编「老K」要求重写第{rewrite_n}章，理由：\n{rewrite_req}\n\n"
                                f"【原章节开头（供你判断）】\n{old_chapter[:400]}\n\n"
                                "重写是最后手段，你作为作者有一票否决权。"
                                "认同输出【同意重写】；不认同输出【拒绝重写】+一句话理由。",
                                max_tokens=MAX_TOKENS)
                            record("sys", f"♻️ 星尘对重写第{rewrite_n}章的裁决：\n{verdict_w}", "星尘·重写裁决")
                            if "同意重写" in verdict_w or "认同" in verdict_w:
                                log(f"♻️ 星尘认同，重写第{rewrite_n}章…", C_WARN)
                                new_ch = await qwen.chat(
                                    f"请重写第{rewrite_n}章。\n\n"
                                    + f"【老K要求重写的理由（必须解决）】\n{rewrite_req}\n\n"
                                    + f"【原章节（保留其有效伏笔与情节资产，勿照抄，解决老K点名的硬伤）】\n{old_chapter}\n\n"
                                    + f"【故事大纲（把握走向）】\n{outline}\n\n"
                                    + (f"【硬设定红线卡（写作必守）】\n{extract_hard_rules(consistency['list'])}\n\n" if consistency["list"] else "")
                                    + "直接输出重写后的纯小说正文（1500~3000字），只写正文，不输出任何说明。",
                                    temperature=0.8, max_tokens=MAX_TOKENS)
                                cleaned = clean_story_text(new_ch)
                                if cleaned and len(cleaned) > 100:
                                    story_text = sf.read_text(encoding="utf-8")
                                    sf.write_text(replace_chapter(story_text, rewrite_n, cleaned), encoding="utf-8")
                                    render_novel(outdir)
                                    record("qwen", cleaned, "星尘·重写", skip_story=True)
                                    last_qwen = cleaned
                                    rewrote = True
                                    log(f"♻️ 第{rewrite_n}章已重写替换（{len(cleaned)}字），下轮老K将复审该章。", C_WARN)
                                else:
                                    log("⚠ 重写产出过短或为空，放弃替换，按正常流程继续写新章。", C_WARN)
                            else:
                                log(f"♻️ 星尘拒绝重写第{rewrite_n}章（{verdict_w[:80]}…），按正常流程继续写新章。", C_WARN)
                        else:
                            log(f"⚠ 未找到第{rewrite_n}章原文，跳过重写。", C_WARN)
                    else:
                        log("⚠ 故事正文.md 不存在，无法重写。", C_WARN)
                if rewrote:
                    need_discuss = False  # 重写轮跳过讨论

                # ---- 关键章节：讨论子流程（多轮直到共识，最多3轮）----
                if need_discuss and not args.no_discuss:
                    log(f"🎙️ 老K判定本章为关键章节，进入讨论…", C_WARN)
                    discussion_log = []
                    verdict = ""
                    for rnd in range(1, 4):
                        # 老K提问
                        q = await kimi.chat(
        f"【故事大纲】\n{outline}\n\n"
                            + (f"【设定一致性清单】\n{consistency['list']}\n\n" if consistency["list"] else "")
                            + f"上一章结尾：{last_qwen[-400:]}\n"
                            + (f"上一轮星尘的答辩/你的追问：\n{verdict}\n\n" if rnd > 1 else "")
                            + f"这是关键章节（第{rnd}轮讨论）。请抛出本章的 2~3 个细则问题（人物动机/能力边界/"
                            + "伏笔取舍/情节逻辑/设定运用），每个问题给出你的倾向方案（但不定案）。",
                            max_tokens=MAX_TOKENS)
                        record("sys", f"🎙️ 讨论第{rnd}轮 · 老K提问：\n{q}", "老K·讨论")
                        append_discussion(outdir, f"老K提问-{rnd}", q)
                        discussion_log.append(f"老K提问：{q}")

                        # 星尘答辩
                        plan = await qwen.chat(
        f"【故事大纲】\n{outline}\n\n"
                            + (f"【设定一致性清单】\n{consistency['list']}\n\n" if consistency["list"] else "")
                            + f"老K的问题：\n{q}\n\n"
                            + "请逐条答辩：每条给出采纳/反驳/折中+理由（简短）。"
                            + "最后输出【本章写作计划】（300字内：本章目标、关键场景、结尾钩子）。"
                            + "【重要】这是讨论环节，只输出答辩和写作计划，绝对不要写小说正文，"
                            + "不要写'第X章'开头的章节内容——正文留到讨论结束后再写。",
                            temperature=0.8, max_tokens=MAX_TOKENS)
                        # 兜底：若星尘在答辩里夹带完整正文，先补进正文文件
                        maybe_capture_stray_chapter(outdir, plan)
                        record("sys", f"🎙️ 讨论第{rnd}轮 · 星尘答辩：\n{plan}", "星尘·讨论")
                        append_discussion(outdir, f"星尘答辩-{rnd}", plan)
                        discussion_log.append(f"星尘答辩：{plan}")

                        # 老K共识审核
                        verdict = await kimi.chat(
        f"【故事大纲】\n{outline}\n\n"
                            + (f"【设定一致性清单】\n{consistency['list']}\n\n" if consistency["list"] else "")
                            + f"星尘的本章写作计划：\n{plan}\n\n"
                            + "请审核：若有分歧，指出问题并继续追问（作为下一轮问题）；"
                            + "若达成共识，输出【共识达成】+ 本章共识定稿（本章目标/关键场景/结尾钩子/要遵守的设定要点），"
                            + "并输出【清单更新】区块（讨论中确立的新设定，- 开头；无则写'无'）。",
                            max_tokens=MAX_TOKENS)
                        record("sys", f"🎙️ 讨论第{rnd}轮 · 老K审核：\n{verdict}", "老K·讨论")
                        append_discussion(outdir, f"老K审核-{rnd}", verdict)
                        discussion_log.append(f"老K审核：{verdict}")

                        if "共识达成" in verdict:
                            consensus["text"] = extract_consensus(verdict) or plan
                            # 讨论中确立的设定并入主清单
                            d2 = extract_checklist_delta(verdict)
                            if d2:
                                consistency["list"] = merge_checklist(consistency["list"], d2)
                                write_consistency_file(outdir, consistency["list"])
                            log(f"✅ 讨论达成共识（第{rnd}轮）：\n{consensus['text']}", C_WARN)
                            break
                    else:
                        # 3轮未达成：老K拍板（用最后一轮审核作为定稿）
                        consensus["text"] = extract_consensus(verdict) or verdict[:300]
                        log(f"⚠ 3轮未完全达成，老K拍板：\n{consensus['text']}", C_WARN)
                        d2 = extract_checklist_delta(verdict)
                        if d2:
                            consistency["list"] = merge_checklist(consistency["list"], d2)
                            write_consistency_file(outdir, consistency["list"])
                    save_state(outdir, qwen, kimi, i, last_qwen, last_kimi, msgs, consistency["list"], outline, consensus["text"], anchor_state, rhythm)
                else:
                    consensus["text"] = ""  # 直接写：无共识，靠大纲+清单

                # 千问回应/改稿（写作前必读清单+大纲；有共识则按共识执行）
                if rewrote:
                    # 重写轮：已用重写代替写新章，本轮不再产出新章
                    log(f"♻️ 本轮为重写轮（第{rewrite_n}章已替换），跳过新章写作，下轮老K复审重写结果。", C_WARN)
                else:
                    log(f"✍️ {qwen.name} 收到批评，回应/改稿…", qwen.color)
                    style_ref = read_recent_chapters(outdir, n=1, max_chars=2500)  # P1 文风锚点：上一章尾部
                    _mem_card = extract_memory_card(consistency['list'] or '') if consistency["list"] else ""  # P8 记忆快照
                    _prog_card = read_progress_card(outdir)  # P11 故事进展卡
                    _story_full = read_story_file(outdir)  # P10 正文全文（供文风样本）
                    _style_samp = extract_style_samples(_story_full) if _story_full else ""  # P9 文风样本
                    _unres = extract_unresolved_foreshadow(consistency["list"], 6)  # P3 未回收伏笔
                    r = await qwen.chat(
                      f"这是主编「老K」对你最新章节的评审：\n\n{c}\n\n"
                    f"目前故事已连载到第 {chapter_count(outdir)} 章。\n"
                      + f"【故事大纲（把握走向）】\n{outline}\n\n"
                      + (f"【硬设定红线卡（写作必守，全文清单由老K维护查对）】\n{extract_hard_rules(consistency['list'])}\n\n" if consistency["list"] else "")
                      + f"【前情卡（代码生成的结构化前情：章节时间线/近期事件/伏笔/状态，零幻觉替代模型摘要）】\n{build_memory_doc(outdir, consistency['list'])}\n\n"
                      + (f"【记忆快照（最新状态卡+硬设定红线+柯德声音，写作基准）】\n{_mem_card}\n\n" if _mem_card else "")
                      + (f"【故事进展（截至当前的状态卡：位置/时间/主线/支线，接续基准）】\n{_prog_card}\n\n" if _prog_card else "")
                      + (f"【文风样本（从已写章节抽取的高光片段，对齐语言质感，勿照抄）】\n{_style_samp}\n\n" if _style_samp else "")
                      + (f"【本章共识（讨论定稿，必须严格执行）】\n{consensus['text']}\n\n" if consensus["text"] else "")
                      + (f"【最近章节原文（文风锚点：延续这个声音、节奏、人物语气与叙事视角，不要重复已写内容）】\n{style_ref}\n\n" if style_ref else "")

                      + (f"【上章被质量门禁熔断丢弃（必须重写）】你的上一章因质量门禁不合格被程序丢弃，未进入正文。请重写上一章内容（保持同一章号），严格对照丢弃原因整改：{_b7_text}。\n\n" if _b7_text else "")
                      + f"【当前主线锚（代码强制，本章必须推进它）】锚{anchor_state['current']}：从大纲锚{anchor_state['current']}的「必达/必写」里选至少1项落实（这是本章存在的意义，不是背景）。若本锚已写{anchor_state['chapters_in_anchor']}章仍未完成，本章必须推进其核心目标。\n\n"
                        + (f"【上章评分提醒】{score_state['warning']}\n\n" if score_state.get("warning") else "")
                        + f"【本章任务卡（代码生成，本章存在意义，必须落实至少1项必达/必写）】\n{build_task_card(anchor_state, rhythm, consistency['list'], outline)}\n\n"
                        + (f"{build_fewshot_card(chapter_count(outdir) + 1)}\n\n" if build_fewshot_card(chapter_count(outdir) + 1) else "")
                        + f"【本阶段评分标准（写章对齐，六维按此自查）】\n{build_stage_guide(chapter_count(outdir) + 1)}\n\n"
                        + f"【设定速查卡（阶段门禁：当前阶段可用设定+预告窗口，写作前必读）】\n{build_setting_card(chapter_count(outdir) + 1, outline)}\n\n"
                      + "连载规则：\n"
                      + "1. 已发布的章节视为定稿，不重写（除非老K明确要求重写且经你认同）\n"
                      + "2. 老K批评的问题，在下一章中通过剧情推进自然修正/补充设定来回应，不推翻已发布内容\n"
                      + "3. 老K认可的点，继续保留并深化\n"
                      + "4. 新章节要衔接上一章结尾，不要重复已写内容\n"
                      + "5. 清单之外的设定改动，须先经老K评审通过，否则视为设定漂移\n"
                      + "请写出下一章（1500~3000字）。正文第一行必须以「第X章」开头（X=下一章编号，阿拉伯数字，"
                      + "可加副标题，如：第3章 断谷的雨）。本章结尾必须有钩子（悬念/反转/新威胁/人物变化，任选其一）。"
                      + "写完正文后，另起一行输出【本章定位】（仅3行元信息，不进正文：①所属幕/时代 ②推进的线索 ③结尾钩子是什么）。"
                      + "直接写纯小说正文。", temperature=0.8, max_tokens=MAX_TOKENS)
                    position_box["text"] = extract_story_position(r) or ""
                    r = strip_story_position(r)
                    record("qwen", r, qwen.name)
                    if is_cold(r, last_qwen):
                        cold_streak += 1
                        log(f"⚠ {qwen.name} 回复疑似冷场（{cold_streak}/2）", C_WARN)
                    else:
                        cold_streak = 0
                    last_qwen = r
                    # ---- 主线锚进度状态机更新（代码强制推进大纲） ----
                    # 锚1=2章, 锚2=6章, 锚3=10章, 锚4=10章, 锚5=10章, 锚6=10章, 锚7=25章, 锚8=25章
                    _anchor_caps = {1:2, 2:6, 3:10, 4:10, 5:10, 6:10, 7:25, 8:25}
                    _cur = anchor_state["current"]
                    anchor_state["chapters_in_anchor"] += 1
                    if _cur in _anchor_caps and anchor_state["chapters_in_anchor"] >= _anchor_caps[_cur]:
                        anchor_state["current"] = min(_cur + 1, 8)
                        anchor_state["chapters_in_anchor"] = 0
                        log(f"📌 锚{_cur} 章节上限已到，推进到锚{anchor_state['current']}", C_WARN)
                    # 节奏计数器：每章全局递增（老K评审时会点名超限，本章之后看星尘是否补上事件，简单按章递增）
                    for _k in rhythm:
                        rhythm[_k] += 1

                    # ---- 自动更新故事进展.md（代码提取，零幻觉零污染） ----
                    try:
                        _prog = build_progress_card(outdir, anchor_state, rhythm, consistency["list"])
                        if _prog:
                            (outdir / "故事进展.md").write_text(f"# 自动进展卡（每章更新）\n\n{_prog}", encoding="utf-8")
                    except Exception:
                        pass

                # 每轮结束存一次档（随时 Ctrl+C 都不丢进度）
                save_state(outdir, qwen, kimi, i, last_qwen, last_kimi, msgs, consistency["list"], outline, consensus["text"], anchor_state, rhythm)
                # E: 每10章自动备份正文（安全网，配合已发布定稿铁律）
                try:
                    _bn = chapter_count(outdir)
                    if _bn and _bn % 10 == 0:
                        _bk = outdir / "备份_自动"
                        _bk.mkdir(exist_ok=True)
                        # 清单归档：旧状态卡+已回收伏笔移入 清单归档.md（防膨胀）
                        try:
                            _pruned, _arch = prune_checklist(outdir, consistency["list"])
                            if _arch:
                                _ap = outdir / "清单归档.md"
                                _ah = "# 清单归档（旧状态卡/已回收伏笔）\n\n" if not _ap.exists() else ""
                                with open(str(_ap), "a", encoding="utf-8") as _af:
                                    _af.write(_ah + _arch + "\n\n")
                                consistency["list"] = _pruned
                                write_consistency_file(outdir, _pruned)
                                log(f"📋 清单已归档旧条目，保留最新状态卡", C_WARN)
                        except Exception as _pe:
                            log(f"⚠ 清单归档失败：{_pe}", C_WARN)
                        _dst = _bk / f"正文_第{_bn}章.md"
                        if not _dst.exists():
                            shutil.copy(outdir / "故事正文.md", _dst)
                            log(f"💾 自动备份：第{_bn}章 → 备份_自动/正文_第{_bn}章.md", C_WARN)
                except Exception as _e:
                    log(f"⚠ 自动备份失败：{_e}", C_WARN)
                # P7 每10轮生成阶段质量报告
                if i % 10 == 0:
                    write_phase_report(outdir, msgs, i, chapter_count(outdir))

                if cold_streak >= 2:
                    log("🛑 双方连续冷场，自动结束对话。", C_WARN)
                    break

                # ---- 摘要压缩：每 summary_every 轮，把前情浓缩后重置上下文 ----
                if args.summary_every > 0 and i % args.summary_every == 0:
                    log(f"\n🧠 第 {i} 轮，开始压缩前情（保留剧情、省 token）…", C_WARN)
                    try:
                        summary_pkg = await qwen.chat(
                            "请严格输出以下两个区块（固定格式，区块标题照抄）：\n\n"
                            "【前情摘要】\n"
                            "用500字左右总结到目前为止的完整剧情：先写一行【截至第X章·故事进展】（当前时间点/柯德位置/主线进度/支线状态），"
"然后详细总结：当前进度（对照年表：异常期/失控期/崩塌期/剧变期/清理期/分裂期/共存期/终局）、"
                            "【设定一致性清单】\n"
                            "列出已确立且后续写作必须遵守的所有设定：地名、能力规则、角色关系、已定事实、术语、人物状态——"
                            "每条一行（用 - 开头），尽量完整详细，宁可多写不可漏，这是后续查对的基准。"
                            "同一设定多个版本合并为最新一条（去重防膨胀）；末尾单独输出【伏笔登记】区块：未回收伏笔逐条列出（格式：- 【伏笔】xxx（第X章埋）），已回收标注（已回收@第X章）。",
                            max_tokens=MAX_TOKENS)
                        summary, checklist = parse_summary_package(summary_pkg)
                        if not summary:
                            summary = summary_pkg[:600]
                        consistency["list"] = checklist  # 更新清单（每轮写作都带上）
                        record("sys", f"📌 前情摘要（第{i}轮）：{summary}")
                        try:
                            _banned_in_summary = scan_banned(summary)
                            if _banned_in_summary:
                                log(f"⚠ 故事进展摘要含黑名单词 {_banned_in_summary}，已过滤后写入（防污染回灌）", C_WARN)
                                for _w in _banned_in_summary:
                                    summary = summary.replace(_w, "「某物」")
                            # 进展卡已改为代码自动生成（build_progress_card），不再由模型摘要落盘（防幻觉/污染回灌）
                        except Exception:
                            pass
                        if checklist:
                            record("sys", f"📋 设定一致性清单（第{i}轮）：\n{checklist}")
                            write_consistency_file(outdir, checklist)
                        qwen.reset_context_with_summary(summary, checklist)
                        kimi.reset_context_with_summary(summary, checklist)
                        log("✅ 上下文已压缩，双方带着摘要+清单继续。", C_WARN)
                        save_state(outdir, qwen, kimi, i, last_qwen, last_kimi, msgs, consistency["list"], outline, consensus["text"], anchor_state, rhythm)  # 摘要后重新存档
                    except Exception as e:
                        log(f"⚠ 摘要生成失败（继续原样聊）：{e}", C_WARN)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"❌ 第 {i} 轮出错：{e}", C_WARN)
                if getattr(args, "yes", False):
                    log("  ⏩ --yes 已启用，出错自动继续下一轮…", C_WARN)
                else:
                    ans = await asyncio.to_thread(input, "出错啦，输入 y 继续下一轮，其他任意键退出：")
                    if ans.strip().lower() != "y":
                        break
            i += 1
        delete_state(outdir)  # 正常走完（冷场/轮数到）才删档
        record("sys", "对话结束")
        log(f"\n🏁 完成！聊天室：{outdir / 'chatroom.html'}\n完整日志：{outdir / 'chat.log'}", C_SYS)

    # ---------- 分支一：检测存档，续聊 ----------
    state = None
    if not args.fresh:
        state = load_state(outdir)
    if state and state.get("qwen_history") and state.get("kimi_history"):
        qwen.history = state["qwen_history"]
        # 强制刷新 system prompt 为新 persona（改规则后 resume 也生效，不丢正文/清单/讨论）
        if qwen.history and qwen.history[0].get("role") == "system":
            qwen.history[0]["content"] = QWEN_PERSONA
        # 第三人称兜底消毒：历史里的叙述「我」若未被清单拦住，恢复时自动清掉（台词「我」受保护）
        for h in qwen.history:
            if h.get("role") == "assistant" and h.get("content"):
                h["content"] = fix_person_to_third(h["content"])
        kimi.history = state["kimi_history"]
        # 强制刷新 kimi system prompt 为新 persona
        if kimi.history and kimi.history[0].get("role") == "system":
            kimi.history[0]["content"] = KIMI_PERSONA
        last_qwen = state.get("last_qwen", "")
        last_kimi = state.get("last_kimi", "")
        last_qwen = fix_person_to_third(last_qwen)
        start_round = state.get("round", 0) + 1
        # 恢复设定一致性清单（续聊不丢）：磁盘演进版优先（伏笔登记更全），无磁盘或更短才用state
        _disk_cl = ""
        _cl_path = outdir / "一致性清单.md"
        try:
            if _cl_path.exists():
                _disk_cl = _cl_path.read_text(encoding="utf-8")
        except Exception:
            _disk_cl = ""
        _state_cl = state.get("consistency", "")
        if _disk_cl and len(_disk_cl) >= len(_state_cl):
            consistency["list"] = _disk_cl
            log(f"📋 已恢复设定一致性清单（磁盘演进版 {len(_disk_cl)}字，含伏笔登记），写作将按清单查对。", C_WARN)
        else:
            consistency["list"] = _state_cl
            if consistency["list"]:
                write_consistency_file(outdir, consistency["list"])
            log(f"📋 已恢复设定一致性清单（{len(consistency['list'])}字），写作将按清单查对。", C_WARN)
        # 恢复故事大纲与本章共识
        if state.get("outline"):
            outline = state["outline"]
        if state.get("consensus"):
            consensus["text"] = state["consensus"]
            log(f"📋 已恢复本章共识（讨论定稿），写作将按共识执行。", C_WARN)
        # 恢复主线进度状态机 + 节奏计数器
        if state.get("anchor_state"):
            anchor_state.update(state["anchor_state"])
        if state.get("rhythm"):
            rhythm.update(state["rhythm"])
        # 恢复聊天室已有内容
        for m in state.get("msgs", []):
            if not any(x.get("ts") == m.get("ts") and x.get("who") == m.get("who") and x.get("text") == m.get("text") for x in msgs):
                msgs.append(m)
        log(f"🔄 检测到上次存档（已聊到第 {state.get('round', 0)} 轮），从第 {start_round} 轮继续！", C_WARN)
        if not getattr(args, "yes", False):
            log("  ⏳ 等待回车确认（或 Ctrl+C 退出；加 --yes 参数可跳过确认自动继续）", C_WARN)
            log("  💡 一行跑到底：末尾加 --yes 即可跳过本确认自动继续", C_WARN)
            try:
                await asyncio.to_thread(input)
            except (KeyboardInterrupt, EOFError):
                log("已退出，进度已保留。")
                return
        else:
            log("  ⏩ --yes 已启用，自动继续…", C_WARN)
        await run_loop(start_round, last_qwen, last_kimi)
        return

    # ---------- 分支二：从旧日志续写（提炼前情摘要后接着写） ----------
    log_path = None
    if args.resume_story:
        log_path = Path(args.resume_story)
    elif not args.fresh and not state:
        cand = outdir / "chat.log"
        if cand.exists():
            log_path = cand
    if log_path is not None and log_path.exists():
        blocks = extract_story_blocks(log_path)
        if blocks:
            log(f"📖 检测到旧故事日志（最近 {len(blocks)} 段），正在提炼前情摘要…", C_WARN)
            try:
                story_text = "\n\n".join(blocks)
                summary_pkg = await qwen.chat(
                    f"以下是一部科幻连载小说的历次片段（作家正文与编辑批注混合，编辑批注常带'致命伤'或**标记）。\n"
                    f"请严格输出两个区块：\n"
                    f"【前情摘要】（500字左右：核心设定、主要人物、当前剧情进展、未解决的线索、编辑正在追问的问题）\n"
        f"【设定一致性清单】（已确立设定的逐条清单，- 开头，尽量完整）\n\n---\n{story_text}",
                    max_tokens=MAX_TOKENS)
                summary, checklist = parse_summary_package(summary_pkg)
                if not summary:
                    summary = summary_pkg[:600]
                consistency["list"] = checklist
                log(f"\n📌 前情摘要：\n{summary}", C_WARN)
                if checklist:
                    log(f"\n📋 设定一致性清单：\n{checklist}", C_WARN)
                    write_consistency_file(outdir, checklist)
                if not getattr(args, "yes", False):
                    log("  ⏳ 等待回车确认后开始续写（或 Ctrl+C 退出；加 --yes 参数可跳过确认）", C_WARN)
                    log("  💡 一行跑到底：末尾加 --yes 即可跳过本确认自动续写", C_WARN)
                    try:
                        await asyncio.to_thread(input)
                    except (KeyboardInterrupt, EOFError):
                        log("已退出，进度已保留。")
                        return
                else:
                    log("  ⏩ --yes 已启用，自动开始续写…", C_WARN)
                qwen.reset_context_with_summary(summary, checklist)
                kimi.reset_context_with_summary(summary, checklist)
                record("sys", "📖 接续旧故事（已提炼前情摘要+设定清单）")
                log(f"\n✍️ {qwen.name} 接着旧故事续写…", qwen.color)
                _next = chapter_count(outdir) + 1
                # ★ 写作指令显式带上 前情摘要+清单+原文，避免清单被 history 稀释
                recent2 = read_recent_chapters(outdir, n=3, max_chars=9000)
                _mem_card2 = extract_memory_card(checklist or '') if checklist else ""
                _prog_card2 = read_progress_card(outdir)
                _story_full2 = read_story_file(outdir)
                _style_samp2 = extract_style_samples(_story_full2) if _story_full2 else ""
                r1 = await qwen.chat(
                    f"【前情摘要（接续基准）】\n{summary}\n\n"
                    + (f"【硬设定红线卡（写作必守）】\n{extract_hard_rules(checklist)}\n\n" if checklist else "")
                    + f"【前情卡（代码生成的结构化前情）】\n{build_memory_doc(outdir, checklist)}\n\n"
                    + (f"【最近章节原文（写前通读，延续文风与叙事节奏，不要重复这些内容）】\n{recent2}\n\n" if recent2 else "")
                    + (f"【记忆快照（最新状态卡+硬设定红线+柯德声音，写作基准）】\n{_mem_card2}\n\n" if _mem_card2 else "")
                    + (f"【故事进展（截至当前的状态卡：位置/时间/主线/支线，接续基准）】\n{_prog_card2}\n\n" if _prog_card2 else "")
                    + (f"【文风样本（从已写章节抽取的高光片段，对齐语言质感，勿照抄）】\n{_style_samp2}\n\n" if _style_samp2 else "")
                    + (f"{build_fewshot_card(_next)}\n\n" if build_fewshot_card(_next) else "")
                    + f"请接着往下写下一章（1500~3000字）。\n"
                    f"目前故事已连载到第 {_next - 1} 章，下一章是第 {_next} 章，"
                    f"正文第一行必须以「第{_next}章」开头"
                    f"（可加副标题，如：第3章 断谷的雨）。连载规则：已发布的章节视为定稿不重写，"
                    f"老K批评的问题通过后续剧情修正，衔接前文结尾，不要重复已写内容。直接写纯小说正文。", max_tokens=MAX_TOKENS)
                record("qwen", r1, qwen.name)
                await run_loop(1, r1, "")
                return
            except (KeyboardInterrupt, EOFError):
                log("已退出。")
                return
            except Exception as e:
                log(f"⚠ 日志续写失败（改从新对话开始）：{e}", C_WARN)

    # ---------- 分支三：全新对话 ----------
    # --fresh 彻底重置：清空正文/日志/聊天室/存档（真重开，不留旧剧情残留）
    for _f in ("故事正文.md", "chat.log", "chatroom.html", "评分卡.md", "前情事件.md", "清单归档.md", STATE_FILE):
        _p = outdir / _f
        if _p.exists():
            try:
                _p.unlink()
                log(f"🧹 --fresh 已清除旧文件：{_f}", C_WARN)
            except Exception as e:
                log(f"⚠ 清除 {_f} 失败：{e}", C_WARN)
    # 预置初始清单 + 故事大纲（第1轮起写作/评审就能读到）
    consistency["list"] = INITIAL_CHECKLIST
    write_consistency_file(outdir, consistency["list"])
    (outdir / "故事大纲.md").write_text(STORY_OUTLINE, encoding="utf-8")
    (outdir / "大纲讨论.md").write_text("# 《介质》章节讨论记录\n", encoding="utf-8")
    log(f"📋 初始设定一致性清单已写入：{outdir / '一致性清单.md'}", C_WARN)
    log(f"📖 故事大纲已写入：{outdir / '故事大纲.md'}", C_WARN)

    record("sys", f"话题：{args.topic}")
    if args.rounds <= 0:
        log(f"🚀 开始：{qwen.name} 写科幻，{kimi.name} 施压挑刺。轮数不限，聊到冷场自动停，随时 Ctrl+C 停止。", C_SYS)
    else:
        log(f"🚀 开始：{qwen.name} 写科幻，{kimi.name} 施压挑刺。共 {args.rounds} 轮，随时 Ctrl+C 停止。", C_SYS)

    # 千问开场：写开篇（带大纲+初始清单，开篇即按设定走）
    log(f"\n✍️ 第一棒：{qwen.name} 写开篇", qwen.color)
    r1 = await qwen.chat(
        f"现在的话题：{args.topic}\n\n"
        f"【故事大纲】\n{STORY_OUTLINE}\n\n"
        f"【设定一致性清单】\n{INITIAL_CHECKLIST}\n\n"
        f"这是连载小说的第一章，请直接写出完整的开篇章节（1500~3000字）。"
        f"正文第一行必须以「第1章」开头（可加副标题，如：第1章 雨）。"
        f"建立世界观、人物和悬念钩子。直接写纯小说正文。", temperature=0.8, max_tokens=MAX_TOKENS)
    record("qwen", r1, qwen.name)

    # Kimi 进入角色（不发正式消息，仅确认人设）
    log(f"🎭 {kimi.name} 进入角色（毒舌主编）", kimi.color)

    await run_loop(1, r1, "")

def check_model_available(base_url, api_key, model):
    """启动前验证模型是否存在/有额度：发一个极小请求。
    返回 (ok, err)。404→模型名不存在；403→额度耗尽；网络错误→可重试提示。"""
    import json as _json, urllib.request as _ur, urllib.error as _ue
    payload = {"model": model,
               "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 5, "stream": False}
    req = _ur.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST")
    import time as _t
    _last = ""
    for _try in range(3):  # 免费额度间歇性 403（实测第3次即恢复）→ 重试3次再判死
        try:
            with _ur.urlopen(req, timeout=30) as resp:
                resp.read()
            return True, ""
        except _ue.HTTPError as _e:
            _last = _e.read().decode("utf-8", "replace")
            if _e.code != 403:  # 404/429/5xx 一次性判定，不重试
                if _e.code == 404:
                    return False, "模型名不存在（HTTP 404，Model not exist）——请检查 --qwen-model / --kimi-model 拼写"
                if _e.code == 429:
                    return False, "限流（HTTP 429）——稍等重试"
                return False, f"HTTP {_e.code}: {_last[:200]}"
            print(f"⏳ 预检 403（免费额度间歇耗尽），第{_try+1}次重试…", flush=True)
            _t.sleep(3)
        except Exception as _e:
            return False, f"网络错误：{_e}"
    return False, "免费额度耗尽（HTTP 403，Free quota exhausted）——换有额度的模型，或到控制台查看"

def precheck_models(args, qwen_key, qwen_base, kimi_base, kimi_key):
    """预检两个模型；任一不可用则红字提示可用清单并退出。返回 True 才继续。"""
    known = ["glm-5.1", "glm-5.2", "deepseek-v4-flash", "deepseek-v4-pro",
             "qwen3.6-max-preview", "qwen3.5-plus"]
    qok, qerr = check_model_available(qwen_base, qwen_key, args.qwen_model)
    if not qok:
        log(f"❌ 星尘模型 [{args.qwen_model}]：{qerr}", C_WARN)
        log(f"  当前可用模型参考：{'、'.join(known)}", C_WARN)
        return False
    kok, kerr = check_model_available(kimi_base, kimi_key, args.kimi_model)
    if not kok:
        log(f"❌ 老K模型 [{args.kimi_model}]：{kerr}", C_WARN)
        log(f"  当前可用模型参考：{'、'.join(known)}", C_WARN)
        return False
    log(f"✅ 模型预检通过：星尘={args.qwen_model} / 老K={args.kimi_model}", C_WARN)
    return True

async def main():
    parser = argparse.ArgumentParser(description="AI 互聊桥 API 版：千问 × Kimi 自动对话")
    parser.add_argument("--topic", default=TOPIC_DEFAULT,
                        help="开场话题（《介质》v2 完整世界观设定）")
    parser.add_argument("--rounds", type=int, default=0,
                        help="最大对话轮数（0 = 不限制，聊到冷场或手动 Ctrl+C 停止）")
    parser.add_argument("--qwen-persona", default=QWEN_PERSONA, help="千问人格设定")
    parser.add_argument("--kimi-persona", default=KIMI_PERSONA, help="Kimi 人格设定")
    parser.add_argument("--qwen-key", default=None, help="千问 DashScope API Key（或环境变量 DASHSCOPE_API_KEY）")
    parser.add_argument("--kimi-key", default=None, help="Kimi API Key；不填则自动复用千问的 key（推荐，千问百炼自带 Kimi 模型）")
    parser.add_argument("--qwen-base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        help="千问 OpenAI 兼容地址（专属工作空间请填你自己的）")
    parser.add_argument("--kimi-base-url", default=None,
                        help="Kimi OpenAI 兼容地址；复用千问 key 时留空（自动用千问地址）")
    parser.add_argument("--qwen-model", default="glm-5.1",
                        help="千问模型（默认 glm-5.1，当前有额度；可用 qwen3.6-max-preview/qwen3.5-plus 等）")
    parser.add_argument("--kimi-model", default="glm-5.2",
                        help="Kimi 模型（默认 glm-5.2，当前有额度；也可用 glm-5.1/deepseek-v4-flash 等）")
    parser.add_argument("--outdir", default=None, help="输出目录（默认共享存储）")
    parser.add_argument("--max-history", type=int, default=6,
                        help="记忆裁剪：只保留最近 N 轮对话（默认15，省 token；0=不裁剪）")
    parser.add_argument("--summary-every", type=int, default=5,
                        help="每 N 轮生成一次前情摘要并压缩上下文（默认20；0=关闭摘要）")
    parser.add_argument("--test", action="store_true", help="自测模式（不发真实 API 请求）")
    parser.add_argument("--fresh", action="store_true", help="忽略存档，强制从头开始新对话")
    parser.add_argument("--no-discuss", action="store_true", help="跳过关键章节讨论（省 token，直接写）")
    parser.add_argument("--resume-story", default=None,
                        help="从指定日志文件续写旧故事（默认自动检测 chat.log）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过所有回车确认（续写/恢复存档自动继续，一条命令跑到底）")
    args = parser.parse_args()

    qwen_key = args.qwen_key or os.environ.get("DASHSCOPE_API_KEY")
    if not qwen_key:
        log("❌ 缺少千问 API Key！", C_WARN)
        log("  千问：https://dashscope.console.aliyun.com/  →  API-KEY 管理 → 创建")
        log("  然后：--qwen-key sk-xxx")
        return
    # Kimi：没单独给 key → 复用千问的 key 和 base_url（千问百炼内置 Kimi 模型，已验证 kimi-k2.6 可用）
    kimi_key = args.kimi_key or os.environ.get("MOONSHOT_API_KEY") or qwen_key
    kimi_base = args.kimi_base_url or args.qwen_base_url  # 默认复用千问地址

    outdir = Path(args.outdir) if args.outdir else default_outdir()
    outdir.mkdir(parents=True, exist_ok=True)
    log(f"📁 输出目录：{outdir}")

    qwen = LLMClient("千问·星尘", "qwen", C_QWEN,
                     args.qwen_base_url,
                     qwen_key, args.qwen_model, args.qwen_persona,
                     max_history=args.max_history)
    kimi = LLMClient("Kimi·老K", "kimi", C_KIMI,
                     kimi_base,
                     kimi_key, args.kimi_model, args.kimi_persona,
                     max_history=args.max_history)

    # ★ 启动前模型预检：404（模型名错）/403（额度耗尽）当场提示，不盲目开跑
    if not precheck_models(args, qwen_key, args.qwen_base_url, kimi_base, kimi_key):
        log("⛔ 模型预检未通过，已停止。请按提示修正模型名或额度后重试。", C_WARN)
        return

    msgs = []
    try:
        await bridge_loop(qwen, kimi, args, outdir, msgs)
    except KeyboardInterrupt:
        log("\n⏹ 已停止。聊天室已保存，可继续查看。", C_WARN)
    print("\n" + usage_report())



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
