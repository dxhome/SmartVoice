"""Text chunk boundaries for committed speech output."""
from smartvoice.domain.streaming import StageError


def _last_sentence_end(text, start, end, language):
    """Return the last complete sentence boundary in the current chunk window."""
    boundary = -1
    for index in range(start, end):
        char = text[index]
        if char in '。！？':
            boundary = index + 1
            continue
        if language != 'en' or char not in '.!?':
            continue
        # Do not split decimal numbers at their period.
        if char == '.' and index and index + 1 < len(text) and text[index - 1].isdigit() and text[index + 1].isdigit():
            continue
        following = index + 1
        while following < len(text) and text[following] in '\"\'”’»)]}':
            following += 1
        if following == len(text) or text[following].isspace():
            boundary = following
    return boundary


def chunks(text,limit=None,language='en',first_limit=None):
    if limit is None:limit=20 if language=='zh' else 100
    current_limit=min(limit,first_limit) if first_limit else limit
    start=0;first=True
    while start<len(text):
        end=min(len(text),start+current_limit)
        if end<len(text):
            # Speak complete sentences as individual TTS inputs whenever they
            # fit. Splitting at an arbitrary word every N characters causes
            # model-internal pauses in the middle of otherwise fluent speech.
            sentence_end = _last_sentence_end(text, start, end, language)
            if sentence_end > start:
                end = sentence_end
            if language == 'zh' and sentence_end <= start:
                cut=max((text.rfind(mark,start,end+1) + 1 for mark in '；，、' if text.rfind(mark,start,end+1)>=start),default=-1)
                if cut>start:
                    end=cut
                # Never bisect a Latin token: doing so can make a CJK
                # fallback receive an unpronounceable suffix (e.g.
                # "R" / "iverside"). Prefer ending before the token.
                elif (end<len(text) and text[end-1].isascii() and text[end-1].isalnum()
                        and text[end].isascii() and text[end].isalnum()):
                    token_start=end-1
                    while token_start>start and text[token_start-1].isascii() and text[token_start-1].isalnum():token_start-=1
                    if token_start>start:
                        end=token_start
                    else:
                        token_end=end
                        while token_end<len(text) and text[token_end].isascii() and text[token_end].isalnum():token_end+=1
                        if token_end-start<=limit:end=token_end
                        else:raise StageError('tts_input_limit','Latin token exceeds the Chinese TTS chunk bound')
            elif language != 'zh' and sentence_end <= start:
                space=text.rfind(' ',start,end+1)
                if space<=start:raise StageError('tts_input_limit','Single word exceeds synthesis chunk bound')
                end=space
        value=text[start:end].strip()
        if value:yield start,end,value
        if first:current_limit=limit;first=False
        start=end
        while start<len(text) and text[start].isspace():start+=1
