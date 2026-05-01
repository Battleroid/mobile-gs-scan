import type { Metadata, Viewport } from "next";
import Link from "next/link";
import { Providers } from "@/components/Providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "mobile-gs-scan",
  description: "self-hosted 3D Gaussian Splatting capture studio",
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
    <html lang="en">
      <body className="min-h-screen flex flex-col">
        <Providers>
          <header className="border-b border-rule px-4 py-3 flex justify-between items-baseline">
            <Link href="/" className="font-semibold tracking-tight">
              mobile-gs-scan
            </Link>
            <nav className="text-xs text-muted flex gap-4">
              <Link href="/" className="hover:text-fg">
                captures
              </Link>
              <Link href="/captures/new" className="hover:text-fg">
                new
              </Link>
            </nav>
          </header>
          <main className="flex-1 max-w-4xl w-full mx-auto px-4 py-6">
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
