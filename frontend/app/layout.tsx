import type { Metadata } from "next";
import localFont from "next/font/local";

import { QueryProvider } from "@/lib/query-provider";

import "./globals.css";

// Variable font files are committed (OFL licence beside them): no font request
// leaves the app, at build time or in the browser.
const sans = localFont({
  src: [
    { path: "./fonts/PlusJakartaSans-latin-wght.woff2", weight: "200 800", style: "normal" },
  ],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "Outly", template: "%s · Outly" },
  description: "Outly: run cold email outreach campaigns, manage leads and mailboxes, and turn conversations into opportunities.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={sans.variable}>
      <body>
        <QueryProvider>{children}</QueryProvider>
      </body>
    </html>
  );
}

