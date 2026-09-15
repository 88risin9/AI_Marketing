# AI 外贸工作台 · 第一版

本地、单公司、单人中文网页应用。第一版聚焦 **MCB 小型断路器**：产品资料 → 英文询盘 → 有原文证据的需求分析 → 人工选型 → 英文报价 → 跟进。

## 1. 打开与启动

在已有本地环境中可双击 **`start.command`**；首次克隆后，该脚本会创建虚拟环境并安装依赖。然后在浏览器打开：

**http://127.0.0.1:8765**

也可以在此目录打开终端：

```sh
./start.command
```

关闭启动终端或按 `Ctrl+C` 停止。再次启动，SQLite 中的数据继续保留。端口被占用时使用 `./start.command --port 8766` 并打开对应地址。

换到其他电脑（Python 3.11 或以上）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

英文 PDF 在这台 Mac 上嵌入系统 Unicode 字体。若迁移后需要其他文字，可通过环境变量 `QUOTE_FONT_PATH` 指向支持所需文字的 TrueType 字体。

当前安装使用 Codex 捆绑 Python 运行时；跨电脑请重新建 `.venv`，不要直接复制虚拟环境。本地开发服务器只监听 `127.0.0.1`，适用于本版本单人本机使用。

## 2. 正式资料与虚构示例

右上角切换 **正式资料 / 虚构示例**。两者使用独立数据库：

- `data/live.sqlite3`：正式资料，初始为空。
- `data/demo.sqlite3`：明确标注的虚构产品、客户和询盘。所有示例报价标记 `FICTIONAL SAMPLE`。

请先在示例空间练习，然后切回正式资料。示例型号、价格、交期、公司与客户均为虚构，不可当作真实选型或供应依据。

### 导入模板

在产品页下载模板，填写后上传，预览无格式错误再点击确认导入。也可使用目录中的：

- `assets/products.xlsx` / `assets/products.csv`：正式空模板。
- `assets/example.xlsx` / `assets/example.csv`：明确标注的虚构资料。

Excel 型号和单位列按文本处理，保留前导零和原始写法；不自动把 `pcs` 改成 `boxes`。缺资料允许保存，并显示缺失项。错误行不会被悄悄略过；本次提交整批成功或整批不写入。每次导入新增记录，同型号不自动覆盖。

详细字段和错误处理见 [导入说明](docs/IMPORT.md)。

## 3. AI 配置

本地开发目录可能已有 `.env`；它不会上传到 GitHub。首次克隆时创建配置：

```sh
cp .env.example .env
chmod 600 .env
```

在本机文本编辑器中填写，不要把密钥发到聊天或粘贴到网页。

```dotenv
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_API_KEY=在本机填写你自己的密钥
AI_TIMEOUT=45
```

环境变量优先于 `.env`；配置每次调用时读取，无需重启。若自行复制模板，运行 `chmod 600 .env`。`.env` 被 Git 忽略，也不包含在业务备份内。

支持 `openai`（Responses + JSON Schema）及 `compatible`（Chat Completions + JSON object）两种协议。模型名、地址和超时可配，兼容服务商需确认支持对应接口。具体见 [模型配置与边界](docs/AI.md)。接入依据为官方 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 和 [Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create)。

**本次没有配置真实 API 密钥，也没有进行真实模型端到端调用验收。** 网络适配器实现、请求结构与错误处理通过模拟响应测试；“演示提取”只是本地规则输出，不代表真实模型测试通过。

缺密钥时手工录入、需求整理、选型、报价、PDF 和跟进可正常使用。仅发送当前询盘原文，不发送整个产品库、采购成本和独立客户档案。邮件仅为草稿，不自动发送。

## 4. 每天怎么用：一个例子

1. 在示例空间打开“虚构示例 · 1,000 pcs MCB 询价”。真实工作时，先导入供应商资料、新建客户，再粘贴邮件原文。
2. 点击“演示提取”；正式使用配置模型后点击真实 AI 分析。检查 `1P / 16 A / 230 V AC / C curve / 6 kA / 1000 pcs` 对应原文。缺项与推测不当作已知信息。
3. 检查候选：C32-2P 的极数、电流和电压冲突，不能选择；C16-1P 仍需人工核实 AC 属性和交期。向供应商核实后，把依据写入选型说明，再确认候选。
4. 创建报价：假设供应商成本为 **8.50 CNY/pcs**、汇率 **1 CNY = 0.14 USD**、数量 **1000 pcs**、售价 **2.50 USD/pcs**，其他已知成本 **20 USD**。报价合计 **2500 USD**，已录入成本合计 **1210 USD**，该成本口径下预估毛利 **1290 USD（51.60%）**。这些仅是虚构算例。
5. 自行确认英文产品描述、交期、有效期、付款条件和贸易条款，保存版本、预览、审核、导出英文 PDF。草稿有明显标记；缺必填项无法审核。每次编辑另存新草稿，须重新审核。
6. 自行发送邮件与 PDF，回来手动标记已发送，记录客户反馈与下次跟进日期。成交或关闭时填写原因。

若没有供应商依据，继续保存待补充状态，不要靠相似型号确认替代。第一版一次处理一组产品需求；多组英文询盘需拆分，不合并成一个规格。

## 5. 报价规则

- 数量、售价、汇率、成本使用 Python `Decimal`。每行金额四舍五入，再相加；USD/EUR/CNY/GBP/AUD/CAD 保留 2 位，JPY 保留 0 位。
- 每份报价只有一种销售币种。汇率方向固定为 **1 采购币种 = X 报价币种**；同币种为 1。不同采购币种的产品可以分别填汇率。
- 报价单位必须与产品资料单位完全相同；包装换算未实现，不会猜测换算关系。
- 采购成本缺项时不显示完整毛利；其他成本未确认时不能默认为零。预估毛利仅反映已录入成本，并不等于净利润。
- 客户 HTML/PDF 经过允许字段过滤，不包含采购价、供应商、汇率、内部备注或毛利；只导出五个已支持的 MCB 技术参数。自定义扩展参数保存在内部资料中。
- 报价快照独立于实时产品和公司资料。修改产品后相关询盘须重新审核；历史报价保留原数据。新版本一律草稿。
- 客户可见文字由使用者填写或审核，正式发送前请检查英文预览。

## 6. 备份与恢复

设置页点击 **下载业务备份**，得到当前空间的 JSON 文件；请定期另存到你控制的备份位置。该文件包含客户、采购成本和业务记录，应按内部资料保管。

恢复时切到对应空间，选取该空间备份。应用先校验格式与关联，再一次性替换数据库；**恢复前的完整数据自动保存到 `backups/`**。若恢复错了，在设置页上传这个自动备份即可恢复原状。示例备份不能覆盖正式空间，反之亦然。

也可在停止应用后复制整个 `data/` 目录做文件备份。`.env` 不在业务备份中，需自行在本机安全保留。不要在运行时仅复制 SQLite 文件作为可靠备份，优先使用页面导出。

## 7. 验证与实现

运行测试（首次需要安装测试依赖）：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖：原文证据、参数冲突、缺参、复杂单位表达、多组需求、CSV/XLSX 导入、定点金额、采购汇率、报价必填项、改版复审、历史快照、PDF 内部信息隔离、模型超时/坏 JSON/错误、数据保留、数据库重开、备份恢复与本地请求来源检查。

实际浏览器和 PDF 检查结果见 [验收记录](docs/ACCEPTANCE.md)。

技术：Flask、SQLite、原生 JavaScript/CSS、openpyxl 导入、ReportLab PDF；业务接口见 `docs/CONTRACT.md`。`app/` 为服务端，`static/` 与 `templates/` 为页面，`tests/` 为测试。

第一版不包含自动获客、邮箱同步、自动外联、下单、收付款、报关税务、进销存或多人功能；代码可通过 GitHub 同步，应用仍只在本机运行，没有部署到公共网站。


## 8. Codex + GitHub Desktop 日常协作

远端仓库：<https://github.com/88risin9/AI_Marketing>，日常使用 `main` 分支。

**Codex 和 GitHub Desktop 必须打开同一个本地目录。** 本机现有工程位于 `~/Documents/ChatGPT/注册公司`；其他电脑可以用 GitHub Desktop 克隆到自行选择的目录，再用 Codex 打开该目录。

1. 开始编辑前，在 GitHub Desktop 选择该仓库和 `main`，点击 **Fetch origin**；如有远端新提交，再点击 **Pull origin**。本地未提交的修改请先提交，或按 Desktop 的提示暂存，避免覆盖。
2. 在 Codex 中打开同一个目录，编辑代码并完成必要测试。
3. 回到 GitHub Desktop，在 **Changes** 查看改动，填写简短 Summary，点击 **Commit to main**。如果已让 Codex 完成 commit，直接检查 **History**。
4. 点击 **Push origin** 上传。其他电脑点击 Fetch/Pull 获取更新；运行中的应用在拉取代码后重新启动。

GitHub 只同步代码、导入空模板和虚构示例。`.env`、SQLite 数据库、业务备份、PDF 导出、虚拟环境均不提交。换电脑使用时，API 密钥需重新本地配置；真实业务数据请通过工作台的备份/恢复迁移，不能依靠 Pull 获取。

较大的功能也可先在 Desktop 创建分支，验证完成后再合并回 `main`。
