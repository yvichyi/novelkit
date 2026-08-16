# NovelKit · 有纪律的 AI 连载写作引擎

> **零依赖 · 手机可跑 · 设定永不崩**
> 不用 `pip`，不用装任何东西——`python3` 就能跑。写完每章，AI 自动评审、自动登记伏笔、下一章注入改进清单。

NovelKit 是一个「给长篇小说用的」AI 写作引擎。它不是那种"输入一句话生成一段文"的玩具——它的核心是**纪律**：设定一致性、红线熔断、评审闭环、省钱缓存。让 AI 从"写一章好玩"变成"连载 100 章不崩"。

---

## ✨ 四个卖点

### 📖 一本书 = 一个目录
世界观 / 大纲 / 红线 / 锚点 / 评分标准全部收敛在一个 `novel_config/` 目录里。**换一本书 = 换一个目录，引擎代码零改动**。

```
NOVEL_DIR=books/我的小说 python3 write_chapter.py --write 1
```

### 🧠 评审闭环（越写越好，不是越写越崩）
每章写完，AI 主编「老K」自动评审：
- **【下章改进清单】** → 注入下一章 prompt，让连载逐章优化
- **【事件】** → 自动登记进正史账本（章节事件表）
- **【伏笔】** → 自动登记伏笔账本（级别 + 预计回收阶段）

### 🚫 红线熔断（垃圾正文进不了正史）
禁词黑名单、人称漂移、元话语（"本章完/待续"）、字数、时间倒推……门禁不过的正文自动拦截，存进待修目录而不是正史。还能自动消毒（叙述区"我"→"他"，台词区保护）。

### 💰 缓存省钱（长文连载的成本杀手）
固定前缀缓存设计：世界观 → 脑洞库 → 红线表 → 前文（累积递增），动态层全挪尾部。
实测连写时缓存命中率 **36~48%**，60K 字 prompt 只付约一半的钱。

---

## 🚀 快速上手（3 分钟）

### 0. 前提
- Python 3.9+（手机 Termux / 电脑 / 服务器都行）
- 一个 DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com)，新用户有免费额度）
- 零第三方依赖，不用 pip

### 1. 建一本新书（傻瓜式，可选 AI 生成设定）

```bash
cd engine
python3 new_novel.py
```

问答式：书名 → 简介 → 主角 → 总章数 → 阶段规划 → 是否用 AI 生成世界观/大纲/脑洞/红线（约 1 分钟，6 个文件并行生成）。

### 2. 写第一章

```bash
NOVEL_DIR=books/我的小说 python3 write_chapter.py --preview           # 先看 prompt（零成本）
NOVEL_DIR=books/我的小说 python3 write_chapter.py --write 1 --confirm # 写1章，人工确认后落盘
```

### 3. 手机上用网页写作台（点按，家人朋友也能连）

```bash
python3 web_writer.py
# 手机浏览器打开 http://127.0.0.1:8080
# 同 Wi-Fi 的家人朋友打开 http://<你的局域网IP>:8080
```

一个页面搞定：写下一章 / 预览 / 读已写章节 / 建新书 / 存 Key。

---

## 📚 自带示例书

`books/测试之书/` 是一本**完整走完全链路**的示例（AI 生成设定 + AI 写了第 1 章 + 老K 评审 + 伏笔登记），打开就能看效果：

```bash
NOVEL_DIR=books/测试之书 python3 write_chapter.py --preview   # 看第1章 prompt（17K 字符，设定/大纲/脑洞全注入）
cat "books/测试之书/已发布正文/第1章.md"                        # 读 AI 写的第1章《雨中电梯》（3527字）
cat "books/测试之书/ledger/评审反馈/第1章.md"                  # 看老K 的评审与伏笔登记
```

> 示例书内容由 AI 生成，仅作演示。**运行需要你自己的 API Key**，本仓库不含任何 key。

---

## 🧱 一本书的配置结构

```
books/我的小说/
├── novel_config/          ← 书专属设定（引擎从这里读，改这里=改设定）
│   ├── persona_qwen.md    ← 写作模型人格
│   ├── persona_kimi.md    ← 评审模型人格（老K）
│   ├── topic.md           ← 完整世界观
│   ├── world_setting.md   ← 世界观精简版（注入每章 prompt 最前）
│   ├── world_idea.md      ← 脑洞库（每章「哇点」原料）
│   ├── redline_table.md   ← 红线对照表
│   ├── outline.md         ← 故事大纲（分层注入，防剧透+省token）
│   ├── checklist.md       ← 设定一致性清单
│   ├── anchors.json       ← 阶段/锚点/评分维度/黑名单
│   ├── timeline.json      ← 角色时间线（防回溯错位）
│   ├── task_template.md   ← 章节任务模板（可选覆盖）
│   └── ...
├── ledger/                ← 运行时账本（自动生成）
│   ├── 章节事件表.md      ← 每章不可逆事件（前文摘要层的数据源）
│   ├── 伏笔账本.md        ← 伏笔埋设/回收
│   ├── 评审反馈/          ← 老K 每章评审
│   └── usage.jsonl        ← 每次调用的 token 与费用
└── 已发布正文/            ← 写好的章节（正史）
```

想自定义？复制 `templates/novel_config/` 到你的书目录，照着 `books/测试之书/novel_config/` 填即可。

---

## 🏗 架构一览

```
engine/
├── write_chapter.py      # 写章引擎（prompt 组装/门禁/落盘/评审闭环）
├── new_novel.py          # 傻瓜式建书向导（交互 + AI 生成设定）
├── web_writer.py         # 网页写作台（零依赖 http.server，局域网可连）
├── start.sh              # 手机 Termux 一键菜单
├── ai_bridge_api.py      # 兼容层（re-export）
└── mediakit/             # 核心包
    ├── config.py         # 书配置加载器（NOVEL_CONFIG_DIR）
    ├── cards.py          # 提示词卡片（时间/人物/大纲/评分/few-shot）
    ├── llm.py            # LLM 客户端（DeepSeek，标准库 urllib，零依赖）
    ├── pipeline.py       # 讨论/评审流水线
    ├── state.py          # 状态持久化
    └── story.py          # 正文处理（门禁/消毒/账本）
```

**缓存原理**：每章 prompt = `[固定前缀：人格→世界观→脑洞→红线→硬规则→前文] + [动态层：时间→人物→大纲→评分→状态→整改→任务]`。前缀稳定 → DeepSeek 前缀缓存命中 → 连写越省。

---

## ⚠️ 说明与免责

- **需要自备 API Key**：NovelKit 只做本地编排，不代理任何模型服务。Key 存本地 `.env`，不上传。
- **示例书由 AI 生成**，版权归仓库；你写的书版权归你。
- 手机 Termux 使用：先 `pkg install python`，再 `./start.sh`；长时间写作建议 `termux-wake-lock`。

---

## 🧭 Roadmap

- [x] 通用化引擎（一本书=一个目录）
- [x] 傻瓜建书（AI 生成设定）
- [x] 网页写作台（局域网）
- [ ] 更多模型支持（本地 Ollama / 其他 API）
- [ ] 导出 EPUB / TXT
- [ ] 多语言（当前中文优先）

---

## 📄 License

MIT — 可自由使用、修改、商用（署名即可）。详见 [LICENSE](LICENSE)。
