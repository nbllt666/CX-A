import React, { useEffect, useRef, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import {
  IS_BACKEND_READY,
  fetchComputerStatus,
  fetchSettings,
  setComputerAuthorized,
  updateSettings,
} from '../api';

/**
 * 设置页（/settings）：
 * 云端提供商选择 / 本地模式开关 / 电脑控制授权开关 / 音色选择。
 *
 * - 云端提供商 / 本地模式 / 音色：首帧从后端 GET /api/settings 读取，切换走
 *   PUT /api/settings 热更新（失败不阻断界面，待后端上线后自动同步）。
 * - 电脑控制授权：已接入真实后端（GET /api/computer/status + POST /api/computer/authorize）。
 * - 本页默认值与后端 config 默认值一致：deepseek / 本地模式关 / cx-open。
 */

/** localStorage 键（cx-a.* 家族）：电脑控制授权开关 */
const LS_AUTH_KEY = 'cx-a.computer.authorized';
/** localStorage 键（cx-a.* 家族）：高危二次确认开关（仅展示，后端常态化开启） */
const LS_CONFIRM_KEY = 'cx-a.computer.confirm_dangerous';
/** 旧版键名（cx.* 家族）：仅用于读取回退迁移，写入一律走新键（见 readLsBool） */
const LEGACY_LS_AUTH_KEY = 'cx.computer.authorized';
const LEGACY_LS_CONFIRM_KEY = 'cx.computer.confirm_dangerous';

/** 云端 provider 白名单（与后端 /api/settings 一致） */
const CLOUD_PROVIDERS = ['deepseek', 'tongyi', 'openai', 'moonshot'];
/** 音色选项（默认 cx-open，其余为演示可选） */
const VOICE_OPTIONS = ['cx-open', 'ling', 'gulu', 'momo'];
/** 后端不可用时的回退默认值（与 config_manager DEFAULTS 一致） */
const FALLBACK_PROVIDER = 'deepseek';
const FALLBACK_LOCAL_MODE = false;
const FALLBACK_VOICE = 'cx-open';

/**
 * 读取 localStorage 布尔值；新键缺失时回落旧版键并顺手写入新键（静默迁移，
 * 防既有用户状态丢失）；均缺失 / 异常时回落默认值。
 */
function readLsBool(key: string, fallback: boolean, legacyKey?: string): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw !== null) return raw === '1';
    if (legacyKey !== undefined) {
      const legacy = localStorage.getItem(legacyKey);
      if (legacy !== null) {
        const value = legacy === '1';
        writeLsBool(key, value);
        return value;
      }
    }
    return fallback;
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
  const [provider, setProvider] = useState(FALLBACK_PROVIDER);
  const [localMode, setLocalMode] = useState(FALLBACK_LOCAL_MODE);
  const [voice, setVoice] = useState(FALLBACK_VOICE);
  // 后端返回的 provider 若不在白名单则附加为自定义项
  const [extraProvider, setExtraProvider] = useState<string | null>(null);

  // 电脑控制授权状态
  const [controlAuth, setControlAuth] = useState(false);
  // 高危二次确认开关（后端 / 本地 mock 的展示值）
  const [confirmDangerous, setConfirmDangerous] = useState(true);
  // 是否走真实后端（false 时降级为本地 mock）
  const [computerOnline, setComputerOnline] = useState(IS_BACKEND_READY);
  // 首帧配置读取失败：区块级降级提示条（默认值可能与后端不一致）
  const [settingsDegraded, setSettingsDegraded] = useState(false);
  // 配置项保存失败的轻量内联提示（哪一项失败显示哪一句）
  const [saveError, setSaveError] = useState<string | null>(null);

  // 挂载初始化：拉后端配置视图 + 电脑控制状态；失败回退默认值（与 config 默认一致）
  useEffect(() => {
    let alive = true;
    (async () => {
      if (IS_BACKEND_READY) {
        try {
          const st = await fetchSettings();
          if (!alive) return;
          const p = st?.cloud?.provider;
          if (p && !CLOUD_PROVIDERS.includes(p)) setExtraProvider(p);
          setProvider((p && CLOUD_PROVIDERS.includes(p) ? p : (st?.cloud?.provider ?? FALLBACK_PROVIDER)) as string);
          setLocalMode(Boolean(st?.local_llm?.enabled ?? FALLBACK_LOCAL_MODE));
          setVoice(st?.tts?.voice || FALLBACK_VOICE);
        } catch {
          if (!alive) return;
          setProvider(FALLBACK_PROVIDER);
          setLocalMode(FALLBACK_LOCAL_MODE);
          setVoice(FALLBACK_VOICE);
          // 首帧加载失败：展示区块级降级提示（本次渲染使用的是默认值）
          setSettingsDegraded(true);
        }
      }
      // 电脑控制授权状态
      if (!IS_BACKEND_READY) {
        setComputerOnline(false);
        setControlAuth(readLsBool(LS_AUTH_KEY, false, LEGACY_LS_AUTH_KEY));
        setConfirmDangerous(readLsBool(LS_CONFIRM_KEY, true, LEGACY_LS_CONFIRM_KEY));
        return;
      }
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
        setControlAuth(readLsBool(LS_AUTH_KEY, false, LEGACY_LS_AUTH_KEY));
        setConfirmDangerous(readLsBool(LS_CONFIRM_KEY, true, LEGACY_LS_CONFIRM_KEY));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 云端 / 本地模式 / 音色走 PUT /api/settings 热更新。
  // 保存失败不再静默吞掉：setSaveError 内联显错；下次操作开头自动清空，自然覆盖重试。
  // 序号守卫：每次操作分配自增序号，异步迟到失败比对序号非最新则丢弃——
  // 防止快速连点时旧操作的 .catch 在新操作清错之后执行，把 saveError 写回与后端真相背离的旧提示。
  const saveSeqRef = useRef(0);
  const handleProviderChange = (next: string) => {
    setProvider(next);
    if (CLOUD_PROVIDERS.includes(next)) {
      const seq = ++saveSeqRef.current;
      setSaveError(null);
      void updateSettings({ cloud: { provider: next } }).catch(() => {
        if (seq !== saveSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
        setSaveError('云端提供商没保存上…待会儿再动一下就好啦');
      });
    }
  };
  const handleLocalModeChange = (next: boolean) => {
    setLocalMode(next);
    const seq = ++saveSeqRef.current;
    setSaveError(null);
    void updateSettings({ local_llm: { enabled: next } }).catch(() => {
      if (seq !== saveSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
      setSaveError('本地模式开关没保存上…待会儿再拨一次就好啦');
    });
  };
  const handleVoiceChange = (next: string) => {
    setVoice(next);
    const seq = ++saveSeqRef.current;
    setSaveError(null);
    void updateSettings({ tts: { voice: next } }).catch(() => {
      if (seq !== saveSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
      setSaveError('音色设置没保存上…待会儿再选一次就好啦');
    });
  };

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

      {/* 首帧加载失败降级提示条：本次展示的是默认值，可能与后端不一致 */}
      {settingsDegraded && (
        <div className="mb-3 rounded-xl border border-[var(--glass-border)] bg-[rgba(255,183,225,0.10)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          设置加载失败啦～下面先用默认值顶着，可能与你的后端配置不太一样，连上后会自动同步的
        </div>
      )}

      {/* 配置项保存失败的轻量内联提示 */}
      {saveError && (
        <p className="-mt-1 mb-2 text-xs font-medium text-[var(--color-error)]">{saveError}</p>
      )}

      <div className="flex max-w-2xl flex-col gap-4">
        {/* 云端提供商 */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <label className="text-sm font-medium">云端提供商</label>
            <p className="text-xs text-[var(--text-tertiary)]">选一个你信任的云端服务来跑智能大脑</p>
            <select
              value={provider}
              onChange={(e) => handleProviderChange(e.target.value)}
              className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
            >
              <option value="deepseek">DeepSeek</option>
              <option value="tongyi">通义（Tongyi）</option>
              <option value="openai">OpenAI</option>
              <option value="moonshot">Moonshot（月之暗面）</option>
              {extraProvider && <option value={extraProvider}>{extraProvider}（当前值）</option>}
            </select>
          </div>
        </GlassCard>

        {/* 本地模式 */}
        <SettingRow title="本地模式" desc="不上传任何内容，所有对话都留在你电脑上（离线优先，默认关闭）">
          <Toggle checked={localMode} onChange={handleLocalModeChange} label="本地模式" />
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
                : 'bg-[rgba(255,130,130,0.16)] text-[var(--color-error)]',
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
              onChange={(e) => handleVoiceChange(e.target.value)}
              className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
            >
              <option value="cx-open">CX-OPEN（默认）</option>
              <option value="ling">灵灵（温柔）</option>
              <option value="gulu">咕噜（元气）</option>
              <option value="momo">默默（低沉）</option>
            </select>
          </div>
        </GlassCard>

        <p className="text-xs text-[var(--text-tertiary)]">
          云端 / 本地模式 / 音色已接入后端配置（GET/PUT /api/settings），后端不可用时回退默认值；电脑控制授权已接入真实后端。
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