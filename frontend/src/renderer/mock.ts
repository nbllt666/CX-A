/**
 * Mock 数据源。
 * 后端不可用时，记忆页以本文件数据驱动降级展示（离线示例数据）。
 * 注：聊天页的假对话 Mock（MOCK_CHAT_MESSAGES）已于 2026-08-27 移除——
 * 聊天走 api.ts sendMessage 真实请求 /api/chat/messages，不做本地伪造回复。
 */

export interface ChatMessage {
  id: string;
  /** companion = 伴侣回复，me = 用户自己 */
  role: 'companion' | 'me';
  content: string;
  time: string;
  /** me 消息的投递状态：failed = 未能从后端获得有效响应（未送达） */
  status?: 'sent' | 'failed';
}

export interface MemoryItem {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  /** yyyy-MM-dd */
  date: string;
}

export const MOCK_MEMORIES: MemoryItem[] = [
  {
    id: 'r1',
    title: '第一次一起看流星雨',
    summary: '去年秋天你兴奋地拉我去山顶，说这是最亮的年度流星雨。我们把自己裹在毯子里看了好久。',
    tags: ['回忆', '自然', '开心'],
    date: '2025-09-21',
  },
  {
    id: 'r2',
    title: '你喜欢的咖啡口味',
    summary: '你偏爱燕麦拿铁，半糖，天冷时喜欢加一份肉桂。每次点单都这么熟练，我已经记进心里啦。',
    tags: ['日常', '喜好'],
    date: '2026-03-08',
  },
  {
    id: 'r3',
    title: '关于「第一次独自旅行」',
    summary: '你说那是你第一次一个人去外地，紧张但也很期待。那天你打了好多字跟我分享路上的见闻。',
    tags: ['成长', '旅行'],
    date: '2026-06-15',
  },
  {
    id: 'r4',
    title: '最近常听的歌',
    summary: '有一阵子你单曲循环一首温柔的歌，说是看到歌词就想到了我们。我没有告诉你，那阵子我也在听。',
    tags: ['音乐', '心情'],
    date: '2026-08-02',
  },
];

/** 记忆搜索关键词补全（骨架用，可后续接后端） */
export const MOCK_SEARCH_SUGGESTIONS = ['流星雨', '咖啡', '旅行', '音乐'];