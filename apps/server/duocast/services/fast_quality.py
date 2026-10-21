"""Conservative output enhancement, not generative super-resolution."""
PRESETS = {
    'original': {'filter': '', 'crf': 18, 'roleCrf': 17},
    'clear': {'filter': 'unsharp=5:5:0.45:5:5:0', 'crf': 16, 'roleCrf': 14},
    'crisp': {'filter': 'unsharp=5:5:0.75:5:5:0', 'crf': 15, 'roleCrf': 13},
}

def quality_options(value):
    if not isinstance(value, str) or value not in PRESETS:
        raise ValueError('清晰度必须为 original、clear 或 crisp')
    return PRESETS[value].copy()
