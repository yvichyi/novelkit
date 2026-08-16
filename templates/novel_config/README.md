# novel_config/ · 书专属配置

**这里每一本书的"大脑"都在这**：引擎每次写章都从这里读设定。改这里 = 改这本书，立即生效（无需重启）。

## 文件速查

| 文件 | 作用 | 谁填 |
|---|---|---|
| `world_setting.md` | 世界观精简版（注入每章 prompt 最前面，最重要） | 作者 / AI |
| `topic.md` | 世界观全文（AI 辅助生成） | 作者 / AI |
| `world_idea.md` | 脑洞库·哇点原料（每章至少一个"哇点"从这里挑或自创） | 作者 / AI |
| `redline_table.md` | 红线对照表（写作三问：尺度/红线/代价） | 作者 / AI |
| `outline.md` | 分层大纲（全局骨架 + 每阶段推进方向） | 作者 / AI |
| `checklist.md` | 设定一致性清单（连载中核对，防吃书） | 作者 / AI |
| `persona_qwen.md` | 正史作家（执笔人）人格 | 作者 |
| `persona_kimi.md` | 章评审（评审）人格 | 作者 |
| `faction.md` | 势力/派系设定（可选） | 作者 |
| `foreshadow.md` | 伏笔窗口提示 | 作者 |
| `anchors.json` | 阶段锚点（阶段名/章区间/评分维度） | 程序生成 |
| `chapter_events.md` | **逐章锚点表**（每章主线锚点关键词；AI 建书自动生成，可手改） | AI / 作者 |
| `timeline.json` | 人物时间线 | 程序生成 |
| `scores.json` | 评审六维评分 | 程序生成 |
| `redlines.json` | 禁词/红线词/术语律（程序自动熔断用） | 程序生成 |
| `engine.json` | **引擎参数**（见下） | 作者（可选） |

## engine.json · 引擎参数（不填 = 用内置默认）

```json
{
  "write":  { "max_tokens": 12000, "thinking": "enabled",  "reasoning_effort": "medium" },
  "review": { "max_tokens": 1200,  "thinking": "disabled" },
  "polish": { "max_tokens": 12000, "thinking": "disabled" }
}
```

| 参数 | 含义 | 建议 |
|---|---|---|
| `write.max_tokens` | 每章正文输出上限（token，含思考） | 12000≈一章 3000 字；写超长章调大，省钱调小 |
| `write.thinking` | 正文写作思考开关：`"enabled"`=先构思再写（质量好，慢）；`"disabled"`=直接写（快、省，可能降质） | 默认 enabled |
| `write.reasoning_effort` | 思考强度：`"low"`（快/省/易写飞）/ `"medium"`（推荐，稳）/ `"high"`（慢/贵，易卡） | 默认 medium，**别轻易改 high** |
| `review.max_tokens` | 评审输出上限 | 1200 够用 |
| `review.thinking` | 评审思考：默认 disabled（防"思考吃光 token 返回空"） | 别改 |
| `polish.*` | 润色同上（仅在 `--polish` 时用） | 默认 disabled |

⚠️ **改 `reasoning_effort` 会清空缓存命中率**（缓存按参数分组），连写省钱能力会暂时下降，定好就别动。
