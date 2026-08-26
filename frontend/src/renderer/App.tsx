import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { getAppInfo } from './bridge';
import type { AppInfo } from './bridge';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import ChatPage from './pages/ChatPage';
import PetPage from './pages/PetPage';
import MemoriesPage from './pages/MemoriesPage';
import SettingsPage from './pages/SettingsPage';

/**
 * 伴侣面视图。
 *
 * 管理面（Agents / Remote / Status）已按决策收敛为纯后端 API：
 * 前端不再路由管理页，管理能力经 /api/agents、/api/remote/*、/api/status 外露，
 * 供另一 Agent 或管理工具调用（见 .trae/documents/20260826_模块0_差异审查登记与处理计划.md）。
 */
export type View = 'chat' | 'pet' | 'memories' | 'settings';

const VIEWS: View[] = ['chat', 'pet', 'memories', 'settings'];

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

  // 路由与 hash 双向同步
  useEffect(() => {
    const onHash = () => setView(parseHash(window.location.hash));
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
    default:
      return <ChatPage />;
  }
}