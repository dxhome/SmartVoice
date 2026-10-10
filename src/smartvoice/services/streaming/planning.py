"""Resolve complete, immutable session plans from declared model capabilities."""
from dataclasses import dataclass
from .workflows import WORKFLOWS
import json
import math
from smartvoice.domain.streaming import StageError
from smartvoice.services.builtin_resources import read_builtin_json

MODES={'transcription':'transcription','translated_subtitles':'translation','spoken_interpretation':'speech'}
CONFIG=json.loads(read_builtin_json('streaming.json'))
if CONFIG.get('schema') != 'smartvoice.streaming.config.v1':
    raise RuntimeError('Unsupported streaming configuration schema')
for key,value in CONFIG['text_policy'].items():
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise RuntimeError('Invalid bounded streaming text policy: '+key)

@dataclass(frozen=True)
class ProcessingPlan:
    mode: str
    public_mode: str
    source_language: str
    target_language: str | None
    asr_model_id: str
    formatting_model_id: str
    translation_model_id: str | None
    tts_model_id: str | None
    asr_threads: int
    translation_threads: int=1
    formatting_threads: int=1
    tts_threads: int=2
    tts_fallback_model_id: str | None=None
    tts_fallback_speaker_id: int=3
    input_policy: str='none'
    text_policy: tuple=()
    translation_policy: tuple=()
    draft_interval_seconds: float=1.8
    source_preview: bool=True
    tts_speed: float=1.0
    tts_chunk_chars: int=100
    tts_speaker_id: int=0
    glossary: tuple=()
    include_source_text: bool=True
    output_consumption: str='delivery'
    decoder: str='greedy_search'
    context_terms: tuple=()
    @property
    def model_ids(self):
        return tuple(x for x in (self.asr_model_id,self.formatting_model_id,self.translation_model_id,self.tts_model_id,self.tts_fallback_model_id) if x)
    def public(self):
        return {'mode':self.public_mode,'source_language':self.source_language,'target_language':self.target_language,
                'models':{'asr':self.asr_model_id,'formatting':self.formatting_model_id,'translation':self.translation_model_id,'tts':self.tts_model_id,'tts_fallback':self.tts_fallback_model_id},
                'strategy':'online','native_asr_partial':True,'native_translation_streaming':False,'native_tts_streaming':False,
                'input_policy':self.input_policy,'tts_speed':self.tts_speed,'output_consumption':self.output_consumption,
                'workflow_stages':list(WORKFLOWS[self.mode].stages),
                'context':{'recent_sentences':3,'max_recent_chars':512,'max_terms':64,'versioned':True},
                'commitment':'acoustic-confirmed semantic units; drafts can be replaced; committed text is immutable'}

def resolve(config, repository, threads=1):
    required={'type','protocol','mode','source_language','audio'}
    optional={'target_language','models','include_source_text','output_consumption','glossary','ack_window','context'}
    if not isinstance(config,dict) or not required<=config.keys() or config.keys()-required-optional:
        raise StageError('invalid_config','Unexpected or missing session fields')
    if config['type']!='configure' or config['protocol']!='smartvoice.stream.v1':
        raise StageError('invalid_config','Expected smartvoice.stream.v1 configure')
    public_mode=config['mode'];lang=config['source_language'];target=config.get('target_language')
    if not isinstance(public_mode,str) or public_mode not in MODES or not isinstance(lang,str) or lang not in ('zh','en'):
        raise StageError('invalid_config','Expected a supported mode and explicit zh/en source language')
    mode=MODES[public_mode]
    audio=config['audio']
    if not isinstance(audio,dict) or type(audio.get('sample_rate')) is not int or type(audio.get('channels')) is not int or audio!={'encoding':'pcm_s16le','sample_rate':16000,'channels':1}:
        raise StageError('invalid_audio_format','Expected 16 kHz mono PCM16')
    if mode!='transcription' and (lang,target) not in (('zh','en'),('en','zh')):
        raise StageError('unsupported_language_pair','Expected zh to en or en to zh')
    if mode=='transcription' and target is not None:
        raise StageError('invalid_config','Transcription has no target language')
    overrides=config.get('models',{})
    if not isinstance(overrides,dict) or overrides.keys()-{'asr','formatting','translation','tts'}:
        raise StageError('invalid_config','Unknown model selector')
    enabled={'asr','formatting'}|({'translation'} if mode!='transcription' else set())|({'tts'} if mode=='speech' else set())
    if overrides.keys()-enabled:raise StageError('invalid_config','Model selected for an inactive stage')
    defaults=CONFIG['defaults'];ids={
        'asr':overrides.get('asr',defaults['asr'][lang]),
        'formatting':overrides.get('formatting',defaults['formatting']),
        'translation':overrides.get('translation',defaults['translation'].get(lang+':'+str(target))) if mode!='transcription' else None,
        'tts':overrides.get('tts',defaults['tts'].get(target)) if mode=='speech' else None}
    specs={};resident=0
    for stage,mid in ids.items():
        if mid is None:
            if stage in enabled:raise StageError('invalid_config','An active stage requires a model ID')
            continue
        if not isinstance(mid,str):raise StageError('invalid_config','Model IDs must be strings')
        try:spec=repository.get_spec(mid)
        except Exception as exc:raise StageError('unsupported_model','Unknown model selection') from exc
        if stage=='tts':
            if spec.task!='speech' or (spec.streaming or {}).get('stage')!='tts' or target not in spec.languages:raise StageError('incompatible_model','TTS model language mismatch')
            resident+=spec.streaming['resident_mib']
        else:
            cap=spec.streaming or {}
            if cap.get('stage')!=stage or lang not in spec.languages:raise StageError('incompatible_model','Model does not support the selected stage/language')
            if stage=='asr' and not cap.get('native_partial'):raise StageError('incompatible_model','An online ASR model is required')
            if stage=='translation' and [lang,target] not in cap.get('pairs',[]):raise StageError('incompatible_model','Translation model direction mismatch')
            resident+=cap['resident_mib']
        ids[stage]=spec.id
        specs[stage]=spec
    fallback = CONFIG['tts'].get(target, {}).get('fallback_model_id') if mode == 'speech' and specs['tts'].streaming.get('requires_mixed_script_fallback') else None
    if fallback:
        spec = repository.get_spec(fallback)
        if spec.task != 'speech' or target not in spec.languages:
            raise StageError('incompatible_model', 'Fallback language mismatch')
        specs['tts_fallback'] = spec
        resident += (spec.streaming or {}).get('resident_mib',800)
    from smartvoice.domain.translation_policy import validate_glossary
    glossary=validate_glossary(config.get('glossary'))
    context=config.get('context',{})
    if not isinstance(context,dict) or context.keys()-{'terms'}:
        raise StageError('invalid_config','Context accepts an explicit terms list')
    terms=context.get('terms',[])
    if not isinstance(terms,list) or len(terms)>64 or any(not isinstance(t,str) or not t.strip() or len(t)>64 or any(ord(c)<32 for c in t) for t in terms):
        raise StageError('invalid_config','Expected at most 64 nonempty context terms of at most 64 characters')
    terms=tuple(dict.fromkeys(t.strip() for t in terms))
    ack=config.get('ack_window',16)
    source=config.get('include_source_text',True);consumption=config.get('output_consumption','delivery')
    if type(ack) is not int or not 1<=ack<=32 or type(source) is not bool or consumption not in ('delivery','playback'):
        raise StageError('invalid_config','Invalid output options')
    if mode!='speech' and consumption!='delivery':raise StageError('invalid_config','Playback consumption requires speech mode')
    mt=specs['translation'].streaming if 'translation' in specs else {}
    tts=CONFIG['tts'].get(target,{})
    text=dict(CONFIG['text_policy']);policy={'eager_partial_preview':mode=='translation',**{k:mt[k] for k in ('draft_min_chars','draft_stable_seconds') if k in mt}}
    if mode=='translation':
        text['min_draft_chars']=mt.get('draft_min_chars',text['min_draft_chars']);text['stable_seconds']=mt.get('draft_stable_seconds',text['stable_seconds'])
    plan=ProcessingPlan(mode,public_mode,lang,target,ids['asr'],ids['formatting'],ids['translation'],ids['tts'],threads,
        input_policy=specs['asr'].streaming.get('input_policy','none'),text_policy=tuple(text.items()),translation_policy=tuple(policy.items()),
        draft_interval_seconds=mt.get('draft_interval_seconds',1.8),tts_speed=tts.get('speed',1.0),tts_chunk_chars=tts.get('max_chunk_chars',100),
        tts_speaker_id=tts.get('speaker_id',0),tts_fallback_model_id=fallback,tts_fallback_speaker_id=tts.get('fallback_speaker_id',3),glossary=glossary,context_terms=terms,include_source_text=source,output_consumption=consumption)
    return plan,specs,resident
