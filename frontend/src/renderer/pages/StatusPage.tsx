import React from 'react';
import { ManagementPlaceholder } from './AgentsPage';
import { API_ENDPOINTS } from '../api';

/**
 * 状态页（/status）：结构化占位，能力以后端 API 提供（API 待接）。
 */
export default function StatusPage() {
  return (
    <ManagementPlaceholder
      icon="📡"
      title="状态"
      desc="系统状态监控：汇总前后端服务与资源运行状况。"
      endpoint={API_ENDPOINTS.management.status}
      sections={[
        {
          key: 'heartbeat',
          icon: '💓',
          title: '服务心跳',
          desc: '前后端存活与版本核对',
          status: 'API 待接',
        },
        {
          key: 'resources',
          icon: '📊',
          title: '资源占用',
          desc: 'CPU / 内存 / 磁盘使用率',
          status: 'API 待接',
        },
        {
          key: 'alerts',
          icon: '🚨',
          title: '异常告警',
          desc: '健康检查失败时自动提示',
          status: 'API 待接',
        },
        {
          key: 'logs',
          icon: '📜',
          title: '运行日志',
          desc: '滚动查看关键事件流',
          status: 'API 待接',
        },
      ]}
    />
  );
}