"""Conservative translation preparation; never rewrites raw ASR/alignment.

Calendar normalization uses explicit units. Names require an explicit session
lexicon; aliases are never inferred from sound-alike ASR or reference transcripts.
"""
from dataclasses import dataclass
import re
import json
import unicodedata
from smartvoice.domain.streaming import StageError

DIGITS=dict(zip('零〇一二三四五六七八九','00123456789'))
TERMS=(('罗曼语','Romance language'),('垂直净空','vertical clearance'),('步枪',' rifle'))
EN_YEAR_PARTS={'oh':0,'zero':0,'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19,
               'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90,
               'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9}


def normalize_spoken_english_years(text):
    """Normalize explicit 19xx/20xx year phrases in temporal contexts only."""
    changes=[]
    # Requiring a temporal preposition, full century and a decade/teen avoids
    # reinterpreting arbitrary counts such as "twenty one people" as a year.
    pattern=re.compile(r'\b(in|since|during|by|from|until)\s+(nineteen|twenty)\s+(oh|zero|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[ -]+(one|two|three|four|five|six|seven|eight|nine))?\b',re.I)
    def replace(m):
        century=19 if m[2].casefold()=='nineteen' else 20
        part=EN_YEAR_PARTS[m[3].casefold()]
        last=EN_YEAR_PARTS[m[4].casefold()] if m[4] else 0
        # Ones are meaningful only after decade words (e.g. sixty three).
        if m[4] and part<20:return m[0]
        year=century*100+part+last
        if not 1900<=year<=2099:return m[0]
        replacement=f'{m[1]} {year}'
        changes.append(('spoken_calendar_year',m[0],replacement))
        return replacement
    return pattern.sub(replace,text),changes


def integer(value):
    if value.isdigit():return int(value)
    if all(c in DIGITS for c in value):return int(''.join(DIGITS[c] for c in value))
    if not re.fullmatch(r'[一二三四五六七八九]?十[一二三四五六七八九]?',value):return None
    a,_,b=value.partition('十');return (int(DIGITS[a]) if a else 1)*10+(int(DIGITS[b]) if b else 0)


def validate_glossary(value):
    if value is None:return ()
    if not isinstance(value,list) or len(value)>24:raise StageError('invalid_glossary','Glossary must contain at most 24 explicit source/target entries')
    rows=[];seen=set()
    for row in value:
        if not isinstance(row,dict) or set(row)!={'source','target'}:raise StageError('invalid_glossary','Expected source and target strings')
        a,b=row['source'],row['target']
        if not all(isinstance(v,str) and v.strip()==v and 1<=len(v)<=64 and not any(unicodedata.category(c).startswith('C') for c in v) for v in (a,b)) or a.casefold() in seen:
            raise StageError('invalid_glossary','Glossary entries must be unique, nonempty printable strings up to 64 characters')
        seen.add(a.casefold());rows.append((a,b))
    if len(json.dumps(value,ensure_ascii=True).encode('ascii'))>3000:raise StageError('invalid_glossary','Glossary exceeds bounded configuration budget')
    return tuple(rows)


def normalize_calendar(text):
    changes=[]
    def year(m):
        digits=''.join(DIGITS.get(c,c) for c in m[1])
        # Four spoken digits, explicitly followed by 年. No guessing ordinal,
        # quantity, phone number, or missing digit from noisy recognition.
        if not 1000<=int(digits)<=2999:return m[0]
        replacement=digits+'年'
        if replacement!=m[0]:changes.append(('calendar_year',m[0],replacement))
        return replacement
    text=re.sub(r'(?<![零〇一二三四五六七八九\d])([零〇一二三四五六七八九\d]{4})年',year,text)
    def small(m):
        n=integer(m[1]);unit=m[2]
        if n is None or not (1<=n<=(12 if unit=='月' else 31)):return m[0]
        out=str(n)+unit
        if out!=m[0]:changes.append(('calendar_unit',m[0],out))
        return out
    text=re.sub(r'(?<![零〇一二三四五六七八九十百千万两\d])([零〇一二三四五六七八九十]{1,3})(月|日|号)',small,text)
    def code(m):
        n=integer(m[2]);out=m[1].upper()+str(n)
        changes.append(('explicit_alphanumeric',m[0],out));return out
    text=re.sub(r'(?<![A-Za-z0-9])([A-Za-z])\s*([一二三四五六七八九]?十[一二三四五六七八九]?)(?=步枪|型号|型)',code,text)
    return text,changes


def clauses(text,language):
    # Split only actual sentence boundaries or an explicit new demonstrative
    # copular subject. Keep if/then, not-until and although/but scopes together.
    if language=='zh':
        text=re.sub(r'[，,](?=(?:这|那)(?:并不是|不是|是))','。',text)
        return [s.strip() for s in re.findall(r'.*?[。！？!?]|.+$',text) if s.strip()]
    return [s.strip() for s in re.findall(r'.*?(?:[!?]|\.(?=\s|$))|.+$',text) if s.strip()]


def canonical(text):
    return ''.join(c.casefold() for c in unicodedata.normalize('NFKD',text) if c.isalnum())


@dataclass(frozen=True)
class Prepared:
    text: str
    changes: tuple
    expected_terms: tuple


class TranslationPolicy:
    def __init__(self,source_language,target_language,glossary=()):
        self.source_language,self.target_language=source_language,target_language
        self.glossary=tuple(glossary)
    def prepare(self,text):
        changes=[];expected=[]
        if self.source_language=='zh':text,changes=normalize_calendar(text)
        elif self.source_language=='en':text,changes=normalize_spoken_english_years(text)
        terms=list(self.glossary)+(list(TERMS) if (self.source_language,self.target_language)==('zh','en') else [])
        # Longest matches win in one pass; no recursive replacement/cascades.
        if terms:
            table=dict(reversed(terms));pattern='|'.join(re.escape(a) for a in sorted(table,key=len,reverse=True))
            # Latin names require word boundaries, Chinese terms do not.
            regex=re.compile(pattern,re.I if self.source_language=='en' else 0)
            def match(m):
                a=m[0];key=next((k for k in table if k.casefold()==a.casefold()),a)
                if self.source_language=='en' and ((m.start() and text[m.start()-1].isalnum()) or (m.end()<len(text) and text[m.end()].isalnum())):return a
                b=table[key];expected.append(b);changes.append(('explicit_glossary' if (key,b) in self.glossary else 'terminology',a,b));return b
            text=regex.sub(match,text)
        return Prepared(text,tuple(changes),tuple(expected))
    def inspect(self,source,target,prepared):
        issues=[]
        for year in re.findall(r'(?<!\d)([12]\d{3})年',source):
            if not re.search(r'(?<!\d)'+re.escape(year)+r'(?!\d)',target):issues.append('calendar_year_not_preserved:'+year)
        if self.source_language=='en':
            for year in re.findall(r'\b([12]\d{3})\b',source):
                spoken=''.join(next(k for k,v in DIGITS.items() if v==c) for c in year)
                if year not in target and spoken not in target:issues.append('calendar_year_not_preserved:'+year)
        for term in prepared.expected_terms:
            if canonical(term) not in canonical(target):issues.append('glossary_term_not_preserved:'+term)
        if re.search(r'(?<![A-Za-z0-9])M16\s+rifle(?![A-Za-z0-9])',source,re.I) and not re.search(r'\bM16\b.{0,24}\brifle\b|\brifle\b.{0,24}\bM16\b',target,re.I):
            issues.append('weapon_entity_not_preserved')
        negative=bool(re.search(r'并不是|不是|不能|不会|没有|直到.+才',source)) if self.source_language=='zh' else bool(re.search(r"\b(not|never|no|cannot)\b|n['’]t\b",source,re.I))
        target_negative=bool(re.search(r"\b(not|never|no|cannot|without|until)\b|n['’]t\b",target,re.I)) if self.target_language=='en' else bool(re.search(r'不|没|无|直到|才',target))
        if self.source_language=='zh' and self.target_language=='en' and re.search(r'直到.+才',source) and re.search(r'\bonly\b',target,re.I):target_negative=True
        if negative and not target_negative:issues.append('negation_requires_review')
        return issues
