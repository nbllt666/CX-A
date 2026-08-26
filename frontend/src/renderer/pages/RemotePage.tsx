import React from 'react';
import { ManagementPlaceholder } from './AgentsPage';
import { API_ENDPOINTS } from '../api';

/**
 * 远程管理页（/remote）：结构化占位，能力以后端 API 提供（局域网待接）。
 */
export default function RemotePage() {
  return (
    <ManagementPlaceholder
      icon="🌐"
      title="远程"
      desc="远端重度 CX-O 遥控：经局域网接管远端 CX-O 设备与会话。"
      endpoint={API_ENDPOINTS.management.remote}
      sections={[
        {
          key: 'discovery',
          icon: '📡',
          title: '设备发现',
          desc: '扫描局域网内可遥控的 CX-O 设备',
          status: '局域网待接',
        },
        {
          key: 'takeover',
          icon: '🎛️',
          title: '会话遥控',
          desc: '远程接管远端 CX-O 的进行中会话',
          status: '局域网待接',
        },
        {
          key: 'relay',
          icon: '📨',
          title: '指令透传',
          desc: '向下发白名单内的遥控指令',
          status: '局域网待接',
        },
        {
          key: 'liveness',
          icon: '❤️',
          title: '通道探测',
          desc: '检测遥控通道的延迟与连通质量',
          status: '局域网待接',
        },
      ]}
    />
  );
}