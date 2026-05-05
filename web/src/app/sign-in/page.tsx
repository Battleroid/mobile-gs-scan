"use client";
import { useMemo } from "react";
import { useRouter } from "next/navigation";
import { PebbleMark } from "@/components/PebbleMark";

/**
 * Sign-in placeholder. Visual fidelity to the design's
 * StudioWebSignIn — left brand collage with floating splat tiles,
 * right QR / 6-digit code / email / Google fallback. None of the
 * forms post anything yet; "Continue with email" and "Continue with
 * Google" both just route to `/`. Real auth wiring lands in a later
 * effort and slots in without touching this layout.
 */
export default function SignInPage() {
  const router = useRouter();
  return (
    <div className="grid h-[100vh] grid-cols-1 overflow-hidden bg-bg lg:grid-cols-[1.05fr_1fr]">
      <Collage />
      <div className="flex items-center justify-center p-8 sm:p-10">
        <div className="w-full max-w-[400px]">
          <div className="mb-[6px] font-mono text-[11px] uppercase tracking-[0.14em] text-muted">
            get started
          </div>
          <h2 className="m-0 text-[32px] font-bold leading-[1.05] tracking-[-0.02em]">
            Sign in with your phone
          </h2>
          <p className="mt-2 text-[13.5px] leading-[1.5] text-inkSoft">
            Open the Pebble app on your phone and point its camera at the QR
            below — or punch in the code manually.
          </p>

          <QrCard />

          <div className="my-[18px] flex items-center gap-[10px] font-mono text-[10px] uppercase tracking-[0.1em] text-muted">
            <span className="h-px flex-1 bg-rule" />
            <span>or sign in on this computer</span>
            <span className="h-px flex-1 bg-rule" />
          </div>

          <div className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => router.push("/")}
              className="flex items-center justify-center gap-[10px] rounded-md border border-rule bg-surface px-[14px] py-[11px] text-sm font-semibold text-fg hover:border-ruleStrong"
            >
              <span>✉</span> Continue with email
            </button>
            <button
              type="button"
              onClick={() => router.push("/")}
              className="flex items-center justify-center gap-[10px] rounded-md border border-rule bg-surface px-[14px] py-[11px] text-sm font-semibold text-fg hover:border-ruleStrong"
            >
              <span
                className="h-[14px] w-[14px] rounded-full"
                style={{
                  background:
                    "conic-gradient(from -45deg, #4285F4 25%, #EA4335 25% 50%, #FBBC05 50% 75%, #34A853 75%)",
                }}
              />
              Continue with Google
            </button>
          </div>

          <div className="mt-[18px] flex items-center justify-center gap-[6px] font-mono text-[10px] text-muted">
            <span className="h-[6px] w-[6px] rounded-full bg-accent3" />
            connected to{" "}
            <span className="font-semibold text-fg">studio.local:5443</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// Left column — gradient backdrop, brand mark + wordmark + version
// chip top-left, tagline at the bottom, three rotated tiles floating
// in between as visual texture.
function Collage() {
  return (
    <div className="relative hidden flex-col justify-between overflow-hidden p-12 lg:flex bg-[linear-gradient(150deg,var(--chip-3)_0%,var(--chip-4)_60%,var(--bg)_100%)]">
      <div className="flex items-center gap-3">
        <PebbleMark size={32} />
        <span className="text-[26px] font-bold tracking-[-0.02em]">pebble</span>
        <span className="ml-1 rounded-xs border border-rule px-[6px] py-[2px] font-mono text-[10px] text-muted">
          v1.0 · LAN
        </span>
      </div>

      <FloatingTiles />

      <div className="relative">
        <div className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-accent">
          ★ self-hosted
        </div>
        <h1 className="m-0 max-w-[460px] text-[56px] font-bold leading-none tracking-[-0.02em]">
          A studio for splats, on your own terms.
        </h1>
        <p className="mt-[14px] max-w-[460px] text-[15px] leading-[1.5] text-inkSoft">
          Train, browse, and share Gaussian-splat captures from your phone —
          without sending a single byte off your network.
        </p>
      </div>
    </div>
  );
}

// Three rotated, gradient-filled square tiles with a sparse particle
// pattern — stand-in for the design's SplatViz tiles. Pure SVG so we
// don't pull a Three.js context into the sign-in route.
function FloatingTiles() {
  const tiles = [
    {
      style:
        "absolute right-[60px] top-[90px] h-[200px] w-[200px] rotate-[-4deg] shadow-[0_24px_60px_rgba(0,0,0,0.18)]",
      from: "var(--chip-1)",
      to: "var(--accent)",
      seed: "tile-a",
    },
    {
      style:
        "absolute right-[220px] top-[280px] h-[150px] w-[150px] rotate-[3deg] shadow-[0_18px_40px_rgba(0,0,0,0.14)]",
      from: "var(--chip-4)",
      to: "var(--accent-3)",
      seed: "tile-b",
    },
    {
      style:
        "absolute right-[100px] top-[460px] h-[130px] w-[130px] rotate-[-2deg] shadow-[0_14px_32px_rgba(0,0,0,0.12)]",
      from: "var(--chip-2)",
      to: "var(--accent-2)",
      seed: "tile-c",
    },
  ];
  return (
    <div className="pointer-events-none absolute inset-0">
      {tiles.map((t) => (
        <div
          key={t.seed}
          className={`overflow-hidden rounded-lg ${t.style}`}
          style={{ background: `linear-gradient(135deg, ${t.from}, ${t.to})` }}
        >
          <Particles seed={t.seed} />
        </div>
      ))}
    </div>
  );
}

function Particles({ seed }: { seed: string }) {
  const dots = useMemo(() => particlesFor(seed), [seed]);
  return (
    <svg
      className="h-full w-full"
      viewBox="0 0 100 100"
      preserveAspectRatio="xMidYMid slice"
    >
      {dots.map((d, i) => (
        <circle key={i} cx={d.x} cy={d.y} r={d.r} fill="white" fillOpacity={d.a} />
      ))}
    </svg>
  );
}

// QR card — a fake QR (deterministic dot pattern) with the Pebble
// mark stamped in the center, plus a 6-digit pairing code and the
// "waiting for phone" status line. This is purely cosmetic until
// real pairing wires up; the dots come from a fixed seed so the
// pattern doesn't reshuffle on each render.
function QrCard() {
  return (
    <div className="mt-[18px] flex flex-col items-center gap-3 rounded-lg border border-rule bg-surface p-[22px]">
      <div className="relative h-[220px] w-[220px] rounded-md border border-rule bg-white p-[14px]">
        <FakeQr />
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="inline-flex h-[44px] w-[44px] items-center justify-center rounded-md border-2 border-fg bg-white">
            <PebbleMark size={28} />
          </div>
        </div>
      </div>
      <div className="font-mono text-[22px] font-bold tracking-[0.32em] text-fg">
        4F · 92K
      </div>
      <div className="inline-flex items-center gap-[6px] font-mono text-[10.5px] text-inkSoft">
        <span className="h-[6px] w-[6px] rounded-full bg-accent" />
        waiting for phone · expires in 4:32
      </div>
    </div>
  );
}

function FakeQr() {
  const cells = useMemo(() => qrCells(), []);
  return (
    <svg className="h-full w-full" viewBox="0 0 25 25" shapeRendering="crispEdges">
      {cells.map((c, i) => (
        <rect key={i} x={c.x} y={c.y} width={1} height={1} fill="#1A1612" />
      ))}
    </svg>
  );
}

// ─── Pure helpers (no React state, safe to memoize) ───────────────

function particlesFor(seed: string) {
  let s = 0;
  for (let i = 0; i < seed.length; i++) s = (s * 31 + seed.charCodeAt(i)) | 0;
  const rand = () => {
    s = (s * 1664525 + 1013904223) | 0;
    return ((s >>> 0) % 10000) / 10000;
  };
  return Array.from({ length: 60 }, () => ({
    x: rand() * 100,
    y: rand() * 100,
    r: 0.4 + rand() * 1.2,
    a: 0.18 + rand() * 0.42,
  }));
}

function qrCells() {
  // 25×25 QR-shaped grid, deterministic seed so the pattern is
  // stable across renders. The three corner finder squares are
  // hardcoded so it reads as a QR at a glance instead of pure noise.
  const N = 25;
  let s = 0xdeadbeef;
  const rand = () => {
    s = (s * 1664525 + 1013904223) | 0;
    return ((s >>> 0) % 10000) / 10000;
  };
  const cells: { x: number; y: number }[] = [];
  const drawFinder = (cx: number, cy: number) => {
    for (let y = 0; y < 7; y++)
      for (let x = 0; x < 7; x++)
        if (
          x === 0 ||
          x === 6 ||
          y === 0 ||
          y === 6 ||
          (x >= 2 && x <= 4 && y >= 2 && y <= 4)
        )
          cells.push({ x: cx + x, y: cy + y });
  };
  drawFinder(0, 0);
  drawFinder(N - 7, 0);
  drawFinder(0, N - 7);
  for (let y = 0; y < N; y++)
    for (let x = 0; x < N; x++) {
      const inFinder =
        (x < 8 && y < 8) || (x >= N - 8 && y < 8) || (x < 8 && y >= N - 8);
      if (!inFinder && rand() > 0.55) cells.push({ x, y });
    }
  return cells;
}
