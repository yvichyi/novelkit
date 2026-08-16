# templates/novel_config/ · 新书配置模板

**用法**（两种）：
- 傻瓜式：`python3 new_novel.py` —— 交互问答自动生成一本新书的配置骨架（可选 AI 辅助生成设定）
- 手工：复制本目录为你的书名，然后填内容：
```
cp -r templates/novel_config 我的新书
# 编辑 我的新书/ 里的文件
NOVEL_CONFIG_DIR=我的新书 python3 write_chapter.py ...
```

**文件说明**见 `novel_config/README.md`（示例书）。
