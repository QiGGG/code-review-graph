# 函数血缘分析（lineage）快速上手

`lineage` 命令用于回答两个问题：

1. **指定包下的某个函数，它的完整调用血缘是什么？**（谁调用了它 + 它调用了谁）
2. **这条血缘链覆盖了多少代码？**（函数数 / 代码行数 / 涉及文件数）

典型场景：评估改动某个核心函数的影响面、快速理解一个陌生函数在调用网中的位置。

---

## 安装

`lineage` 是本地二次开发的新功能，**不在上游 PyPI 发布版本中**，必须从本地源码安装才能使用。安装方式遵循项目 README 文档中源码树安装的标准做法。

### 安装前先确认

如果**之前已经安装过** `code-review-graph`，先卸载避免版本冲突：

```bash
uv tool uninstall code-review-graph      # 如果是 uv tool 装的
pipx uninstall code-review-graph         # 如果是 pipx 装的
pip uninstall -y code-review-graph       # 如果是 pip 装的
```

> 全新环境可跳过这一步。

### 方式 A：`uv tool install`（推荐，Windows 命令行直接可用）

这是项目 README Troubleshooting 中为源码树安装推荐的标准做法，会把 `code-review-graph.exe` 注册到系统 PATH：

```bash
cd /path/to/code-review-graph
uv tool install . --force
```

完成后在 Windows cmd / PowerShell / Git Bash 中任意目录可直接运行 `code-review-graph` 命令。uv 默认把可执行文件放在 `%USERPROFILE%\.local\bin\`，通常会自动加入用户 PATH；若未生效，重开终端即可。

> 这是**快照安装**——之后源码再有改动，需重新 `uv tool install . --force` 才生效。

### 方式 B：`pipx install .`

项目官方 quickstart 的备选，与方式 A 行为相同：

```bash
cd /path/to/code-review-graph
pipx install .
```

卸载：`pipx uninstall code-review-graph`。

### 方式 C：editable 开发模式（仅本仓库内调试用）

按项目 CONTRIBUTING.md 的方式，仅在本仓库目录内使用：

```bash
uv sync --extra dev                # 创建 .venv 并安装 dev 依赖
uv run code-review-graph ...       # 所有命令前加 uv run；或先激活 .venv 后省略
```

这种方式不写入系统 PATH，**只在本仓库目录有效**，但每次改动即时生效。

### 验证安装

```bash
code-review-graph --version         # 应显示 2.3.x
code-review-graph lineage --help    # 应能打印参数列表（新功能就位标志）
```

### 本机现状

按方式 A 已在本机完成安装（版本 `2.3.8`，含 `lineage` 子命令）。同时存在一份独立的开发 venv `C:\Users\Administrator\.workbuddy\binaries\python\envs\default`（editable 安装），用于隔离运行 pytest 与二次开发。

---

## 前置条件

血缘数据来自项目的知识图谱，**必须先建图**：

```bash
# 在仓库根目录执行，首次全量构建
code-review-graph build

# 之后代码有变动，增量更新即可
code-review-graph update
```

> 注意：图谱的文件枚举基于 `git ls-files`。**新建的源文件需先 `git add -N <文件>`（intent-to-add）**，否则不会入图。

---

## 一分钟上手

```bash
code-review-graph lineage --package code_review_graph/tools --function query_graph --depth 3
```

输出示例（文本树 + 双口径统计）：

```text
函数血缘: query_graph  (包: code_review_graph/tools, 深度: 3)
================================================================

▲ 上游 — 谁调用了它 (2)
└── run_query_tool (code_review_graph/cli.py:xxx-yyy, NN 行)
    └── ...

▼ 下游 — 它调用了谁 (5)
└── query_graph (code_review_graph/graph.py:xxx-yyy, NN 行)
    ├── callers_of (...)
    └── callees_of (...)

────────────────────────────────────────────
代码规模统计
────────────────────────────────────────────
函数级:  12 个函数, 共 486 行
文件级:  4 个文件
  code_review_graph/tools/query.py      210 行 (3 个函数)
  code_review_graph/graph.py            180 行 (6 个函数)
  ...
```

---

## 参数说明

| 参数 | 必填 | 说明 |
|---|---|---|
| `--package` | 二选一 | 包/目录路径，用于过滤候选文件（推荐写**从仓库根目录开始的相对路径**，如 `code_review_graph/tools`；匹配规则见下文） |
| `--file` | 二选一 | 文件路径过滤器：裸文件名 / 仓库相对路径 / 绝对路径均可（匹配规则见下文）。与 `--package` 同时给出时取**交集** |
| `--function` | ✅ | 函数名，**精确匹配** |
| `--depth` | 否 | 每个方向的最大展开层数，默认不限 |
| `--json` | 否 | 输出原始 JSON，便于二次加工 |
| `--html <路径>` | 否 | 额外生成交互式 HTML 血缘子图 |
| `--repo` | 否 | 指定仓库根目录（默认自动探测） |

> **`--package` 与 `--file` 至少要给一个**——两者都缺省时命令直接报错退出（exit code 2），避免在全仓库范围盲目搜索。

### `--package` 路径规则（重要）

- **推荐写法**：从**当前项目根目录**开始的相对路径，例如 `code_review_graph/tools`。
- **匹配机制**：不是严格的前缀匹配，而是**路径片段连续包含**——把 `--package` 和文件路径都按 `/` 切分后，`--package` 的片段序列只要连续出现在文件路径的目录部分中（不含文件名），即视为命中。
- **推论**：
  - 写**绝对路径**（如 `D:/AWork/.../code_review_graph/tools`）也能匹配，因为片段同样连续出现；
  - 写**过短的片段**（如只写 `tools`）也能匹配，但更容易误中其他同名目录、导致多义（ambiguous）；
  - 路径分隔符 `/` 与 `\` 均可，内部会统一归一化。
- **最佳实践**：始终写仓库根目录开始的相对路径，既清晰又能最大限度避免多义。

### `--file` 路径规则

与 `--package` 同为"路径片段连续包含"，**唯一区别是允许命中文件名本身**，因此三种写法都有效：

| 写法 | 示例 | 说明 |
|---|---|---|
| 裸文件名 | `--file lineage.py` | 最省事；若仓库中存在多个同名文件，可能多义，需配合 `--package` 或改用全路径 |
| 仓库相对路径 | `--file code_review_graph/lineage.py` | **推荐**，精确且不歧义 |
| 绝对路径 | `--file D:\AWork\...\code_review_graph\lineage.py` | 直接从 IDE / 文件管理器复制的路径可直接粘贴，`/`、`\` 分隔符均可 |

典型用法：

- **单独使用**（不确定函数在哪个包时）：`--file xxx.py --function foo`
- **与 `--package` 组合**（交集过滤，消歧利器）：`--package code_review_graph --file lineage.py --function resolve`

### 常用组合

```bash
# 1. 快速看完整血缘（不限深度）
code-review-graph lineage --package src/order --function create_order

# 2. 控制输出规模，只看两层
code-review-graph lineage --package src/order --function create_order --depth 2

# 3. 生成可视化网页
code-review-graph lineage --package src/order --function create_order \
    --html diagrams/create_order_lineage.html

# 4. 导出 JSON 给其他工具用
code-review-graph lineage --package src/order --function create_order --json > lineage.json

# 5. 只知道文件、不知道包：单用 --file（仓库相对路径，推荐）
code-review-graph lineage --file code_review_graph/lineage.py --function trace_lineage

# 6. 从 IDE 复制的绝对路径直接粘贴（/ 与 \ 分隔符均可）
code-review-graph lineage --file "D:\AWork\APythonWorkspace\code-review-graph\code_review_graph\lineage.py" \
    --function trace_lineage

# 7. 多义消歧：--package + --file 取交集，精确定位同名函数
code-review-graph lineage --package code_review_graph --file lineage.py --function resolve

# 8. 裸文件名也能用（注意：仓库内有多个同名文件时可能多义）
code-review-graph lineage --file lineage.py --function trace_lineage --html out.html
```

---

## 输出结构说明

### JSON 字段（`--json`）

| 字段 | 含义 |
|---|---|
| `status` | `ok` / `ambiguous`（多义）/ `error`（未找到或缺参数） |
| `package` / `file` | 本次查询使用的包路径 / 文件过滤器（未提供则为空字符串） |
| `root` | 目标函数节点（含文件路径、起止行号） |
| `upstream` | 上游调用树（谁调它），递归 `children` |
| `downstream` | 下游调用树（它调谁），递归 `children` |
| `node_count` / `edge_count` | 血缘子图的节点数 / 调用边数 |
| `scale` | 代码规模统计（见下） |

### `scale` 统计口径

- **函数级**：血缘覆盖的每个函数按 `line_end - line_start + 1` 计算行数并求和，附函数总数
- **文件级**：涉及文件数 + 每个文件的行数与函数数明细

标记为 `(外部/未解析)` 的节点是图中无法定位定义的调用（如第三方库函数），不计入行数统计。

### 多义匹配（ambiguous）

同一个函数名在给定范围内匹配到多个节点时，命令不会盲目选择，而是返回带 **文件路径 + 行号** 的候选列表：

```text
'resolve' under package='code_review_graph' matches 2 nodes. Re-run with a more specific --package or --file.
  - D:/AWork/.../code_review_graph/lineage.py::_EndpointResolver.resolve (D:/AWork/.../code_review_graph/lineage.py:148)
  - D:/AWork/.../code_review_graph/parser.py::CodeParser.resolve (D:/AWork/.../code_review_graph/parser.py:3902)
```

**处理方式**（任选其一）：

1. **用 `--file` 锁定文件**（推荐，最直接）：
   ```bash
   code-review-graph lineage --package code_review_graph --file lineage.py --function resolve
   ```
2. 把 `--package` 写得更具体（精确到子目录）。

JSON 输出中每个候选节点额外附带 `display` 字段（`file_path:line_start: qualified_name [kind]`），便于程序化处理。

---

## HTML 页面功能

`--html` 生成的交互式页面内置以下能力（无需额外参数）：

- **搜索框**：页面右上角 "Search nodes…"，按名称模糊搜索血缘子图中的节点，点击结果可定位高亮
- **类型过滤面板**：左上角按 File / Class / Function / Test / Type 过滤节点
- **底部统计栏**：
  - 常规项：Nodes / Edges / Files / Languages
  - 血缘规模项（仅血缘页面显示）：**Lineage Fns**（血缘覆盖的函数数）、**Lineage LOC**（血缘覆盖的代码总行数）
- **文件级明细**：点击统计栏中的 **Lineage LOC**，在统计栏上方展开/收起每个文件的函数数与行数明细面板
- **交互**：拖拽平移、滚轮缩放、点击节点展开详情、Fit 按钮适配屏幕

> 血缘规模统计项只在 `--html` 生成的血缘页面上出现；全图可视化页面（`visualize` 命令）不含 `lineage_scale` 数据，统计栏保持原样。

### `visualize` 全图页面中的节点级血缘规模

`code-review-graph visualize`（`--mode full` 或小图 `auto`）生成的全图 HTML 中，**点击任意节点**（包括通过右上角搜索框搜索并点击结果）打开右侧详情面板时，面板底部会显示 **Lineage Scale** 区块：

- 汇总：该节点的血缘覆盖 **函数数 · 总行数(LOC) · 涉及文件数**（向上 callers 链 + 向下 callees 链）
- 文件级明细：按行数降序列出各文件的函数数与行数（最多 8 条，超出折叠）

注意事项：

- 该统计在浏览器端基于页面内嵌的 CALLS 边实时计算，口径与 CLI `lineage` 一致（分向遍历、只统计 Function/Method/Test 节点），但**数字可能与 CLI 略有差异**——`visualize` 导出边时采用名称索引解析未限定端点，与 CLI 的解析策略（同扩展名/同文件优先）不完全相同，属正常现象
- 仅 `full` 模式（含小图 `auto` 降级为 full 的情况）可用；`community` / `file` 聚合模式下节点为聚合节点，无行号信息，不显示该区块
- 节点无任何 CALLS 边（孤立节点）时不显示该区块

---

## 常见问题

**Q: 提示 `No function named 'xxx' found`？**
- 确认已执行 `code-review-graph build` / `update`
- 确认 `--package` / `--file` 路径与实际文件路径一致（可用 `code-review-graph search <函数名>` 先查一下该函数在哪个文件，然后直接 `--file <该文件>`）
- 若是新建文件，先 `git add -N <文件>` 再 `update`

**Q: 报错 `at least one of --package or --file is required`？**
- 两个参数至少要给一个。不确定函数在哪个包时，用 `--file <文件名>` 单独定位最省事。

**Q: 血缘树比预期浅 / 缺分支？**
- 动态调用（反射、字符串拼接调用、回调注册）静态分析不可见，属正常现象
- 外部库函数会显示为 `(外部/未解析)` 叶子节点，不会继续展开

**Q: HTML 里图表没渲染出来？**
- 可视化依赖 D3，优先加载本地资源，失败时回退 CDN——需要网络可达 CDN 或本地资源校验通过

---

## 实现位置

- 核心逻辑：`code_review_graph/lineage.py`
- CLI 注册：`code_review_graph/cli.py`（`lineage` 子命令）

---

## 相关功能

- **函数血缘聚合 `lineage-agg`**：统计指定文件/文件夹下所有函数血缘覆盖的去重总行数，见 [lineage-agg.md](./lineage-agg.md)。
