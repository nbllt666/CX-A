import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { getAppInfo } from './bridge';
import type { AppInfo } from './bridge';
import { fetchSetupStatus } from './api';
import type { SetupStatusView } from './api';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import ChatPage from './pages/ChatPage';
import PetPage from './pages/PetPage';
import MemoriesPage from './pages/MemoriesPage';
import SettingsPage from './pages/SettingsPage';
import SetupWizard from './pages/SetupWizard';
import FleetPage from './pages/FleetPage';
import { usePetEnabled } from './hooks/usePetEnabled';

/**
 * 主界面视图。
 *
 * 管理面（CX-A 管理 CX-O 实例群）按 20261004 决策补齐前端：`fleet` 视图存在，
 * 但**无任何可见入口**——唯一进入方式为连点侧栏 logo 5 次（隐藏入口）或深链
 * `#/fleet`；治理 API 仍经 /api/fleet/* 外露（spec add-fleet-frontend-hidden，
 * 解除 20260826_模块0_差异审查登记与处理计划.md 中「前端不放管理页」旧边界）。
 *
 * `setup` = 首启向导（首次启动覆盖主界面，或从设置页 / `#/setup` 深链重入）。
 */
export type View = 'chat' | 'pet' | 'memories' | 'settings' | 'setup' | 'fleet';

const VIEWS: View[] = ['chat', 'pet', 'memories', 'settings', 'setup', 'fleet'];

/**
 * 首启门控状态：
 * - `checking`：首帧等待接口，渲染轻量 loading（不白屏）；
 * - `wizard`：后端明确要求初始化 → 向导覆盖主界面（不渲染 TopBar / Sidebar / 主视图）；
 * - `main`：正常渲染主界面（后端说已完成，或接口不可达时本次会话降级放行）。
 *
 * 门控真相唯一来源是后端 `/api/setup/status`：**不持久化「已跳过向导」标记**。
 * 早期版本把「接口不可达」写进 localStorage 并据此短路门控，导致首启时后端
 * 稍慢一拍（或端口被占）就会让向导**永久不再出现**；降级放行只应作用于
 * 本次会话，下次启动必须重新询问后端。
 */
type SetupGate = 'checking' | 'wizard' | 'main';

interface RouterValue {
  view: View;
  appInfo: AppInfo | null;
  /** 在主界面内切换视图（hash 路由，不重启窗口） */
  navigate: (view: View) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

export function useRouter(): RouterValue {
  const ctx = useContext(RouterContext);
  if (!ctx) throw new Error('useRouter 需要在 Router 作用域内使用');
  return ctx;
}

/**
 * 非抛错版路由读取：脱离 Router 作用域时返回 null。
 *
 * 供「既可能在应用内、也可能被组件级单测直接裸渲染」的页面使用
 * （如设置页的向导入口——既有 settingspage.test.tsx 裸渲染 SettingsPage，
 * 若直接调用 useRouter 会因无 Provider 抛错并击穿既有测试）。
 */
export function useRouterOptional(): RouterValue | null {
  return useContext(RouterContext);
}

/** 从 hash 解析出主界面视图；非法回退到 /chat */
function parseHash(hash: string): View {
  const clean = hash.replace(/^#\/?/, '').split('.')[0] as View;
  return VIEWS.includes(clean) ? clean : 'chat';
}

function viewToHash(view: View): string {
  return `#/${view}`;
}

export default function App() {
  const [view, setView] = useState<View>(() => parseHash(window.location.hash));
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  // 首帧门控：每次启动都询问后端（不持久化跳过标记；显式 `#/setup` 深链可在主界面内进向导）
  const [gate, setGate] = useState<SetupGate>('checking');
  // 门控拿到的状态：透传给向导作为默认选择，避免重复请求
  const [setupStatus, setSetupStatus] = useState<SetupStatusView | null>(null);

  // 读取应用信息（electron 下走桥，浏览器下走 mock）
  useEffect(() => {
    let alive = true;
    getAppInfo()
      .then((info) => {
        if (alive) setAppInfo(info);
      })
      .catch(() => {
        // D4 修复：桥不可用 / IPC 异常时降级——appInfo 保持 null（TopBar 自动隐藏版本徽标），
        // 不产生 unhandled rejection
        if (alive) setAppInfo(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 首启门控：问一次「要不要初始化」
  //   wizard_required → 覆盖主界面展示向导；
  //   其余（completed）→ 正常主界面；
  //   请求失败（后端未就绪 / 浏览器 dev）→ 本次会话降级放行进主界面，
  //   绝不出现空白页 / 死循环 / 永久 loading，也不持久化跳过标记
  //   （否则首启时后端慢一拍就会让向导永久消失）。
  useEffect(() => {
    if (gate !== 'checking') return;
    let alive = true;
    (async () => {
      try {
        const st = await fetchSetupStatus();
        if (!alive) return;
        setSetupStatus(st ?? null);
        if (st?.wizard_required) {
          setGate('wizard');
        } else {
          setGate('main');
        }
      } catch {
        if (!alive) return;
        // 接口不可达：本次会话降级放行（不写持久标记），下次启动重新询问后端
        setGate('main');
      }
    })();
    return () => {
      alive = false;
    };
  }, [gate]);

  // 路由与 hash 双向同步
  useEffect(() => {
    const onHash = () => {
      const parsed = parseHash(window.location.hash);
      setView(parsed);
      // 运行期非法 hash 归一：回落视图的同时回写地址栏，消除 URL 与视图失配。
      // 死循环防护：仅在「非空且不在路由表」时回写；写回的目标必然合法，
      // 二次触发的 hashchange 不满足条件即短路（写入同值浏览器也不会再派发事件）。
      const clean = window.location.hash.replace(/^#\/?/, '').split('.')[0];
      if (clean && !VIEWS.includes(clean as View)) {
        window.location.hash = viewToHash(parsed);
      }
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 非法 hash 路由回落：非空但不在主界面路由的 hash 归一化写回 /chat
  useEffect(() => {
    const clean = window.location.hash.replace(/^#\/?/, '').split('.')[0];
    if (!clean) return;
    if (!VIEWS.includes(clean as View)) {
      window.location.hash = viewToHash('chat');
    }
  }, []);

  const router = useMemo<RouterValue>(
    () => ({
      view,
      appInfo,
      navigate(nextView) {
        if (!VIEWS.includes(nextView)) return;
        window.location.hash = viewToHash(nextView);
        setView(nextView);
      },
    }),
    [view, appInfo],
  );

  // 向导走完（或选择「先进去用」）：进聊天页
  const handleWizardDone = () => {
    setGate('main');
    window.location.hash = viewToHash('chat');
    setView('chat');
  };

  // 首帧等待：轻量 loading，不白屏
  if (gate === 'checking') {
    return <SetupLoading />;
  }

  // 需要初始化：向导覆盖主界面（不渲染 TopBar / Sidebar / 主视图）
  if (gate === 'wizard') {
    return (
      <RouterContext.Provider value={router}>
        <SetupWizard onDone={handleWizardDone} initialStatus={setupStatus} />
      </RouterContext.Provider>
    );
  }

  return (
    <RouterContext.Provider value={router}>
      <div className="app-surface flex h-full w-full overflow-hidden">
        <PetLifecycle />
        <TopBar />
        <Sidebar />
        <main className="flex-1 min-w-0 pt-14 pl-56">
          <ViewRenderer view={view} onSetupDone={handleWizardDone} />
        </main>
      </div>
    </RouterContext.Provider>
  );
}

/**
 * 桌宠生命周期挂载点：进入主界面即按开关自动拉起透明悬浮窗。
 *
 * 为什么要挂在 App 主界面而不是只挂桌宠页：`usePetEnabled` 的挂载恢复逻辑
 * 只在「hook 被挂载」时才执行——若仅挂桌宠页，用户不点进那一页就永远看不到
 * 悬浮角色，与「默认开启、双击即见」的预期不符。挂在主界面后每次启动都会
 * 询问一次开关状态；主进程侧建窗幂等（已存在则复用），不会重复开窗。
 * 向导阶段（gate==='wizard'）不挂载，避免悬浮角色盖在首启向导上。
 */
function PetLifecycle() {
  usePetEnabled();
  return null;
}

/** 首帧等待用的轻量加载态（避免白屏） */
function SetupLoading() {
  return (
    <div className="app-surface flex h-full w-full items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <span className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-[var(--glass-border)] border-t-[var(--color-primary)]" />
        <p className="text-sm text-[var(--text-secondary)]">正在准备…</p>
      </div>
    </div>
  );
}

function ViewRenderer({ view, onSetupDone }: { view: View; onSetupDone: () => void }) {
  switch (view) {
    case 'chat':
      return <ChatPage />;
    case 'pet':
      return <PetPage />;
    case 'memories':
      return <MemoriesPage />;
    case 'settings':
      return <SettingsPage />;
    case 'fleet':
      // 管理面（隐藏入口：连点侧栏 logo 5 次或深链 #/fleet）
      return <FleetPage />;
    case 'setup':
      return <SetupWizard onDone={onSetupDone} />;
    default:
      return <ChatPage />;
  }
}
