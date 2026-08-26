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
from typing import Dict, List

from lite.cloud.adapter import CloudAdapter
from lite.memory.storage import MemoryStore


class DistillationPaused(Exception):
    """蒸馏暂停异常。

    触发场景：记忆蒸馏执行前检测到云端主 LLM 离线（``cloud.is_online() == False``）。
    调用方捕获本异常后应将蒸馏任务标记为"暂停"，静默跳过当前批次，不向用户报错。
    """


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

    # ------------------------------------------------------------------ #
    # 公开接口                                                           #
    # ------------------------------------------------------------------ #

    def distill_long_conversation(
        self,
        messages: List[Dict],
        agent_id: str = "default",
        chunk_token_estimate: int = 1800,
    ) -> List[Dict]:
        """蒸馏一段超长对话，返回新增记忆清单。

        流程：
            1. 空消息直接返回空列表（不触发云端调用）；
            2. 离线保护：``cloud.is_online() == False`` 时抛 ``DistillationPaused``；
            3. 按「消息条数 / 估算 token」将消息切分为多个 chunk；
            4. 每块拼接蒸馏提示词后流式调用 ``cloud.chat``，拼接返回文本；
            5. 解析云端返回（JSON 数组或行式列表）得到事实与重要度；
            6. 逐条调用 ``store.add`` 落盘为 long_term 记忆（agent_id 归属）。

        :param messages: OpenAI 兼容消息列表（[{role, content}, ...]）
        :param agent_id: 记忆归属的 agent 标识，默认 "default"
        :param chunk_token_estimate: 每块估算 token 上限，默认 1800
        :return: 新增记忆清单（含 id / type / content / importance / agent_id）
        :raises DistillationPaused: 云端离线时抛出，调用方捕获后暂停不报错
        """
        if not messages:
            return []

        if self._cloud.is_online() is False:
            raise DistillationPaused(
                "云端主 LLM 当前离线，记忆蒸馏已暂停，请联网后重试。"
            )

        added: List[Dict] = []
        for chunk in self._split_messages(messages, chunk_token_estimate):
            prompt_messages = self._build_prompt_messages(chunk)
            raw_text = self._stream_call(prompt_messages)
            facts = self._parse_facts(raw_text)
            for fact in facts:
                content = fact["content"]
                importance = fact.get("importance") or 3
                mem_id = self._store.add(
                    {
                        "type": "long_term",
                        "content": content,
                        "importance": int(importance),
                        "agent_id": agent_id,
                    }
                )
                added.append(
                    {
                        "id": mem_id,
                        "type": "long_term",
                        "content": content,
                        "importance": int(importance),
                        "agent_id": agent_id,
                    }
                )
        return added

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