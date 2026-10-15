/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 高德 JS API key（Web 端）。未配置或为占位符时前端走无底图降级模式。 */
  readonly VITE_AMAP_KEY?: string;
  /** 高德 JS API 安全密钥 securityJsCode（2021-12 后申请的 key 必配）。 */
  readonly VITE_AMAP_SECURITY_JSCODE?: string;
  /** 后端 API 基址（默认同源，经 vite dev proxy 转发到 8000）。 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
