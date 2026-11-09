import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Outly brand: one royal-indigo scale for actions, active navigation,
        // links and focus. Semantic colors (emerald/amber/red) are reserved
        // for status; everything else is neutral slate.
        brand: {
          50: "#eef0ff",
          100: "#e1e4ff",
          200: "#c8ccff",
          300: "#a5a8fd",
          400: "#807ef8",
          500: "#625cf0",
          600: "#4a40e2",
          700: "#3d32c8",
          800: "#332aa2",
          900: "#2c2780",
        },
        // The dark navigation surface and anything that sits on it.
        ink: {
          300: "#aab3d1",
          400: "#8791b5",
          500: "#5c668c",
          600: "#2a3560",
          700: "#1b2447",
          800: "#121934",
          900: "#0b1024",
          950: "#070b1a",
        },
      },
      fontFamily: {
        // Plus Jakarta Sans is self-hosted (app/fonts, loaded in app/layout.tsx),
        // so nothing is downloaded at build or run time. The rest is fallback.
        sans: [
          "var(--font-sans)",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 3px 0 rgb(15 23 42 / 0.06)",
        "card-hover":
          "0 4px 12px -2px rgb(15 23 42 / 0.08), 0 2px 4px -2px rgb(15 23 42 / 0.06)",
        overlay:
          "0 12px 32px -8px rgb(15 23 42 / 0.18), 0 4px 8px -4px rgb(15 23 42 / 0.1)",
      },
      keyframes: {
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "pop-in": {
          from: { opacity: "0", transform: "translateY(-4px) scale(0.98)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "slide-in-left": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fade-in 150ms ease-out",
        "pop-in": "pop-in 120ms ease-out",
        "slide-in-left": "slide-in-left 180ms ease-out",
        "slide-up": "slide-up 180ms ease-out",
      },
    },
  },
  plugins: [],
};

export default config;
