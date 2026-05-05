import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        bgAlt: "var(--bg-alt)",
        surface: "var(--surface)",
        fg: "var(--fg)",
        inkSoft: "var(--ink-soft)",
        muted: "var(--muted)",
        rule: "var(--rule)",
        ruleStrong: "var(--rule-strong)",
        accent: "var(--accent)",
        accent2: "var(--accent-2)",
        accent3: "var(--accent-3)",
        warn: "var(--warn)",
        danger: "var(--danger)",
        chip1: "var(--chip-1)",
        chip2: "var(--chip-2)",
        chip3: "var(--chip-3)",
        chip4: "var(--chip-4)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      // Soft radius scale from tokens.jsx → RADIUS_SCALES[1]. The
      // default Tailwind radii (rounded-sm/md/lg/xl/2xl/full) map onto
      // these so existing utility classes pick up the new scale; xs +
      // pill are added for the design's mono pills + tiny chips.
      borderRadius: {
        xs: "4px",
        sm: "8px",
        md: "12px",
        lg: "18px",
        xl: "24px",
        "2xl": "28px",
        pill: "999px",
      },
    },
  },
  plugins: [],
};

export default config;
