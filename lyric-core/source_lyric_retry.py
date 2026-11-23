"""Choose a local reread around a low-confidence CJK block in the original band."""
def han(letter):
    return '\u3400' <= letter <= '\u9fff'


def _line_boxes(original_text):
    for line in original_text.splitlines():
        fields = line.split('\t')
        if len(fields) == 7 and fields[0] == 'L':
            yield dict(confidence=float(fields[1]), box=list(map(int, fields[2:6])),
                       text=fields[6])



def retry_regions(group, original_text):
    height = group['font_height']
    x0, _, x1, _ = group['entry']['crop']
    baseline = group['baseline']
    blocks = sorted(_line_boxes(original_text), key=lambda block: block['box'][0])
    regions = []
    for index, block in enumerate(blocks):
        x, _, width, _ = block['box']
        han_count = sum(han(letter) for letter in block['text'])
        if not (.65 <= block['confidence'] < .95 and 2 <= han_count <= 5):
            continue
        if not 1.4 * height <= width <= 6 * height:
            continue
        left = x
        for neighbor in reversed(blocks[max(0, index - 2):index]):
            nx, _, nw, _ = neighbor['box']
            if neighbor['confidence'] < .95 or sum(han(c) for c in neighbor['text']) != 1:
                break
            if not 0 <= left - (nx + nw) <= 2 * height:
                break
            left = nx
        right = x + width + round(.88 * height)
        if index + 1 < len(blocks):
            right = min(right, blocks[index + 1]['box'][0] - round(.2 * height))
        left = max(x0, x0 + left - round(.44 * height))
        right = min(x1, x0 + right)
        if 2 * height < right-left <= 14 * height:
            regions.append((left, round(baseline-1.20*height), right, round(baseline+.27*height)))
    return regions
