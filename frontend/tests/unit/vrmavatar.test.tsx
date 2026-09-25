import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/react';
import VrmAvatar, {
  REASON_BY_STAGE,
  vrmExpressionForMood,
} from '../../src/renderer/components/VrmAvatar';
import { fetchPetModelBuffer } from '../../src/renderer/api';

/**
 * Test1 · VrmAvatar 的失败提示与纯函数映射。
 *
 * jsdom 无 WebGL：无法创建 THREE.WebGLRenderer，正好用来验证「任何失败都给出中文
 * 『暂时显示不了 3D 桌宠』提示、且**不渲染任何替代形象**（早期那个 CSS 卡通形象
 * 组件已整体删除，不再是兜底路径）、绝不抛到 React 边界」。
 * 悬浮窗 / 桌宠页的真实 WebGL 渲染由 e2e/pet-vrm.e2e.spec.ts 在 Electron 内断言
 * data-vrm-state="ready"。
 */

vi.mock('../../src/renderer/api', () => ({
  fetchPetModelBuffer: vi.fn(),
}));

const mockedFetchPetModel = vi.mocked(fetchPetModelBuffer);

describe('VrmAvatar：失败时给中文提示（页面无任何替代形象）', () => {
  beforeEach(() => {
    // jsdom 未实现 canvas.getContext('webgl')，会经虚拟控制台报错并让 three 抛异常；
    // 这里静音日志，专注断言「不崩溃、有提示」。
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockedFetchPetModel.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('无 WebGL：渲染不抛错，落 data-vrm-state="unsupported" 并显示提示，且无替代形象', async () => {
    // 即便模型字节取得到，jsdom 仍无法创建 WebGLRenderer → 走不支持分支
    mockedFetchPetModel.mockResolvedValue(new ArrayBuffer(8));

    const { container, getByText } = render(<VrmAvatar mood="happy" talking={false} size={150} />);

    await waitFor(() => {
      expect(container.querySelector('[data-vrm-state="unsupported"]')).not.toBeNull();
    });
    // 明确的中文提示 + 可读原因（本用例失败在渲染器阶段 → 文案指向显卡 / WebGL）
    expect(getByText('暂时显示不了 3D 桌宠')).not.toBeNull();
    expect(container.textContent).toContain('WebGL');
    // 不留空 canvas 冒充「加载中」
    expect(container.querySelector('canvas')).toBeNull();
  });

  it('模型接口失败（fetchPetModelBuffer reject）：同样落 unsupported 且不崩溃', async () => {
    mockedFetchPetModel.mockRejectedValue(new Error('桌宠模型请求失败: 404 Not Found'));

    const { container, getByText } = render(<VrmAvatar mood="sad" talking size={120} />);

    await waitFor(() => {
      expect(container.querySelector('[data-vrm-state="unsupported"]')).not.toBeNull();
    });
    expect(getByText('暂时显示不了 3D 桌宠')).not.toBeNull();
    expect(container.querySelector('canvas')).toBeNull();
  });
});

describe('vrmExpressionForMood：心情 → VRM 预设表情名映射（纯函数）', () => {
  it('6 档心情各映射到预期 VRM 表情名', () => {
    expect(vrmExpressionForMood('happy')).toBe('happy');
    expect(vrmExpressionForMood('calm')).toBe('neutral');
    expect(vrmExpressionForMood('sad')).toBe('sad');
    expect(vrmExpressionForMood('surprised')).toBe('surprised');
    expect(vrmExpressionForMood('shy')).toBe('relaxed');
    expect(vrmExpressionForMood('sleepy')).toBe('relaxed');
  });
});

describe('REASON_BY_STAGE：三档失败原因文案（用户在失败态唯一能读到的信息）', () => {
  it('renderer / model / parse 三档均有可读中文且指向不同成因', () => {
    // 三档都要够长、是中文句子（不是错误码/堆栈），且能区分「环境 / 资源 / 文件」三类成因
    for (const text of Object.values(REASON_BY_STAGE)) {
      expect(text.trim().length).toBeGreaterThan(8);
      expect(text.endsWith('）')).toBe(true); // 统一以括号补充说明收尾
    }
    expect(REASON_BY_STAGE.renderer).toContain('WebGL');
    expect(REASON_BY_STAGE.model).toContain('没取到桌宠模型文件');
    expect(REASON_BY_STAGE.parse).toContain('解析失败');
    // 三档文案互不相同（否则用户无法据此区分失败位置）
    expect(new Set(Object.values(REASON_BY_STAGE)).size).toBe(3);
  });
});