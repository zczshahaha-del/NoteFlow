import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const base = process.env.NOTEFLOW_BASE_PATH || '/'
if (!/^\/(?:[a-zA-Z0-9_-]+\/)*$/.test(base)) {
  throw new Error('NOTEFLOW_BASE_PATH must be an absolute path ending in /')
}

function devApiProxyTarget(): string {
  const fromEnv = process.env.VITE_API_PROXY_TARGET?.trim()
  if (fromEnv) {
    return fromEnv.replace(/\/$/, '')
  }
  try {
    const portFile = path.join(__dirname, 'server', '.dev-api-port')
    const raw = fs.readFileSync(portFile, 'utf8').trim()
    if (/^\d+$/.test(raw)) {
      return `http://127.0.0.1:${raw}`
    }
  } catch {
    /* file missing before backend starts */
  }
  return 'http://127.0.0.1:8080'
}

const apiPrefix = `${base}api`
const proxy = {
  [`^${apiPrefix}(?:/|\\?|$)`]: {
    target: devApiProxyTarget(),
    changeOrigin: true,
    rewrite: (url: string) => url.slice(base.length - 1),
    cookiePathRewrite: { '/': base },
  },
}

export default defineConfig({
  base,
  plugins: [react(), tailwindcss()],
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'tiptap-markdown',
              test: /node_modules[\\/]@tiptap[\\/]markdown/,
              priority: 30,
            },
            {
              name: 'tiptap-prosemirror',
              test: /node_modules[\\/](?:@tiptap[\\/]pm|prosemirror-)/,
              priority: 20,
            },
            {
              name: 'app-store',
              test: /[\\/]src[\\/]store[\\/]index\.tsx$/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  server: { proxy },
  preview: { proxy },
})
