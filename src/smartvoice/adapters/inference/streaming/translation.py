"""Finite CTranslate2 translation; tokenization stays inside the adapter."""
import json
from smartvoice.domain.streaming import StageError,TranslationResult
from smartvoice.adapters.inference.runtime.stream_profile import span
TranslationError=StageError

class TextTranslation:
    def __init__(self,settings,spec,plan):
        import ctranslate2
        import sentencepiece as spm
        from .assets import model_files
        files=model_files(settings,spec);descriptor=spec.streaming
        self.m2m=descriptor['family']=='m2m100'
        self.source_language=plan.source_language;self.target_language=plan.target_language
        self.quality_policy=None;self.policy=dict(descriptor['policy']);self.policy['intra_threads']=plan.translation_threads
        source_name='sentencepiece.bpe.model' if self.m2m else 'source.spm'
        self.source=spm.SentencePieceProcessor(model_file=str(files['tokenizer/'+source_name]))
        self.target=self.source if self.m2m else spm.SentencePieceProcessor(model_file=str(files['tokenizer/target.spm']))
        self.vocab=json.loads(files['tokenizer/vocab.json'].read_text())
        self.model=ctranslate2.Translator(str(files[spec.model_file].parent),device='cpu',compute_type='int8',intra_threads=plan.translation_threads,inter_threads=1)
        self.plan={'model_id':spec.id,'native_streaming':False,'beam_size':self.policy['beam_size']}
        self.configure_quality(plan.glossary)
    def source_tokens(self,text):
        if not text.strip() or len(text)>self.policy['max_source_chars']:
            raise TranslationError('translation_input_limit','Expected nonempty source within character limit')
        pieces=self.source.encode(text,out_type=str)
        tokens=([ '__'+getattr(self,'source_language','zh')+'__' ] if self.m2m else [])+[piece if piece in self.vocab else '<unk>' for piece in pieces]+['</s>']
        if len(tokens)>self.policy['max_source_tokens']:
            raise TranslationError('translation_input_limit','Source token limit exceeded; no truncation allowed')
        return tokens

    def configure_quality(self,glossary=()):
        from smartvoice.domain.translation_policy import TranslationPolicy
        self.quality_policy=TranslationPolicy(self.source_language,self.target_language,glossary)
        self.plan={**self.plan,'quality_policy':'conservative_v1','glossary':[dict(source=a,target=b) for a,b in glossary],'raw_source_preserved':True}

    def _translate(self,text):
        with span('translation.tokenize'):tokens=self.source_tokens(text)
        began=__import__('time').monotonic()
        result=self.model.translate_batch([tokens],target_prefix=[['__'+getattr(self,'target_language','en')+'__']] if self.m2m else None,beam_size=self.policy['beam_size'],
            max_input_length=0,max_decoding_length=self.policy['max_target_tokens'],return_end_token=True)[0]
        from smartvoice.adapters.inference.runtime.stream_profile import RECORDER
        recorder=RECORDER.get()
        if recorder:recorder.add('translation.translate_batch',__import__('time').monotonic()-began)
        target=result.hypotheses[0]
        if not target or target[-1]!='</s>':
            raise TranslationError('translation_output_limit','Target reached decoding bound without completion')
        clean=[piece for piece in target if piece not in ('</s>','<pad>','__en__','__zh__')]
        with span('translation.detokenize'):output=self.target.decode(clean).strip()
        if not output:
            raise TranslationError('translation_empty','No nonempty target generated')
        return {'text':output,'source_tokens':len(tokens),'target_tokens':len(target)}

    def _stream(self,text,on_text,cancelled):
        with span('translation.tokenize'):tokens=self.source_tokens(text)
        pieces=[]
        def callback(step):
            if cancelled():return True
            token=step.token
            if token not in ('</s>','<pad>','__en__','__zh__'):
                pieces.append(token)
                with span('translation.detokenize'):decoded=self.target.decode(pieces).strip()
                on_text(decoded)
            return False
        with span('translation.greedy_decode'):
            result=self.model.translate_batch([tokens],target_prefix=[['__'+getattr(self,'target_language','en')+'__']] if self.m2m else None,
                beam_size=1,max_input_length=0,max_decoding_length=self.policy['max_target_tokens'],
                return_end_token=True,callback=callback)[0]
        target=result.hypotheses[0]
        if cancelled():return {'text':'','source_tokens':len(tokens),'target_tokens':len(target)}
        if not target or target[-1]!='</s>':raise TranslationError('translation_output_limit','Greedy output did not finish')
        clean=[t for t in target if t not in ('</s>','<pad>','__en__','__zh__')]
        with span('translation.detokenize'):output=self.target.decode(clean).strip()
        if not output:raise TranslationError('translation_empty','Empty greedy target')
        return {'text':output,'source_tokens':len(tokens),'target_tokens':len(target)}

    def _quality(self,text,on_text=None,cancelled=lambda:False):
        from smartvoice.domain.translation_policy import clauses
        policy=self.quality_policy
        if not text.strip() or len(text)>self.policy['max_source_chars']:raise TranslationError('translation_input_limit','Source exceeds character budget')
        prepared=policy.prepare(text)
        if len(prepared.text)>self.policy['max_source_chars']:raise TranslationError('translation_input_limit','Normalized/glossary input exceeds character budget')
        parts=clauses(prepared.text,self.source_language)
        if len(parts)>12:raise TranslationError('translation_input_limit','Too many independent clauses in one translation job')
        outputs=[];source_tokens=target_tokens=0;issues=[]
        for part in parts:
            if cancelled():return {'text':'','source_tokens':source_tokens,'target_tokens':target_tokens}
            if on_text:
                prefix=(' ' if self.target_language=='en' else '').join(outputs)
                def callback(draft):on_text(prefix+(' ' if prefix and self.target_language=='en' else '')+draft)
                out=self._stream(part,callback,cancelled)
            else:out=self._translate(part)
            if cancelled():return {'text':'','source_tokens':source_tokens,'target_tokens':target_tokens}
            outputs.append(out['text']);source_tokens+=out['source_tokens'];target_tokens+=out['target_tokens']
            if source_tokens>self.policy['max_source_tokens']:raise TranslationError('translation_input_limit','Combined clause token budget exceeded')
            if target_tokens>self.policy['max_target_tokens']:raise TranslationError('translation_output_limit','Combined clause output budget exceeded')
            # Check negation per independent clause, not only the whole paragraph.
            from smartvoice.domain.translation_policy import Prepared
            issues+=policy.inspect(part,out['text'],Prepared(part,(),()))
        target=(' ' if self.target_language=='en' else '').join(outputs)
        issues+=policy.inspect(prepared.text,target,prepared)
        return {'text':target,'source_tokens':source_tokens,'target_tokens':target_tokens,
                'translation_input':prepared.text,'normalizations':prepared.changes,'quality_issues':tuple(dict.fromkeys(issues))}
    def translate(self,text):
        return TranslationResult(**(self._quality(text) if getattr(self,'quality_policy',None) else self._translate(text)))
    def stream(self,text,on_text,cancelled):
        return TranslationResult(**(self._quality(text,on_text,cancelled) if getattr(self,'quality_policy',None) else self._stream(text,on_text,cancelled)))

    def close(self):
        if self.model is None:return
        self.model.unload_model()
        self.model=self.source=self.target=None
