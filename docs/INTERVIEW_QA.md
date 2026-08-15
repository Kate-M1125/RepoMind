# RepoMind 面试问题与参考回答

> 回答时先讲结论，再结合代码中的具体设计解释。不要逐字背诵；最好加入你实际调试、评测或取舍的经历。

## 一、项目与架构

### 1. 请介绍一下这个项目

RepoMind 是一个面向 GitHub Issue 和 PR 的代码分析 Agent。Issue 流程会分类问题、检索仓库、追踪代码和 Git 证据，最终输出根因与修复建议；PR 流程会从安全、风格和逻辑三路并行审查 diff。核心技术是 LangGraph 状态机、RAG、ReAct 工具调用、Reflection、Memory 和 checkpoint。它与普通聊天机器人的区别是分析过程可路由、可循环、可暂停恢复，并且输出有结构化约束。

### 2. 为什么使用 LangGraph，而不是写一个顺序脚本？

因为这里不只有线性步骤，还包含四类动态路由、三路并行、低置信度回环、Security 人工暂停以及断点恢复。顺序脚本也能实现，但状态传递、重试边界和恢复逻辑会散落在大量 `if/else` 中。LangGraph 将节点、边和共享状态显式化，并能通过 checkpointer 保存执行状态，更适合这种有状态 Agent 工作流。

### 3. IssueState 为什么用 TypedDict？

它相当于工作流的共享黑板：节点读取已有字段，只返回自己修改的字段。TypedDict 能让字段契约和类型更清楚，也便于 LangGraph 合并状态。Issue 和 PR 使用不同 State，是为了避免字段混杂。它不是运行时强校验，所以关键 LLM 输出仍使用 Pydantic；如果继续工程化，我会考虑为节点边界增加更严格的运行时验证。

### 4. 为什么 Issue 和 PR 要做成两个 Graph？

两者共享底层能力，但业务状态和控制流差异很大。Issue 有 bug/feature/question/security 路由、回归检测和修复类型；PR 则围绕 diff 做多维审查。拆成两个 Graph 可以保持状态简单、降低条件分支复杂度，同时复用 LLM、工具、RAG、Memory 和 checkpoint 模块。

### 5. 什么是 Plan-Execute？在项目中如何体现？

Plan-Execute 是先确定任务性质和策略，再让 Agent 执行。Bug 分支先判断是否为 regression，再将修复分成 code fix、dependency upgrade 或 config fix，随后把这个策略注入 ReAct 分析提示。这样模型的工具轮次主要用于找证据，而不是反复判断任务属于什么类型。

## 二、RAG 与代码理解

### 6. 为什么不能只把 Issue 和代码直接交给 LLM？

真实仓库远超上下文窗口，而且大量文件与问题无关。直接输入成本高、噪声大，也无法可靠获得最新 Git 证据。项目先检索候选代码，再允许模型按需调用工具继续探索，相当于先缩小搜索空间，再做动态取证。

### 7. 代码如何切片？为什么这样设计？

Python 使用 AST 按函数和类切片，因为函数/类是较完整的语义单元，并保留行号方便定位。Markdown 按标题切片，其他语言目前采用重叠窗口作为通用降级方案。统一定长切片实现简单，但容易截断函数；纯 AST 又无法覆盖所有语言，因此采用分语言策略。

### 8. HyDE 是什么？为什么代码检索也用它？

HyDE 先让模型根据 Issue 生成一段“可能相关的假设代码”，再用这段代码做向量查询。Issue 是自然语言，仓库内容是代码，两者存在语义空间差异；假设代码可以让查询形式更接近被检索文档。它可能产生错误猜测，所以这里只把它作为多查询中的一路，而不是直接作为答案。

### 9. RRF 的作用是什么？

RRF 用 `1 / (k + rank)` 融合多次检索排名。项目同时使用 HyDE、标题、Issue 类型与正文、堆栈信息查询，每次结果都有偏差。RRF 不依赖不同查询分数是否同尺度，更看重一个文档是否在多路中稳定靠前，因此比直接平均向量分数更稳健。

### 10. 为什么召回后还要 CrossEncoder rerank？

Embedding 检索适合快速召回，但 query 和文档是独立编码的，细粒度相关性有限。CrossEncoder 会联合编码 query-document 对，精度通常更高但计算更贵，所以只对融合后的 top-10 精排，最终保留 top-3。这是典型的“高召回、低成本粗排 + 小集合高精度精排”。

### 11. 为什么还需要 grep？RAG 不够吗？

RAG 擅长语义相似，但不保证找到精确符号定义，也不理解调用关系。若 Issue 明确出现函数名，grep 往往比向量检索更可靠。项目先让 LLM 提取入口符号，再 grep 定义并把入口代码放在上下文前面，RAG 用来补充相关实现。两者是互补关系。

### 12. 如何处理跨仓库依赖问题？

索引 Python 文件时会把 import 包名写入 metadata。检索到相关 chunk 后，将这些 import 与项目依赖取交集，解析依赖对应的 GitHub 仓库并建立独立索引，再查询依赖源码。上层还有 Orchestrator，可以识别多个相关仓库，通过 Send 动态分发给 RepoAgent 并聚合结果。当前包名到仓库地址的解析和跨语言分析仍是需要继续增强的部分。

### 13. 调用图有什么局限？

当前调用图基于 Python AST 和函数名称静态匹配，速度快、适合作为上下文扩展，但对同名函数、继承、动态分派、反射、装饰器和运行时注入不够准确，也不能完整覆盖跨语言调用。它提供候选证据而不是形式化证明。进一步可以接入 LSP、tree-sitter、类型推断或运行时 trace。

## 三、Agent 与并发

### 14. ReAct 与普通 RAG 问答有什么区别？

普通 RAG 通常检索一次后直接生成答案；ReAct 允许模型根据已有证据决定下一步动作。例如先查定义，再找 callers，随后读取文件或查看 blame。这样可以进行多跳代码导航。代价是延迟、token 和行为不确定性上升，因此项目限制最大轮数，并对最终输出做 Schema 校验。

### 15. 工具系统是怎样设计的？

`Tool` 类保存名称、描述、JSON Schema 参数和执行函数，`build_tools` 同时生成 OpenAI function-calling schema 与 executor 映射。LLM 只看到结构化工具描述，应用侧负责解析参数、执行函数并把结果作为 tool message 放回上下文。新增工具只需实现函数并注册，分析逻辑不需要硬编码调用顺序。

### 16. LangGraph Send API 如何实现并行？

派发函数返回多个 `Send`，每个 Send 指向同一个工作节点但携带不同类型字段。Issue 分别发送 entry、rag、git；PR 分别发送 security、style、logic。子节点写不同状态 key，LangGraph 等分支完成后再进入下游。若多个分支需要写同一列表，则应使用 reducer；跨仓库 Orchestrator 就用 `Annotated[list, operator.add]` 聚合结果。

### 17. 并行写 State 会不会产生竞争？

当前同一组并行节点各写不同字段，例如 `_entry_ctx`、`_rag_ctx`、`_git_ctx`，因此不存在同 key 覆盖。共享的仓库 clone/index 另有 per-repo lock，且使用 double-check 避免重复工作。如果未来多个分支更新同一字段，需要定义 reducer 或改成事件/结果列表，不能依赖完成顺序。

### 18. 为什么 Reflection 使用独立 critic？

分析模型容易延续自己的错误假设。独立 critic 重新检查证据、遗漏和结论一致性，可以形成 Actor-Critic 结构。低置信度时返回分析节点补证据，但最多重试两次，避免无限循环。需要承认的是，同类 LLM 仍可能共享偏差，所以 Reflection 只能提高稳健性，不能保证正确。

### 19. 如何防止 Agent 无限调用工具或死循环？

工具循环设置 `max_turns`，达到上限后强制模型基于已有信息输出最终 JSON；Graph 的 Reflection 用 `iteration < 2` 作为回环上限。生产环境还应增加单任务 token、费用、墙钟时间和单工具超时预算，并记录每轮轨迹。

## 四、可靠性、安全与工程化

### 20. LLM 输出不合法怎么办？

第一层使用 JSON response format；第二层用 Pydantic 校验必填字段、枚举和嵌套结构；失败后把原始结果交给修复 prompt 再生成一次；仍失败则返回明确的降级结果。普通 JSON 解析也支持去 Markdown code fence、提取 JSON 对象和有限的截断修复。

### 21. 外部服务失败怎么办？

LLM 超时、连接错误和限流使用指数退避重试。Reranker、调用图、依赖索引和 Git 上下文属于增强能力，失败时记录并跳过，使主流程仍能给出结果。GitHub 数据拉取等必要步骤失败则应让任务进入 failed 状态。这样的原则是区分“核心依赖”和“可选增强”。

### 22. Checkpoint 有什么作用？

每次调用使用唯一 `thread_id`，LangGraph 将状态保存到 SQLite。Security 节点执行 `interrupt()` 后，用户决定可以通过相同 thread_id 和 `Command(resume)` 从暂停位置继续，而不是重跑抓取、索引和检索。Issue 与 PR 使用不同数据库文件，减少状态相互影响。

### 23. HITL 为什么只用于 Security？

安全问题可能包含敏感细节，继续自动处理或输出可能需要权限判断，因此先暂停让人确认。这只是示例策略；实际系统也可以在低置信度、高风险修复、写操作或发布评论前设置人工审批。当前 CLI 支持恢复，但 Web API 还没有完整暴露 resume 接口。

### 24. 这个项目有哪些安全风险？

主要风险包括：用户输入 URL 的严格校验不足、clone 不受信任的大仓库导致资源消耗、Issue 内容对 Agent 的 prompt injection、工具读取或执行范围过大、日志泄露敏感信息、API 无认证和限流。改进方案是只允许 GitHub host 与合法 owner/repo 格式，限制仓库大小和任务预算，隔离 clone/索引进程，对工具参数做授权校验，区分数据与指令，并增加认证、审计和敏感信息过滤。

### 25. Prompt injection 怎么防？

Issue、评论和仓库文件都应被视为不可信数据，不能允许其中的文字修改 system policy。工具权限应由应用控制，而不是由 prompt 控制；读取范围、网络目标和参数必须校验。还可以在 prompt 中明确标记外部内容、过滤高风险指令、采用最小权限工具、对关键动作加 HITL。当前实现有工具 Schema 和固定 executor，但对内容级 prompt injection 的防护还不完整。

### 26. 后端为什么使用 Job API？它当前有什么问题？

完整分析耗时较长，不适合让 HTTP 请求一直阻塞，所以 `POST /analyze` 返回 job_id，后台在线程池执行，客户端轮询 `GET /jobs/{id}`。当前 `_jobs` 是进程内字典，服务重启会丢失，也无法支持多 worker；生产化应使用持久队列和共享存储，并提供取消、超时、进度和幂等键。

### 27. 如何做可观测性？

当前主要是节点级打印。生产环境应记录 trace_id/thread_id、节点耗时、LLM token 与成本、工具调用参数摘要、检索命中、重试与降级原因、置信度以及最终反馈。可以接 OpenTelemetry 和 LangSmith，并建立 P50/P95 延迟、成功率、单任务成本、人工接管率和质量分数看板。

## 五、测试、评估与取舍

### 28. 你如何证明效果变好了？

不能只看几个演示案例。项目从真实关闭 Issue 构造数据集，Bug/Feature 用合并 PR diff 作为参考，Question 用维护者回复作为参考，再由 LLM Judge 分维度评分。改动前后固定数据集、模型和 judge 配置比较 macro average，同时用测试覆盖 JSON 解析、工具边界、节点路由、并行派发和报告规则。

### 29. LLM Judge 可靠吗？

它可扩展、适合做相对回归，但不是绝对真值。它会受模型偏好、prompt、顺序和随机性影响。项目通过多次评分取均值缓解波动；进一步应固定版本、随机交换答案顺序、做 blind judge、多个 judge 投票，并抽样由人工校准相关性。

### 30. 单元测试怎样避免真的调用 LLM？

节点测试通过 mock LLM、GitHub API 和工具结果，验证输入输出、分支、错误降级和 Graph 结构；代码导航工具可以针对本地测试仓库验证；真正依赖网络和模型的测试单独标记为 regression/quality。这样日常测试快且确定，端到端质量门禁单独运行。

### 31. 项目中最关键的技术取舍是什么？

我认为是“确定性工作流”和“自主 Agent”的边界。分类、并行、重试上限、报告规则和人工审批由代码控制；只有需要语义判断和多跳探索的部分交给 LLM。完全固定流程缺乏适应性，完全自治又难以测试和控制，这个项目在两者之间做了分层。

### 32. 为什么 PR 中 medium 问题仍然 APPROVE？

当前规则只让 critical/high 阻塞合并，目的是避免审查器因低风险建议产生过多 false positive。但这不是普适规则，应根据团队政策配置。例如逻辑类 medium 也可能必须阻塞，而 style medium 只做评论。更好的实现是按类别与严重度配置 policy，而不是硬编码。

### 33. 如果分析结果与真实修复 PR 不一致，可能是什么原因？

可能是检索没有召回关键文件、Issue 信息不足、仓库索引版本与问题发生版本不一致、浅克隆缺少历史、调用图对动态行为判断错误、工具轮次不足或模型过早收敛。排查时应先看执行轨迹：入口识别、RAG 命中、工具调用、Git 证据和 critic note，而不是只改 prompt。

## 六、扩展设计题

### 34. 如果仓库规模扩大 100 倍，怎样优化？

首先按 commit SHA 做增量索引，避免每次全量扫描；索引服务与在线分析解耦，任务进入队列。检索先按语言、目录、符号或变更范围过滤，再做向量召回和精排。大模型上下文设置 token budget，缓存 embedding、HyDE 和工具结果。基础设施上使用对象存储、向量数据库集群、Redis 队列和独立 worker，并对热门仓库预热。

### 35. 如何支持私有仓库？

需要使用 GitHub App installation token，而不是共享个人 token；每个租户的凭证、clone 目录、向量 collection、日志和 Memory 必须隔离。token 只在任务执行期间获取并最小授权，禁止写入 prompt 和日志。还要处理 webhook、token 轮换、数据保留与删除策略。

### 36. 如何实现自动在 GitHub PR 上发评论？

先把问题映射到 diff 的有效行号，生成 review comments 草稿；在发布前增加去重、置信度阈值和 HITL。写 GitHub 是有副作用操作，需要幂等键、权限审计、重试保护和 dry-run，不能把模型输出直接作为 API 参数。还要控制评论数量，优先合并同类问题，避免噪声。

### 37. 如何降低成本和延迟？

先用规则和小模型完成 URL 解析、分类或简单过滤；只对高价值候选做 rerank。缓存仓库索引、查询结果和相同 commit 的 project map。PR reviewer 根据变更类型动态派发，而不是永远三路全开。Reflection 只在证据不足或高风险时触发，并为每个任务设置 token/tool budget。最后通过 trace 数据定位真正的耗时热点，而不是盲目减少步骤。

### 38. 如果让你继续做一周，优先改什么？

我会先做三件事：第一，按 commit SHA 增量同步索引，解决代码版本不一致这个质量根因；第二，把内存 Job Queue 改为持久队列并补齐 PR/HITL API，形成完整服务闭环；第三，加入执行 trace、token 成本和检索命中可视化，用固定评测集做消融实验，确认 HyDE、rerank、调用图和 Reflection 各自的真实收益。

## 七、容易被追问的数字与代码位置

面试前应自己再次核对，不要只背数字：

| 内容 | 当前实现/位置 |
|---|---|
| Reflection 阈值 | `0.7`，`workflow/graph.py`、`workflow/pr_graph.py` |
| Reflection 最大重试 | `iteration < 2` |
| Issue 并行任务 | entry、rag、git，`workflow/nodes/retrieve.py` |
| PR 并行任务 | security、style、logic，`workflow/nodes/analyze_pr.py` |
| RAG 单路召回 | top-5 |
| RRF 后候选 | top-10 |
| rerank 后上下文 | top-3 |
| Embedding | `all-MiniLM-L6-v2` |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| LLM 网络重试 | 最多 3 次，指数退避 |
| 仓库 Git 历史 | shallow clone depth 200 |
| Checkpoint | Issue/PR 各自一个 SQLite 文件 |
| PR 阻塞规则 | critical/high → REQUEST CHANGES |

## 八、回答技巧

- 不要只罗列名词，要说清“为什么需要、怎样实现、有什么代价”。
- 每个亮点都准备一个失败案例，例如 RAG 找错文件后为何加入 grep 和 Git 证据。
- 主动区分“已实现”和“准备改进”，尤其是安全、任务持久化、增量索引和跨语言调用图。
- 被问到不确定的数字时直接说明需要查代码，不要编造。
- 最好现场沿着一个 Issue 讲完整数据流，再展示一次低置信度重试或工具调用轨迹。
