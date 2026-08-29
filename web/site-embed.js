/* Gibby site embed v3 — renders approved classes onto theeverett.org:
   1. the Art Workshops page list (as before), and
   2. the homepage "What's Next" carousel, as real slides in date order.
   Both obey an editor-authored GIBBY OVERRIDES text block (see below).

   Squarespace loads this via a single header-injection line:
     <script src="https://gibby-app-ddjo.onrender.com/site-embed.js" defer></script>

   Overrides block: a normal Squarespace TEXT block anywhere on the page whose
   first line is "GIBBY OVERRIDES". Visitors never see it (this script hides it);
   editors see and edit it in the Squarespace editor, where this script does not
   run. Syntax, one directive per line:

     GIBBY OVERRIDES
     [Rug Tufting]
     hide
     [Stained Glass]
     title: Stained Glass Snowflakes
     description: Replacement description. Extra lines continue the field.
     when: Saturday, December 5 · 1:00 PM
     ages: Ages 14+
     price: $60

   [Bracket] lines name a class by any part of its title (case-insensitive).
   Fields: title, description (or desc), when, ages, price, link. "hide" hides
   the card/slide. Lines that match nothing continue the previous field.

   What's Next carousel: Squarespace's carousel controller reads its slides once
   at page load and cannot adopt slides added later (verified: injected slides
   overlap slide 1 even after resize / re-bind attempts). So this script clones
   the carousel element and swaps the clone in — cloning discards Squarespace's
   event listeners, leaving identical markup and CSS that this script then owns:
   it inserts app classes as slides in date order (deduped against hand-made
   entries), restores lazy images the dead Squarespace loader never loaded, and
   drives the arrows itself with a flex track. */
(function(){
var APP='https://gibby-app-ddjo.onrender.com';
function rep(m){try{fetch(APP+'/api/client-error',{method:'POST',mode:'no-cors',headers:{'Content-Type':'text/plain'},body:JSON.stringify({section:'site-embed',message:m,agent:navigator.userAgent,path:location.pathname})})}catch(e){}}
var MM={january:1,february:2,march:3,april:4,may:5,june:6,july:7,august:8,september:9,october:10,november:11,december:12};
function pdate(txt){var m=/(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})/i.exec(txt||'');if(!m)return null;var mo=MM[m[1].toLowerCase()],dy=+m[2];return(mo>=8?2026:2027)*10000+mo*100+dy}
function esc(x){return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function norm(s){return String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'')}
function dateKey(c){var pp=String(c.date||'').split('-');return(+pp[0])*10000+(+pp[1])*100+(+pp[2])}

/* ---------------- editor overrides ---------------- */
function blockLines(host){
  var ps=host.querySelectorAll('p');
  if(!ps.length)return (host.textContent||'').split('\n');
  var lines=[];
  for(var i=0;i<ps.length;i++){
    var segs=ps[i].innerHTML.split(/<br\s*\/?\s*>/i);
    for(var j=0;j<segs.length;j++){
      var d=document.createElement('div');d.innerHTML=segs[j];
      lines.push(d.textContent);
    }
  }
  return lines;
}
function readOverrides(){
  var rules=[],host=null;
  var blocks=document.querySelectorAll('.sqs-block-html,.sqs-html-content');
  for(var i=0;i<blocks.length;i++){
    var t=(blocks[i].textContent||'').trim();
    if(/^gibby overrides/i.test(t)){host=blocks[i];break}
  }
  if(!host)return rules;
  var wrap=host.closest?host.closest('.sqs-block')||host:host;
  wrap.style.display='none';
  var cur=null,lastField=null;
  var lines=blockLines(host);
  for(var j=1;j<lines.length;j++){
    var ln=lines[j].trim();
    if(!ln)continue;
    var b=/^\[(.+)\]$/.exec(ln);
    if(b){cur={key:b[1].trim().toLowerCase(),hide:false,set:{}};rules.push(cur);lastField=null;continue}
    if(!cur)continue;
    if(/^hide$/i.test(ln)){cur.hide=true;lastField=null;continue}
    var f=/^(title|description|desc|when|ages|price|link)\s*[:=]\s*(.*)$/i.exec(ln);
    if(f){lastField=f[1].toLowerCase();if(lastField==='desc')lastField='description';cur.set[lastField]=f[2];continue}
    if(lastField)cur.set[lastField]+='\n'+ln;
  }
  return rules;
}
function applyOverrides(classes,rules){
  if(!rules.length)return classes;
  var out=[];
  classes.forEach(function(c){
    var r=null,tl=(c.title||'').toLowerCase();
    for(var i=0;i<rules.length;i++){if(tl.indexOf(rules[i].key)!==-1){r=rules[i];break}}
    if(r&&r.hide)return;
    if(r){
      if(r.set.title)c.title=r.set.title;
      if(r.set.description)c.desc=r.set.description;
      if(r.set.when)c.when=r.set.when;
      if(r.set.ages)c.ages=r.set.ages;
      if(r.set.price)c.price=r.set.price;
      if(r.set.link)c.url=r.set.link;
    }
    out.push(c);
  });
  return out;
}

/* ---------------- art workshops list ---------------- */
function renderList(list,classes){
  classes.forEach(function(c){
    var li=document.createElement('li');li.className='list-item gibby-item';
    var h='';
    if(c.img)h+='<div class="list-item-media" style="margin-bottom:4%;width:75%"><div class="list-item-media-inner" style="position:relative;padding-bottom:133.33%;overflow:hidden"><img class="list-image" loading="lazy" src="'+APP+c.img+'" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;display:block"></div></div>';
    h+='<div class="list-item-content"><div class="list-item-content__text-wrapper"><h2 class="list-item-content__title" style="max-width:100%">'+esc(c.title)+'</h2><div class="list-item-content__description" style="margin-top:1%;max-width:100%"><p style="white-space:pre-wrap">'+esc(c.when)+'</p>';
    esc(c.desc).split('\n\n').forEach(function(p){if(p.trim())h+='<p style="white-space:pre-wrap">'+p+'</p>'});
    h+='<p style="white-space:pre-wrap">'+esc(c.ages)+(c.price?'  I  '+esc(c.price):'')+'</p></div></div><div class="list-item-content__button-wrapper"><div class="list-item-content__button-container" style="margin-top:4%;max-width:100%"><a class="list-item-content__button sqs-block-button-element sqs-block-button-element--medium sqs-button-element--primary" style="color:inherit" href="'+c.url+'" target="_blank" rel="noopener">Register</a></div></div></div>';
    li.innerHTML=h;
    var k=dateKey(c),before=null;
    for(var j=0;j<list.children.length;j++){
      var el2=list.children[j];
      if(el2.classList.contains('gibby-item'))continue;
      var dEl=el2.querySelector('.list-item-content__description p');
      var kk=pdate(dEl?dEl.textContent:'');
      if(kk!==null&&kk>k){before=el2;break}
    }
    if(before){list.insertBefore(li,before)}else{list.appendChild(li)}
  });
}

/* ---------------- what's next carousel (homepage) ---------------- */
var wnState=null; /* {car, track, offset, wired} once we own the carousel */
function ownCarousel(){
  if(wnState&&document.contains(wnState.car))return wnState;
  var carOld=null;
  var secs=document.querySelectorAll('section.user-items-list-section [data-controller="UserItemsListCarousel"]');
  if(secs.length)carOld=secs[0];
  if(!carOld)return null;
  var car=carOld.cloneNode(true);
  carOld.replaceWith(car);
  var track=car.querySelector('.user-items-list-carousel__slides');
  if(!track)return null;
  /* restore images Squarespace's (now dead) lazy loader never loaded */
  car.querySelectorAll('img').forEach(function(im){
    if(!im.getAttribute('src')&&im.dataset.src){im.src=im.dataset.src+'?format=750w';im.style.objectFit='cover';im.style.width='100%';im.style.height='100%'}
  });
  var wrap=track.parentElement;
  wrap.style.overflow='hidden';
  track.style.display='flex';
  track.style.gap='20px';
  track.style.transition='transform .35s ease';
  wnState={car:car,track:track,offset:0,wired:false};
  return wnState;
}
function wnLayout(){
  if(!wnState)return null;
  var track=wnState.track,wrap=track.parentElement;
  var w=wrap.getBoundingClientRect().width;
  var cols=Math.max(1,Math.min(4,Math.floor(w/290)));
  var sw=Math.round((w-20*cols-20)/cols);
  var slides=Array.from(track.children);
  slides.forEach(function(li){li.style.transform='none';li.style.flex='0 0 '+sw+'px';li.style.maxWidth=sw+'px'});
  var maxOff=Math.max(0,slides.length-cols);
  if(wnState.offset>maxOff)wnState.offset=maxOff;
  track.style.transform='translateX('+(-wnState.offset*(sw+20))+'px)';
  return {cols:cols,maxOff:maxOff};
}
function renderCarousel(classes){
  var st=ownCarousel();
  if(!st)return 0;
  var track=st.track;
  track.querySelectorAll('.gibby-item').forEach(function(x){x.remove()});
  var tpl=track.querySelector('li.list-item');
  if(!tpl)return 0;
  var existing=Array.from(track.children).map(function(li){
    var t=li.querySelector('.list-item-content__title');
    return norm(t?t.textContent:'');
  });
  var added=0;
  classes.forEach(function(c){
    var n=norm(c.title);
    if(!n)return;
    if(existing.some(function(e){return e&&(e.indexOf(n)!==-1||n.indexOf(e)!==-1)}))return;
    var li=tpl.cloneNode(true);
    li.classList.add('gibby-item');
    li.style.transform='none';
    var title=li.querySelector('.list-item-content__title');
    if(title)title.textContent='Art Workshop: '+c.title;
    var dm=/([A-Za-z]+ \d{1,2})/.exec(c.when||'');
    var series=/(\d+)-week/.exec(c.when||'');
    var dateLabel=(dm?dm[1]:'')+(series?' · '+series[1]+'-week course':'');
    var desc=li.querySelector('.list-item-content__description');
    if(desc){
      var h='<p style="white-space:pre-wrap">'+esc(dateLabel)+'</p>';
      esc(c.desc||'').split('\n\n').forEach(function(p){if(p.trim())h+='<p style="white-space:pre-wrap">'+p+'</p>'});
      desc.innerHTML=h;
    }
    var inner=li.querySelector('.user-items-list-carousel__media-inner');
    if(inner){
      if(c.img){
        inner.innerHTML='<img src="'+APP+c.img+'" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;display:block">';
        inner.style.position='relative';inner.style.paddingBottom='150%';inner.style.overflow='hidden';
      }else if(inner.parentElement){inner.parentElement.style.display='none'}
    }
    var a=li.querySelector('a.list-item-content__button');
    if(a){a.href=c.url;a.target='_blank';a.rel='noopener';a.textContent='Register'}
    var k=dateKey(c),before=null;
    var kids=Array.from(track.children);
    for(var j=0;j<kids.length;j++){
      var dEl=kids[j].querySelector('.list-item-content__description p');
      var kk=pdate(dEl?dEl.textContent:'');
      if(kk!==null&&kk>k){before=kids[j];break}
    }
    track.insertBefore(li,before);
    added++;
  });
  if(!st.wired){
    st.wired=true;
    st.car.querySelectorAll('button').forEach(function(b){
      var left=/--l\b|--left/.test(b.className);
      b.addEventListener('click',function(e){
        e.preventDefault();
        var g=wnLayout();if(!g)return;
        st.offset=Math.max(0,Math.min(st.offset+(left?-1:1),g.maxOff));
        wnLayout();
      });
    });
    window.addEventListener('resize',wnLayout);
  }
  wnLayout();
  return added;
}

/* ---------------- boot ---------------- */
function ginit(){
  var olds=document.querySelectorAll('#gibbywrap,#gibbyframe');
  for(var i=0;i<olds.length;i++)olds[i].remove();
  var onWorkshops=location.pathname.indexOf('artworkshops')!==-1;
  var onHome=location.pathname==='/'||location.pathname==='';
  if(!onWorkshops&&!onHome)return;
  var tries=0;
  function load(){
    fetch(APP+'/embed.json').then(function(r){return r.json()}).then(function(d){
      var rules=readOverrides();
      var classes=applyOverrides(d.classes||[],rules);
      var msg='v3';
      if(onWorkshops){
        var list=document.querySelector('.user-items-list-simple');
        if(list){
          list.querySelectorAll('.gibby-item').forEach(function(x){x.remove()});
          renderList(list,classes);
          msg+=' list='+classes.length;
        }else{msg+=' list-missing'}
      }
      if(onHome){
        msg+=' carousel+'+renderCarousel(classes);
      }
      rep(msg+' (rules='+rules.length+')');
    }).catch(function(e){rep('fetch failed try '+tries+': '+e);if(++tries<6)setTimeout(load,2000*tries)})
  }
  load()
}
document.addEventListener('DOMContentLoaded',ginit);
window.addEventListener('mercury:load',ginit);
if(document.readyState==='interactive'||document.readyState==='complete')ginit();
})();
