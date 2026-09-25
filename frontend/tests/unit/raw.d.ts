/**
 * Vite `?raw` 导入的类型声明（测试用）。
 *
 * 例：`import html from '../../index.html?raw'` 直接取文件原文
 * （tests/unit/csp.test.ts 的 CSP 静态断言用它，避免依赖 cwd / import.meta.url 路径）。
 */
declare module '*?raw' {
  const content: string;
  export default content;
}