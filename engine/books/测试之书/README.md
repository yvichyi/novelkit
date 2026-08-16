# 《测试之书》· 新书工程

- 简介：一个关于记忆交易与自我认知的悬疑故事
- 主角：阿澈
- 阶段规划：3 阶段

## 写章（3 条命令）
```
NOVEL_DIR=books/测试之书 python3 write_chapter.py --preview            # ① 看第1章 prompt（零成本）
NOVEL_DIR=books/测试之书 python3 write_chapter.py --write 1 --confirm  # ② 写第1章，人工确认后落盘
NOVEL_DIR=books/测试之书 python3 write_chapter.py --write 3            # ③ 连写3章（自动落盘）
```
## 目录
- `novel_config/` 书专属设定（世界观/人格/大纲/红线/锚点），引擎从这里读，改这里 = 改设定
- `ledger/` 运行时账本（正史卡/评审反馈/费用），自动生成
- `已发布正文/` 写好的章节
