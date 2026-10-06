import { describe, it, expect, vi, afterEach } from 'vitest';
import { requestJson, synthesizeSpeech, synthesizeSpeechStream, transcribeAudio } from '../../src/renderer/api';

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

/* ────────────────────────────────────────────────────────────────────────────
 * 追加（Task 8）：首启向导 6 个请求函数的 URL / method / body 断言。
 * 既有用例与断言零修改，本段为纯追加。
 * ──────────────────────────────────────────────────────────────────────────── */
import {
  API_ENDPOINTS,
  cancelModelDownload,
  completeSetup,
  fetchModelProgress,
  fetchSetupRecommend,
  fetchSetupStatus,
  startModelDownload,
} from '../../src/renderer/api';

describe('首启向导接口：URL / method / body（Task 8）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  /** stub fetch 返回 2xx JSON，返回 mock 以便断言调用参数 */
  function stubJson(payload: unknown): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('fetchSetupStatus → GET /api/setup/status', async () => {
    const fetchMock = stubJson({
      completed: false,
      wizard_required: true,
      download: { channel: 'mirror' },
      local_llm_source: 'modelscope',
    });
    const data = await fetchSetupStatus();
    expect(data.wizard_required).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.status);
    expect(API_ENDPOINTS.setup.status).toMatch(/\/api\/setup\/status$/);
    expect(init.method ?? 'GET').toBe('GET');
  });

  it('fetchSetupRecommend → GET /api/setup/recommend', async () => {
    const fetchMock = stubJson({
      profile: {},
      recommendation: { use_local: true, device: 'cpu', tier: '1.7B', reasons: [], probe_notes: [] },
      tiers: [],
      suggested_source: 'modelscope',
    });
    await fetchSetupRecommend();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.recommend);
    expect(API_ENDPOINTS.setup.recommend).toMatch(/\/api\/setup\/recommend$/);
    expect(init.method ?? 'GET').toBe('GET');
  });

  it('completeSetup → POST /api/setup/complete，body 携带完整选择', async () => {
    const fetchMock = stubJson({
      ok: true,
      applied: [],
      ignored: [],
      setup: { completed: true, completed_at: 'x' },
      config: {},
    });
    await completeSetup({
      cloud: { provider: 'deepseek', api_key: 'sk-test' },
      download: { channel: 'official' },
      local_llm: { enabled: false },
      apply_recommended: true,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.complete);
    expect(API_ENDPOINTS.setup.complete).toMatch(/\/api\/setup\/complete$/);
    expect(init.method).toBe('POST');
    // 模型仓库由线路在服务端派生：请求体不含 local_llm.source
    expect(JSON.parse(String(init.body))).toEqual({
      cloud: { provider: 'deepseek', api_key: 'sk-test' },
      download: { channel: 'official' },
      local_llm: { enabled: false },
      apply_recommended: true,
    });
    expect(JSON.parse(String(init.body)).local_llm).not.toHaveProperty('source');
  });

  it('startModelDownload → POST /api/setup/model/download（入参可省，省时发 {}）', async () => {
    const fetchMock = stubJson({ ok: true, state: 'downloading', already_running: false, model: null });
    await startModelDownload({ tier: '1.7B' });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.modelDownload);
    expect(API_ENDPOINTS.setup.modelDownload).toMatch(/\/api\/setup\/model\/download$/);
    expect(init.method).toBe('POST');
    // 只传档位，不传 source（模型仓库由线路派生）
    expect(JSON.parse(String(init.body))).toEqual({ tier: '1.7B' });

    await startModelDownload();
    expect(JSON.parse(String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body))).toEqual({});
  });

  it('fetchModelProgress → GET /api/setup/model/progress，且使用 10s 短超时', async () => {
    const fetchMock = stubJson({
      state: 'downloading',
      downloaded: 1,
      total: 2,
      percent: 50,
      file: 'b.gguf',
      error: null,
      model: null,
    });
    const data = await fetchModelProgress();
    expect(data.percent).toBe(50);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.modelProgress);
    expect(API_ENDPOINTS.setup.modelProgress).toMatch(/\/api\/setup\/model\/progress$/);
    expect(init.method ?? 'GET').toBe('GET');
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it('fetchModelProgress 超时 10s（而非默认 300s）：到期 abort 并抛中文超时错误', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const pending = fetchModelProgress();
    const assertion = expect(pending).rejects.toThrow(/后端请求超时（10 秒）/);
    await vi.advanceTimersByTimeAsync(0); // 冲刷微任务：请求发出、abort 定时器就位
    await vi.advanceTimersByTimeAsync(10_000); // 推进到 10s 超时
    await assertion;
  });

  it('cancelModelDownload → POST /api/setup/model/cancel', async () => {
    const fetchMock = stubJson({ ok: true, state: 'canceled' });
    const data = await cancelModelDownload();
    expect(data.state).toBe('canceled');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.setup.modelCancel);
    expect(API_ENDPOINTS.setup.modelCancel).toMatch(/\/api\/setup\/model\/cancel$/);
    expect(init.method).toBe('POST');
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * 追加（语音接线）：合成 / 识别请求函数的 URL / method / body 断言。
 * 既有用例与断言零修改，本段为纯追加。
 * ──────────────────────────────────────────────────────────────────────────── */

describe('语音接口：合成与识别请求（voice）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** stub fetch 返回 2xx JSON，返回 mock 以便断言调用参数 */
  function stubJson(payload: unknown): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('synthesizeSpeech → POST /api/voice/synthesize，body 携带 text 与可选 voice', async () => {
    const fetchMock = stubJson({ ok: true, audio_base64: 'UklGRg==', mime: 'audio/wav' });
    const result = await synthesizeSpeech('你好', 'cx-open');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.voice.synthesize);
    expect(API_ENDPOINTS.voice.synthesize).toMatch(/\/api\/voice\/synthesize$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ text: '你好', voice: 'cx-open' });
    expect(result.audio_base64).toBe('UklGRg==');

    // 不传音色时请求体不含 voice 键（由服务端使用默认音色）
    await synthesizeSpeech('你好');
    expect(JSON.parse(String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body))).toEqual({
      text: '你好',
    });
  });

  it('transcribeAudio → POST /api/voice/transcribe，body 携带 audio_base64 与 sample_rate', async () => {
    const fetchMock = stubJson({ ok: true, text: '你好呀' });
    const result = await transcribeAudio('AAA=', 16000);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_ENDPOINTS.voice.transcribe);
    expect(API_ENDPOINTS.voice.transcribe).toMatch(/\/api\/voice\/transcribe$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ audio_base64: 'AAA=', sample_rate: 16000 });
    expect(result.text).toBe('你好呀');
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * 追加（20261003_模块0_流式合成中断异常处理）：synthesizeSpeechStream 的
 * AbortError 消化语义——中断是预期控制流，本层静默结束；其余错误照常上抛。
 * ──────────────────────────────────────────────────────────────────────────── */

describe('synthesizeSpeechStream：AbortError 消化（20261003）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** NDJSON 流式 200 响应：start 中 enqueue 首帧后挂起，controller 暴露给测试用例按需 error/close */
  function stubStreamFetch(): ReadableStreamDefaultController<Uint8Array> {
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        streamController = c;
        c.enqueue(new TextEncoder().encode('{"seq":1,"text":"你"}\n'));
        // 不 close：第二个 read() 挂起，等待测试用例 error/close 驱动
      },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } }),
      ),
    );
    return streamController;
  }

  it('正常流：逐帧回调 onChunk、done 帧回调 onDone（回归防线）', async () => {
    const controller = stubStreamFetch();
    const onChunk = vi.fn();
    const onDone = vi.fn<(total: number) => void>();
    const pending = synthesizeSpeechStream('你好', onChunk, onDone);

    await vi.waitFor(() => expect(onChunk).toHaveBeenCalledTimes(1));
    controller.enqueue(new TextEncoder().encode('{"done":true,"total":3}\n'));
    await expect(pending).resolves.toBeUndefined();
    expect(onDone).toHaveBeenCalledWith(3);
  });

  it('fetch 阶段中断：静默返回，不抛错、不回调 onDone', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new DOMException('The operation was aborted.', 'AbortError'));
            });
          }),
      ),
    );
    const controller = new AbortController();
    const onChunk = vi.fn();
    const onDone = vi.fn<(total: number) => void>();

    const pending = synthesizeSpeechStream('你好', onChunk, onDone, controller.signal);
    // fetch 已被调用（监听器就位）后触发中断：fetch 以 AbortError 拒绝
    await vi.waitFor(() => expect(onChunk).not.toHaveBeenCalled());
    controller.abort();

    await expect(pending).resolves.toBeUndefined();
    expect(onChunk).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it('流式读取阶段 read() 抛 AbortError：静默返回，不抛错、不回调 onDone', async () => {
    const streamController = stubStreamFetch();
    const controller = new AbortController();
    const onChunk = vi.fn();
    const onDone = vi.fn<(total: number) => void>();

    const pending = synthesizeSpeechStream('你好', onChunk, onDone, controller.signal);
    await vi.waitFor(() => expect(onChunk).toHaveBeenCalledTimes(1));

    // 模拟 signal 中断后 read() 以 AbortError 拒绝（浏览器真实行为）
    controller.abort();
    streamController.error(new DOMException('The operation was aborted.', 'AbortError'));

    await expect(pending).resolves.toBeUndefined();
    expect(onDone).not.toHaveBeenCalled();
  });

  it('流式读取阶段非中断错误：照常上抛（不被静默吞掉）', async () => {
    const streamController = stubStreamFetch();
    const onChunk = vi.fn();

    const pending = synthesizeSpeechStream('你好', onChunk);
    await vi.waitFor(() => expect(onChunk).toHaveBeenCalledTimes(1));

    streamController.error(new Error('boom'));
    await expect(pending).rejects.toThrow('boom');
  });
});
