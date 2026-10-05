"""Explicitly fetch a small pinned AISHELL-4 / AMI continuous regression corpus.

Raw assets, derived audio and reference text remain in the ignored local workspace.
This command is never run by application startup or CI.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import wave
import xml.etree.ElementTree as ET
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'src'))
from scripts.validation.stt_validation_common import fingerprint


def fetch(url, path, expected=None):
    import httpx
    if path.is_file() and (expected is None or fingerprint(path)==expected):return path
    path.parent.mkdir(parents=True,exist_ok=True)
    partial=path.with_suffix(path.suffix+'.part')
    with httpx.stream('GET',url,follow_redirects=True,timeout=120) as response:
        response.raise_for_status()
        with partial.open('wb') as target:
            for block in response.iter_bytes(1024*1024):target.write(block)
    if expected and fingerprint(partial)!=expected:raise ValueError('Source digest mismatch')
    partial.replace(path);return path


def textgrid(text):
    rows=[]
    for tier in re.split(r'(?:^|\n)\s*item \[\d+\]:',text)[1:]:
        speaker=re.search(r'name\s*=\s*"(.*?)"',tier)
        for match in re.finditer(r'intervals \[\d+\]:\s*xmin\s*=\s*([\d.]+)\s*xmax\s*=\s*([\d.]+)\s*text\s*=\s*"((?:[^"]|"")*)"',tier):
            start,end,content=match.groups(); content=content.replace('""','"').strip()
            # Corpus non-lexical markup is retained in a separate annotation,
            # never interpreted as transcribed words.
            lexical=re.sub(r'<[^>]*>|\[[^]]*\]','',content).strip()
            if lexical:rows.append({'start':float(start),'end':float(end),'speaker':speaker.group(1) if speaker else '', 'text':lexical})
    return sorted(rows,key=lambda r:(r['start'],r['end'],r['speaker']))


def ami_words(archive, recording):
    rows=[]
    for name in sorted(archive.namelist()):
        if '/words/' not in '/'+name or not Path(name).name.startswith(recording+'.'):continue
        speaker=Path(name).name.split('.')[1]
        for word in ET.fromstring(archive.read(name)).iter('w'):
            if 'starttime' not in word.attrib or 'endtime' not in word.attrib:continue
            if word.attrib.get('punc')=='true':continue
            content=''.join(word.itertext()).strip()
            if content:rows.append({'start':float(word.attrib['starttime']),'end':float(word.attrib['endtime']), 'speaker':speaker,'text':content})
    return sorted(rows,key=lambda r:(r['start'],r['end'],r['speaker']))


def overlap_seconds(rows, end):
    events=[]
    for index,row in enumerate(rows):
        left,right=max(0,row['start']),min(end,row['end'])
        speaker=row.get('speaker',index)
        if right>left:events.extend([(left,1,speaker),(right,-1,speaker)])
    total=0;active={};previous=0
    for at,change,speaker in sorted(events,key=lambda event:(event[0],event[1])):
        if sum(count>0 for count in active.values())>1:total+=at-previous
        active[speaker]=active.get(speaker,0)+change;previous=at
    return round(total,4)


def first_channel_pcm(path, seconds=590):
    import av
    import numpy as np
    blocks=[];count=0;limit=seconds*16000
    with av.open(str(path)) as source:
        resampler=av.AudioResampler(format='s16',layout='mono',rate=16000)
        for frame in source.decode(audio=0):
            # Explicit channel zero; do not average AISHELL's eight channels.
            data=frame.to_ndarray()
            if frame.layout.nb_channels>1:
                if not frame.format.is_planar:data=data.reshape(-1,frame.layout.nb_channels).T
                mono=av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:1]),format=frame.format.name if frame.format.is_planar else frame.format.name+'p',layout='mono')
                mono.sample_rate=frame.sample_rate
            else:mono=frame
            for converted in resampler.resample(mono):
                raw=converted.to_ndarray().reshape(-1).astype('<i2',copy=False)
                blocks.append(raw[:max(0,limit-count)].tobytes());count+=len(raw)
            if count>=limit:break
        if count<limit:
            for converted in resampler.resample(None):blocks.append(converted.to_ndarray().reshape(-1).astype('<i2',copy=False).tobytes())
    pcm=b''.join(blocks)[:limit*2]
    if len(pcm)<limit*2:raise ValueError('Recording shorter than 590 seconds')
    return pcm


def main():
    import httpx
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'sandbox/stt-continuous')
    parser.add_argument('--aishell-revision',default='aada72727856313b19d4a030383c426364931dbf')
    args=parser.parse_args();base=args.output;raw=base/'sources';raw.mkdir(parents=True,exist_ok=True)
    manifest={'schema':1,'cases':[],'sources':[], 'completed':False,
              'reference_rule':'start-sorted source annotations whose midpoint is within the prefix; crossing endpoint annotations explicitly marked; no human audit claimed'}
    try:
        api='https://huggingface.co/api/datasets/AISHELL/AISHELL-4'
        revision=args.aishell_revision
        tree=httpx.get(api+'/tree/'+revision+'/test?recursive=true&limit=1000',timeout=30).json()
        files={r['path']:r for r in tree if r['type']=='file'}
        ids=sorted(Path(p).stem for p in files if p.endswith('.flac'))
        sources=[]
        for identifier in ids:
            annotation_path=f'test/TextGrid/{identifier}.TextGrid'
            if annotation_path not in files:continue
            url=f'https://huggingface.co/datasets/AISHELL/AISHELL-4/resolve/{revision}/'
            annotation=fetch(url+annotation_path,raw/(identifier+'.TextGrid'))
            rows=textgrid(annotation.read_text())
            if not rows or max(r['end'] for r in rows)<590:continue
            asset=f'test/wav/{identifier}.flac'; expected=files[asset].get('lfs',{}).get('oid')
            audio=fetch(url+asset,raw/(identifier+'.flac'),expected)
            sources.append(('zh',identifier,audio,rows,{'dataset':'AISHELL-4','revision':revision,'license':'CC BY-SA 4.0','attribution':'Beijing AISHELL Technology / Fu et al., Interspeech 2021', 'source':url+asset,'annotation_sha256':fingerprint(annotation),'audio_sha256':fingerprint(audio),'channel':0}))
            if len(sources)==2:break
        if len(sources)!=2:raise RuntimeError('Need two complete AISHELL recordings')
        annotation_url='https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip'
        annotation=fetch(annotation_url,raw/'ami_public_manual_1.6.2.zip','b56e5babb2496b8795deeeda7e71178d7fbc9963f94276cf2a3f4b56ebbc9f9d')
        with zipfile.ZipFile(annotation) as archive:
            ids=sorted({Path(n).name.split('.')[0] for n in archive.namelist() if '/words/' in '/'+n and n.endswith('.words.xml')})
            selected=0
            for identifier in ids:
                rows=ami_words(archive,identifier)
                if not rows or max(r['end'] for r in rows)<590:continue
                url=f'https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{identifier}/audio/{identifier}.Mix-Headset.wav'
                try:audio=fetch(url,raw/(identifier+'.wav'))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code==404:continue
                    raise
                sources.append(('en',identifier,audio,rows,{'dataset':'AMI','revision':'manual-1.6.2','license':'CC BY 4.0','attribution':'AMI Consortium', 'source':url,'annotation_sha256':fingerprint(annotation),'audio_sha256':fingerprint(audio),'channel':'mono headset mix'}))
                selected+=1
                if selected==2:break
        if len(sources)!=4:raise RuntimeError('Need two complete AMI recordings')
        for language,identifier,audio,rows,metadata in sources:
            pcm=first_channel_pcm(audio); manifest['sources'].append({'id':identifier,**metadata})
            for duration in (75,300,590):
                selected=[r for r in rows if 0<=(r['start']+r['end'])/2<duration]
                crossing=[r for r in rows if r['start']<duration<r['end']]
                path=base/'audio'/f'{identifier}-{duration}.wav';path.parent.mkdir(exist_ok=True)
                with wave.open(str(path),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(pcm[:duration*32000])
                manifest['cases'].append({'id':f'{identifier}-{duration}','recording':identifier,'language':language,'group':'continuous','path':str(path.relative_to(base)), 'sha256':fingerprint(path),'duration':duration,'reference':' '.join(r['text'] for r in selected),'annotations':selected, 'overlap_seconds':overlap_seconds(rows,duration),'endpoint_crossing_annotations':len(crossing),'human_audit':'pending'})
            print(json.dumps({'recording':identifier,'language':language,'clips':[75,300,590]}),flush=True)
        manifest['completed']=True
    finally:(base/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
