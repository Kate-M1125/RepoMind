from pathlib import Path

from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


OUT = Path('/Users/msy/Desktop/github/RepoMind/RepoMind_Agent开发面试问答_第八批A_111-130.docx')

QA = [
('111. Java 中 HashMap 的底层结构是什么？JDK 8 为什么引入红黑树？', [
'我的回答是：JDK 8 的 HashMap 可以概括为“数组 + 链表 + 红黑树”。数组中的每个位置是一个桶，put 时先对 key 的 hashCode 做扰动，再通过数组长度减一与 hash 做按位与来定位桶。桶为空时直接插入；发生哈希冲突时，先比较 hash，再通过 equals 判断是否为同一个 key；如果不是，就沿链表或红黑树继续查找。数组长度保持为 2 的幂，是为了用位运算代替取模，同时让高低位都参与桶位置计算。',
'JDK 8 引入红黑树，主要是为了限制极端冲突下的最坏时间复杂度。普通链表查找是 O(n)，当 key 分布很差或遭遇恶意构造的哈希碰撞时，一个桶可能退化成很长的链表；红黑树查找可以控制在 O(log n)。但树结构节点更大、维护成本也更高，所以不是一发生冲突就树化：桶中节点达到 8，并且整个数组容量至少为 64 时才会树化；容量较小时优先扩容，因为重新分桶通常比建树更划算。节点数降到较低水平时还会退化回链表。',
'HashMap 不是线程安全容器。并发 put、扩容和遍历时可能出现覆盖、状态不可见或迭代结果不一致，不能因为 JDK 8 修复了早期扩容成环的问题，就认为它可以并发使用。若 RepoMind 进程内要缓存模型、工具或索引句柄，单线程初始化后只读可以使用普通 Map；如果存在并发写入，Java 实现中应选择 ConcurrentHashMap，或在更高层明确同步和生命周期。'
]),
('112. ConcurrentHashMap 如何实现线程安全？为什么不直接给整个 Map 加锁？', [
'我的回答是：ConcurrentHashMap 的目标不是“完全无锁”，而是在保证线程安全的同时缩小竞争范围。JDK 8 取消了早期版本的 Segment 分段结构，主要使用数组、CAS 和桶头 synchronized：空桶插入通过 CAS 完成；桶已经存在时，只锁当前桶的头节点，在锁内处理链表或红黑树；扩容时多个线程还可以协作迁移桶。读操作主要依赖 volatile 可见性和节点结构，通常不需要获取互斥锁。',
'如果给整个 Map 加一把全局锁，实现虽然简单，但任意两个 key 的写操作都会互相阻塞，读写并发也会明显下降。ConcurrentHashMap 把冲突限制在同一个桶，并用 CAS 优化无冲突路径，因此并发度更高。不过它只保证单个操作的线程安全，不保证“先检查、再写入”这类组合逻辑原子；这种场景应使用 putIfAbsent、compute、merge 等原子 API，而不是 containsKey 后再 put。',
'工程上还要关注回调语义。computeIfAbsent 的计算函数应短小、无副作用，不能在里面执行长时间的模型请求、网络调用或递归更新同一张表，否则会扩大桶锁持有时间，甚至触发异常行为。RepoMind 当前是 Python 项目，没有直接使用 ConcurrentHashMap；如果将工具注册表或仓库级资源池迁移到 Java 服务，我会用原子 API 做一次初始化，并把耗时资源创建和 Map 临界区解耦。'
]),
('113. synchronized 和 ReentrantLock 有什么区别？应该如何选择？', [
'我的回答是：synchronized 是 JVM 内置的监视器锁，可以修饰方法或代码块，退出作用域时由 JVM 自动释放；ReentrantLock 是 java.util.concurrent 提供的显式锁，需要调用 lock，并在 finally 中 unlock。两者都是可重入锁，也都能建立 happens-before 关系，保证临界区修改对后续获得同一把锁的线程可见。现代 JVM 已经对 synchronized 做了大量优化，不能简单地认为它一定比 ReentrantLock 慢。',
'ReentrantLock 的优势是控制能力更强：可以使用 tryLock 避免无限等待，使用 lockInterruptibly 响应中断，配置公平锁，并通过多个 Condition 表达不同等待队列。代价是代码更复杂，忘记在 finally 解锁会造成永久阻塞。synchronized 的优势是语义直接、异常时自动释放，而且线程 dump 更容易识别监视器关系；只需要简单互斥时，我通常优先使用它。',
'选择标准应从业务语义出发：临界区短、没有超时和多条件等待需求，就用 synchronized；需要限时抢锁、可中断等待、公平性或多个条件队列时，再用 ReentrantLock。无论哪种锁，都不应在持锁期间调用 LLM、GitHub API 或数据库等不可控外部服务。若 RepoMind 改造成 Java 多线程服务，状态更新应保持短小，把外部调用放在锁外，并通过状态机或不可变结果合并。'
]),
('114. Java 的 volatile 能保证什么？为什么不能用它实现 count++ 的线程安全？', [
'我的回答是：volatile 主要保证可见性和一定的有序性。一个线程写 volatile 变量，对随后读取这个变量的线程可见；在 Java 内存模型中，volatile 写与后续 volatile 读建立 happens-before 关系。它还会限制编译器和处理器对相关读写的重排序，因此很适合表示停止标记、配置快照引用或已经安全构造好的不可变对象引用。',
'volatile 不保证复合操作的原子性。count++ 实际包含读取旧值、加一、写回三个步骤，两个线程可能都读到相同旧值，然后分别写回同一个新值，造成更新丢失。要做并发计数，应根据场景使用 synchronized、AtomicLong 或 LongAdder；低冲突下 AtomicLong 用 CAS 很直接，高并发统计下 LongAdder 通过分散热点减少竞争，但 sum 不是一个严格线性化快照。',
'双重检查单例中的实例引用需要 volatile，是因为它既要保证其他线程看到初始化结果，也要避免“分配内存、赋引用、执行构造”发生危险重排序。不过更简单的实现通常是静态内部类或依赖注入容器。RepoMind 的任务状态不能靠一个 volatile 标记保证整体一致性；多个字段的原子转换仍应由锁、数据库事务或工作流 Checkpoint 来承担。'
]),
('115. Java 线程池的核心参数有哪些？任务提交后是怎样执行的？', [
'我的回答是：ThreadPoolExecutor 的关键参数包括 corePoolSize、maximumPoolSize、keepAliveTime、workQueue、threadFactory 和 rejectedExecutionHandler。提交任务时，线程数小于核心线程数就创建核心线程；否则任务先进入队列；队列放不下且线程数未到最大值时继续创建非核心线程；线程和队列都满时执行拒绝策略。这个顺序说明 maximumPoolSize 是否生效与队列类型直接相关，例如使用无界 LinkedBlockingQueue 时，线程数通常不会增长到核心数以上。',
'参数不能脱离任务类型拍脑袋设置。CPU 密集型任务的线程数一般接近 CPU 核数，过多线程只会增加切换；IO 密集型任务可以适度增加线程，但还必须受到下游连接池、API 配额和内存约束。队列也不应默认无界，因为流量持续高于消费能力时，它只是把过载转化为内存积压和超长延迟。生产上更合理的是有界队列、明确拒绝或降级策略，以及对活跃线程、排队长度、拒绝数和执行耗时的监控。',
'RepoMind 中的 GitHub 请求、检索和模型调用以 IO 为主，当前实现使用 Python 异步和 LangGraph 并行，不是 Java 线程池。如果在 Java 网关中承载这类任务，我会按外部依赖拆分线程池或并发限额，避免慢模型调用占满所有工作线程；任务拒绝时返回可重试状态或进入持久化队列，而不是无界堆积。'
]),
('116. JVM 运行时内存区域有哪些？堆、栈和元空间分别存什么？', [
'我的回答是：JVM 运行时数据区中，程序计数器、Java 虚拟机栈和本地方法栈通常是线程私有的；堆和方法区是线程共享的。Java 栈由一个个栈帧组成，每次方法调用都会产生栈帧，里面包含局部变量表、操作数栈、动态链接和返回信息。递归过深或栈帧过大可能导致 StackOverflowError，线程创建过多或无法申请栈空间也可能出现 OutOfMemoryError。',
'堆主要存放对象实例和数组，是垃圾收集器管理的核心区域，但 JIT 的逃逸分析可能进行标量替换，所以不能机械地说“所有对象一定真实分配在堆上”。方法区是 JVM 规范概念，HotSpot 从 JDK 8 开始主要用本地内存中的 Metaspace 实现，用于保存类元数据；字符串常量池和静态字段的具体归属经历过版本变化，面试时应区分规范、HotSpot 实现和 JDK 版本。直接内存也不在 Java 堆中，但 NIO 或本地库大量使用时同样可能 OOM。',
'排查内存问题时，首先要识别是哪一块耗尽：Java heap space 看对象保留和堆转储；Metaspace 看类加载器是否泄漏和动态生成类；unable to create native thread 看线程数量、栈大小和系统限制；Direct buffer memory 看直接缓冲区。RepoMind 不是 JVM 项目，但它的 embedding 模型、向量索引和任务上下文同样需要区分 Python 堆、原生库内存与外部存储，不能只看进程中的一种指标。'
]),
('117. JVM 的垃圾回收如何判断对象是否存活？常见 GC Roots 有哪些？', [
'我的回答是：主流 JVM 使用可达性分析，而不是单纯引用计数。从一组 GC Roots 出发沿引用关系遍历，能够到达的对象视为存活，无法到达的对象才可能被回收。引用计数难以独立处理循环引用，例如两个对象互相引用但已经与程序入口断开；可达性分析可以识别这种整体不可达。对象进入不可达状态也不等于立即释放，还会受到引用类型、终结机制和具体收集周期影响。',
'常见 GC Roots 包括各线程栈帧局部变量中引用的对象、已加载类的静态字段引用、运行时常量引用、JNI 本地引用、活动线程以及 JVM 内部结构等。排查内存泄漏时，关键不是只看“哪个类实例最多”，而是沿 dominator tree 和引用链找出谁把对象连接到了 GC Root。只要仍有强引用路径，GC 就会认为它有用，即使业务上已经不需要它。',
'Java 的内存泄漏通常表现为对象生命周期被无意延长，例如静态 Map 不清理、ThreadLocal 在线程池中未 remove、监听器未注销、缓存没有上限、类加载器被旧线程引用。类似地，RepoMind 若把每次 Issue 的完整代码上下文长期保存在进程级缓存，即使语言有 GC 也会持续增长；正确方式是为任务状态、模型缓存和索引句柄设置明确所有权、上限和淘汰策略。'
]),
('118. G1、CMS 和 ZGC 有什么区别？如何根据场景选择垃圾收集器？', [
'我的回答是：CMS 以老年代低停顿为目标，使用并发标记和并发清除，缺点是会产生内存碎片、对 CPU 敏感，并且在并发失败时可能退化为较长停顿；它已在较新的 JDK 中移除。G1 把堆划分为多个 Region，统一管理年轻代和老年代，通过预测模型优先回收垃圾最多的 Region，并可设置期望停顿目标，是现代服务端 JDK 中较通用的选择。',
'ZGC 更强调超大堆下的低停顿，把大量标记、重定位工作并发化，并利用染色指针和读屏障等机制，使停顿时间对堆大小不那么敏感。代价是实现和运行时开销不同于传统收集器，吞吐、CPU、内存余量以及所用 JDK 版本都要实测。低停顿不等于零停顿，也不意味着任何业务都应该切到 ZGC。',
'选择时我会先确定目标：批处理更关注吞吐，在线 API 更关注 P99 停顿，超大堆交互服务可能优先考虑 ZGC。然后用真实流量观察分配速率、对象晋升、停顿分布、并发周期和 CPU，而不是只改一个 GC 参数。对 Agent 服务而言，模型调用延迟往往远大于 GC 平均停顿，但大上下文、JSON 和检索结果会制造大量短命对象，仍要监控分配速率，避免 GC 抖动放大尾延迟。'
]),
('119. 什么情况下会发生 Full GC？遇到频繁 Full GC 应如何排查？', [
'我的回答是：Full GC 通常意味着对整个 Java 堆以及相关区域进行范围较大的回收，但具体触发条件依赖收集器和 JDK。常见原因包括老年代空间不足、晋升失败、元空间达到阈值、显式 System.gc、G1 并发周期来不及完成或碎片导致大对象无法分配。不能看到日志里的 Full GC 就直接得出“堆太小”的结论，扩大堆可能只是延后故障并增加单次停顿。',
'排查时先保留 GC 日志和时间线，观察 Full GC 前后的堆占用、回收效果、分配速率、晋升量、停顿原因与频率。如果回收后老年代仍然很高，优先怀疑对象泄漏或缓存无界，结合 heap dump、类直方图、dominant object 和 GC Root 引用链定位；如果回收效果很好但很快再次填满，则关注突发分配、大对象、年轻代配置和吞吐容量。如果元空间持续增长，要检查动态代理、脚本引擎和类加载器泄漏。',
'还要把 GC 与业务指标关联，例如请求量、批任务、发布版本和某类大输入是否同步变化。优化顺序通常是先修对象生命周期和无界数据结构，再调整堆、年轻代和收集器参数。若 Java 版 RepoMind 把整仓代码片段、Tool Observation 和历史 messages 全部堆在单个任务对象里，最有效的办法是控制上下文与及时外置，而不是依赖 GC 参数掩盖状态模型问题。'
]),
('120. MySQL 为什么常用 B+ 树索引，而不是哈希表或普通 B 树？', [
'我的回答是：数据库索引的核心成本通常是页 IO。B+ 树分支节点只保存键和子页指针，不保存完整行，因此单页可以容纳更多键，树的扇出更大、层级更低；叶子节点保存索引记录，并通过有序链表连接。这使等值查询、范围查询、排序和最左前缀扫描都能使用同一种结构。InnoDB 聚簇索引的叶子存整行，二级索引叶子存主键值，查询非覆盖列时可能需要再按主键回表。',
'普通 B 树的内部节点也存数据，在相同页大小下扇出更小，而且范围扫描可能频繁在树层级之间跳转。哈希索引的平均等值查询很快，但不保序，不适合范围、ORDER BY、前缀匹配，也难以利用联合索引的有序性。所以 MySQL 默认使用 B+ 树不是因为它在单次内存比较上最快，而是因为它在磁盘页访问和通用查询能力之间更平衡。',
'设计索引时还要考虑选择性、联合索引顺序、覆盖能力和写放大。索引不是越多越好，每次 INSERT、UPDATE、DELETE 都要维护相关树，也会占用 Buffer Pool 和磁盘。若 RepoMind 将任务、Checkpoint 元数据或评测记录存入 MySQL，我会围绕 thread_id、repo_id、status、created_at 等真实查询路径设计联合索引，并用 EXPLAIN 验证，而不是为每个字段单独建索引。'
]),
('121. 联合索引的最左前缀原则是什么？哪些情况可能导致索引失效？', [
'我的回答是：假设联合索引是 (a, b, c)，B+ 树先按 a 排序，a 相同时再按 b，最后按 c。因此查询从 a 开始的连续前缀最容易利用索引，例如 a、a+b、a+b+c；只查询 b 或 c 时，通常无法直接利用这棵树快速定位。出现 a 的范围条件后，后续列在传统规则下通常难以继续用于缩小扫描范围，不过 MySQL 8 的 Index Condition Pushdown、Skip Scan 等优化可能改善部分场景，最终应以执行计划为准。',
'常见“索引没用好”的原因包括对索引列做无法转换的函数或表达式、隐式类型转换、前导百分号 LIKE、低选择性条件、OR 分支没有合适索引，以及返回行太多导致优化器判断全表扫描更便宜。需要注意，“没有出现在 key 中”“没有用于定位范围”和“完全没有利用索引”不是同一件事，某些列仍可能用于覆盖索引或下推过滤。',
'优化时我会先从 SQL 的过滤、排序和分页方式出发设计索引列顺序，再用 EXPLAIN ANALYZE 看实际行数、循环次数和耗时。不能只背最左原则，也不能用 FORCE INDEX 掩盖统计信息或查询设计问题。RepoMind 若按 repo_id + status + updated_at 拉取待恢复任务，这三个字段的组合和排序需求应一起设计，而不是分别创建三个单列索引。'
]),
('122. MySQL 的事务隔离级别有哪些？MVCC 如何实现一致性读？', [
'我的回答是：SQL 标准常见四个隔离级别是 Read Uncommitted、Read Committed、Repeatable Read 和 Serializable，隔离能力依次增强。脏读是读到未提交数据，不可重复读是同一行前后读取结果变化，幻读是同一条件范围内记录集合变化。InnoDB 默认通常是 Repeatable Read，但具体行为还要区分快照读和当前读，不能只根据隔离级别名称推断所有 SQL 的锁行为。',
'MVCC 让普通一致性读不必和写操作互相阻塞。InnoDB 行记录包含隐藏事务信息，旧版本通过 undo log 串联；事务创建 Read View，其中包含活跃事务集合等可见性信息，读取时沿版本链找到对当前快照可见的版本。Read Committed 通常每条语句生成新的 Read View，Repeatable Read 通常在事务第一次一致性读时建立快照并复用，所以后者能保持多次快照读结果一致。',
'UPDATE、DELETE、SELECT ... FOR UPDATE 等当前读需要读取最新可见版本并加锁。InnoDB 在 Repeatable Read 下通过记录锁、间隙锁和 Next-Key Lock 防止范围当前读中的幻影插入，但锁范围与索引访问路径有关。工程上事务应尽量短，外部模型或网络调用不能包在数据库事务内；RepoMind 的长任务更适合用状态版本和短事务提交阶段结果，而不是开启一个事务等待整个 Agent 完成。'
]),
('123. MySQL 出现慢查询时，你会如何系统排查？', [
'我的回答是：我会先确认慢的是数据库执行本身，还是连接池等待、网络、锁等待或应用反序列化。通过慢查询日志、APM Trace 和 performance_schema 找到具体 SQL、调用频率、P95/P99、扫描行数和等待类型，并区分一直慢、数据量增长后变慢，还是只在峰值或特定参数下变慢。仅拿一条脱离参数和并发环境的 SQL 做 EXPLAIN，往往会遗漏真实原因。',
'针对 SQL，先用 EXPLAIN ANALYZE 查看执行顺序、估算行数与实际行数偏差、访问类型、使用的索引、回表、临时表和排序。再检查过滤条件是否可索引、联合索引顺序、统计信息是否过期、是否存在深分页、N+1 查询或一次返回过多列。若执行时间主要是 lock wait，就查长事务和阻塞链；若 Buffer Pool 命中下降或 IO 饱和，则需要从工作集、索引体积和存储能力排查。',
'优化必须验证副作用：新索引会增加写入成本和存储，改写 JOIN 可能改变语义，缓存可能带来一致性问题。上线后比较真实执行计划和尾延迟，并保留回滚路径。RepoMind 如果把每次节点 Trace 逐条同步写数据库，峰值下可能形成大量小写入；可以批量或异步化观测数据，但任务状态和幂等记录仍要优先保证一致性。'
]),
('124. Redis 常见的数据类型及其适用场景是什么？', [
'我的回答是：String 不只是字符串，也可保存整数、序列化对象和位图，适合缓存、计数器和简单分布式状态；Hash 适合字段级读写的对象，但字段过多或 value 很大时同样会占用大量内存；List 是双端链表语义，适合简单队列，但可靠消息一般应优先考虑 Stream 或专业消息系统；Set 适合去重、交并集；Sorted Set 为成员维护 score，适合排行榜、延时任务候选和按权重排序。Stream 提供消息 ID、消费组和待确认列表，更接近可追踪消息队列。',
'选择数据类型要看访问模式和原子操作，而不是只看业务对象名称。例如排行榜用 ZSet 是因为需要按 score 排序；分布式限流可以用 String 计数配合 Lua 原子执行；任务详情如果经常整体读取，一个序列化 String 可能比大量 Hash 字段更简单。还要评估过期策略、单 Key 大小、热 Key 和持久化，因为逻辑上合适的数据结构也可能造成物理热点。',
'RepoMind 当前核心状态使用 SQLite Checkpoint、向量记忆使用 ChromaDB，并没有把 Redis 描述为已实现组件。如果生产化部署多实例，可以用 Redis 做短期结果缓存、限流计数或任务通知，但权威任务状态仍应有明确持久化来源；不能把一个可能淘汰的缓存同时当成唯一数据库。'
]),
('125. 什么是缓存穿透、缓存击穿和缓存雪崩？分别如何治理？', [
'我的回答是：缓存穿透是查询一个本来就不存在的数据，请求每次都越过缓存访问数据库；可以缓存短 TTL 的空值、使用布隆过滤器提前拦截，并在入口校验明显非法参数。布隆过滤器可能误判“存在”但不会把真实存在判断为不存在，更新时也要考虑同步。对于攻击流量，还需要限流和鉴权，不能只依靠空值缓存。',
'缓存击穿是某个热点 Key 失效瞬间，大量并发同时回源。常见方案是互斥重建、singleflight、逻辑过期加后台刷新，或对少量极热点数据预热。缓存雪崩则是大量 Key 在同一时间过期，或整个缓存服务不可用；治理包括 TTL 加随机抖动、多级缓存、高可用集群、限流熔断和数据库容量保护。三个概念的差别在于“不存在的数据”“单个热点”和“大范围失效”。',
'任何治理方案都要定义过期数据可接受程度。逻辑过期能保护后端，但会返回旧值；互斥重建保证较新数据，却会让部分请求等待。RepoMind 若缓存仓库索引或 Issue 分析结果，Key 必须包含仓库 commit 和配置版本，否则表面命中率很高，实际返回的是错误版本；重建还应按 repo 做合并，避免同一仓库并发重复 embedding。'
]),
('126. 如何保证数据库与 Redis 缓存的一致性？', [
'我的回答是：数据库和 Redis 之间通常无法获得免费的强一致性，首先要明确允许多长时间的不一致。最常见的 Cache-Aside 是读时先查缓存，未命中再查库并回填；写时先更新数据库，提交成功后删除缓存。相比同时更新数据库和缓存，删除缓存更容易避免并发覆盖，因为缓存值由下一次读取从权威数据重建。',
'“先写库再删缓存”仍有删缓存失败和极端并发时序问题。工程上可以在事务中写 Outbox，由异步消费者可靠删除或更新缓存；也可以订阅 binlog 做失效，并配合重试、幂等和监控。延迟双删只能缓解部分竞态，第二次删除时间难以精确设置，不能当作通用强一致方案。如果业务必须线性一致，关键读取就不应依赖异步缓存，或者要引入版本校验。',
'回填时还要防旧值覆盖新值：可以让缓存 value 携带数据库版本，只允许更高版本写入，或在失效后短暂禁止旧请求回填。RepoMind 若缓存任务进度，SQLite/数据库 Checkpoint 应是事实源，Redis 仅加速读取；每次状态更新携带 state_version，客户端发现缓存版本落后时直接读取事实源并修复缓存。'
]),
('127. Redis 分布式锁应该如何实现？有哪些容易忽略的问题？', [
'我的回答是：单 Redis 实例的基础实现通常使用 SET key unique_value NX PX ttl，保证加锁和设置过期时间原子完成；释放时不能直接 DEL，而要用 Lua 脚本先比较 value 是否还是自己的随机令牌，再删除，避免客户端 A 超时后误删客户端 B 新获得的锁。业务执行时间可能超过 TTL 时，需要续期机制，但续期必须确认锁所有权，并对进程停顿和网络分区保持警惕。',
'分布式锁解决的是并发协调，不自动保证业务结果正确。持锁客户端可能因长时间 GC、网络延迟或调度暂停，在租约过期后仍继续写；这时即使另一个客户端已获得新锁，旧客户端也可能覆盖新结果。更严格的方案会使用单调递增的 fencing token，让下游存储拒绝旧 token 的写入。Redis 故障转移还可能丢失尚未复制的锁，是否能接受要根据一致性要求评估。',
'因此我不会用 Redis 锁包住长时间 LLM 调用。RepoMind 若要避免同一仓库重复建索引，可以先通过幂等任务表和唯一约束确定 owner，再用短租约协调执行；最终发布索引快照时检查 fencing token 或任务版本。锁是降低重复工作的手段，唯一约束、幂等写和状态校验才是结果正确性的最后防线。'
]),
('128. Kafka 如何保证消息不丢失？生产者、Broker 和消费者分别要做什么？', [
'我的回答是：消息不丢需要逐段分析。生产者端设置合适的 acks，关键消息通常使用 acks=all，并开启幂等生产者和重试；同时设置合理的 delivery timeout，不能把异步发送失败静默忽略。Broker 端使用足够的副本数，配置 min.insync.replicas，并避免在 ISR 不足时仍接受关键写入；磁盘、复制延迟和副本分布也需要监控。即使配置正确，跨机房灾难等场景仍要根据 RPO 单独设计。',
'消费者端的关键是提交 offset 的时机。如果先提交 offset 再处理，进程在处理中崩溃会丢业务结果；通常应处理成功后再提交。若处理包含数据库写入，消费者必须支持幂等，例如用业务唯一键、消费记录表或 Upsert，因为崩溃可能发生在数据库提交成功但 offset 尚未提交之间，随后消息会再次投递。Kafka 常见语义是至少一次，不能把 broker 的 exactly-once 功能误解为对任意外部系统的端到端恰好一次。',
'RepoMind 当前没有使用 Kafka，长任务主要通过应用接口和 Checkpoint 管理。若生产化引入 Kafka 承载分析任务，我会把 task_id 作为幂等键，消费者先读取任务状态，节点结果按版本写入，再提交 offset；DLQ 只保存超过重试阈值且确认不可自动恢复的消息，并保留错误原因和原始任务引用，不能把所有异常无限重试。'
]),
('129. Kafka 为什么吞吐量高？分区数量应该如何设置？', [
'我的回答是：Kafka 的高吞吐来自一组协同设计：日志采用追加写和顺序 IO，消息按批次发送、压缩和落盘，Broker 可以利用页缓存及零拷贝路径减少用户态复制；Partition 让不同分区能够并行读写。Kafka 并不是每条消息都同步随机写磁盘，因此在大量小消息场景下，batch.size、linger.ms 和压缩算法会显著影响吞吐与延迟。',
'分区是并行度和顺序性的边界。同一消费组内，一个分区同一时刻最多由一个消费者处理，所以分区数限制消费并行度；同一 key 进入同一分区时可以保持分区内顺序，但不存在全局顺序。分区过少会限制吞吐，过多则增加文件句柄、元数据、Leader 选举、恢复时间和小批次开销。设置时应根据目标吞吐、单分区基准、消费者并发、未来增长和 Broker 数量估算，并为热点 key 单独设计。',
'分区数通常只能增加，增加后默认 key 哈希到分区的映射可能变化，因此依赖 key 顺序的业务要谨慎。RepoMind 如果按 repo_id 作为 key，可以保证同一仓库的索引更新按分区有序，但超大仓库会形成热点；可以把“索引发布”保持 repo 级串行，把只读分析任务按 task_id 并行，从业务层拆开顺序要求与计算吞吐。'
]),
('130. TCP 三次握手、四次挥手分别解决什么问题？HTTP/1.1、HTTP/2 和 HTTP/3 有什么主要区别？', [
'我的回答是：TCP 三次握手用于双方确认收发能力、交换初始序列号并建立连接状态。客户端发 SYN，服务端回 SYN+ACK，客户端再回 ACK；如果只有两次，服务端无法确认自己发出的确认是否被客户端收到，历史延迟报文也更容易造成错误连接。关闭时 TCP 是全双工的，双方的发送方向需要分别关闭，因此通常表现为四次挥手；主动关闭方进入 TIME_WAIT，是为了让最后 ACK 丢失时可以重发，并避免旧连接报文污染相同四元组的新连接。',
'HTTP/1.1 支持持久连接，但同一连接上的请求响应顺序以及浏览器并发连接限制容易造成队头阻塞。HTTP/2 在一条 TCP 连接上用二进制帧进行多路复用，并加入头部压缩和流控制，但底层 TCP 丢包时，所有流仍会受到传输层队头阻塞。HTTP/3 基于 QUIC/UDP，把可靠传输、TLS 1.3 和多路流集成在用户态；一个流丢包通常不会阻塞其他流，还支持更快的握手和连接迁移，但部署、网络兼容和 CPU 成本需要评估。',
'工程排障时要把应用超时、连接超时、TLS 握手、服务端处理和连接池等待拆开观察。RepoMind 调用 GitHub 与 LLM API 时，复用连接、设置分阶段超时、限制并发和安全重试比盲目提高总超时更重要；POST 是否可重试要看幂等性，读取类请求可以退避重试，而可能产生外部副作用的请求必须使用幂等键或先查询结果。'
]),
]

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}
ET.register_namespace('w', W)
TEMPLATE = Path('/Users/msy/Desktop/github/RepoMind/RepoMind_Agent开发面试问答_第七批20题.docx')


def tag(name):
    return f'{{{W}}}{name}'


def paragraph(text='', style=None, align=None, before=None, after=None,
              size=None, bold=False, color=None, page_break=False,
              keep_together=False):
    p = ET.Element(tag('p'))
    ppr = ET.SubElement(p, tag('pPr'))
    if style:
        ET.SubElement(ppr, tag('pStyle'), {tag('val'): style})
    if align:
        ET.SubElement(ppr, tag('jc'), {tag('val'): align})
    if before is not None or after is not None:
        attrs = {}
        if before is not None:
            attrs[tag('before')] = str(before)
        if after is not None:
            attrs[tag('after')] = str(after)
        ET.SubElement(ppr, tag('spacing'), attrs)
    if keep_together:
        ET.SubElement(ppr, tag('keepLines'))
    r = ET.SubElement(p, tag('r'))
    rpr = ET.SubElement(r, tag('rPr'))
    ET.SubElement(rpr, tag('rFonts'), {
        tag('ascii'): 'Arial Unicode MS', tag('hAnsi'): 'Arial Unicode MS',
        tag('eastAsia'): 'Arial Unicode MS', tag('cs'): 'Arial Unicode MS'
    })
    if size:
        ET.SubElement(rpr, tag('sz'), {tag('val'): str(size * 2)})
        ET.SubElement(rpr, tag('szCs'), {tag('val'): str(size * 2)})
    if bold:
        ET.SubElement(rpr, tag('b'))
    if color:
        ET.SubElement(rpr, tag('color'), {tag('val'): color})
    if page_break:
        ET.SubElement(r, tag('br'), {tag('type'): 'page'})
    else:
        t = ET.SubElement(r, tag('t'))
        t.text = text
    return p


with ZipFile(TEMPLATE) as zin:
    document_xml = zin.read('word/document.xml')
    root = ET.fromstring(document_xml)
    body = root.find('w:body', NS)
    sectpr = deepcopy(body.find('w:sectPr', NS))
    for child in list(body):
        body.remove(child)
    body.append(paragraph('RepoMind Agent 开发面试问答', align='center', before=1400,
                          after=200, size=24, bold=True, color='1F4D78'))
    body.append(paragraph('第八批 A：Java/JVM 与工程基础（20 题）', align='center',
                          after=700, size=14, color='2E74B5'))
    body.append(paragraph('本批为第 111—130 题，覆盖 Java 并发、JVM、MySQL、Redis、Kafka 与计算机网络。回答以工程原理和排障思路为主；涉及 RepoMind 时，会明确区分当前实现与可选的生产化设计。'))
    body.append(paragraph(page_break=True))
    for question, answers in QA:
        body.append(paragraph(question, style='Heading1'))
        for answer in answers:
            body.append(paragraph(answer, keep_together=True))
    body.append(sectpr)
    new_document = ET.tostring(root, encoding='utf-8', xml_declaration=True)

    with ZipFile(OUT, 'w', ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith('.xml'):
                data = data.replace(b'PingFang SC', b'Arial Unicode MS')
            if item.filename == 'word/document.xml':
                data = new_document
            elif item.filename.startswith('word/footer') and item.filename.endswith('.xml'):
                data = data.replace('第七批 · 第 91—110 题'.encode('utf-8'),
                                    '第八批 A · 第 111—130 题'.encode('utf-8'))
            elif item.filename == 'docProps/core.xml':
                data = data.replace('RepoMind Agent 开发面试问答：第七批20题'.encode('utf-8'),
                                    'RepoMind Agent 开发面试问答：第八批A 111—130题'.encode('utf-8'))
            zout.writestr(item, data)

print(OUT)
