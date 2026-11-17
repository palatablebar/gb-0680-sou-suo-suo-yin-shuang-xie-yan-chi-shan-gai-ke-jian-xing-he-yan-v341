const issue=document.querySelector('#run-error');
async function runAction(url,payload,upload=false){
  issue.textContent='裁决中…';
  try{
    const options={method:payload?undefined:'GET',body:upload?payload:JSON.stringify(payload)};
    if(!upload)options.headers={'Content-Type':'application/json'};
    const response=await fetch(url,options);
    const result=await response.json();
    if(!response.ok)throw Error(result.message);
    location.reload();
  }catch(error){issue.textContent=error.message;}
}
document.querySelector('#replay').addEventListener('click',()=>
  runAction('/api/release/runs',{sample:document.querySelector('#stream-preset').value}));
document.querySelector('#signoff').addEventListener('click',()=>
  runAction('/api/release/runs/'+window.releaseBatch.id+'/review',
    {version:window.releaseBatch.version,decision:'已复核'}));
document.querySelector('#load-stream').addEventListener('click',()=>{
  const file=document.querySelector('#receipt-file').files[0];
  if(!file){issue.textContent='请选择回执 CSV 文件。';return;}
  const form=new FormData();form.append('file',file);
  runAction('/api/release/upload',form,true);
});
document.querySelector('#doc-filter')?.addEventListener('input',event=>{
  for(const row of document.querySelectorAll('#doc-rows tr')){
    row.hidden=!row.textContent.includes(event.target.value);
  }
});
