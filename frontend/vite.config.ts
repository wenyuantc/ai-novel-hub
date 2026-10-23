/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.VITE_BACKEND_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
        // SSE：禁止代理层压缩/缓冲，保证增量实时到达
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type'] ?? '').startsWith('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform'
              proxyRes.headers['x-accel-buffering'] = 'no'
            }
          })
        },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
    rolldownOptions: {
      output: {
        // 给共享的第三方依赖起清晰的 chunk 名（Vite 8 / rolldown 中 manualChunks 已弃用，等价写法为 codeSplitting.groups）。
        // 图谱库（@xyflow/react、@dagrejs/dagre 及其依赖）刻意不分组，留在懒加载的 RelationGraph chunk 中。
        codeSplitting: {
          groups: [
            {
              name: 'vendor-react',
              test: /node_modules[\\/](react|react-dom|scheduler|react-router|react-router-dom|@remix-run)[\\/]/,
              priority: 20,
            },
            {
              name: 'vendor-antd',
              test: /node_modules[\\/](antd|@ant-design|@rc-component|rc-[^\\/]+|@emotion|stylis|dayjs|@babel[\\/]runtime|classnames|resize-observer-polyfill|throttle-debounce|compute-scroll-into-view|scroll-into-view-if-needed|json2mq|string-convert|toggle-selection|copy-to-clipboard)[\\/]/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
