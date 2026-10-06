import React, { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { GLTFLoader, type GLTF } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils, type VRM, type VRMExpressionManager } from '@pixiv/three-vrm';
import { type PetMood } from '../petMood';
import { fetchPetModelBuffer } from '../api';

/**
 * VrmAvatar — 真实 VRM 桌宠渲染（three + @pixiv/three-vrm）。
 *
 * 两个调用点：PetOverlay（悬浮窗，286px）与 PetPage（页内预览，330px）。
 * 失败态给中文提示、不回落任何替代形象——早期那个 CSS 卡通形象组件（PetAvatar）
 * 已按设计对齐要求整体删除（见 .trae/documents/20260925_模块0_清理不合设计风格元素.md）。
 *
 * ============ 为何走 HTTP（GET /api/pet/model）取模型而不 file:// 直读 ============
 * Chromium 对 file:// 页面发起的 fetch/XHR 默认拦截本地文件读取（webSecurity 默认开启），
 * 打包态（Electron loadFile dist/*.html）下直读会失败成白屏；而走既有 127.0.0.1:8600
 * 通道后，开发态（浏览器 dev）与打包态完全同一条路径，且与项目「资产归 data/、由后端
 * 服务」的约定一致。模型字节由后端按 Content-Type: model/gltf-binary 原样回传。
 *
 * ============ 透明背景 ============
 * 悬浮窗必须透明：WebGLRenderer 以 { alpha: true } 创建，并 setClearAlpha(0)，
 * 使 canvas 的空白区域完全透明（不出现黑底），配合 BrowserWindow transparent。
 *
 * ============ 兼容 VRM 0.x / 1.x 朝向 ============
 * VRM 0.x 模型面朝 -Z，VRM 1.x 面朝 +Z。读 vrm.meta.metaVersion，为 '0' 时经
 * VRMUtils.rotateVRM0()（等价于 vrm.scene.rotation.y = Math.PI）转到与 1.x 一致朝向。
 *
 * ============ 站姿（T-pose → 自然垂臂） ============
 * VRM 文件只含骨骼绑定、不含姿势动画，直接渲染即 T-pose（双臂平举），观感僵直。
 * 加载后经 applyIdlePose() 在归一化骨骼空间把上臂放到身侧并轻弯肘腕，形成自然站姿。
 *
 * ============ 相机自适应 ============
 * 不同模型身高 / 缩放差异极大，禁止写死单一距离：加载后对 vrm.scene 取包围盒，
 * 依实际高度与相机 FOV 反算取景距离，保证「任何模型都完整可见」。
 *
 * ============ 自动曝光 ============
 * 不同模型材质亮度差异极大：旧模型（CX-OPEN）偏暗需强光硬拉，而材质亮度正常的
 * 模型（CX-A_S 及任意用户导入的 .vrm）在同等光照下必然过曝死白——写死光照参数
 * 只能适配单一模型。故在模型与相机就位后渲染一帧做亮度采样（autoExpose）：
 * 偏亮 → 调低 toneMappingExposure，偏暗 → 调高，clamp 到 [0.5, 2.0]、最多
 * EXPOSURE_MAX_ITER 次迭代；判定逻辑为纯函数 nextExposureStep（单测直接覆盖）。
 * 配合 Khronos PBR Neutral 色调映射（THREE.NeutralToneMapping，只对超出白点的
 * 高光做柔和滚降、不改中间调色彩与饱和度，比 ACES 更适合动漫 MToon 渲染）兜底
 * 高光边缘。灯光基准相应回调至接近 @pixiv/three-vrm 官方口径（单灯 π≈3.14）。
 *
 * ============ 每帧更新 ============
 * 表情 / 看向 / 弹簧骨均依赖 vrm.update(delta)，必须每帧调用（用 THREE.Clock 求 delta）。
 *
 * ============ 兜底与可观测 ============
 * 整个初始化包在 try/catch：无 WebGL / 接口失败 / 解析失败 → data-vrm-state='unsupported'
 * 并在原位给出一块**中文「暂时显示不了 3D 桌宠」提示**（含可读原因），绝不把异常抛到
 * React 边界（不白屏、不崩溃），也**不再回落卡通形象**——回落卡通会让人误以为「根本没做 VRM」。
 * 取模型字节带**退避重试**（见 loadModelBufferWithRetry）：Electron 启动链是
 * 「先建窗口、后端并行拉起」，渲染进程可能早于后端就绪发起请求，只试一次会永久
 * 停在失败态上。根节点 data-vrm-state 取值 'loading' | 'ready' | 'unsupported'，供 E2E 断言。
 *
 * ============ 释放 ============
 * 甄浮窗会被反复开关，卸载时必须 cancelAnimationFrame、停 clock、abort 加载中的 fetch、
 * renderer.dispose() / forceContextLoss() 并移除 canvas、VRMUtils.deepDispose(vrm.scene)，
 * 否则 GPU 资源与连接会累积泄漏。
 */

/** 渲染状态：loading 加载中 / ready 已就绪（canvas） / unsupported 当前环境或资源不支持（给提示，不回落卡通） */
export type VrmState = 'loading' | 'ready' | 'unsupported';

/**
 * 失败原因的可读文案（按失败阶段区分，便于用户判断是环境问题还是资源问题）。
 * 导出供单测直接覆盖三档映射——这是用户在失败态唯一能读到的信息。
 */
export const REASON_BY_STAGE = {
  renderer: '当前环境不支持 3D 显示（显卡 / WebGL 不可用）',
  model: '没取到桌宠模型文件（模型缺失，或本地服务还没就绪）',
  parse: '桌宠模型解析失败（文件可能不是有效的 VRM）',
} as const;

type FailStage = keyof typeof REASON_BY_STAGE;

interface VrmAvatarProps {
  mood: PetMood;
  /** 是否处于说话状态（触发口型开合） */
  talking: boolean;
  /** 画面宽度（px），高度按 1.05 比例跟随 */
  size?: number;
  /**
   * 模型代际计数（可选，默认 0）：变化时作废模块级缓存并重新拉取模型
   * （更换桌宠模型后立即生效；PetPage / PetOverlay 经 petModelReload 总线递增）。
   */
  reloadKey?: number;
}

/**
 * 心情 → VRM 预设表情名映射（纯函数，供单测直接覆盖）。
 *
 * happy→happy、calm→neutral、sad→sad、surprised→surprised、
 * shy→relaxed（另有腮红候选表情叠加，见 BLUSH_CANDIDATES）、sleepy→relaxed（眨眼放慢）。
 * 表情名在各模型可能缺失，实际写入时由 setExpressionValue 先查 expressionManager 静默降级。
 */
export function vrmExpressionForMood(mood: PetMood): string | null {
  switch (mood) {
    case 'happy':
      return 'happy';
    case 'calm':
      return 'neutral';
    case 'sad':
      return 'sad';
    case 'surprised':
      return 'surprised';
    case 'shy':
      return 'relaxed';
    case 'sleepy':
      return 'relaxed';
    default:
      return null;
  }
}

/** 腮红类表情的候选名（预设表里没有标准名，模型自建命名不一，逐个探测命中即用） */
const BLUSH_CANDIDATES = ['blush', 'Blush', 'cheek', 'Cheek', 'cheekColor'];

/** 取模型的退避重试次数与间隔（见 loadModelBufferWithRetry 的说明） */
const LOAD_ATTEMPTS = 6;
const LOAD_RETRY_DELAY_MS = 1500;

/** 自动曝光的迭代上限（见 autoExpose：防极端贴图模型下曝光调整震荡发散） */
const EXPOSURE_MAX_ITER = 6;
/** 自动曝光的曝光量边界（见 nextExposureStep：既不许过暗也不许无限提亮） */
const EXPOSURE_MIN = 0.5;
const EXPOSURE_MAX = 2.0;

/**
 * 模型字节的模块级缓存（约 15MB）。
 *
 * 为什么缓存：尺寸档位切换（PetOverlay 以 key={size} 重挂载本组件）与悬浮窗反复
 * 开关会多次消费同一份 VRM 字节，逐次走 HTTP 会有可感知的 loading 间隙；缓存命中后
 * 重挂载零请求、零等待（GN-004 F2）。缓存保存**原始字节原件**，永不直接外借——
 * parse 前统一传 slice(0) 副本（见 loadModelBufferWithRetry / init 内说明）。
 * 注意：主窗口与悬浮窗是两个渲染进程，各自持有独立缓存实例，互不影响。
 */
let modelBufferCache: ArrayBuffer | null = null;

/**
 * 缓存对应的模型代际（reloadKey）：换模型后旧代际字节必须作废——缓存命中
 * 需同时满足「有缓存」且「代际一致」，否则丢弃旧缓存重新拉取。
 */
let modelBufferCacheGen: number | null = null;

/** 可被 abort 打断的等待：中止时立即返回，避免卸载后仍挂着定时器。 */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * 取模型字节（模块缓存优先，未命中才走带退避重试的 HTTP）。
 *
 * 为什么要重试：Electron 启动链是「先建窗口、后端并行拉起」，渲染进程可能在
 * 后端 /api/health 就绪前就发起请求——若只尝试一次，用户会永久停在「显示不了」
 * 提示上（必须手动关开悬浮窗才能恢复），这正是「界面看起来不正常」的成因之一。
 * 这里按 LOAD_ATTEMPTS 次、每次间隔 LOAD_RETRY_DELAY_MS 重试；卸载（signal 中止）
 * 时立即结束，不产生悬挂定时器。全部失败才抛错，由外层给出中文不支持提示。
 *
 * 缓存语义：命中直接返回原件（调用方 parse 时自行 slice 副本）；未命中且 fetch
 * 成功后写入缓存。同一 ArrayBuffer 可被多次消费——three 的 GLTFLoader.parse 不会
 * transfer/detach 输入 buffer，但保守起见 parse 统一传副本，缓存原件零风险。
 */
async function loadModelBufferWithRetry(signal: AbortSignal, gen: number): Promise<ArrayBuffer> {
  if (modelBufferCache && modelBufferCacheGen === gen) return modelBufferCache;
  // 代际不匹配（模型被更换）→ 先作废旧缓存再拉取新模型
  modelBufferCache = null;
  modelBufferCacheGen = null;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < LOAD_ATTEMPTS; attempt += 1) {
    if (signal.aborted) break;
    try {
      const buffer = await fetchPetModelBuffer(signal);
      modelBufferCache = buffer; // fetch 成功 → 写入模块级缓存，后续挂载零请求
      modelBufferCacheGen = gen;
      return buffer;
    } catch (err) {
      lastError = err;
      if (signal.aborted) break;
      // 最后一次失败后不再等待，直接抛出（由外层给出「暂时显示不了」提示）
      if (attempt < LOAD_ATTEMPTS - 1) await delay(LOAD_RETRY_DELAY_MS, signal);
    }
  }
  throw lastError instanceof Error ? lastError : new Error('取桌宠模型失败');
}

/** 在模型的表情表中找到第一个存在的候选名；都没有则返回 null。 */
function findFirstExpression(em: VRMExpressionManager, names: readonly string[]): string | null {
  for (const name of names) {
    if (em.getExpression(name)) return name;
  }
  return null;
}

/**
 * 施加自然站姿（idle pose）——把 VRM 默认的 T-pose 放成垂臂站姿。
 *
 * 为什么必须做：VRM 文件本身只带骨骼绑定，不含姿势动画；直接渲染就是双臂平举的
 * T-pose（业内称 A/T-pose），在桌宠场景里看起来「僵直、不像活人」——这是本组件
 * 首版观感不正常的直接原因。
 *
 * 做法：在归一化骨骼空间（three-vrm humanoid normalized bones）里把上臂绕 Z 轴
 * 放下至身侧（左臂 -70°、右臂 +70°，方向由模型坐标约定决定：左臂初始朝 +X，
 * 绕 Z 负向旋转即落到 -Y 下方），肘部与手腕各给极小弯曲避免僵直。
 * 非标准人形 / 缺骨骼时静默跳过（不抛错）。
 */
function applyIdlePose(vrm: VRM): void {
  const humanoid = vrm.humanoid;
  if (!humanoid) return;
  const deg = Math.PI / 180;
  /** 安全设置归一化骨骼欧拉旋转（骨骼缺失时静默忽略） */
  const setBone = (name: string, xDeg: number, yDeg: number, zDeg: number): void => {
    try {
      const node = humanoid.getNormalizedBoneNode(name as never);
      if (node) node.rotation.set(xDeg * deg, yDeg * deg, zDeg * deg);
    } catch {
      /* 该骨骼名不被此模型支持时忽略 */
    }
  };
  // 上臂放下贴至身侧（T-pose → 自然垂臂）
  // 符号经实机截图迭代确定：该模型归一化骨骼下左臂 +Z、右臂 -Z 为「向外下方转」，
  // 反向会把手举过头顶（试过 -Z/+Z，实拍为举手，故取此号）。
  setBone('leftUpperArm', 0, 0, 70);
  setBone('rightUpperArm', 0, 0, -70);
  // 肘部轻微内弯，避免整条手臂像根棍子
  setBone('leftLowerArm', 0, 12, 6);
  setBone('rightLowerArm', 0, -12, -6);
  // 手腕极小幅内收
  setBone('leftHand', 0, 0, 6);
  setBone('rightHand', 0, 0, -6);
}

/**
 * 安全写入表情权重：模型缺失该表情时静默跳过（不抛错、不打断动画循环）。
 * VRMExpressionManager.setValue 对未注册名只会告警，但仍统一走这里以保证兜底语义。
 */
function setExpressionValue(
  em: VRMExpressionManager | undefined,
  name: string,
  value: number,
): void {
  if (!em) return;
  if (!em.getExpression(name)) return; // 缺失 → 静默降级
  try {
    em.setValue(name, value);
  } catch {
    /* 单帧写入异常不应中断渲染循环 */
  }
}

/**
 * 自动曝光的单步判定（纯函数，供单测直接覆盖）。
 *
 * 输入为一次渲染帧的亮度统计（sRGB 0~255 口径）：
 * - mean：模型像素（透明背景除外）的平均亮度；
 * - overRatio：死白像素（亮度 > 250）占比——漫反射高光被 clamp 成纯白的直接信号。
 *
 * 判定：偏亮（mean > 190 或 overRatio > 0.10）→ 曝光 ×0.85；偏暗（mean < 70）→
 * 曝光 ×1.15；正常区间 → 返回 null（收敛停止）。结果 clamp 到 [EXPOSURE_MIN, EXPOSURE_MAX]。
 */
export function nextExposureStep(
  mean: number,
  overRatio: number,
  exposure: number,
): number | null {
  if (overRatio > 0.1 || mean > 190) {
    return Math.max(EXPOSURE_MIN, exposure * 0.85);
  }
  if (mean < 70) {
    return Math.min(EXPOSURE_MAX, exposure * 1.15);
  }
  return null;
}

/**
 * 自动曝光：模型与相机就位后，同步渲染一帧并降采样统计亮度，按 nextExposureStep
 * 迭代调整 toneMappingExposure，直至落进正常区间或达到 EXPOSURE_MAX_ITER 次上限。
 *
 * 为什么在启动动画循环前同步做：曝光只取决于「灯光 × 材质」，加载后算一次即可；
 * 每帧重算既浪费又可能在透明背景占比波动时引起亮度跳动。采样经 drawImage 把
 * WebGL 画布拷到 2D canvas 再 getImageData（64×64）——在同一同步流程内 render 后
 * 立即拷贝无需 preserveDrawingBuffer。取不到 2D 上下文 / 画面全透明（相机没框住
 * 模型）时静默放弃，保持曝光基准 1.0。
 */
function autoExpose(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
): void {
  const probe = document.createElement('canvas');
  probe.width = 64;
  probe.height = 64;
  const pctx = probe.getContext('2d', { willReadFrequently: true });
  if (!pctx) return;
  for (let i = 0; i < EXPOSURE_MAX_ITER; i += 1) {
    renderer.render(scene, camera);
    pctx.drawImage(renderer.domElement, 0, 0, probe.width, probe.height);
    const { data } = pctx.getImageData(0, 0, probe.width, probe.height);
    let sum = 0;
    let count = 0;
    let over = 0;
    for (let p = 0; p < data.length; p += 4) {
      if (data[p + 3] < 16) continue; // 透明背景（清屏 alpha=0）不参与统计
      const lum = 0.2126 * data[p] + 0.7152 * data[p + 1] + 0.0722 * data[p + 2];
      sum += lum;
      count += 1;
      if (lum > 250) over += 1;
    }
    if (!count) return; // 全透明：模型未入画，不做亮度判断
    const next = nextExposureStep(sum / count, over / count, renderer.toneMappingExposure);
    if (next === null) return; // 已收敛到正常区间
    renderer.toneMappingExposure = next;
  }
}

export default function VrmAvatar({ mood, talking, size = 220, reloadKey = 0 }: VrmAvatarProps) {
  const [state, setState] = useState<VrmState>('loading');
  /** 不支持态的原因文案（按失败阶段给出中文说明） */
  const [failReason, setFailReason] = useState<string>('');
  const hostRef = useRef<HTMLDivElement | null>(null);
  const vrmRef = useRef<VRM | null>(null);
  const moodRef = useRef<PetMood>(mood);
  const talkingRef = useRef<boolean>(talking);
  const prevExprRef = useRef<string | null>(null);

  // ---- 初始化：创建渲染器 → 取模型 → 解析 → 自适应相机 → 启动动画循环 ----
  // 依赖为 [reloadKey]：模型代际变化（更换桌宠模型）时清理旧渲染并按新代际重新拉取；
  // 其余重挂载（PetOverlay 换尺寸档位经 key={size}）命中模块级缓存（代际一致），零请求零等待。
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const width = size;
    const height = Math.round(size * 1.05);

    let disposed = false;
    let rafId = 0;
    let renderer: THREE.WebGLRenderer | null = null;
    let scene: THREE.Scene | null = null;
    let camera: THREE.PerspectiveCamera | null = null;
    let vrm: VRM | null = null;
    let baseY = 0;
    // 失败阶段：用于给出可读的中文原因（环境不支持 / 资源取不到 / 解析失败）
    let stage: FailStage = 'renderer';
    const clock = new THREE.Clock();
    const aborter = new AbortController();

    // 眨眼状态机：blinkStart<0 表示不在眨眼中；nextBlinkAt 为下一次眨眼时刻（秒）
    let blinkStart = -1;
    let nextBlinkAt = 1.2 + Math.random() * 2.5;

    /** 释放全部 GPU / DOM / 定时资源（幂等）。 */
    function disposeAll(): void {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
      }
      if (vrm) {
        try {
          VRMUtils.deepDispose(vrm.scene);
        } catch {
          /* 释放异常不阻断卸载 */
        }
        vrmRef.current = null;
        vrm = null;
      }
      if (renderer) {
        try {
          renderer.forceContextLoss();
        } catch {
          /* 部分环境下 context 已丢失 */
        }
        renderer.dispose();
        renderer.domElement.remove();
        renderer = null;
      }
      scene = null;
      camera = null;
    }

    const init = async (): Promise<void> => {
      try {
        // 透明背景：alpha + 清屏 alpha=0（悬浮窗不出现黑底）
        renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
        renderer.setClearAlpha(0);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        renderer.outputColorSpace = THREE.SRGBColorSpace;
        // PBR Neutral 色调映射：只压超出白点的高光、不改中间调色彩（动漫渲染不降饱和），
        // 与 autoExpose 配合兜底高光死白；曝光基准 1.0，实际值由 autoExpose 收敛。
        renderer.toneMapping = THREE.NeutralToneMapping;
        renderer.toneMappingExposure = 1.0;
        renderer.setSize(width, height);
        const canvas = renderer.domElement;
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';
        host.appendChild(canvas);

        scene = new THREE.Scene();
        // 30° 视角 + 自适应距离，兼顾「完整可见」与「面部清晰」
        camera = new THREE.PerspectiveCamera(30, width / height, 0.01, 200);

        // 光照：three r155+ 起采用**物理光照单位**（useLegacyLights 已移除），旧的
        // 低强度值（≈1）会渲染得明显发灰发暗。但历史值（2.4/2.6/1.2）是为救旧模型
        // 「偏暗」拉满的，对材质亮度正常的模型（CX-A_S 及任意用户导入模型）必然
        // 过曝死白。现回调到接近 @pixiv/three-vrm 官方口径（单灯 π≈3.14）的温和
        // 三点布光基准；最终整体亮度由 autoExpose 按模型实际渲染结果自动收敛。
        scene.add(new THREE.AmbientLight(0xffffff, 1.0));
        const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
        keyLight.position.set(0.6, 1.4, 1.6);
        scene.add(keyLight);
        const fillLight = new THREE.DirectionalLight(0xffffff, 0.9);
        fillLight.position.set(-0.9, 0.8, 1.4);
        scene.add(fillLight);

        // 取模型字节（HTTP，带 X-Client-Token，带退避重试以跨过后端启动窗口期；
        // 传入当前代际 reloadKey——代际变化时旧缓存作废、重新拉取新模型）
        stage = 'model';
        const buffer = await loadModelBufferWithRetry(aborter.signal, reloadKey);
        if (disposed) return;

        stage = 'parse';
        const loader = new GLTFLoader();
        loader.register((parser) => new VRMLoaderPlugin(parser));
        // parse 传 slice(0) 副本：缓存原件保持完好，可被后续档位切换 / 重挂载反复消费
        const gltf = await new Promise<GLTF>((resolve, reject) => {
          loader.parse(buffer.slice(0), '', resolve, (event) =>
            reject(new Error(event?.message ? `VRM 解析失败：${event.message}` : 'VRM 解析失败')),
          );
        });
        if (disposed) return;

        const loaded = gltf.userData.vrm as VRM | undefined;
        if (!loaded) throw new Error('模型未包含 VRM 数据（可能不是有效的 .vrm 文件）');
        vrm = loaded;

        // VRM0 面朝 -Z → 转 180° 对齐 VRM1（相机在 +Z 侧看正面）
        if (vrm.meta.metaVersion === '0') {
          VRMUtils.rotateVRM0(vrm);
        }
        scene.add(vrm.scene);
        baseY = vrm.scene.position.y;

        // T-pose → 自然垂臂站姿（必须在取包围盒之前：姿势会改变整体尺寸）
        applyIdlePose(vrm);

        // 自适应相机：按包围盒实际尺寸反算距离，兼容不同身高 / 缩放
        const box = new THREE.Box3().setFromObject(vrm.scene);
        const boxSize = new THREE.Vector3();
        const boxCenter = new THREE.Vector3();
        box.getSize(boxSize);
        box.getCenter(boxCenter);
        const modelHeight = Math.max(boxSize.y, 0.2);
        const fovRad = (camera.fov * Math.PI) / 180;
        // 取景系数：d = k·H / tan(fov/2) ⇒ 模型占画面高度比 = 1/(2k)。
        // k=0.59 → 约占 85%，桌宠场景希望角色「有存在感」而非远远站着。
        const distance = ((modelHeight * 0.56) * 1.05) / Math.tan(fovRad / 2);
        camera.position.set(
          boxCenter.x,
          boxCenter.y + modelHeight * 0.05,
          boxCenter.z + distance,
        );
        camera.lookAt(
          boxCenter.x,
          boxCenter.y + modelHeight * 0.01,
          boxCenter.z,
        );
        camera.updateProjectionMatrix();

        // 自动曝光：按本模型实际渲染亮度收敛 toneMappingExposure（过亮压 / 过暗提，
        // 见 autoExpose 说明）——任意材质亮度的模型都能落进正常观感区间
        autoExpose(renderer, scene, camera);

        vrmRef.current = vrm;
        if (disposed) {
          disposeAll();
          return;
        }
        setState('ready');

        const tick = (): void => {
          if (disposed) return;
          rafId = requestAnimationFrame(tick);
          const delta = clock.getDelta();
          const t = clock.elapsedTime;
          const em = vrm?.expressionManager;
          const sleepy = moodRef.current === 'sleepy';

          // idle：缓慢呼吸起伏 + 极小幅左右摆动（幅度克制，避免抽搐观感）
          if (vrm) {
            vrm.scene.position.y = baseY + Math.sin(t * 1.6) * 0.006;
            vrm.scene.rotation.z = Math.sin(t * 0.9) * 0.008;
          }

          // 眨眼：随机间隔 0→1→0；sleepy 档放慢（间隔更长）
          if (blinkStart < 0 && t >= nextBlinkAt) {
            blinkStart = t;
          }
          if (blinkStart >= 0) {
            const duration = 0.16;
            const d = t - blinkStart;
            let weight: number;
            if (d < duration / 2) {
              weight = d / (duration / 2);
            } else if (d < duration) {
              weight = 1 - (d - duration / 2) / (duration / 2);
            } else {
              weight = 0;
              blinkStart = -1;
              nextBlinkAt = t + (sleepy ? 5 + Math.random() * 4 : 1.8 + Math.random() * 3);
            }
            setExpressionValue(em, 'blink', weight);
          }

          // 说话：正弦驱动口型 aa（缺失时 setExpressionValue 静默跳过）
          if (talkingRef.current) {
            setExpressionValue(em, 'aa', (Math.sin(t * 14) * 0.5 + 0.5) * 0.8);
          } else {
            setExpressionValue(em, 'aa', 0);
          }

          // 每帧更新 VRM（表情 / 看向 / 弹簧骨依赖于此）
          vrm?.update(delta);
          if (renderer && scene && camera) {
            renderer.render(scene, camera);
          }
        };
        tick();
      } catch {
        // 无 WebGL / 接口失败 / 解析失败 → 给「暂时显示不了 3D 桌宠」提示（不回落卡通），
        // 绝不抛到 React 边界（不白屏、不崩溃）。
        if (!disposed) {
          setFailReason(REASON_BY_STAGE[stage]);
          setState('unsupported');
        }
        disposeAll();
      }
    };

    void init();

    return () => {
      disposed = true;
      aborter.abort(); // 取消仍在加载中的 fetch（15MB），避免反复开关累积连接
      disposeAll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  // 心情联动：写入 VRM 表情（缺失静默降级），并清掉上一档权重
  useEffect(() => {
    moodRef.current = mood;
    const em = vrmRef.current?.expressionManager;
    if (!em || state !== 'ready') return;
    const name = vrmExpressionForMood(mood);
    const prev = prevExprRef.current;
    if (prev && prev !== name) setExpressionValue(em, prev, 0);
    if (name) setExpressionValue(em, name, 1);
    prevExprRef.current = name;
    // shy 叠加腮红类表情（模型有才用）
    const blush = findFirstExpression(em, BLUSH_CANDIDATES);
    if (blush) setExpressionValue(em, blush, mood === 'shy' ? 0.6 : 0);
  }, [mood, state]);

  // 说话状态同步进 ref，供动画循环读取最新值
  useEffect(() => {
    talkingRef.current = talking;
  }, [talking]);

  return (
    <div
      className="cx-vrm"
      data-vrm-state={state}
      style={{ position: 'relative', width: size, height: Math.round(size * 1.05) }}
    >
      <style>{VRM_CSS}</style>
      <div
        ref={hostRef}
        className="cx-vrm-host"
        style={{ position: 'absolute', inset: 0, display: state === 'unsupported' ? 'none' : 'block' }}
      />
      {state === 'loading' ? <span className="cx-vrm-loading" aria-hidden="true" /> : null}
      {state === 'unsupported' ? (
        <div className="cx-vrm-unsupported" role="status">
          <p className="cx-vrm-unsupported-title">暂时显示不了 3D 桌宠</p>
          <p className="cx-vrm-unsupported-desc">{failReason || '当前环境不支持 3D 显示'}</p>
        </div>
      ) : null}
    </div>
  );
}

const VRM_CSS = `
.cx-vrm {
  display: grid;
  place-items: center;
  overflow: hidden;
}
.cx-vrm-host {
  pointer-events: none;
}
.cx-vrm-loading {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 34px;
  height: 34px;
  margin: -17px 0 0 -17px;
  border-radius: 50%;
  border: 3px solid rgba(255, 176, 222, 0.45);
  border-top-color: rgba(255, 120, 185, 0.95);
  animation: cx-vrm-spin 0.9s linear infinite;
  pointer-events: none;
}
@keyframes cx-vrm-spin {
  to { transform: rotate(360deg); }
}
/* 不支持态提示卡：玻璃拟态小面板，与悬浮窗按钮同一视觉语言。
   用 inset:12px 而非铺满，保证 286px 悬浮窗里文字不贴边、读得清。 */
.cx-vrm-unsupported {
  position: absolute;
  inset: 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 14px;
  border-radius: 18px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.5);
  backdrop-filter: blur(12px) saturate(1.4);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 4px 16px rgba(255, 145, 210, 0.2);
  text-align: center;
  pointer-events: none;
  font-family: 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
}
.cx-vrm-unsupported-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: #5c5c70;
}
.cx-vrm-unsupported-desc {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: rgba(124, 124, 150, 0.9);
}
`;