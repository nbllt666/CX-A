import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import FleetPage from '../../src/renderer/pages/FleetPage';

/**
 * FleetPage（管理面前端）单元测试——mock api 模块，零真实网络。
 *
 * 覆盖：台账渲染与脱敏、空态、不可达降级卡与刷新恢复、实例选择、
 * 登记（手动/补录语义/400 中文错误）、注销两段确认、健康探测、能力清单、
 * 治理下发成功与 401 原样呈现、params 非法 JSON 前端拦截、target 级联。
 */

const apiMocks = vi.hoisted(() => {
  class FleetApiError extends Error {
    status: number;
    body: { error?: string; message?: string } | null;
    constructor(status: number, body: { error?: string; message?: string } | null, message: string) {
      super(message);
      this.status = status;
      this.body = body;
    }
  }
  return {
    listFleetInstances: vi.fn(),
    addFleetInstance: vi.fn(),
    deleteFleetInstance: vi.fn(),
    fetchFleetManifest: vi.fn(),
    fetchFleetHealth: vi.fn(),
    sendFleetControl: vi.fn(),
    FleetApiError,
  };
});

vi.mock('../../src/renderer/api', () => apiMocks);

function makeInstance(overrides: Record<string, unknown> = {}) {
  return {
    id: 'inst-1',
    name: '家里的重实例',
    base_url: 'http://192.168.1.10:8000',
    role: 'active',
    source: 'manual',
    registered_at: '2026-10-04T12:00:00',
    last_seen: '2026-10-04T14:00:00',
    has_token: true,
    token_suffix: '1234',
    ...overrides,
  };
}

function selectOption(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  // 计数与实现全量重置：调用次数断言按「本用例」口径统计
  for (const key of [
    'listFleetInstances',
    'addFleetInstance',
    'deleteFleetInstance',
    'fetchFleetManifest',
    'fetchFleetHealth',
    'sendFleetControl',
  ] as const) {
    apiMocks[key].mockReset();
  }
  apiMocks.listFleetInstances.mockResolvedValue({ status: 'success', instances: [] });
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('FleetPage：台账展示', () => {
  it('渲染实例字段且不出现令牌明文', async () => {
    apiMocks.listFleetInstances.mockResolvedValue({
      status: 'success',
      instances: [makeInstance({ token: 'tok-super-secret' })],
    });
    render(<FleetPage />);
    // 首个实例自动选中（列表 + 详情标题两处出现，用 findAllByText）
    const nameHits = await screen.findAllByText('家里的重实例');
    expect(nameHits.length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/手动登记/).length).toBeGreaterThanOrEqual(1); // 来源徽章（表单说明里也有该词）
    expect(screen.getByText(/尾号 1234/)).toBeInTheDocument();
    expect(screen.queryByText(/tok-super-secret/)).not.toBeInTheDocument();
  });

  it('空台账呈现引导文案（禁 Mock 假数据）', async () => {
    apiMocks.listFleetInstances.mockResolvedValue({ status: 'success', instances: [] });
    render(<FleetPage />);
    expect(await screen.findByText(/台账还是空的/)).toBeInTheDocument();
  });

  it('后端不可达呈现中文降级卡，刷新可恢复', async () => {
    apiMocks.listFleetInstances
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValueOnce({
        status: 'success',
        instances: [makeInstance()],
      });
    render(<FleetPage />);
    expect(await screen.findByText(/实例台账暂时读不出来/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /刷新/ }));
    expect(await screen.findByText('家里的重实例')).toBeInTheDocument();
  });
});

describe('FleetPage：选择与登记', () => {
  it('选中实例：默认选中首个，点击第二行切换操作目标', async () => {
    apiMocks.listFleetInstances.mockResolvedValue({
      status: 'success',
      instances: [makeInstance(), makeInstance({ id: 'inst-2', name: '爸爸的机器' })],
    });
    render(<FleetPage />);
    // 首个实例自动选中（操作区立即可用）
    expect(await screen.findByText(/的体检与能力/)).toBeInTheDocument();
    // 点击第二行 → 操作目标切换
    fireEvent.click(screen.getByRole('button', { name: /爸爸的机器/ }));
    expect(screen.getByText(/爸爸的机器 的体检与能力/)).toBeInTheDocument();
    expect(screen.getByText(/给 爸爸的机器 下发指令/)).toBeInTheDocument();
  });

  it('登记成功（手动）：展示「已保存」并刷新列表', async () => {
    apiMocks.addFleetInstance.mockResolvedValue({
      status: 'success',
      instance: makeInstance({ id: 'new-1', name: '新实例', source: 'manual' }),
    });
    render(<FleetPage />);
    fireEvent.change(await screen.findByLabelText('实例名称'), { target: { value: '新实例' } });
    fireEvent.change(screen.getByLabelText('实例地址'), { target: { value: 'http://10.0.0.5:8000' } });
    fireEvent.change(screen.getByLabelText('实例令牌'), { target: { value: 'tok-9999' } });
    fireEvent.click(screen.getByRole('button', { name: /登记 \/ 补录/ }));
    await waitFor(() => expect(screen.getByText('已保存')).toBeInTheDocument());
    expect(apiMocks.addFleetInstance).toHaveBeenCalledWith({
      name: '新实例',
      base_url: 'http://10.0.0.5:8000',
      token: 'tok-9999',
    });
    expect(apiMocks.listFleetInstances).toHaveBeenCalledTimes(2); // 初始 + 刷新
  });

  it('补录语义：同地址提交后 source 仍为 registered → 提示「已补录令牌」', async () => {
    apiMocks.addFleetInstance.mockResolvedValue({
      status: 'success',
      instance: makeInstance({ source: 'registered' }),
    });
    render(<FleetPage />);
    fireEvent.change(await screen.findByLabelText('实例名称'), { target: { value: 'smoke' } });
    fireEvent.change(screen.getByLabelText('实例地址'), { target: { value: 'http://127.0.0.1:18601' } });
    fireEvent.change(screen.getByLabelText('实例令牌'), { target: { value: 'tok-0001' } });
    fireEvent.click(screen.getByRole('button', { name: /登记 \/ 补录/ }));
    expect(await screen.findByText('已补录令牌，这个实例可以管理了')).toBeInTheDocument();
  });

  it('登记 400 错误原样呈现中文信息', async () => {
    apiMocks.addFleetInstance.mockRejectedValue(
      new apiMocks.FleetApiError(400, { error: 'bad_request', message: 'base_url 已登记' }, '管理面请求失败: 400'),
    );
    render(<FleetPage />);
    fireEvent.change(await screen.findByLabelText('实例名称'), { target: { value: 'x' } });
    fireEvent.change(screen.getByLabelText('实例地址'), { target: { value: 'http://a:8000' } });
    fireEvent.change(screen.getByLabelText('实例令牌'), { target: { value: 't' } });
    fireEvent.click(screen.getByRole('button', { name: /登记 \/ 补录/ }));
    expect(await screen.findByText('base_url 已登记')).toBeInTheDocument();
  });
});

describe('FleetPage：注销 / 体检 / 能力清单', () => {
  async function selectFirstInstance() {
    apiMocks.listFleetInstances.mockResolvedValue({
      status: 'success',
      instances: [makeInstance()],
    });
    render(<FleetPage />);
    fireEvent.click(await screen.findByText('家里的重实例'));
  }

  it('注销需两段确认：第一次只变确认按钮', async () => {
    await selectFirstInstance();
    fireEvent.click(screen.getByRole('button', { name: /移除记录/ }));
    expect(apiMocks.deleteFleetInstance).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /再点一次确认移除/ })).toBeInTheDocument();
  });

  it('确认后调用注销并刷新列表', async () => {
    apiMocks.deleteFleetInstance.mockResolvedValue({ status: 'success' });
    await selectFirstInstance();
    fireEvent.click(screen.getByRole('button', { name: /移除记录/ }));
    fireEvent.click(screen.getByRole('button', { name: /再点一次确认移除/ }));
    await waitFor(() => expect(apiMocks.deleteFleetInstance).toHaveBeenCalledWith('inst-1'));
    expect(apiMocks.listFleetInstances).toHaveBeenCalledTimes(2);
  });

  it('健康探测：结果 JSON 只读展示', async () => {
    apiMocks.fetchFleetHealth.mockResolvedValue({ status: 'healthy', components: { memory: 'healthy' } });
    await selectFirstInstance();
    fireEvent.click(screen.getByRole('button', { name: /健康探测/ }));
    expect(await screen.findByText(/"healthy"/)).toBeInTheDocument();
    expect(apiMocks.fetchFleetHealth).toHaveBeenCalledWith('inst-1');
  });

  it('健康探测不可达：504 fleet_unreachable 原样呈现 + 中文语义', async () => {
    apiMocks.fetchFleetHealth.mockRejectedValue(
      new apiMocks.FleetApiError(504, { error: 'fleet_unreachable' }, '管理面请求失败: 504'),
    );
    await selectFirstInstance();
    fireEvent.click(screen.getByRole('button', { name: /健康探测/ }));
    expect(await screen.findByText(/失败（504 fleet_unreachable）/)).toBeInTheDocument();
    expect(screen.getByText(/实例不可达/)).toBeInTheDocument();
  });

  it('能力清单：manifest 只读展示', async () => {
    apiMocks.fetchFleetManifest.mockResolvedValue({ instance_id: 'fake', capabilities: { autonomy: true } });
    await selectFirstInstance();
    fireEvent.click(screen.getByRole('button', { name: /查看能力清单/ }));
    expect(await screen.findByText(/"autonomy": true/)).toBeInTheDocument();
  });
});

describe('FleetPage：治理面板', () => {
  async function openControlPanel() {
    apiMocks.listFleetInstances.mockResolvedValue({
      status: 'success',
      instances: [makeInstance()],
    });
    render(<FleetPage />);
    fireEvent.click(await screen.findByText('家里的重实例'));
  }

  it('下发成功：请求体正确且结果回显', async () => {
    apiMocks.sendFleetControl.mockResolvedValue({ status: 'success', result: { echo: true } });
    await openControlPanel();
    fireEvent.click(screen.getByRole('button', { name: /下发指令/ }));
    await waitFor(() =>
      expect(apiMocks.sendFleetControl).toHaveBeenCalledWith('inst-1', { target: 'config', action: 'reload' }),
    );
    expect(await screen.findByText(/"echo": true/)).toBeInTheDocument();
  });

  it('401 ADMIN_AUTH_FAILED 原样呈现状态码 + 错误码 + 中文说明', async () => {
    apiMocks.sendFleetControl.mockRejectedValue(
      new apiMocks.FleetApiError(401, { error: 'ADMIN_AUTH_FAILED' }, '管理面请求失败: 401'),
    );
    await openControlPanel();
    fireEvent.click(screen.getByRole('button', { name: /下发指令/ }));
    expect(await screen.findByText(/失败（401 ADMIN_AUTH_FAILED）/)).toBeInTheDocument();
    expect(screen.getByText(/实例令牌无效/)).toBeInTheDocument();
  });

  it('params 非法 JSON：前端拦截，不发请求', async () => {
    await openControlPanel();
    fireEvent.change(screen.getByLabelText('指令参数'), { target: { value: '不是json' } });
    fireEvent.click(screen.getByRole('button', { name: /下发指令/ }));
    expect(await screen.findByText(/不是合法 JSON/)).toBeInTheDocument();
    expect(apiMocks.sendFleetControl).not.toHaveBeenCalled();
  });

  it('params 合法 JSON：随请求体下发', async () => {
    apiMocks.sendFleetControl.mockResolvedValue({ status: 'success', result: {} });
    await openControlPanel();
    fireEvent.change(screen.getByLabelText('指令参数'), {
      target: { value: '{"agent_id": "default"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: /下发指令/ }));
    await waitFor(() =>
      expect(apiMocks.sendFleetControl).toHaveBeenCalledWith('inst-1', {
        target: 'config',
        action: 'reload',
        params: { agent_id: 'default' },
      }),
    );
  });

  it('target 切换级联 action：cluster 目标给出集群专属动作集', async () => {
    apiMocks.sendFleetControl.mockResolvedValue({ status: 'success', result: {} });
    await openControlPanel();
    selectOption('指令目标', 'cluster');
    // 级联后 action 校正为 cluster 集合首项 topology；下发体必须匹配矩阵
    fireEvent.click(screen.getByRole('button', { name: /下发指令/ }));
    await waitFor(() =>
      expect(apiMocks.sendFleetControl).toHaveBeenCalledWith('inst-1', { target: 'cluster', action: 'topology' }),
    );
  });
});
