from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "demo.gif"


def font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def frame(active: int) -> Image.Image:
    image = Image.new("RGB", (1100, 650), "#f4f1e8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((35, 28, 1065, 622), 24, fill="#fffdf7", outline="#d7d5ca", width=2)
    draw.rounded_rectangle((66, 60, 114, 108), 12, fill="#15231f")
    draw.text((79, 70), "H3", font=font(19, True), fill="#d8ef8f")
    draw.text((130, 62), "HY3 TRACEJUDGE", font=font(14, True), fill="#176b52")
    draw.text((66, 127), "答案对了，过程真的成立吗？", font=font(36, True), fill="#15231f")
    stages = ["Hy3 生成分步解答", "固定测试 6/6", "Hypothesis 找到并缩减反例", "Hy3 复核：首错步骤 2"]
    for index, label in enumerate(stages):
        y = 210 + index * 74
        done = index <= active
        color = "#176b52" if done else "#d7d5ca"
        draw.rounded_rectangle((70, y, 500, y + 54), 12, fill="#f5f7ed", outline=color, width=3)
        draw.ellipse((88, y + 17, 108, y + 37), fill=color)
        draw.text((124, y + 13), label, font=font(18, done), fill="#15231f")
    draw.text((570, 208), "评估结果", font=font(16, True), fill="#62706b")
    if active < 3:
        draw.text((570, 250), "正在收集可执行证据…", font=font(24, True), fill="#15231f")
    else:
        draw.text((570, 250), "最终答案：正确", font=font(25, True), fill="#176b52")
        draw.text((570, 295), "推理过程：不成立", font=font(25, True), fill="#ad3b35")
        draw.text((570, 340), "首个错误：步骤 2", font=font(25, True), fill="#ad3b35")
        draw.rounded_rectangle((570, 402, 1000, 535), 12, fill="#17231f")
        draw.text((592, 420), "最小反例", font=font(15, True), fill="#d8ef8f")
        draw.text((592, 455), "coins=[1, 3, 4], amount=6", font=font(18), fill="#dcecdf")
        draw.text((592, 490), "expected=2, actual=3", font=font(18), fill="#dcecdf")
    return image


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [frame(index) for index in range(4)] + [frame(3)] * 2
    frames[0].save(OUTPUT, save_all=True, append_images=frames[1:], duration=[700, 700, 900, 1300, 900, 900], loop=0)
    print(OUTPUT)


if __name__ == "__main__":
    main()
