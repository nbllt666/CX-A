/** @type {import('tailwindcss').Config} */
// 语义 token 在 src/renderer/styles/tokens.css 中定义，组件通过 arbitrary value（如 bg-[var(--color-primary)]）直接消费，此处无需重复映射。
module.exports = {
  content: ['./index.html', './src/renderer/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {},
  },
  plugins: [],
};