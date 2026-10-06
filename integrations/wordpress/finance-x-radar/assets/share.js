(function(){
  'use strict';
  function countView(){
    if(!window.fxrArticle||!fxrArticle.ajaxUrl||!fxrArticle.postId)return;
    var body=new URLSearchParams({action:'fxr_count_view',post_id:String(fxrArticle.postId)});
    fetch(fxrArticle.ajaxUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body.toString()}).then(function(response){return response.json();}).then(function(result){
      if(!result.success||!result.data)return;
      var target=document.querySelector('[data-fxr-view-count]');
      if(target)target.textContent=Number(result.data.views||0).toLocaleString();
    }).catch(function(){});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',countView);else countView();
  document.addEventListener('click',function(event){
    var button=event.target.closest('[data-fxr-copy-url]');
    if(!button)return;
    var url=button.getAttribute('data-fxr-copy-url');
    var done=function(){button.textContent=button.getAttribute('data-copied-label');button.classList.add('is-copied');window.setTimeout(function(){button.textContent=button.getAttribute('data-copy-label');button.classList.remove('is-copied');},1800);};
    if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(url).then(done);return;}
    var field=document.createElement('textarea');field.value=url;field.setAttribute('readonly','');field.style.position='fixed';field.style.opacity='0';document.body.appendChild(field);field.select();try{document.execCommand('copy');done();}finally{document.body.removeChild(field);}
  });
}());
