import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Checkpoint Next.js Demo",
  description: "A minimal SSR demo app for Checkpoint workflows.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

