import React, { useEffect, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import { IS_BACKEND_READY, fetchComputerStatus, setComputerAuthorized } from '../api';

/**
 * 设置页（/settings）：
 * 云端提供商选择 / 本地模式开关 / 电脑控制授权开关 / 音色选择。
 *
 * 「电脑控制授权」区块已接入真实后端：
 * - 挂载时 GET /api/computer/status 初始化授权与高危确认状态；
 * - 切换授权走 POST /api/computer/authorize；
 * - 后端不可用时降级为本地 mock 交互（localStorage 记忆，界面提示离线）。
 */

/** localStorage 键：电脑控制授权开关 */
const LS_AUTH_KEY = 'cx.computer.authorized';
/** localStorage 键：高危二次确认开关（仅展示，后端常态化开启） */
const LS_CONFIRM_KEY = 'cx.computer.confirm_dangerous';

/** 读取 localStorage 布尔值；缺失 / 异常时回落默认值 */
function readLsBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return raw === '1';
  } catch {
    return fallback;
  }
}

/** 写入 localStorage 布尔值（'1'/'0'），异常静默 */
function writeLsBool(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    /* 存储满 / 被禁用时静默忽略 */
  }
}

export default function SettingsPage() {
  const [provider, setProvider] = useState('auto');
  const [localMode, setLocalMode] = useState(true);
  const [voice, setVoice] = useState('ling');

  // 电脑控制授权状态
  const [controlAuth, setControlAuth] = useState(false);
  // 高危二次确认开关（后端 / 本地 mock 的展示值）
  const [confirmDangerous, setConfirmDangerous] = useState(true);
  // 是否走真实后端（false 时降级为本地 mock）
  const [computerOnline, setComputerOnline] = useState(IS_BACKEND_READY);

  // 挂载初始化：优先拉后端 status，失败降级到本地 mock
  useEffect(() => {
    if (!IS_BACKEND_READY) {
      setComputerOnline(false);
      setControlAuth(readLsBool(LS_AUTH_KEY, false));
      setConfirmDangerous(readLsBool(LS_CONFIRM_KEY, true));
      return;
    }
    let alive = true;
    (async () => {
      try {
        const st = await fetchComputerStatus();
        if (!alive) return;
        setComputerOnline(true);
        setControlAuth(st.authorized);
        setConfirmDangerous(st.confirm_dangerous);
        writeLsBool(LS_AUTH_KEY, st.authorized);
        writeLsBool(LS_CONFIRM_KEY, st.confirm_dangerous);
      } catch {
        if (!alive) return;
        // 后端不可用：降级到本地记忆
        setComputerOnline(false);
        setControlAuth(readLsBool(LS_AUTH_KEY, false));
        setConfirmDangerous(readLsBool(LS_CONFIRM_KEY, true));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 切换授权：在线走 POST authorize；离线/失败则本地记忆
  const handleControlAuthChange = async (next: boolean) => {
    setControlAuth(next);
    if (computerOnline) {
      try {
        const st = await setComputerAuthorized(next);
        setControlAuth(st.authorized);
        setConfirmDangerous(st.confirm_dangerous);
        writeLsBool(LS_AUTH_KEY, st.authorized);
        writeLsBool(LS_CONFIRM_KEY, st.confirm_dangerous);
      } catch {
        // 后端请求失败：降到本地记忆交互
        setComputerOnline(false);
        writeLsBool(LS_AUTH_KEY, next);
      }
    } else {
      writeLsBool(LS_AUTH_KEY, next);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-y-auto p-5">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gradient">设置</h1>
        <p className="text-sm text-[var(--text-secondary)]">按照你的习惯，把它调成喜欢的样子</p>
      </div>

      <div className="flex max-w-2xl flex-col gap-4">
        {/* 云端提供商 */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <label className="text-sm font-medium">云端提供商</label>
            <p className="text-xs text-[var(--text-tertiary)]">选一个你信任的云端服务来跑智能大脑</p>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
            >
              <option value="auto">自动（推荐）</option>
              <option value="providerA">云端 A</option>
              <option value="providerB">云端 B</option>
              <option value="custom">自定义地址</option>
            </select>
          </div>
        </GlassCard>

        {/* 本地模式 */}
        <SettingRow title="本地模式" desc="不上传任何内容，所有对话都留在你电脑上（离线优先）">
          <Toggle checked={localMode} onChange={setLocalMode} label="本地模式" />
        </SettingRow>

        {/* 电脑控制授权 */}
        <SettingRow
          title="电脑控制授权"
          desc={
            computerOnline
              ? '允许它帮你点点鼠标、敲敲键盘、跑跑指令？权限很敏感，谨慎开关'
              : '后端还没连上，先在本地记一下你的选择（离线记忆）'
          }
        >
          <Toggle
            checked={controlAuth}
            onChange={(v) => {
              void handleControlAuthChange(v);
            }}
            label="电脑控制授权"
          />
        </SettingRow>

        {/* 高危二次确认（展示 confirm_dangerous） */}
        <SettingRow
          title="高危操作二次确认"
          desc={
            confirmDangerous
              ? '删除、重启这类危险指令会先问你一声，稳稳的'
              : '危险指令直接执行，不留中间确认（不推荐）'
          }
        >
          <span
            className={[
              'inline-flex shrink-0 items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
              confirmDangerous
                ? 'bg-[rgba(124,216,255,0.16)] text-[var(--color-primary)]'
                : 'bg-[rgba(255,130,130,0.16)] text-[var(--danger)]',
            ].join(' ')}
          >
            {confirmDangerous ? '已开启' : '已关闭'}
          </span>
        </SettingRow>

        {/* 音色选择 */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <label className="text-sm font-medium">音色</label>
            <p className="text-xs text-[var(--text-tertiary)]">挑一个舒服的声音陪你说话</p>
            <select
              value={voice}
              onChange={(e) => setVoice(e.target.value)}
              className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
            >
              <option value="ling">灵灵（温柔）</option>
              <option value="gulu">咕噜（元气）</option>
              <option value="momo">默默（低沉）</option>
            </select>
          </div>
        </GlassCard>

        <p className="text-xs text-[var(--text-tertiary)]">
          云端 / 本地模式 / 音色仍为演示占位；电脑控制授权已接入真实后端，后端不可用时会自动转为本地记忆。
        </p>
      </div>
    </div>
  );
}

function SettingRow({
  title,
  desc,
  children,
}: {
  title: string;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <GlassCard>
      <div className="flex items-center justify-between gap-4 p-4">
        <div>
          <p className="font-medium">{title}</p>
          <p className="text-xs text-[var(--text-tertiary)]">{desc}</p>
        </div>
        {children}
      </div>
    </GlassCard>
  );
}