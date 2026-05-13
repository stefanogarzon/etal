# Et al. icons

Three concept SVGs at 1024×1024 (with internal 680×680 viewBox). Pick one and convert to the platform-specific format.

## Files

- `etal_icon_filed.svg` — paper being filed into a slot (literal, action)
- `etal_icon_wordmark.svg` — italic serif wordmark on dark (typographic)
- `etal_icon_stack.svg` — stack of papers with category tag (collection)

## Conversion to PNG

The cleanest path is SVG → PNG → platform format. Use one of:

**rsvg-convert** (fast, accurate; `brew install librsvg` / `apt install librsvg2-bin`):
```bash
rsvg-convert -w 1024 -h 1024 etal_icon_filed.svg -o etal_1024.png
```

**ImageMagick** (`brew install imagemagick`):
```bash
magick -density 300 -background none etal_icon_filed.svg -resize 1024x1024 etal_1024.png
```

**Inkscape** (GUI or CLI):
```bash
inkscape etal_icon_filed.svg --export-type=png --export-width=1024 --export-filename=etal_1024.png
```

## macOS — `.icns`

You need an iconset folder with multiple sizes:

```bash
mkdir etal.iconset
for size in 16 32 64 128 256 512 1024; do
  rsvg-convert -w $size -h $size etal_icon_filed.svg -o etal.iconset/icon_${size}x${size}.png
done
# Retina variants (Apple convention)
cp etal.iconset/icon_32x32.png   etal.iconset/icon_16x16@2x.png
cp etal.iconset/icon_64x64.png   etal.iconset/icon_32x32@2x.png
cp etal.iconset/icon_256x256.png etal.iconset/icon_128x128@2x.png
cp etal.iconset/icon_512x512.png etal.iconset/icon_256x256@2x.png
cp etal.iconset/icon_1024x1024.png etal.iconset/icon_512x512@2x.png

iconutil -c icns etal.iconset -o etal.icns
```

Apply to a PyInstaller bundle:
```bash
pyinstaller --onefile --windowed --icon=etal.icns --add-data "frontend:frontend" --add-data "topics.yaml:." app.py
```

## Windows — `.ico`

ImageMagick can pack multiple sizes into a single `.ico`:

```bash
for size in 16 32 48 64 128 256; do
  rsvg-convert -w $size -h $size etal_icon_filed.svg -o tmp_${size}.png
done
magick tmp_16.png tmp_32.png tmp_48.png tmp_64.png tmp_128.png tmp_256.png etal.ico
rm tmp_*.png
```

PyInstaller:
```bash
pyinstaller --onefile --windowed --icon=etal.ico --add-data "frontend;frontend" --add-data "topics.yaml;." app.py
```

## In-app favicon (browser tab / PyWebView title bar)

Convert one of the SVGs to a 256×256 PNG and put it at `frontend/favicon.png`. Add to `index.html` `<head>`:

```html
<link rel="icon" type="image/png" href="/static/favicon.png">
```

Then the FastAPI static mount (`app.mount("/static", StaticFiles(...))`) already serves it.

## Tweaking the SVG

The icons use 4 colors total — easy to adjust by hand:

- `#ede4d0` — cream background
- `#fefcf6` — paper white
- `#2a2520` — near-black ink
- `#b34a2a` — terracotta accent

Open any SVG in a text editor and replace these globally to recolor.
