"""Lexical preservation and coarse source-offset mapping."""
import unicodedata

def lexical(text):
    return ''.join(c.casefold() for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))

def source_offsets(raw, formatted):
    """Map formatted lexical characters to raw offsets; no timestamps invented."""
    raw_indices=[i for i,c in enumerate(raw) if not c.isspace() and not unicodedata.category(c).startswith('P')]
    out=[];n=0
    for c in formatted:
        if c.isspace():out.append(raw_indices[n-1]+1 if n else 0)
        elif unicodedata.category(c).startswith('P'):
            previous=raw_indices[n-1]+1 if n else 0
            following=raw_indices[n] if n<len(raw_indices) else len(raw)
            source_marks=[i for i in range(previous,following) if unicodedata.category(raw[i]).startswith('P')]
            out.append(source_marks[-1]+1 if source_marks else previous)
        else:out.append(raw_indices[n]+1);n+=1
    return out
