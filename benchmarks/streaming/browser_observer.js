// QA-only observation injected by the loopback benchmark proxy.
(() => {
 let evidence, lastDOM='', currentRun=run, savedRun=-1;
 const fresh=()=>({user_agent:navigator.userAgent,mode,language:$('language').value,
    fixture_name:$('file').files[0]?.name,events:[],audio:[],playback:[],dom_updates:[],samples:[],errors:[]});
 evidence=fresh();
 const originalHandle=handle;
 handle=async function(message,received,token,generation){
  if(currentRun!==run){currentRun=run;evidence=fresh();lastDOM='';savedRun=-1}
  if(token===run&&generation===transportGeneration&&typeof message.data==='string'){
   const e=JSON.parse(message.data);
   if(e.type!=='input_ack'){
    const row={type:e.type,at:performance.now(),event_sequence:e.event_sequence,unit_id:e.unit_id,
      revision:e.revision,text:e.text,committed:e.committed,retracted:e.retracted,
      audio_sequence:e.audio_sequence,translation_id:e.translation_id,status:e.status,code:e.code};
    evidence.events.push(row);
    if(e.type==='audio_segment')evidence.audio.push({...row,chunk_index:e.chunk_index,chunk_count:e.chunk_count,
      target_text_start:e.target_text_start,target_text_end:e.target_text_end,audio_bytes:e.audio_bytes});
   }
  }
  return originalHandle(message,received,token,generation);
 };
 const originalSend=WebSocket.prototype.send;
 WebSocket.prototype.send=function(value){
  if(typeof value==='string'){
   try{const row=JSON.parse(value);if(['audio_started','audio_played','resume'].includes(row.type))
    evidence.playback.push({type:row.type,audio_sequence:row.audio_sequence,last_audio_played:row.last_audio_played,at:performance.now()});
   }catch(_){}
  }
  return originalSend.call(this,value);
 };
 const panel=document.createElement('div');panel.style='padding:12px;border:1px solid #aaa;margin:12px';
 panel.innerHTML='<strong>Local Browser QA</strong> <button id="qa-normal">Normal Network</button> <button id="qa-slow">Slow Network (48/64 KB/s, 80ms)</button> <button id="qa-disconnect">Disconnect Once</button> <button id="qa-disconnect-upload">Disconnect During Upload</button> <button id="qa-disconnect-audio">Disconnect During Playback</button> <button id="qa-save">Save Evidence</button> <span id="qa-state">Ready</span>';
 document.body.append(panel);
 for(const action of ['normal','slow','disconnect','disconnect-upload','disconnect-audio'])$('qa-'+action).onclick=async()=>{
   const value=await(await fetch('/qa/'+action,{method:'POST'})).json();$('qa-state').textContent=JSON.stringify(value);
 };
 async function save(){
  let diagnostics;try{diagnostics=JSON.parse($('diagnostics').textContent)}catch(_){diagnostics=$('diagnostics').textContent}
  const body={...evidence,diagnostics,final_dom:{source:[...document.querySelectorAll('.source-line')].map(e=>e.textContent),
     target:[...document.querySelectorAll('.translation-line')].map(e=>e.textContent)},state:$('state').textContent,
     last_audio_played:lastAudioPlayed,input_pending:inputPending.size};
  const result=await(await fetch('/evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  $('qa-state').textContent='Saved '+result.saved;savedRun=run;
 }
 $('qa-save').onclick=save;
 new MutationObserver(()=>{
  const dom=$('transcript').textContent;
  if(dom!==lastDOM){evidence.dom_updates.push({at:performance.now(),text:dom,
   source_drafts:document.querySelectorAll('.source-line.draft').length,target_drafts:document.querySelectorAll('.translation-line.draft').length});lastDOM=dom}
 }).observe($('transcript'),{childList:true,subtree:true,characterData:true});
 setInterval(()=>{
  if(currentRun!==run){currentRun=run;evidence=fresh();lastDOM='';savedRun=-1}
  if(starting)evidence.samples.push({at:performance.now(),visibility:document.visibilityState,
    audio_context:context?.state,pending_input:inputPending.size,active_nodes:activeNodes.length,last_audio_played:lastAudioPlayed});
  if(!starting&&inputStart!==null&&savedRun!==run&&/session_complete|"type": "error"/.test($('diagnostics').textContent)){
    savedRun=run;save().catch(error=>evidence.errors.push(String(error)));
  }
 },250);
})();
