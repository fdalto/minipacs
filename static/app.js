(() => {
  const csrf = document.body.dataset.csrf;
  const tbody = document.querySelector('#studies');
  const search = document.querySelector('#search');
  const selectAll = document.querySelector('#select-all');
  const bulkDownload = document.querySelector('#bulk-download');
  const bulkDelete = document.querySelector('#bulk-delete');
  const dialog = document.querySelector('#confirm');
  const confirmText = document.querySelector('#confirm-text');
  const downloadProgress = document.querySelector('#download-progress');
  const queueOverlay = document.querySelector('#bulk-download-queue');
  const queueList = document.querySelector('#queue-list');
  const queueMessage = document.querySelector('#queue-message');
  const queueCount = document.querySelector('#queue-count');
  const queueProgressBar = document.querySelector('#queue-progress-bar');
  const queueCancel = document.querySelector('#queue-cancel');
  let items = [];
  let downloading = false;
  let queueCancelRequested = false;

  const escape = (s) => String(s || '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const selected = () => [...document.querySelectorAll('.study-check:checked')].map(x => x.value);
  const bytes = n => { const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++} return `${n.toFixed(i?1:0)} ${u[i]}` };
  const date = value => value ? new Date(value).toLocaleString('pt-BR') : '—';

  function updateButtons(){
    const count=selected().length;
    bulkDownload.disabled=downloading||!count;
    bulkDelete.disabled=downloading||!count;
    document.querySelectorAll('.download-one,.delete-one,.study-check').forEach(control=>control.disabled=downloading);
    selectAll.disabled=downloading;
    selectAll.checked=items.length>0&&count===items.length;
    selectAll.indeterminate=count>0&&count<items.length;
  }

  function render(data){
    const keep=new Set(selected());
    items=data.studies;
    document.querySelector('#count-studies').textContent=data.summary.studies;
    document.querySelector('#count-images').textContent=data.summary.images;
    document.querySelector('#count-bytes').textContent=bytes(data.summary.bytes);
    if(!items.length){tbody.innerHTML='<tr><td colspan="8" class="muted">Nenhum estudo encontrado.</td></tr>';updateButtons();return}
    tbody.innerHTML=items.map(s=>`<tr><td><input class="study-check" type="checkbox" value="${escape(s.study_instance_uid)}" ${keep.has(s.study_instance_uid)?'checked':''}></td><td><span class="patient-name">${escape(s.patient_name)||'—'}</span><small class="patient-id">${escape(s.patient_id)}</small></td><td>${escape(s.study_date)||'—'}</td><td>${escape(s.study_description)||'—'}</td><td>${escape(s.destination_ae)||'—'}</td><td>${s.image_count}</td><td>${date(s.last_received_at)}</td><td class="actions"><button class="download-one" data-uid="${escape(s.study_instance_uid)}" ${downloading?'disabled':''}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v11m0 0 4-4m-4 4-4-4M5 20h14"/></svg>Baixar</button><button class="danger delete-one" data-uid="${escape(s.study_instance_uid)}" ${downloading?'disabled':''}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v5m4-5v5M9 7l1-3h4l1 3m-9 0 1 13h10l1-13"/></svg>Excluir</button></td></tr>`).join('');
    updateButtons();
  }

  async function load(){
    try{const r=await fetch(`/api/studies?q=${encodeURIComponent(search.value)}`);if(r.status===401){location='/login';return}if(!r.ok)throw Error();render(await r.json())}
    catch{tbody.innerHTML='<tr><td colspan="8" class="error">Não foi possível carregar os estudos.</td></tr>'}
  }

  async function deletion(uids){
    const images=items.filter(s=>uids.includes(s.study_instance_uid)).reduce((sum,s)=>sum+s.image_count,0);
    const size=items.filter(s=>uids.includes(s.study_instance_uid)).reduce((sum,s)=>sum+s.total_size_bytes,0);
    confirmText.textContent=`Você removerá ${uids.length} estudo(s), ${images} imagem(ns), ocupando ${bytes(size)}. Esta ação não pode ser desfeita.`;
    dialog.showModal();
    const answer=await new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));
    if(answer!=='confirm')return;
    const path=uids.length===1?`/api/studies/${encodeURIComponent(uids[0])}`:'/api/studies/delete-bulk';
    const options={method:uids.length===1?'DELETE':'POST',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'}};
    if(uids.length>1)options.body=JSON.stringify({study_uids:uids});
    const r=await fetch(path,options);
    if(!r.ok)alert('Não foi possível excluir o estudo. Ele pode estar sendo recebido neste momento.');
    await load();
  }

  function setDownloadState(active){
    downloading=active;
    downloadProgress.classList.toggle('is-visible',active);
    downloadProgress.setAttribute('aria-hidden',String(!active));
    updateButtons();
  }

  function saveDownload(blob,filename){
    const link=document.createElement('a');
    const url=URL.createObjectURL(blob);
    link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }

  async function requestDownload(studyUid){
    const response=await fetch(`/download/study/${encodeURIComponent(studyUid)}`);
    if(!response.ok)throw Error();
    saveDownload(await response.blob(),'minipacs-study.zip');
  }

  async function downloadOne(studyUid){
    if(downloading)return;
    setDownloadState(true);
    try{await requestDownload(studyUid)}catch{alert('Não foi possível preparar o download. Tente novamente.')}finally{setDownloadState(false)}
  }

  function showQueue(studies){
    queueCancelRequested=false;
    queueCancel.disabled=false;
    queueCancel.textContent='Cancelar após o atual';
    queueMessage.textContent='Os estudos serão compactados um por vez.';
    queueCount.textContent=`0 de ${studies.length}`;
    queueProgressBar.style.width='0%';
    queueList.innerHTML=studies.map((study,index)=>`<li data-index="${index}"><span class="queue-item-state" aria-hidden="true"></span><span>${escape(study.patient_name)||'Paciente sem nome'}<small>${escape(study.study_description)||escape(study.patient_id)||'Estudo DICOM'}</small></span></li>`).join('');
    queueOverlay.hidden=false;
  }

  function updateQueueItem(index,state){
    const row=queueList.querySelector(`[data-index="${index}"]`);
    if(row)row.dataset.state=state;
  }

  function updateQueueProgress(completed,total){
    queueCount.textContent=`${completed} de ${total}`;
    queueProgressBar.style.width=`${Math.round((completed/total)*100)}%`;
  }

  async function downloadSelectedQueue(){
    if(downloading)return;
    const chosen=new Set(selected());
    const studies=items.filter(study=>chosen.has(study.study_instance_uid));
    if(!studies.length)return;
    setDownloadState(true);
    showQueue(studies);
    let completed=0;
    for(let index=0;index<studies.length;index++){
      updateQueueItem(index,'active');
      queueMessage.textContent=`Compactando ${index+1} de ${studies.length}…`;
      try{await requestDownload(studies[index].study_instance_uid);updateQueueItem(index,'done')}
      catch{updateQueueItem(index,'error')}
      completed++;
      updateQueueProgress(completed,studies.length);
      if(queueCancelRequested)break;
    }
    if(queueCancelRequested)queueMessage.textContent='Downloads restantes cancelados.';
    queueOverlay.hidden=true;
    setDownloadState(false);
  }

  function requestQueueCancellation(){
    if(!downloading||queueOverlay.hidden)return;
    queueCancelRequested=true;
    queueCancel.disabled=true;
    queueCancel.textContent='Cancelamento solicitado';
    queueMessage.textContent='O download atual será concluído; os próximos serão cancelados.';
  }

  let timer;
  search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,250)});
  selectAll.addEventListener('change',()=>{document.querySelectorAll('.study-check').forEach(x=>x.checked=selectAll.checked);updateButtons()});
  tbody.addEventListener('change',updateButtons);
  tbody.addEventListener('click',e=>{
    const deleteButton=e.target.closest('button.delete-one');
    const downloadButton=e.target.closest('button.download-one');
    if(deleteButton&&!deleteButton.disabled)deletion([deleteButton.dataset.uid]);
    if(downloadButton&&!downloadButton.disabled)downloadOne(downloadButton.dataset.uid);
  });
  bulkDelete.addEventListener('click',()=>deletion(selected()));
  bulkDownload.addEventListener('click',downloadSelectedQueue);
  queueCancel.addEventListener('click',requestQueueCancellation);
  load();
  setInterval(load,30000);
})();
