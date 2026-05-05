import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Providers } from "@/components/Providers";
import { PebbleHeader } from "@/components/PebbleHeader";
import "./globals.css";

// Both fonts surface as CSS variables so globals.css + Tailwind's
// fontFamily extension both pick them up without further wiring.
const geistSans = Geist({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

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
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body className="min-h-screen flex flex-col">
        <Providers>
          <PebbleHeader />
          <main className="flex-1 w-full">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
