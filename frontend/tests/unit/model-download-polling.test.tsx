import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/renderer/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/renderer/api')>(),
  fetchSetupRecommend: vi.fn(),
  fetchModelProgress: vi.fn(),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
}));

import * as api from '../../src/renderer/api';
import SetupWizard from '../../src/renderer/pages/SetupWizard';
import ModelTierCard from '../../src/renderer/components/settings/ModelTierCard';

const progressMock = vi.mocked(api.fetchModelProgress);
const DOWNLOADING: api.ModelProgress = {
  state: 'downloading', downloaded: 42, total: 100, percent: 42,
  file: 'test.gguf', error: null, model: null,
};
const DONE: api.ModelProgress = { ...DOWNLOADING, state: 'done', downloaded: 100, percent: 100 };
const RECOMMEND: api.SetupRecommendResult = {
  profile: {
    cpu_cores: 8, ram_gb: 16, gpu_vendor: null, vram_gb: null,
    cuda_version: null, disk_free_gb: 100, probe_notes: [],
  },
  recommendation: {
    use_local: true, device: 'cpu', tier: '1.7B', config_patch: {},
    model: null, reasons: [], probe_notes: [],
  },
  tiers: [{
    tier: '1.7B', repo: 'test/model', filename: 'test.gguf',
    approximate_size_gb: 1, ram_requirement_gb: 8, vram_requirement_gb: 0,
    description: 'Test model',
  }],
  suggested_source: 'modelscope',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function flush() {
  await act(async () => {});
}

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe.each(['wizard', 'settings'] as const)('%s download polling', (surface) => {
  const startName = surface === 'wizard'
    ? '\u73b0\u5728\u4e0b\u8f7d'
    : '\u4e0b\u8f7d\u6240\u9009\u6863\u4f4d';
  const cancelName = '\u53d6\u6d88\u4e0b\u8f7d';
  const retryName = '\u91cd\u65b0\u5f00\u59cb\u4e0b\u8f7d';

  async function mountAndStart() {
    const view = surface === 'wizard'
      ? render(<SetupWizard onDone={vi.fn()} initialStatus={{
        completed: false, wizard_required: true, download: { channel: 'mirror' },
        local_llm_source: 'modelscope',
      }} />)
      : render(<ModelTierCard />);
    await flush();
    if (surface === 'wizard') {
      fireEvent.click(screen.getByRole('button', { name: '\u5c31\u7528\u63a8\u8350\u7684' }));
      fireEvent.click(screen.getByRole('button', { name: '\u4e0b\u4e00\u6b65' }));
    }
    fireEvent.click(screen.getByRole('button', { name: startName }));
    await flush();
    return view;
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.resetAllMocks();
    vi.mocked(api.fetchSetupRecommend).mockResolvedValue(RECOMMEND);
    vi.mocked(api.startModelDownload).mockResolvedValue({
      ok: true, state: 'downloading', already_running: false, model: null,
    });
    vi.mocked(api.cancelModelDownload).mockResolvedValue({ ok: true, state: 'canceled' });
    progressMock.mockResolvedValue(DOWNLOADING);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('does not overlap slow progress requests', async () => {
    const first = deferred<api.ModelProgress>();
    progressMock.mockReturnValueOnce(first.promise);
    await mountAndStart();

    await tick(5000);
    expect(progressMock).toHaveBeenCalledTimes(1);
    await act(async () => { first.resolve(DOWNLOADING); });
    await tick(1000);
    expect(progressMock).toHaveBeenCalledTimes(2);
  });

  it('aborts a pending query on cancellation and ignores its late response', async () => {
    const stale = deferred<api.ModelProgress>();
    progressMock.mockResolvedValueOnce(DOWNLOADING).mockReturnValueOnce(stale.promise);
    await mountAndStart();
    await tick(1000);
    const signal = progressMock.mock.calls[1][0]!;

    fireEvent.click(screen.getByRole('button', { name: cancelName }));
    await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => { stale.resolve(DONE); });
    expect(screen.getByText(/\u5df2\u7ecf\u505c\u4e0b\u5566/)).toBeInTheDocument();
    expect(screen.queryByText(/\u4e0b\u8f7d\u597d\u5566|\u4e0b\u8f7d\u5b8c\u6210/)).not.toBeInTheDocument();
    await tick(5000);
    expect(progressMock).toHaveBeenCalledTimes(2);
  });

  it('does not let an old response release a restarted polling lock', async () => {
    const stale = deferred<api.ModelProgress>();
    const fresh = deferred<api.ModelProgress>();
    progressMock.mockResolvedValueOnce(DOWNLOADING)
      .mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise);
    await mountAndStart();
    await tick(1000);
    fireEvent.click(screen.getByRole('button', { name: cancelName }));
    await flush();
    fireEvent.click(screen.getByRole('button', { name: retryName }));
    await flush();

    await act(async () => { stale.resolve(DONE); });
    await tick(5000);
    expect(progressMock).toHaveBeenCalledTimes(3);
    await act(async () => { fresh.resolve(DOWNLOADING); });
    await tick(1000);
    expect(progressMock).toHaveBeenCalledTimes(4);
  });

  it('aborts an in-flight query and stops polling on unmount', async () => {
    const pending = deferred<api.ModelProgress>();
    progressMock.mockReturnValueOnce(pending.promise);
    const { unmount } = await mountAndStart();
    const signal = progressMock.mock.calls[0][0]!;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { pending.resolve(DOWNLOADING); });
    await tick(5000);
    expect(progressMock).toHaveBeenCalledTimes(1);
  });

  it('retries after a failed query and stops on terminal progress', async () => {
    const failed = deferred<api.ModelProgress>();
    progressMock.mockReturnValueOnce(failed.promise).mockResolvedValueOnce(DONE);
    await mountAndStart();
    await act(async () => { failed.reject(new Error('offline')); });
    await tick(1000);
    expect(progressMock).toHaveBeenCalledTimes(2);
    await tick(5000);
    expect(progressMock).toHaveBeenCalledTimes(2);
  });
});
