import io
from PIL import Image, ImageDraw, ImageFont

def generate_pnl_card(symbol, side, entry_price, current_price, pnl_pct, status_text):
    # Image Canvas (800x450 Dark theme)
    width, height = 800, 450
    bg_color = (18, 22, 28)
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Colours
    is_profit = pnl_pct >= 0
    accent_color = (14, 203, 129) if is_profit else (246, 70, 93)  # Binance Green / Red
    white = (240, 243, 246)
    gray = (132, 142, 156)

    # Standard fonts (Default cross-platform font)
    try:
        font_large = ImageFont.truetype("arial.ttf", 58)
        font_mid = ImageFont.truetype("arial.ttf", 26)
        font_small = ImageFont.truetype("arial.ttf", 20)
    except Exception:
        font_large = font_mid = font_small = ImageFont.load_default()

    # Header: Brand & Status
    draw.text((40, 30), "BYBIT FUTURES", fill=gray, font=font_small)
    draw.text((40, 60), f"{symbol}  {side.upper()} 10x", fill=white, font=font_mid)

    # Status Tag (e.g. TP 1 HIT / TP 2 HIT / STOP LOSS)
    draw.rounded_rectangle([(600, 40), (760, 85)], radius=6, fill=accent_color)
    draw.text((615, 52), status_text, fill=white, font=font_small)

    # ROI %
    pnl_sign = "+" if pnl_pct > 0 else ""
    pnl_display = f"{pnl_sign}{pnl_pct:.2f}%"
    draw.text((40, 140), "ROI", fill=gray, font=font_small)
    draw.text((40, 175), pnl_display, fill=accent_color, font=font_large)

    # Line Separator
    draw.line([(40, 280), (760, 280)], fill=(38, 43, 51), width=2)

    # Entry & Current Price
    draw.text((40, 310), "Entry Price", fill=gray, font=font_small)
    draw.text((40, 345), f"{entry_price:.4f}", fill=white, font=font_mid)

    draw.text((450, 310), "Last Price", fill=gray, font=font_small)
    draw.text((450, 345), f"{current_price:.4f}", fill=white, font=font_mid)

    # Footer note
    draw.text((40, 400), "SMC Trend & Dual-CCI Automated System", fill=gray, font=font_small)

    # Return buffer
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf