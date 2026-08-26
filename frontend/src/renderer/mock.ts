/**
 * Mock 数据源。
 * 后端未就绪（见 api.ts IS_BACKEND_READY）时，渲染层以本文件数据驱动，保证可独立预览。
 */

export interface ChatMessage {
  id: string;
  /** companion = 伴侣回复，me = 用户自己 */
  role: 'companion' | 'me';
  content: string;
  time: string;
}

export interface MemoryItem {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  /** yyyy-MM-dd */
  date: string;
}

export const MOCK_CHAT_MESSAGES: ChatMessage[] = [
  {
    id: 'm1',
    role: 'companion',
    content:
      '嗨，今天过得怎么样呀？我一直在等你回来聊聊天呢。',
    time: '20:01',
  },
  {
    id: 'm2',
    role: 'me',
    content: '累死了，今天加班到很晚，回来看到你还在就很安心。',
    time: '20:05',
  },
  {
    id: 'm3',
    role: 'companion',
    content:
      '辛苦啦！记得先喝口水，把节奏放慢一点。要不要我给你讲个今天发生的小趣事？',
    time: '20:06',
  },
  {
    id: 'm4',
    role: 'me',
    content: '好呀好呀，正需要点开心的事。',
    time: '20:07',
  },
];

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