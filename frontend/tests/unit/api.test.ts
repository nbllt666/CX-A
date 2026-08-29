import { describe, it, expect, vi, afterEach } from 'vitest';
import { requestJson } from '../../src/renderer/api';

/**
 * Test1 · requestJson 非 2xx 错误体透出（D5 修复回归）。
 *
 * 修复前：非 2xx 直接丢弃响应体，后端错误码 / 错误说明不可见；
 * 修复后：读取响应体片段（截断 200 字符）拼入 Error message。
 */
describe('requestJson：非 2xx 错误体透出（D5）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('错误信息包含后端响应体中的错误码', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ error: 'ERR_AUTH_REQUIRED' }), {
          status: 401,
          statusText: 'Unauthorized',
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(requestJson('http://127.0.0.1:8600/api/x')).rejects.toThrow(/ERR_AUTH_REQUIRED/);
    await expect(requestJson('http://127.0.0.1:8600/api/x')).rejects.toThrow(
      /后端请求失败: 401 Unauthorized/,
    );
  });

  it('超长响应体截断到 200 字符，错误信息不整体透传', async () => {
    const longBody = 'A'.repeat(500);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(longBody, { status: 503, statusText: 'Service Unavailable' }),
      ),
    );

    const err: unknown = await requestJson('http://127.0.0.1:8600/api/x').catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    const message = (err as Error).message;
    expect(message).toContain('A'.repeat(200));
    expect(message).not.toContain('A'.repeat(201));
  });

  it('2xx 时正常返回解析后的 JSON，不受错误体逻辑影响', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ ok: true, value: 42 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(requestJson<{ ok: boolean; value: number }>('http://127.0.0.1:8600/api/x')).resolves.toEqual({
      ok: true,
      value: 42,
    });
  });
});
