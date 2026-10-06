import React, { useEffect, useRef, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import ModelTierCard from '../components/settings/ModelTierCard';
import PetModelControls from '../components/PetModelControls';
import { useRouterOptional } from '../App';
import {
  IS_BACKEND_READY,
  fetchComputerStatus,
  fetchSettings,
  fetchSetupRecommend,
  fetchVoices,
  hasVoiceFolderPicker,
  importVoice,
  pickVoiceFolder,
  setComputerAuthorized,
  updateSettings,
} from '../api';
import type {
  AccelMode,
  DownloadChannel,
  HardwareProfile,
  TtsAccel,
  TtsAccelDevice,
  VoiceInfo,
} from '../api';

/**
 * 设置页（/settings）：
 * 云端提供商与 API Key / 运行偏好 / 本地模式（含就绪徽标）/ 本地模型档位管理 /
 * 下载线路 / 主动视觉 / 电脑控制授权 / 音色选择（含导入自定义音色包）/
 * 桌宠模型更换 / 语音加速。
 *
 * - 云端提供商 / API Key / 下载线路 / 本地模式 / 音色 / 语音加速 / 主动视觉：
 *   首帧从后端 GET /api/settings 读取，切换走 PUT /api/settings 热更新
 *   （失败不阻断界面，待后端上线后自动同步）。
 * - 电脑控制授权：已接入真实后端（GET /api/computer/status + POST /api/computer/authorize）。
 * - 本页默认值与后端 config 默认值一致：deepseek / 本地模式关 / cx-open / 国内线路。
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
 * 下载线路选项（与向导 CHANNEL_OPTIONS 同口径：一个问题定全部，
 * 模型仓库由线路在服务端派生，前端不单独持有 source）。
 */
const CHANNEL_OPTIONS: Array<{ value: DownloadChannel; label: string; desc: string }> = [
  { value: 'mirror', label: '国内线路（魔塔，推荐）', desc: '下载更快更稳，模型从国内的魔塔拿' },
  { value: 'official', label: '海外线路（HuggingFace）', desc: '直连海外站点，模型从 HuggingFace 拿' },
];

/** 语音加速方式选项（tts.accel，值域与后端白名单一致；展示中文化） */
const TTS_ACCEL_OPTIONS: Array<{ value: TtsAccel; label: string }> = [
  { value: 'auto', label: '自动（推荐）' },
  { value: 'cpu', label: '处理器（稳定省电）' },
  { value: 'cuda', label: 'NVIDIA 显卡加速' },
  { value: 'dml', label: '显卡加速（通用）' },
  { value: 'rocm', label: 'AMD 显卡加速' },
  { value: 'off', label: '关闭加速' },
];

/** 语音加速设备选项（tts.accel_device；'' = 自动选设备） */
const TTS_ACCEL_DEVICE_OPTIONS: Array<{ value: TtsAccelDevice; label: string }> = [
  { value: '', label: '自动选设备' },
  { value: 'igpu', label: '核显（更省电）' },
  { value: 'dgpu', label: '独立显卡（更快）' },
];

/** tts.accel 合法值域（后端白名单外的值回落 auto） */
const TTS_ACCEL_VALUES: readonly string[] = TTS_ACCEL_OPTIONS.map((o) => o.value);
/** tts.accel_device 合法值域（白名单外的值回落 ''） */
const TTS_ACCEL_DEVICE_VALUES: readonly string[] = TTS_ACCEL_DEVICE_OPTIONS.map((o) => o.value);

/**
 * 设备画像是否「明确已知」：仅当 recommend 探测成功且返回了核显/独显结论
 * （has_igpu 为布尔、dgpu_vendor 键存在）时才参与选项过滤。
 *
 * 为什么需要这个闸门：探测失败的后端降级画像（_degraded_profile）不带这两个字段
 * （undefined），一次探测失败不应把功能选项永久藏没——字段缺失时保守显示全部
 * （与改动前行为一致）；只有「明确探测到」才裁剪。
 */
export function hardwareKnown(profile: HardwareProfile | null): boolean {
  return (
    profile != null &&
    typeof profile.has_igpu === 'boolean' &&
    profile.dgpu_vendor !== undefined
  );
}

/** 该设备是否有任一可用于加速的 GPU（核显或已知独显；N 卡经 gpu_vendor 兜底） */
function hasAnyGpu(profile: HardwareProfile): boolean {
  return profile.has_igpu === true || !!profile.dgpu_vendor || profile.gpu_vendor === 'nvidia';
}

/**
 * 按设备画像过滤「加速方式」选项（tts.accel）：
 * cuda 仅 N 卡显示、rocm 仅 AMD 独显显示、dml（通用显卡加速）仅在任一真 GPU 时显示；
 * auto / cpu / off 恒显示。画像未知（hardwareKnown=false）时原样返回全部。
 */
export function filterTtsAccelOptions(profile: HardwareProfile | null): Array<{
  value: TtsAccel;
  label: string;
}> {
  if (!hardwareKnown(profile)) return TTS_ACCEL_OPTIONS;
  const p = profile as HardwareProfile;
  return TTS_ACCEL_OPTIONS.filter((opt) => {
    if (opt.value === 'cuda') return p.dgpu_vendor === 'nvidia' || p.gpu_vendor === 'nvidia';
    if (opt.value === 'rocm') return p.dgpu_vendor === 'amd';
    if (opt.value === 'dml') return hasAnyGpu(p);
    return true;
  });
}

/**
 * 按设备画像过滤「加速设备」选项（tts.accel_device）：
 * 「核显」仅 has_igpu 时显示、「独立显卡」仅有已知独显时显示；
 * 「自动选设备」恒显示。画像未知时原样返回全部。
 */
export function filterTtsAccelDeviceOptions(profile: HardwareProfile | null): Array<{
  value: TtsAccelDevice;
  label: string;
}> {
  if (!hardwareKnown(profile)) return TTS_ACCEL_DEVICE_OPTIONS;
  const p = profile as HardwareProfile;
  return TTS_ACCEL_DEVICE_OPTIONS.filter((opt) => {
    if (opt.value === 'igpu') return p.has_igpu === true;
    if (opt.value === 'dgpu') return !!p.dgpu_vendor;
    return true;
  });
}

/**
 * 「运行偏好（省电/性能）」卡片是否显示：无任何真 GPU 时隐藏——
 * accel_plan 语义下无核显节能模式 TTS 落点为 cpu、无 GPU 性能模式同为 cpu，
 * 双模式完全等价，展示两选项只会让用户困惑（同一问题的另一半：有核显/独显时
 * 双模式分别对应不同加速落点，仍保留）。
 */
export function shouldShowAccelModeCard(profile: HardwareProfile | null): boolean {
  if (!hardwareKnown(profile)) return true;
  return hasAnyGpu(profile as HardwareProfile);
}

/** LLM 显卡偏好选项（value 对应 local_llm.gpu_preference 白名单） */
const LLM_GPU_OPTIONS: Array<{ value: string; label: string; match: (p: HardwareProfile) => boolean }> = [
  { value: '', label: '自动（推荐）', match: () => true },
  {
    value: 'dgpu',
    label: 'NVIDIA 独显',
    match: (p) => p.dgpu_vendor === 'nvidia' || p.gpu_vendor === 'nvidia',
  },
  {
    value: 'dgpu',
    label: '独立显卡（AMD/Intel）',
    match: (p) => p.dgpu_vendor === 'amd' || p.dgpu_vendor === 'intel',
  },
  { value: 'igpu', label: '核显', match: (p) => p.has_igpu === true },
];

/**
 * 按设备画像过滤「本地大脑跑哪块卡」选项：自动恒显示；独显/核显选项仅在
 * 设备实际具备时显示。画像未知（hardwareKnown=false）时原样返回全部。
 * 注意 value 可重复（dgpu 按厂商拆两个 label），key 消费方须用 label 区分。
 */
export function filterLlmGpuOptions(profile: HardwareProfile | null): Array<{
  value: string;
  label: string;
}> {
  if (!hardwareKnown(profile)) return LLM_GPU_OPTIONS.map(({ value, label }) => ({ value, label }));
  const p = profile as HardwareProfile;
  return LLM_GPU_OPTIONS.filter((opt) => opt.match(p)).map(({ value, label }) => ({ value, label }));
}

/** LLM 显卡偏好合法值域（与后端 _LLM_GPU_PREFERENCE_VALUES 一致） */
const LLM_GPU_VALUES: readonly string[] = ['', 'igpu', 'dgpu'];

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
  // 聊天时自动回忆开关（GET settings memory.context_inject，RAG 记忆注入）
  const [contextInject, setContextInject] = useState(true);
  // 音色导入成功的轻量提示（样式与运行偏好提示 accelHint 同款）
  const [importHint, setImportHint] = useState<string | null>(null);

  // ---- 向导选项入设置（Task 9）：API Key / 下载线路 / 本地模型档位 / 语音加速 ----
  // API Key：输入框留空（新值才输入）；apiKeyMasked 为后端脱敏回显（sk-****尾4位），
  // 以占位符展示——已有钥匙不回填明文，输入新值保存即覆盖
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [apiKeyMasked, setApiKeyMasked] = useState('');
  const [apiKeyHint, setApiKeyHint] = useState<string | null>(null);
  const [apiKeySaving, setApiKeySaving] = useState(false);
  // 下载线路（mirror=国内魔塔 / official=海外 HuggingFace；缺省回落 mirror）
  const [channel, setChannel] = useState<DownloadChannel>('mirror');
  const [channelHint, setChannelHint] = useState<string | null>(null);
  // 语音加速（tts.accel / tts.accel_device）
  const [ttsAccel, setTtsAccel] = useState<TtsAccel>('auto');
  const [ttsAccelDevice, setTtsAccelDevice] = useState<TtsAccelDevice>('');
  const [ttsHint, setTtsHint] = useState<string | null>(null);
  // 当前本地模型路径（后端视图可得才传给档位卡展示）
  const [modelPath, setModelPath] = useState('');

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
  // 设备画像（GET /api/setup/recommend 的 profile）：驱动 TTS 选项 / 运行偏好按
  // 实际硬件裁剪显示；拉取失败保持 null（保守显示全部，一次失败不藏功能）
  const [hwProfile, setHwProfile] = useState<HardwareProfile | null>(null);
  // LLM 显卡偏好（local_llm.gpu_preference，20261006）："" 自动 / igpu 核显 / dgpu 独显
  const [llmGpuPref, setLlmGpuPref] = useState<string>('');
  const [llmGpuHint, setLlmGpuHint] = useState<string | null>(null);
  // 语音交互模式（voice.interaction_mode，20261006 全双工降级版）：vad 传统 / duplex 全双工
  const [interactionMode, setInteractionMode] = useState<'vad' | 'duplex'>('vad');
  const [voiceModeHint, setVoiceModeHint] = useState<string | null>(null);

  // 挂载初始化：拉后端配置视图 + 音色包列表 + 电脑控制状态；失败回退默认值（与 config 默认一致）
  useEffect(() => {
    let alive = true;
    (async () => {
      if (IS_BACKEND_READY) {
        // 设备画像：独立拉取、失败静默（选项过滤保守退化为全部显示）
        void fetchSetupRecommend()
          .then((res) => {
            if (alive) setHwProfile(res?.profile ?? null);
          })
          .catch(() => {
            /* 探测失败 → hwProfile 保持 null → 显示全部选项（与旧行为一致） */
          });
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
          setContextInject(Boolean(st?.memory?.context_inject ?? true));
          const v = st?.tts?.voice || FALLBACK_VOICE;
          setVoice(v);
          currentVoice = v;
          // 运行偏好：以后端值为准，非法/缺失回落默认
          const m = st?.accel?.mode;
          setAccelMode(m === 'eco' || m === 'performance' ? m : FALLBACK_ACCEL_MODE);
          // API Key 脱敏回显（后端已脱敏，形如 sk-****尾4位；未配置为空）
          setApiKeyMasked(typeof st?.cloud?.api_key === 'string' ? st.cloud.api_key : '');
          // 下载线路：非法/缺失回落 mirror（与向导默认一致）
          setChannel(st?.download?.channel === 'official' ? 'official' : 'mirror');
          // 语音加速：白名单外 / 缺失回落 auto 与 ''（自动）
          const acc = st?.tts?.accel;
          setTtsAccel(
            typeof acc === 'string' && TTS_ACCEL_VALUES.includes(acc) ? (acc as TtsAccel) : 'auto',
          );
          const accDev = st?.tts?.accel_device;
          setTtsAccelDevice(
            typeof accDev === 'string' && TTS_ACCEL_DEVICE_VALUES.includes(accDev)
              ? (accDev as TtsAccelDevice)
              : '',
          );
          // 当前本地模型路径：可得才展示（档位卡据此显示/隐藏该行）
          setModelPath(typeof st?.local_llm?.model_path === 'string' ? st.local_llm.model_path : '');
          // LLM 显卡偏好：白名单外 / 缺失回落自动（""）
          const gp = st?.local_llm?.gpu_preference;
          setLlmGpuPref(typeof gp === 'string' && LLM_GPU_VALUES.includes(gp) ? gp : '');
          // 语音交互模式：白名单外 / 缺失回落传统（vad）
          const vMode = st?.voice?.interaction_mode;
          setInteractionMode(vMode === 'duplex' ? 'duplex' : 'vad');
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
  // 聊天时自动回忆开关：PUT /api/settings {memory:{context_inject}}（热更段即时生效）
  const handleContextInjectChange = (next: boolean) => {
    setContextInject(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setCustomHint(null);
    void updateSettings({ memory: { context_inject: next } }).catch(() => {
      if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到失败
      setSaveError('自动回忆开关没保存上…待会儿再拨一次就好啦');
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

  // ---- 向导选项入设置（Task 9）：保存链路均走 PUT /api/settings，失败内联显错不静默 ----

  /** 保存 API Key：输入新值覆盖（输入框平时留空）；成功后清空输入、占位符换新脱敏值 */
  const handleSaveApiKey = async () => {
    const key = apiKeyInput.trim();
    if (!key || apiKeySaving) return;
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setApiKeyHint(null);
    setApiKeySaving(true);
    try {
      const res = await updateSettings({ cloud: { api_key: key } });
      if (seq !== settingsSeqRef.current) return; // 已有更新的操作接管，丢弃迟到响应
      const masked = res.config?.cloud?.api_key;
      if (typeof masked === 'string' && masked) setApiKeyMasked(masked);
      setApiKeyInput('');
      setApiKeyHint('API Key 已保存');
    } catch {
      if (seq !== settingsSeqRef.current) return;
      setSaveError('API Key 没保存上…待会儿再试一次就好啦');
    } finally {
      if (seq === settingsSeqRef.current) setApiKeySaving(false);
    }
  };

  /** 切换下载线路：PUT {download:{channel}}；模型仓库由线路在服务端派生（前端不传 source） */
  const handleChannelChange = (next: DownloadChannel) => {
    setChannel(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setChannelHint(null);
    void updateSettings({ download: { channel: next } })
      .then(() => {
        if (seq !== settingsSeqRef.current) return;
        setChannelHint('下载线路已保存，之后的模型下载会走新线路');
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('下载线路没保存上…待会儿再选一次就好啦');
      });
  };

  /** 切换语音加速方式：PUT {tts:{accel}}；后端重建语音桥后按需提示重启（不静默） */
  const handleTtsAccelChange = (next: TtsAccel) => {
    setTtsAccel(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setTtsHint(null);
    void updateSettings({ tts: { accel: next } })
      .then((res) => {
        if (seq !== settingsSeqRef.current) return;
        const wb = res.voice_backend;
        setTtsHint(
          wb?.needs_restart ? wb.message || '已保存，重启应用后完全生效' : '语音加速已保存',
        );
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('语音加速没保存上…待会儿再选一次就好啦');
      });
  };

  /** 切换语音加速设备：PUT {tts:{accel_device}}（'' = 自动选设备） */
  const handleTtsAccelDeviceChange = (next: TtsAccelDevice) => {
    setTtsAccelDevice(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setTtsHint(null);
    void updateSettings({ tts: { accel_device: next } })
      .then((res) => {
        if (seq !== settingsSeqRef.current) return;
        const wb = res.voice_backend;
        setTtsHint(
          wb?.needs_restart ? wb.message || '已保存，重启应用后完全生效' : '语音加速已保存',
        );
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('语音加速设备没保存上…待会儿再选一次就好啦');
      });
  };

  /**
   * 切换 LLM 显卡（local_llm.gpu_preference，20261006）：保存后后端经
   * accel_plan 重算落点并热重建本地大脑——提示语说明重新加载与嵌入需重启。
   */
  const handleLlmGpuPrefChange = (next: string) => {
    setLlmGpuPref(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setLlmGpuHint(null);
    void updateSettings({ local_llm: { gpu_preference: next } })
      .then(() => {
        if (seq !== settingsSeqRef.current) return;
        setLlmGpuHint('已保存——本地大脑正在按新显卡重新加载，稍后就绪');
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('显卡选择没保存上…待会儿再选一次就好啦');
      });
  };

  /** 切换语音交互模式（voice.interaction_mode）：保存后下一次语音会话按新模式运行。 */
  const handleInteractionModeChange = (next: 'vad' | 'duplex') => {
    setInteractionMode(next);
    const seq = ++settingsSeqRef.current;
    setSaveError(null);
    setVoiceModeHint(null);
    void updateSettings({ voice: { interaction_mode: next } })
      .then(() => {
        if (seq !== settingsSeqRef.current) return;
        setVoiceModeHint(
          next === 'duplex' ? '已切换——全双工模式建议戴耳机（无回声消除）' : '已切换',
        );
      })
      .catch(() => {
        if (seq !== settingsSeqRef.current) return;
        setSaveError('语音模式没保存上…待会儿再选一次就好啦');
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

      {/* 向导选项入设置（Task 9）：API Key / 下载线路 / 语音加速的保存成功提示 */}
      {apiKeyHint && (
        <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{apiKeyHint}</p>
      )}
      {channelHint && (
        <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{channelHint}</p>
      )}
      {ttsHint && <p className="-mt-1 mb-2 text-xs text-[var(--text-secondary)]">{ttsHint}</p>}

      <div className="flex max-w-2xl flex-col gap-4">
        {/* 云端提供商：本地模式开启时聊天完全不碰云端，配置项隐藏并说明入口
            （不静默消失——原位一行字告诉用户想配云端该怎么做） */}
        {localMode ? (
          <GlassCard>
            <div className="flex flex-col gap-1 p-4">
              <p className="text-sm font-medium">云端大脑</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                本地模式开着，聊天都在这台电脑上完成，用不上云端；想配云端大脑的话，先关掉上面的「本地模式」
              </p>
            </div>
          </GlassCard>
        ) : (
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
            {/* API Key：脱敏回显占位（sk-****尾4位），输入新值保存即覆盖 */}
            <div className="mt-1 flex flex-col gap-1.5">
              <label className="text-sm font-medium" htmlFor="settings-api-key">
                云端钥匙（API Key）
              </label>
              <div className="flex items-center gap-2">
                <input
                  id="settings-api-key"
                  type="password"
                  value={apiKeyInput}
                  onChange={(e) => {
                    setApiKeyInput(e.target.value);
                    setApiKeyHint(null);
                  }}
                  placeholder={apiKeyMasked ? `已配置（${apiKeyMasked}），输入新值可覆盖` : '还没有配置，粘贴你的钥匙'}
                  autoComplete="off"
                  className="h-9 flex-1 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
                />
                <button
                  type="button"
                  onClick={() => {
                    void handleSaveApiKey();
                  }}
                  disabled={apiKeyInput.trim() === '' || apiKeySaving}
                  className="shrink-0 rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {apiKeySaving ? '保存中…' : '保存钥匙'}
                </button>
              </div>
              <p className="text-xs text-[var(--text-tertiary)]">
                钥匙只存在这台电脑上；已配置的只显示结尾几位，不会泄露完整内容
              </p>
            </div>
          </div>
        </GlassCard>
        )}

        {/* 运行偏好（省电优先 / 性能优先）：保存后按新配置重建语音桥生效；
            无任何 GPU 的设备上双模式加速落点完全等价（accel_plan 均为 cpu），整卡隐藏 */}
        {shouldShowAccelModeCard(hwProfile) && (
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
        )}

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

        {/* 本地大脑跑哪块卡（20261006 LLM 显卡切换）：localMode 且设备有可用 GPU
            时才渲染（无显卡设备不显示相关配置项）；选项按设备画像过滤 */}
        {localMode && shouldShowAccelModeCard(hwProfile) && (
          <GlassCard>
            <div className="flex flex-col gap-2 p-4">
              <p className="font-medium">本地大脑跑哪块卡</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                玩游戏时可以让大脑用核显跑；想更快就用独显；不确定选自动
              </p>
              <select
                id="settings-llm-gpu"
                value={llmGpuPref}
                onChange={(e) => handleLlmGpuPrefChange(e.target.value)}
                className="mt-1 h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
              >
                {filterLlmGpuOptions(hwProfile).map((opt) => (
                  <option key={opt.label} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              {llmGpuHint && (
                <p className="text-xs text-[var(--text-secondary)]">{llmGpuHint}</p>
              )}
            </div>
          </GlassCard>
        )}

        {/* 本地模型档位管理（Task 9.3）：当前模型 + 四档下载 + 进度 + 取消；
            「设为本地默认大脑」沿用既有 local_llm.enabled 保存链路 */}
        <ModelTierCard
          modelPath={modelPath || undefined}
          localEnabled={localMode}
          onLocalEnabledChange={handleLocalModeChange}
        />

        {/* 下载线路（Task 9.2）：一个问题定全部，模型仓库由线路在服务端派生 */}
        <GlassCard>
          <div className="flex flex-col gap-2 p-4">
            <p className="font-medium">下载线路</p>
            <p className="text-xs text-[var(--text-tertiary)]">
              模型下载走哪条路；国内更快更稳，随时能换
            </p>
            <div className="mt-1 flex flex-col gap-2">
              {CHANNEL_OPTIONS.map((opt) => {
                const active = channel === opt.value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => handleChannelChange(opt.value)}
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

        {/* 主动视觉 */}
        <SettingRow
          title="主动视觉"
          desc="开启后它会看看屏幕、记住你正在忙什么，越陪你越懂你；画面只在这台电脑上理解、不会上传（本地大脑没就绪时才会用云端补位）"
        >
          <Toggle checked={visionEnabled} onChange={handleVisionChange} label="主动视觉" />
        </SettingRow>

        {/* 聊天时自动回忆（RAG 记忆注入，20261005） */}
        <SettingRow
          title="聊天时自动回忆"
          desc="聊天时自动想起相关的记忆，聊过的事它自然记得；关闭后只有到记忆页主动查才会用"
        >
          <Toggle checked={contextInject} onChange={handleContextInjectChange} label="聊天时自动回忆" />
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
            <label className="text-sm font-medium" htmlFor="settings-voice">
              音色
            </label>
            <p className="text-xs text-[var(--text-tertiary)]">挑一个舒服的声音陪你说话</p>
            <select
              id="settings-voice"
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

        {/* 桌宠模型（Task 8）：更换自定义 VRM / 恢复默认；导入成功后悬浮窗经总线立即重载 */}
        <GlassCard>
          <div className="flex flex-col gap-2 p-4">
            <p className="font-medium">桌宠模型</p>
            <p className="text-xs text-[var(--text-tertiary)]">
              挑一个你喜欢的 VRM 模型换上；不满意随时一键恢复默认（原模型会自动备份）
            </p>
            <PetModelControls />
          </div>
        </GlassCard>

        {/* 语音加速（Task 9.4）：tts.accel / tts.accel_device 两键热更；需重启时明确提示 */}
        <GlassCard>
          <div className="flex flex-col gap-2 p-4">
            <p className="font-medium">语音加速</p>
            <p className="text-xs text-[var(--text-tertiary)]">
              让说话的声音合成得更快更顺；不确定就选「自动」
            </p>
            <div className="mt-1 flex flex-col gap-2 sm:flex-row sm:gap-3">
              <div className="flex flex-1 flex-col gap-1">
                <label className="text-sm font-medium" htmlFor="settings-tts-accel">
                  加速方式
                </label>
                <select
                  id="settings-tts-accel"
                  value={ttsAccel}
                  onChange={(e) => handleTtsAccelChange(e.target.value as TtsAccel)}
                  className="h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
                >
                  {/* 按设备画像过滤后的选项；当前已存值若被过滤掉（如换卡后），附加展示防空白 */}
                  {filterTtsAccelOptions(hwProfile).map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                  {!filterTtsAccelOptions(hwProfile).some((opt) => opt.value === ttsAccel) && (
                    <option value={ttsAccel}>
                      {TTS_ACCEL_OPTIONS.find((opt) => opt.value === ttsAccel)?.label ?? ttsAccel}（当前值）
                    </option>
                  )}
                </select>
              </div>
              <div className="flex flex-1 flex-col gap-1">
                <label className="text-sm font-medium" htmlFor="settings-tts-accel-device">
                  加速设备
                </label>
                <select
                  id="settings-tts-accel-device"
                  value={ttsAccelDevice}
                  onChange={(e) => handleTtsAccelDeviceChange(e.target.value as TtsAccelDevice)}
                  className="h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
                >
                  {/* 按设备画像过滤：无核显不显示核显、无独显不显示独显（自动选设备恒在） */}
                  {filterTtsAccelDeviceOptions(hwProfile).map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                  {!filterTtsAccelDeviceOptions(hwProfile).some(
                    (opt) => opt.value === ttsAccelDevice,
                  ) && (
                    <option value={ttsAccelDevice}>
                      {
                        TTS_ACCEL_DEVICE_OPTIONS.find((opt) => opt.value === ttsAccelDevice)?.label ??
                        ttsAccelDevice
                      }
                      （当前值）
                    </option>
                  )}
                </select>
              </div>
            </div>
          </div>
        </GlassCard>

        {/* 语音交互模式（20261006 全双工降级版）：vad=传统自动断句 / duplex=全双工；
            切换保存后下一次语音会话按新模式运行（voice 段热更新，无需重启） */}
        <GlassCard>
          <div className="flex flex-col gap-1.5 p-4">
            <p className="font-medium">语音交互模式</p>
            <p className="text-xs text-[var(--text-tertiary)]">
              传统：说一句话停一下，它自动接话；全双工：边说边聊，说话时能打断它
            </p>
            <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <button
                type="button"
                aria-pressed={interactionMode === 'vad'}
                onClick={() => handleInteractionModeChange('vad')}
                className={[
                  'flex flex-col gap-0.5 rounded-xl border px-3 py-2 text-left text-sm transition',
                  interactionMode === 'vad'
                    ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
                    : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]',
                ].join(' ')}
              >
                <span>传统 · 自动断句</span>
                <span className="text-xs text-[var(--text-tertiary)]">说一句停一下，稳一些</span>
              </button>
              <button
                type="button"
                aria-pressed={interactionMode === 'duplex'}
                onClick={() => handleInteractionModeChange('duplex')}
                className={[
                  'flex flex-col gap-0.5 rounded-xl border px-3 py-2 text-left text-sm transition',
                  interactionMode === 'duplex'
                    ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
                    : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]',
                ].join(' ')}
              >
                <span>全双工 · 边说边聊</span>
                <span className="text-xs text-[var(--text-tertiary)]">
                  可以连着说，还能随时打断它
                </span>
              </button>
            </div>
            {voiceModeHint && (
              <p className="text-xs text-[var(--text-secondary)]">{voiceModeHint}</p>
            )}
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