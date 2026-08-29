# -*- coding: utf-8 -*-
"""记忆管理器 MemoryManager——组合 MemoryStore + DecayCalculator + 三维打分。

提供记忆添加（含相似去重）、检索（向量检索→相关性→三维打分→衰减→去重→截断）、
再激活、分层升降级等能力。嵌入为依赖注入（embed_fn 参数），本模块不依赖 llama.cpp。

对齐 CX-O：去重阈值 0.85（DeduplicationEngine）、三维权重/场景权重（MemoryRouter）、
再激活加成与衰减（DecayCalculator）；升降级阈值按重要性分档与再激活次数自定义合理默认。
"""

from datetime import datetime

from .decay import DecayCalculator
from .scoring import _get_weights, score_memories
from .storage import MemoryStore
from .vector_store import InMemoryVectorStore

# 分层阈值（对齐 CX-O permaneent_threshold=0.95 与再激活语义）
LONG_TERM_PROMOTE_IMPORTANCE = 0.60  # importance 达到该分数升 long_term
LONG_TERM_PROMOTE_REACTIVATION = 3  # 再激活达到该次数升 long_term
PERMANENT_PROMOTE_IMPORTANCE = 0.95  # importance 达到该分数升 permanent（对齐 config.memory.permanent_threshold）
PERMANENT_PROMOTE_REACTIVATION = 10  # 再激活达到该次数升 permanent

# 分层顺序（用于升降级判定）
_LEVEL_ORDER = {"short_term": 0, "long_term": 1, "permanent": 2}

# 文本相似去重阈值（对齐 CX-O DeduplicationEngine.threshold=0.85）
DEDUP_THRESHOLD = 0.85

# 写入口相似判定的有界扫描上限（中-1c，第四轮体检批次B）：
# _find_similar 仅取最近 N 条做相似判定，避免每次写入对全 agent 全表扫描
FIND_SIMILAR_SCAN_LIMIT = 500


def tokenize_text(text) -> set:
    """共享分词：CJK 连续段取相邻两字 bigram，ASCII 拉丁/数字连续段取整词小写。

    中-2（第四轮体检批次B）：manager 与 pipeline 原先各自用 ``lower().split()``
    空白切分，中文整句落为单 token——写入口去重恒不命中、keyword_score 恒 0。
    统一收敛为本实现（pipeline 顶层导入本函数，单份实现避免重复；不引入
    jieba 等新依赖）。规则：

    - 连续 ASCII 字母/数字 -> 整词小写（与原空白切分对英文的行为一致）；
    - 连续 CJK（U+4E00–U+9FFF）段长度 >=2 -> 相邻两字 bigram；单字成段 -> 该字本身；
    - 其余字符（空白/标点/其他符号）视作分隔符。

    Returns:
        set[str]: token 集合（空输入返回空集）。
    """
    tokens = set()
    if text is None:
        return tokens
    word_buf = []
    cjk_buf = []

    def _flush_word():
        if word_buf:
            tokens.add("".join(word_buf))
            word_buf.clear()

    def _flush_cjk():
        if len(cjk_buf) >= 2:
            for i in range(len(cjk_buf) - 1):
                tokens.add(cjk_buf[i] + cjk_buf[i + 1])
        elif len(cjk_buf) == 1:
            tokens.add(cjk_buf[0])
        cjk_buf.clear()

    for ch in str(text).lower():
        if ch.isascii() and ch.isalnum():
            _flush_cjk()
            word_buf.append(ch)
        elif "\u4e00" <= ch <= "\u9fff":
            _flush_word()
            cjk_buf.append(ch)
        else:
            _flush_word()
            _flush_cjk()
    _flush_word()
    _flush_cjk()
    return tokens


class MemoryManager:
    """记忆管理器门面：存储 + 衰减 + 三维打分 + 去重 + 分层升降级。"""

    def __init__(self, store=None, vector_store=None, db_path=None, permanent_threshold=None, dedup_threshold=None):
        """初始化内存管理器。

        Args:
            store: 可选 MemoryStore 实例（缺省新建并按 db_path 指向默认库）。
            vector_store: 可选向量存储（缺省使用纯 Python 的 InMemoryVectorStore）。
            db_path: 存储数据库路径（store 未注入时使用）。
            permanent_threshold: 永久晋级 importance 阈值（默认 0.95，对齐
                config.memory.permanent_threshold 与 CX-O 语义；None 回落模块级
                常量 PERMANENT_PROMOTE_IMPORTANCE）。
            dedup_threshold: 内容相似去重阈值（默认 0.85，对齐 config.memory.dedup
                与 CX-O DeduplicationEngine 语义；None 回落模块级常量
                DEDUP_THRESHOLD）。
        """
        self.store = store or MemoryStore(db_path=db_path)
        self.vector_store = vector_store or InMemoryVectorStore()
        # 先确定 permanent 阈值，再构建衰减器：把阈值注入 DecayCalculator，
        # 使「极高重要性免疫衰减」判定与永久晋级共用同一配置来源（M5 接线）
        self._permanent_threshold = (
            float(permanent_threshold)
            if permanent_threshold is not None
            else PERMANENT_PROMOTE_IMPORTANCE
        )
        self.decay = DecayCalculator(permanent_importance_threshold=self._permanent_threshold)
        self.dedup_threshold = (
            float(dedup_threshold) if dedup_threshold is not None else DEDUP_THRESHOLD
        )
        self.store.create_table()

    # -------------------------------------------------------- 工具
    @staticmethod
    def _text_similarity(text1, text2) -> float:
        """两个文本的 Jaccard 相似度（对齐 CX-O deduplication._calculate_text_similarity）。

        分词统一走共享 tokenize_text（中-2：中文 bigram + 拉丁整词），
        修复中文整句单 token 导致的相似度恒 0、去重恒不命中问题。
        """
        if not text1 or not text2:
            return 0.0
        set1 = tokenize_text(text1)
        set2 = tokenize_text(text2)
        if not set1 or not set2:
            return 0.0
        inter = len(set1 & set2)
        union = len(set1 | set2)
        return inter / union if union else 0.0

    def _find_similar(self, content, agent_id):
        """在同 agent 下查找与 content 相似度达阈值的已有记忆，命中返回 (id, 相似度)。

        中-1c（第四轮体检批次B）：改用 storage.list_recent 有界扫描——仅取
        最近 FIND_SIMILAR_SCAN_LIMIT 条（id 降序）做相似判定，不再每次写入
        全 agent 全表扫描。
        """
        for mem in self.store.list_recent(agent_id=agent_id, limit=FIND_SIMILAR_SCAN_LIMIT):
            sim = self._text_similarity(content, mem.get("content", ""))
            if sim >= self.dedup_threshold:
                return mem["id"], sim
        return None

    # -------------------------------------------------------- 写：添加
    def add_memory(
        self,
        content,
        type="short_term",
        importance=3,
        importance_score=None,
        decay_type="ebbinghaus_opt",
        decay_params=None,
        reactivation_count=0,
        emotion_score=0.0,
        permanent=False,
        agent_id="default",
        tags=None,
        metadata=None,
        embed_fn=None,
    ):
        """新增一条记忆；与已有记忆内容相似度达阈值时跳过写入（去重）。

        Args:
            content: 记忆内容（必填）。
            type: 记忆类型（short_term / long_term / permanent）。
            importance: 重要性等级（1~5）。
            importance_score: 重要性分数（0~1，缺省按 importance/5.0 折算）。
            decay_type: 衰减类型（ebbinghaus_opt / two_stage 等）。
            embed_fn: 可选的嵌入函数（content -> 向量）；提供则同步向量化写入向量库。
        Returns:
            int|None: 新记忆 id；因去重跳过返回 None。
        Raises:
            ValueError: type 非法或 content 为空（由 MemoryStore 校验）。
        """
        dup = self._find_similar(content, agent_id)
        if dup is not None:
            return None

        score = importance_score if importance_score is not None else max(0.0, min(1.0, int(importance) / 5.0))
        memory_id = self.store.add(
            {
                "content": content,
                "type": type,
                "importance": importance,
                "importance_score": score,
                "decay_type": decay_type,
                "decay_params": decay_params,
                "reactivation_count": reactivation_count,
                "emotion_score": emotion_score,
                "permanent": permanent,
                "tags": tags,
                "metadata": metadata,
                "agent_id": agent_id,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            }
        )
        if embed_fn is not None:
            vector = embed_fn(content)
            self.vector_store.upsert(str(memory_id), vector, metadata={"memory_id": memory_id, "agent_id": agent_id})
        return memory_id

    def soft_delete(self, memory_id):
        """软删除一条记忆，并同步清理向量库中的对应向量（M-10，第三轮体检批次3）。

        修复前软删除只置 is_deleted=1，向量库中对应向量永不清理——孤儿向量
        既占内存又挤占 retrieve 的 top_k 候选名额。向量 id 与写入约定一致为
        ``str(memory_id)``；向量清理失败仅告警（软删除语义不回滚）。

        :param memory_id: 记忆 id
        :return: bool 是否命中（store 侧软删成功）
        """
        deleted = self.store.soft_delete(memory_id)
        if deleted:
            try:
                self.vector_store.delete(str(memory_id))
            except Exception as exc:  # noqa: BLE001 - 向量清理失败不回滚软删
                print(f"[WARN] 软删除后向量清理失败（memory_id={memory_id}）：{exc}")
        return deleted

    # -------------------------------------------------------- 写：再激活 / 分层
    def _get_memory(self, memory_id):
        """取单条记忆（未软删除）；不存在返回 None。"""
        mem = self.store.get(memory_id)
        if mem is None or mem.get("is_deleted"):
            return None
        return mem

    def update_reactivation(self, memory_id, emotion_intensity=0.0):
        """记录一次再激活：reactivation_count+1、更新 emotion_score，并检查自动提升分层。

        Returns:
            dict|None: 更新后的记忆；记忆不存在返回 None。
        """
        mem = self._get_memory(memory_id)
        if mem is None:
            return None
        new_count = int(mem.get("reactivation_count", 0)) + 1
        old_emotion = float(mem.get("emotion_score", 0.0) or 0.0)
        new_emotion = (old_emotion + abs(float(emotion_intensity))) / 2.0
        self.store.update(memory_id, {"reactivation_count": new_count, "emotion_score": new_emotion})
        # 自动提升检查
        return self._maybe_promote(memory_id)

    def _maybe_promote(self, memory_id):
        """根据 importance 与再激活次数自动判断是否升级（short_term->long_term->permanent）。"""
        mem = self._get_memory(memory_id)
        if mem is None or mem.get("permanent"):
            return mem
        importance = float(mem.get("importance_score", 0.6) or 0.0)
        reac = int(mem.get("reactivation_count", 0))
        current = mem.get("type", "short_term")
        target = current
        if importance >= self._permanent_threshold or reac >= PERMANENT_PROMOTE_REACTIVATION:
            target = "permanent"
        elif importance >= LONG_TERM_PROMOTE_IMPORTANCE or reac >= LONG_TERM_PROMOTE_REACTIVATION:
            target = "long_term"
        if target != current and _LEVEL_ORDER.get(target, -1) > _LEVEL_ORDER.get(current, 0):
            self.store.update(memory_id, {"type": target, "permanent": True if target == "permanent" else mem.get("permanent", False)})
            return self._get_memory(memory_id)
        return self._get_memory(memory_id)

    def promote(self, memory_id, target_type=None):
        """显式提升一条记忆分层。

        Args:
            memory_id: 记忆 id。
            target_type: 目标类型；缺省按阈值推断为 long_term/permanent。

        Returns:
            dict|None: 更新后的记忆；记忆不存在返回 None。
        """
        mem = self._get_memory(memory_id)
        if mem is None:
            return None
        if target_type is None:
            return self._maybe_promote(memory_id)
        current = mem.get("type", "short_term")
        if _LEVEL_ORDER.get(target_type, -1) <= _LEVEL_ORDER.get(current, 0):
            return mem
        self.store.update(memory_id, {"type": target_type, "permanent": True if target_type == "permanent" else mem.get("permanent", False)})
        return self._get_memory(memory_id)

    def demote(self, memory_id, target_type=None):
        """显式降低一条记忆分层。

        Args:
            memory_id: 记忆 id。
            target_type: 目标类型；缺省降一级（permanent->long_term, long_term->short_term）。

        Returns:
            dict|None: 更新后的记忆；记忆不存在或已是短期记忆返回原样。
        """
        mem = self._get_memory(memory_id)
        if mem is None:
            return None
        current = mem.get("type", "short_term")
        if target_type is None:
            if current == "permanent":
                target_type = "long_term"
            elif current == "long_term":
                target_type = "short_term"
            else:
                return mem
        if _LEVEL_ORDER.get(target_type, -1) >= _LEVEL_ORDER.get(current, 0):
            return mem
        self.store.update(memory_id, {"type": target_type, "permanent": False if target_type != "permanent" else mem.get("permanent", False)})
        return self._get_memory(memory_id)

    # -------------------------------------------------------- 读：检索
    def retrieve(
        self,
        query,
        embed_fn,
        top_k=10,
        agent_id="default",
        max_memories=30,
        scene_context=None,
        min_score=None,
    ):
        """检索最相关的若干条记忆。

        流程：向量检索 → 取命中的记忆记录 → 三维打分（relevance 由向量相似度提供，
        time 经衰减矫正）→ 去重 → 取 top max_memories。

        Args:
            query: 查询文本。
            embed_fn: 嵌入函数（query -> 向量）。
            top_k: 向量检索候选数。
            agent_id: 限定记忆归属 agent。
            max_memories: 最终返回条数上限。
            scene_context: 场景上下文（用于三维权重调整）。
            min_score: 可选最低 final_score 过滤。

        Returns:
            list[dict]: 按 final_score 降序的记忆，附加 final_score/component_scores。
        """
        qvec = embed_fn(query)
        hits = self.vector_store.search(qvec, top_k=max(1, int(top_k)))

        candidates = []
        for hit in hits:
            raw_id = hit.get("vector_id")
            if raw_id in (None, ""):
                continue
            try:
                memory_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            mem = self.store.get(memory_id)
            if mem is None or mem.get("is_deleted") or mem.get("agent_id") != agent_id:
                continue
            mem["score"] = float(hit.get("score", 0.0))
            candidates.append(mem)

        if not candidates:
            return []

        weights = _get_weights(scene_context)
        scored = score_memories(
            candidates,
            query=query,
            importance_weight=weights["importance"],
            time_weight=weights["time"],
            relevance_weight=weights["relevance"],
            _decay_calculator=self.decay,
        )

        # 中-1b（第四轮体检批次B）：先截断再去重——只对最终 top 候选两两比较，
        # 去重后不足 max_memories 不再回捞（保持简单），避免全量候选 O(N²) 比较
        deduped = self._dedup_retrieved(scored[: max(0, int(max_memories))])

        if min_score is not None:
            deduped = [m for m in deduped if m.get("final_score", 0) >= min_score]

        return deduped[: max(0, int(max_memories))]

    def _dedup_retrieved(self, scored):
        """检索结果去重：内容相似度达阈值时保留 final_score 更高的一条。"""
        kept = []
        for item in scored:
            dup_existing = None
            for existed in kept:
                if self._text_similarity(item.get("content", ""), existed.get("content", "")) >= self.dedup_threshold:
                    dup_existing = existed
                    break
            if dup_existing is not None:
                # 保留分数高者
                if item.get("final_score", 0) > dup_existing.get("final_score", 0):
                    kept.remove(dup_existing)
                    kept.append(item)
            else:
                kept.append(item)
        kept.sort(key=lambda m: m.get("final_score", 0), reverse=True)
        return kept