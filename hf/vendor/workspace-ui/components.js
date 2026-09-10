(function (global) {
  'use strict';
  const initialized = new WeakSet();
  const busyStates = new WeakMap();
  const triggers = new WeakMap();
  function setBusy(button, busy) {
    if (busy) {
      if (!busyStates.has(button)) busyStates.set(button, { disabled:button.disabled, label:button.getAttribute('aria-label') });
      const previous=busyStates.get(button);
      button.disabled=true;
      button.setAttribute('aria-busy','true');
      button.setAttribute('aria-label',(previous.label || button.textContent.trim())+'，处理中');
    } else if (busyStates.has(button)) {
      const previous=busyStates.get(button);
      button.disabled=previous.disabled;
      button.removeAttribute('aria-busy');
      if(previous.label===null) button.removeAttribute('aria-label'); else button.setAttribute('aria-label',previous.label);
      busyStates.delete(button);
    }
  }
  function init(root=document) {
    root.querySelectorAll('[data-wu-tabs]').forEach(group=>{
      if(initialized.has(group)) return;
      initialized.add(group);
      const tabs=[...group.querySelectorAll('[role="tab"]')];
      function select(tab, focus=false) {
        if(tab.disabled) return;
        tabs.forEach(item=>{
          const active=item===tab;
          item.setAttribute('aria-selected',String(active)); item.tabIndex=active?0:-1;
          const panel=document.getElementById(item.getAttribute('aria-controls'));
          if(panel) panel.hidden=!active;
        });
        if(focus) tab.focus();
        group.dispatchEvent(new CustomEvent('wu:tabchange',{bubbles:true,detail:{id:tab.id}}));
      }
      tabs.forEach(tab=>{
        tab.addEventListener('click',()=>select(tab));
        tab.addEventListener('keydown',event=>{
          const enabled=tabs.filter(item=>!item.disabled); let i=enabled.indexOf(tab);
          if(event.key==='ArrowRight') i=(i+1)%enabled.length;
          else if(event.key==='ArrowLeft') i=(i-1+enabled.length)%enabled.length;
          else if(event.key==='Home') i=0;
          else if(event.key==='End') i=enabled.length-1;
          else return;
          event.preventDefault(); select(enabled[i],true);
        });
      });
      const initial=tabs.find(tab=>tab.getAttribute('aria-selected')==='true'&&!tab.disabled)||tabs.find(tab=>!tab.disabled);
      if(initial) select(initial);
    });
    root.querySelectorAll('[data-wu-dialog-open]').forEach(button=>{
      if(initialized.has(button)) return;
      initialized.add(button);
      button.addEventListener('click',()=>{
        const dialog=document.getElementById(button.dataset.wuDialogOpen);
        if(!dialog || dialog.open) return;
        triggers.set(dialog,button); dialog.showModal();
      });
    });
    root.querySelectorAll('dialog.wu-dialog').forEach(dialog=>{
      if(initialized.has(dialog)) return;
      initialized.add(dialog);
      dialog.addEventListener('close',()=>{const trigger=triggers.get(dialog);if(trigger?.isConnected) trigger.focus();});
      dialog.querySelectorAll('[data-wu-dialog-close]').forEach(button=>button.addEventListener('click',()=>dialog.close()));
    });
  }
  global.WorkspaceUI={init,setBusy};
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',()=>init()); else init();
})(window);
