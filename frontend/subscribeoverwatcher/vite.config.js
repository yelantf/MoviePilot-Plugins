import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'subscribeoverwatcher',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './src/Config.vue',
        './Page': './src/Page.vue',
      },
      shared: {
        vue: { requiredVersion: false, generate: false },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
      },
      format: 'esm',
    }),
  ],
  build: {
    target: 'esnext',
    outDir: fileURLToPath(new URL('../../plugins.v3/subscribeoverwatcher/dist', import.meta.url)),
    emptyOutDir: true,
    cssCodeSplit: true,
  },
})
