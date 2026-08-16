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
    print("\n" + "=" * 56)
    print(f"  结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 56)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
