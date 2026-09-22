import type { NextConfig } from 'next';

const nextConfig = {
  allowedDevOrigins: ['192.168.1.6'],
  // Proxy API calls to the FastAPI backend so the frontend works from any
  // host (localhost, LAN IP, ...) without hardcoded origins and without CORS.
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://127.0.0.1:8000/api/:path*',
      },
    ];
  },
}

module.exports = nextConfig
