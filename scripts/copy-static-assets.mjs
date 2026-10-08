import { copyFileSync, cpSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(projectRoot, "node_modules", "leaflet", "dist", "images");
const destination = resolve(projectRoot, "static", "build", "images");
const fontDestination = resolve(projectRoot, "static", "build", "fonts");

mkdirSync(destination, { recursive: true });
cpSync(source, destination, { recursive: true });
mkdirSync(fontDestination, { recursive: true });

const fonts = [
  // The Tabler webfont stylesheet declares .ttf, .woff2 and .woff sources.
  // Manifests static storage requires every referenced file to exist, so all
  // three must be present or `collectstatic` aborts with MissingFileError.
  ["@tabler/icons-webfont/dist/fonts/tabler-icons.woff2", "tabler-icons.woff2"],
  ["@tabler/icons-webfont/dist/fonts/tabler-icons.woff", "tabler-icons.woff"],
  ["@tabler/icons-webfont/dist/fonts/tabler-icons.ttf", "tabler-icons.ttf"],
  ["@fontsource-variable/manrope/files/manrope-latin-wght-normal.woff2", "manrope-latin-wght-normal.woff2"],
  ["@fontsource-variable/inter/files/inter-latin-wght-normal.woff2", "inter-latin-wght-normal.woff2"],
];

fonts.forEach(([sourcePath, outputName]) => {
  copyFileSync(resolve(projectRoot, "node_modules", sourcePath), resolve(fontDestination, outputName));
});

console.log("Copied Leaflet images and local UI fonts to static/build.");
