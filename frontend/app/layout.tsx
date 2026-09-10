import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Smart Photo Scraper",
  description: "Album-to-full-size-photo downloader",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
