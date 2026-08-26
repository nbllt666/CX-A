import React from 'react';

export type PetMood = 'happy' | 'calm';

interface PetAvatarProps {
  mood: PetMood;
  /** 是否处于说话状态（触发口型两档开合动画） */
  talking: boolean;
  /** 画面宽度（px），高度按比例跟随 */
  size?: number;
}

/**
 * 简化二次元卡通桌宠形象 —— 纯 CSS 占位，无 VRM / Three.js 等重依赖。
 *
 * 形象构成：圆脸 + 刘海 + 耳朵 + 眼睛 + 腮红 + 嘴。
 *   - 呼吸：脸整体做轻微缩放起伏；
 *   - 口型：data-talking 时嘴在两档开合间循环（说话占位动画）；
 *   - 表情：data-mood 控制眼睛/嘴形态，happy（开心）与 calm（平静）两种。
 *
 * PetPage（浏览器预览态）与 PetOverlay（Electron 悬浮窗）复用此组件。
 * 组件自带 scoped <style>，不依赖全局样式，可独立作为根节点渲染。
 */
export default function PetAvatar({ mood, talking, size = 220 }: PetAvatarProps) {
  return (
    <div
      className="cx-pet"
      style={{ width: size, height: Math.round(size * 1.05) }}
      data-mood={mood}
      data-talking={talking ? 'true' : 'false'}
    >
      <style>{PET_CSS}</style>

      {/* 身体 / 脸 */}
      <div className="cx-pet-face">
        {/* 耳朵 */}
        <div className="cx-pet-ear cx-pet-ear-l" />
        <div className="cx-pet-ear cx-pet-ear-r" />
        {/* 刘海 */}
        <div className="cx-pet-hair" />
        {/* 眼睛 */}
        <div className="cx-pet-eye cx-pet-eye-l">
          <span className="cx-pet-eye-iris" />
          <span className="cx-pet-eye-hi" />
        </div>
        <div className="cx-pet-eye cx-pet-eye-r">
          <span className="cx-pet-eye-iris" />
          <span className="cx-pet-eye-hi" />
        </div>
        {/* 腮红 */}
        <span className="cx-pet-blush cx-pet-blush-l" />
        <span className="cx-pet-blush cx-pet-blush-r" />
        {/* 嘴 */}
        <div className="cx-pet-mouth" />
      </div>
    </div>
  );
}

const PET_CSS = `
.cx-pet {
  position: relative;
  display: grid;
  place-items: center;
}
.cx-pet-face {
  position: relative;
  width: 76%;
  height: 64%;
  background: linear-gradient(160deg, #fff2f8, #ffd6ea);
  border-radius: 50% 50% 46% 46%;
  border: 2.5px solid rgba(255, 255, 255, 0.9);
  box-shadow:
    inset 0 -7px 16px rgba(255, 176, 222, 0.38),
    0 12px 26px rgba(255, 145, 210, 0.32);
  transform-origin: 50% 82%;
  animation: cx-pet-breathe 3.4s ease-in-out infinite;
}
@keyframes cx-pet-breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.045, 1.025); }
}

/* ---- 耳朵 ---- */
.cx-pet-ear {
  position: absolute;
  top: -10%;
  width: 27%;
  height: 38%;
  background: linear-gradient(170deg, #ffd4ec, #ffb9dc);
  border: 2.5px solid rgba(255, 255, 255, 0.9);
  border-radius: 50% 50% 28% 28%;
  box-shadow: inset 0 -3px 6px rgba(255, 150, 205, 0.3);
}
.cx-pet-ear-l { left: 0%; transform: rotate(-16deg); }
.cx-pet-ear-r { right: 0%; transform: rotate(16deg); }

/* ---- 刘海 ---- */
.cx-pet-hair {
  position: absolute;
  top: -6%;
  left: 7%;
  width: 86%;
  height: 30%;
  background: linear-gradient(180deg, #59bef0, #8fdcff);
  border-radius: 58% 58% 34% 34%;
  box-shadow: inset 0 -4px 8px rgba(39, 132, 190, 0.28);
}

/* ---- 眼睛（calm 平静：圆眼） ---- */
.cx-pet-eye {
  position: absolute;
  top: 36%;
  width: 15%;
  height: 25%;
  background: #ffffff;
  border-radius: 50%;
}
.cx-pet-eye-l { left: 20%; }
.cx-pet-eye-r { right: 20%; }
.cx-pet-eye-iris {
  position: absolute;
  left: 20%;
  top: 16%;
  width: 60%;
  height: 62%;
  background: radial-gradient(circle at 38% 30%, #b79cff, #7a5bff);
  border-radius: 50%;
}
.cx-pet-eye-hi {
  position: absolute;
  left: 14%;
  top: 12%;
  width: 26%;
  height: 26%;
  background: #ffffff;
  border-radius: 50%;
}

/* ---- 眼睛（happy 开心：弯弯笑脸眼） ---- */
.cx-pet[data-mood='happy'] .cx-pet-eye {
  top: 40%;
  height: 12%;
  background: transparent;
  box-shadow: none;
}
.cx-pet[data-mood='happy'] .cx-pet-eye::after {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  border-top: 4.5px solid #56468c;
  border-left: 4.5px solid #56468c;
  border-radius: 50% 0 0 0;
  box-sizing: border-box;
}
.cx-pet[data-mood='happy'] .cx-pet-eye-r::after {
  transform: scaleX(-1);
  border-radius: 0 50% 0 0;
}
.cx-pet[data-mood='happy'] .cx-pet-eye-iris,
.cx-pet[data-mood='happy'] .cx-pet-eye-hi {
  display: none;
}

/* ---- 腮红（普通态 + 开心加深） ---- */
.cx-pet-blush {
  position: absolute;
  top: 52%;
  width: 16%;
  height: 9%;
  background: rgba(255, 138, 194, 0.42);
  border-radius: 50%;
}
.cx-pet-blush-l { left: 8%; }
.cx-pet-blush-r { right: 8%; }
.cx-pet[data-mood='happy'] .cx-pet-blush {
  background: rgba(255, 120, 185, 0.6);
}

/* ---- 嘴（平静：小圆嘴） ---- */
.cx-pet-mouth {
  position: absolute;
  left: 50%;
  bottom: 20%;
  width: 13%;
  height: 9%;
  transform: translateX(-50%);
  background: #e05a8f;
  border-radius: 0 0 50% 50%;
  transform-origin: 50% 100%;
}
/* 开心：饱满笑脸嘴 */
.cx-pet[data-mood='happy'] .cx-pet-mouth {
  width: 17%;
  height: 12%;
  border-radius: 0 0 50% 50%;
  background: linear-gradient(180deg, #f477a8, #d94a84);
}
/* 说话：口型两档开合循环（说话占位动画） */
.cx-pet[data-talking='true'] .cx-pet-mouth {
  animation: cx-pet-talk 0.52s ease-in-out infinite;
}
@keyframes cx-pet-talk {
  0%, 100% { scale: 1; }
  50% { scale: 1 1.9; }
}
`;