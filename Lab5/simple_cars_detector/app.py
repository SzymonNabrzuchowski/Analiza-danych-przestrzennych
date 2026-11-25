"""Simple Cars Detector: upload a GeoTIFF, run vehicle detection tile by tile and draw the boxes."""

import base64
import io

import cv2
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from PIL import Image
from rasterio.io import MemoryFile
from ultralytics import YOLO

MODEL_FILE = "yolo11n-obb.pt"  # trained on DOTA (aerial images), downloaded on first use
VEHICLE_CLASSES = {"small vehicle", "large vehicle"}

TILE = 1024  # the model sees the image in tiles of this size
OVERLAP = 128  # tiles overlap, so a car on a tile edge is whole in one of them
MIN_CONF = 0.1  # detection keeps weak boxes too; the slider filters them afterwards

DISPLAY_MAX_PX = 3000  # larger images are shrunk for display only (zooming in shows no more detail)
BOX_COLOR = "#ffe600"
PLOTLY_CONFIG = {"scrollZoom": True, "displaylogo": False}
VEHICLE_COLORS = ["Biały", "Czarny", "Szary", "Czerwony", "Niebieski", "Zielony", "Żółty", "Pomarańczowy", "Brązowy", "Nieokreślony"]


@st.cache_data
def read_geotiff(data: bytes) -> tuple[np.ndarray, dict]:
    """GeoTIFF bytes -> RGB image (H, W, 3, uint8) + basic metadata."""
    with MemoryFile(data) as memfile, memfile.open() as src:
        bands = src.read(list(range(1, min(src.count, 3) + 1)))
        meta = {
            "width": src.width, "height": src.height, "count": src.count, "dtype": src.dtypes[0],
            "crs": src.crs.to_string() if src.crs else None, "res": src.res, "transform": src.transform,
        }
    if bands.shape[0] == 1:  # single band -> grayscale shown as RGB
        bands = np.repeat(bands, 3, axis=0)
    return to_uint8(bands).transpose(1, 2, 0), meta


def to_uint8(bands: np.ndarray) -> np.ndarray:
    """Stretch any data type to 0-255 (2-98 percentile), so e.g. 16-bit images are visible."""
    if bands.dtype == np.uint8:
        return bands
    low, high = np.nanpercentile(bands, (2, 98))
    return (np.clip((bands - low) / max(high - low, 1e-9), 0, 1) * 255).astype(np.uint8)


@st.cache_resource
def load_model() -> YOLO:
    return YOLO(MODEL_FILE)


@st.cache_data
def image_source(image: np.ndarray) -> tuple[str, float]:
    """Image as a JPEG data URI (shrunk to DISPLAY_MAX_PX) + how many original pixels one shown pixel covers."""
    picture = Image.fromarray(image)
    picture.thumbnail((DISPLAY_MAX_PX, DISPLAY_MAX_PX))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(), image.shape[1] / picture.width


def tiles(width: int, height: int) -> list[tuple[int, int]]:
    """Top-left corners (col, row) of overlapping tiles covering the whole image."""
    step = TILE - OVERLAP
    return [(col, row) for row in range(0, max(height - OVERLAP, 1), step)
            for col in range(0, max(width - OVERLAP, 1), step)]


def vehicle_color(image: np.ndarray, corners: np.ndarray) -> str:
    """Estimate the dominant visible colour inside an oriented vehicle box."""
    points = np.asarray(corners, dtype=np.float32)
    # OBB corners are cyclic; crop locally before rectifying to avoid copying the whole raster.
    x0, y0 = np.maximum(np.floor(points.min(axis=0)).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(points.max(axis=0)).astype(int) + 1, (image.shape[1], image.shape[0]))
    if x1 <= x0 or y1 <= y0 or np.any(points < 0) or np.any(points > (image.shape[1] - 1, image.shape[0] - 1)):
        return "Nieokreślony"
    width = int(round(np.linalg.norm(points[1] - points[0])))
    height = int(round(np.linalg.norm(points[2] - points[1])))
    if min(width, height) < 5:
        return "Nieokreślony"
    target = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    matrix = cv2.getPerspectiveTransform((points - (x0, y0)).astype(np.float32), target)
    crop = cv2.warpPerspective(np.ascontiguousarray(image[y0:y1, x0:x1]), matrix, (width, height))
    # Exclude the outer 10% of the box to reduce the influence of road pixels.
    mx, my = max(1, width // 10), max(1, height // 10)
    hsv = cv2.cvtColor(crop[my:height - my, mx:width - mx], cv2.COLOR_RGB2HSV)
    h, s, v = hsv.reshape(-1, 3).T

    # OpenCV HSV: H = 0–179, S and V = 0–255.
    black = v < 55
    neutral = (~black) & (s < 55)
    chromatic = (~black) & (~neutral)
    masks = {
        "Biały": neutral & (v >= 180),
        "Czarny": black,
        "Szary": neutral & (v < 180),
        "Czerwony": chromatic & ((h < 10) | (h >= 170)),
        "Niebieski": chromatic & (h >= 90) & (h < 140),
        "Zielony": chromatic & (h >= 35) & (h < 90),
        "Żółty": chromatic & (h >= 25) & (h < 35),
        "Pomarańczowy": chromatic & (h >= 10) & (h < 25) & (v >= 150),
        "Brązowy": chromatic & (h >= 10) & (h < 25) & (v < 150),
    }
    counts = {name: int(mask.sum()) for name, mask in masks.items()}
    counts["Nieokreślony"] = len(h) - sum(counts.values())
    ranked = sorted(counts, key=counts.get, reverse=True)
    # Reject ambiguous results rather than assigning a colour arbitrarily.
    if counts[ranked[0]] / len(h) < 0.35 or (counts[ranked[0]] - counts[ranked[1]]) / len(h) < 0.08:
        return "Nieokreślony"
    return ranked[0]


def detect_tile(image: np.ndarray, col: int, row: int) -> list[dict]:
    """Vehicles in one tile, with corners in pixels of the whole image."""
    tile = image[row:row + TILE, col:col + TILE]
    # The model expects BGR. agnostic_nms: one car must not come back as both "small" and "large vehicle".
    result = load_model().predict(tile[..., ::-1].copy(), imgsz=TILE, conf=MIN_CONF, agnostic_nms=True, verbose=False)[0]
    boxes = []
    for corners, cls, score in zip(result.obb.xyxyxyxy.cpu().numpy(), result.obb.cls.tolist(), result.obb.conf.tolist()):
        label = result.names[int(cls)]
        cx, cy = corners.mean(axis=0)
        # Overlapping tiles see the same car twice; keep it only in the tile whose inner part holds its centre.
        inner = (col == 0 or cx >= OVERLAP / 2) and (row == 0 or cy >= OVERLAP / 2) \
            and (col + TILE >= image.shape[1] or cx < TILE - OVERLAP / 2) \
            and (row + TILE >= image.shape[0] or cy < TILE - OVERLAP / 2)
        if label in VEHICLE_CLASSES and inner:
            global_corners = corners + (col, row)
            boxes.append({"label": label, "score": score, "corners": global_corners,
                          "color": vehicle_color(image, global_corners)})
    return boxes


def interactive_view(image: np.ndarray, boxes: list[dict]) -> go.Figure:
    """Zoomable image (scroll wheel, drag to pan) with vehicle boxes; axes are pixels of the original image."""
    source, scale = image_source(image)
    # x0/y0: plotly places pixel centres at x0, x0 + dx, ...; box corners use pixel edges (0 = left edge).
    fig = go.Figure(go.Image(source=source, dx=scale, dy=scale, x0=scale / 2, y0=scale / 2, hoverinfo="skip"))
    xs, ys = [], []
    for box in boxes:  # all boxes in one trace, separated by None, so even thousands draw fast
        corners = box["corners"]
        xs += [*corners[:, 0], corners[0, 0], None]
        ys += [*corners[:, 1], corners[0, 1], None]
    # go.Scatter (SVG), not Scattergl: the WebGL layer gets a wrong size over an image with locked aspect ratio.
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line={"color": BOX_COLOR, "width": 2}, hoverinfo="skip"))
    centres = np.array([box["corners"].mean(axis=0) for box in boxes]).reshape(-1, 2)
    fig.add_trace(go.Scatter(  # invisible points in box centres, only for the hover label
        # plain lists: numpy arrays get a binary encoding that older plotly.js bundled in Streamlit may not read
        x=centres[:, 0].tolist(), y=centres[:, 1].tolist(), mode="markers", marker={"size": 10, "opacity": 0},
        customdata=[[box["label"], box["score"], box["color"]] for box in boxes],
        hovertemplate="%{customdata[0]}<br>pewność: %{customdata[1]:.2f}<br>kolor: %{customdata[2]}<extra></extra>",
    ))
    fig.update_layout(height=750, margin={"l": 0, "r": 0, "t": 0, "b": 0}, dragmode="pan", showlegend=False)
    fig.update_xaxes(visible=False, range=[0, image.shape[1]])
    fig.update_yaxes(visible=False, range=[image.shape[0], 0], scaleanchor="x")  # rows grow downwards
    return fig


def results_table(boxes: list[dict], transform) -> pd.DataFrame:
    """One row per vehicle: class, confidence, centre in pixels and in map coordinates."""
    rows = []
    for box in boxes:
        col, row = box["corners"].astype(float).mean(axis=0)  # float64: map coordinates need the precision
        x, y = transform * (col, row)  # pixel -> map coordinates of the box centre
        rows.append({"klasa": box["label"], "pewność": round(box["score"], 2), "kolor": box["color"],
                     "kolumna": round(col), "wiersz": round(row), "x": round(x, 2), "y": round(y, 2)})
    return pd.DataFrame(rows, columns=["klasa", "pewność", "kolor", "kolumna", "wiersz", "x", "y"])


st.set_page_config(page_title="Simple Cars Detector", page_icon="🚗", layout="wide")

st.title("🚗 Simple Cars Detector")
st.write("Wgraj zdjęcie lotnicze lub ortofotomapę w formacie GeoTIFF i kliknij „Wykrywaj”.")

uploaded = st.file_uploader("Plik GeoTIFF", type=["tif", "tiff"])
if uploaded is None:
    st.stop()

image, meta = read_geotiff(uploaded.getvalue())

with st.sidebar:
    st.header("Metadane")
    st.write(f"Rozmiar: {meta['width']} x {meta['height']} px, pasma: {meta['count']} ({meta['dtype']})")
    st.write(f"Układ współrzędnych: {meta['crs'] or 'brak (zwykły TIFF)'}")
    if meta["crs"]:
        st.write(f"Rozmiar piksela: {meta['res'][0]:.2f} x {meta['res'][1]:.2f}")

        st.subheader("Współrzędne narożników")

        transform = meta["transform"]
        width, height = meta["width"], meta["height"]

        corners = {
            "Lewy górny": transform * (0, 0),
            "Prawy górny": transform * (width, 0),
            "Lewy dolny": transform * (0, height),
            "Prawy dolny": transform * (width, height),
        }

        for name, (x, y) in corners.items():
            st.write(f"**{name}:**")
            st.write(f"X: {x:.2f}, Y: {y:.2f}")

# Results live in session_state, so they survive reruns (e.g. moving the slider); a new file clears them.
if st.session_state.get("file_id") != uploaded.file_id or st.session_state.get("color_version") != 1:
    st.session_state.color_version = 1
    st.session_state.file_id = uploaded.file_id
    st.session_state.boxes = None

if st.button("Wykrywaj", type="primary"):
    positions = tiles(meta["width"], meta["height"])
    progress = st.progress(0.0, text="Wczytuję model...")
    boxes = []
    for i, (col, row) in enumerate(positions, start=1):
        boxes += detect_tile(image, col, row)
        progress.progress(i / len(positions), text=f"Kafelek {i} z {len(positions)}: wykryto {len(boxes)} pojazdów")
    st.session_state.boxes = boxes

boxes = st.session_state.boxes
if boxes is None:
    st.plotly_chart(interactive_view(image, []), config=PLOTLY_CONFIG, width="stretch")
    st.stop()

conf = st.slider("Próg pewności", MIN_CONF, 0.95, 0.25, 0.05)
selected_colors = st.multiselect("Kolor pojazdu", VEHICLE_COLORS, default=VEHICLE_COLORS)
shown = [b for b in boxes if b["score"] >= conf and b["color"] in selected_colors]

st.subheader(f"Wykryte pojazdy: {len(shown)}")
st.caption("Kółko myszy przybliża i oddala, przeciąganie przesuwa obraz, podwójne kliknięcie wraca do całości.")
st.plotly_chart(interactive_view(image, shown), config=PLOTLY_CONFIG, width="stretch")

st.dataframe(results_table(shown, meta["transform"]), hide_index=True)