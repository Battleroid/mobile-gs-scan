import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Providers } from "@/components/Providers";
import { PebbleHeader } from "@/components/PebbleHeader";
import "./globals.css";

// Vercel's `geist` package ships the same Geist + Geist Mono woff2
// binaries `next/font/google` would otherwise fetch from Google at
// build time. The package's `.variable` surface is identical, so the
// Tailwind / globals.css wiring downstream sees the same CSS vars.
//
// The reason we don't use `next/font/google`: Pebble is positioned
// for self-hosted LAN deployments. `next build` then runs in air-
// gapped environments where `fonts.googleapis.com` isn't reachable
// and the build fails before compile. `geist` is fully local — the
// woff2 files land in node_modules and are served alongside the app.
//
// (The `.variable` property is a class name applied to the HTML
// element below; it sets `--font-sans` / `--font-mono` as CSS vars
// scoped to <html>. Tailwind's `fontFamily.sans = var(--font-sans)`
// picks them up without further plumbing.)

export const metadata: Metadata = {
  title: "Pebble",
  description: "a small studio for 3D scans.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body className="min-h-screen flex flex-col">
        <Providers>
          <PebbleHeader />
          <main className="flex-1 w-full">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
