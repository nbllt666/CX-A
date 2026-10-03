import React, { useEffect, useRef, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import { useRouterOptional } from '../App';
import {
  IS_BACKEND_READY,
  fetchComputerStatus,
  fetchSettings,
  fetchVoices,
  hasVoiceFolderPicker,
  importVoice,
  pickVoiceFolder,
  setComputerAuthorized,
  updateSettings,
} from '../api';
import type { AccelMode, VoiceInfo } from '../api';

/**
 * 设置页（/settings）：
 * 云端提供商选择 / 运行偏好 / 本地模式（含就绪徽标）/ 主动视觉 / 电脑控制授权 /
 * 音色选择（选项来自后端音色包列表，支持导入自定义音色包）。
 *
 * - 云端提供商 / 本地模式 / 音色 / 主动视觉：首帧从后端 GET /api/settings 读取，切换走
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
/** 音色选项（后端列表不可用时的回退演示项；真实列表以 GET /api/voices 为准） */
const VOICE_OPTIONS = ['cx-open', 'ling', 'gulu', 'momo'];
/** 回退演示音色的展示名（与 VOICE_OPTIONS 一一对应；后端列表可用时按 builtin/size 生成） */
const VOICE_FALLBACK_LABELS: Record<string, string> = {
  'cx-open': 'CX-OPEN（默认）',
  ling: '灵灵（温柔）',
  gulu: '咕噜（元气）',
  momo: '默默（低沉）',
};
/** 后端不可用时的回退默认值（与 config_manager DEFAULTS 一致） */
const FALLBACK_PROVIDER = 'deepseek';
const FALLBACK_LOCAL_MODE = false;
const FALLBACK_VOICE = 'cx-open';
/** 运行偏好回退默认（与 config_manager DEFAULTS accel.mode 一致） */
const FALLBACK_ACCEL_MODE: AccelMode = 'performance';

/** 运行偏好选项（口语化，零术语） */
const ACCEL_MODE_OPTIONS: Array<{ value: AccelMode; label: string; desc: string }> = [
  { value: 'eco', label: '省电优先', desc: '日常更省电更安静，够用就好' },
  { value: 'performance', label: '性能优先', desc: '需要时火力全开，反应更快' },
];

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

/** 音色包字节数的人性化展示（如「约 120MB」）；无有效体积时返回空串 */
function formatVoiceSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '';
  const mb = size / (1024 * 1024);
  if (mb >= 1024) return `约 ${(mb / 1024).toFixed(1)}GB`;
  if (mb >= 1) return `约 ${Math.round(mb)}MB`;
  const kb = size / 1024;
  return kb >= 1 ? `约 ${Math.round(kb)}KB` : '';
}

/** 音色下拉项展示名：内置 cx-open 固定「CX-OPEN（默认）」，自定义展示 id + 人性化大小 */
function voiceOptionLabel(v: VoiceInfo): string {
  if (v.builtin) return 'CX-OPEN（默认）';
  const sizeText = formatVoiceSize(v.size);
  return sizeText ? `${v.id}（${sizeText}）` : v.id;
}

export default function SettingsPage() {
  // 非抛错版路由读取：应用内可取到；组件级单测裸渲染时为 null（按钮静默不可跳转，不抛错）
  const router = useRouterOptional();
  const [provider, setProvider] = useState(FALLBACK_PROVIDER);
  const [localMode, setLocalMode] = useState(FALLBACK_LOCAL_MODE);
  const [voice, setVoice] = useState(FALLBACK_VOICE);
  // 运行偏好（省电优先 / 性能优先）：保存后由后端重建语音桥生效
  const [accelMode, setAccelMode] = useState<AccelMode>(FALLBACK_ACCEL_MODE);
  // 需要重启应用才完全生效时的非阻断提示（不静默）
  const [accelHint, setAccelHint] = useState<string | null>(null);
  // 后端返回的 provider / voice 若不在白名单则附加为自定义项（select 显示空白修复，D8）
  const [extraProvider, setExtraProvider] = useState<string | null>(null);
  const [extraVoice, setExtraVoice] = useState<string | null>(null);
  // 音色包列表（GET /api/voices）：null = 后端列表不可用，回退 VOICE_OPTIONS 演示项
  const [voices, setVoices] = useState<VoiceInfo[] | null>(null);
  // 是否正在导入音色包（按钮禁用防连点）
  const [importing, setImporting] = useState(false);
  // 本地小模型就绪状态（GET settings local_llm.ready，驱动本地模式卡片就绪徽标）
  const [localReady, setLocalReady] = useState(false);
  // 主动视觉开关（GET settings vision.enabled）
  const [visionEnabled, setVisionEnabled] = useState(false);
  // 音色导入成功的轻量提示（样式与运行偏好提示 accelHint 同款）
  const [importHint, setImportHint] = useState<string | null>(null);

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
  // 自定义 provider / voice 选中时的非阻断提示（不支持在线修改，显式告知而非静默跳过，D8）
  const [customHint, setCustomHint] = useState<string | null>(null);

  // 挂载初始化：拉后端配置视图 + 音色包列表 + 电脑控制状态；失败回退默认值（与 config 默认一致）
  useEffect(() => {
    let alive = true;
    (async () => {
      if (IS_BACKEND_READY) {
        // 音色列表与配置视图并行拉取，互不阻塞；各自失败独立降级
        const voicesTask = fetchVoices().catch(() => null);
        // 首帧读到的当前音色（供列表装载后判定 extraVoice 附加项）
        let currentVoice = FALLBACK_VOICE;
        try {
          const st = await fetchSettings();
          if (!alive) return;
          const p = st?.cloud?.provider;
          if (p && !CLOUD_PROVIDERS.includes(p)) setExtraProvider(p);
          setProvider((p && CLOUD_PROVIDERS.includes(p) ? p : (st?.cloud?.provider ?? FALLBACK_PROVIDER)) as string);
          setLocalMode(Boolean(st?.local_llm?.enabled ?? FALLBACK_LOCAL_MODE));
          setLocalReady(Boolean(st?.local_llm?.ready ?? false));
          setVisionEnabled(Boolean(st?.vision?.enabled ?? false));
          const v = st?.tts?.voice || FALLBACK_VOICE;
          setVoice(v);
          currentVoice = v;
          // 运行偏好：以后端值为准，非法/缺失回落默认
          const m = st?.accel?.mode;
          setAccelMode(m === 'eco' || m === 'performance' ? m : FALLBACK_ACCEL_MODE);
        } catch {
          if (!alive) return;
          setProvider(FALLBACK_PROVIDER);
          setLocalMode(FALLBACK_LOCAL_MODE);
          setVoice(FALLBACK_VOICE);
          setAccelMode(FALLBACK_ACCEL_MODE);
          // 首帧加载失败：展示区块级降级提示（本次渲染使用的是默认值）
          setSettingsDegraded(true);
        }
        // 音色列表装载：成功 → 下拉以后端列表为准；失败 → 回退演示选项（VOICE_OPTIONS）。
        // 当前音色不在列表时沿用 extraVoice 附加项逻辑，避免 select 显示空白（D8）。
        const list = await voicesTask;
        if (!alive) return;
        setVoices(list);
        const knownVoice = list
          ? list.some((item) => item.id === currentVoice)
          : VOICE_OPTIONS.includes(currentVoice);
        setExtraVoice(currentVoice && !knownVoice ? currentVoice : null);
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
  // 序号守卫（D1 修复）：配置保存与授权切换使用两个独立计数器分桶——此前共用单一
  // saveSeqRef 时，任一在途请求都会让另一类操作的迟到回调被判「非最新」而静默丢弃，
  // 授权失败分支（降级离线记忆）被跳过 → UI 与后端授权真相背离且无提示。
  const settingsSeqRef = useRef(0);
  const authSeqRef = useRef(0);
  const handleProviderChange = (next: string) => {
    setProvider(next);
    if (CLOUD_PROVIDERS.includes(next)) {
      const seq = ++settingsSeqRef.current;
      setSaveError(null);
      setCustomHint(null);
      void updateSettings({ cloud: { provider: next } }).catch(() => {
        if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
        setSaveError('云端提供商没保存上…待会儿再动一下就好啦');
      });
    } else {
      // 选中白名单外的自定义 provider（后端既有值）：不支持在线保存，显式提示而非静默跳过（D8）
      setSaveError(null);
      setCustomHint('当前为自定义提供商，暂不支持在线修改；如需切换请选择列表中的提供商');
    }
  };
  const handleLocalModeChange = (next: boolean) => {
    setLocalMode(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setCustomHint(null);
    void updateSettings({ local_llm: { enabled: next } }).catch(() => {
      if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
      setSaveError('本地模式开关没保存上…待会儿再拨一次就好啦');
    });
  };
  // 主动视觉开关：PUT /api/settings {vision:{enabled}}（白名单新键），失败内联显错不静默
  const handleVisionChange = (next: boolean) => {
    setVisionEnabled(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setCustomHint(null);
    void updateSettings({ vision: { enabled: next } }).catch(() => {
      if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
      setSaveError('主动视觉开关没保存上…待会儿再拨一次就好啦');
    });
  };
  const handleVoiceChange = (next: string) => {
    setVoice(next);
    // 合法值域：后端列表可用时以列表为准；列表不可用（降级）时回退演示选项白名单
    const known = voices ? voices.some((v) => v.id === next) : VOICE_OPTIONS.includes(next);
    if (known) {
      const seq = ++settingsSeqRef.current;
      setSaveError(null);
      setCustomHint(null);
      void updateSettings({ tts: { voice: next } }).catch(() => {
        if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
        setSaveError('音色设置没保存上…待会儿再选一次就好啦');
      });
    } else {
      // 选中后端自定义音色：不支持在线保存，显式提示而非静默跳过（D8）
      setSaveError(null);
      setCustomHint('当前音色不在列表里，暂不支持在线修改；如需切换请选择列表中的音色');
    }
  };

  /**
   * 导入音色包：先经 Electron 桥选文件夹（浏览器 dev / 测试环境无此能力，显式提示），
   * 拿到路径后 POST /api/voices/import；成功刷新列表、轻量提示、自动选中新音色并
   * PUT tts.voice；失败把后端中文 message（或通用文案）内联展示，不静默。
   * 导入中禁用按钮防连点；Electron 内用户取消选夹时静默返回。
   */
  const handleImportVoice = async () => {
    if (importing) return;
    setSaveError(null);
    setCustomHint(null);
    setImportHint(null);
    const hasPicker = hasVoiceFolderPicker();
    let folder: string | null = null;
    try {
      folder = await pickVoiceFolder();
    } catch {
      folder = null;
    }
    if (!folder) {
      // 桥缺失 = 非 Electron 环境；桥在但返回空 = 用户主动取消，不打扰
      if (!hasPicker) setCustomHint('导入音色要在桌面应用里才能用哦');
      return;
    }
    setImporting(true);
    try {
      const imported = await importVoice(folder);
      // 刷新列表；刷新失败不阻断换新：至少把新音色以附加项兜底展示（D8 同款）
      try {
        const list = await fetchVoices();
        setVoices(list);
        setExtraVoice(null);
      } catch {
        setExtraVoice(imported.id);
      }
      setVoice(imported.id);
      setImportHint('音色导入成功，已经帮你换上啦');
      const seq = ++settingsSeqRef.current;
      void updateSettings({ tts: { voice: imported.id } }).catch(() => {
        if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
        setSaveError('音色设置没保存上…待会儿再选一次就好啦');
      });
    } catch (err) {
      setSaveError(
        err instanceof Error && err.message ? err.message : '导入音色失败…待会儿再试一次就好啦',
      );
    } finally {
      setImporting(false);
    }
  };

  /**
   * 切换运行偏好（省电优先 / 性能优先）：PUT /api/settings {accel:{mode}}。
   * 后端保存后按新配置重建语音桥；若需重启才完全生效，以后端 message 明确提示（不静默）。
   */
  const handleAccelModeChange = (next: AccelMode) => {
    setAccelMode(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setCustomHint(null);
    setAccelHint(null);
    void updateSettings({ accel: { mode: next } })
      .then((res) => {
        if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到响应
        const wb = res.voice_backend;
        if (wb && wb.needs_restart) {
          setAccelHint(wb.message || '已切换，重启应用后完全生效');
        } else {
          setAccelHint('已切换，新的运行偏好马上生效');
        }
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('运行偏好没保存上…待会儿再试一次就好啦');
      });
  };

  // 切换授权：在线走 POST authorize；离线/失败则本地记忆。
  // F-8（第三轮体检批次6）：补序号守卫（F3 修复未覆盖此处）——快速连点时
  // 并发 POST 响应可乱序，迟到的旧响应不得把 UI 拉回与后端真相背离的状态。
  // D1（第四轮体检批次D）：改用独立 authSeqRef 计数桶，与配置保存互不干扰。
  const handleControlAuthChange = async (next: boolean) => {
    setControlAuth(next);
    const seq = ++authSeqRef.current;
    if (computerOnline) {
      try {
        const st = await setComputerAuthorized(next);
        if (seq !== authSeqRef.current) return; // 已有更新的操作接管，丢弃迟到响应
        setControlAuth(st.authorized);
        setConfirmDangerous(st.confirm_dangerous);
        writeLsBool(LS_AUTH_KEY, st.authorized);
        writeLsBool(LS_CONFIRM_KEY, st.confirm_dangerous);
      } catch {
        if (seq !== authSeqRef.current) return; // 同上：迟到失败丢弃
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
          设置加载失败啦～下面先用默认值顶着，连上后会自动同步的
        </div>
      )}

      {/* 配置项保存失败的轻量内联提示 */}
      {saveError && (
        <p className="-mt-1 mb-2 text-xs font-medium text-[var(--color-error)]">{saveError}</p>
      )}

      {/* 自定义 provider / voice 选中时的非阻断提示（不支持在线修改） */}
      {customHint && (
        <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{customHint}</p>
      )}

      {/* 运行偏好切换结果提示（需重启时明确告知，不静默） */}
      {accelHint && (
        <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{accelHint}</p>
      )}

      {/* 音色导入成功提示（轻量，样式与运行偏好提示同款） */}
      {importHint && (
        <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{importHint}</p>
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

        {/* 运行偏好（省电优先 / 性能优先）：保存后按新配置重建语音桥生效 */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <label className="text-sm font-medium">运行偏好</label>
            <p className="text-xs text-[var(--text-tertiary)]">
              省电优先更省电更安静，性能优先反应更快；切换后马上生效
            </p>
            <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2">
              {ACCEL_MODE_OPTIONS.map((opt) => {
                const active = accelMode === opt.value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => handleAccelModeChange(opt.value)}
                    className={[
                      'flex flex-col gap-0.5 rounded-xl border px-3 py-2 text-left text-sm transition',
                      active
                        ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
                        : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]',
                    ].join(' ')}
                  >
                    <span>{opt.label}</span>
                    <span className="text-xs text-[var(--text-tertiary)]">{opt.desc}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </GlassCard>

        {/* 本地模式：开关 + 就绪徽标（SettingRow 装不下徽标，改用自定义 GlassCard） */}
        <GlassCard>
          <div className="flex items-center justify-between gap-4 p-4">
            <div>
              <p className="font-medium">本地模式</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                开启后聊天都在这台电脑上完成，不联网也能陪你说
              </p>
            </div>
            <Toggle checked={localMode} onChange={handleLocalModeChange} label="本地模式" />
          </div>
          {/* 状态徽标：仅本地模式开启时展示（ready 绿/蓝调；未就绪中性调，指引去新手引导） */}
          {localMode && (
            <div className="px-4 pb-4">
              <span
                className={[
                  'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
                  localReady
                    ? 'bg-[rgba(124,216,255,0.16)] text-[var(--color-primary)]'
                    : 'bg-[var(--bg-secondary)] text-[var(--text-secondary)]',
                ].join(' ')}
              >
                {localReady ? '本地大脑已就绪' : '本地大脑还没准备好——新手引导里可以下载'}
              </span>
            </div>
          )}
        </GlassCard>

        {/* 主动视觉 */}
        <SettingRow
          title="主动视觉"
          desc="开启后它会看看屏幕、记住你正在忙什么，越陪你越懂你；画面不会离开这台电脑，理解画面时需要联网"
        >
          <Toggle checked={visionEnabled} onChange={handleVisionChange} label="主动视觉" />
        </SettingRow>

        {/* 电脑控制授权 */}
        <SettingRow
          title="电脑控制授权"
          desc={
            computerOnline
              ? '允许它帮你点点鼠标、敲敲键盘、跑跑指令？权限很敏感，谨慎开关'
              : '还没连上 TA，先在本地记一下你的选择（稍后自动同步）'
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

        {/* 音色选择：选项来自 GET /api/voices（失败回退演示项），支持导入自定义音色包 */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <label className="text-sm font-medium">音色</label>
            <p className="text-xs text-[var(--text-tertiary)]">挑一个舒服的声音陪你说话</p>
            <select
              value={voice}
              onChange={(e) => handleVoiceChange(e.target.value)}
              className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
            >
              {voices
                ? voices.map((v) => (
                    <option key={v.id} value={v.id}>
                      {voiceOptionLabel(v)}
                    </option>
                  ))
                : VOICE_OPTIONS.map((id) => (
                    <option key={id} value={id}>
                      {VOICE_FALLBACK_LABELS[id] ?? id}
                    </option>
                  ))}
              {extraVoice && <option value={extraVoice}>{extraVoice}（当前值）</option>}
            </select>
            <div className="mt-1">
              <button
                type="button"
                onClick={() => {
                  void handleImportVoice();
                }}
                disabled={importing}
                className="shrink-0 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {importing ? '导入中…' : '导入音色包'}
              </button>
            </div>
          </div>
        </GlassCard>

        {/* 新手引导入口：重新跑一遍向导（选云端大脑 / 下载线路 / 本地小模型，含下载） */}
        <GlassCard>
          <div className="flex items-center justify-between gap-4 p-4">
            <div>
              <p className="font-medium">新手引导</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                想重新挑云端大脑、换条下载线路，或者下载本地小模型？点这里再跑一遍
              </p>
            </div>
            <button
              type="button"
              onClick={() => router?.navigate('setup')}
              className="shrink-0 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)]"
            >
              重新跑一遍新手引导
            </button>
          </div>
        </GlassCard>
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