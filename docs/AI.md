# AI 配置、证据和验收边界

## 配置

应用支持两条真实 HTTP 调用路径：OpenAI Responses API，以及兼容 Chat Completions JSON 模式的服务。请求由 Python 后端发送，浏览器不会读取密钥。

```sh
cp .env.example .env
chmod 600 .env
```

用本地文本编辑器填写 `.env` 中 `AI_API_KEY`，并确认 `AI_PROVIDER`、`AI_BASE_URL`、`AI_MODEL`。不要把密钥粘贴到询盘或网页表单。环境变量优先于 `.env`；不会执行 `.env` 内容，也不做 shell 展开。配置变更在下一次调用生效。缺少密钥时页面显示「AI 未接入」，正式资料可以手动整理。

- `AI_PROVIDER=openai`：仅允许 `https://api.openai.com` 域名；向基础地址追加 `/responses`。使用严格 JSON Schema，`store=false`，默认模型 `gpt-4.1-mini`，可按账号可用模型更改。
- `AI_PROVIDER=compatible`：向基础地址追加 `/chat/completions`，使用 `response_format: {"type":"json_object"}`，并在提示词提供完整 JSON Schema；返回后再执行本地格式和证据验证。该模式不表示任意兼容服务都经过实测；需要服务商支持 JSON 模式。
- 例如 DeepSeek 的基础地址可为 `https://api.deepseek.com`，模型填写账户实际可用名称。其他服务的基础地址可能包含 `/v1`，请按该服务官方文档填写。
- `AI_TIMEOUT` 为 5–120 秒，默认 45 秒。远程只允许 HTTPS，本机模型地址支持 HTTP。为防止密钥随跳转发送，接口重定向会被拒绝。
- `.env` 只能由当前用户读写（`600`），不能是符号链接；配置文件、密钥不写入数据库、普通日志或应用备份。备份只包含业务记录。HTTP 失败只显示整理过的中文消息，不记录供应商原始响应正文。

本应用只发送本次询盘原文与固定整理规则，不发送整个客户表、产品库、采购成本或供应商资料。请只粘贴本次需要处理的询盘内容；粘贴在原文里的联系方式仍会作为原文的一部分发送给你配置的服务商。

## 分析与匹配规则

1. 已说明的信息必须有逐字原文证据，关键数值还要匹配对应参数的单位与数值，不能引用一段真实文字却填写不同数字。缺失项必须留空；推测不会参与成功匹配。
2. 缺失关键字段自动列为待确认。英文回复只是可编辑草稿；用户自己审核和发送。
3. 匹配由确定性代码完成，不交给模型猜替代型号。第一版支持 MCB 小型断路器的极数、电流、电压、脱扣曲线和分断能力；严格核对标量值。
4. 大小写、明确的等价数值单位可归一化，例如 `0.016 kV` 为 `16 V`、`6000 A` 分断能力为 `6 kA`。`1P+N` 不等同于 `1P`；`230/400 V`、范围、上下限以及带 AC/DC 的电压表达均保留原文，要求人工核实，不从中挑选一个数字。
5. 产品计量单位与询盘单位不同、MOQ 缺失、包装换算不明、资料来源缺失时列为待确认；不自动把箱换算为个。明确低于 MOQ 或参数不符列为冲突，不能确认该候选。
6. 交付时间和认证等额外要求需要人工结合供应商材料核实，候选可能保持「待确认」，不能把技术参数一致当成全部满足。
7. 多组数量或不同参数会提示拆分询盘；第一版不合并不同产品行做选型。保守检测可能把同一产品的可选配置也标为多组，需要拆成明确需求再处理。
8. 演示按钮仅在示例空间可用，使用明确标注的本地规则，不能代表真实模型验收。未知输入不会自动填入示例产品参数。

## 接口参考与验证状态

接口实现参考开发时核对的官方文档：

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Responses 创建接口](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)

自动化测试使用模拟 HTTP 返回验证请求结构、错误 JSON、超时、拒绝、无效证据、敏感错误清洗和失败后的原文保留边界。它们不会产生外部费用，也不能代替真实模型测试。**尚未提供实际 API 密钥，因此真实模型调用验收待完成。**

配置完成后，在正式空间保存一条不含敏感资料的英文询盘，点击「AI 提取需求」，核对活动记录中的 live 模式与成功/失败、原文证据和缺失项。再分别测试缺少参数和明显冲突的询盘。重试只替换成功分析结果；失败保留原始资料与之前保存的数据。
