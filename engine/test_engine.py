#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用引擎回归测试（零依赖，不联网，不花钱）

用法：python3 test_engine.py（在 engine/ 目录下）

覆盖：
  1. 常量完整性：零配置加载不崩
  2. 完本纪律：planned_total / completion_status
  3. 宽松时间正则：自然时间表达可匹配
  4. 结局卡：前 3/4 不注入，后 1/4 注入
  5. 逐章锚点：chapter_events.md 解析回填 ANCHOR_KEYWORDS
  6. 分卷导出：epub 生成器按卷分组目录
  7. 门禁不崩：_quality_gate 对通用文本不误伤
"""
import os
import re
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mediakit.config as mc
import mediakit.cards as mc2
import write_chapter as w

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def t1_constants():
    print("\n[1] 常量完整性（零配置）")
    check("STAGE_ANCHORS 是 list", isinstance(mc.STAGE_ANCHORS, list))
    check("ANCHOR_KEYWORDS 是 dict", isinstance(mc.ANCHOR_KEYWORDS, dict))
    check("TIME_ANCHOR_RE_LOOSE 可用", hasattr(mc, "TIME_ANCHOR_RE_LOOSE"))
    check("STAGE_ANCHORS 加载不崩", mc.STAGE_ANCHORS is not None)


def t2_completion():
    print("\n[2] 完本纪律")
    total = w.planned_total()
    check("planned_total 返回数字或 None", total is None or isinstance(total, int))
    comp = w.completion_status()
    check("completion_status 返回四元组", isinstance(comp, tuple) and len(comp) == 4)


def t3_time_re():
    print("\n[3] 宽松时间正则（新书不误杀自然时间表达）")
    r = mc.TIME_ANCHOR_RE_LOOSE
    check("匹配「凌晨四点」", bool(r.search("凌晨四点，他出发")))
    check("匹配「第3天」", bool(r.search("第3天，他醒了")))
    check("匹配「次日」", bool(r.search("次日清晨，城市安静")))
    check("匹配「暮色」", bool(r.search("暮色四合")))
    check("不匹配纯动作", not r.search("他推开门走了出去"))


def t4_ending():
    print("\n[4] 结局收束卡")
    # 用零配置（无 STAGE_ANCHORS → total=999）验证前 1/4 空
    c1 = mc2.build_ending_card(100)
    check("前1/4不注入", c1 == "")
    c2 = mc2.build_ending_card(900)
    check("后1/4注入", bool(c2))


def t5_anchor_parse():
    print("\n[5] 逐章锚点解析（chapter_events.md）")
    tmp = tempfile.mkdtemp()
    nc = Path(tmp) / "novel_config"
    nc.mkdir(parents=True)
    (nc / "chapter_events.md").write_text(
        "第1章|暗号、影子|发现异常\n第2章|消散者|城市恐慌\n", encoding="utf-8")
    (nc / "anchors.json").write_text(
        json.dumps({"ANCHOR_KEYWORDS": {}, "STAGE_ANCHORS": [["开端", 1, 1, 1, 10]]}),
        encoding="utf-8")
    saved = os.environ.get("NOVEL_CONFIG_DIR")
    os.environ["NOVEL_CONFIG_DIR"] = str(nc)
    import importlib
    importlib.reload(mc)
    importlib.reload(mc2)
    check("解析到第1章关键词", mc.ANCHOR_KEYWORDS.get(1) == ["暗号", "影子"], f"{mc.ANCHOR_KEYWORDS}")
    check("解析到第2章关键词", mc.ANCHOR_KEYWORDS.get(2) == ["消散者"])
    if saved:
        os.environ["NOVEL_CONFIG_DIR"] = saved
    else:
        os.environ.pop("NOVEL_CONFIG_DIR", None)
    importlib.reload(mc)
    importlib.reload(mc2)


def t6_epub_volume():
    print("\n[6] 分卷 epub 生成")
    try:
        from tools_gen_epub import build as epub_build
    except Exception as e:
        check("tools_gen_epub 可导入", False, str(e))
        return
    tmp = tempfile.mkdtemp()
    md = Path(tmp) / "test.md"
    md.write_text("# 卷1：开端\n\n## 第1章 开始\n\n正文一\n\n# 卷2：发展\n\n## 第11章 发展\n\n正文二\n",
                  encoding="utf-8")
    ep = Path(tmp) / "test.epub"
    epub_build(str(md), str(ep), book_title="测试书")
    import zipfile
    z = zipfile.ZipFile(str(ep))
    nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
    check("分卷目录含卷1", "卷1：开端" in nav)
    check("分卷目录含卷2", "卷2：发展" in nav)
    check("两章都在", nav.count("<li><a href") >= 2)
    z.close()


def t7_gate():
    print("\n[7] 质量门禁（通用文本不误伤）")
    import mediakit.story as st
    good = "## 第1章 开始\n\n清晨，林屿推开窗，城市还在沉睡。"
    issues = st._quality_gate(Path("."), good, Path(".") / "none.md", anchor_no=1)
    fatal = [i for i in issues if "熔断" in i]
    check("通用文本无熔断", len(fatal) == 0, f"{fatal}")


def t8_foreshadow():
    print("\n[8] 伏笔分级管控（解析/锚绑定/注入）")
    from mediakit import foreshadow as fh
    fake_ledger = (
        "# 伏笔账本\n\n"
        "| ID | 内容 | 埋设章 | 计划回收 | 状态 | 关联 |\n"
        "|:---|:---|:---|:---|:---|:---|\n"
        "| V-01 | 会算的陌生人 | 第1章 | 中期 | 已埋 | 主线威胁 |\n"
        "| V-02 | 铁锈红天空 | 第1章 | 后期 | 已回收 | 大气被改 |\n\n"
        "## 第5章 伏笔登记\n"
        "- 营地纸片「走」｜级别A｜主线｜短中期\n"
        "- 哨音人（级别B·预计短中期回收）\n\n"
        "## 支线池（暂不回收，回收时转主表）\n"
        "- 收音机杂音｜级别C｜支线｜3章内\n"
    )
    parsed = fh.parse_ledger(fake_ledger)
    main_names = [x["content"] for x in parsed["main"]]
    check("解析：主线2条（表格+登记）", "会算的陌生人" in main_names and "营地纸片「走」" in main_names, f"{main_names}")
    check("解析：旧格式登记也识别", "哨音人" in main_names, f"{main_names}")
    check("解析：支线入池", any("收音机杂音" in x["content"] for x in parsed["side"]), f"{parsed['side']}")
    check("解析：已回收识别", any("V-02" in x["content"] for x in parsed["recovered"]))
    outline = ("◆ 锚3 成长（第5~15章）：xxx\n- 回收伏笔：V-01\n\n"
               "◆ 锚4 冲击（第10~20章）：xxx\n- 回收伏笔：V-01, 营地纸片「走」\n")
    rec_map = dict(fh.current_anchor_recovery(outline, 12))
    check("锚绑定：第12章命中锚3+锚4", rec_map.get("V-01") == (3, 4) and rec_map.get("营地纸片「走」") == (4,), f"{rec_map}")
    card = fh.build_foreshadow_card(fake_ledger, fh.current_anchor_recovery(outline, 12))
    check("注入：含应收网", "应收网" in card and "会算的陌生人" in card and "必须回收" in card)
    check("注入：含主线TOP", "未回收主线伏笔" in card)
    check("注入：支线只给统计", "1 条支线伏笔未回收" in card, f"{card[-120:]}")
    # 8.5 集成：write_chapter.foreshadow_card（临时书目录）
    import write_chapter as w
    td = Path(tempfile.mkdtemp())
    (td / "novel_config").mkdir(parents=True)
    (td / "ledger").mkdir()
    (td / "ledger" / "伏笔账本.md").write_text(fake_ledger, encoding="utf-8")
    (td / "novel_config" / "outline.md").write_text(outline, encoding="utf-8")
    (td / "novel_config" / "world_setting.md").write_text("世界观：测试", encoding="utf-8")
    saved = os.environ.get("NOVEL_DIR")
    os.environ["NOVEL_DIR"] = str(td)
    os.environ["NOVEL_CONFIG_DIR"] = str(td / "novel_config")
    import importlib
    importlib.reload(w)   # NOVEL_DIR 在 import 时读取，必须重载才生效
    try:
        c = w.foreshadow_card(12)
        check("集成：新书模式读到应收网", "应收网" in c and "V-01" in c, f"{c[:120]}")
    finally:
        if saved:
            os.environ["NOVEL_DIR"] = saved
        else:
            os.environ.pop("NOVEL_DIR", None)
        os.environ.pop("NOVEL_CONFIG_DIR", None)
        importlib.reload(w)


def main():
    print("=" * 56)
    print("  novelkit-engine golden test (通用版)")
    print("=" * 56)
    t1_constants()
    t2_completion()
    t3_time_re()
    t4_ending()
    t5_anchor_parse()
    t6_epub_volume()
    t7_gate()
    t8_foreshadow()
    print("\n" + "=" * 56)
    print(f"  结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 56)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
