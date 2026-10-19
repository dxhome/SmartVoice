// Deterministic browser-client simulation for microphone streaming and stop/drain.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('src/smartvoice/web/streaming.html','utf8');
const source=html.split('<script>')[1].split('</script>')[0];
const noop=()=>{};
const elements=new Map();
function element(id){
 if(!elements.has(id))elements.set(id,{value:'',textContent:'',disabled:false,hidden:false,classList:{toggle:noop,add:noop,contains:()=>false},append:noop,replaceChildren:noop,setAttribute:noop,options:[]});
 return elements.get(id);
}
element('inputSource').value='microphone';element('language').value='en';element('glossary').value='[]';
element('file').files=[];
const localizedNodes=['fileInput','microphone','inputSource','microphoneHint','languageHint'].map(key=>({dataset:{message:key},textContent:''}));
const document={readyState:'loading',documentElement:{},addEventListener:noop,querySelector:()=>element('queryResult'),querySelectorAll:selector=>selector==='[data-message]'?localizedNodes:[],
 getElementById:element,createDocumentFragment:()=>({append:noop}),createElement:()=>({dataset:{},classList:{toggle:noop},append:noop})};
const server={frames:[],samples:0,sequence:0,finished:false,configure:null};
let processor,tracksStopped=0;
class AudioContext{
 constructor(){this.sampleRate=Number(process.env.MIC_SAMPLE_RATE||48000);this.currentTime=0;this.destination={}}
 async resume(){} async close(){}
 createMediaStreamSource(){return {connect:noop,disconnect:noop}}
 createScriptProcessor(){processor={connect:noop,disconnect:noop,onaudioprocess:null};return processor}
 createGain(){return {gain:{value:1},connect:noop,disconnect:noop}}
}
class Socket{
 static OPEN=1;
 constructor(){this.readyState=0;this.bufferedAmount=0;setTimeout(()=>{this.readyState=1;this.onopen()},0)}
 emit(row){queueMicrotask(()=>{if(this.readyState===1)this.onmessage({data:JSON.stringify(row)})})}
 send(data){
  if(typeof data!=='string'){
   const header=this.header;this.header=null;assert(header);assert.equal(data.byteLength%2,0);
   assert.equal(header.sequence,server.frames.length+1);assert.equal(header.start_sample,server.samples);
   server.frames.push(header.sequence);server.samples+=data.byteLength/2;
   this.emit({type:'input_ack',input_sequence:header.sequence});
   if(server.frames.length===1){this.emit({type:'source_unit_partial',event_sequence:++server.sequence,unit_id:'u1',revision:1,text:'Microphone input',committed:false});if(server.configure.mode!=='transcription')this.emit({type:'target_partial',event_sequence:++server.sequence,unit_id:'u1',revision:1,text:'Translated output',committed:false})}
   return;
  }
  const row=JSON.parse(data);
  if(row.type==='configure'){
   server.configure=row;
   this.emit({type:'session_ready',session_id:'mic-session',event_sequence:++server.sequence,
    heartbeat_interval_seconds:20,plan:{workflow_stages:['asr','formatting'],models:{asr:'mock-asr',formatting:'mock-punct'}}});
  }else if(row.type==='audio_frame')this.header=row;
  else if(row.type==='finish'){
   server.finished=true;
   this.emit({type:'session_complete',status:'complete',event_sequence:++server.sequence,counts:{source_unit_partial:1},
    profiling:{'asr.decode_stream':{count:4,p50_seconds:.02,p90_seconds:.04}}});
  }
 }
 close(){if(this.readyState===3)return;this.readyState=3;setTimeout(()=>this.onclose?.(),0)}
}
const stream={getTracks:()=>[{stop:()=>tracksStopped++}]};
const sandbox={document,location:{search:'',hash:'',protocol:'http:',host:'localhost'},URLSearchParams,performance,
 WebSocket:Socket,AudioContext,Float32Array,DataView,Math,JSON,Map,Array,Set,Promise,Error,ReferenceError,
 navigator:{mediaDevices:{getUserMedia:async()=>stream}},setTimeout,clearTimeout,setInterval,clearInterval,queueMicrotask,
 console,window:{addEventListener:noop},localStorage:{getItem:()=>null},AbortController,
 fetch:async()=>{throw Error('unexpected fetch')}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
assert.deepEqual(vm.runInContext("['zh','es','ja','ko'].flatMap(language=>Object.keys(messages.en).filter(key=>!(key in messages[language])))",sandbox),[],'every English UI message must have a translation in each supported locale');
for(const [language,fileText,micText] of [['en','Audio file','Microphone'],['zh','本地音频文件','麦克风'],['es','Archivo de audio','Micrófono'],['ja','音声ファイル','マイク'],['ko','오디오 파일','마이크']]){vm.runInContext(`applyLocale('${language}')`,sandbox);assert.equal(localizedNodes[0].textContent,fileText);assert.equal(localizedNodes[1].textContent,micText)}
const selectedMode=process.env.STREAMING_MODE||'transcription';
vm.runInContext(`selectMode(${JSON.stringify(selectedMode)});applyLocale('en');capabilitiesLoaded=true;capabilities={enabled:true,limits:{max_audio_seconds:0,max_sessions:2,max_input_queue_frames:250,estimated_model_memory_mib:8192},chains:['transcription','translated_subtitles','spoken_interpretation'].map(mode=>({mode,source_language:'en',available:true}))};start()`,sandbox);
const deadline=setTimeout(()=>{console.error('Microphone simulation timed out');process.exit(1)},5000);
(async()=>{
 const capture=setInterval(()=>{
  if(processor?.onaudioprocess)processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4096).fill(.1)}});
  if(server.frames.length>=5)vm.runInContext('stopMicrophoneCapture()',sandbox);
 },10);
 while(vm.runInContext('starting',sandbox))await new Promise(resolve=>setTimeout(resolve,20));
 clearInterval(capture);clearTimeout(deadline);
 assert(server.configure);assert.equal(server.configure.mode,selectedMode);assert.equal(server.configure.audio.sample_rate,16000);assert.equal(server.configure.audio.channels,1);
 assert.equal(server.configure.resumable,true);assert(server.frames.length>=5);assert(server.samples>=1600);
 assert(server.finished);assert.equal(tracksStopped,1);assert.equal(vm.runInContext('canceled',sandbox),false);
 assert.equal(element('state').textContent,'Session ended: complete');assert.notEqual(element('ready').textContent,'—');assert.notEqual(element('source-time').textContent,'—');assert.equal(element('target-time').textContent!=='—',selectedMode!=='transcription');assert.equal(element('audio-time').textContent,'—');if(selectedMode!=='transcription')assert.equal(vm.runInContext("targetRows.get('u1').text",sandbox),'Translated output');
 console.log(`Microphone stream completed cleanly in ${selectedMode} mode; selected task configured correctly and output routed as expected`);
})().catch(error=>{clearTimeout(deadline);console.error(error);process.exit(1)});
