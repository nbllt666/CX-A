import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatMessageStream, synthesizeSpeechStream } from '../../src/renderer/api';

function streamFetch(text = '', close = false, cancel?: () => Promise<void>) {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let reader!: ReadableStreamDefaultReader<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
      if (text) value.enqueue(new TextEncoder().encode(text));
      if (close) value.close();
    },
    cancel,
  });
  const getReader = body.getReader.bind(body);
  vi.spyOn(body, 'getReader').mockImplementation(() => {
    reader = getReader();
    vi.spyOn(reader, 'cancel');
    vi.spyOn(reader, 'releaseLock');
    return reader;
  });
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body)));
  return { body, controller, get reader() { return reader; } };
}

function expectCleanup(stream: ReturnType<typeof streamFetch>) {
  expect(stream.reader.cancel).toHaveBeenCalledTimes(1);
  expect(stream.reader.releaseLock).toHaveBeenCalledTimes(1);
  expect(vi.mocked(stream.reader.cancel).mock.invocationCallOrder[0])
    .toBeLessThan(vi.mocked(stream.reader.releaseLock).mock.invocationCallOrder[0]);
  expect(stream.body.locked).toBe(false);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('chatMessageStream terminal metadata', () => {
  it('returns on done without waiting for EOF or emitting frames after done', async () => {
    const stream = streamFetch(
      '{"delta":"hello"}\n{"done":true,"ok":true,"clean_text":"hello","mood":"happy"}\n{"delta":"ignored"}\n',
    );
    const onDelta = vi.fn();
    await expect(chatMessageStream('test', onDelta)).resolves.toMatchObject({
      ok: true, incomplete: false, clean_text: 'hello', mood: 'happy',
    });
    expect(onDelta).toHaveBeenCalledExactlyOnceWith('hello');
    expectCleanup(stream);
  });

  it('retains partial clean text and failure metadata from incomplete done', async () => {
    const stream = streamFetch(
      '{"delta":"partial"}\n{"done":true,"ok":false,"incomplete":true,"clean_text":"partial","mood":"calm","error":"provider_failed"}\n',
    );
    const onDelta = vi.fn();
    await expect(chatMessageStream('test', onDelta)).resolves.toMatchObject({
      ok: false, incomplete: true, clean_text: 'partial', mood: 'calm', error: 'provider_failed',
    });
    expect(onDelta).toHaveBeenCalledWith('partial');
    expectCleanup(stream);
  });

  it('cannot turn an earlier error frame into a successful done', async () => {
    const stream = streamFetch('{"error":"provider_failed"}\n{"done":true,"ok":true,"clean_text":"partial"}\n');
    await expect(chatMessageStream('test', vi.fn())).resolves.toMatchObject({
      ok: false, incomplete: true, error: 'provider_failed', clean_text: 'partial',
    });
    expectCleanup(stream);
  });

  it('marks EOF without done as incomplete while retaining delivered deltas', async () => {
    const stream = streamFetch('{"delta":"partial"}\n', true);
    const onDelta = vi.fn();
    const result = await chatMessageStream('test', onDelta);
    expect(result).toMatchObject({ ok: false, incomplete: true });
    expect(result.error).toBeTruthy();
    expect(result.clean_text).toBeUndefined();
    expect(onDelta).toHaveBeenCalledWith('partial');
    expectCleanup(stream);
  });

  it('retains error metadata when the stream closes without done', async () => {
    const stream = streamFetch('{"error":"provider_failed"}', true);
    await expect(chatMessageStream('test', vi.fn())).resolves.toMatchObject({
      ok: false, incomplete: true, error: 'provider_failed',
    });
    expectCleanup(stream);
  });

  it('parses terminal frames without a trailing newline', async () => {
    const stream = streamFetch('{"done":true,"ok":true,"clean_text":"done"}', true);
    await expect(chatMessageStream('test', vi.fn())).resolves.toMatchObject({ ok: true, clean_text: 'done' });
    expectCleanup(stream);
  });
});

describe.each(['chat', 'speech'] as const)('%s stream cleanup', (kind) => {
  const firstFrame = kind === 'chat' ? '{"delta":"first"}\n' : '{"seq":1,"text":"first"}\n';
  const doneFrame = kind === 'chat' ? '{"done":true,"ok":true}\n' : '{"done":true,"total":1}\n';
  const start = (onFrame: (frame: unknown) => void, signal?: AbortSignal, onDone: (total: number) => void = vi.fn()) =>
    kind === 'chat'
      ? chatMessageStream('test', onFrame, signal)
      : synthesizeSpeechStream('test', onFrame, onDone, signal);

  it('cancels and releases an open stream on done', async () => {
    const stream = streamFetch(firstFrame + doneFrame);
    const onDone = vi.fn();
    await start(vi.fn(), undefined, onDone);
    if (kind === 'speech') expect(onDone).toHaveBeenCalledWith(1);
    expectCleanup(stream);
  });

  it('wakes a stalled read when fetch itself does not react to external abort', async () => {
    const stream = streamFetch(firstFrame);
    const controller = new AbortController();
    const onFrame = vi.fn();
    const onDone = vi.fn();
    const removeListener = vi.spyOn(controller.signal, 'removeEventListener');
    const pending = start(onFrame, controller.signal, onDone);
    const assertion = kind === 'chat'
      ? expect(pending).rejects.toMatchObject({ name: 'AbortError' })
      : expect(pending).resolves.toBeUndefined();
    await vi.waitFor(() => expect(onFrame).toHaveBeenCalledTimes(1));

    controller.abort();
    await assertion;
    expect(onDone).not.toHaveBeenCalled();
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expectCleanup(stream);
  });

  it('cancels and releases the reader when a frame callback throws', async () => {
    const stream = streamFetch(firstFrame);
    const error = new Error('callback_failed');
    await expect(start(vi.fn(() => { throw error; }))).rejects.toBe(error);
    expectCleanup(stream);
  });

  it('does not block abort completion on asynchronous source cancellation', async () => {
    const stream = streamFetch(firstFrame, false, () => new Promise<void>(() => {}));
    const controller = new AbortController();
    const onFrame = vi.fn();
    const pending = start(onFrame, controller.signal);
    const assertion = kind === 'chat'
      ? expect(pending).rejects.toMatchObject({ name: 'AbortError' })
      : expect(pending).resolves.toBeUndefined();
    await vi.waitFor(() => expect(onFrame).toHaveBeenCalledTimes(1));
    controller.abort();
    await assertion;
    expectCleanup(stream);
  });

  it('preserves read failures while canceling and releasing the reader', async () => {
    const stream = streamFetch(firstFrame);
    const onFrame = vi.fn();
    const pending = start(onFrame);
    const error = new Error('stream_failed');
    const assertion = expect(pending).rejects.toBe(error);
    await vi.waitFor(() => expect(onFrame).toHaveBeenCalledTimes(1));
    stream.controller.error(error);
    await assertion;
    expectCleanup(stream);
  });

  it('does not fetch when already aborted', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    controller.abort();
    const pending = start(vi.fn(), controller.signal);
    if (kind === 'chat') {
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    } else {
      await expect(pending).resolves.toBeUndefined();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
