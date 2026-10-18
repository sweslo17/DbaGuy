# DbaGuy — Transparent Logo Pack

All PNG assets are **RGBA with real alpha transparency**; no opaque white canvas is baked into the logo.

## Folders

- `00_Master-PNG`: source-quality transparent artwork used for this pack (painted raster masters).
- `01_Horizontal-Logo`: horizontal full logo for light and dark backgrounds, with optional tagline. Files are named by width in pixels; height preserves the artwork aspect ratio.
- `02_Wordmark-Only`: DbaGuy text without the symbol.
- `03_Standalone-Symbol`: golden rake + blue database on a transparent square canvas.
- `04_App-Icon-Transparent-Outside`: rounded navy macOS icon with transparent pixels outside the icon shape.
- `05_Monochrome`: navy, white, black, gray single-color treatments for small sizes and restrictive interfaces.

## Choose the right logo

- Light webpage/README: `01_Horizontal-Logo/For-Light-Background/DbaGuy-logo-color-1024px.png`
- Dark webpage/README: `01_Horizontal-Logo/For-Dark-Background/DbaGuy-logo-reversed-1024px.png`
- GitHub avatar/App: `04_App-Icon-Transparent-Outside/DbaGuy-AppIcon-512px.png`
- Transparent unframed image: `03_Standalone-Symbol/DbaGuy-rake-database-512px.png`
- Small status and monochrome treatments: `05_Monochrome`

## Rendering notes

- The number in file name is **width** for horizontal logos and **canvas width/height** for square images.
- All sizes are resized from the **same image master**, not separately generated variations.
- Sizes exceeding their raster master's native dimensions are upscaled and do not introduce more true source detail. Use the `00_Master-PNG` originals where possible.
- The 16px app icon is available but a lot of detail is lost at that size. Prefer the compact 32/64px icon in UI contexts when practical.
- These are **raster assets**, not native editable vector artwork.
