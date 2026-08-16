#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容层：保留 ai_bridge_api 原名与全部公开接口。

由 mediakit/ 各模块重新导出，write_chapter.py / probe_models.py / test_serial.py
的 `import ai_bridge_api as m` 无需任何改动。
（原文件已备份为 ai_bridge_api.py.bak）
"""
from mediakit.config import *   # noqa: F401,F403
from mediakit.llm import *      # noqa: F401,F403
from mediakit.state import *    # noqa: F401,F403
from mediakit.story import *    # noqa: F401,F403
from mediakit.cards import *    # noqa: F401,F403

# 下划线私有名（个别调用方会用到，如 test_serial 的 m._quality_gate）
from mediakit.story import _quality_gate, _fuzzy_regex, _cn_num, _next_chapter_no, _stage_by_anchor  # noqa: F401
from mediakit.config import _META_STRONG, _CHAPTER_TITLE_RE  # noqa: F401
