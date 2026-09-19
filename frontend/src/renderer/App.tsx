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

/**
 * 伴侣面视图。
 *
 * 管理面（Agents / Remote / Status）已按决策收敛为纯后端 API：
 * 前端不再路由管理页，管理能力经 /api/agents、/api/remote/*、/api/status 外露，
 * 供另一 Agent 或管理工具调用（见 .trae/documents/20260826_模块0_差异审查登记与处理计划.md）。
 *
 * `setup` = 首启向导（首次启动覆盖主界面，或从设置页 / `#/setup` 深链重入）。
 */
export type View = 'chat' | 'pet' | 'memories' | 'settings' | 'setup';

const VIEWS: View[] = ['chat', 'pet', 'memories', 'settings', 'setup'];

/** localStorage 键（cx-a.* 家族）：接口不可达时本地记「已跳过向导」 */
const LS_SETUP_SKIPPED_KEY = 'cx-a.setup.skipped';
/** 旧版键名（cx.* 家族）：仅用于读取回退迁移，写入一律走新键 */
const LEGACY_LS_SETUP_SKIPPED_KEY = 'cx.setup.skipped';

/** 读取「已跳过向导」本地记录；新键缺失时回落旧键并顺手写入新键（静默迁移） */
function readSetupSkipped(): boolean {
  try {
    const raw = localStorage.getItem(LS_SETUP_SKIPPED_KEY);
    if (raw !== null) return raw === '1';
    const legacy = localStorage.getItem(LEGACY_LS_SETUP_SKIPPED_KEY);
    if (legacy !== null) {
      const value = legacy === '1';
      writeSetupSkipped(value);
      return value;
    }
    return false;
  } catch {
    return false;
  }
}

/** 写入「已跳过向导」（'1'/'0'），异常静默 */
function writeSetupSkipped(value: boolean): void {
  try {
    localStorage.setItem(LS_SETUP_SKIPPED_KEY, value ? '1' : '0');
  } catch {
    /* 存储被禁用时静默忽略 */
  }
}

/**
 * 首启门控状态：
 * - `checking`：首帧等待接口，渲染轻量 loading（不白屏）；
 * - `wizard`：需要初始化 → 向导覆盖主界面（不渲染 TopBar / Sidebar / 主视图）；
 * - `main`：正常渲染主界面（含「已完成初始化」「已跳过」两种来源）。
 */
type SetupGate = 'checking' | 'wizard' | 'main';

interface RouterValue {
  view: View;
  appInfo: AppInfo | null;
  /** 在伴侣面内切换视图（hash 路由，不重启窗口） */
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

/** 从 hash 解析出伴侣面视图；非法回退到 /chat */
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
  // 首帧门控：已本地记过「跳过」则不再请求（显式 `#/setup` 深链仍可在主界面内进向导）
  const [gate, setGate] = useState<SetupGate>(() => (readSetupSkipped() ? 'main' : 'checking'));
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
  //   其余（completed）→ 正常主界面并清掉「已跳过」记录；
  //   请求失败（后端未就绪 / 浏览器 dev）→ 降级放行：记「已跳过」并进主界面，
  //   绝不出现空白页 / 死循环 / 永久 loading。
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
          writeSetupSkipped(false);
          setGate('main');
        }
      } catch {
        if (!alive) return;
        writeSetupSkipped(true);
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

  // 非法 hash 路由回落：非空但不在伴侣面路由的 hash 归一化写回 /chat
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

  // 向导走完（或选择「先进去用」）：清掉「已跳过」记录并进聊天页
  const handleWizardDone = () => {
    writeSetupSkipped(false);
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
        <TopBar />
        <Sidebar />
        <main className="flex-1 min-w-0 pt-14 pl-56">
          <ViewRenderer view={view} onSetupDone={handleWizardDone} />
        </main>
      </div>
    </RouterContext.Provider>
  );
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
    case 'setup':
      return <SetupWizard onDone={onSetupDone} />;
    default:
      return <ChatPage />;
  }
}
