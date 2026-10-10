import { describe, it, expect } from 'vitest';
import { StreamTextPipeline } from '../../src/renderer/streamText';

/**
 * 流式回复增量管线单测（20261010_模块0_语音交互延迟优化）。
 *
 * 核心语义：raw 增量 → 剥已识别 [emotion:x] 标签（与后端 EmotionTagParser 同口径：
 * 支持集剥离 / 未知保留 / 大小写不敏感 / 畸形空参数保留）→ 悬挂 "[emotion:" 段
 * 跨 chunk 缓冲 → 句边界（。！？；、，：…换行）凑齐即产出。
 */

describe('StreamTextPipeline：标签剥离（与后端同口径）', () => {
  it('已识别标签剥离且 mood 取首个', () => {
    const p = new StreamTextPipeline();
    const out = p.push('[emotion:happy]今天[emotion:sad]天气不错');
    expect(out).toEqual([]); // 无句尾标点 → 不产出
    expect(p.flush()).toEqual(['今天天气不错']);
    expect(p.getMood()).toBe('happy');
  });

  it('未知标签原文保留、不影响 mood（[badtag:xx] / [emotion:] 畸形）', () => {
    const p = new StreamTextPipeline();
    const out = p.push('[badtag:xx]你好[emotion:]呀！');
    expect(out).toEqual(['[badtag:xx]你好[emotion:]呀！']); // 两者均原文保留
    expect(p.flush()).toEqual([]);
    expect(p.getMood()).toBeUndefined();
  });

  it('大小写不敏感（[EMOTION:HAPPY] 等价小写）', () => {
    const p = new StreamTextPipeline();
    const out = p.push('[EMOTION:HAPPY]早上好！');
    expect(out).toEqual(['早上好！']);
    expect(p.flush()).toEqual([]);
    expect(p.getMood()).toBe('happy');
  });
});

describe('StreamTextPipeline：跨 chunk 悬挂缓冲', () => {
  it('标签被 chunk 撕裂：半截不下发，闭合后整段剥离', () => {
    const p = new StreamTextPipeline();
    const a = p.push('好的[emo'); // 悬挂：[ 后无 ] → 半截标签起不下发
    expect(a).toEqual([]); // 无句尾标点 → 不产出（"好的"已入 buffer 待句号）
    const b = p.push('tion:happy]好的，');
    expect(b).toEqual(['好的好的，']); // 标签闭合且被剥掉，句尾标点凑齐
    expect(p.flush()).toEqual([]);
    expect(p.getMood()).toBe('happy');
  });

  it('首个完整句在生成中途即产出（不等 done）', () => {
    const p = new StreamTextPipeline();
    expect(p.push('[emotion:calm]好')).toEqual([]);
    expect(p.push('的，')).toEqual(['好的，']); // 逗号即句边界（与 TTS 切分同口径）
    expect(p.push('我在呢。')).toEqual(['我在呢。']);
    expect(p.flush()).toEqual([]);
  });

  it('flush 强制收口：未闭合标签原文保留、残余文本下发', () => {
    const p = new StreamTextPipeline();
    p.push('好的。后面还有[emotion:hap');
    expect(p.flush()).toEqual(['后面还有[emotion:hap']); // 未闭合标签原文保留
  });
});

describe('StreamTextPipeline：句边界与多句', () => {
  it('一次 push 含多句 → 全部按序产出', () => {
    const p = new StreamTextPipeline();
    const out = p.push('第一句。第二句！第三句，尾句');
    expect(out).toEqual(['第一句。第二句！第三句，']);
    expect(p.flush()).toEqual(['尾句']);
  });

  it('长回复流式喂入：产出顺序与原文一致', () => {
    const p = new StreamTextPipeline();
    const collected: string[] = [];
    const chunks = ['[emotion:happy]', '好', '呀，', '今天', '天气不错。', '想做点', '什么？'];
    for (const c of chunks) collected.push(...p.push(c));
    collected.push(...p.flush());
    expect(collected.join('')).toBe('好呀，今天天气不错。想做点什么？');
    expect(p.getMood()).toBe('happy');
  });
});
