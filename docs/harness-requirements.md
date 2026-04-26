# Hivemind 评测框架（Harness）需求文档

_2026-04-24，由对话整理。本文档是**需求**而非解决方案。下一个负责架构设计的 agent 应基于这些需求重新设计，不必受限于现有 `bench/` 目录的实现。_

---

## 1. 系统背景

Hivemind 是一个跑在 TEE（dstack CVM，Intel TDX）里的隐私保护数据访问平台。核心机制：

- **数据**：原始 Postgres 数据库，明文跑在 enclave 内。LUKS2 + TDX 在硬件层做加密。
- **三类 agent**，都是 Claude-Code 级别的完整 agent（不是单条 SQL 发射器），各自带 prompt + 工具循环 + 多轮推理 + 文件读写 + 代码执行能力：
  - **Query agent**（访客）：进入 enclave，被授予对数据的访问权限，做开放式分析任务（生成报告、回答问题、发现模式等）
  - **Scope agent**（守门员）：拥有"超能力"——可以模拟（simulation）、读取 query agent 源码、迭代验证、对数据做完全读取——其职责是判断 query agent **能带什么离开 enclave**
  - **Index agent**：处理写入（构建索引、转换数据）。同样是完整 agent，但有 DB 写权限
- **承诺**：scope agent 不保留 query agent 的源代码，enclave 拆销时所有内存清零；只有经 scope 批准的内容能离开 enclave
- **架构核心赌注**：用一个**有判断力的 agent** 做 gatekeeper，比静态规则（RBAC、DP、k-anonymity）更适合开放式 agentic 数据分析这个 use case。理由：真正的隐私是 contextual integrity（信息流跟语境匹配），需要判断而不是规则

**架构尚未定型**——这是我们做这份需求文档的原因。harness 是为了让我们能 principled 地比较和迭代不同架构。

## 2. 我要这个 harness 干什么

一句话：**让我能够确定性地、不被 LLM-judge 污染地、按场景类型分别地，回答"在哪些工作负载和能力等级下，哪种架构（包括 scope agent 的超能力配置）真的有效"。**

具体要解决的问题：

1. **回答超能力是不是真的有用**：现有 51 轮迭代收敛在 B 91，scope agent 的 simulation/源码读取/迭代验证等超能力**几乎没被使用**（详见 `autoresearch/LEARNINGS.md`）。我需要 harness 能区分"超能力被实际用上了"和"超能力是装饰"。
2. **支持架构迭代**：需要能对比静态规则、单次 LLM、迭代 LLM、完整超能力 agent 这几种 scope 实现，以及不同能力等级的 query agent。架构不是固定的——harness 要支持 swap 各种实现。
3. **不被 shared-prior 坑住**：现有 bench 用 LLM judge 评分，导致 iter54（硬编码 PII 列表的 mediator）拿 A 95，而 iter57b（policy-aware 原则化设计）反而退到 C 80。判官跟生成端共享训练分布，benchmark 奖励的是"跟 judge 偶然对齐"而不是"原则上正确"。任何新 harness 必须**绝对避免** LLM judge 作为主要评分依据。
4. **生成可发表的科学结果**：我希望最终能写一篇短论文 / 技术博客，所以 harness 输出的指标要有学术公信力（最好接近 empirical DP audit 那种量化）。
5. **作为偏好对收集的底层基础设施**：长期方向是 personalization——用户在 onboarding 时回答 ~10 个偏好对，系统据此调整 scope/mediator 行为（详见 `autoresearch/pivot_design.md`）。harness 要能产生这些偏好对，并能用偏好数据回测系统。

## 3. 评测对象的特征

下一轮架构设计需要明确知道**待评测的系统长什么样**：

- 待评测的不是单点函数，是**完整 agentic trajectory**：访客 agent 在 enclave 内可能跑几十个轮次、调用多种工具、写中间文件、执行 Python 代码、根据中间结果调整策略
- **泄漏面是整条轨迹**，不是只有最终输出：包括 chain-of-thought（如果被记录）、agent 写的代码（可能内嵌敏感值）、工具调用序列（侧信道）、中间文件（持久化泄漏）、tape 上的 LLM 消息
- **对手也是 agent**：场景里需要包含会主动尝试 prompt injection、慢速多轮推断、任务漂移诱导的对手，模拟真实威胁
- **scope 不再是预过滤**：对一个反应式的多轮 agent，pre-computed scope_fn 不可能覆盖所有未来查询。scope 在新架构下可能需要做**持续监督**（in-flight intervention），而不是一次性过滤
- **三类 agent 都需要被评测**，不只是 query。index agent 因为有写权限，泄漏向量是不同的（持久化在 DB 里）；scope agent 自己的输出（scope_fn 源码、中间推理）也可能泄漏

## 4. Harness 必须做到的事

按重要性排：

### 4.1 确定性的泄漏检测（最重要）

必须有**不依赖 LLM 判断的硬泄漏信号**。具体来说，待评测系统的每一次运行结束后，harness 要能用确定性方法回答："这次运行有没有把不该泄漏的东西带出 enclave？"

要求：

- 信号必须是确定性的（regex / 集合包含 / 字符串匹配 / 分类器 AUC 等）
- 不能依赖 LLM 对"敏感性"的判断
- 必须覆盖**所有泄漏通道**：最终输出、中间文件、生成代码、工具调用参数、chain-of-thought、tape 上的 LLM 消息
- 同时支持"逐字泄漏"（agent 复述了具体内容）和"推断式泄漏"（agent 没复述但推断出了不该说的事实）

### 4.2 隐私-效用前沿测量（不是单点打分）

不能只产出一个"这个系统多好"的标量。harness 必须：

- 沿一个或多个可调旋钮（严格度、阈值、严苛程度等）扫一遍系统行为，产出 **(privacy, utility) 曲线**
- privacy 和 utility 的两个轴都用 4.1 要求的确定性指标
- 系统对比用**前沿 dominance**（哪条曲线在另一条之上）而不是均值
- 报告**永远按场景类别拆开**，绝对不允许把不同 policy class（PII redaction / row exclusion / aggregation only / temporal / topic）平均成一个总分。LEARNINGS #3 和 #11 已证明：不同场景类的效果常常符号相反，平均会抵消掉真正的信号

### 4.3 能力前沿评测（capability evaluation）

待评测系统不是固定的——访客 agent 和 scope agent 的能力都是变量。harness 要能：

- **变量 1**：访客 agent 能力（从一次性 SQL 到完整 Claude-Code 级别）
- **变量 2**：scope agent 能力（从纯静态规则到完整超能力配置）
- 把两个变量都扫一遍，产出 **2D 能力矩阵**：在哪个能力组合下系统的隐私防御开始崩溃
- 不同的超能力（simulation / 源码读取 / 迭代验证）应该能独立开关，做 ablation

### 4.4 场景必须能 stress 出超能力

现有 bench 跑出来超能力没被用——因为场景不需要它们。新 harness 的场景必须**按超能力的 demand 标注**，并且包含**只有用上对应超能力才能解的场景**：

- 慢速多轮推断（slow-drip inference）：单条查询都安全，序列拼起来识别个体——只有 simulation 能解
- 代码内嵌数据（data-in-code）：访客把记录值写进 Python 常量——只有读 trajectory 才能解
- prompt 注入泄漏：数据里嵌入对抗指令——只有 scope 持续监督才能解
- 任务漂移：访客做了用户没要求的事——需要任务边界监督
- agent 行为相关泄漏：scope_fn 对预期 agent 是对的，但实际 agent 用工具的顺序绕过——必须读访客源码
- index agent 持久化：index 把原始内容写进索引表——必须独立的 index trajectory grader

### 4.5 偏好对收集（次要但重要）

长期方向。harness 要能对同一个 query，用不同的 mediator strictness（或其他知识轴）跑出 A/B 两个输出，让我（之后是其他用户）做选择，并把选择持久化。这是**唯一不被 shared-prior 污染的"对错"信号源**——人的偏好。

### 4.6 成本和可重复性

- 每次运行都要记录 token 用量、LLM 调用次数、wall time、美元成本
- 同样的 trajectory 必须能 replay（现有 `tape.py` 已经有这个能力，要保留）
- harness 自身的 LLM 调用（如果有）也要计入成本
- 51 轮迭代的实际成本应该能从 harness 历史里查出来

## 5. 必须避免的反模式（重要）

这些是 51 轮迭代付了真金白银学到的：

1. **不要用 LLM 作为主要评分依据**。LLM judge 跟生成端共享先验，benchmark 会奖励"对齐 judge 口味"而不是"原则上正确"。LLM judge 只能做仪表盘上的诊断信息，永远不能作为通过/失败的依据。
2. **不要按总分汇总**。不同场景类的效果常常符号相反；平均掉就什么都看不见了。永远按场景类拆开报告。
3. **不要让单一标量当优化目标**。哪怕是隐私-效用前沿也不能压缩成"曲线下面积"再去 hill-climb，会重蹈 51 轮迭代的覆辙。
4. **不要把 attacker 进化跑在 LLM judge 的 fitness 上**。GAN 风格的 attacker-defender 协同进化，如果 fitness 是被污染的判官，进化会漂向 judge 盲区而不是真隐私。如果做进化对抗，fitness 必须是确定性的（canary / groundedness / distinguishability）。
5. **不要假设 scope = 预过滤**。对完整 agentic 访客，单次 pre-computed scope_fn 不够。
6. **不要忽视 chain-of-thought 和中间文件**。最终输出干净不代表整条 trajectory 干净。
7. **不要忽视 index agent**。它有写权限，可以制造持久化泄漏。

## 6. 待决的架构问题

下一轮架构设计应该重点想清楚这些：

1. **Scope agent 在新架构下的位置**：是 pre-filter（现状）、in-flight 持续监督、post-hoc 审计、还是混合？这决定了 harness 怎么 hook 进系统。
2. **Mediator 是否还独立存在**：在 trajectory 级别评测里，只看最终输出的 mediator 不够。要么 mediator 升级看 trajectory（增加暴露面），要么职责并入 scope。
3. **可调旋钮（knobs）的定义**：要画隐私-效用曲线，系统必须有连续可调的轴。`pivot_design.md` 里提到的 `redact_severity`、`aggregation_threshold`、`mediator_strictness` 是 v0 设计——具体哪些轴最有信息量？
4. **对手 agent 的能力等级如何定义**：从一次性 SQL 到 Claude Code 级，但中间的"档位"如何切？怎么保证对手在同一档位下行为可比？
5. **Trajectory 持久化的格式**：bundle 应该包含什么？bridge tape 已经有了，但 chain-of-thought、agent 写的文件、工具调用序列还没系统化捕获。这是 harness 的最低层基础设施。
6. **Distinguishability audit 的代价**：成对数据库 + 多次重复运行非常贵。怎么 budget？哪些场景类适合做？
7. **Index agent 的 trajectory 评测**：写持久化是另一类威胁，需要专门的 grader 设计。
8. **静态基线（dumb baseline）的定位**：是作为对照组永远跑、作为 fallback 路径、还是混合架构里"易于规则化的查询走规则、剩下走 LLM"？这影响 harness 怎么 instrument 多个并存的实现。

## 7. 已经做过的工作（可参考但不要被绑定）

- `bench/` 目录：现有 GAN 风格 harness，已被 `bench/DEPRECATED.md` 标记为退役（shared-prior 问题）。代码可以参考，**架构不要被绑定**。
- `autoresearch/LEARNINGS.md` + `CONCLUSIONS.md`：51 轮迭代的发现总结。**强烈推荐先读**。
- `autoresearch/pivot_design.md`：偏好对收集的初步设计。下一轮架构设计可以纳入或重新设计。
- `bench/scenarios_real.json`：35 个真实来源场景（PrivaCI + ConfAIde）。可以作为种子，但不应作为唯一来源。
- `hivemind/sandbox/tape.py` + `bridge.py`：LLM 调用的 tape 录制/回放已经实现，是 trajectory 持久化的良好起点。
- `docs/conditional-recall.md`：系统的核心理念阐述（"用知识做决策、然后可证地遗忘"）。

## 8. 不在范围内

- **不需要在第一版就支持多用户/多租户偏好**：先从我自己的数据 + 我自己的偏好开始。多用户是第二阶段。
- **不需要 RL / DPO 训练**：harness 产出偏好数据，但训练是下游决策。
- **不需要严格的差分隐私保证**：经验 ε（distinguishability audit）就够，不要求形式化证明。
- **不需要完整的对抗性防御**：scope 自身的 prompt 被恶意构造（malicious data owner）这种威胁可以放第二阶段。
- **不需要兼容现有 `bench/` 的接口**：可以推倒重来。

## 9. 给下一个架构设计 agent 的提示

读这份需求时建议按这个顺序：

1. 先读 `autoresearch/LEARNINGS.md` 和 `CONCLUSIONS.md`——理解 51 轮迭代踩过的坑
2. 再读 `bench/DEPRECATED.md`——理解为什么现有 bench 退役
3. 然后读 `ARCHITECTURE.md` 和 `docs/conditional-recall.md`——理解系统设计意图
4. 最后读 `autoresearch/pivot_design.md`——理解长期方向

设计时要解决的核心张力：
- 系统是 agentic 的，所以评测必须是 agentic 的；但 agentic 评测又贵又非确定。怎么平衡？
- 超能力是架构核心赌注，但现有数据显示几乎没被用。是赌注错了，还是评测错了？（我的判断：评测错了）
- 偏好对是唯一干净信号，但偏好对收集慢且数据稀疏。怎么用偏好对校准更便宜的代理指标？

---

_本需求文档由对话整理而成。具体的实现选择（canary、groundedness 算法、Docker 镜像组织、grader 接口等）下一轮架构设计可以自由决定。_
