import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ListChecks,
  Plus,
  RefreshCw,
  Send,
  Server,
  Trash2,
} from 'lucide-react';
import {
  addFleetInstance,
  deleteFleetInstance,
  fetchFleetHealth,
  fetchFleetManifest,
  FleetApiError,
  listFleetInstances,
  sendFleetControl,
  type FleetInstance,
} from '../api';

/**
 * 管理面（CX-A 管理 CX-O 实例群）——隐藏页面。
 *
 * 入口：连点侧栏 logo 5 次（3 秒窗口）或深链 `#/fleet`；界面上没有任何可见入口，
 * 普通用户不会到达本页（spec add-fleet-frontend-hidden）。
 *
 * 能力：实例台账（脱敏）、登记/补录/注销、能力清单查看、健康探测、单实例控制指令下发。
 * 口径：
 *  - 任何降级场景（后端不可达 / 空台账 / 请求失败）一律中文提示卡，**禁止 Mock 假台账**；
 *  - CX-O / 后端错误原样呈现（状态码 + ADMIN_* 错误码）并附中文语义说明；
 *  - 治理 target/action 依 CX-O 匹配矩阵级联（§6.3 + §6.5），非法组合不出现；
 *  - params 非法 JSON 在前端拦截，绝不发出请求。
 */

/** CX-O 控制匹配矩阵（§6.3 + §6.5）：target → 合法 action 集合 */
const CONTROL_MATRIX: Record<string, string[]> = {
  autonomy: ['enable', 'disable', 'pause', 'resume'],
  voice: ['enable', 'disable', 'pause', 'resume'],
  live: ['enable', 'disable', 'pause', 'resume'],
  config: ['reload', 'reload_config', 'reset'],
  agent: ['create', 'update', 'delete', 'restart'],
  tuner: ['start', 'stop'],
  instance: ['restart', 'shutdown'],
  cluster: ['topology', 'state', 'sync_status', 'trigger_failover', 'set_role', 'add_peer', 'remove_peer'],
};

/** target 中文名（界面口语化） */
const TARGET_LABELS: Record<string, string> = {
  autonomy: '自主性',
  voice: '语音',
  live: '直播',
  config: '配置',
  agent: '智能体',
  tuner: '调参',
  instance: '实例进程',
  cluster: '哨兵集群',
};

/** action 中文名（界面口语化） */
const ACTION_LABELS: Record<string, string> = {
  enable: '启用',
  disable: '停用',
  pause: '暂停',
  resume: '恢复',
  reload: '重载',
  reload_config: '重载配置',
  reset: '重置',
  create: '创建',
  update: '更新',
  delete: '删除',
  restart: '重启',
  shutdown: '关停',
  start: '启动',
  stop: '停止',
  topology: '查拓扑',
  state: '查状态',
  sync_status: '查同步',
  trigger_failover: '触发故障转移',
  set_role: '设置角色',
  add_peer: '加入节点',
  remove_peer: '移除节点',
};

/** 错误码 → 中文语义说明（原样呈现错误码之外的人话） */
const ERROR_HINTS: Record<string, string> = {
  ADMIN_AUTH_FAILED: '实例令牌无效，请重新登记或补录令牌',
  ADMIN_FORBIDDEN: '令牌权限级别不足（只读令牌不能下发控制指令）',
  ADMIN_REPLAYED: '指令编号重复（防重放生效），稍后重试',
  ADMIN_RATE_LIMITED: '触发实例限流，稍等再试',
  ADMIN_DISABLED: '该实例没有开启管理面',
  ADMIN_UNKNOWN_ACTION: '未知指令组合（target 与 action 不匹配）',
  ADMIN_SERVICE_ERROR: '实例执行指令失败',
  fleet_unreachable: '实例不可达（连接失败或超时）',
  not_found: '台账里没有这个实例',
  bad_request: '请求参数不合法',
};

function errorHint(body: { error?: string; message?: string } | null): string {
  if (!body) return '';
  // 后端 400/404 的 message 已是具体中文（如「base_url 已登记」），优先呈现
  if ((body.error === 'bad_request' || body.error === 'not_found') && body.message) {
    return body.message;
  }
  if (body.error && ERROR_HINTS[body.error]) return ERROR_HINTS[body.error];
  return body.message ?? '';
}

function sourceLabel(source: string): string {
  return source === 'registered' ? '自动注册' : '手动登记';
}

const CARD =
  'rounded-2xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] backdrop-blur-[var(--glass-blur)]';
const PRIMARY_BTN =
  'inline-flex items-center justify-center gap-1.5 rounded-full bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-4 py-1.5 text-sm font-medium text-[var(--color-primary-foreground)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50';
const GHOST_BTN =
  'inline-flex items-center justify-center gap-1.5 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50';
const DANGER_BTN =
  'inline-flex items-center justify-center gap-1.5 rounded-full border border-[rgba(255,99,132,0.5)] px-4 py-1.5 text-sm text-[var(--color-error)] transition hover:bg-[rgba(255,99,132,0.12)] disabled:cursor-not-allowed disabled:opacity-50';
const INPUT =
  'h-9 w-full rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]';

export default function FleetPage() {
  const [instances, setInstances] = useState<FleetInstance[]>([]);
  const [loadState, setLoadState] = useState<'loading' | 'ok' | 'error'>('loading');
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [detailKind, setDetailKind] = useState<'manifest' | 'health' | null>(null);

  const [formName, setFormName] = useState('');
  const [formUrl, setFormUrl] = useState('');
  const [formToken, setFormToken] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [formMessage, setFormMessage] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);

  const [controlTarget, setControlTarget] = useState<string>('config');
  const [controlAction, setControlAction] = useState<string>('reload');
  const [controlParams, setControlParams] = useState('');
  const [controlBusy, setControlBusy] = useState(false);
  const [controlResult, setControlResult] = useState<string | null>(null);
  const [controlError, setControlError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoadState('loading');
    setLoadError(null);
    try {
      const data = await listFleetInstances();
      setInstances(data.instances ?? []);
      setSelectedId((prev) => prev ?? data.instances?.[0]?.id ?? null);
      setLoadState('ok');
    } catch (err) {
      setLoadState('error');
      setLoadError(err instanceof Error ? err.message : '实例台账没能加载出来');
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const selected = useMemo(
    () => instances.find((inst) => inst.id === selectedId) ?? null,
    [instances, selectedId],
  );

  // 治理面板：action 依 target 级联，切换 target 时校正 action
  const actions = CONTROL_MATRIX[controlTarget] ?? [];
  const effectiveAction = actions.includes(controlAction) ? controlAction : (actions[0] ?? '');

  const handleAdd = async () => {
    setFormBusy(true);
    setFormMessage(null);
    setFormError(null);
    try {
      const data = await addFleetInstance({
        name: formName.trim(),
        base_url: formUrl.trim(),
        token: formToken.trim(),
      });
      const source = data.instance?.source;
      setFormMessage(source === 'registered' ? '已补录令牌，这个实例可以管理了' : '已保存');
      setFormName('');
      setFormUrl('');
      setFormToken('');
      await reload();
    } catch (err) {
      if (err instanceof FleetApiError) {
        const hint = errorHint(err.body);
        setFormError(hint || `登记失败（${err.status}）`);
      } else {
        setFormError(err instanceof Error ? err.message : '登记失败');
      }
    } finally {
      setFormBusy(false);
    }
  };

  const handleRemove = async (id: string) => {
    setRemoveBusy(true);
    try {
      await deleteFleetInstance(id);
      setConfirmRemoveId(null);
      if (selectedId === id) setSelectedId(null);
      await reload();
    } catch {
      // 注销失败保留确认态，允许再试
    } finally {
      setRemoveBusy(false);
    }
  };

  const showManifest = async () => {
    if (!selected) return;
    setDetailKind('manifest');
    setDetail('加载中…');
    try {
      const data = await fetchFleetManifest(selected.id);
      setDetail(JSON.stringify(data, null, 2));
    } catch (err) {
      setDetail(formatError(err));
    }
  };

  const showHealth = async () => {
    if (!selected) return;
    setDetailKind('health');
    setDetail('加载中…');
    try {
      const data = await fetchFleetHealth(selected.id);
      setDetail(JSON.stringify(data, null, 2));
    } catch (err) {
      setDetail(formatError(err));
    }
  };

  const sendControl = async () => {
    if (!selected) return;
    // params 非法 JSON：前端拦截，绝不发出请求
    let params: Record<string, unknown> | undefined;
    if (controlParams.trim()) {
      try {
        const parsed: unknown = JSON.parse(controlParams);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setControlError('params 需要是一个 JSON 对象（花括号包起来的键值对）');
          return;
        }
        params = parsed as Record<string, unknown>;
      } catch {
        setControlError('params 不是合法 JSON，请检查后再下发');
        return;
      }
    }
    setControlBusy(true);
    setControlError(null);
    setControlResult(null);
    try {
      const data = await sendFleetControl(selected.id, {
        target: controlTarget,
        action: effectiveAction,
        ...(params ? { params } : {}),
      });
      setControlResult(JSON.stringify(data, null, 2));
    } catch (err) {
      setControlError(formatError(err));
    } finally {
      setControlBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-6">
      {/* 页头 */}
      <div className="flex flex-col gap-1">
        <h1 className="flex items-center gap-2 text-xl font-bold text-gradient">
          <Server size={20} aria-hidden />
          管理面
        </h1>
        <p className="text-sm text-[var(--text-secondary)]">
          CX-A 在这里打理 CX-O 实例群：登记、体检、下发指令。隐藏页面，日常使用不需要它。
        </p>
      </div>

      {/* 台账 */}
      <div className={`${CARD} p-4`}>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">实例台账</h2>
          <button type="button" className={GHOST_BTN} onClick={() => void reload()}>
            <RefreshCw size={14} aria-hidden />
            刷新
          </button>
        </div>

        {loadState === 'loading' && (
          <p className="text-sm text-[var(--text-secondary)]">正在读取实例台账…</p>
        )}
        {loadState === 'error' && (
          <div className="rounded-xl border border-[var(--glass-border)] p-3 text-sm text-[var(--text-secondary)]">
            <p>实例台账暂时读不出来：{loadError}</p>
            <p className="mt-1 text-xs text-[var(--text-tertiary)]">
              请确认 CX-A 的后台服务正在运行，然后点「刷新」再试。
            </p>
          </div>
        )}
        {loadState === 'ok' && instances.length === 0 && (
          <p className="text-sm text-[var(--text-secondary)]">
            台账还是空的——在下面登记第一台实例，或等 CX-O 实例自动报上门。
          </p>
        )}
        {loadState === 'ok' && instances.length > 0 && (
          <ul className="flex flex-col gap-2">
            {instances.map((inst) => (
              <li key={inst.id}>
                <button
                  type="button"
                  className={`w-full rounded-xl border px-3 py-2 text-left transition ${
                    selectedId === inst.id
                      ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
                      : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]'
                  }`}
                  onClick={() => {
                    setSelectedId(inst.id);
                    setDetail(null);
                    setDetailKind(null);
                    setControlResult(null);
                    setControlError(null);
                  }}
                >
                  <span className="flex items-center justify-between gap-2 text-sm font-medium">
                    <span>{inst.name}</span>
                    <span className="text-xs font-normal text-[var(--text-tertiary)]">
                      {sourceLabel(inst.source)}
                    </span>
                  </span>
                  <span className="mt-0.5 block text-xs text-[var(--text-tertiary)]">
                    {inst.base_url} · {inst.role || '角色未知'} · 最近在线 {inst.last_seen || '未知'} · 令牌{' '}
                    {inst.token_suffix ? `尾号 ${inst.token_suffix}` : '未补录'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 登记表单 */}
      <div className={`${CARD} p-4`}>
        <h2 className="mb-3 text-sm font-semibold">登记实例</h2>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <input
            className={INPUT}
            placeholder="名称（比如：家里的重实例）"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            aria-label="实例名称"
          />
          <input
            className={INPUT}
            placeholder="地址（http://192.168.1.10:8000）"
            value={formUrl}
            onChange={(e) => setFormUrl(e.target.value)}
            aria-label="实例地址"
          />
          <input
            className={INPUT}
            placeholder="令牌（实例管理面登记的那个）"
            value={formToken}
            onChange={(e) => setFormToken(e.target.value)}
            aria-label="实例令牌"
          />
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" className={PRIMARY_BTN} disabled={formBusy} onClick={() => void handleAdd()}>
            <Plus size={14} aria-hidden />
            {formBusy ? '正在保存…' : '登记 / 补录'}
          </button>
          {formMessage && <p className="text-xs text-[var(--color-success)]">{formMessage}</p>}
          {formError && <p className="text-xs font-medium text-[var(--color-error)]">{formError}</p>}
        </div>
        <p className="mt-1 text-xs text-[var(--text-tertiary)]">
          同一台机器上的实例会自动报上门；不在同一台机器的，用这里手动登记。已自动注册但缺令牌的实例，填同地址再提交就是补录。
        </p>
      </div>

      {/* 选中实例的操作区 */}
      {selected && (
        <>
          <div className={`${CARD} p-4`}>
            <h2 className="mb-3 text-sm font-semibold">
              {selected.name} 的体检与能力
            </h2>
            <div className="flex flex-wrap gap-2">
              <button type="button" className={GHOST_BTN} onClick={() => void showHealth()}>
                <Activity size={14} aria-hidden />
                健康探测
              </button>
              <button type="button" className={GHOST_BTN} onClick={() => void showManifest()}>
                <ListChecks size={14} aria-hidden />
                查看能力清单
              </button>
              {confirmRemoveId === selected.id ? (
                <button
                  type="button"
                  className={DANGER_BTN}
                  disabled={removeBusy}
                  onClick={() => void handleRemove(selected.id)}
                >
                  <Trash2 size={14} aria-hidden />
                  {removeBusy ? '正在移除…' : '再点一次确认移除'}
                </button>
              ) : (
                <button type="button" className={DANGER_BTN} onClick={() => setConfirmRemoveId(selected.id)}>
                  <Trash2 size={14} aria-hidden />
                  移除记录
                </button>
              )}
            </div>
            {detail && (
              <pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[var(--bg-tertiary)] p-3 text-xs leading-relaxed text-[var(--text-secondary)]">
                {detail}
              </pre>
            )}
          </div>

          {/* 治理面板 */}
          <div className={`${CARD} p-4`}>
            <h2 className="mb-3 text-sm font-semibold">
              给 {selected.name} 下发指令
            </h2>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-xs text-[var(--text-tertiary)]">
                对什么下手
                <select
                  className={INPUT}
                  value={controlTarget}
                  onChange={(e) => setControlTarget(e.target.value)}
                  aria-label="指令目标"
                >
                  {Object.keys(CONTROL_MATRIX).map((t) => (
                    <option key={t} value={t}>
                      {t}（{TARGET_LABELS[t]}）
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-[var(--text-tertiary)]">
                做什么
                <select
                  className={INPUT}
                  value={effectiveAction}
                  onChange={(e) => setControlAction(e.target.value)}
                  aria-label="指令动作"
                >
                  {actions.map((a) => (
                    <option key={a} value={a}>
                      {a}（{ACTION_LABELS[a]}）
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <textarea
              className="mt-2 w-full rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] p-2 font-mono text-xs outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
              rows={3}
              placeholder='参数（可选，JSON 对象，比如 {"agent_id": "default"}）'
              value={controlParams}
              onChange={(e) => setControlParams(e.target.value)}
              aria-label="指令参数"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                className={PRIMARY_BTN}
                disabled={controlBusy}
                onClick={() => void sendControl()}
              >
                <Send size={14} aria-hidden />
                {controlBusy ? '正在下发…' : '下发指令'}
              </button>
              {controlError && (
                <p className="text-xs font-medium text-[var(--color-error)]">{controlError}</p>
              )}
            </div>
            {controlResult && (
              <pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[var(--bg-tertiary)] p-3 text-xs leading-relaxed text-[var(--text-secondary)]">
                {controlResult}
              </pre>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** fleet 错误格式化：状态码 + 错误码原样呈现，并附中文语义说明 */
function formatError(err: unknown): string {
  if (err instanceof FleetApiError) {
    const code = err.body?.error ? ` ${err.body.error}` : '';
    const hint = errorHint(err.body);
    return `失败（${err.status}${code}）${hint ? `：${hint}` : ''}`;
  }
  return err instanceof Error ? err.message : '操作失败';
}
