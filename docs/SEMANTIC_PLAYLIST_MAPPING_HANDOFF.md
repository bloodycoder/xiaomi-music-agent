# 语义歌单映射交接文档

更新时间：2026-05-19（第四轮：通用 scene/activity mood NLU）
项目路径：`/Users/picard/xiaomi-music`



## 0.1 第三轮更新（2026-05-19）：下雨场景 mood 理解已落地

本轮按用户反馈重点解决：

```text
现在下雨，给我下雨适合的歌单
```

不应映射成 `雨声/雷雨/ASMR/白噪音`，而应理解为“下雨这个场景带来的心情”，对应 `舒缓 / 舒服 / 慵懒 / 卧室 / 氛围感 / 安静 / 放松 / chill`。

已改文件：

```text
scripts/semantic_playlist_mapper.py
runtime/benchmark_results.csv
runtime/benchmark_review.md
```

核心实现：

1. 新增 `analyze_query_mood(query)`：轻量 NLU，区分：
   - `literal_content`：用户真的要雨声/白噪音/睡眠声音，例如 `放点雨声`、`给我放点雨声睡觉`。
   - `scene_mood`：用户描述下雨场景并要求推荐/适合的歌单，例如 `下雨天推荐个歌单`、`现在下雨，给我下雨适合的歌单`。
2. 对 `scene_mood + rain_cozy` 做 query rewrite：

```text
原 query + 雨天心情 舒缓 舒服 慵懒 卧室 氛围感 安静 放松 chill 温柔 人声 歌单
```

注意：没有加入 `不是雨声` 这种负向文本，因为 bi-encoder 看到“雨声”仍可能被 literal token 吸引。

3. 新增 `_playlist_mood_adjustment(row, nlu)`：
   - 对雨声/雷雨/ASMR/白噪音/助眠背景音乐类歌单轻微降权。
   - 对名字包含卧室/氛围/舒服/chill/安静/放松等 mood 的歌单轻微加权。
   - 对随机推荐/周X/年度/五星等通用歌单轻微降权。

验证结果：

```bash
python3 scripts/semantic_playlist_mapper.py predict '现在下雨，给我下雨适合的歌单' --top-k 10 --json
```

当前 top：

```text
1. 『卧室推荐』关上门/舒适度百分之百
2. 慵懒卧室——氛围感
3. 另类独立&CHILL歌单
```

并且 literal 雨声仍保留：

```bash
python3 scripts/semantic_playlist_mapper.py predict '给我放点雨声睡觉' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '放点雨声' --top-k 5
```

仍会命中 `雨 声（amsr睡眠用）` / `雨声睡觉`。

额外修复：entity fallback 收紧。艺人/实体 query 如果 top score < 0.58，统一 fallback，避免 `来点陈奕迅` 被 KTV 泛歌单误接。本地无明确艺人歌单的 `陈奕迅/陈楚生/罗大佑/小虎队` 当前均 fallback。




## 0.2 第四轮更新（2026-05-19）：mood NLU 已扩为通用 scene/activity taxonomy

用户指出：mood NLU 不应只有“下雨”，还要覆盖夜晚、早起、通勤、做饭、拉屎、做爱、休闲、放松、冥想等活动/场景。

已改：`scripts/semantic_playlist_mapper.py`

核心变化：

1. `_MOOD_PROFILES` 从单个 `rain_cozy` 扩展为通用 mood taxonomy：
   - `rain_cozy`：下雨/雨天 → 舒缓、舒服、慵懒、卧室、氛围感
   - `night_cozy`：晚上/夜晚/深夜 → 安静、卧室、氛围、温柔、R&B
   - `morning_fresh`：早起/早上/清晨/起床/洗漱 → 清爽、元气、轻松、提神
   - `commute_drive`：通勤/开车/路上/地铁 → 轻快、有节奏、提神、旋律、华语/说唱
   - `cooking_light`：做饭/做菜/厨房 → 轻松、不吵、咖啡店、爵士、chill
   - `toilet_casual`：拉屎/上厕所/蹲坑 → 摸鱼、短时间、轻松、随意、不严肃
   - `intimate_sexy`：做爱/亲热/情侣/约会/暧昧 → 亲密、性感、R&B、慢节奏、浪漫
   - `leisure_chill`：休闲/摸鱼/放空/散步 → 惬意、chill、轻松、咖啡店
   - `relax_calm`：放松/舒缓/解压/焦虑/累了 → 舒缓、安静、轻柔、不焦虑
   - `meditation_empty`：冥想/打坐/瑜伽/禅/正念 → 禅、静坐、空灵、呼吸、平静
   - `focus_study`：学习/工作/写代码/看书/专注 → lofi、沉浸、轻音乐、不吵
   - `sleep_rest`：睡前/睡觉/助眠/失眠 → 安静、轻柔、放松、晚安
   - `bath_comfort`：洗澡/洗漱/泡澡 → 舒服、轻松、清爽、起床/化妆 bgm
   - `fitness_energy`：健身/运动/跑步/撸铁 → 节奏、能量、燃、说唱/hiphop

2. 每个 profile 只包含：
   - `triggers`：场景触发词
   - `query_terms`：用于 embedding query rewrite 的 mood 词
   - `positive_name_terms` / `negative_name_terms`：候选重排的轻微加减权

   **没有绑定 playlist id，也不写“query -> 某个歌单”的死规则。**

3. `analyze_query_mood()` 现在会支持多 mood 组合。例如：

```text
早上通勤提神一点
→ morning_fresh + commute_drive
→ 清晨/清爽/元气 + 通勤/轻快/节奏/提神
```

4. scene mood query 不再走 entity fallback，避免：

```text
早起给我来点清爽的
来个放松歌单
冥想的时候放点音乐
```

被误判为“清爽的/放松歌单/冥想音乐”实体 query 而 fallback。

新增 benchmark query 到 `runtime/benchmark_queries.txt`，总数从 21 增到 28，并重新生成：

```text
runtime/benchmark_results.csv
runtime/benchmark_review.md
```

快速验证：

```bash
python3 scripts/semantic_playlist_mapper.py predict '晚上想听安静一点的' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '早起给我来点清爽的' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '通勤路上来点有节奏的' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '做饭来点不吵的' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '我要拉屎了来点歌' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '做爱的时候来点氛围音乐' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '休闲放空来点舒服的' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '来个放松歌单' --top-k 5
python3 scripts/semantic_playlist_mapper.py predict '冥想的时候放点音乐' --top-k 5
```

本轮典型 top1：

```text
晚上想听安静一点的       -> 慵懒卧室——氛围感
早起给我来点清爽的       -> 精致女孩的起床洗漱洗澡化妆bgm
通勤路上来点有节奏的     -> 下班路上！轻快华语赶走焦虑
做饭来点不吵的           -> 【情侣】适合做菜Do 的时候听的歌
我要拉屎了来点歌         -> 随机推荐002
做爱的时候来点氛围音乐   -> 成人氛围音乐(Sex Music)
休闲放空来点舒服的       -> 慵懒卧室——氛围感
来个放松歌单             -> 【古典音乐】安静 轻柔 放松 不再焦虑
冥想的时候放点音乐       -> 禅.静坐.打坐.冥想.瑜伽.空灵音乐
```

注意：这些结果还需要用户评分校准，尤其是“拉屎/厕所”这类偏好不明确的 activity。


## 1. 当前目标

用户不想继续维护大量死规则，希望把"自然语言输入 -> 本地歌单 / 在线搜索 fallback"的映射改成更泛化的语义理解。

当前只调试映射层，**不要触发播放**，不要改小爱/xiaomusic/ffplay/mpv 主链路。

目标链路：

```text
用户自然语言 query
  -> 本地语义模型 embedding
  -> 本地歌单语义画像向量检索 top-k
  -> 高置信 local playlist / 低置信 online_fallback
  -> 输出给用户评分
```

用户是唯一评审。不要替用户主观判定"好不好"，要把 benchmark 结果给用户评分。

## 2. 用户明确表达的偏好

### 2.1 不要死规则

用户原话方向：

- "我要泛化，要的是懂我，不是我自己写死规则去匹配。"
- "我说话不会这么固定，比如：我要洗澡了给我放点歌。"

所以：

- 不要继续生成无穷口语模板让用户标注。
- 不要靠大量 if/contains 规则硬匹配。
- 规则只可作为极少数临时 baseline/override，语义版本身应尽量不用死规则。

### 2.2 已知用户标注/偏好

这些是用户明确说过的映射偏好，可用于 benchmark 判断或后续少量训练样本：

```text
洗澡前来点舒服 -> 精致女孩的起床洗漱洗澡化妆bgm
洗澡的时候来点轻松的 -> 精致女孩的起床洗漱洗澡化妆bgm
下雨天推荐个歌单 -> 慵懒卧室——氛围感
晚上想听安静一点的 -> 『卧室推荐』关上门/舒适度百分之百
来个Higher Brothers的歌单 -> crazy rap shit
```

另外：

```text
陈楚生 / 陈奕迅 这类本地没有明确歌单时，应 online_fallback 到网易云在线歌单搜索，不要乱配本地。
早上通勤提神一点，本地没有时也 online_fallback。
```

注意：上面这些是偏好/评估依据，不代表应该全部写成硬规则。

### 2.3 本轮新增的用户反馈

```text
罗大佑/小虎队 — 本地没这些艺人的歌单，应 fallback，不要乱配。
下雨天推荐个歌单 — 已经下雨了，不要推荐雨声歌单！用户要的是下雨天的氛围感/mood，
                     不是白噪音。这需要语用层面的理解。
```

## 3. 文件清单

### 3.1 `scripts/semantic_playlist_mapper.py` — 语义版映射器

命令：

```bash
python3 scripts/semantic_playlist_mapper.py build-index
python3 scripts/semantic_playlist_mapper.py predict '我要洗澡了给我放点歌' --top-k 5
```

默认模型：`BAAI/bge-small-zh-v1.5`

本地索引：`runtime/semantic_playlist_index.json`

#### 本轮修复

1. **`load_tracks()` 致命 bug（已修复）**：曲目缓存文件 `runtime/playlist_tracks_cache.json` 的结构是 `{"playlists": {"<pid>": {"tracks": [...]}}}`，旧代码 `load_tracks()` 直接返回顶层 dict，导致 `profile_text()` 永远取不到曲目数据。结果是 98 个歌单的 embedding 全部缺失歌手和曲目信息。修复后 `load_tracks()` 正确解包 `d['playlists']`。

2. **`profile_text()` 噪音优化（已修复）**：去掉了自动生成的模版别名（"播放xxx"、"放xxx"、"xxx歌单"），这些是纯噪音，增加了 profile 长度但零信息量。同时减少了歌曲名数量（20→10）、艺人信息提前到名称之后。

3. **阈值 0.45→0.50（已调整）**：旧阈值 0.45 导致大量弱匹配被判 local（陈楚生 0.461、高达 0.450 等）。提到 0.50 后自动过滤了这些。

4. **entity-content 验证（新增）**：对于看起来像艺人/实体名的 query，在判定 local 之前验证该实体名是否出现在匹配歌单的 profile 中。如果歌单的歌手/歌名里根本没有这个艺人，强制 fallback。解决了罗大佑、小虎队、jojo 等假阳性问题。

函数：

- `_extract_query_core(query)` — 从 query 中提取目标实体（去前缀"来点/来个/播放"、去后缀"的歌单/经典/风格"）
- `_looks_like_entity(text)` — 判断提取出来的文本是否像艺人/实体名（排除 mood 词如"舒服""安静""下雨天"）
- `predict()` 中：score ≥ 0.50 且 score < 0.58 且是 entity query 时，检查 entity 是否在 playlist text 中

### 3.2 `scripts/benchmark_playlist_mapping.py`

benchmark 脚本，比较 baseline（`scripts/local_playlist_mapper.py`）和 semantic。在同一 Python 进程内 import semantic_playlist_mapper，模型只加载一次。

```bash
python3 scripts/benchmark_playlist_mapping.py
# 输出: runtime/benchmark_results.csv
```

### 3.3 `scripts/summarize_benchmark_scores.py`

读取 CSV 中用户填的评分列，输出汇总统计：

```bash
python3 scripts/summarize_benchmark_scores.py
```

输出：平均分、semantic 胜率、低分 case、decision mismatch 分析。

### 3.4 `runtime/benchmark_queries.txt`

21 条 query。

### 3.5 `runtime/benchmark_results.csv`

最新一轮 benchmark 已跑完。用户评分列仍为空。

### 3.6 `runtime/benchmark_review.md`

Markdown 格式的评分表，方便用户阅读和打分。包含最新结果和已知问题说明。

## 4. 当前 benchmark 结果摘要（2026-05-19 第四轮）

| 改善 | 变差/仍存问题 |
|------|-------------|
| 早上通勤：五一时听 → 下班路上（虽 fallback 但 top1 更合理） | 醒脑：脑波 → 下班路上（旧版脑波 0.463 也算弱匹配） |
| 下雨天：雨声/ASMR → 『卧室推荐』/慵懒卧室/CHILL ✅ | 早上通勤 0.493 被 0.50 阈值挡掉（用户说可接受 fallback） |
| 罗大佑：世界古典 → 随机推荐002 + fallback ✅ | Higher Brothers 仍不理想（crazy rap shit 排第 6） |
| 小虎队：世界古典 → 功夫胖 + fallback ✅ | Higher Brothers 仍不理想（crazy rap shit 未排第一） |
| 陈楚生：KTV热歌 → fallback ✅ | |
| 高达风格：Slow Down → fallback ✅ | |
| jojo风格：蒸汽波 → fallback ✅ | |

## 5. 当前主要问题 & 下一步

### 5.1 已完成：benchmark 基础设施

评分表和汇总脚本已就绪。下一步需要用户打分。

```bash
open /Users/picard/xiaomi-music/runtime/benchmark_review.md
# 或者直接编辑 CSV
open /Users/picard/xiaomi-music/runtime/benchmark_results.csv
```

用户填完评分后运行：

```bash
python3 scripts/summarize_benchmark_scores.py
```

### 5.2 已扩展：通用语用理解 / mood 识别（重要）

本轮已先针对最核心问题做轻量 NLU：

> "下雨天推荐个歌单" → 不再推荐雨声歌单，改为雨天心情/氛围感方向

这是 **literal matching vs pragmatic understanding** 的差距。纯 embedding 检索无法区分：
- "给我放点雨声"（真的要雨声白噪音）
- "下雨天推荐个歌单"（描述场景，要 mood-based music）

**后续仍可继续增强 mood tag / reranker。** 可能的方案：

1. **Query intent 分类器**：在 embedding 检索之前加一层轻量 NLU，判断 query 是"要内容"（放雨声）还是"描述场景"（下雨天氛围）。可以是一个简单的 few-shot prompt 给本地小模型，或者训练一个二分类器。

2. **歌单 mood/scene tag**：让用户给歌单打 mood 标签（"慵懒""氛围感""雨天人声"等），然后 query→tag 匹配 + embedding 混合检索。

3. **Query 改写**：用 LLM 把自然语言 query 改写成更适合 embedding 检索的形式。例如 "下雨天推荐个歌单" → "慵懒 卧室 氛围感 歌单"。

4. **Reranker**：用 cross-encoder 对 top-k 候选重排序，cross-encoder 比 bi-encoder 更能捕捉 query 和 playlist 之间的细微语义关系。

5. **负向过滤**：当 query 包含场景描述词（"下雨天""洗澡时"）时，对名字中包含相同词的 playlist 做降权——因为那可能是"内容本身"而非"适合该场景的音乐"。

### 5.3 待解决：通用歌单干扰

"随机推荐001/002"、"周X推荐歌曲"、"五星歌单"等通用歌单因曲目多样性高，embedding 覆盖面广，往往排在 thematic playlist 前面。

可能的方案：
- 对名称匹配特定模式的 playlist 做微弱的 score 惩罚（如名称含"随机""周X""五星"）
- 或者给 playlist 名称一个小的权重参与评分（之前的双路评分实验失败了，因为英文名 playlist 吃亏，可以考虑仅对中文名 playlist 做名称加权）

### 5.4 待解决：Higher Brothers 排名

"crazy rap shit" 排名第 6（0.648），低于"随机推荐001"（0.681，因为也含 Higher Brothers 歌）。baseline 的硬规则直接命中了 crazy rap shit（score 9.9）。

用户不想死规则，但这是"语义画像不够强"的例子。可能方案：
- 增强 crazy rap shit 的 profile：如果曲目中有 Higher Brothers，可在 profile 中重复 artist 名以提高 embedding 中的权重
- 或者接受这个 case 作为模型局限性，让用户评分时给出判断

### 5.5 待解决：阈值调优

当前阈值 0.50 是拍脑袋的。应该在用户评分后，根据"用户认为 local 正确"的最低分和"用户认为应 fallback"的最高分之间找到最优阈值。

## 6. 下一步行动建议

1. **先让用户评分** `runtime/benchmark_results.csv`，跑 `summarize_benchmark_scores.py`
2. **根据评分确定最优阈值** — 找到用户认可的 local/fallback 分界线
3. **继续扩展 mood/scene NLU 层** — 下雨、夜晚、早起、通勤、做饭、厕所、亲密、休闲、放松、冥想、学习、睡前、洗澡、健身等已进入通用 taxonomy；下一步根据用户评分校准 profile 权重
4. **给歌单打 mood tag** — 如果用户愿意，手动给几个核心歌单标注 mood
5. **在语义版明显优于 baseline 之前，不要动 `scripts/music_agent.py`**

## 7. 可用命令

```bash
cd /Users/picard/xiaomi-music

# 重建语义索引
python3 scripts/semantic_playlist_mapper.py build-index

# 单条预测
python3 scripts/semantic_playlist_mapper.py predict '我要洗澡了给我放点歌' --top-k 5

# 跑 benchmark
python3 scripts/benchmark_playlist_mapping.py

# 打开评分表
open runtime/benchmark_review.md

# 用户评分后汇总
python3 scripts/summarize_benchmark_scores.py
```

## 8. 注意事项

- 语义模型是本地 sentence-transformers，不走云 API。
- benchmark 只做映射，不会播放音乐。
- 不要输出 `.env.local` 里的 API key。
- 不要动 xiaomusic、小爱插件、ffplay/mpv 主播放链路，除非用户明确要求接入。
- 用户是唯一评审，不要替用户判断结果好坏。
