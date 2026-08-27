import React from 'react';
import { createRoot } from 'react-dom/client';
import PetOverlay from './components/PetOverlay';
import './styles/globals.css';

/**
 * 桌宠透明悬浮窗独立入口（对应 pet-overlay.html，由 main.js createPetOverlayWindow 加载）。
 * 与主入口 main.jsx 分离：本窗口只挂载 PetOverlay，不加载 TopBar/Sidebar 等主界面结构。
 * 透明背景已在 pet-overlay.html 中对 html/body 兜底置为 transparent。
 */
const container = document.getElementById('root');
if (!container) {
  throw new Error('找不到 #root 挂载点（pet-overlay）');
}

createRoot(container).render(
  <React.StrictMode>
    <PetOverlay />
  </React.StrictMode>,
);
