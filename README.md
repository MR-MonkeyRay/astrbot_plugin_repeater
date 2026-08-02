# AstrBot 自动复读插件

为 AstrBot 群聊提供按群独立的自动复读、随机打断与可选 LLM 智能打断、智能禁言提示功能。

## 功能

- 每个群独立开启或关闭，默认状态由配置决定。
- 按完整、有序的消息链核实连续消息；文本、图片、QQ `face` 表情以及 OneBot `mface` 商城表情包都会参与判重。
- 图片优先使用平台提供的 `file` 标识核实，缺失时回退到 URL；`face` 使用表情 ID，`mface` 使用表情包与表情 ID。
- 同一用户重复发送只计一次；达到人数阈值后按配置概率回发原始消息链，保留文本与媒体的原始顺序。
- 可独立开启打断复读；达到阈值后优先按配置概率发送一条随机打断文本，未命中时再尝试普通复读。
- 可选智能打断在打断命中时按共享生成设置使用 AstrBot 已配置的聊天供应商，或使用 OpenAI 兼容直连服务生成一条打断语。OpenAI 兼容直连模式需要 API Base URL、API Key 和自定义模型 ID；供应商解析、直连配置不完整、请求失败、非助手响应或空响应时，仍只发送一条随机后备文本。
- 可选智能禁言提示仅在禁言 API 成功后按与智能打断相同的生成设置生成一条提示文案；默认关闭，供应商解析、直连配置不完整、请求失败、非助手响应或空响应时，仍只发送一条已填充占位符的静态后备文本。
- 触发时先持久化待发送指纹，再执行发送：明确的发送失败和主打断发送前的智能生成取消都会尝试回滚；回滚保存成功时允许重试，回滚保存失败时继续抑制。已进入消息发送或进程中断等发送结果不确定的情况也会继续抑制，以避免重复发送。

## 指令

- `自动复读 查看`：查看本群状态、触发阈值和概率。
- `自动复读 开启`：开启本群自动复读。
- `自动复读 关闭`：关闭本群自动复读。
- `自动复读 帮助`：显示帮助。
- `repeatMsg`：`自动复读` 的英文别名。
- `打断复读 查看`：查看本群打断状态、概率和可选文本数量。
- `打断复读 开启`：开启本群打断复读。
- `打断复读 关闭`：关闭本群打断复读。
- `打断复读 帮助`：显示帮助。
- `interruptRepeat`：`打断复读` 的英文别名。

`开启`、`关闭` 仅限 AstrBot 管理员、当前群群主或群管理员执行；其他成员会收到权限错误提示。

指令受 AstrBot 的唤醒前缀规则约束，例如默认使用 `/自动复读 查看`。

## 配置

插件首次加载后，AstrBot 根据 `_conf_schema.json` 生成配置文件。配置使用 AstrBot 推荐的 `object` 嵌套 Schema，按功能显示为六个独立区块：基础复读、打断复读、打断后禁言、智能文案服务、智能打断和智能禁言提示。`description` 是字段名称，`hint` 是补充说明。

### 基础复读（`repeat`）

- `repeat.default_enabled`：未单独设置过的群是否默认开启，默认 `true`。
- `repeat.disabled_group_ids`：关闭复读的 QQ 群号列表，由开关指令自动更新。
- `repeat.threshold`：同一内容进入打断或普通复读判定所需的独立用户数（含首位发送者），默认 `3`；配置页提供范围 `2`–`50`、步长 `1` 的滑块。
- `repeat.probability`：达到该人数且未命中打断时，回发原消息的概率，默认 `0.3`；配置页提供范围 `0%`–`100%`、步长 `1%` 的滑块。

### 打断复读（`interrupt`）

- `interrupt.default_enabled`：未单独设置过的群是否默认开启打断，默认 `true`。
- `interrupt.disabled_group_ids`：关闭打断复读的 QQ 群号列表，由开关指令自动更新。
- `interrupt.probability`：达到 `repeat.threshold` 后优先打断的概率，默认 `0.1`；配置页提供范围 `0%`–`100%`、步长 `1%` 的滑块。
- `interrupt.texts`：命中打断时随机选择的静态后备文本；为空时使用默认文本 `打断！`。

### 打断后禁言（`mute`）

- `mute.enabled`：是否启用打断复读禁言，默认 `false`。
- `mute.disabled_group_ids`：关闭打断复读禁言的 QQ 群号列表。
- `mute.probability`：打断复读后尝试禁言的概率，默认 `0.05`；配置页提供范围 `0%`–`100%`、步长 `1%` 的滑块。
- `mute.duration_min` / `mute.duration_max`：随机禁言时长下限和上限（秒），默认 `1` / `15`；配置页提供范围 `1`–`3600` 秒、步长 `1` 秒的滑块。
- `mute.texts`：智能禁言提示关闭、不可用或生成失败时随机选择的静态后备文本；支持 `{user}`（被禁言用户）和 `{time}`（禁言秒数）占位符；留空时仅使用单条内置默认文案 `用户{user}因命中打断复读禁言策略而被禁言{time}s`。

### 智能文案服务（`intelligent_provider`）

- `intelligent_provider.mode`：智能文案生成方式，默认 `"astrbot"`；可选 AstrBot 聊天供应商或 OpenAI 兼容直连服务（`"openai_compatible"`）。
- `intelligent_provider.provider_id`：仅 AstrBot 模式使用的聊天供应商 ID；留空时生产消息跟随触发会话，智能文案测试页面必须先选择明确的聊天供应商。
- `intelligent_provider.manual_api_base`：仅 OpenAI 兼容直连模式使用的 API Base URL。直连调用还需已保存 API Key 和自定义模型 ID。非空时必须是长度不超过 256 字符、带主机的绝对 `http` 或 `https` URL，不能含用户名、密码、查询串或片段，末尾 `/` 会被移除。
- `intelligent_provider.manual_api_key`：仅 OpenAI 兼容直连模式使用的 Bearer API Key。直连调用还需已保存 API Base URL 和自定义模型 ID。Key 会随插件配置保存；能够访问该配置的人员应视为可以读取它。Page 响应、智能历史和插件日志不会回显 Key。
- `intelligent_provider.model`（自定义模型 ID）：AstrBot 模式通常留空，插件不会覆盖聊天供应商的模型；仅需覆盖所选聊天供应商的模型时填写。OpenAI 兼容直连模式必须填写，且不会枚举第三方服务的模型。

### 智能打断（`intelligent_interrupt`）

- `intelligent_interrupt.enabled`：是否在打断命中时使用 LLM 生成主打断文本，默认 `false`。
- `intelligent_interrupt.prompt`：智能打断的 LLM 系统提示词；空值或非字符串会回退到内置默认提示词。

### 智能禁言提示（`intelligent_mute`）

- `intelligent_mute.enabled`：是否在禁言 API 成功时使用 LLM 生成禁言提示，默认 `false`。
- `intelligent_mute.prompt`：智能禁言提示的 LLM 系统提示词；空值或非字符串会回退到内置默认提示词。

#### 智能文案测试页面

在 AstrBot WebUI 打开 **复读机 → 智能文案测试**。按生成方式保存 `intelligent_provider` 中的共享设置，再分别运行智能打断或智能禁言提示测试；页面会展示两个智能功能当前是否启用，但测试不受开关影响，以便验证已保存的设置和对应提示词。

- AstrBot 模式下，供应商留空时生产消息仍会跟随触发会话的聊天供应商；页面没有真实群消息的 UMO，因此会明确拒绝测试，不会私自改用默认供应商。
- OpenAI 兼容直连模式不查询 AstrBot 供应商或模型目录。必须同时填写 API Base URL、API Key 与自定义模型 ID；缺少任一项时页面测试返回配置错误而不发起网络请求，运行时改用静态后备文案。
- 页面加载后 API Key 输入框始终为空。保存时不填写 Key 会保留现有值，填写非空值会轮换 Key，点击“清除 Key”会删除它；Page GET/POST 响应绝不返回 Key。
- 智能打断测试使用固定模拟复读内容；智能禁言提示测试使用“测试用户 / 60 秒”。两者只向当前 WebUI 返回生成结果，绝不发送群消息、调用 `set_group_ban` 或修改复读状态。直连成功的结果与历史使用固定供应商标签 `manual-openai-compatible`，不记录 API Base URL 或 Key。
- 页面显示当天、过去 24 小时、2 天、3 天和 7 天的智能打断/禁言记录，并可按类型筛选和翻页。点击一条记录可展开查看复读内容、LLM 请求与回复、复读人数等详情；切换到另一条记录会自动收起已展开的详情。当天从 AstrBot 运行环境本地时区的午夜开始；其余范围为滚动时间窗。
- 记录数据库位于 `data/plugin_data/astrbot_plugin_repeater/intelligent_history.sqlite3`。保存时间、来源、动作、结果、供应商/模型、群 ID（运行时）、禁言时长、耗时、安全失败码、复读内容、LLM 请求与回复，以及已计入本次复读的不同用户数。调用内容可能包含群消息或提示中的用户信息，能够访问此页的人员应视为可以读取这些内容；不单独保存发送者 ID、异常详情、API Base URL 或凭据。后台每小时删除严格早于七天的记录，恰好位于七天边界的记录会保留。

打断与普通复读互斥：命中打断后，智能打断关闭时使用 `interrupt.texts`；开启后生成成功时使用智能文本，供应商解析或请求失败、收到非助手响应或空响应时使用同一静态后备。OpenAI 兼容直连模式缺少 API Base URL、API Key 或自定义模型 ID 时不会发起网络请求，并以 `provider_resolution_failed` 记录安全失败元数据后使用静态后备。生成在发送前取消时不发送文本并尝试回滚，回滚保存成功时允许重试、回滚保存失败时继续抑制。未命中打断时，才按 `repeat.probability` 尝试普通复读。

启用禁言后，插件会在打断复读后按 `mute.probability` 尝试禁言，禁言时长在 `mute.duration_min` 与 `mute.duration_max` 之间随机选择。禁言操作仅在机器人是该群群主或管理员时可执行；缺少该权限时不会执行禁言。只有禁言 API 成功且 `intelligent_mute.enabled` 开启时才生成智能提示；供应商解析、OpenAI 兼容直连配置不完整、请求、非助手响应或空响应失败时，发送一条已填充占位符的静态后备文本。

通过智能文案测试页面保存的 `intelligent_provider` 配置会作为一个运行时配置快照立即替换；其余配置页字段按 AstrBot 的插件重载规则生效。
