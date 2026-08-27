# -*- coding: utf-8 -*-
"""记忆蒸馏模块（Task A7）：MemoryDistiller。

基于云端主 LLM 把超长对话蒸馏为"值得长期记住的事实"，并落盘存储为 long_term 记忆。

设计要点：
- 走云端 LLM（``CloudAdapter.chat`` 流式），离线时抛出 ``DistillationPaused``，
  由调用方捕获后暂停而不报错；
- 超长上下文按「消息条数 / 估算 token」分块，每块独立调用云端归纳；
- 支撑两种返回解析：JSON 数组 / 每行 ``- `` 开头的行式列表；
- importance（重要度）优先取云端返回，缺失时默认 3；
- 路径 / 导入规范：一律使用包绝对导入（``from lite.cloud.adapter import CloudAdapter``）。
"""

import json
import sys
import uuid
from typing import Dict, List

from lite.cloud.adapter import CloudAdapter
from lite.memory.storage import MemoryStore


class DistillationPaused(Exception):
    """蒸馏暂停异常。

    触发场景：记忆蒸馏执行前检测到云端主 LLM 离线（``cloud.is_online() == False``）。
    调用方捕获本异常后应将蒸馏任务标记为"暂停"，静默跳过当前批次，不向用户报错。
    """


class DistillStateError(RuntimeError):
    """蒸馏会话非法状态转移。"""


# --------------------------------------------------------------------------- #
# 蒸馏会话状态机（对齐 CX-O distillation_service 的 9 态语义，精简为 7 态）：#
#   pending -> extracting -> quality_check -> committing -> done             #
#                              |-> rejected（质量门拒绝，不落库）            #
#   extracting/quality_check/committing --异常--> failed                     #
#   （M8 补边：quality_check 态内解析/评分异常可合法直达 failed，不再依赖     #
#     兜底覆写；committing 部分落库失败时终态同样为 failed，已落库条目        #
#     如实保留在 added 中，partial/committed 字段记录部分失败语义。）         #
# 与 CX-O 的对应：pending≈S_PREREAD、extracting≈S_EXTRACT、                  #
#   quality_check≈S_STORAGE_DECISION 前的质量评估、committing≈落库、         #
#   rejected≈S_REJECT、failed/done≈S_FINALIZE 收束。                        #
# --------------------------------------------------------------------------- #
S_PENDING = "pending"  # 待处理（任务创建）
S_EXTRACT = "extracting"  # 提取中（云端归纳）
S_QUALITY = "quality_check"  # 质量评估门（对齐 CX-O quality_score 判定）
S_REJECT = "rejected"  # 质量不合格，拒绝落库（对齐 CX-O S_REJECT）
S_COMMIT = "committing"  # 质量通过，正在落库
S_FAILED = "failed"  # 异常失败（对齐 CX-O 失败收束）
S_DONE = "done"  # 成功收束

#: 合法状态转移表（拒绝/失败为终止态，均不继续）
_TRANSITIONS = {
    S_PENDING: (S_EXTRACT,),
    S_EXTRACT: (S_QUALITY, S_FAILED),
    S_QUALITY: (S_COMMIT, S_REJECT, S_FAILED),
    S_COMMIT: (S_DONE, S_FAILED),
    S_REJECT: (),
    S_FAILED: (),
    S_DONE: (),
}

#: 质量拒绝阈值（对齐 CX-O rubric.quality_reject_threshold 默认 0.3）
QUALITY_REJECT_THRESHOLD = 0.3


def _is_normal_char(ch: str) -> bool:
    """判断字符是否属「正常内容字符」：常见 ASCII（含 JSON 结构） / 中文 / 常用中文标点。

    用于质量门的乱码检测：控制字符、高位区块符号（如 ♠♦）、Emoji 等视为异常。
    """
    o = ord(ch)
    if 0x20 <= o <= 0x7E:  # 常见 ASCII（含 JSON 结构字符 []{}":, 等）
        return True
    if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F:  # 中文（CJK）与 CJK 标点区
        return True
    return ch in "，。、！？；：""''（）《》【】…—·"


#: 蒸馏提示词（中文）：请云端 LLM 从给定对话中提取值得长期记住的事实。
#: 输出支持两种格式：JSON 数组（元素可为字符串或含 content/importance 的对象），
#: 或每行一条以 "- " 开头的行式列表。
DISTILL_PROMPT = (
    "你是一位记忆蒸馏助手。请你从下面的对话中，提取出所有'值得长期记住的事实'"
    "（如用户的偏好、经历、身份、规划、重要结论等）。\n"
    "要求：\n"
    "- 只输出事实本身，不要输出无关的重复或寒暄；\n"
    "- 每条事实尽量完整、独立、可读；\n"
    "- 重要度 importance 用整数 1~5 表示（5 为最重要）；\n"
    "- 输出格式二选一：\n"
    "  方式A：JSON 数组，形如 "
    '[{"content": "事实内容", "importance": 4}]；\n'
    "  方式B：每行一条，以 '- ' 开头，重要度可追加为 "
    "'[重要度:4]'，如：\n"
    "    - 用户偏好阅读科幻小说 [重要度:3]\n"
    "请严格按上述格式之一返回，不要添加多余解释。\n"
    "\n"
    "【对话内容】\n"
)


def _estimate_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数（中文按 2 字符/token，其余按 4 字符/token）。

    用于上下文分块时的开销估算，非精确值。
    """
    mass_estimate = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f":
            mass_estimate += 2
        else:
            mass_estimate += 1
    return max(1, mass_estimate // 2)


class MemoryDistiller:
    """记忆蒸馏器：把超长对话蒸馏为 long_term 记忆并落盘。"""

    def __init__(self, cloud: CloudAdapter, store: MemoryStore):
        """构造记忆蒸馏器。

        :param cloud: CloudAdapter 实例，用于云端 LLM 流式归纳
        :param store: MemoryStore 实例，用于持久化新增记忆
        """
        self._cloud = cloud
        self._store = store
        #: 最近一次批量蒸馏的完整会话（含 rejected/failed，供诊断）
        self.last_sessions: List[Dict] = []

    # ------------------------------------------------------------------ #
    # 公开接口                                                           #
    # ------------------------------------------------------------------ #

    def distill_long_conversation(
        self,
        messages: List[Dict],
        agent_id: str = "default",
        chunk_token_estimate: int = 1800,
    ) -> List[Dict]:
        """蒸馏一段超长对话，返回新增记忆清单（兼容原接口）。

        流程（每 chunk 走状态机：pending→extracting→quality_check→committing→done，
        质量门拒绝则→rejected 不落库）：

            1. 空消息直接返回空列表（不触发云端调用）；
            2. 离线保护：``cloud.is_online() == False`` 时抛 ``DistillationPaused``；
            3. 按「消息条数 / 估算 token」将消息切分为多个 chunk；
            4. 每块拼接蒸馏提示词后流式调用 ``cloud.chat``，拼接返回文本；
            5. 质量门（回归启发式，阈值 0.3）：不合格→rejected，不落库；
            6. 合格逐条 ``store.add`` 落盘为 long_term 记忆（agent_id 归属）。

        :param messages: OpenAI 兼容消息列表（[{role, content}, ...]）
        :param agent_id: 记忆归属的 agent 标识，默认 "default"
        :param chunk_token_estimate: 每块估算 token 上限，默认 1800
        :return: 新增记忆清单（含 id / type / content / importance / agent_id）
        :raises DistillationPaused: 云端离线时抛出，调用方捕获后暂停不报错
        """
        sessions = self.distill_with_sessions(messages, agent_id, chunk_token_estimate)
        added: List[Dict] = []
        for session in sessions:
            added.extend(session.get("added") or [])
        return added

    def distill_with_sessions(
        self,
        messages: List[Dict],
        agent_id: str = "default",
        chunk_token_estimate: int = 1800,
    ) -> List[Dict]:
        """蒸馏并返回完整会话记录（每个 chunk 一个会话，含状态机状态与质量门结论）。

        与 ``distill_long_conversation`` 同一主流程；返回结构为：

        ``[{session_id, agent_id, state, quality_score, reason, facts, added,
            partial, committed, error}]``

        其中 ``state`` 为终止态之一：``done``（已落库）/ ``rejected``（质量门拒绝）/
        ``failed``（异常）。``partial`` 为真表示落库阶段仅部分条目成功（终态 failed），
        ``committed`` 为已成功落库条数，``added`` 如实反映已落库条目（不清空、不误导
        统计）。供管理 API 与测试核对状态机行为。

        :raises DistillationPaused: 云端离线时抛出
        """
        if not messages:
            return []

        if self._cloud.is_online() is False:
            raise DistillationPaused(
                "云端主 LLM 当前离线，记忆蒸馏已暂停，请联网后重试。"
            )

        sessions: List[Dict] = []
        for chunk in self._split_messages(messages, chunk_token_estimate):
            sessions.append(self._distill_chunk(chunk, agent_id))
        self.last_sessions = sessions
        return sessions

    # ------------------------------------------------------------------ #
    # 每个 chunk 的状态机执行                                             #
    # ------------------------------------------------------------------ #

    def _new_session(self, agent_id: str) -> Dict:
        """新建一个蒸馏会话（初始态 pending；partial/committed 记录部分落库语义）。"""
        return {
            "session_id": uuid.uuid4().hex,
            "agent_id": agent_id,
            "state": S_PENDING,
            "quality_score": None,
            "reason": "",
            "facts": [],
            "added": [],
            "partial": False,
            "committed": 0,
            "error": None,
        }

    def _set_state(self, session: Dict, next_state: str) -> None:
        """按状态机转移表推进会话状态；非法转移抛 :class:`DistillStateError`。"""
        current = session["state"]
        if next_state not in _TRANSITIONS.get(current, ()):
            raise DistillStateError(f"非法状态转移：{current} -> {next_state}")
        session["state"] = next_state

    @staticmethod
    def _heuristic_quality_score(facts: List[Dict], raw_text: str) -> float:
        """启发式质量评分（0~1，确定性，不额外消耗云端额度）。

        判据（对齐 CX-O 质量门"极低质→拒绝"的方向）：
        - 无事实或输出为空 -> 0.0（直接拒）；
        - 每条事实 +0.2 分，封顶 0.8；
        - 事实过短（<6 字符）占比过半 -> -0.3（疑似占位/噪声）；
        - 非常用符号（乱码倾向）占比 >15% -> -0.4。
        """
        if not facts or not (raw_text or "").strip():
            return 0.0
        score = min(0.2 + 0.2 * len(facts), 0.8)
        if facts:
            short = sum(1 for f in facts if len(str(f.get("content") or "")) < 6)
            if short / len(facts) > 0.5:
                score *= 0.25  # 占位/过短事实占比过半：乘性降权，确保可被质量门拒绝
        # 乱码倾向：仅统计「非常见符号」（控制字符 / 非 ASCII 非中文标点 / 高位区块），
        # 常见 ASCII（含 JSON 结构字符 []{}":, 等）与中文均视为正常。
        unusual = sum(
            1 for ch in raw_text
            if not _is_normal_char(ch)
        )
        if raw_text and unusual / max(1, len(raw_text)) > 0.15:
            score *= 0.3  # 乱码占比过高：乘性降权
        return max(0.0, min(1.0, score))

    def _distill_chunk(self, chunk: List[Dict], agent_id: str) -> Dict:
        """执行单个 chunk 的蒸馏状态机，返回终止态会话记录。"""
        session = self._new_session(agent_id)
        try:
            # 1) 提取
            self._set_state(session, S_EXTRACT)
            prompt_messages = self._build_prompt_messages(chunk)
            raw_text = self._stream_call(prompt_messages)
            session["raw_text"] = raw_text

            # 2) 质量门（对齐 CX-O：quality_score < 阈值 -> S_REJECT）
            self._set_state(session, S_QUALITY)
            facts = self._parse_facts(raw_text)
            quality = self._heuristic_quality_score(facts, raw_text)
            session["quality_score"] = round(quality, 3)
            session["facts"] = facts
            if quality < QUALITY_REJECT_THRESHOLD:
                session["reason"] = (
                    f"质量分 {quality:.2f} 低于阈值 {QUALITY_REJECT_THRESHOLD:.2f}，"
                    "拒绝落库（可能为乱码/占位/无事实）"
                )
                self._set_state(session, S_REJECT)
                return session

            # 3) 落库（真正持久化，对齐 CX-O 蒸馏必须落真实记忆的要求）。
            #    M8 部分落库语义：单条 fact 落库/int(importance) 异常只跳过该条，
            #    继续落其余事实，不再中断整个 chunk；已成功条目如实保留在 added。
            self._set_state(session, S_COMMIT)
            fact_errors: List[str] = []
            for fact in facts:
                try:
                    content = fact["content"]
                    importance = int(fact.get("importance") or 3)
                    mem_id = self._store.add(
                        {
                            "type": "long_term",
                            "content": content,
                            "importance": importance,
                            "agent_id": agent_id,
                        }
                    )
                except Exception as fact_exc:  # noqa: BLE001 - 单条失败不放大为整块失败
                    fact_errors.append(f"第 {len(session['added']) + len(fact_errors) + 1} 条落库失败：{fact_exc}")
                    continue
                session["added"].append(
                    {
                        "id": mem_id,
                        "type": "long_term",
                        "content": content,
                        "importance": importance,
                        "agent_id": agent_id,
                    }
                )
            session["committed"] = len(session["added"])
            if fact_errors:
                # 部分落库：终态仍 failed，但已落库条目如实保留（不清空/不误导统计）
                session["partial"] = True
                session["reason"] = (
                    f"质量分 {quality:.2f}，部分落库 {session['committed']}/{len(facts)} 条"
                )
                session["error"] = "；".join(fact_errors)
                self._set_state(session, S_FAILED)
                return session

            session["reason"] = f"质量分 {quality:.2f}，已落库 {len(session['added'])} 条"
            self._set_state(session, S_DONE)
            return session
        except Exception as exc:  # noqa: BLE001 - 单 chunk 失败不阻断其余 chunk
            session["error"] = str(exc)
            try:
                self._set_state(session, S_FAILED)
            except DistillStateError:
                # 最后防线兜底：非法转移不应静默。正常情况下转移表已覆盖
                # extracting / quality_check / committing 三个在途态到 failed 的边，
                # 走到这里说明出现了状态机之外的非预期转移（如未来新增在途态
                # 却忘记补 failed 边），必须显式告警而不是悄悄覆写状态。
                print(
                    f"[WARNING] 蒸馏状态机发生非预期转移（{session['state']} -> {S_FAILED}），"
                    f"已强制置为 failed 兜底。会话：{session.get('session_id')}",
                    file=sys.stderr,
                )
                session["state"] = S_FAILED
            return session

    # ------------------------------------------------------------------ #
    # 私有辅助方法                                                       #
    # ------------------------------------------------------------------ #

    def _split_messages(
        self, messages: List[Dict], chunk_token_estimate: int
    ) -> List[List[Dict]]:
        """按估算 token 把消息贪心切分为多个 chunk（每块至少 1 条消息）。

        单条消息即便超限也独立成块，避免丢失消息。
        """
        chunks: List[List[Dict]] = []
        current: List[Dict] = []
        current_tokens = 0
        for msg in messages:
            text = str(msg.get("content") or "")
            tokens = _estimate_tokens(text)
            if current and current_tokens + tokens > chunk_token_estimate:
                chunks.append(current)
                current = []
                current_tokens = 0
            current.append(msg)
            current_tokens += tokens
        if current:
            chunks.append(current)
        return chunks

    def _build_prompt_messages(self, chunk: List[Dict]) -> List[Dict]:
        """构造发给云端的蒸馏提示：把对话逐条拼接进 [system, user] 消息。"""
        transcript_lines = []
        for msg in chunk:
            role = str(msg.get("role") or "unknown")
            content = str(msg.get("content") or "")
            transcript_lines.append(f"{role}: {content}")
        transcript = "\n".join(transcript_lines)
        return [
            {"role": "system", "content": "你是记忆蒸馏助手，严格按要求输出。"},
            {"role": "user", "content": DISTILL_PROMPT + transcript},
        ]

    def _stream_call(self, prompt_messages: List[Dict]) -> str:
        """流式调用云端 LLM，把流式文本块拼接为完整字符串。"""
        parts = []
        for part in self._cloud.chat(prompt_messages):
            parts.append(part)
        return "".join(parts)

    def _parse_facts(self, raw_text: str) -> List[Dict]:
        """解析云端返回，兼容 JSON 数组与行式 "- " 两种格式。

        结果元素统一为 dict：{content, importance}；importance 缺失时默认 3。
        """
        stripped = (raw_text or "").strip()
        if not stripped:
            return []

        # 剥离 markdown 代码块包裹
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            stripped = "\n".join(lines).strip()

        # 优先尝试 JSON 数组
        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    return self._facts_from_json_list(data)
            except json.JSONDecodeError:
                pass  # 回落行式解析

        # 行式解析
        return self._facts_from_lines(stripped)

    @staticmethod
    def _facts_from_json_list(data: list) -> List[Dict]:
        """JSON 数组：元素可为字符串或含 content/importance 的对象。"""
        facts = []
        for item in data:
            if isinstance(item, str):
                facts.append({"content": item.strip(), "importance": 3})
            elif isinstance(item, dict):
                content = item.get("content") or item.get("fact")
                if not isinstance(content, str) or not content.strip():
                    continue
                importance = item.get("importance", 3)
                facts.append(
                    {
                        "content": content.strip(),
                        "importance": int(importance) if importance is not None else 3,
                    }
                )
        return facts

    @staticmethod
    def _facts_from_lines(text: str) -> List[Dict]:
        """行式列表：每行以 "- / * / • / 数字." 开头，重要度可含 [重要度:N]。"""
        facts = []
        for line in text.splitlines():
            raw = line.rstrip()
            stripped_line = raw.strip()
            if not stripped_line:
                continue
            first = stripped_line[0]
            if first not in "-*•":
                # 兼容 "1. xxx" 数字前缀
                head = stripped_line.split(".", 1)
                if not (head[0].isdigit() and len(head) > 1):
                    continue
                body = head[1].strip()
            else:
                body = stripped_line[1:].strip()
            if not body:
                continue
            importance = 3
            # 提取 [重要度:N] 标记
            if "[重要度:" in body and body.rstrip().endswith("]"):
                r_idx = body.rfind("[重要度:")
                tail = body[r_idx + len("[重要度:"):-1].strip()
                try:
                    importance = int(float(tail))
                except (ValueError, TypeError):
                    pass
                body = body[:r_idx].strip()
                body = body.rstrip(" ").rstrip("-").strip()
            body = body.strip()
            if body:
                facts.append({"content": body, "importance": importance})
        return facts