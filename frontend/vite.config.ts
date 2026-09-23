import { fileURLToPath, URL } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

/**
 * 开发代理只读取 VITE_BACKEND_URL，默认指向本地后端 127.0.0.1:8000。
 * 生产构建不嵌入任何密钥或凭据。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), '');
  const backendUrl = env.VITE_BACKEND_URL || 'http://127.0.0.1:8000';

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      rollupOptions: {
        output: {
          // ECharts 是体积最大的可选展示依赖，独立分包避免主应用 chunk 被其占满。
          manualChunks: {
            echarts: ['echarts'],
          },
        },
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      include: ['tests/**/*.test.ts'],
      restoreMocks: true,
    },
  };
});
