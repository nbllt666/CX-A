import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';

/**
 * 桌宠模型更换 / 恢复默认（Task 8）。
 *
 * 模块级部分 mock `../src/renderer/api`（总线 read/publish/on 保持真实实现，
 * 用于验证 localStorage 信号链）；VrmAvatar 在 jsdom 下无 WebGL，
 * fetchPetModelBuffer mock 为立即 resolve（组件落 unsupported 态，不进入重试循环）。
 *
 * 覆盖：
 *  1. 桌宠页渲染「更换桌宠模型 / 恢复默认」按钮（启用态）；
 *  2. 更换成功链路：选文件 → import → 成功中文提示 + localStorage 重载信号已发布；
 *  3. 更换失败：后端中文 message 原样展示；
 *  4. 非 Electron（桥缺失）：显式引导提示，不发导入请求；
 *  5. 恢复默认成功 / 失败（无备份）双路径；
 *  6. petModelReload 总线纯逻辑：publish 写键、onPetModelReload 仅对匹配键的 storage 事件回调。
 */
vi.mock('../../src/renderer/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/renderer/api')>();
  return {
    ...actual,
    fetchPetModelBuffer: vi.fn(),
    pickVrmFile: vi.fn(),
    hasVrmFilePicker: vi.fn(),
    importPetModel: vi.fn(),
    resetPetModel: vi.fn(),
  };
});

import PetPage from '../../src/renderer/pages/PetPage';
import * as api from '../../src/renderer/api';

const fetchPetModelBufferMock = vi.mocked(api.fetchPetModelBuffer);
const pickVrmFileMock = vi.mocked(api.pickVrmFile);
const hasVrmFilePickerMock = vi.mocked(api.hasVrmFilePicker);
const importPetModelMock = vi.mocked(api.importPetModel);
const resetPetModelMock = vi.mocked(api.resetPetModel);

describe('桌宠模型更换 / 恢复默认（PetPage）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    // jsdom 无 WebGL：模型字节 mock 为立即 resolve，组件落 unsupported 态（不重试）
    fetchPetModelBufferMock.mockResolvedValue(new ArrayBuffer(8));
    // 默认：具备选文件能力（桥在），具体返回值由各用例覆盖
    hasVrmFilePickerMock.mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('桌宠页（启用态）渲染「更换桌宠模型 / 恢复默认」按钮', async () => {
    render(<PetPage />);
    expect(await screen.findByRole('button', { name: '更换桌宠模型' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复默认' })).toBeInTheDocument();
  });

  it('更换成功：选文件 → 导入 → 成功提示 + localStorage 重载信号已发布', async () => {
    pickVrmFileMock.mockResolvedValue('C:\\models\\custom.vrm');
    importPetModelMock.mockResolvedValue({ ok: true, message: '模型导入成功' });

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '更换桌宠模型' }));

    expect(await screen.findByText('模型已更换，桌宠马上换上新模样')).toBeInTheDocument();
    // 导入请求携带所选文件绝对路径
    await waitFor(() => expect(importPetModelMock).toHaveBeenCalledWith('C:\\models\\custom.vrm'));
    // 重载总线信号已发布（其他窗口 / 后续挂载据此重载新模型）
    expect(window.localStorage.getItem('cx-a.petModelReload')).not.toBeNull();
    // 进行中态退出：按钮恢复可用
    expect(screen.getByRole('button', { name: '更换桌宠模型' })).not.toBeDisabled();
  });

  it('更换失败：后端中文 message 原样展示（不静默）', async () => {
    pickVrmFileMock.mockResolvedValue('C:\\models\\broken.vrm');
    importPetModelMock.mockRejectedValue(new Error('这个文件不是有效的 VRM 模型'));

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '更换桌宠模型' }));

    expect(await screen.findByText('这个文件不是有效的 VRM 模型')).toBeInTheDocument();
    // 失败不发布重载信号
    expect(window.localStorage.getItem('cx-a.petModelReload')).toBeNull();
  });

  it('非 Electron（桥缺失）：显式引导「要在桌面应用里才能用」，不发导入请求', async () => {
    hasVrmFilePickerMock.mockReturnValue(false);
    pickVrmFileMock.mockResolvedValue(null);

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '更换桌宠模型' }));

    expect(await screen.findByText('更换桌宠模型要在桌面应用里才能用哦')).toBeInTheDocument();
    expect(importPetModelMock).not.toHaveBeenCalled();
  });

  it('恢复默认成功：成功提示 + 重载信号发布', async () => {
    resetPetModelMock.mockResolvedValue({ ok: true });

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '恢复默认' }));

    expect(await screen.findByText('已恢复默认的桌宠模型')).toBeInTheDocument();
    expect(resetPetModelMock).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem('cx-a.petModelReload')).not.toBeNull();
  });

  it('恢复默认失败（无备份 404）：后端中文 message 展示', async () => {
    resetPetModelMock.mockRejectedValue(new Error('恢复默认模型失败：没有找到可还原的备份'));

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '恢复默认' }));

    expect(await screen.findByText('恢复默认模型失败：没有找到可还原的备份')).toBeInTheDocument();
    expect(window.localStorage.getItem('cx-a.petModelReload')).toBeNull();
  });

  it('进行中禁重复：导入期间两个按钮均禁用', async () => {
    let resolveImport: (v: { ok: boolean }) => void = () => undefined;
    pickVrmFileMock.mockResolvedValue('C:\\models\\custom.vrm');
    importPetModelMock.mockImplementation(
      () => new Promise<{ ok: boolean }>((resolve) => (resolveImport = resolve)),
    );

    render(<PetPage />);
    fireEvent.click(await screen.findByRole('button', { name: '更换桌宠模型' }));

    // 导入中：更换按钮进入 loading 态且禁用，恢复默认也禁用（防并发替换）
    expect(await screen.findByRole('button', { name: '导入中…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '恢复默认' })).toBeDisabled();

    await act(async () => {
      resolveImport({ ok: true });
    });
    expect(await screen.findByText('模型已更换，桌宠马上换上新模样')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '更换桌宠模型' })).not.toBeDisabled();
  });
});

describe('petModelReload 总线（纯逻辑）', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it('publish 写键；onPetModelReload 仅对匹配键的 storage 事件回调，去订阅后不再回调', () => {
    expect(api.readPetModelReloadTick()).toBe(0);

    const seen: number[] = [];
    const off = api.onPetModelReload(() => seen.push(api.readPetModelReloadTick()));

    api.publishPetModelReload();
    const tick = api.readPetModelReloadTick();
    expect(tick).toBeGreaterThan(0);

    // 模拟其他窗口的 storage 事件（同窗口写入不触发，与浏览器语义一致）
    const fire = (key: string, newValue: string | null) => {
      window.dispatchEvent(new StorageEvent('storage', { key, newValue }));
    };
    act(() => {
      fire('cx-a.petModelReload', String(tick));
      fire('cx-a.otherKey', String(tick)); // 非目标键：不回调
      fire('cx-a.petModelReload', null); // 无新值：不回调
    });
    expect(seen).toEqual([tick]);

    off();
    act(() => {
      fire('cx-a.petModelReload', String(tick + 1));
    });
    expect(seen).toEqual([tick]); // 去订阅后不再回调
  });

  it('readPetModelReloadTick：非法值 / 缺失回落 0', () => {
    window.localStorage.setItem('cx-a.petModelReload', 'not-a-number');
    expect(api.readPetModelReloadTick()).toBe(0);
    window.localStorage.setItem('cx-a.petModelReload', '-5');
    expect(api.readPetModelReloadTick()).toBe(0);
  });
});
