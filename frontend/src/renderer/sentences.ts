/**
 * 句子切分（20261006 全双工语音降级版）。
 *
 * 识别文本（SenseVoice 输出带标点）按标点符号切分为句，供全双工模式逐句
 * 轮询 LLM——用户一句话识别出多句时拆开依次发送，形成「边说边回」。
 * 与 TTS 侧的 text_splitter（lite/audio/text_splitter.py，按标点切 TTS 块）
 * 同为标点切分口径：终端标点（。！？；…）与分句标点（，、：）都作为切分点，
 * 换行同为分隔。切出的句子**保留尾部标点**（LLM 语境完整），空句过滤。
 */

/** 句子切分正则：终端/分句标点 + 换行；标点本身保留在句尾。 */
const SENTENCE_SPLIT_RE = /(?<=[。！？；、，：…\n])/;

/** 切分后剔除的纯空白残片 */
function isBlank(text: string): boolean {
  return text.trim().length === 0;
}

/**
 * 按标点符号把文本切分为句（保留尾部标点、过滤空句）。
 *
 * 无任何标点的长句整体返回为单句（识别引擎没给标点时整段一句话处理）；
 * 连续标点（如「！！！」）各自成切分点，产生的空句被过滤。
 *
 * @param text 识别文本（可能含标点与换行）
 * @returns 句子数组（顺序保持原文；空文本返回空数组）
 */
export function splitSentences(text: string): string[] {
  if (!text || !text.trim()) return [];
  return text
    .split(SENTENCE_SPLIT_RE)
    .map((part) => part.trim())
    .filter((part) => !isBlank(part));
}
