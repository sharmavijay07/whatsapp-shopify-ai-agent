import type { Metadata } from "next"
import { Fraunces, Outfit } from "next/font/google"

import "./globals.css"

const outfit = Outfit({
  subsets: ["latin"],
  variable: "--font-outfit",
})

const fraunces = Fraunces({
  subsets: ["latin"],
  variable: "--font-fraunces",
})

export const metadata: Metadata = {
  title: "Harbor & Co. — WhatsApp commerce sandbox",
  description:
    "Demo of a WhatsApp commerce agent that answers from the catalogue, checks Shopify stock, and places an order only after confirmation.",
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${outfit.variable} ${fraunces.variable} antialiased`}>{children}</body>
    </html>
  )
}
