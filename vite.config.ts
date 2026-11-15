import { defineConfig, type Connect } from 'vite';
import react from '@vitejs/plugin-react';

const framePolicy: Connect.NextHandleFunction = (request, response, next) => {
  const path = request.url?.split('?')[0];
  const ancestors = path === '/embed' || path === '/embed/' ? "'self' https:" : "'none'";
  response.setHeader('Content-Security-Policy', `frame-ancestors ${ancestors}`);
  next();
};

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'viewer-frame-policy',
      configureServer(server) { server.middlewares.use(framePolicy); },
      configurePreviewServer(server) { server.middlewares.use(framePolicy); },
    },
  ],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
});
