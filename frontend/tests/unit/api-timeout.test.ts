import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchModelProgress, requestJson } from '../../src/renderer/api';

const URL = 'http://127.0.0.1:8600/api/test-timeout';

function waitForAbort(signal: AbortSignal): Promise<never> {
  return new Promise((_resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  });
}

function stubPendingBody(ok = true) {
  const readBody = vi.fn((signal: AbortSignal) => waitForAbort(signal));
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => ({
    ok,
    status: ok ? 200 : 503,
    statusText: ok ? 'OK' : 'Service Unavailable',
    json: () => readBody(init!.signal!),
    text: () => readBody(init!.signal!),
  } as unknown as Response));
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, readBody };
}

describe('requestJson timeout and caller cancellation', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([true, false])('times out while reading a response body (ok=%s)', async (ok) => {
    vi.useFakeTimers();
    const { readBody } = stubPendingBody(ok);
    const pending = requestJson(URL, undefined, 1000);
    const assertion = expect(pending).rejects.toThrow(/\u540e\u7aef\u8bf7\u6c42\u8d85\u65f6/);

    await vi.advanceTimersByTimeAsync(0);
    expect(readBody).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([true, false])('keeps caller cancellation active during body reads (ok=%s)', async (ok) => {
    vi.useFakeTimers();
    const { readBody } = stubPendingBody(ok);
    const controller = new AbortController();
    const reason = new DOMException('Cancelled by caller', 'AbortError');
    const removeListener = vi.spyOn(controller.signal, 'removeEventListener');
    const pending = requestJson(URL, { signal: controller.signal }, 1000);
    const assertion = expect(pending).rejects.toBe(reason);

    await vi.advanceTimersByTimeAsync(0);
    expect(readBody).toHaveBeenCalledTimes(1);
    controller.abort(reason);
    await assertion;
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels a pending fetch without reporting a timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => waitForAbort(init!.signal!));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const pending = requestJson(URL, { signal: controller.signal }, 1000);
    const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });

    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    controller.abort();
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not issue a request if the caller signal is already aborted', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    controller.abort();

    await expect(requestJson(URL, { signal: controller.signal })).rejects.toBe(controller.signal.reason);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards download progress cancellation to the request wrapper', async () => {
    vi.useFakeTimers();
    const { readBody } = stubPendingBody();
    const controller = new AbortController();
    const pending = fetchModelProgress(controller.signal);
    const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });

    await vi.advanceTimersByTimeAsync(0);
    expect(readBody).toHaveBeenCalledTimes(1);
    controller.abort();
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('removes cancellation listeners and timers after success', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ ok: true })),
    );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const removeListener = vi.spyOn(controller.signal, 'removeEventListener');

    await expect(requestJson(URL, { signal: controller.signal }, 1000)).resolves.toEqual({ ok: true });
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
    controller.abort();
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false);
  });

  it('cleans up after JSON parse failures and preserves the parse error', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(async () => new Response('not json')));
    const controller = new AbortController();
    const removeListener = vi.spyOn(controller.signal, 'removeEventListener');

    await expect(requestJson(URL, { signal: controller.signal }, 1000)).rejects.toBeInstanceOf(SyntaxError);
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
  });
});
