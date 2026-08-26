import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { getAppInfo, onModeSwitch } from './bridge';
import type { AppInfo } from './bridge';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import ChatPage from './pages/ChatPage';
import PetPage from './pages/PetPage';
import MemoriesPage from './pages/MemoriesPage';
import SettingsPage from './pages/SettingsPage';
import AgentsPage from './pages/AgentsPage';
import RemotePage from './pages/RemotePage';
import StatusPage from './pages/StatusPage';

/** 视图面：伴侣面（普通用户）/ 管理面（高级管理） */
export type Mode = 'companion' | 'management';
export type View =
  | 'chat'
  | 'pet'
  | 'memories'
  | 'settings'
  | 'agents'
  | 'remote'
  | 'status';

export const MODE_LABEL: Record<Mode, string> = {
  companion: '伴侣面',
  management: '管理面',
};

/** 每个面允许的路由 */
export const MODE_ROUTES: Record<Mode, View[]> = {
  companion: ['chat', 'pet', 'memories', 'settings'],
  management: ['agents', 'remote', 'status'],
};

/** 每个面进入时的默认路由 */
const MODE_DEFAULT: Record<Mode, View> = {
  companion: 'chat',
  management: 'agents',
};

/** 持久化键：当前视图面（localStorage，刷新保持） */
const MODE_STORAGE_KEY = 'cx-a.mode';

/** 读取持久化的模式；无/非法时返回 null。 */
function readStoredMode(): Mode | null {
  try {
    const v = window.localStorage.getItem(MODE_STORAGE_KEY);
    return v === 'companion' || v === 'management' ? v : null;
  } catch {
    return null;
  }
}

/** 写入持久化模式；存储不可用（隐私模式等）时静默忽略，路由仍以 hash 保持。 */
function writeStoredMode(mode: Mode): void {
  try {
    window.localStorage.setItem(MODE_STORAGE_KEY, mode);
  } catch {
    /* no-op */
  }
}

interface RouterValue {
  mode: Mode;
  view: View;
  appInfo: AppInfo | null;
  /** 仅在当前面内切换路由 */
  navigate: (view: View) => void;
  /** 伴侣面 ⇄ 管理面（同进程切换视图，不重启窗口） */
  switchMode: (mode: Mode) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

export function useRouter(): RouterValue {
  const ctx = useContext(RouterContext);
  if (!ctx) throw new Error('useRouter 需要在 Router 作用域内使用');
  return ctx;
}

/** 从 hash 解析出 (mode, view)，非法回退到伴侣面/聊天 */
function parseHash(hash: string): { mode: Mode; view: View } {
  const clean = hash.replace(/^#\/?/, '').split('.')[0] as View;
  if ((MODE_ROUTES.management as View[]).includes(clean)) {
    return { mode: 'management', view: clean };
  }
  if ((MODE_ROUTES.companion as View[]).includes(clean)) {
    return { mode: 'companion', view: clean };
  }
  return { mode: 'companion', view: 'chat' };
}

function viewToHash(view: View): string {
  return `#/${view}`;
}

export default function App() {
  const [{ mode, view }, setRoute] = useState(() => {
    // hash 为空（首次加载/直达根路径）时，优先还原上次持久化的模式到其默认页
    const clean = window.location.hash.replace(/^#\/?/, '').split('.')[0];
    if (!clean) {
      const stored = readStoredMode();
      const m: Mode = stored ?? 'companion';
      return { mode: m, view: MODE_DEFAULT[m] };
    }
    return parseHash(window.location.hash);
  });
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);

  // 读取应用信息（electron 下走桥，浏览器下走 mock）
  useEffect(() => {
    let alive = true;
    getAppInfo().then((info) => {
      if (alive) setAppInfo(info);
    });
    return () => {
      alive = false;
    };
  }, []);

  // 路由与 hash 双向同步；模式变化同步持久化
  useEffect(() => {
    const onHash = () => {
      const next = parseHash(window.location.hash);
      writeStoredMode(next.mode);
      setRoute(next);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 非法 hash 路由回落：非空但不在任何面的 hash 归一化写回 /chat
  useEffect(() => {
    const clean = window.location.hash.replace(/^#\/?/, '').split('.')[0];
    if (!clean) return;
    const inCompanion = (MODE_ROUTES.companion as View[]).includes(clean as View);
    const inManagement = (MODE_ROUTES.management as View[]).includes(clean as View);
    if (!inCompanion && !inManagement) {
      window.location.hash = viewToHash('chat');
    }
  }, []);

  // 支持主进程（托盘等）触发的视图面切换
  useEffect(() => {
    return onModeSwitch((nextMode) => {
      const target = MODE_ROUTES[nextMode][0];
      writeStoredMode(nextMode);
      window.location.hash = viewToHash(target);
      setRoute({ mode: nextMode, view: target });
    });
  }, []);

  const router = useMemo<RouterValue>(
    () => ({
      mode,
      view,
      appInfo,
      navigate(nextView) {
        if (!(MODE_ROUTES[mode] as View[]).includes(nextView)) return;
        window.location.hash = viewToHash(nextView);
        setRoute((prev) => ({ ...prev, view: nextView }));
      },
      switchMode(nextMode) {
        const target = MODE_DEFAULT[nextMode];
        writeStoredMode(nextMode);
        window.location.hash = viewToHash(target);
        setRoute({ mode: nextMode, view: target });
      },
    }),
    [mode, view, appInfo],
  );

  return (
    <RouterContext.Provider value={router}>
      <div className="app-surface flex h-full w-full overflow-hidden">
        <TopBar />
        <Sidebar />
        <main className="flex-1 min-w-0 pt-14 pl-56">
          <ViewRenderer view={view} />
        </main>
      </div>
    </RouterContext.Provider>
  );
}

function ViewRenderer({ view }: { view: View }) {
  switch (view) {
    case 'chat':
      return <ChatPage />;
    case 'pet':
      return <PetPage />;
    case 'memories':
      return <MemoriesPage />;
    case 'settings':
      return <SettingsPage />;
    case 'agents':
      return <AgentsPage />;
    case 'remote':
      return <RemotePage />;
    case 'status':
      return <StatusPage />;
    default:
      return <ChatPage />;
  }
}