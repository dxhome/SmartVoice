import re
from collections import Counter, deque
from itertools import islice


def append_timed_segments(previous, current, window_start, overlap_seconds):
    """Shift native timestamps without guessing the duration of start-only tokens.

    Start-only entries are removed only when the previous window supplied the
    same text at the same rounded absolute start. Uncertain overlap is retained.
    Matching consumes previous occurrences, preserving repeated native tokens.
    """
    boundary = window_start + overlap_seconds
    known = Counter((item.get('text'), item['start']) for item in previous
                    if item.get('text') and 'end' not in item
                    and window_start <= item['start'] < boundary)
    for item in current:
        shifted = {**item, 'start': round(float(item['start']) + window_start, 3)}
        if 'end' in item:
            if float(item['start']) < overlap_seconds and float(item['end']) <= overlap_seconds:
                continue
            shifted['end'] = round(float(item['end']) + window_start, 3)
        elif shifted['start'] < boundary:
            key = (shifted.get('text'), shifted['start'])
            if known[key]:
                known[key] -= 1
                continue
        previous.append(shifted)

def split_text(text, maximum, legacy=False):
    result=[]; start=0
    protected=[] if legacy else [(m.start(),m.end()) for m in re.finditer(
        r"\b\d+(?:[.,]\d+)?\s*(?:kilograms?|kg|grams?|km|cm|mm|ml|liters?|dollars?|USD)\b",text,re.I)]
    while start < len(text):
        end=min(start+maximum,len(text))
        if end < len(text):
            marks="。！？!?；;" if legacy else "\n。！？!?；;."
            boundary=max(text.rfind(mark,start+1,end) for mark in marks)
            # Do not split decimal numbers or common abbreviations at a period.
            if boundary > start and text[boundary] == ".":
                previous=text[max(start,boundary-4):boundary].split()[-1:]
                if ((text[boundary-1].isdigit() and text[boundary+1].isdigit()) or
                    previous and previous[0].lower() in {"mr","mrs","dr","ms","e.g","i.e"}):
                    boundary=-1
            if boundary <= start: boundary=text.rfind(" ",start+1,end)
            if boundary > start: end=boundary+1
            for left,right in protected:
                if start<left<end<right:
                    end=left;break
            # An unbroken token longer than the hard budget must still be split.
        result.append(text[start:end]); start=end
    return result


def merge(previous,current,overlap_seconds):
    """Only remove a bounded exact suffix/prefix when audio actually overlaps.

    No fuzzy matching: recognition errors remain visible. Long repeated speech
    without audio overlap is preserved. Text-only overlap is inherently uncertain.
    """
    if not previous: return current
    if not current: return previous
    if overlap_seconds:
        # Keep offsets in the original string: Unicode lower/casefold may
        # change its length. Bound stored matches to the alignment budget.
        tokens=lambda text:re.finditer(r'[\u3400-\u9fff]|[\w]+',text)
        a,b=list(deque(tokens(previous),maxlen=8)),list(islice(tokens(current),8))
        for count in range(min(8,len(a),len(b)),1,-1):
            if [x.group().casefold() for x in a[-count:]]==[x.group().casefold() for x in b[:count]]:
                current=current[b[count-1].end():].lstrip(' ,.!?，。！？')
                break
    if not current: return previous
    separator='' if re.search(r'[\u3400-\u9fff]$',previous) and re.match(r'[\u3400-\u9fff]',current) else ' '
    return previous+separator+current
