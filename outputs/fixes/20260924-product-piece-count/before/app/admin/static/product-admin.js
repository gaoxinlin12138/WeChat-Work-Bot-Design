(function(){
  const COMPONENT_NAMES=['说明书','PVC管','电话管','顶喷','手喷','喷枪','直管','七字管','中叉','固定座','装饰盖','弯角','六分橡皮','膨胀螺丝','生料带','内盒','外箱','布袋','珍珠棉带','泡沫盒'];
  const WECOM_VIDEO_SAFE_MAX_BYTES=9000000;
  const WECOM_IMAGE_SAFE_MAX_BYTES=10*1024*1024;
  function val(form,name){return form.elements[name].value.trim()}
  function num(form,name){return val(form,name)===''?null:Number(val(form,name))}
  function renderComponentEditor(components=[]){
    const byName=new Map((components||[]).map(x=>[x.name,x]));
    $('#component-editor-body').innerHTML=COMPONENT_NAMES.map((name,index)=>{const item=byName.get(name)||{};return '<tr><td><b>'+esc(name)+'</b><input type="hidden" data-component-name value="'+esc(name)+'"></td><td><input data-component-supplier data-index="'+index+'" value="'+esc(item.supplier||'')+'" placeholder="供应商"></td><td><input data-component-price data-index="'+index+'" type="number" min="0" step="0.01" value="'+(item.unit_price??'')+'" placeholder="—"></td></tr>'}).join('');
    $('#component-editor-body').querySelectorAll('input').forEach(input=>input.addEventListener('input',updateComponentTotal));
    updateComponentTotal();
  }
  function componentValues(){return COMPONENT_NAMES.map((name,index)=>({name,supplier:document.querySelector('[data-component-supplier][data-index="'+index+'"]')?.value.trim()||'',unit_price:(document.querySelector('[data-component-price][data-index="'+index+'"]')?.value||'').trim()||null})).filter(x=>x.supplier||x.unit_price!==null)}
  function updateComponentTotal(){const total=componentValues().reduce((sum,item)=>sum+(item.unit_price===null?0:Number(item.unit_price||0)),0);$('#component-total').textContent=total?('¥'+total.toFixed(2)):'—'}
  function payload(){
    const form=$('#product-form');
    if(!form.reportValidity())throw new Error('请填写产品型号');
    return {sequence_number:num(form,'sequence_number'),model:val(form,'model'),series:val(form,'series'),product_description:val(form,'product_description'),style:state.editingProductOriginal?.style||'',material:val(form,'material'),spray_modes:val(form,'spray_modes'),color:val(form,'color'),special_function:val(form,'special_function'),shipping_origin:val(form,'shipping_origin'),set_price:num(form,'set_price'),body_price:num(form,'body_price'),cost_price:num(form,'cost_price'),supplier:val(form,'supplier'),body_supplier:val(form,'body_supplier'),components:componentValues(),notes:val(form,'notes'),source_row:state.editingProductOriginal?.source_row??null}
  }
  function label(p){return [p.model,p.color,p.material,p.style].filter(Boolean).join(' · ')}
  const PRODUCT_PRICE_FIELDS=new Set(['set_price','body_price','cost_price','unit_price']);
  function comparableProductValue(value,key=''){
    if(value==null)return null;
    // SQL Decimal amounts arrive as strings; JSONB snapshots contain numbers.
    // Only prices are numeric: model and supplier identifiers stay exact text.
    if(PRODUCT_PRICE_FIELDS.has(key)&&(typeof value==='number'||(typeof value==='string'&&value.trim()!==''))){
      const amount=Number(value);
      if(Number.isFinite(amount))return amount;
    }
    if(Array.isArray(value))return value.map(item=>comparableProductValue(item));
    if(typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(name=>[name,comparableProductValue(value[name],name)]));
    return value;
  }
  function hasPendingProductChanges(p){
    if(p?.status!=='active'||!p.published_snapshot)return false;
    const fieldsChanged=['sequence_number','model','series','product_description','style','material','spray_modes','color','special_function','shipping_origin','set_price','body_price','cost_price','supplier','body_supplier','components','notes','source_row'].some(key=>JSON.stringify(comparableProductValue(p[key],key))!==JSON.stringify(comparableProductValue(p.published_snapshot[key],key)));
    const currentMedia=(p.media||[]).map(item=>String(item.id)).sort();
    const publishedMedia=(p.published_snapshot.media_ids||[]).map(String).sort();
    return fieldsChanged||JSON.stringify(currentMedia)!==JSON.stringify(publishedMedia);
  }
  const MEDIA_UPLOADS=[
    {name:'main_image_upload',role:'main_image',label:'主图'},
    {name:'gallery_image_upload',role:'gallery_image',label:'实物图'},
    {name:'configuration_image_upload',role:'configuration_image',label:'配置图'},
    {name:'product_video_upload',role:'product_video',label:'产品视频'},
  ];
  const pendingPreviewUrls=new Map();
  function validateProductVideo(file){
    if(!file)return;
    if(!file.name.toLowerCase().endsWith('.mp4'))throw new Error('产品视频仅支持 MP4 文件');
    if(file.size>WECOM_VIDEO_SAFE_MAX_BYTES)throw new Error('产品视频需压缩到 9 MB 以内后再上传');
  }
  function validatePendingMedia(){
    const input=$('#product-form input[name="product_video_upload"]');
    [...(input?.files||[])].forEach(validateProductVideo);
    MEDIA_UPLOADS.filter(item=>item.role!=='product_video').forEach(({name,label})=>{
      [...($('#product-form input[name="'+name+'"]')?.files||[])].forEach(file=>{
        if(file.size>WECOM_IMAGE_SAFE_MAX_BYTES)throw new Error(`${label}“${file.name}”不能超过 10MB`);
      });
    });
  }
  function formatFileSize(bytes){if(bytes<1024)return `${bytes} B`;if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} KB`;return `${(bytes/1024/1024).toFixed(1)} MB`}
  function renderPendingMedia(){
    MEDIA_UPLOADS.forEach(({name})=>{
      const input=$(`#product-form input[name="${name}"]`),target=document.querySelector(`[data-upload-preview="${name}"]`);
      if(!input||!target)return;
      const previous=pendingPreviewUrls.get(name);if(previous)URL.revokeObjectURL(previous);pendingPreviewUrls.delete(name);
      const files=[...input.files];
      if(!files.length){target.className='upload-preview empty';target.textContent='尚未选择文件';return}
      const file=files[0],isImage=file.type.startsWith('image/');
      let preview='';
      if(isImage){const url=URL.createObjectURL(file);pendingPreviewUrls.set(name,url);preview=`<img src="${url}" alt="${esc(file.name)}">`}
      else preview='<span class="video-tile"><svg aria-hidden="true"><use href="#i-play"></use></svg></span>';
      const more=files.length>1?` · 共 ${files.length} 个文件`:'';
      target.className='upload-preview selected';target.innerHTML=`<div class="file-preview">${preview}<span><b>${esc(file.name)}</b><small>${formatFileSize(file.size)}${more}</small></span></div>${files.length>1?`<small class="upload-file-list">${files.slice(1).map(item=>esc(item.name)).join('、')}</small>`:''}`;
    });
  }
  function clearForm(){const form=$('#product-form');form.reset();state.editingId=null;state.editingProductOriginal=null;$('#product-form-title').textContent='单条建档';renderComponentEditor();renderMedia(null);renderPendingMedia();setEditorStatus('未保存更改')}
  function setEditorStatus(message,tone=''){const target=$('#product-save-state');if(!target)return;target.textContent=message;target.className='save-state'+(tone?' '+tone:'')}
  function fillForm(p){const form=$('#product-form');clearForm();state.editingId=p.id;state.editingProductOriginal={style:p.style??'',source_row:p.source_row??null};$('#product-form-title').textContent='编辑产品 · '+p.model;['sequence_number','model','series','product_description','material','spray_modes','color','special_function','shipping_origin','set_price','body_price','cost_price','supplier','body_supplier','notes'].forEach(name=>form.elements[name].value=p[name]??'');renderComponentEditor(p.components||[]);renderMedia(p)}
  function openProductEditor(){const editor=$('#product-editor');if(!editor.open)editor.showModal();requestAnimationFrame(()=>$('#product-form [name="model"]')?.focus())}
  function closeProductEditor(){const editor=$('#product-editor');if(editor.open)editor.close()}
  function renderMedia(p){
    const target=$('#product-media-list');
    if(!p?.media?.length){target.innerHTML='<p class="hint">尚未上传实际媒体文件。</p>';return}
    const names={main_image:'主图',gallery_image:'实物图',configuration_image:'配置图',product_video:'产品视频'};
    target.innerHTML='<div class="existing-media-head"><span>已保存媒体</span><span>'+p.media.length+' 个文件</span></div><div class="media-strip">'+p.media.map(m=>{const url='/business/products/'+encodeURIComponent(p.id)+'/media/'+encodeURIComponent(m.id)+'/file';const visual=m.media_type==='image'?`<img src="${url}" alt="${esc(m.original_name)}" loading="lazy">`:'<span class="video-tile"><svg aria-hidden="true"><use href="#i-play"></use></svg><small>视频</small></span>';return '<article class="media-item">'+visual+'<div><b>'+esc(names[m.role]||m.role)+'</b><span title="'+esc(m.original_name)+'">'+esc(m.original_name)+'</span></div><a href="'+url+'" target="_blank" rel="noopener noreferrer">打开</a><button type="button" class="icon danger" data-remove-media="'+m.id+'" aria-label="删除 '+esc(m.original_name)+'"><svg aria-hidden="true"><use href="#i-close"></use></svg></button></article>'}).join('')+'</div>';
    target.querySelectorAll('[data-remove-media]').forEach(button=>button.onclick=()=>removeMedia(p.id,button.dataset.removeMedia))
  }
  async function uploadMedia(productId){
    const form=$('#product-form'),jobs=[];
    validatePendingMedia();
    MEDIA_UPLOADS.forEach(({name,role,label})=>[...(form.elements[name]?.files||[])].forEach((file,index)=>jobs.push({file,role,label,index})));
    for(const job of jobs){
      const body=new FormData();body.append('file',job.file);body.append('role',job.role);body.append('sort_order',String(job.index));
      try{await api('/business/products/'+encodeURIComponent(productId)+'/media',{method:'POST',body})}
      catch(error){throw new Error(`${job.label}“${job.file.name}”上传失败：${error.message}`)}
    }
    MEDIA_UPLOADS.forEach(({name})=>{if(form.elements[name])form.elements[name].value=''});renderPendingMedia();
    return jobs.length;
  }
  async function performSaveProduct(publish){
    setEditorStatus(publish?'正在保存并发布…':'正在保存草稿…','working');
    try{validatePendingMedia();const body=payload(),url=state.editingId?'/business/products/'+encodeURIComponent(state.editingId):'/business/products';let p=await api(url,{method:state.editingId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});state.editingId=p.id;const uploaded=await uploadMedia(p.id);if(publish)p=await api('/business/products/'+encodeURIComponent(p.id)+'/publish',{method:'POST'});const message=uploaded?`${publish?'产品已发布':'产品草稿已保存'}，已上传 ${uploaded} 个媒体文件`:publish?'产品已发布':'产品草稿已保存';toast(message);await loadCatalog();fillForm(state.catalog.find(x=>x.id===p.id)||p);setEditorStatus(message,'success')}catch(error){setEditorStatus('保存失败，请检查提示后重试','error');toast(error.message,true,7000)}
  }
  function saveProduct(publish){
    let replacements=[];
    try{
      validatePendingMedia();
      const current=state.catalog.find(item=>item.id===state.editingId);
      replacements=MEDIA_UPLOADS.filter(({name,role})=>role!=='gallery_image'&&$('#product-form').elements[name]?.files?.length&&current?.media?.some(media=>media.role===role)).map(item=>item.label);
      if(!publish&&!replacements.length){performSaveProduct(false);return}
      const model=$('#product-form').elements.model.value.trim()||'当前产品';
      const effects=[];
      if(publish)effects.push('保存后会立即发布，机器人可以查询到这份产品资料');
      if(replacements.length)effects.push(`${replacements.join('、')}的草稿媒体会替换当前编辑版本；已发布版本仍会保留原媒体，直到发布切换`);
      showConfirmModal({title:replacements.length?'确认保存并替换媒体':'确认发布产品',message:`${model}：${effects.join('；')}。`,danger:replacements.length>0,onConfirm:()=>performSaveProduct(publish)})
    }catch(error){toast(error.message,true,7000)}
  }
  window.editCatalog=function(id){const p=state.catalog.find(x=>x.id===id);if(!p)return toast('未找到产品',true);fillForm(p);setCatalogModule('library');openProductEditor()};
  window.renderCatalog=function(){
    const query=$('#catalog-filter').value.trim().toLowerCase(),status=$('#catalog-status-filter').value;
    const items=state.catalog.filter(p=>p.status!=='deleted').filter(p=>(!status||p.status===status)&&(!query||[p.model,p.series,p.color,p.material,p.style,p.product_description,p.special_function].join(' ').toLowerCase().includes(query))),visibleIds=new Set(items.map(p=>p.id));
    state.selectedCatalogIds.forEach(id=>{if(!visibleIds.has(id))state.selectedCatalogIds.delete(id)});
    $('#catalog-list').innerHTML=items.map(p=>{const pending=hasPendingProductChanges(p),lifecycle=p.status==='active'?(pending?'<button onclick="publishExistingCatalog(\''+p.id+'\')">发布修改</button>':'<button class="secondary" onclick="disableCatalog(\''+p.id+'\')">停用</button>'):'<button onclick="publishExistingCatalog(\''+p.id+'\')">发布</button>',checked=state.selectedCatalogIds.has(p.id),productLabel=label(p),statusBadge=pending?badge(p.status)+'<span class="badge pending_review">有修改待发布</span>':badge(p.status);return '<article class="catalog-card library-table-row'+(checked?' is-selected':'')+'"><label class="selection-column library-selection"><input type="checkbox" aria-label="选择产品 '+esc(productLabel)+'" data-catalog-select="'+p.id+'"'+(checked?' checked':'')+' onchange="toggleCatalogSelection(\''+p.id+'\',this.checked)"><span class="sr-only">选择产品 '+esc(productLabel)+'</span></label><div class="catalog-summary"><h3>'+esc(productLabel)+'</h3><div class="catalog-badges">'+statusBadge+'</div><p>'+esc(p.product_description||'暂无产品描述')+'<br>系列：'+esc(p.series||'—')+' · 档位：'+esc(p.spray_modes||'—')+' · 出货地：'+esc(p.shipping_origin||'—')+(pending?'<br><small class="status-note">机器人仍使用发布前内容，发布修改后才会替换。</small>':'')+'</p></div><div class="catalog-commercial"><strong>套装 ¥'+(p.set_price==null?'—':money(p.set_price))+'</strong><span>主体 ¥'+(p.body_price==null?'—':money(p.body_price))+'</span><span>不含税 · 不含运费</span></div><div class="catalog-counts"><span><b>'+p.media.length+'</b> 个媒体</span><span><b>'+(p.components||[]).filter(x=>x.supplier||x.unit_price!=null).length+'</b> 个配件</span></div><div class="catalog-actions"><button class="secondary" onclick="editCatalog(\''+p.id+'\')">编辑</button>'+lifecycle+'<button class="danger" onclick="deleteCatalog(\''+p.id+'\')">删除</button></div></article>'}).join('')||'<div class="empty-preview library-empty">没有符合条件的产品。</div>';
    syncCatalogSelection()
  };
  function syncCatalogSelection(){const boxes=$$('[data-catalog-select]'),selected=boxes.filter(box=>state.selectedCatalogIds.has(box.dataset.catalogSelect)).length,all=$('#select-all-products'),button=$('#bulk-delete-products');if(all){all.checked=boxes.length>0&&selected===boxes.length;all.indeterminate=selected>0&&selected<boxes.length;all.disabled=boxes.length===0||state.bulkDeletingCatalog}if(button){button.disabled=selected===0||state.bulkDeletingCatalog;button.textContent=state.bulkDeletingCatalog?'删除中…':selected?`批量删除（${selected}）`:'批量删除'}}
  window.toggleCatalogSelection=function(id,checked){const box=document.querySelector(`[data-catalog-select="${id}"]`);if(checked&&state.selectedCatalogIds.size>=100){if(box)box.checked=false;toast('每次最多选择 100 个产品',true);syncCatalogSelection();return}if(checked)state.selectedCatalogIds.add(id);else state.selectedCatalogIds.delete(id);box?.closest('.catalog-card')?.classList.toggle('is-selected',checked);syncCatalogSelection()};
  window.toggleAllCatalog=function(checked){const boxes=$$('[data-catalog-select]');boxes.forEach((box,index)=>{const select=checked&&index<100;box.checked=select;if(select)state.selectedCatalogIds.add(box.dataset.catalogSelect);else state.selectedCatalogIds.delete(box.dataset.catalogSelect);box.closest('.catalog-card')?.classList.toggle('is-selected',select)});if(checked&&boxes.length>100)toast('已选择前 100 个产品，每次最多批量删除 100 个',true);syncCatalogSelection()};
  window.bulkDeleteCatalog=function(){const validIds=new Set(state.catalog.filter(p=>p.status!=='deleted').map(p=>p.id)),ids=[...state.selectedCatalogIds].filter(id=>validIds.has(id));if(!ids.length){syncCatalogSelection();return}const count=ids.length;showConfirmModal({title:`批量删除 ${count} 个产品`,message:`选中的 ${count} 个产品及其媒体关联将批量移入回收站；机器人会立即停止使用。${state.documentRetentionDays||7} 天内可恢复，到期后产品、检索数据和不再被引用的媒体文件将永久清理。`,danger:true,onConfirm:async()=>{state.bulkDeletingCatalog=true;syncCatalogSelection();try{const result=await bulkTrashWithFallback('/business/products/bulk-trash',{product_ids:ids},ids,id=>`/business/products/${encodeURIComponent(id)}`);state.selectedCatalogIds.clear();toast(`已将 ${result.deleted} 个产品移入回收站`);if(ids.includes(state.editingId)){closeProductEditor();clearForm()}await loadCatalog()}catch(error){toast(error.message,true,7000);await loadCatalog()}finally{state.bulkDeletingCatalog=false;syncCatalogSelection()}}})};
  function renderTrash(){const items=state.catalog.filter(p=>p.status==='deleted'),retention=state.documentRetentionDays||7,now=Date.now();$('#trash-count').textContent=String(items.length);$('#trash-list').innerHTML=items.map(p=>{const deleted=p.deleted_at?new Date(p.deleted_at):null,days=deleted?Math.max(0,Math.ceil(retention-(now-deleted.getTime())/86400000)):retention;return '<article class="trash-item"><div><b>'+esc(label(p))+'</b><span>机器人不会查询此产品 · 还可保留约 '+days+' 天</span></div><div class="trash-actions"><button onclick="restoreCatalog(\''+p.id+'\')">恢复</button><button class="danger" onclick="permanentlyDeleteCatalog(\''+p.id+'\')">永久删除</button></div></article>'}).join('')||'<div class="empty-preview">回收站是空的。</div>'}
  window.loadCatalog=async function(){state.catalog=await api('/business/products?include_deleted=true');const active=state.catalog.filter(x=>x.status==='active'),current=state.catalog.filter(x=>x.status!=='deleted'),deleted=state.catalog.filter(x=>x.status==='deleted').length,currentIds=new Set(current.map(x=>x.id));state.selectedCatalogIds.forEach(id=>{if(!currentIds.has(id))state.selectedCatalogIds.delete(id)});$('#stat-models').textContent=active.length;$('#stat-media').textContent=current.reduce((n,x)=>n+x.media.length,0);if($('#nav-product-count'))$('#nav-product-count').textContent=String(active.length);if($('#nav-trash-count'))$('#nav-trash-count').textContent=deleted?String(deleted):'';renderCatalog();renderTrash()};
  window.publishExistingCatalog=id=>{const p=state.catalog.find(x=>x.id===id);if(!p)return;showConfirmModal({title:hasPendingProductChanges(p)?'发布产品修改':'发布产品',message:`发布“${label(p)}”后，机器人会立即使用这份产品资料。`,onConfirm:async()=>{try{await api('/business/products/'+id+'/publish',{method:'POST'});toast(hasPendingProductChanges(p)?'产品修改已发布':'产品已发布');await loadCatalog()}catch(error){toast(error.message,true)}}})};
  window.disableCatalog=id=>{const p=state.catalog.find(x=>x.id===id);if(!p)return;showConfirmModal({title:'将产品转为草稿',message:`“${label(p)}”转为草稿后，机器人会立即停止使用；草稿超过 ${state.draftRetentionDays||7} 天未操作将自动进入回收站。`,onConfirm:async()=>{try{await api('/business/products/'+id+'/disable',{method:'POST'});toast('产品已停用并回到草稿');await loadCatalog()}catch(error){toast(error.message,true)}}})};
  window.deleteCatalog=id=>{const p=state.catalog.find(x=>x.id===id);if(!p)return;showConfirmModal({title:'将产品移入回收站',message:`产品“${label(p)}”及其媒体关联将进入回收站并保留 ${state.documentRetentionDays||7} 天，期间可以恢复；机器人会立即停止使用，到期后将永久清理。`,danger:true,onConfirm:async()=>{try{await api('/business/products/'+id,{method:'DELETE'});toast('产品已移入回收站');if(state.editingId===id){closeProductEditor();clearForm()}await loadCatalog()}catch(error){toast(error.message,true)}}})};
  window.restoreCatalog=id=>{const p=state.catalog.find(x=>x.id===id);if(!p)return;showConfirmModal({title:'恢复产品',message:`恢复“${label(p)}”及其媒体关联，并回到删除前的状态。`,onConfirm:async()=>{try{await api('/business/products/'+id+'/restore',{method:'POST'});toast('产品已恢复');await loadCatalog()}catch(error){toast(error.message,true)}}})};
  window.permanentlyDeleteCatalog=function(id){const p=state.catalog.find(x=>x.id===id);if(!p)return;showConfirmModal({title:'永久删除产品',message:`产品“${label(p)}”将不等待剩余的 ${state.documentRetentionDays||7} 天保留期，立即永久删除；产品信息、检索数据以及不再被其他产品使用的图片和视频都会被彻底清除，无法恢复。请输入产品型号确认。`,code:p.model,codeLabel:'产品型号',codePlaceholder:'请输入产品型号',danger:true,onConfirm:async()=>{try{await api('/business/products/'+id+'/permanent',{method:'DELETE'});toast('产品及附属数据已永久删除');await loadCatalog()}catch(error){toast(error.message,true)}}})};
  function removeMedia(productId,mediaId){const p=state.catalog.find(x=>x.id===productId),media=p?.media?.find(x=>x.id===mediaId);if(!media)return;showConfirmModal({title:'移除图片或视频',message:`“${media.original_name}”会从当前编辑版本移除；如果已发布版本仍引用它，原文件会保留到发布切换。`,danger:true,onConfirm:async()=>{try{await api('/business/products/'+productId+'/media/'+mediaId,{method:'DELETE'});toast('媒体已从当前编辑版本移除');await loadCatalog();fillForm(state.catalog.find(x=>x.id===productId))}catch(error){toast(error.message,true)}}})}
  $('#product-form').onsubmit=event=>{event.preventDefault();saveProduct(false)};$('#publish-product').onclick=()=>saveProduct(true);$('#clear-product-form').onclick=clearForm;$('#new-product').onclick=()=>{clearForm();openProductEditor()};$('#close-product-editor').onclick=closeProductEditor;$('#cancel-product-editor').onclick=closeProductEditor;$('#product-editor').addEventListener('cancel',event=>{event.preventDefault();closeProductEditor()});$('#product-editor').addEventListener('click',event=>{if(event.target===$('#product-editor'))closeProductEditor()});$('#catalog-filter').oninput=renderCatalog;$('#catalog-status-filter').onchange=renderCatalog;
  MEDIA_UPLOADS.forEach(({name})=>$('#product-form input[name="'+name+'"]').addEventListener('change',renderPendingMedia));
  $('#product-import-form input[type=file]').onchange=event=>$('#product-file-name').textContent=event.target.files[0]?.name||'选择 .xlsx 文件';
  $('#product-import-form').onsubmit=async event=>{event.preventDefault();const form=event.target,button=form.querySelector('button');button.disabled=true;try{const result=await api('/business/products/import-xlsx',{method:'POST',body:new FormData(form)});$('#product-import-result').innerHTML='<div class="quality-head ok"><div><b>已导入 '+result.count+' 条产品草稿</b><span>'+(result.warnings.length?'另有 '+result.warnings.length+' 条提示':'所有有效行已导入')+'</span></div></div>'+result.warnings.map(x=>'<p class="hint">第 '+x.row+' 行：'+esc(x.message)+'</p>').join('');toast('产品档案已导入');form.reset();$('#product-file-name').textContent='选择 .xlsx 文件';await loadCatalog();setCatalogModule('library')}catch(error){toast(error.message,true,7000)}finally{button.disabled=false}};
  clearForm();loadCatalog().catch(error=>toast(error.message,true));
})();
