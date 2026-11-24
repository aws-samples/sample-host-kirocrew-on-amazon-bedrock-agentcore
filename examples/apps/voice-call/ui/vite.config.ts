import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    lib: {
      // index.mjs: the app page. session-control.mjs: the chip under every chat box.
      entry: { index: 'src/App.tsx', 'session-control': 'src/SessionControl.tsx' },
      formats: ['es'],
      fileName: (_format, name) => `${name}.mjs`,
    },
    outDir: 'dist',
    rollupOptions: {
      external: [
        'react', 'react-dom', 'react/jsx-runtime',
        '@kirocrew/app-sdk', '@kirocrew/app-sdk/ui', 'lucide-react',
      ],
      output: { chunkFileNames: '[name].mjs' },
    },
  },
})
