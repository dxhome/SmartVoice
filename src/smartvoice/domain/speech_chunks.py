"""Text chunk boundaries for committed speech output."""
from smartvoice.domain.streaming import StageError
def chunks(text,limit=None,language='en',first_limit=None):
    if limit is None:limit=20 if language=='zh' else 100
    current_limit=min(limit,first_limit) if first_limit else limit
    start=0;first=True
    while start<len(text):
        end=min(len(text),start+current_limit)
        if end<len(text):
            if language=='zh':
                cut=max((text.rfind(mark,start,end+1) + 1 for mark in '。！？；，、' if text.rfind(mark,start,end+1)>=start),default=-1)
                if cut>start:end=cut
                else:
                    # Never bisect a Latin token: doing so can make a CJK
                    # fallback receive an unpronounceable suffix (e.g.
                    # "R" / "iverside"). Prefer ending before the token.
                    if (end<len(text) and text[end-1].isascii() and text[end-1].isalnum()
                            and text[end].isascii() and text[end].isalnum()):
                        token_start=end-1
                        while token_start>start and text[token_start-1].isascii() and text[token_start-1].isalnum():token_start-=1
                        if token_start>start:end=token_start
                        else:
                            token_end=end
                            while token_end<len(text) and text[token_end].isascii() and text[token_end].isalnum():token_end+=1
                            if token_end-start<=limit:end=token_end
                            else:raise StageError('tts_input_limit','Latin token exceeds the Chinese TTS chunk bound')
            else:
                space=text.rfind(' ',start,end+1)
                if space<=start:raise StageError('tts_input_limit','Single word exceeds synthesis chunk bound')
                end=space
        value=text[start:end].strip()
        if value:yield start,end,value
        if first:current_limit=limit;first=False
        start=end
        while start<len(text) and text[start].isspace():start+=1
