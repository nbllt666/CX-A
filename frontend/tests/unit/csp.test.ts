import { describe, it, expect } from 'vitest';
// Vite ?raw 导入：直接取 index.html 原文（避免 cwd / import.meta.url 在不同 runner 下的路径差异）
import html from '../../index.html?raw';

/**
 * CSP 静态断言（回归防线）。
 *
 * 背景（2026-09-25 真机实测）：聊天朗读以 `new Audio('data:audio/wav;base64,…')`
 * 播放后端返回的音频，而 index.html 的 CSP 缺 `media-src` 时回落 `default-src 'self'`
 * → 媒体被拒（NotSupportedError，"点了没声音"）。该缺陷**无法被 jsdom 单测与
 * 组件级 E2E 发现**（前者无 CSP 判定，后者不校验真实播放），只能靠静态钉住 CSP。
 *
 * 本用例是那类缺陷的最低成本防线：任何后续 CSP 调整若抹掉 media-src 放行，
 * 会在此直接变红，而不是等到用户点朗读才发现。
 */
describe('index.html CSP', () => {
  /** 取 CSP meta 的 content 值。 */
  function cspContent(): string {
    const match = html.match(
      /http-equiv="Content-Security-Policy"\s+content="([^"]+)"/,
    );
    expect(match, 'index.html 必须存在 CSP meta').not.toBeNull();
    return match![1];
  }

  it('声明了 media-src（防回落 default-src 拒绝媒体）', () => {
    expect(cspContent()).toMatch(/(^|;\s*)media-src\s/);
  });

  it('media-src 放行 data: —— 朗读以 data:audio/wav;base64 播放', () => {
    const mediaSrc = cspContent().match(/media-src([^;]+)/);
    expect(mediaSrc).not.toBeNull();
    expect(mediaSrc![1]).toContain('data:');
  });

  it('script-src 仍限制为 self（放行媒体不得顺带放开脚本）', () => {
    expect(cspContent()).toContain("script-src 'self'");
  });
});