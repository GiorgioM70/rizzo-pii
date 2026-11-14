const $ = id => document.getElementById(id);
/* ---- token di sessione (protezione del server locale, vedi local_guard.py) ----
   Arriva dentro la pagina e va rimandato a ogni chiamata all'API: senza, il server
   risponde 403. fetch() verso il proprio server lo porta nell'header; <img> e i
   download di /doc/... non possono, quindi lo portano in query (docUrl). */
const PII_TOKEN = (document.querySelector('meta[name="pii-token"]')||{}).content||'';
const _fetch = window.fetch.bind(window);
window.fetch = (url, opts={}) => {
  const same = typeof url==='string' && url.startsWith('/');
  if(!same) return _fetch(url, opts);
  const h = new Headers(opts.headers||{});
  h.set('X-PII-Token', PII_TOKEN);
  return _fetch(url, {...opts, headers:h});
};
const docUrl = p => p+(p.includes('?')?'&':'?')+'t='+encodeURIComponent(PII_TOKEN);
let DATA = null;            // ultimo risultato analyze
let MAP = {};              // {placeholder -> valore} sessione corrente
const off = new Set();     // label nascoste nella preview
let L = 'it';              // lingua UI corrente
let TAGS = [];             // legenda dei tag servita da /settings
let EXCL = new Set();      // tag da NON anonimizzare (scelta corrente)
let TAGS_LOADED = false;   // /settings ha risposto -> possiamo mandare gli override
let MAPPING = true;        // dizionario reversibile on/off (switch nella card di input)
let SRC_DOC = null;        // PDF caricato, renderizzato dal server (colonna sinistra)
let OUT_DOC = null;        // PDF anonimizzato (colonna destra), generato pigramente
let VIEW = 'prev';         // vista attiva a destra: prev | text | pdf

/* ---- i18n (IT default, EN opzionale) ---- */
const T = {
 it:{
  tagline:"modello locale su CPU · GDPR compliant", badge:"100% in locale",
  notice:"<b>Versione in sviluppo.</b> Il modello AI non è perfetto e può commettere errori: verifica sempre il risultato prima di usarlo. Queste sono le prime versioni e il progetto è completamente <b>open source</b>. Se ti è utile, <b>lascia una ⭐ alla repo</b> e contribuisci a migliorarlo: <a href=\"https://github.com/Rizzo-AI-Academy/rizzo-pii\" target=\"_blank\" rel=\"noopener\">apri la repo su GitHub ↗</a>",
  tab1:"Anonimizza", tab2:"Ripristina la risposta",
  in_title:"① Il tuo documento", in_hint:"incolla testo o trascina un PDF / .md",
  src_ph:"Incolla qui il testo dell'atto, del contratto o della sentenza…\n\nOppure trascina un PDF o un file .md nell'area qui sotto.",
  drop:"Trascina un <b>PDF</b> o un <b>.md</b> qui, oppure <b>scegli un file</b>",
  go:"Anonimizza", clear:"Pulisci",
  out_title:"② Risultato", v_prev:"Anteprima", v_text:"Testo da copiare",
  v_pdf:"Anteprima PDF", v_raw:"Testo", v_opdf:"PDF censurato",
  t_prev_loading:"Renderizzo il PDF…", t_prev_err:"Anteprima del PDF non disponibile",
  t_pdf_render:"Genero il PDF anonimizzato…",
  t_pdf_locked:"PDF protetto da password: rimuovi la protezione e riprova",
  t_zoom_out:"Riduci zoom", t_zoom_in:"Aumenta zoom", t_zoom_reset:"Reimposta zoom",
  empty_prev:"L'anteprima con le PII evidenziate apparirà qui.",
  anon_ph:"Il testo anonimizzato apparirà qui.",
  copy:"Copia per ChatGPT", dl:"Scarica dizionario", dlpdf:"Scarica PDF anonimizzato",
  t_pdf_making:"Genero il PDF…", t_pdf_ok:"PDF anonimizzato scaricato",
  t_pdf_err:"Errore nella creazione del PDF",
  t_pdf_warn:(r,s)=>"PDF scaricato · ATTENZIONE: "+(r+s)+" valori sono rimasti in chiaro"
    +" ("+r+" non redatti, "+s+" troppo corti per essere cercati): controlla il file"
    +" prima di condividerlo",
  dict_title:"Dizionario reversibile", dict_hint:"resta solo qui, in locale",
  th_id:"ID", th_val:"Valore originale", th_type:"Tipo",
  callout:"Incolla qui la <b>risposta dell'LLM</b> (che contiene i placeholder come <span class=\"kbd\">[FULLNAME_1]</span>): l'app rimette i valori veri usando il dizionario di questa sessione. Se hai chiuso e riaperto l'app, <b>carica il dizionario .json</b> che avevi salvato.",
  r_title1:"Risposta con i placeholder", loaddict:"📁 Carica dizionario",
  rin_ph:"Incolla qui la risposta di ChatGPT…",
  rev:"Ripristina valori", r_title2:"Testo ripristinato",
  empty_rout:"Il testo con i valori reali apparirà qui.", rcopy:"Copia testo ripristinato",
  st_ent:"entità", st_uniq:"valori unici", st_model:"dal modello",
  st_regex:"da regex/checksum", st_chars:"caratteri", analyzing:"Analizzo…",
  t_need_input:"Inserisci del testo o un PDF", t_error:"Errore",
  t_copied:"Testo anonimizzato copiato", t_need_anon:"Prima anonimizza un testo",
  t_nothing_dl:"Niente da scaricare", t_dl_ok:"Dizionario scaricato",
  t_paste_restore:"Incolla la risposta da ripristinare",
  t_no_dict:"Nessun dizionario: caricane uno .json", t_restored:"Valori ripristinati",
  t_nothing_copy:"Niente da copiare", t_restored_copied:"Testo ripristinato copiato",
  t_dict_loaded:"Dizionario caricato", t_json_invalid:"JSON non valido",
  t_drag_pdf:"Formato non supportato: usa PDF, .md o .txt",
  pii_found:(n,u)=>n+" PII trovate · "+u+" valori unici",
  dict_n:n=>n+" ID nel dizionario",
  dict_loaded_n:n=>"dizionario caricato · "+n+" ID",
  dict_session:n=>"dizionario sessione · "+n+" ID",
  chars:n=>n.toLocaleString('it'),
  cfg_title:"Configurazione server", cfg_host:"Indirizzo", cfg_port:"Porta",
  cfg_check:"Verifica porta", cfg_save:"Salva", cfg_cancel:"Annulla",
  cfg_available:"Porta disponibile ✓", cfg_in_use:"Porta occupata ✗",
  cfg_saved:"Configurazione salvata (riavvia per applicare)",
  cfg_expose_confirm:h=>`Con l'indirizzo ${h} il server e i documenti diventano raggiungibili da altri computer della rete, senza cifratura. Confermi?`,
  cfg_restart_note:"Le modifiche avranno effetto al prossimo avvio.",
  tg_title:"Tag da anonimizzare",
  tg_sub:"Deseleziona i tipi che vuoi <b>lasciare in chiaro</b>: verranno comunque rilevati, ma non sostituiti da un placeholder.",
  tg_all:"Seleziona tutti", tg_none:"Nessuno",
  tg_saved:"Selezione salvata", tg_err:"Salvataggio non riuscito",
  tg_note:p=>"Salvato in "+p+" · vale anche per l'API /analyze.",
  tg_env:"⚠️ La variabile d'ambiente PII_EXCLUDE_TAGS ha la precedenza al prossimo avvio.",
  tg_count:(on,tot)=>on+" di "+tot+" tag anonimizzati",
  st_excl:"tag esclusi",
  map_ttl:"Dizionario reversibile", map_on:"ATTIVO", map_off:"DISATTIVO",
  map_sub_on:"Ogni PII riceve un ID: potrai ripristinare i valori veri dalla risposta dell'LLM.",
  map_sub_off:"Anonimizzazione <b>definitiva</b>: nessuna chiave placeholder → valore viene creata, salvata o scaricabile. Il ripristino non sarà possibile.",
  t_map_on:"Dizionario reversibile attivo", t_map_off:"Dizionario disattivato: anonimizzazione definitiva",
  dict_off:"Nessun dizionario: l'anonimizzazione di questo testo è <b>definitiva</b>.",
  dict_off_hint:"lo switch è su DISATTIVO",
  r_off:"Lo switch <b>Dizionario reversibile</b> è su DISATTIVO: le nuove anonimizzazioni non producono chiavi. Qui puoi comunque ripristinare con un dizionario <b>.json salvato in precedenza</b>.",
 },
 en:{
  tagline:"local model on CPU · GDPR compliant", badge:"100% local",
  notice:"<b>Work in progress.</b> The AI model isn't perfect and can make mistakes: always double-check the result before relying on it. These are the very first versions and the project is fully <b>open source</b>. If you find it useful, <b>leave a ⭐ on the repo</b> and help improve it: <a href=\"https://github.com/Rizzo-AI-Academy/rizzo-pii\" target=\"_blank\" rel=\"noopener\">open the repo on GitHub ↗</a>",
  tab1:"Anonymize", tab2:"Restore the answer",
  in_title:"① Your document", in_hint:"paste text or drop a PDF / .md",
  src_ph:"Paste here the text of the deed, contract or judgment…\n\nOr drop a PDF or a .md file onto the area below.",
  drop:"Drop a <b>PDF</b> or a <b>.md</b> here, or <b>choose a file</b>",
  go:"Anonymize", clear:"Clear",
  out_title:"② Result", v_prev:"Preview", v_text:"Text to copy",
  v_pdf:"PDF preview", v_raw:"Text", v_opdf:"Redacted PDF",
  t_prev_loading:"Rendering the PDF…", t_prev_err:"PDF preview not available",
  t_pdf_render:"Building the anonymized PDF…",
  t_pdf_locked:"Password-protected PDF: remove the protection and try again",
  t_zoom_out:"Zoom out", t_zoom_in:"Zoom in", t_zoom_reset:"Reset zoom",
  empty_prev:"The preview with highlighted PII will appear here.",
  anon_ph:"The anonymized text will appear here.",
  copy:"Copy for ChatGPT", dl:"Download dictionary", dlpdf:"Download anonymized PDF",
  t_pdf_making:"Building the PDF…", t_pdf_ok:"Anonymized PDF downloaded",
  t_pdf_err:"Error while creating the PDF",
  t_pdf_warn:(r,s)=>"PDF downloaded · WARNING: "+(r+s)+" values were left in clear"
    +" ("+r+" not redacted, "+s+" too short to be searched safely): check the file"
    +" before sharing it",
  dict_title:"Reversible dictionary", dict_hint:"stays here only, locally",
  th_id:"ID", th_val:"Original value", th_type:"Type",
  callout:"Paste here the <b>LLM's answer</b> (containing placeholders like <span class=\"kbd\">[FULLNAME_1]</span>): the app puts the real values back using this session's dictionary. If you closed and reopened the app, <b>load the .json dictionary</b> you saved.",
  r_title1:"Answer with placeholders", loaddict:"📁 Load dictionary",
  rin_ph:"Paste ChatGPT's answer here…",
  rev:"Restore values", r_title2:"Restored text",
  empty_rout:"The text with the real values will appear here.", rcopy:"Copy restored text",
  st_ent:"entities", st_uniq:"unique values", st_model:"from the model",
  st_regex:"from regex/checksum", st_chars:"characters", analyzing:"Analyzing…",
  t_need_input:"Enter some text or a PDF", t_error:"Error",
  t_copied:"Anonymized text copied", t_need_anon:"Anonymize a text first",
  t_nothing_dl:"Nothing to download", t_dl_ok:"Dictionary downloaded",
  t_paste_restore:"Paste the answer to restore",
  t_no_dict:"No dictionary: load a .json one", t_restored:"Values restored",
  t_nothing_copy:"Nothing to copy", t_restored_copied:"Restored text copied",
  t_dict_loaded:"Dictionary loaded", t_json_invalid:"Invalid JSON",
  t_drag_pdf:"Unsupported format: use PDF, .md or .txt",
  pii_found:(n,u)=>n+" PII found · "+u+" unique values",
  dict_n:n=>n+" IDs in the dictionary",
  dict_loaded_n:n=>"dictionary loaded · "+n+" IDs",
  dict_session:n=>"session dictionary · "+n+" IDs",
  chars:n=>n.toLocaleString('en'),
  cfg_title:"Server configuration", cfg_host:"Host", cfg_port:"Port",
  cfg_check:"Check port", cfg_save:"Save", cfg_cancel:"Cancel",
  cfg_available:"Port available ✓", cfg_in_use:"Port in use ✗",
  cfg_saved:"Config saved (restart to apply)",
  cfg_expose_confirm:h=>`With address ${h} the server and your documents become reachable from other computers on the network, unencrypted. Confirm?`,
  cfg_restart_note:"Changes take effect on next startup.",
  tg_title:"Tags to anonymize",
  tg_sub:"Untick the types you want to <b>leave in clear text</b>: they are still detected, but not replaced by a placeholder.",
  tg_all:"Select all", tg_none:"None",
  tg_saved:"Selection saved", tg_err:"Could not save",
  tg_note:p=>"Saved to "+p+" · also applies to the /analyze API.",
  tg_env:"⚠️ The PII_EXCLUDE_TAGS environment variable takes precedence on next startup.",
  tg_count:(on,tot)=>on+" of "+tot+" tags anonymized",
  st_excl:"excluded tags",
  map_ttl:"Reversible dictionary", map_on:"ON", map_off:"OFF",
  map_sub_on:"Every PII gets an ID: you will be able to restore the real values from the LLM's answer.",
  map_sub_off:"<b>Irreversible</b> anonymization: no placeholder → value key is created, stored or downloadable. Restoring will not be possible.",
  t_map_on:"Reversible dictionary on", t_map_off:"Dictionary off: anonymization is irreversible",
  dict_off:"No dictionary: anonymization of this text is <b>irreversible</b>.",
  dict_off_hint:"the switch is OFF",
  r_off:"The <b>Reversible dictionary</b> switch is OFF: new anonymizations produce no keys. You can still restore here using a <b>.json dictionary saved earlier</b>.",
 }
};
const tt=k=>T[L][k];

function routEmpty(){
  return '<div class="empty"><img src="/assets/mascot_doc.png" alt="" onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),{className:\'big\',textContent:\'🔓\'}))"><div>'+tt('empty_rout')+'</div></div>';
}

function applyLang(l){
  L=(l==='en')?'en':'it';
  localStorage.setItem('pii_lang',L);
  document.documentElement.lang=L;$('lang').value=L;
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    const v=T[L][el.getAttribute('data-i18n')]; if(v!=null) el.innerHTML=v;});
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{
    const v=T[L][el.getAttribute('data-i18n-ph')]; if(v!=null) el.placeholder=v;});
  if(!$('pdf').files.length) $('dropTxt').innerHTML=tt('drop');   // dropzone: solo se nessun file
  if(!$('rout')._raw) $('rout').innerHTML=routEmpty();
  renderMapping();
  if(TAGS.length && $('tagsOverlay').classList.contains('open')) renderTags();
  if(DATA) render();
}

/* ---- scroll sincronizzato editor (sx) <-> anteprima/testo (dx) ---- */
let syncing=false;
function matchScroll(target,from){
  const rf=from.scrollHeight-from.clientHeight, rt=target.scrollHeight-target.clientHeight;
  target.scrollTop = rf>0 ? (from.scrollTop/rf)*rt : 0;
}
function linkScroll(a,b){
  a.addEventListener('scroll',()=>{
    if(syncing)return; syncing=true; matchScroll(b,a);
    setTimeout(()=>syncing=false,0);});   // reset robusto (rAF puo' non scattare in bg)
}

/* ---- colore deterministico per tipo di tag ---- */
function hue(s){let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))%360;return h;}
function colors(label){const h=hue(label);
  return {bg:`hsl(${h} 56% 96%)`,bd:`hsl(${h} 42% 81%)`,tx:`hsl(${h} 40% 38%)`};}

function toast(msg,ok=true,ms=1800){const t=$('toast');t.textContent=msg;t.className='show'+(ok?' ok':'');
  clearTimeout(t._t);t._t=setTimeout(()=>t.className='',ms);}

/* ---- render di un PDF a video ----------------------------------------------
   Le pagine arrivano gia' renderizzate dal server (/doc/<id>/page/<n>.png):
   niente viewer PDF del browser (dentro Tauri non c'e' garanzia che ci sia) e
   niente libreria JS esterna (l'app e' offline, nessuna CDN raggiungibile).
   `loading=lazy` -> un PDF di 200 pagine non scarica 200 immagini all'apertura. */
function renderPages(el,doc){
  el.innerHTML='';
  el.appendChild(zoomBar(el));
  for(let i=0;i<doc.n_pages;i++){
    const pg=document.createElement('div');pg.className='pg';
    const im=document.createElement('img');
    im.loading='lazy';im.alt=(i+1)+' / '+doc.n_pages;   // alt = pagina: se il render manca si vede quale
    im.src=docUrl(`/doc/${doc.doc_id}/page/${i}.png`);
    pg.appendChild(im);
    const n=document.createElement('span');n.className='pgn';
    n.textContent=(i+1)+' / '+doc.n_pages;pg.appendChild(n);
    el.appendChild(pg);
  }
  el.scrollTop=0;
}

/* barra zoom (issue #92): +/- e reset sulla %, piu' ctrl/cmd+rotellina sull'intera vista.
   Il livello vive in --z su el stesso, letto da .pdfview .pg{width:...} in CSS; si azzera
   a ogni nuovo rendering (nuovo documento = vista pulita, come per gli altri stati del pane). */
function zoomBar(el){
  const bar=document.createElement('div');bar.className='zoomctl';
  const pill=document.createElement('div');pill.className='pill';
  const out=document.createElement('button');out.type='button';out.textContent='−';out.title=tt('t_zoom_out');
  const pct=document.createElement('span');pct.className='zpct';pct.title=tt('t_zoom_reset');
  const inn=document.createElement('button');inn.type='button';inn.textContent='+';inn.title=tt('t_zoom_in');
  const setZ=z=>{z=Math.max(.5,Math.min(3,z));el.style.setProperty('--z',z);pct.textContent=Math.round(z*100)+'%';};
  const curZ=()=>parseFloat(el.style.getPropertyValue('--z'))||1;
  out.onclick=()=>setZ(curZ()-.25);
  inn.onclick=()=>setZ(curZ()+.25);
  pct.onclick=()=>setZ(1);
  el.onwheel=e=>{if(!e.ctrlKey&&!e.metaKey)return;e.preventDefault();setZ(curZ()+(e.deltaY<0?.1:-.1));};
  pill.append(out,pct,inn);bar.appendChild(pill);
  setZ(1);
  return bar;
}
function busy(el,msg){
  el.innerHTML='<div class="pdfbusy"><span class="spin"></span>'+msg+'</div>';}

/* ---- tabs ---- */
function showTab(n){
  $('tab1').classList.toggle('on',n===1);$('tab2').classList.toggle('on',n===2);
  $('pane1').classList.toggle('on',n===1);$('pane2').classList.toggle('on',n===2);
}

/* ---- documento caricato: anteprima renderizzata (sx) ----------------------
   Appena si sceglie un PDF lo si manda a /preview: il server lo tiene in memoria,
   ne ritorna il testo estratto e le pagine da renderizzare. Da li' in poi la
   colonna sinistra ha due viste, "Anteprima PDF" (default) e "Testo".
   Per .md/.txt non c'e' niente da renderizzare: resta la sola textarea. */
function setSrcView(v){
  const p=(v==='pdf'&&SRC_DOC);
  $('sPdf').classList.toggle('on',!!p);$('sText').classList.toggle('on',!p);
  $('pdfSrcView').style.display=p?'':'none';
  $('src').style.display=p?'none':'';
}

async function onFile(f){
  $('dropTxt').innerHTML='📎 <b>'+escapeHtml(f.name)+'</b>';
  SRC_DOC=null;$('srcTabs').style.display='none';$('inHint').style.display='';
  setSrcView('text');
  if(!(f.type==='application/pdf'||/\.pdf$/i.test(f.name)))return;
  $('inHint').textContent=tt('t_prev_loading');
  busy($('pdfSrcView'),tt('t_prev_loading'));
  $('srcTabs').style.display='';$('pdfSrcView').style.display='';$('src').style.display='none';
  $('sPdf').classList.add('on');$('sText').classList.remove('on');
  try{
    const fd=new FormData();fd.append('pdf',f);
    const r=await fetch('/preview',{method:'POST',body:fd});
    const d=await r.json();
    if(d.code==='pdf_locked'){        // non si legge: niente da anonimizzare, via il file
      SRC_DOC=null;$('srcTabs').style.display='none';setSrcView('text');
      $('pdf').value='';$('dropTxt').innerHTML=tt('drop');
      $('inHint').innerHTML=tt('in_hint');$('inHint').style.display='';
      toast(tt('t_pdf_locked'),false,4000);return;
    }
    if(!r.ok)throw new Error(d.error||'');
    SRC_DOC=d;$('src').value=d.text||'';
    renderPages($('pdfSrcView'),d);
    $('inHint').style.display='none';
  }catch(e){
    SRC_DOC=null;$('srcTabs').style.display='none';setSrcView('text');
    $('inHint').innerHTML=tt('in_hint');$('inHint').style.display='';
    toast(tt('t_prev_err'),false);          // il file resta valido: l'analisi funziona lo stesso
  }
}

/* ---- analyze ---- */
async function run(){
  const file=$('pdf').files[0];const text=$('src').value.trim();
  if(!file&&!text){toast(tt('t_need_input'),false);return;}
  $('go').disabled=true;const old=$('go').innerHTML;
  $('go').innerHTML='<span class="spin"></span> '+tt('analyzing');
  try{
    let resp;
    // override della UI. Se /settings non ha risposto non mandiamo nulla: comanda il server.
    const excl=TAGS_LOADED?[...EXCL]:null;
    if(file){const fd=new FormData();fd.append('pdf',file);
      if(excl){fd.append('exclude_tags',excl.join(','));fd.append('include_mapping',MAPPING?'1':'0');}
      resp=await fetch('/analyze',{method:'POST',body:fd});}
    else{const body={text};if(excl){body.exclude_tags=excl;body.include_mapping=MAPPING;}
      resp=await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)});}
    const d=await resp.json();
    if(!resp.ok){toast(d.error||tt('t_error'),false);return;}
    if(d.source_text&&file)$('src').value=d.source_text;
    DATA=d;off.clear();
    // senza dizionario non tocchiamo MAP ne' il localStorage: nessuna chiave nuova nasce
    if(d.mapping_enabled!==false){MAP=d.mapping;localStorage.setItem('pii_map',JSON.stringify(MAP));}
    render();
    toast(T[L].pii_found(d.n_entities,d.n_unique));
  }catch(e){toast(tt('t_error')+': '+e.message,false);}
  finally{$('go').disabled=false;$('go').innerHTML=old;}
}

function render(){
  const d=DATA;
  OUT_DOC=null;$('pdfOutView').innerHTML='';  // risultato nuovo -> il PDF censurato va rifatto
  $('dictCard').style.display='';            // mostra la card dizionario (sotto le due colonne)
  document.querySelector('.app').classList.add('has-result');  // -> scroll pagina, niente schiacciamento
  // preview evidenziata
  const prev=$('prev');prev.innerHTML='';prev.style.display='';$('emptyPrev').style.display='none';
  for(const s of d.segments){
    if(s.label){
      const c=colors(s.label);const sp=document.createElement('span');
      sp.className='ph'+(off.has(s.label)?' dim':'');
      sp.style.background=c.bg;sp.style.borderColor=c.bd;sp.style.color=c.tx;
      // senza dizionario il server non manda il valore originale: niente da mostrare al passaggio
      sp.title=(s.t?s.t+'\n':'')+`(${s.src}${s.validated?' · checksum ✓':''})`;
      sp.innerHTML=s.ph.replace(/[\[\]]/g,'')+(s.validated?'<span class="ck">✓</span>':'');
      prev.appendChild(sp);
    }else prev.appendChild(document.createTextNode(s.t));
  }
  // testo da copiare
  $('anon').value=d.anonymized_text;
  // meta
  $('meta').innerHTML=
    `<span class="stat"><b>${d.n_entities}</b> ${tt('st_ent')}</span>`+
    `<span class="stat"><b>${d.n_unique}</b> ${tt('st_uniq')}</span>`+
    `<span class="stat"><b>${(d.by_source.modello||0)}</b> ${tt('st_model')}</span>`+
    `<span class="stat"><b>${(d.by_source.regex||0)}</b> ${tt('st_regex')}</span>`+
    `<span class="stat"><b>${T[L].chars(d.n_chars)}</b> ${tt('st_chars')}</span>`+
    ((d.excluded_tags&&d.excluded_tags.length)
      ? `<span class="stat" title="${d.excluded_tags.join(', ')}"><b>${d.excluded_tags.length}</b> ${tt('st_excl')}</span>` : '');
  // legenda cliccabile (toggle highlight)
  const lg=$('legend');lg.innerHTML='';
  for(const [k,v] of Object.entries(d.by_label)){
    const c=colors(k);const el=document.createElement('span');
    el.className='chip'+(off.has(k)?' off':'');
    el.innerHTML=`<span class="sw" style="background:${c.bd}"></span>${k}<span class="n">${v}</span>`;
    el.onclick=()=>{off.has(k)?off.delete(k):off.add(k);render();};
    lg.appendChild(el);
  }
  // dizionario (assente quando lo switch e' su DISATTIVO)
  const hasMap=d.mapping_enabled!==false;
  const rows=$('maprows');rows.innerHTML='';
  const keys=hasMap?Object.keys(d.mapping):[];
  $('tablewrap').style.display=keys.length?'':'none';
  $('dictNone').style.display=hasMap?'none':'';
  $('dictHint').innerHTML=hasMap?tt('dict_hint'):tt('dict_off_hint');
  $('dl').style.display=hasMap?'':'none';
  for(const ph of keys){const lab=ph.slice(1,ph.lastIndexOf('_'));const c=colors(lab);
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="k" style="color:${c.tx}">${ph}</td>`+
      `<td class="v">${escapeHtml(d.mapping[ph])}</td>`+
      `<td><span class="chip" style="cursor:default"><span class="sw" style="background:${c.bd}"></span>${lab}</span></td>`;
    rows.appendChild(tr);}
  $('ulock').textContent=keys.length?T[L].dict_n(keys.length):'';
  if(VIEW==='pdf')setView('pdf');            // stavo guardando il PDF: lo rigenero
}

function escapeHtml(s){return s.replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));}

/* ---- view toggle (dx): anteprima con i tag | testo da copiare | PDF censurato ---- */
async function setView(v){
  if(v==='pdf'&&!DATA&&!$('pdf').files.length&&!$('src').value.trim()){
    toast(tt('t_need_anon'),false);return;}
  VIEW=v;
  $('vPrev').classList.toggle('on',v==='prev');
  $('vText').classList.toggle('on',v==='text');
  $('vPdf').classList.toggle('on',v==='pdf');
  $('viewPrev').style.display=v==='prev'?'':'none';
  $('anon').style.display=v==='text'?'':'none';
  $('pdfOutView').style.display=v==='pdf'?'':'none';
  if(v!=='pdf'){
    matchScroll(v==='prev'?$('viewPrev'):$('anon'),$('src'));  // allinea la vista appena mostrata
    return;
  }
  if(OUT_DOC){renderPages($('pdfOutView'),OUT_DOC);return;}
  busy($('pdfOutView'),tt('t_pdf_render'));
  const d=await buildOutPdf();
  if(VIEW!=='pdf')return;                    // l'utente ha cambiato vista nel frattempo
  if(!d){setView('prev');return;}
  renderPages($('pdfOutView'),d);
  if(d.residual+d.skipped>0)toast(T[L].t_pdf_warn(d.residual,d.skipped),false,9000);
}

/* Genera UNA volta il PDF anonimizzato e lo lascia sul server: la stessa copia
   serve sia l'anteprima a destra sia il download (una sola inferenza).
   Come /pdf, il dizionario non viene inviato: il server lo ricostruisce e lo
   butta, quindi funziona anche con lo switch "dizionario" su DISATTIVO. */
let PDF_JOB=null;
function buildOutPdf(){
  if(OUT_DOC)return Promise.resolve(OUT_DOC);
  if(PDF_JOB)return PDF_JOB;                 // click su tab + download insieme -> una richiesta sola
  const file=$('pdf').files[0];const text=$('src').value.trim();
  if(!DATA&&!file&&!text){toast(tt('t_need_anon'),false);return Promise.resolve(null);}
  const excl=TAGS_LOADED?[...EXCL]:null;
  PDF_JOB=(async()=>{
    try{
      let resp;
      if(file){const fd=new FormData();fd.append('pdf',file);
        if(excl)fd.append('exclude_tags',excl.join(','));
          resp=await fetch('/pdf/preview',{method:'POST',body:fd});}
      else{const body={text};if(excl)body.exclude_tags=excl;
          resp=await fetch('/pdf/preview',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(body)});}
      const d=await resp.json();
      if(!resp.ok){toast(d.error||tt('t_pdf_err'),false);return null;}
      OUT_DOC=d;return d;
    }catch(e){toast(tt('t_pdf_err')+': '+e.message,false);return null;}
    finally{PDF_JOB=null;}
  })();
  return PDF_JOB;
}

/* ---- copy / download ---- */
$('copy').onclick=()=>{if(!DATA){toast(tt('t_need_anon'),false);return;}
  navigator.clipboard.writeText(DATA.anonymized_text).then(()=>toast(tt('t_copied')));};
$('dl').onclick=()=>{if(!DATA||!Object.keys(MAP).length){toast(tt('t_nothing_dl'),false);return;}
  const blob=new Blob([JSON.stringify(MAP,null,2)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='dizionario_anonimizzazione.json';a.click();URL.revokeObjectURL(a.href);
  toast(tt('t_dl_ok'));};

/* ---- PDF anonimizzato (issue #7): redazione vera del PDF caricato, oppure PDF
       ricostruito dal testo anonimizzato quando l'input non era un PDF.
       Se lo si e' gia' guardato nella vista "PDF censurato" il file esiste gia'
       sul server: si scarica quello, senza una seconda anonimizzazione. ---- */
$('dlpdf').onclick=async()=>{
  const btn=$('dlpdf');btn.disabled=true;const old=btn.innerHTML;
  btn.innerHTML='<span class="spin"></span> '+tt('t_pdf_making');
  try{
    const d=await buildOutPdf();
    if(!d)return;
    const a=document.createElement('a');
    a.href=docUrl('/doc/'+d.doc_id+'/file.pdf');
    a.download=d.filename||'documento_anonimizzato.pdf';a.click();
    // residui = valori ancora leggibili nell'output; saltati = valori troppo corti
    // per essere cercati senza devastare il documento. Entrambi restano IN CHIARO.
    if(d.residual+d.skipped>0)toast(T[L].t_pdf_warn(d.residual,d.skipped),false,9000);
    else toast(tt('t_pdf_ok'));
  }finally{btn.disabled=false;btn.innerHTML=old;}
};

/* ---- reverse ---- */
function reverse(){
  const txt=$('rin').value;
  if(!txt.trim()){toast(tt('t_paste_restore'),false);return;}
  const keys=Object.keys(MAP);
  if(!keys.length){toast(tt('t_no_dict'),false);return;}
  // Una sola passata sul testo ORIGINALE, non N replace() sequenziali su un
  // `out` che si accumula: se il valore sostituito per una chiave contenesse
  // per caso il nome di una chiave successiva (es. una ragione sociale con
  // dentro "CF_2"), il replace seguente lo ri-sostituiva -- un falso positivo
  // di secondo ordine che nessuna delle due chiavi, prese da sole, produce.
  // Il lookup avviene ricostruendo il nome dal testo appena matchato, quindi
  // l'ordine delle chiavi nella mappa e' ora irrilevante (non serve piu'
  // ordinare per lunghezza: parentesi TUTTO-O-NIENTE e confine di parola
  // bastano a evitare che FULLNAME_1 matchi dentro FULLNAME_10, si veda sotto).
  const byInner=new Map(keys.map(ph=>[ph.slice(1,-1),MAP[ph]]));
  const alts=keys.map(ph=>{
    const inner=ph.slice(1,-1);                 // FULLNAME_1
    const esc=inner.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    // tollerante: parentesi o forma nuda, spazi, eventuale grassetto markdown.
    // Gli spazi si consumano SOLO insieme alla parentesi: con '\[?\s*' un placeholder
    // scritto senza parentesi si portava via anche gli spazi intorno, e "Il FULLNAME_1 ha"
    // tornava "IlMario Rossiha". Con un tab o un a-capo spariva la colonna o la riga.
    // E le parentesi sono TUTTO-O-NIENTE, con un confine di parola sulla forma nuda:
    // se ognuna e' opzionale per conto suo, con CF_1 in mappa un indice inventato dal
    // modello ([CF_12]) matcha per meta' e il ripristino scrive un codice fiscale
    // SBAGLIATO - un segnaposto rimasto si vede, un valore sbagliato no.
    // Il confine e' Unicode-aware (specchia _is_word() lato Python, che tratta le
    // lettere accentate come interne alla parola): \b di JS e' ASCII-only, quindi
    // su "CF_1e' gia'" non vedeva un confine e il valore tornava incollato alla
    // lettera accentata -- lo stesso difetto che questa funzione deve evitare,
    // riaperto in modo asimmetrico solo per le parole italiane.
    return '\\[\\s*'+esc+'\\s*\\]|(?<![\\p{L}\\p{N}_])'+esc+'(?![\\p{L}\\p{N}_])';
  }).join('|');
  // Gli asterischi di grassetto markdown si assorbono in coppie SIMMETRICHE
  // (stesso numero prima e dopo, fino a 2, col backreference \1): una sequenza
  // di asterischi condivisa fra due placeholder adiacenti si divide cosi' in
  // modo deterministico, invece di dipendere da quale chiave viene elaborata
  // prima (con N pattern separati, un lato mangiava tutta la sequenza e
  // lasciava l'altro placeholder non risolto o incollato al primo valore).
  const rx=new RegExp('(\\*{0,2})(?:'+alts+')\\1','gu');
  const out=txt.replace(rx,(m)=>{
    const core=m.replace(/^\*+|\*+$/g,'');
    const br=core.match(/^\[\s*([\s\S]*?)\s*\]$/);
    const val=byInner.get(br?br[1]:core);
    return val===undefined?m:val;               // per costruzione sempre trovato
  });
  const o=$('rout');o.textContent=out;o._raw=out;
  toast(tt('t_restored'));
}
$('rev').onclick=reverse;
$('rcopy').onclick=()=>{const o=$('rout');if(!o._raw){toast(tt('t_nothing_copy'),false);return;}
  navigator.clipboard.writeText(o._raw).then(()=>toast(tt('t_restored_copied')));};
$('rclear').onclick=()=>{$('rin').value='';$('rout').innerHTML=routEmpty();$('rout')._raw='';};

/* ---- carica dizionario da file (per sessioni diverse) ---- */
$('dictFile').onchange=e=>{const f=e.target.files[0];if(!f)return;
  const r=new FileReader();r.onload=()=>{try{
    const parsed=JSON.parse(r.result);
    const dictKeys=parsed&&typeof parsed==='object'&&!Array.isArray(parsed)?Object.keys(parsed):[];
    // Un file dizionario corrotto/troncato con una chiave vuota o non testuale
    // degenera il pattern di reverse() in un match quasi universale e corrompe
    // l'intero documento al ripristino, non solo un placeholder.
    const valid=dictKeys.length>0&&dictKeys.every(k=>/^\[.+\]$/.test(k)&&typeof parsed[k]==='string');
    if(!valid){toast(tt('t_json_invalid'),false);return;}
    MAP=parsed;
    $('dictInfo').textContent=T[L].dict_loaded_n(dictKeys.length);
    toast(tt('t_dict_loaded'));}catch{toast(tt('t_json_invalid'),false);}};
  r.readAsText(f);};

/* ---- input helpers ---- */
$('go').onclick=run;
$('clear').onclick=()=>{$('src').value='';$('pdf').value='';$('dropTxt').innerHTML=tt('drop');
  DATA=null;$('prev').style.display='none';$('emptyPrev').style.display='';
  $('anon').value='';$('meta').innerHTML='';$('legend').innerHTML='';
  $('dictCard').style.display='none';$('ulock').textContent='';
  // la card era solo nascosta: senza queste tre righe il dizionario resta in MAP e su
  // disco, e al riavvio ricompare zitto al posto di quello del documento nuovo
  MAP={};localStorage.removeItem('pii_map');$('dictInfo').textContent='';
  SRC_DOC=null;OUT_DOC=null;$('pdfSrcView').innerHTML='';$('pdfOutView').innerHTML='';
  $('srcTabs').style.display='none';$('inHint').innerHTML=tt('in_hint');
  $('inHint').style.display='';setSrcView('text');setView('prev');
  document.querySelector('.app').classList.remove('has-result');};
$('src').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')run();});

/* pdf picker + dropzone */
const drop=$('drop');
$('pdf').onchange=e=>{const f=e.target.files[0];if(f)onFile(f);};
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('hot');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('hot');}));
const OK_EXT=/\.(pdf|md|markdown|txt|text)$/i;
drop.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];
  if(f&&(f.type==='application/pdf'||OK_EXT.test(f.name))){
    const dt=new DataTransfer();dt.items.add(f);$('pdf').files=dt.files;
    onFile(f);}else toast(tt('t_drag_pdf'),false);});

/* scroll sincronizzato: editor (sx) <-> anteprima e testo (dx) */
linkScroll($('src'),$('viewPrev'));linkScroll($('viewPrev'),$('src'));
linkScroll($('src'),$('anon'));linkScroll($('anon'),$('src'));
/* e le due viste PDF fra loro: stesso documento, prima e dopo la censura */
linkScroll($('pdfSrcView'),$('pdfOutView'));linkScroll($('pdfOutView'),$('pdfSrcView'));

/* lingua: selettore + applicazione iniziale (default IT, preferenza salvata) */
$('lang').onchange=e=>applyLang(e.target.value);
applyLang(localStorage.getItem('pii_lang')||'it');

/* avviso: popup a click (non hover), si chiude cliccando fuori o con Esc */
$('infoBtn').addEventListener('click',e=>{e.stopPropagation();$('infoBtn').classList.toggle('open');});
$('infoBtn').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('infoBtn').classList.toggle('open');}});
document.addEventListener('click',e=>{if(!$('infoBtn').contains(e.target))$('infoBtn').classList.remove('open');});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('infoBtn').classList.remove('open');closeConfig();closeTags();}});

/* ---- config modal ---- */
async function openConfig(){
  const r=await fetch('/config');const d=await r.json();
  $('cfgHost').value=d.host||'127.0.0.1';
  $('cfgPort').value=d.port||5005;
  $('cfgStatus').className='cfg-status';$('cfgStatus').textContent='';
  $('cfgOverlay').classList.add('open');
}
function closeConfig(){$('cfgOverlay').classList.remove('open');}
$('cfgOverlay').addEventListener('click',e=>{if(e.target===$('cfgOverlay'))closeConfig();});
async function checkPort(){
  const h=$('cfgHost').value.trim(),p=parseInt($('cfgPort').value);
  if(!p||p<1024||p>65535){$('cfgStatus').className='cfg-status fail';$('cfgStatus').textContent=tt('cfg_in_use');return;}
  const r=await fetch(`/port-check?host=${encodeURIComponent(h)}&port=${p}`);
  const d=await r.json();
  $('cfgStatus').className=d.available?'cfg-status ok':'cfg-status fail';
  $('cfgStatus').textContent=d.available?tt('cfg_available'):tt('cfg_in_use');
}
async function saveConfig(){
  const h=$('cfgHost').value.trim(),p=parseInt($('cfgPort').value);
  if(!p||p<1024||p>65535){toast(tt('cfg_in_use'),false);return;}
  // fuori dal loopback il server (e i documenti) diventano raggiungibili dalla rete:
  // il server lo accetta solo con confirm_expose, che chiediamo qui esplicitamente
  const loop=/^(localhost|127(\.\d{1,3}){3}|::1|\[::1\])$/i.test(h);
  if(!loop && !confirm(T[L].cfg_expose_confirm(h)))return;
  const r=await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host:h,port:p,confirm_expose:!loop})});
  if(!r.ok){let m='';try{m=(await r.json()).error||'';}catch{}toast(m||tt('cfg_in_use'),false);return;}
  toast(tt('cfg_saved'));
  closeConfig();
}

/* ---- switch "Dizionario reversibile" ---- */
function renderMapping(){
  const on=MAPPING;
  $('mapToggle').classList.toggle('off',!on);
  $('mapToggle').setAttribute('aria-checked',String(on));
  $('mapSw').classList.toggle('off',!on);
  $('mapState').textContent=on?tt('map_on'):tt('map_off');
  $('mapSub').innerHTML=on?tt('map_sub_on'):tt('map_sub_off');
  $('rOff').style.display=on?'none':'';
  $('dl').style.display=on?'':'none';      // niente dizionario -> niente download
}
async function toggleMapping(){
  MAPPING=!MAPPING;
  renderMapping();
  toast(MAPPING?tt('t_map_on'):tt('t_map_off'),MAPPING);
  try{                                 // persiste: vale anche per l'API /analyze
    await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mapping_enabled:MAPPING})});
  }catch(e){}
}

/* ---- tags modal: legenda + selezione dei tag da anonimizzare ---- */
let TAGS_META={};
async function loadTags(){
  try{
    const d=await (await fetch('/settings')).json();
    TAGS=d.tags||[];EXCL=new Set(d.excluded_tags||[]);TAGS_META=d;TAGS_LOADED=true;
    MAPPING=d.mapping_enabled!==false;renderMapping();
  }catch(e){/* server vecchio o offline: si continua con il comportamento di default */}
}
function renderTags(){
  const list=$('tgList');list.innerHTML='';
  for(const t of TAGS){
    const c=colors(t.tag),on=!EXCL.has(t.tag);
    const row=document.createElement('label');
    row.className='tg-row'+(on?'':' off');
    row.innerHTML=`<input type="checkbox" ${on?'checked':''}>`+
      `<span class="sw" style="background:${c.bd}"></span>`+
      `<span class="txt"><span class="nm" style="color:${c.tx}">${t.tag}</span>`+
      `<div class="ds">${escapeHtml(L==='en'?t.en:t.it)}</div>`+
      `<div class="ex">${escapeHtml(t.example||'')}</div></span>`;
    row.querySelector('input').onchange=e=>{
      e.target.checked?EXCL.delete(t.tag):EXCL.add(t.tag);
      row.classList.toggle('off',!e.target.checked);tgCount();};
    list.appendChild(row);
  }
  tgCount();
}
function tgCount(){$('tgCount').textContent=T[L].tg_count(TAGS.length-EXCL.size,TAGS.length);}
function setAllTags(on){
  EXCL=on?new Set():new Set(TAGS.map(t=>t.tag));
  renderTags();
}
async function openTags(){
  if(!TAGS.length)await loadTags();
  if(!TAGS.length){toast(tt('t_error'),false);return;}
  $('tgStatus').className='cfg-status';$('tgStatus').textContent='';
  $('tgNote').innerHTML=(TAGS_META.env_override?'<b>'+tt('tg_env')+'</b><br>':'')+
    T[L].tg_note(TAGS_META.config_path||'');
  renderTags();
  $('tagsOverlay').classList.add('open');
}
function closeTags(){$('tagsOverlay').classList.remove('open');loadTags();}  // ricarica: annulla = scarta
$('tagsOverlay').addEventListener('click',e=>{if(e.target===$('tagsOverlay'))closeTags();});
async function saveTags(){
  try{
    const r=await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({excluded_tags:[...EXCL]})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'');
    EXCL=new Set(d.excluded_tags||[]);
    toast(tt('tg_saved'));
    $('tagsOverlay').classList.remove('open');
  }catch(e){$('tgStatus').className='cfg-status fail';$('tgStatus').textContent=tt('tg_err');}
}
loadTags();

/* recupera dizionario da sessione precedente (dopo applyLang -> testo nella lingua giusta) */
try{const m=localStorage.getItem('pii_map');if(m){MAP=JSON.parse(m);
  if(Object.keys(MAP).length)$('dictInfo').textContent=T[L].dict_session(Object.keys(MAP).length);}}catch{}
