import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        // Use explicit IPv4 address — Node 17+ resolves 'localhost' to ::1 first,
        // but uvicorn binds on 127.0.0.1 by default, causing proxy connection failures.
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
