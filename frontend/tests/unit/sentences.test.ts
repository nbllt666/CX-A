import { describe, it, expect } from 'vitest';
import { splitSentences } from '../../src/renderer/sentences';

/**
 * splitSentences 单测（20261006 全双工语音降级版）：
 * 按标点切句是「逐句轮询 LLM」的地基——切分口径直接决定语音对话节奏。
 */

describe('splitSentences：按标点符号切句', () => {
  it('终端标点切分：保留句尾标点', () => {
    expect(splitSentences('今天天气不错。我们去公园吧？')).toEqual([
      '今天天气不错。',
      '我们去公园吧？',
    ]);
  });

  it('分句标点（，、：；）同样作为切分点', () => {
    expect(splitSentences('先吃早饭，然后散步，最后看看书')).toEqual([
      '先吃早饭，',
      '然后散步，',
      '最后看看书',
    ]);
    expect(splitSentences('苹果、香蕉和橘子')).toEqual(['苹果、', '香蕉和橘子']);
  });

  it('连续标点（！！！）各自成切分点，空句被过滤', () => {
    expect(splitSentences('太好了！！！')).toEqual(['太好了！', '！', '！']);
  });

  it('无标点长句整体返回为单句', () => {
    expect(splitSentences('这是一句没有标点的很长的话')).toEqual([
      '这是一句没有标点的很长的话',
    ]);
  });

  it('空串 / 纯空白 → 空数组', () => {
    expect(splitSentences('')).toEqual([]);
    expect(splitSentences('   \n\t ')).toEqual([]);
  });

  it('换行同为分隔符；句首尾空白被裁剪', () => {
    expect(splitSentences('第一行\n第二行')).toEqual(['第一行', '第二行']);
    expect(splitSentences('  喂，在吗。  ')).toEqual(['喂，', '在吗。']);
  });

  it('省略号（…）作为切分点；连续省略号逐字切分（中间空段过滤）', () => {
    expect(splitSentences('我想想……对了')).toEqual(['我想想…', '…', '对了']);
  });
});
