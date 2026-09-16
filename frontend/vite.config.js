import { defineConfig } from 'vite'

import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig(({ mode }) => {
  const isDesktop = mode === 'desktop'

  return {
    base: isDesktop ? './' : '/',
    plugins: [
      vue(),
      !isDesktop && VitePWA({
        registerType: 'prompt',
        includeAssets: ['logos/*.png'],
        manifest: {
          name: 'WaveFlow Radio',
          short_name: 'WaveFlow',
          description: '极简电台聚合播放器',
          lang: 'zh-CN',
          theme_color: '#f8f8f7',
          background_color: '#f8f8f7',
          display: 'standalone',
          orientation: 'any',
          start_url: '/',
          scope: '/',
          icons: [
            { src: '/icons/apple-touch-icon.png', sizes: '180x180', type: 'image/png' },
            { src: '/icons/android-chrome-192x192.png', sizes: '192x192', type: 'image/png' },
            { src: '/icons/android-chrome-512x512.png', sizes: '512x512', type: 'image/png' },
            { src: '/icons/android-chrome-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
          ],
        },
        workbox: {
          globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
          navigateFallback: 'index.html',
          navigateFallbackDenylist: [/^\/api(?:\/|$)/],
          runtimeCaching: [
            {
              urlPattern: /^\/api(?:\/|$)/,
              handler: 'NetworkOnly',
            },
            {
              urlPattern: /^\/logos\/.*\.(png|jpe?g|svg)$/i,
              handler: 'NetworkFirst',
              options: {
                cacheName: 'waveflow-logo-assets-v1',
                networkTimeoutSeconds: 3,
                cacheableResponse: { statuses: [200] },
                expiration: { maxEntries: 100, maxAgeSeconds: 30 * 24 * 3600 },
              },
            },
          ],
        },
      }),
    ].filter(Boolean),

    server: {
      host: '0.0.0.0',

      port: 5173,

      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
          xfwd: true,
        },
      },
    },
  }
})
