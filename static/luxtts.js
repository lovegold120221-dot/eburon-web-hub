// Voicebox LuxTTS client adapter.
// Plays server-generated WAV chunks from the local Voicebox sidecar.
(function(){
  const state={
    chain:Promise.resolve(),
    currentAudio:null,
    stopToken:0,
    streams:new Map(),
    lastStreamAudioAt:0,
  };

  function _url(path){
    const rel=String(path||'').replace(/^\/+/,'');
    return new URL(rel,document.baseURI||location.href).href;
  }

  function _toast(message, ms){
    if(typeof showToast==='function') showToast(message, ms||3500);
    else console.warn(message);
  }

  function _providerEnabled(){
    try{ return localStorage.getItem('hermes-luxtts-enabled')!=='false'; }
    catch(_){ return true; }
  }

  function _realtimeEnabled(){
    try{
      const explicit=localStorage.getItem('hermes-luxtts-realtime');
      return explicit===null?true:explicit==='true';
    }catch(_){ return true; }
  }

  function _profileId(){
    try{ return localStorage.getItem('hermes-luxtts-profile-id')||''; }
    catch(_){ return ''; }
  }

  function _language(){
    try{
      if(typeof _locale!=='undefined'&&_locale&&_locale._speech) return _locale._speech;
      const saved=localStorage.getItem('hermes-lang')||localStorage.getItem('eburon-pref-language');
      return saved||navigator.language||'en';
    }catch(_){
      return 'en';
    }
  }

  function buildStartOptions(){
    const enabled=_providerEnabled()&&_realtimeEnabled();
    return {
      enabled,
      realtime:enabled,
      provider:'luxtts',
      engine:'luxtts',
      profile_id:_profileId(),
      language:_language(),
    };
  }

  function _stream(streamId){
    const id=String(streamId||'');
    if(!id) return null;
    if(!state.streams.has(id)){
      state.streams.set(id,{id,hadAudio:false,started:false,ended:false});
    }
    return state.streams.get(id);
  }

  function noteStreamStarted(streamId){
    _stream(streamId);
  }

  function _b64ToBlob(b64,mime){
    const binary=atob(String(b64||''));
    const bytes=new Uint8Array(binary.length);
    for(let i=0;i<binary.length;i++) bytes[i]=binary.charCodeAt(i);
    return new Blob([bytes],{type:mime||'audio/wav'});
  }

  function _playBlob(blob, opts={}){
    const token=state.stopToken;
    return new Promise(resolve=>{
      const audio=new Audio();
      const url=URL.createObjectURL(blob);
      let settled=false;
      const done=()=>{
        if(settled) return;
        settled=true;
        if(state.currentAudio===audio) state.currentAudio=null;
        URL.revokeObjectURL(url);
        if(opts.button) opts.button.dataset.speaking='0';
        if(typeof opts.onEnd==='function') opts.onEnd();
        resolve();
      };
      audio.preload='auto';
      audio.src=url;
      audio.onended=done;
      audio.onerror=done;
      state.currentAudio=audio;
      if(opts.button) opts.button.dataset.speaking='1';
      if(typeof opts.onStart==='function') opts.onStart();
      audio.play().catch(err=>{
        if(token===state.stopToken) _toast('LuxTTS playback blocked: '+(err&&err.message?err.message:err));
        done();
      });
    });
  }

  function stop(){
    state.stopToken++;
    if(state.currentAudio){
      try{ state.currentAudio.pause(); state.currentAudio.src=''; }catch(_){}
      state.currentAudio=null;
    }
    state.chain=Promise.resolve();
    document.querySelectorAll('[data-speaking="1"]').forEach(btn=>{ btn.dataset.speaking='0'; });
  }

  async function _errorText(res){
    const text=await res.text().catch(()=>'');
    try{
      const data=JSON.parse(text);
      return data.error||data.message||text||res.statusText;
    }catch(_){
      return text||res.statusText;
    }
  }

  function speak(text, opts={}){
    if(!_providerEnabled()) return Promise.reject(new Error('LuxTTS disabled'));
    const clean=String(text||'').trim();
    if(!clean) return Promise.resolve();
    stop();
    const payload={
      text:clean,
      profile_id:opts.profile_id||_profileId(),
      language:opts.language||_language(),
    };
    if(opts.button) opts.button.dataset.speaking='1';
    return fetch(_url('api/tts/speak'),{
      method:'POST',
      credentials:'include',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload),
    }).then(async res=>{
      if(!res.ok) throw new Error(await _errorText(res));
      return res.blob();
    }).then(blob=>_playBlob(blob, opts)).catch(err=>{
      if(opts.button) opts.button.dataset.speaking='0';
      _toast('LuxTTS failed: '+(err&&err.message?err.message:err),5000);
      throw err;
    });
  }

  function handleSseAudio(data, activeSid){
    if(!_providerEnabled()||!_realtimeEnabled()) return;
    if(data&&data.session_id&&activeSid&&data.session_id!==activeSid) return;
    if(!data||!data.audio_base64) return;
    const stream=_stream(data.stream_id);
    if(!stream) return;
    stream.hadAudio=true;
    state.lastStreamAudioAt=Date.now();
    const blob=_b64ToBlob(data.audio_base64,data.mime||'audio/wav');
    state.chain=state.chain.catch(()=>{}).then(()=>_playBlob(blob,{
      onStart:()=>{
        if(!stream.started){
          stream.started=true;
          if(typeof window._voiceModeOnLuxTtsStart==='function'){
            window._voiceModeOnLuxTtsStart(stream.id);
          }
        }
      },
    }));
  }

  function handleSseEnd(data){
    const stream=_stream(data&&data.stream_id);
    if(!stream) return;
    stream.ended=true;
    state.chain=state.chain.catch(()=>{}).then(()=>{
      if(stream.hadAudio&&typeof window._voiceModeOnLuxTtsComplete==='function'){
        window._voiceModeOnLuxTtsComplete(stream.id);
      }
      setTimeout(()=>state.streams.delete(stream.id),30000);
    });
  }

  function handleSseError(data){
    const msg=data&&data.message?String(data.message):'LuxTTS stream failed';
    console.warn('[luxtts]', msg);
  }

  function hadRecentStreamAudio(streamId){
    if(streamId&&state.streams.get(String(streamId))&&state.streams.get(String(streamId)).hadAudio) return true;
    return Date.now()-state.lastStreamAudioAt<45000;
  }

  window.HermesLuxTTS={
    buildStartOptions,
    noteStreamStarted,
    speak,
    stop,
    handleSseAudio,
    handleSseEnd,
    handleSseError,
    hadRecentStreamAudio,
    providerEnabled:_providerEnabled,
    realtimeEnabled:_realtimeEnabled,
  };
})();
