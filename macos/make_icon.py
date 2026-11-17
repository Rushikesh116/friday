"""Draw Friday's app icon (1024×1024 PNG): the glowing core orb from the dashboard on a
dark navy tile, laid out on Apple's macOS icon grid (824 px body, rounded corners).

    .venv/bin/python macos/make_icon.py build/icon-1024.png
"""

import math
import sys

import AppKit
import Quartz

SIZE = 1024
BODY = 824                     # Apple's icon grid: the tile is 824 px inside a 1024 canvas
INSET = (SIZE - BODY) / 2
RADIUS = 185.4                 # corner radius for an 824 px tile
CX = CY = SIZE / 2


def rgba(r, g, b, a=1.0):
    return Quartz.CGColorCreateGenericRGB(r / 255, g / 255, b / 255, a)


def gradient(stops):
    """stops: [(position, (r, g, b, a)), ...] with 0-255 colour channels and 0-1 alpha."""
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    comps, locs = [], []
    for pos, (r, g, b, a) in stops:
        comps += [r / 255, g / 255, b / 255, a]
        locs.append(pos)
    return Quartz.CGGradientCreateWithColorComponents(space, comps, locs, len(locs))


def radial(ctx, cx, cy, r, stops):
    Quartz.CGContextDrawRadialGradient(ctx, gradient(stops), (cx, cy), 0, (cx, cy), r, 0)


def tile_path():
    rect = Quartz.CGRectMake(INSET, INSET, BODY, BODY)
    return Quartz.CGPathCreateWithRoundedRect(rect, RADIUS, RADIUS, None)


def draw(ctx):
    # Soft drop shadow under the tile, like system icons.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextSetShadowWithColor(ctx, (0, -14), 30, rgba(0, 0, 0, 0.5))
    Quartz.CGContextAddPath(ctx, tile_path())
    Quartz.CGContextSetFillColorWithColor(ctx, rgba(4, 12, 24))
    Quartz.CGContextFillPath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    # Everything else is clipped to the tile.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, tile_path())
    Quartz.CGContextClip(ctx)

    # Background: deep navy, lighter towards the top, with a cyan bloom behind the orb.
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient([(0, (2, 7, 16, 1)), (0.55, (7, 22, 44, 1)), (1, (12, 34, 64, 1))]),
        (CX, INSET), (CX, INSET + BODY), 0)
    radial(ctx, CX, CY, 430, [(0, (40, 200, 255, 0.42)), (0.45, (20, 120, 210, 0.14)), (1, (0, 0, 0, 0))])

    # Faint HUD grid.
    Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(70, 190, 255, 0.06))
    Quartz.CGContextSetLineWidth(ctx, 2)
    step = BODY / 10
    for i in range(1, 10):
        x = INSET + i * step
        Quartz.CGContextMoveToPoint(ctx, x, INSET)
        Quartz.CGContextAddLineToPoint(ctx, x, INSET + BODY)
        Quartz.CGContextMoveToPoint(ctx, INSET, x)
        Quartz.CGContextAddLineToPoint(ctx, INSET + BODY, x)
    Quartz.CGContextStrokePath(ctx)

    R = 236  # orb radius

    # Tilted orbit ellipse behind the orb.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextTranslateCTM(ctx, CX, CY)
    Quartz.CGContextRotateCTM(ctx, math.radians(-20))
    Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(80, 220, 255, 0.32))
    Quartz.CGContextSetLineWidth(ctx, 5)
    Quartz.CGContextStrokeEllipseInRect(ctx, Quartz.CGRectMake(-R * 1.5, -R * 0.42, R * 3.0, R * 0.84))
    Quartz.CGContextRestoreGState(ctx)

    # Particle sphere (same golden-spiral layout as the dashboard orb), back to front.
    n, golden = 520, math.pi * (3 - math.sqrt(5))
    ay, ax = 0.6, 0.38
    pts = []
    for i in range(n):
        y = 1 - (i / (n - 1)) * 2
        r = math.sqrt(1 - y * y)
        th = golden * i
        x, z = math.cos(th) * r, math.sin(th) * r
        X = x * math.cos(ay) + z * math.sin(ay)
        Z = -x * math.sin(ay) + z * math.cos(ay)
        Y = y * math.cos(ax) - Z * math.sin(ax)
        Z = y * math.sin(ax) + Z * math.cos(ax)
        pts.append((CX + X * R, CY + Y * R, Z))
    for px, py, z in sorted(pts, key=lambda p: p[2]):
        depth = (z + 1) / 2                      # 0 back … 1 front
        s = 3.2 + depth * 7.5
        Quartz.CGContextSetFillColorWithColor(ctx, rgba(120 + 100 * depth, 230, 255, 0.18 + depth * 0.72))
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(px - s / 2, py - s / 2, s, s))

    # Bright core.
    radial(ctx, CX, CY, R * 0.82, [(0, (245, 255, 255, 1.0)), (0.16, (190, 250, 255, 0.95)),
                                   (0.42, (53, 228, 255, 0.55)), (1, (0, 120, 220, 0))])

    # HUD ring with ticks.
    ring = R * 1.24
    Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(80, 225, 255, 0.55))
    Quartz.CGContextSetLineWidth(ctx, 5)
    Quartz.CGContextStrokeEllipseInRect(ctx, Quartz.CGRectMake(CX - ring, CY - ring, ring * 2, ring * 2))
    for k in range(60):
        a = k / 60 * 2 * math.pi
        long = k % 5 == 0
        r1, r2 = ring + 6, ring + (26 if long else 15)
        Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(80, 225, 255, 0.7 if long else 0.32))
        Quartz.CGContextSetLineWidth(ctx, 5 if long else 3)
        Quartz.CGContextMoveToPoint(ctx, CX + math.cos(a) * r1, CY + math.sin(a) * r1)
        Quartz.CGContextAddLineToPoint(ctx, CX + math.cos(a) * r2, CY + math.sin(a) * r2)
        Quartz.CGContextStrokePath(ctx)

    # Three glowing arcs, like the dashboard's rotating HUD arcs.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextSetShadowWithColor(ctx, (0, 0), 22, rgba(53, 228, 255, 0.95))
    Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(150, 245, 255, 1.0))
    Quartz.CGContextSetLineWidth(ctx, 16)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    arc_r = R * 1.42
    for k in range(3):
        start = math.radians(100 + k * 120)
        Quartz.CGContextAddArc(ctx, CX, CY, arc_r, start, start + math.radians(52), 0)
        Quartz.CGContextStrokePath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    # Glass highlight across the top of the tile.
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient([(0, (255, 255, 255, 0.0)), (1, (255, 255, 255, 0.07))]),
        (CX, INSET + BODY * 0.55), (CX, INSET + BODY), 0)
    Quartz.CGContextRestoreGState(ctx)

    # Thin bright rim.
    Quartz.CGContextAddPath(ctx, Quartz.CGPathCreateWithRoundedRect(
        Quartz.CGRectMake(INSET + 2, INSET + 2, BODY - 4, BODY - 4), RADIUS - 2, RADIUS - 2, None))
    Quartz.CGContextSetStrokeColorWithColor(ctx, rgba(90, 210, 255, 0.35))
    Quartz.CGContextSetLineWidth(ctx, 3)
    Quartz.CGContextStrokePath(ctx)


def main(out: str) -> None:
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    gctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(gctx)
    ctx = gctx.CGContext()
    Quartz.CGContextClearRect(ctx, Quartz.CGRectMake(0, 0, SIZE, SIZE))
    Quartz.CGContextSetShouldAntialias(ctx, True)
    draw(ctx)
    gctx.flushGraphics()
    AppKit.NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if not png.writeToFile_atomically_(out, True):
        raise SystemExit(f"couldn't write {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "icon-1024.png")
