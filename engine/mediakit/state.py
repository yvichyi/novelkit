#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态持久化：会话保存/加载/删除 + checklist 合并（从 ai_bridge_api.py 拆分，原文未改）。"""
import json
import os
import re
from .config import C_WARN, STATE_FILE, log
from .story import similarity


def save_state(outdir, qwen, kimi, i, last_qwen, last_kimi, msgs, consistency="", outline="", consensus="", anchor_state=None, rhythm=None):
    """保存对话状态（每轮后调用），支持 Ctrl+C 后续聊"""
    try:
        state = {
            "round": i,
            "last_qwen": last_qwen,
            "last_kimi": last_kimi,
            "qwen_history": qwen.history,
            "kimi_history": kimi.history,
            "msgs": msgs[-50:],  # 聊天室只需最近50条（html 会渲染全部 msgs，这里只做备份）
            "consistency": consistency,  # 设定一致性清单（续聊/换API不丢）
            "outline": outline,          # 故事大纲
            "consensus": consensus,      # 本章共识（讨论定稿）
            "anchor_state": anchor_state,  # 主线进度状态机（当前锚+本锚章数）
            "rhythm": rhythm,              # 节奏计数器
        }
        (outdir / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"⚠ 存档失败：{e}", C_WARN)

def parse_summary_package(text):
    """从 AI 输出的摘要包中拆出【前情摘要】和【设定一致性清单】两区块"""
    text = (text or "").strip()
    m = re.search(r"【设定一致性清单】", text)
    if not m:
        return text, ""
    summary = text[:m.start()].strip()
    checklist = text[m.start():].strip()
    summary = re.sub(r"^【前情摘要】\s*", "", summary).strip()
    return summary, checklist

def extract_checklist_delta(review):
    """从评审中提取【清单更新】/## 清单更新 区块（新增/修正的设定条目）"""
    m = re.search(r"(?:【清单更新】|#{1,4}\s*清单更新)\s*(.*?)(?:\n\n|\Z)", review or "", re.S)
    if not m:
        return ""
    delta = m.group(1).strip()
    if not delta or delta in ("无", "无。", "暂无", "没有"):
        return ""
    return delta

def parse_discuss_signal(review):
    """从评审中提取【讨论建议】/## 讨论建议 区块：下一章是否需要讨论。
    兼容多种输出风格：『需要讨论』『直接写』『- 需要讨论：…』『直接写：…』"""
    m = re.search(r"(?:【讨论建议】|#{1,4}\s*讨论建议)\s*[-—:：]?\s*(需要讨论|直接写)", review or "")
    if not m:
        return False  # 默认直接写，保守
    return m.group(1) == "需要讨论"

def extract_consensus(verdict):
    """从评审共识审核中提取【共识达成】区块（本章定稿计划）"""
    m = re.search(r"【共识达成】\s*(.*?)(?:\n\n|\Z)", verdict or "", re.S)
    if not m:
        return ""
    return m.group(1).strip()

def append_discussion(outdir, round_no, text):
    """把讨论过程追加到 大纲讨论.md（手机可见）"""
    try:
        with open(outdir / "大纲讨论.md", "a", encoding="utf-8") as f:
            f.write(f"\n\n--- 讨论轮 {round_no} ---\n{text}")
    except Exception as e:
        log(f"⚠ 讨论记录写入失败：{e}", C_WARN)

def merge_checklist(current, delta):
    """把评审的清单更新合并进主清单：逐条去重、保留顺序"""
    current = (current or "").strip()
    delta = (delta or "").strip()
    if not delta:
        return current
    if not current:
        return delta
    cur_lines = [l for l in current.split("\n") if l.strip()]
    new_lines = [l.strip() for l in delta.split("\n") if l.strip() and l.strip() != "-"]
    seen = set(cur_lines)
    for line in new_lines:
        # 以"- "开头的内容条目
        item = line[2:].strip() if line.startswith("- ") else line.strip()
        # 伏笔回收：- 【伏笔✅】xxx 已回收@第X章 → 替换同主题的旧 - 【伏笔】xxx（未回收）
        if "【伏笔✅】" in item and "已回收" in item:
            topic = item.split("已回收")[0].replace("【伏笔✅】", "").strip()
            old_key = f"- 【伏笔】{topic}"
            if old_key in cur_lines:
                cur_lines = [old_key if l == old_key else l for l in cur_lines]  # 占位
            # 删除旧的未回收条目（同主题模糊匹配）
            cur_lines = [l for l in cur_lines
                         if not (l.startswith("- 【伏笔】") and topic[:8] in l and "已回收" not in l)]
            # 把回收标记追加到伏笔区
            if item not in seen:
                cur_lines.append("- " + item)
                seen.add(item)
            continue
        # 修正指令（含"改为"/"修正为"）总是追加，且替换旧的对应条目
        if "改为" in item or "修正为" in item or "不再" in item:
            # 简单处理：追加到末尾并标记（后续同义去重交给下一次摘要）
            if item not in seen:
                cur_lines.append("- " + item)
                seen.add(item)
        elif item not in seen:
            # B3 相似度去重：与已有条目相似>0.85 视为同条，保留新版（替换旧条，防清单膨胀）
            replaced = False
            for ci, cl in enumerate(cur_lines):
                l_item = cl[2:].strip() if cl.startswith("- ") else cl.strip()
                if l_item and similarity(item, l_item) > 0.85:
                    cur_lines[ci] = "- " + item
                    replaced = True
                    break
            if not replaced:
                cur_lines.append("- " + item)
                seen.add(item)
    return "\n".join(cur_lines)

def write_consistency_file(outdir, checklist):
    """把设定一致性清单单独写成 md 文件，供随时查看"""
    try:
        (outdir / "一致性清单.md").write_text(
            checklist or "（暂无清单，连载数章后自动生成）", encoding="utf-8")
    except Exception as e:
        log(f"⚠ 清单写入失败：{e}", C_WARN)

def load_state(outdir):
    """读取存档；没有则返回 None"""
    p = outdir / STATE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def delete_state(outdir):
    try:
        (outdir / STATE_FILE).unlink(missing_ok=True)
    except Exception:
        pass

