import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AudioLines, Eye, Maximize2, Monitor, ShieldCheck, X } from 'lucide-react';
import VrmAvatar from './VrmAvatar';
import { normalizeBackendMood, type PetMood } from '../petMood';
import { usePetMoodFeed } from '../hooks/usePetMoodFeed';
import { PET_ENABLED_KEY } from '../hooks/usePetEnabled';
import {
  fetchComputerStatus,
  fetchSettings,
  onPetModelReload,
  publishChatTick,
  readPetModelReloadTick,
  sendChatMessage,
  setComputerAuthorized,
  synthesizeSpeechStream,
  transcribeAudio,
  updateSettings,
} from '../api';
import { startRecording, type RecordingSession } from '../audioRecorder';
import {
  closePetOverlay,
  dragPetOverlayEnd,
  dragPetOverlayStart,
  getPetOverlaySizeBounds,
  resizePetOverlay,
  showMainWindow,
} from '../bridge';
import {
  advanceDragGesture,
  createDragGestureState,
  isDragGestureClick,
  type DragGestureState,
} from './petDragGesture';

/**
 * PetOverlay — Electron 桌宠透明悬浮窗的独立根组件。
 *
 * 渲染真实 VRM 模型（VrmAvatar，three + @pixiv/three-vrm，透明背景 canvas）；
 * 当无 WebGL / 模型接口不可达 / 解析失败时，在窗口内给出「暂时显示不了 3D 桌宠」
 * 的中文提示（含可读原因），**不回落卡通形象**——回落会让人误以为「根本没做 VRM」
 * （见 VrmAvatar 的兜底逻辑与 data-vrm-state 状态位）。
 *
 * ======================== 接线说明（Electron 环境生效） ========================
 * 1. main.js 的 createPetOverlayWindow() 创建透明、无边框、置顶、跳过任务栏的
 *    悬浮窗（初始尺寸 = 中档预设，transparent:true），由 IPC『pet-overlay:open』
 *    触发创建，『pet-overlay:close』关闭；本组件经独立入口 pet-overlay.html 挂载。
 * 2. 透明开启：BrowserWindow 以 show:false 创建，ready-to-show 后再 show()，
 *    避免部分 Linux 上直接 show 会丢透明；VRM canvas 亦以 alpha+setClearAlpha(0) 保证透明。
 * 3. 关闭链路：点菜单「关闭」→ bridge.closePetOverlay()（IPC）让主进程关窗；
 *    同时写 localStorage 记录关闭状态，主窗口内 usePetEnabled 经 storage
 *    事件同步收敛开关显示。开后必有窗、关后必无窗，断链不再出现。
 * 4. 拖拽（主进程光标跟随，20261003_模块0_桌宠拖拽主进程化）：桌宠本体
 *    pointerdown 发 pet-overlay:drag-start——主进程启动 16ms 光标跟随循环
 *    （getCursorScreenPoint → setPosition，无渲染层 IPC 往返，跟手关键）；
 *    pointermove 仅推进 petDragGesture 纯逻辑（>6px 判定拖拽，零 IPC）；
 *    pointerup/cancel 发 drag-end 停表，未拖拽视为点击弹/收菜单。仅左键拖拽。
 * 5. 弹出菜单（对齐 CX-O PetContextMenu 弧形气泡的应用控制语义）：点击本体弹出——
 *    圆形玻璃按钮沿桌宠上半身椭圆弧排布（打开主窗口/说话/屏幕共享/操作授权/大小/关闭），
 *    开关语义（聆听/共享/授权对应项主色填充）；hover 放大 + 侧向文字标签；
 *    逐个错峰入场动画；点击他处 / Escape / 窗口失焦关闭；弧形随窗口档位缩放并收拢在视口内。
 * 6. 尺寸档位：220 / 286 / 360 三档（画面宽）。点击档位 → 写 localStorage
 *    『cx-a.petSize』→ resizePetOverlay(档位)（主进程按映射表换窗口宽高、保持
 *    中心不变）→ setSize + VrmAvatar key={size} 重挂载（模型字节走模块级缓存，
 *    零请求零等待无感切换）。挂载时读记忆档位并幂等校准窗口尺寸。
 * ======================== 鼠标穿透说明 ========================
 * 本组件未做整窗镂空穿透。要「区域外点击穿透到桌面」时，可：
 *   - 交互区（本体 / 菜单）保留 pointer-events:auto；
 *   - 非交互展示区设 pointer-events:none；
 *   - 并在 BrowserWindow 侧配合 setIgnoreMouseEvents(true, { forward: true })。
 * 当前简单起见整窗保留可交互，穿透作为后续扩展点。
 */

/** 尺寸量程兜底值（主进程按主屏分辨率推导失败时使用；正常量程经 size-bounds IPC 取得） */
const PET_SIZE_FALLBACK_BOUNDS = { min: 160, max: 640 } as const;
const PET_SIZE_DEFAULT = 286;
type PetSize = number;

/** 尺寸记忆的本地存储键 */
const PET_SIZE_KEY = 'cx-a.petSize';

/** 语音链路状态：空闲 / 录音中（聆听）/ 识别+对话中 / 朗读回复中 */
type VoiceState = 'idle' | 'listening' | 'thinking' | 'speaking';

/** 读取记忆尺寸；非法 / 缺失回落默认 286（先按兜底量程钳制，量程到位后再动态收紧） */
function readStoredPetSize(): PetSize {
  try {
    const raw = window.localStorage.getItem(PET_SIZE_KEY);
    const value = raw === null ? NaN : Number(raw);
    if (!Number.isFinite(value)) return PET_SIZE_DEFAULT;
    return Math.max(
      PET_SIZE_FALLBACK_BOUNDS.min,
      Math.min(PET_SIZE_FALLBACK_BOUNDS.max, Math.round(value)),
    );
  } catch {
    return PET_SIZE_DEFAULT;
  }
}

/** 弧形菜单项：e2e 与用户都按 accessible name 定位（打开主窗口/说话/屏幕共享/操作授权/大小/关闭） */
interface PetMenuItem {
  key: string;
  /** accessible name（aria-label），同时是 hover 侧标签文案 */
  name: string;
  /** 圆钮内容：icon 优先；无 icon 时渲染 labelText 文字 */
  icon?: React.ReactNode;
  labelText?: string;
  checked?: boolean;
  danger?: boolean;
  /** 滑块项（CX-O PetContextMenu 同款）：圆钮侧挂玻璃胶囊 range，实时 onChange */
  slider?: {
    value: number;
    min: number;
    max: number;
    onChange: (value: number) => void;
  };
  onSelect: () => void;
}

export default function PetOverlay() {
  // 表情由 LLM 情绪标签驱动（20261004_模块0_桌宠表情LLM标签驱动）：聊天回复的
  // mood 经表情总线跨窗口送达本组件，非 calm 档 8 秒后自动回落——表情是瞬时
  // 反应而非本地按钮写死的持久状态（表情按钮已按 CX-O 语义对齐移除）。
  // pushMood（发布 + 本窗口立即生效）：悬浮窗「说话」链路回复到达时直接驱动表情。
  const { mood, pushMood } = usePetMoodFeed();
  const [voiceState, setVoiceState] = useState<VoiceState>('idle');
  const [size, setSize] = useState<PetSize>(() => readStoredPetSize());
  const [sizeBounds, setSizeBounds] = useState<{ min: number; max: number }>(
    PET_SIZE_FALLBACK_BOUNDS,
  );
  const [menuOpen, setMenuOpen] = useState(false);
  /** 屏幕共享开关（vision.enabled；null=尚未探测到，按关闭渲染） */
  const [screenShare, setScreenShare] = useState<boolean | null>(null);
  /** 操作授权开关（电脑控制授权；null=尚未探测到，按未授权渲染） */
  const [computerAuth, setComputerAuth] = useState<boolean | null>(null);
  // 模型代际（petModelReload 总线）：主窗口更换桌宠模型后经 storage 事件送达，
  // 叠加到 VrmAvatar 的 reloadKey 触发作废旧缓存并重载新模型（挂载时先对齐当前 tick）。
  const [modelTick, setModelTick] = useState<number>(() => readPetModelReloadTick());

  useEffect(
    () =>
      onPetModelReload(() => {
        setModelTick(readPetModelReloadTick());
      }),
    [],
  );

  const menuRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragGestureState | null>(null);
  /** 语音链路句柄：录音会话 / 朗读中断控制器 */
  const recordingRef = useRef<RecordingSession | null>(null);
  const speakAbortRef = useRef<AbortController | null>(null);

  // 挂载即校准窗口尺寸（幂等）：主进程建窗固定为默认档，若记忆尺寸非默认，
  // 这里立即把窗口对齐到记忆尺寸，保证「初始 size 与窗口大小一致」
  useEffect(() => {
    void resizePetOverlay(size).catch(() => {
      /* 主进程不可达时静默：档位下次交互仍可再校准 */
    });
    // 量程按主屏分辨率动态推导：到位后收紧当前记忆尺寸（越界则同步窗口）
    void getPetOverlaySizeBounds()
      .then((bounds) => {
        // 防御：量程数值非法（如主进程返回 NaN/null）时沿用兜底量程，拒绝污染尺寸链
        const min = Number.isFinite(bounds?.min) ? Math.round(bounds.min) : PET_SIZE_FALLBACK_BOUNDS.min;
        const max = Number.isFinite(bounds?.max) ? Math.round(bounds.max) : PET_SIZE_FALLBACK_BOUNDS.max;
        if (!(min > 0 && max > min)) return;
        setSizeBounds({ min, max });
        setSize((current) => {
          const clamped = Math.max(min, Math.min(max, Math.round(current)));
          if (clamped !== current) {
            void resizePetOverlay(clamped).catch(() => {
              /* 静默 */
            });
            try {
              window.localStorage.setItem(PET_SIZE_KEY, String(clamped));
            } catch {
              /* no-op */
            }
          }
          return clamped;
        });
      })
      .catch(() => {
        /* 主进程不可达：沿用兜底量程 */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 开关态探测（一次即可；失败静默——按钮按关闭/未授权渲染，点击时再实时取真值）
  useEffect(() => {
    fetchSettings()
      .then((settings) => setScreenShare(settings?.vision?.enabled ?? false))
      .catch(() => {
        /* 后端不可达：按钮仍可点，点击时按当前本地值取反 */
      });
    fetchComputerStatus()
      .then((status) => setComputerAuth(status.authorized))
      .catch(() => {
        /* 同上 */
      });
  }, []);

  // 卸载收尾：停录音、停朗读（悬浮窗关闭/热重载不留悬挂会话）
  useEffect(() => {
    return () => {
      try {
        recordingRef.current?.cancel();
      } catch {
        /* no-op */
      }
      try {
        speakAbortRef.current?.abort();
      } catch {
        /* no-op */
      }
    };
  }, []);

  // 菜单展开时：Escape / 窗口失焦关闭（对齐 CX-O；点击他处由根 pointerdown 收敛）
  useEffect(() => {
    if (!menuOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('blur', () => setMenuOpen(false));
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [menuOpen]);

  // 换档：写记忆 → 同步 renderer 画面（key={size} 重挂载 VrmAvatar，模型字节走缓存）
  // → 主进程换窗口宽高（保持中心不变）
  const applySize = (next: PetSize) => {
    try {
      window.localStorage.setItem(PET_SIZE_KEY, String(next));
    } catch {
      /* 存储不可用（隐私模式等）时静默：本会话内档位仍生效 */
    }
    setSize(next);
    void resizePetOverlay(next).catch(() => {
      /* 主进程不可达时静默 */
    });
  };

  // 关闭：优先经 IPC 桥让主进程关闭悬浮窗；localStorage 写入保留作状态记录
  const handleClose = () => {
    try {
      window.localStorage.setItem(PET_ENABLED_KEY, '0');
    } catch {
      /* no-op */
    }
    stopVoiceSession();
    void closePetOverlay().catch(() => {
      /* 主进程不可达时静默：窗口自身状态仍由本次写入收敛 */
    });
  };

  // ---- 语音链路（20261004 悬浮窗语音闭环）：录音 → 识别 → 对话 → 朗读回复 ----
  // 「说话」是语音输入开关（不是口型动画）：开启录音（聆听），再点停止并把
  // 识别文本直发 /api/chat/message；回复经流式合成在悬浮窗内朗读（口型由
  // data-talking 驱动），表情经 pushMood 即时驱动，历史经聊天刷新总线同步主窗口。

  /** 停止当前朗读（中断流式请求 + 复位状态；录音会话由调用方单独管理）。 */
  const stopSpeaking = useCallback(() => {
    const abort = speakAbortRef.current;
    speakAbortRef.current = null;
    try {
      abort?.abort();
    } catch {
      /* no-op */
    }
  }, []);

  /** 停掉整段语音会话（朗读 + 录音；关闭悬浮窗 / 开始新一轮前调用）。 */
  const stopVoiceSession = useCallback(() => {
    stopSpeaking();
    const session = recordingRef.current;
    recordingRef.current = null;
    try {
      session?.cancel();
    } catch {
      /* no-op */
    }
    setVoiceState('idle');
  }, [stopSpeaking]);

  /** 朗读回复（流式合成简化版，参考 ChatPage speak/playNext）：播放中驱动口型。 */
  const speakReply = useCallback(
    (text: string) => {
      stopSpeaking();
      if (!text.trim()) {
        setVoiceState('idle');
        return;
      }
      setVoiceState('speaking');
      const controller = new AbortController();
      speakAbortRef.current = controller;
      const queue: string[] = [];
      let playing = false;
      let finished = false;

      const playNext = () => {
        const next = queue.shift();
        if (!next) {
          playing = false;
          if (finished) setVoiceState('idle');
          return;
        }
        const player = new Audio(`data:audio/wav;base64,${next}`);
        player.onended = () => playNext();
        player.onerror = () => playNext();
        void player.play().catch(() => playNext());
      };

      synthesizeSpeechStream(
        text,
        (chunk) => {
          if (chunk.audio_base64) {
            queue.push(chunk.audio_base64);
            if (!playing) {
              playing = true;
              playNext();
            }
          }
          // error 帧：忽略该句，继续等待后续帧
        },
        () => {
          finished = true;
          if (!playing && queue.length === 0) setVoiceState('idle');
        },
        controller.signal,
      ).catch((err) => {
        if (controller.signal.aborted) return;
        console.error('[Pet] 朗读失败:', err);
        setVoiceState('idle');
      });
    },
    [stopSpeaking],
  );

  /** 「说话」开关：开启录音（聆听）→ 再点停止 → 识别 → 直发对话 → 朗读回复。 */
  const toggleVoice = useCallback(async () => {
    if (voiceState === 'listening') {
      const session = recordingRef.current;
      recordingRef.current = null;
      setVoiceState('thinking');
      try {
        const audio = await session?.stop();
        if (!audio) {
          setVoiceState('idle');
          return;
        }
        const result = await transcribeAudio(audio.audioBase64, audio.sampleRate);
        const text = result?.ok === true ? (result.text ?? '').trim() : '';
        if (!text) {
          setVoiceState('idle');
          return;
        }
        const data = await sendChatMessage({ message: text });
        const reply = typeof data?.clean_text === 'string' ? data.clean_text.trim() : '';
        // 表情即时驱动（发布总线 + 本窗口立即生效）；主窗口聊天页经 tick 重载历史
        pushMood(normalizeBackendMood(data?.mood));
        publishChatTick();
        if (reply) {
          speakReply(reply);
        } else {
          setVoiceState('idle');
        }
      } catch (err) {
        console.error('[Pet] 语音对话失败:', err);
        setVoiceState('idle');
      }
      return;
    }
    // 其余状态 → 开始新一轮录音（朗读中先停朗读）
    stopSpeaking();
    try {
      recordingRef.current = await startRecording();
      setVoiceState('listening');
    } catch (err) {
      console.error('[Pet] 麦克风不可用:', err);
      setVoiceState('idle');
    }
  }, [voiceState, pushMood, speakReply, stopSpeaking]);

  /** 屏幕共享开关：乐观翻转 + 热更新 vision.enabled；失败回退并留痕。 */
  const toggleScreenShare = useCallback(() => {
    const next = !(screenShare ?? false);
    setScreenShare(next);
    updateSettings({ vision: { enabled: next } })
      .then((res) => setScreenShare(res.config?.vision?.enabled ?? next))
      .catch((err) => {
        console.error('[Pet] 屏幕共享切换失败:', err);
        setScreenShare(!next);
      });
  }, [screenShare]);

  /** 操作授权开关：开启前 confirm 确认（对齐 CX-O），撤销直接生效。 */
  const toggleComputerAuth = useCallback(() => {
    void (async () => {
      try {
        const status = await fetchComputerStatus();
        const next = !status.authorized;
        if (next) {
          const confirmed = window.confirm(
            '允许 TA 查看和操作这台电脑吗？\n（开启后可随时在设置里撤销）',
          );
          if (!confirmed) return;
        }
        const updated = await setComputerAuthorized(next);
        setComputerAuth(updated.authorized);
      } catch (err) {
        console.error('[Pet] 操作授权失败:', err);
      }
    })();
  }, []);

  // ---- 桌宠本体手势：拖拽（主进程光标跟随）+ 点击弹菜单 ----
  const handleStagePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.isPrimary || e.button !== 0) return; // 仅主指针 + 左键（右键留作扩展）
    dragRef.current = createDragGestureState(e.screenX, e.screenY);
    // 主进程接管移动：启动光标跟随循环（16ms 轮询 setPosition，无渲染层 IPC 往返）
    void dragPetOverlayStart().catch(() => {
      /* 主进程不可达时静默：退化为窗口不动，不影响点击 */
    });
    // 捕获指针：移出本体（甚至窗口）仍能继续收到 move/up，拖拽不断手
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* capture 失败不影响后续手势（部分环境对已释放指针会抛错） */
    }
  };

  const handleStagePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const gesture = dragRef.current;
    if (!gesture) return;
    // 仅推进手势状态（判定点击 vs 拖拽），窗口移动由主进程循环负责——本层零 IPC
    advanceDragGesture(gesture, e.screenX, e.screenY);
  };

  const handleStagePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const gesture = dragRef.current;
    dragRef.current = null;
    void dragPetOverlayEnd().catch(() => {
      /* 主进程不可达时静默 */
    });
    if (isDragGestureClick(gesture)) {
      setMenuOpen((v) => !v); // 未拖拽 → 视为点击：弹/收菜单
    }
  };

  const handleStagePointerCancel = () => {
    dragRef.current = null; // 手势被打断：丢弃状态，不触发菜单 toggle
    void dragPetOverlayEnd().catch(() => {
      /* 主进程不可达时静默 */
    });
  };

  // ---- 点菜单外收起：根元素 pointerdown 且目标不在菜单内 → 收起 ----
  // 本体（stage）除外：本体的 pointerup toggle 自管开关——若这里也收起，
  // 「pointerdown 关 + pointerup 开」两次更新相抵消，表现为点本体永远收不起菜单。
  const handleRootPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const target = e.target as Node;
    if (menuRef.current?.contains(target)) return; // 菜单内点击：按钮 onClick 自行处理
    if (stageRef.current?.contains(target)) return; // 本体点击：由 toggle 接管
    setMenuOpen(false);
  };

  // ---- 弧形菜单项（顺序即弧上顺序：左→上→右，共六项） ----
  // 对齐 CX-O 应用控制语义（20261004_模块0_悬浮窗语音/视觉/授权闭环）：
  // 打开主窗口 / 说话（语音输入开关）/ 屏幕共享 / 操作授权 / 大小 / 关闭。
  const menuItems: PetMenuItem[] = [
    {
      key: 'open-main',
      name: '打开主窗口',
      icon: <Monitor className="pet-overlay-menu-ico" aria-hidden="true" />,
      onSelect: () => {
        void showMainWindow().catch(() => {
          /* 主进程不可达时静默 */
        });
      },
    },
    {
      key: 'talk',
      name: '说话',
      icon: <AudioLines className="pet-overlay-menu-ico" aria-hidden="true" />,
      checked: voiceState === 'listening',
      onSelect: () => {
        void toggleVoice();
      },
    },
    {
      key: 'screen',
      name: '屏幕共享',
      icon: <Eye className="pet-overlay-menu-ico" aria-hidden="true" />,
      checked: screenShare === true,
      onSelect: toggleScreenShare,
    },
    {
      key: 'auth',
      name: '操作授权',
      icon: <ShieldCheck className="pet-overlay-menu-ico" aria-hidden="true" />,
      checked: computerAuth === true,
      onSelect: toggleComputerAuth,
    },
    {
      key: 'size',
      name: '大小',
      icon: <Maximize2 className="pet-overlay-menu-ico" aria-hidden="true" />,
      slider: {
        value: size,
        min: sizeBounds.min,
        max: sizeBounds.max,
        onChange: applySize,
      },
      // 圆钮本身无动作（调节经侧挂滑块实时生效）
      onSelect: () => {},
    },
    {
      key: 'close',
      name: '关闭',
      icon: <X className="pet-overlay-menu-ico" aria-hidden="true" />,
      danger: true,
      onSelect: handleClose,
    },
  ];

  return (
    <div
      className="pet-overlay"
      data-testid="pet-overlay-root"
      data-talking={voiceState === 'speaking' ? 'true' : 'false'}
      onPointerDown={handleRootPointerDown}
    >
      <style>{PET_OVERLAY_CSS}</style>

      <div
        ref={stageRef}
        className="pet-overlay-stage"
        data-mood={mood}
        onPointerDown={handleStagePointerDown}
        onPointerMove={handleStagePointerMove}
        onPointerUp={handleStagePointerUp}
        onPointerCancel={handleStagePointerCancel}
      >
        <VrmAvatar
          key={size}
          mood={mood}
          talking={voiceState === 'speaking'}
          size={size}
          reloadKey={modelTick}
        />
      </div>

      {menuOpen ? (
        <div ref={menuRef} className="pet-overlay-menu" role="menu" aria-label="桌宠菜单">
          {menuItems.map((item, index) => {
            const progress = index / Math.max(menuItems.length - 1, 1);
            const angle = -Math.PI + progress * Math.PI; // 上半弧：左 → 上 → 右
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            const scale = Math.max(Math.min(vw / 314, vh / 366), 0.6, Math.min(vw / 314, vh / 366, 1.6));
            const btn = Math.round(44 * scale);
            const half = btn / 2;
            const cx = vw / 2;
            const cy = vh * 0.44; // 弧心锚定桌宠上半身
            const rx = Math.max((cx - half - 8) * 0.92, 40);
            const ry = Math.max(cy - half - 8, 40);
            const x = cx + Math.cos(angle) * rx;
            const y = cy + Math.sin(angle) * ry;
            return (
              <button
                key={item.key}
                type="button"
                aria-label={item.name}
                aria-pressed={item.checked}
                className={`pet-overlay-menu-btn group${item.danger ? ' pet-overlay-close' : ''}`}
                data-active={item.checked ? 'true' : 'false'}
                style={{
                  left: x - half,
                  top: y - half,
                  width: btn,
                  height: btn,
                  animationDelay: `${index * 25}ms`,
                }}
                onClick={item.onSelect}
              >
                {item.icon ?? (
                  <span className="pet-overlay-menu-char" style={{ fontSize: Math.round(15 * scale) }}>
                    {item.labelText}
                  </span>
                )}
                {item.slider && (() => {
                  // 滑块胶囊挂在圆钮正下方（左挂会压住弧上相邻按钮），JS 计算位置并 clamp 到窗口内
                  const pillW = Math.round(176 * scale);
                  const pillLeft = Math.max(6, Math.min(x - pillW / 2, vw - pillW - 6));
                  const pillTop = y + half + 6;
                  return (
                    <span
                      className="pet-overlay-slider"
                      style={{ left: pillLeft, top: pillTop, width: pillW }}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <input
                        aria-label={`${item.name}滑块`}
                        type="range"
                        min={item.slider.min}
                        max={item.slider.max}
                        step="any"
                        value={item.slider.value}
                        onChange={(event) => item.slider?.onChange(Number(event.target.value))}
                      />
                      <span className="pet-overlay-slider-value">{item.slider.value}px</span>
                    </span>
                  );
                })()}
                <span className="pet-overlay-menu-label">{item.name}</span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

const PET_OVERLAY_CSS = `
.pet-overlay {
  position: fixed;
  inset: 0;
  background: transparent;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px;
  user-select: none;
  font-family: 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
}
/* 桌宠本体：交互手势承载层（拖拽 / 点击弹菜单）。
   纯 pointer 事件实现（屏幕坐标增量），不用 CSS 拖拽区域标记（会吞 mousedown/click）。 */
.pet-overlay-stage {
  pointer-events: auto;
  cursor: grab;
  /* 交给 pointer 事件处理，避免浏览器把按住滑动当触摸滚动吞掉 move 序列 */
  touch-action: none;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.pet-overlay-stage:active {
  cursor: grabbing;
}
/* 弧形气泡菜单容器：铺满窗口的定位层，本身不拦截事件（圆钮各自 auto） */
.pet-overlay-menu {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 5;
}
/* 圆形玻璃按钮（沿上半弧排布，CX-O PetContextMenu 同语言）：
   开关项（data-active=true）主色填充；hover 放大 + 侧标签浮出；逐个错峰入场 */
.pet-overlay-menu-btn {
  pointer-events: auto;
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.55);
  background: rgba(255, 255, 255, 0.62);
  backdrop-filter: blur(12px) saturate(1.4);
  color: #5c5c70;
  cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 4px 14px rgba(255, 145, 210, 0.28);
  transition: transform 120ms ease-out, background 150ms ease-out, border-color 150ms ease-out;
  animation: pet-overlay-bubble-in 200ms var(--ease-spring, cubic-bezier(0.34, 1.56, 0.64, 1)) both;
}
.pet-overlay-menu-btn:hover {
  transform: scale(1.12);
  border-color: rgba(255, 145, 210, 0.6);
  background: rgba(255, 214, 236, 0.55);
}
.pet-overlay-menu-btn[data-active='true'] {
  background: linear-gradient(135deg, rgba(255, 123, 186, 0.85), rgba(157, 124, 255, 0.85));
  border-color: rgba(255, 158, 207, 0.9);
  color: #fff;
}
.pet-overlay-menu-btn.pet-overlay-close {
  background: rgba(240, 120, 170, 0.68);
  border-color: rgba(240, 120, 170, 0.85);
  color: #fff;
}
.pet-overlay-menu-ico {
  width: 40%;
  height: 40%;
}
.pet-overlay-menu-char {
  font-weight: 600;
  line-height: 1;
  pointer-events: none;
}
/* hover 侧标签：圆钮右侧浮出的文字胶囊 */
.pet-overlay-menu-label {
  pointer-events: none;
  position: absolute;
  left: calc(100% + 6px);
  top: 50%;
  transform: translateY(-50%);
  white-space: nowrap;
  font-size: 10px;
  line-height: 1;
  padding: 4px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.72);
  color: #5c5c70;
  opacity: 0;
  transition: opacity 120ms ease-out;
}
.pet-overlay-menu-btn:hover .pet-overlay-menu-label {
  opacity: 1;
}
@keyframes pet-overlay-bubble-in {
  from { opacity: 0; transform: scale(0.5); }
  to { opacity: 1; transform: scale(1); }
}
/* 大小滑块胶囊：挂在"大小"圆钮正下方（位置由 JS 计算并 clamp 到窗口内，
   左挂会压住弧上相邻按钮）；pointer-events 已随圆钮 auto，pointerdown/click
   stopPropagation 防误触拖拽/收菜单 */
.pet-overlay-slider {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(12px) saturate(1.4);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 4px 14px rgba(255, 145, 210, 0.25);
}
.pet-overlay-slider input[type='range'] {
  width: 110px;
  accent-color: var(--color-primary, #ff7bba);
  cursor: pointer;
}
.pet-overlay-slider-value {
  min-width: 38px;
  text-align: right;
  font-size: 10px;
  line-height: 1;
  color: #5c5c70;
  font-variant-numeric: tabular-nums;
}
`;
