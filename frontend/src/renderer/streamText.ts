/**
 * 流式回复增量管线（20261010_模块0_语音交互延迟优化）。
 *
 * 把 LLM 流式增量（/api/chat/message_stream 的 delta 帧）实时转成「可立即朗读的
 * 完整句」——首个完整句在 LLM 生成出第一个句号时即送 TTS，不等完整回复。
 *
 * 三段处理（与后端 lite/avatar/tags.py EmotionTagParser 同口径）：
 * 1. **剥已识别标签**：`[emotion:x]`（x ∈ 支持集，大小写不敏感）从干净文本移除，
 *    mood 取首个已识别情绪；未知标签（`[badtag:xx]`、`[emotion:]` 畸形等）原文保留；
 * 2. **悬挂缓冲**：最后一个未闭合的 `[` 起的内容暂不下发——标签可能跨 chunk
 *    撕裂，半截标签既不能朗读也不该提前判定；流结束 flush 时强制下发（未闭合
 *    标签原文保留，与后端畸形标签口径一致）；
 * 3. **句边界切分**：与 splitSentences 同标点口径（。！？；、，：…换行），凑齐
 *    完整句立即产出，残余留 buffer。
 */

/** 支持的情绪集（与后端 SUPPORTED_EMOTIONS 同口径；前端 petMood 另有渲染归一） */
export const SUPPORTED_EMOTIONS: ReadonlySet<string> = new Set([
  'happy',
  'calm',
  'sad',
  'surprised',
  'angry',
  'sleepy',
  'shy',
]);

/** [emotion:x] 标签匹配（大小写不敏感；与后端 _EMOTION_TAG_RE 同形） */
const EMOTION_TAG_RE = /\[emotion:([^\]]*)\]/gi;

/** 句尾标点（与 sentences.ts SENTENCE_SPLIT_RE 同字符集） */
const SENTENCE_END_RE = /[。！？；、，：…\n]/;

export class StreamTextPipeline {
  /** 未处理的 raw 累积（含标签原文） */
  private raw = '';
  /** raw 中「已剥标签前缀」对应的剥后文本量（已确认下发） */
  private emittedCleanLen = 0;
  /** 尚未凑齐完整句的剥后文本残余 */
  private buffer = '';
  /** 首个已识别情绪（提前驱动表情；done 帧权威值兜底） */
  private mood: string | undefined;

  /** 首个已识别情绪；尚无标签时 undefined */
  getMood(): string | undefined {
    return this.mood;
  }

  /**
   * 喂入一个 raw 增量。
   *
   * @returns 本次可立即朗读的完整句数组（可能为空；多句时按顺序）
   */
  push(delta: string): string[] {
    if (!delta) return [];
    this.raw += delta;
    return this.consume(false);
  }

  /**
   * 流结束：悬挂段（未闭合标签等）强制收口——未闭合标签原文保留（后端畸形
   * 标签同口径），全部残余一次性产出（含句切分后无标点的尾部残句）。
   *
   * @returns 剩余可朗读文本（可能为空串）
   */
  flush(): string[] {
    const out = this.consume(true);
    // consume 的句切分只产出「句尾标点齐」的部分；尾部残句在此强制产出
    const tail = this.buffer.trim();
    this.buffer = '';
    if (tail) out.push(tail);
    return out;
  }

  /**
   * 核心消费：计算本次可安全处理的 raw 前缀（非 flush 时止于最后一个未闭合
   * `[`），剥已识别标签后取增量文本，凑完整句产出。
   */
  private consume(force: boolean): string[] {
    // 悬挂点：最后一个未闭合 '[' 的位置（其后可能有跨 chunk 标签残段）
    let safeEnd = this.raw.length;
    if (!force) {
      const open = this.raw.lastIndexOf('[');
      if (open !== -1 && this.raw.indexOf(']', open) === -1) {
        safeEnd = open;
      }
    }
    const safeRaw = this.raw.slice(0, safeEnd);
    // 剥已识别标签（幂等：对前缀重复剥结果一致）+ 提取首个 mood
    const clean = this.stripTags(safeRaw);
    // 本次新增的剥后文本（此前已下发部分不重发）
    const fresh = clean.slice(this.emittedCleanLen);
    this.emittedCleanLen = clean.length;
    if (!fresh) return [];
    this.buffer += fresh;
    // 取完整句：最后一个句尾标点（含）之前的全部文本一次性产出
    const out: string[] = [];
    let lastEnd = -1;
    for (let i = this.buffer.length - 1; i >= 0; i -= 1) {
      if (SENTENCE_END_RE.test(this.buffer[i])) {
        lastEnd = i;
        break;
      }
    }
    if (lastEnd !== -1) {
      const spoken = this.buffer.slice(0, lastEnd + 1).trim();
      this.buffer = this.buffer.slice(lastEnd + 1);
      if (spoken) out.push(spoken);
    }
    return out;
  }

  /** 剥已识别 [emotion:x] 标签（副作用：记录首个已识别 mood）；未知标签原文保留 */
  private stripTags(text: string): string {
    return text.replace(EMOTION_TAG_RE, (match, arg: string) => {
      const emotion = arg.trim().toLowerCase();
      if (SUPPORTED_EMOTIONS.has(emotion)) {
        if (!this.mood) this.mood = emotion;
        return '';
      }
      return match;
    });
  }
}
