import nextConfig from "eslint-config-next";

// `eslint-config-next` exports a flat-config array directly. Spread
// it; `next-env.d.ts` etc. are already in its `ignores`.
const config = [...nextConfig];

export default config;
