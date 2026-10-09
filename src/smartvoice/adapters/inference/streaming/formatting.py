"""Punctuation-only adapter with lexical preservation checks."""
import re,unicodedata
from difflib import SequenceMatcher
import sherpa_onnx
from smartvoice.domain.streaming import StageError
from smartvoice.domain.stream_text import lexical
from smartvoice.adapters.inference.runtime.stream_profile import span

class PunctuationAdapter:
    def __init__(self,settings,spec,plan):
        from .assets import model_files
        path=model_files(settings,spec)[spec.model_file];num_threads=plan.formatting_threads
        config=sherpa_onnx.OfflinePunctuationConfig()
        config.model.ct_transformer=str(path);config.model.num_threads=num_threads
        self.model=sherpa_onnx.OfflinePunctuation(config)
        self.projection_repairs=0
        self.plan={'model_id':spec.id,'lexical_preservation':True,'max_chars':512}
    def format(self,text,language,final=False):
        if not text.strip():return ''
        if len(text)>512:raise StageError('formatting_input_limit','Formatting context exceeds 512 characters')
        with span('formatting.add_punct'):result=self.model.add_punctuation(text)
        if lexical(result)!=lexical(text) or (language=='en' and re.findall(r'[a-z0-9]+',result.casefold())!=re.findall(r'[a-z0-9]+',text.casefold())):
            # Some sherpa export inputs lose terminal English words. Project
            # punctuation only onto matching raw lexical positions, retaining
            # every original word. This is declared and counted, never passed
            # through as a supposedly punctuation-only model result.
            self.projection_repairs+=1
            raw_positions=[i for i,c in enumerate(text) if not c.isspace() and not unicodedata.category(c).startswith('P')]
            model_chars=[];punct={}
            for c in result:
                if unicodedata.category(c).startswith('P'):
                    if model_chars:punct.setdefault(len(model_chars)-1,'');punct[len(model_chars)-1]+=c
                elif not c.isspace():model_chars.append(c.casefold())
            mapping={}
            for block in SequenceMatcher(None,lexical(text),''.join(model_chars),autojunk=False).get_matching_blocks():
                for k in range(block.size):mapping[block.b+k]=block.a+k
            insert={}
            for index,marks in punct.items():
                if index in mapping:insert[raw_positions[mapping[index]]]=marks
            result=''.join(c+insert.get(i,'') for i,c in enumerate(text))
            if final and not result.endswith(('。','.','?','!','？','！')):result+='。' if language=='zh' else '.'
        if lexical(result)!=lexical(text):raise StageError('formatting_lexical_change','Projection did not preserve input')
        if language=='en':
            # Explicit casing policy, retaining original all-caps ASR separately.
            if text.isupper():result=result.lower()
            result=re.sub(r'\bi\b','I',result)
            result=result.translate(str.maketrans({'，':',','。':'.','？':'?','！':'!','：':':','；':';'}))
            result=re.sub(r'([,;:!?])(?=\w)',r'\1 ',result)
            result=re.sub(r'(^|[.!?]\s+)([a-z])',lambda m:m[1]+m[2].upper(),result)
        if language=='en':
            # An offline punctuation prediction must not separate a preposition
            # from its complement across a forced ASR chunk boundary.
            result=re.sub(r'\b(and|but|or|because|although|if|by|with|of|to|from|for)\.(?=\s+\w)',r'\1',result,flags=re.I)
        if language=='zh':
            # An explicit new demonstrative copular subject can delimit a
            # complete clause; preserve if/then and not-until relations.
            result=re.sub(r'[，,](?=(?:这|那)(?:并不是|不是|是))','。',result)
            if not re.match(r'^(如果|虽然|尽管|因为|假如)',result):result=re.sub(r'(?<=[米吨岁])[，,](?=(?:该|本|这个|那个)(?:项目|工程)(?:于|在|已|将))','。',result)
        # The offline model closes its current snapshot; trailing punctuation is
        # tentative until enough lookahead or explicit input-final confirms it.
        if not final:result=result.rstrip('。.!?！？')
        return result
    def close(self):self.model=None
