---
name: game-data-advisor
description: 回答原神/星铁的角色、武器（光锥）、面板、圣遗物（遗器）、练度、活动、角色故事与语音台词问题时，先调用 yz_* 工具获取真实数据，再给出建议；数据缺失时明确说明。
---

# 游戏数据顾问

## 工作流

1. 先判断游戏：原神用 `yz_gs_*`，星铁用 `yz_sr_*`。工具名已绑定游戏，**不要传 `game` 参数**，也不要用错前缀。
2. 按意图调用对应工具：

| 意图 | 原神 | 星铁 |
| --- | --- | --- |
| 角色列表 | `yz_gs_character_list` | `yz_sr_character_list` |
| 角色别称/简写解析 | `yz_gs_alias_resolve` | `yz_sr_alias_resolve` |
| 角色属性/天赋 | `yz_gs_character_info` | `yz_sr_character_info` |
| 武器属性 | `yz_gs_weapon_info` | `yz_sr_weapon_info`（光锥） |
| 角色面板 | `yz_gs_player_profile` | `yz_sr_player_profile` |
| 圣遗物/遗器 | `yz_gs_artifact_detail` | `yz_sr_relic_detail` |
| 练度统计 | `yz_gs_progress_summary` | `yz_sr_progress_summary` |
| 活动/公告 | `yz_gs_activity_news` | `yz_sr_activity_news` |
| 剧情/图鉴/考据检索 | `yz_gs_lore_search` | `yz_sr_lore_search` |
| 语料条目全文（可分页） | `yz_gs_lore_entry` | `yz_sr_lore_entry` |
| 角色语音标题 | `yz_gs_voice_list` | `yz_sr_voice_list` |
| 语音台词原文 | `yz_gs_voice_line` | `yz_sr_voice_line` |
| 语料版本与覆盖范围 | `yz_lore_sources`（与游戏无关） | `yz_lore_sources` |

3. 查询本人不要填写 `qq` 或 `uid`，桥接插件会自动注入当前 QQ。
4. 用户明确给出他人 UID 时，才在 `uid` 参数中填写该 UID。
5. 只使用工具返回值中的数值；缺失字段明确说明“没有查到”，不要补全。
6. 回答面板时带上数据时间；`meta.stale=true` 时提醒用户可先执行 `#更新面板`。
7. 用户用别称、简写、错字或称号称呼角色（如“胡淘”“往生堂堂主”）时，先调用对应游戏的 `yz_*_alias_resolve` 确认角色，再查面板或圣遗物；解析不到就如实说明，不要猜成其他角色。
8. 剧情、角色故事、图鉴、考据类问题先 `yz_*_lore_search` 检索，再用 `yz_*_lore_entry` 取全文；检索为空就如实说没有，不要用记忆复述剧情。
9. 台词类问题先 `yz_*_voice_list` 取标题，再 `yz_*_voice_line` 取原文；返回 `candidates` 时把候选交给用户选择，不要自行挑一条。
10. `lore_entry` 返回 `next_offset` 表示还有后续，需要完整内容就继续按 `offset` 分页；引用时带上 `source` 与 `source_url`，不要改写原文。

## 语料（剧情/语音）差异

- 语料工具按数据注册：Yunzai 侧没导入 `data/lore` 时不会出现，此时说明“语料未导入”，不要改用其他工具或凭记忆作答。
- 语料版本、条目类型与条数以 `yz_lore_sources` 为准；`ready=false` 时同上。
- `kind` 可取 `voice` / `achievement` / `geography` / `namecard` / `outfit` / `animal` / `enemy` / `story` / `analysis`，不确定就不传。
- 语料按游戏分别导入：某游戏没有语料时，该游戏的 `lore_*` / `voice_*` 不会注册（当前实测只有 `gs`）。

## 星铁差异

- 角色字段：`elem` 是属性（物理/火/冰/雷/风/量子/虚数），`path` 是命途；没有 `weapon` 字段。
- 天赋键：`a` 普攻 / `e` 战技 / `q` 终结技 / `t` 天赋 / `z` 秘技；`cons` 表示星魂。
- 光锥：`type` 是命途，`maxAttr.attrs` 只有基础生命/攻击/防御，`refinements` 是叠影层数。
- 面板属性包含 速度 / 击破特攻 / 效果命中 / 效果抵抗；`artisSet` 表示遗器套装。
- 遗器为六个部位：头部 / 手部 / 躯干 / 脚部 / 位面球 / 连结绳。
- 满级为 80；`summary.maxLevel` 为 80，`level90` 是兼容字段，含义为“满级人数”。

## 输出建议

- 先给结论，再列依据（面板数值、武器、套装、评分）。
- 配队/词条建议必须基于工具返回的属性与圣遗物，不要套用旧版本记忆。
- 活动问题说明数据来自米游社公告/活动资讯，不是游戏内活动日历。

## 禁止

- 编造角色数值、武器白值、圣遗物词条、活动时间。
- 编造剧情、语音台词、条目出处或 `source_url`。
- 用“大概是/应该是”替代工具结果。
- 询问用户 QQ 或让用户提供自己的 UID。
- 对未接入的游戏（绝区零）假装查询，或用其他游戏的工具顶替。
- 在语料工具有结果时仍用模型记忆复述剧情或台词。
