"""跨平台中文字体设置（matplotlib）"""
import logging
logger = logging.getLogger(__name__)


def setup_cjk_font() -> bool:
    """跨平台中文字体检测 & 设置。返回 True 表示已设置 CJK 字体。"""
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    import platform

    system = platform.system()
    preferred = []
    if system == "Windows":
        preferred = ["Microsoft YaHei", "SimHei", "KaiTi", "FangSong", "SimSun"]
    else:
        preferred = ["Noto Sans CJK SC", "WenQuanYi Micro Hei",
                     "Droid Sans Fallback", "Noto Sans CJK JP",
                     "PingFang SC", "Heiti SC"]

    all_fonts = preferred + ["DejaVu Sans", "Arial", "sans-serif"]
    plt.rcParams["font.sans-serif"] = all_fonts
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False

    available = {f.name for f in fm.fontManager.ttflist}
    for pf in preferred:
        if pf in available:
            logger.debug(f"CJK字体已设置: {pf} (platform={system})")
            return True
    logger.warning(
        f"未找到CJK字体，图表中文可能显示为方块。"
        f"可用字体: {sorted(available)[:20]}"
    )
    return False
