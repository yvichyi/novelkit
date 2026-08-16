#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型层：LLMClient/MockLLM + Token 用量记账（从 ai_bridge_api.py 拆分，原文未改）。"""
import asyncio
import json
import urllib.request
import urllib.error
from .config import DEEPSEEK_PRICE, MAX_TOKENS, REASONING_EFFORT, price_for


_usage = {"cache_hit": 0, "cache_miss": 0, "output": 0, "cost": 0.0, "calls": 0, "by_model": {}}

def record_usage(model, usage):
    """累计一次调用的 token 用量与费用（缓存命中/未命中/输出）"""
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens", 0) or 0
    out = usage.get("completion_tokens", 0) or 0
    price = price_for(model)
    cost = 0.0
    if price:
        cost = (hit * price["cache_hit"] + miss * price["cache_miss"] + out * price["output"]) / 1_000_000
    _usage["cache_hit"] += hit
    _usage["cache_miss"] += miss
    _usage["output"] += out
    _usage["cost"] += cost
    _usage["calls"] += 1
    m = _usage["by_model"].setdefault(model, {"cache_hit": 0, "cache_miss": 0, "output": 0, "cost": 0.0, "calls": 0})
    m["cache_hit"] += hit
    m["cache_miss"] += miss
    m["output"] += out
    m["cost"] += cost
    m["calls"] += 1

def usage_report():
    """返回累计 token 用量与费用报告（多行文本）"""
    lines = [
        f"💰 Token 用量与费用（累计 {_usage['calls']} 次调用）",
        f"   缓存命中 {_usage['cache_hit']:,} tok｜缓存未命中 {_usage['cache_miss']:,} tok｜输出 {_usage['output']:,} tok｜合计费用 ¥{_usage['cost']:.4f}",
    ]
    for model in sorted(_usage["by_model"]):
        m = _usage["by_model"][model]
        lines.append(f"   · {model}：命中 {m['cache_hit']:,} / 未命中 {m['cache_miss']:,} / 输出 {m['output']:,} tok｜¥{m['cost']:.4f}（{m['calls']} 次）")
    return "\n".join(lines)

class LLMClient:
    """OpenAI 兼容接口客户端（零依赖，仅用 Python 标准库；千问 / Kimi 都支持）"""

    def __init__(self, name, who, color, base_url, api_key, model, persona, max_history=10):
        self.name = name
        self.who = who
        self.color = color
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.persona = persona
        self.max_history = max_history
        self.history = [{"role": "system", "content": persona}]
        self.last_usage = None  # 最近一次调用的 token 用量（v2.8）

    def _trim_history(self):
        """记忆裁剪：保留 system + 最近 max_history 轮（每轮=1条user+1条assistant）"""
        if self.max_history <= 0:
            return
        max_msgs = 1 + self.max_history * 2  # system + N轮
        if len(self.history) > max_msgs + 2:  # 留点余量再裁
            keep = [self.history[0]] + self.history[-(max_msgs - 1):]
            # 在裁剪处插入占位提示，让 AI 知道前面聊过但被精简了
            self.history = [keep[0]] + [{"role": "user", "content": "（前情已压缩省略，继续当前话题，不要重复已写过的内容）"}] + keep[1:]

    def _call(self, temperature, max_tokens, thinking=None):
        """流式调用 OpenAI 兼容接口（SSE 实时输出）；无超时；断连自动断点续写重试"""
        print(f"\n⏳ {self.name} 开始生成（流式实时输出）…\n", flush=True)
        payload_base = {
            "model": self.model,
            "messages": self.history,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},  # v2.8：返回 token 用量（缓存命中/未命中/输出）
            "reasoning_effort": REASONING_EFFORT,  # 思考强度：low=快省成本（写作执行任务够用）
        }
        if thinking:
            payload_base["thinking"] = {"type": thinking}  # disabled=关闭思考（评审/结构化任务）
            if thinking == "disabled":
                payload_base.pop("reasoning_effort", None)  # 思考已关就不发思考强度，避免 API 冲突/报错
        last_err = None
        prefix = ""  # 断连前已收到的内容（断点续写用，不白费）
        last_payload = None
        for attempt in range(4):  # 首次 + 3 次重试（403 间歇性额度耗尽也重试）
            payload = dict(payload_base)
            if prefix:
                # 断点续写：把已收内容作为 assistant 消息注入，让 AI 接着写
                payload["messages"] = self.history + [
                    {"role": "assistant", "content": prefix},
                    {"role": "user", "content": "（以上是你已写的内容，请直接从断点继续完成，不要重复已写内容）"},
                ]
            last_payload = payload
            if prefix:
                print(f"\n↻ 断点续写：已保留 {len(prefix)} 字，继续生成…", flush=True)
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            collected = []
            last_usage = None
            try:
                import time as _t
                _t0 = _t.time()
                thinking = True  # 思考阶段标志
                print(f"🧠 {self.name} 思考中…（写作前会先构思，约0.5~2分钟，请勿关闭）", flush=True)
                with urllib.request.urlopen(req, timeout=None) as resp:  # 无超时：一章写多久都等
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            if chunk.get("usage"):
                                last_usage = chunk["usage"]  # 记录 usage（最后一个 chunk 携带）
                            if not chunk.get("choices"):
                                continue  # 结束包（choices 为空）跳过
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                if thinking:
                                    thinking = False
                                    print(f"\n✨ {self.name} 构思完成，开始写正文：\n", flush=True)
                                collected.append(delta)
                                print(delta, end="", flush=True)
                            else:
                                # 思考中：每5秒打一个心跳点，证明进程活着
                                if thinking and _t.time() - _t0 >= 5:
                                    print(".", end="", flush=True)
                                    _t0 = _t.time()
                        except Exception:
                            continue
                print("\n", flush=True)
                text = "".join(collected)
                if last_usage:
                    record_usage(self.model, last_usage)
                    self.last_usage = last_usage
                else:
                    # deepseek-v4 流式不返回 usage：用非流式探针补一次精确用量（同一 prompt，max_tokens=1，
                    # 缓存命中后成本≈0.0002元；失败则 usage 保持 None，由调用方显示"不可用"）
                    self.last_usage = self._probe_usage(last_payload)
                    if self.last_usage:
                        record_usage(self.model, self.last_usage)
                if not text.strip():
                    raise RuntimeError("流式响应为空")
                return (prefix + text).strip()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                # HTTP 4xx（鉴权/参数错）不重试；5xx/429（限流/服务端）/403（免费额度间歇耗尽，实测可恢复）重试
                if e.code < 500 and e.code != 429 and e.code != 403:
                    raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
                last_err = f"HTTP {e.code}: {body[:200]}"
            except Exception as e:
                last_err = str(e)
            # 失败：保留已收内容作为断点续写前缀（收到>200字才续写，否则全重试）
            new_prefix = prefix + "".join(collected)
            if len(new_prefix) >= 200:
                prefix = new_prefix
            if attempt < 3:
                _wait = 15 if "403" in (last_err or "") else 5  # 403 间歇额度耗尽等久一点
                print(f"\n⚠️ 网络中断（{last_err}，已收 {len(prefix)} 字），{_wait}秒后重试第 {attempt+2} 次…", flush=True)
                import time as _t
                _t.sleep(_wait)
        raise RuntimeError(f"网络请求失败（重试4次仍失败，已保留 {len(prefix)} 字）: {last_err}")

    def _probe_usage(self, payload):
        """非流式用量探针：deepseek-v4 流式不返回 usage，用同一 prompt 发一个 max_tokens=1 的非流式请求，
        精确拿到 prompt_cache_hit/miss。缓存命中后 prompt 部分极便宜（≈0.02元/M），失败返回 None 不阻塞。"""
        if not payload:
            return None
        try:
            probe = dict(payload)
            probe["stream"] = False
            probe["max_tokens"] = 1
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(probe, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            return d.get("usage") or None
        except Exception:
            return None

    def reset_context_with_summary(self, summary, checklist=""):
        """摘要压缩：清空上下文，只保留人设 + 前情摘要 + 设定一致性清单（记忆不丢，token 大减）"""
        content = f"（前情摘要）{summary}"
        if checklist:
            content += f"\n\n【设定一致性清单（写作查对基准，已确立设定不可自相矛盾）】\n{checklist}"
        content += "\n\n请基于以上剧情继续，不要重复已写内容。"
        self.history = [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": content},
            {"role": "assistant", "content": "好的，我已记住前情摘要与设定一致性清单，将继续推进。"},
        ]

    async def chat(self, text, temperature=0.9, max_tokens=MAX_TOKENS, thinking=None):
        """发一条消息，返回 AI 回复；自动维护对话历史 + 记忆裁剪。
        thinking: None=默认思考模式（跟随 API）；'disabled'=关闭思考（结构化任务/短输出用，快且不空）；'enabled'=强制思考"""
        self._trim_history()  # 发请求前先裁剪，省 token
        self.history.append({"role": "user", "content": text})
        try:
            reply = await asyncio.to_thread(self._call, temperature, max_tokens, thinking)
            self.history.append({"role": "assistant", "content": reply})
            return reply
        except Exception:
            # 出错时回退历史（避免污染上下文）
            self.history.pop()
            raise

    async def reset_with_persona(self, persona, opening=None):
        """换人格（比如开场注入话题时）"""
        self.persona = persona
        self.history = [{"role": "system", "content": persona}]
        if opening:
            return await self.chat(opening, max_tokens=MAX_TOKENS)
        return None

class MockLLM:
    """离线测试用：模拟 AI 回复（不发真实 API），验证全流程逻辑无 bug"""

    def __init__(self, name, who, color, base_url, api_key, model, persona, max_history=10):
        self.name = name
        self.who = who
        self.color = color
        self.persona = persona
        self.history = [{"role": "system", "content": persona}]
        self._call_count = 0

    def reset_context_with_summary(self, summary, checklist=""):
        """Mock：模拟摘要压缩后的上下文重置"""
        self.history = [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": f"（前情摘要）{summary}\n\n【设定一致性清单】\n{checklist}"},
            {"role": "assistant", "content": "好的，我已记住前情摘要与设定一致性清单，将继续推进。"},
        ]

    async def reset_with_persona(self, persona, opening=None):
        """换人格（mock）"""
        self.persona = persona
        self.history = [{"role": "system", "content": persona}]
        if opening:
            return await self.chat(opening, max_tokens=MAX_TOKENS)
        return None

    async def chat(self, text, temperature=0.9, max_tokens=MAX_TOKENS):
        self._call_count += 1
        # 按 who 返回模拟内容，覆盖主流程需要的所有区块（通用测试桩，不含任何具体作品设定）
        if self.who == "qwen":
            if "是否认同" in text or ("同意重写" in text and "重写" in text):
                return "【同意重写】评审点名的硬伤属实，该章确实需要推翻，认同重写。"
            if "请重写第" in text:
                return ("重写后的第一章（修正版）：主角在雨夜中第一次确认世界可以被改写，"
                        "但这次他没有像上次那样直接动手——他先算清了代价。"
                        "每一项改变都对应一处可追踪的成本，改完的瞬间他胸腔发闷，像被抽走了一口气。"
                        "他想起床底硬盘里拷贝的离线资料，盘算着怎么把这段推演记进系统，"
                        "决定以后每一次改写都先算账。窗外的雨幕偏移，街角有人抬头看了一眼又低头走开。"
                        "他站在窗后想：要不要继续存在？答案是，想。那就得先学会还债。")
            if "总结到目前为止" in text or "【前情摘要】" in text:
                return ("【前情摘要】\n主角确认能改写现实，第1章。"
                        "\n\n【设定一致性清单】\n- 主角：程序员\n- 核心设定：改写现实需付代价")
            if "本章写作计划" in text or "答辩" in text:
                return ("采纳：本章推进关键角色登场。\n【本章写作计划】"
                        "本章目标：主角初遇关键角色；关键场景：雨夜；结尾钩子：异常天气。")
            # 写正文（开篇/回应评审都返回长文）——先匹配写作指令，避免"评审"关键词误伤
            if "写出下一章" in text or "写纯小说正文" in text or "开篇章节" in text or "第一章" in text:
                return ("第1章。雨下到第三分钟，主角才第一次确认：天上那朵云，是他改的。"
                        "没有弹窗，没有提示音。环境从不给用户任何体面的反馈，它只把结果砸进现实。"
                        "窗外，雨滴在玻璃外忽然慢了半拍，像被临时修正了阻力系数，随后又若无其事地坠落。"
                        "街上的行人没有抬头，也没人敢喊一句这雨是谁改的。这种好奇心通常活不过下一场异常。"
                        "网断了，手机只剩最后一批离线新闻，窗外零落的街道上偶尔闪过匆忙的影子，"
                        "他想起床底那块硬盘里拷贝的离线资料还没读完。主角站在窗后，手里还捏着半包受潮饼干。"
                        "十分钟前，他只是把一场普通阵雨拆到不能再拆：水汽条件、气流速度、"
                        "云层温度、风向切变，再把它们一项项压进推演链。他像写代码一样删掉所有'大概'，"
                        "像气象爱好者一样补上边界条件，固执地相信模型必须先于现实跑通。然后，世界接受了。"
                        "没有权限申请，没有密码，也没有日志。空气里只留下一点极淡的燥味，像有人悄悄欠了热力学一笔。"
                        "他盘算着代价的落点，觉得值不值先不说，总得先活下去。"
                        "主角看着云缝裂开，雨幕偏向不该偏的方向，忽然意识到，世界真的开始运行在他写下的那几行推演里了。"
                        "他第一次认真地想：要不要继续存在？答案是，想。那就得活下去。")
            if "原文：" in text and "改为：" in text:
                return "【同意修改】评审点名的硬伤属实，认同修改。"
            if "评审" in text:
                return "这是作者刚写的最新章节的评审。"
            return "这是默认回复，用于未匹配的分支。"
        else:  # kimi
            if "共识审核" in text or "审核" in text:
                return ("同意。\n【共识达成】本章目标：主角初遇关键角色；关键场景：雨夜；结尾钩子：异常天气。"
                        "\n\n【清单更新】\n- 关键角色：第1章登场")
            if "讨论" in text and "第" in text and "轮" in text:
                return ("问题1：关键角色动机是否可信？建议：让她为线索而来。"
                        "\n问题2：本章要不要展示代价？建议：雨夜改写留代价痕迹。")
            if "答辩" in text or "写作计划" in text:
                return ("采纳问题1。\n【本章写作计划】目标：初遇关键角色。")
            return ("## 致命伤\n节奏稍慢。\n## 修改指令\n下章加快推进。\n"
                    "【讨论建议】需要讨论 新角色登场需先敲定动机\n"
                    "【要求重写】无\n"
                    "【直接修改】无\n"
                    "【清单更新】无")

