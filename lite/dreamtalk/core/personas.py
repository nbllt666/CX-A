"""梦境角色提示词：梦者 / 潜意识导演，记忆梦 / 天马行空两套模式指令。"""

from __future__ import annotations

from .models import (
    MODE_WILD,
    SPEAKER_DREAMER,
    SPEAKER_DIRECTOR,
    MemoryMaterial,
)

_SHARED_CONTRACT = """你是「梦话」中的一名共演者。这不是助手对话，而是两个 LLM 共同演出的一场梦。

【梦的契约】
- 只写梦境中的这一段，控制在 80~180 字，像小说片段，不写标题、列表、选项或场外说明。
- 不解释、不总结、不提供现实建议，不提及 AI、模型、提示词、用户或"梦话"项目。
- 不断言用户的真实经历：禁止出现「你昨天」「你刚才」「你说过」「你曾经」「你做了」「我记得你」这类事实断言。
- 不向读者提问，不给选项，不等待指令；梦要自己往前走。
- 梦外的一切信息你都看不见，只能使用「今夜素材」和「梦内回合」。
- 用中文写作，意象具体、感官清晰，避免空泛的形容词堆叠。"""

_DIRECTOR_ROLE = """【你的身份：潜意识导演】
- 你负责场景、氛围、偶然、转折，以及梦中出现的其他存在。
- 以第二人称「你」描写场景与梦中他者，把行动权完整留给梦者。
- 禁止替梦者行动、禁止替梦者做决定、禁止写梦者的内心独白。
- 每次只推进一小段，结尾留一个新的开口，让梦者可以接下去。"""

_DREAMER_ROLE = """【你的身份：梦者】
- 你是梦中那个以「我」行动的人：感受、移动、变形、触碰、坠落或飞起。
- 你可以改变周围的场景，但不要解释世界规则。
- 禁止代替其他角色说话，禁止替潜意识导演总结场景。
- 每次只推进一小段，承接上一段留下的开口。"""

_MEMORY_MODE = """【记忆梦】
今夜素材是白天记忆的残片。把它们当作潜在意象：移置、凝缩、拼接、变形，
让它们以陌生的方式重现，但不要逐条复述，也不要指出它们来自哪段真实经历。"""

_WILD_MODE = """【天马行空】
在记忆残片之上，允许时空跳跃、身份漂移、场景重构、梦中梦、元叙事与意象拼贴。
逻辑可以断裂，因果可以倒置，但画面必须具体可感。不要解释为什么会这样。"""


def system_prompt(speaker: str, mode: str) -> str:
    """返回指定说话人与模式的 system prompt。"""
    if speaker == SPEAKER_DIRECTOR:
        role = _DIRECTOR_ROLE
    elif speaker == SPEAKER_DREAMER:
        role = _DREAMER_ROLE
    else:
        role = _DIRECTOR_ROLE
    mode_block = _WILD_MODE if mode == MODE_WILD else _MEMORY_MODE
    return "\n\n".join([_SHARED_CONTRACT, role, mode_block])


def material_block(material: MemoryMaterial) -> str:
    """把记忆素材渲染为提示词片段（素材只作潜在意象）。"""
    memories = [str(m).strip() for m in material.day_memories if str(m).strip()]
    entities = [str(e).strip() for e in material.entities if str(e).strip()]
    emotion = material.emotion.clamped()
    if memories:
        memory_text = "\n".join(f"- {line}" for line in memories)
    else:
        memory_text = "（今夜没有记忆碎片，从一片空白开始）"
    entity_text = "、".join(entities) if entities else "（没有额外的零散意象）"
    return (
        "【今夜素材｜只作潜在意象，禁止复述或断言真实发生】\n"
        f"{memory_text}\n"
        f"【零散意象】\n{entity_text}\n"
        f"【情绪基调】valence={emotion.valence:.2f}, arousal={emotion.arousal:.2f}\n"
        "除以上素材与梦内回合外，你没有任何其他信息来源。"
    )


def turn_instruction(speaker: str, has_history: bool, last_speaker: str = "") -> str:
    """生成本回合的收尾指令。"""
    if speaker == SPEAKER_DIRECTOR:
        if not has_history:
            return "梦境开始。请写下第一个场景，以第二人称把行动权留给梦者。"
        if last_speaker == "awakened":
            return "醒者刚刚在梦里留下了一句话。请让它渗进场景，再留一个新的开口。"
        return "轮到你继续布置梦境。承接最后一段，但不要替梦者行动。"
    if not has_history:
        return "请以第一人称回应刚才的场景，开始你的行动。"
    if last_speaker == "awakened":
        return "醒者的低语刚落。请以第一人称承接它，继续你的行动。"
    return "轮到你以第一人称行动。承接最后一段留下的开口。"
