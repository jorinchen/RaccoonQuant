"""
生成小浣熊图标 (.ico)，供 PyInstaller 打包使用
"""
import sys
import os
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
from PyQt6.QtCore import Qt

def draw_raccoon(size=256):
    """绘制小浣熊图标，返回 QPixmap"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = size
    cx, cy = s / 2, s / 2

    # --- 耳朵 ---
    ear_w, ear_h = s * 0.22, s * 0.28
    ear_color = QColor("#7A7A7A")
    ear_inner = QColor("#D4A574")
    for side in [-1, 1]:
        ex = cx + side * s * 0.22
        ey = s * 0.15
        p.setBrush(ear_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(ex - ear_w / 2), int(ey - ear_h / 2), int(ear_w), int(ear_h))
        p.setBrush(ear_inner)
        p.drawEllipse(int(ex - ear_w / 3.5), int(ey - ear_h / 3.5), int(ear_w * 0.55), int(ear_h * 0.55))

    # --- 脸部 ---
    face_w, face_h = s * 0.58, s * 0.52
    p.setBrush(QColor("#B0B0B0"))
    p.drawEllipse(int(cx - face_w / 2), int(cy - face_h / 2 + s * 0.02), int(face_w), int(face_h))

    # --- 白色脸颊区域 ---
    cheek_w, cheek_h = s * 0.36, s * 0.28
    p.setBrush(QColor("#E8E8E8"))
    p.drawEllipse(int(cx - cheek_w / 2), int(cy - cheek_h / 2 + s * 0.03), int(cheek_w), int(cheek_h))

    # --- 眼罩 ---
    mask_w, mask_h = s * 0.19, s * 0.13
    p.setBrush(QColor("#2C2C2C"))
    for side in [-1, 1]:
        mx = cx + side * s * 0.095
        my = cy - s * 0.06
        p.drawEllipse(int(mx - mask_w / 2), int(my - mask_h / 2), int(mask_w), int(mask_h))

    # --- 眼睛 ---
    eye_w, eye_h = s * 0.095, s * 0.1
    p.setBrush(QColor("#FFFFFF"))
    for side in [-1, 1]:
        ex = cx + side * s * 0.095
        ey = cy - s * 0.06
        p.drawEllipse(int(ex - eye_w / 2), int(ey - eye_h / 2), int(eye_w), int(eye_h))
    # 瞳孔
    pupil_r = s * 0.04
    p.setBrush(QColor("#111111"))
    for side in [-1, 1]:
        px = cx + side * s * 0.1
        py = cy - s * 0.055
        p.drawEllipse(int(px - pupil_r), int(py - pupil_r), int(pupil_r * 2), int(pupil_r * 2))
    # 高光
    p.setBrush(QColor("#FFFFFF"))
    hl_r = s * 0.018
    for side in [-1, 1]:
        hx = cx + side * s * 0.11
        hy = cy - s * 0.075
        p.drawEllipse(int(hx - hl_r), int(hy - hl_r), int(hl_r * 2), int(hl_r * 2))

    # --- 鼻子 ---
    nose_r = s * 0.04
    p.setBrush(QColor("#1A1A1A"))
    p.drawEllipse(int(cx - nose_r), int(cy - nose_r + s * 0.05), int(nose_r * 2), int(nose_r * 2))

    # --- 嘴巴 ---
    p.setPen(QPen(QColor("#555555"), max(1, int(s * 0.025))))
    p.setBrush(Qt.BrushStyle.NoBrush)
    mouth_y = cy + s * 0.1
    mouth_half = s * 0.06
    p.drawArc(int(cx - mouth_half), int(mouth_y - mouth_half * 0.5),
              int(mouth_half * 2), int(mouth_half), 0, 180 * 16)

    p.end()
    return pix


def main():
    app = QApplication(sys.argv)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 生成 256x256 的 PNG
    pix = draw_raccoon(256)
    png_path = os.path.join(script_dir, 'raccoon.png')
    pix.save(png_path, 'PNG')
    print(f'生成 PNG: {png_path}')

    # 生成 64x64 的 PNG（备用）
    pix64 = draw_raccoon(64)
    png64_path = os.path.join(script_dir, 'raccoon_64.png')
    pix64.save(png64_path, 'PNG')
    print(f'生成 PNG: {png64_path}')

    # 用 Pillow 转换为 ICO（包含多尺寸）
    try:
        from PIL import Image
        img = Image.open(png_path)
        # 生成多尺寸 ICO
        sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
        ico_path = os.path.join(script_dir, 'raccoon.ico')
        img.save(ico_path, format='ICO', sizes=sizes)
        print(f'生成 ICO: {ico_path}')
    except ImportError:
        print('WARN: 未安装 Pillow，无法生成 .ico')
        print('      运行: pip install Pillow')
        sys.exit(1)

    print('图标生成完成！')
    sys.exit(0)


if __name__ == '__main__':
    main()
