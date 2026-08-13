import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ForexBot Dashboard",
  description: "Automated trading dashboard powered by Delta Exchange",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#0d1117]">{children}</body>
    </html>
  );
}
