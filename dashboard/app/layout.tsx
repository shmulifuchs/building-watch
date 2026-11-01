import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Building Watch — Your building, at a glance",
  description: "Your private view of camera health, activity, and recording requests.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
