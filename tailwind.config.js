/** @type {import('tailwindcss').Config} */
export default {
  content: ["./frontend/index.html", "./frontend/src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#142b38",
        muted: "#52666d",
        paper: "#f2f6f3",
        line: "#d6e0da",
        coral: "#d95741",
        teal: "#147f7a",
        gold: "#efc456",
      },
      boxShadow: {
        soft: "0 16px 40px rgba(20, 43, 56, .08)",
      },
    },
  },
  plugins: [],
};
