import { describe, it, expect } from 'vitest';
import {
  concatFloat32,
  downsample,
  pcm16ToBase64,
  startRecording,
  TARGET_SAMPLE_RATE,
  toPcm16,
} from '../../src/renderer/audioRecorder';

/**
 * 语音输入采集模块的单测（纯函数为主，不依赖浏览器音频设备）。
 *
 * 覆盖：
 * - downsample：等率直通 / 44.1k→16k 长度与线性插值边界 / 空输入与非法参数；
 * - toPcm16：[-1,1] 映射与钳制（防越界回绕）；
 * - pcm16ToBase64：已知样本 → 已知 base64（小端）；
 * - concatFloat32：多块按序合并；
 * - startRecording：无 Web Audio 环境（jsdom）时给出明确中文错误。
 */

describe('audioRecorder 纯函数', () => {
  it('downsample：等率输入原样返回', () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsample(input, TARGET_SAMPLE_RATE, TARGET_SAMPLE_RATE)).toBe(input);
  });

  it('downsample：44.1k → 16k 长度按比例收缩', () => {
    const input = new Float32Array(44100);
    const output = downsample(input, 44100, TARGET_SAMPLE_RATE);
    expect(Math.abs(output.length - 16000)).toBeLessThanOrEqual(1);
  });

  it('downsample：线性插值保留端点值', () => {
    const input = new Float32Array([0, 1, 0, -1]);
    const output = downsample(input, 4, 2);
    expect(output.length).toBe(2);
    expect(output[0]).toBeCloseTo(0, 5);
    expect(output[output.length - 1]).toBeCloseTo(-1, 5);
  });

  it('downsample：空输入与非法参数原样返回', () => {
    const empty = new Float32Array(0);
    expect(downsample(empty, 44100, 16000)).toBe(empty);
    const input = new Float32Array([0.5]);
    expect(downsample(input, 0, 16000)).toBe(input);
    expect(downsample(input, 44100, 0)).toBe(input);
  });

  it('toPcm16：[-1,1] 映射到 int16 并钳制', () => {
    const pcm = toPcm16(new Float32Array([0, 1, -1, 2, -2]));
    expect(Array.from(pcm)).toEqual([0, 32767, -32767, 32767, -32768]);
  });

  it('pcm16ToBase64：小端样本 → 已知 base64', () => {
    // 0x0100 = 256（小端 bytes: 00 01）；-1 = 0xFFFF（bytes: FF FF）
    const pcm = new Int16Array([256, -1]);
    expect(pcm16ToBase64(pcm)).toBe(btoa('\u0000\u0001\u00ff\u00ff'));
  });

  it('concatFloat32：多块按序合并', () => {
    const merged = concatFloat32([
      new Float32Array([1, 2]),
      new Float32Array([3]),
      new Float32Array([4, 5]),
    ]);
    expect(Array.from(merged)).toEqual([1, 2, 3, 4, 5]);
  });

  it('startRecording：无 Web Audio 环境给出明确中文错误', async () => {
    await expect(startRecording()).rejects.toThrow(/不支持麦克风录音/);
  });
});