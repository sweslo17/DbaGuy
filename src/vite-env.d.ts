/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND_URL?: string;
  readonly VITE_BACKEND_TOKEN?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** The release this build is (vite.config.ts): the CI tag's `0.2.0-beta.1`, or `0.2.0-dev` for a local build. */
declare const __APP_RELEASE__: string;
