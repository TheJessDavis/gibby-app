/* Gibby site embed v2 — renders approved classes onto theeverett.org/artworkshops
   and obeys an editor-authored GIBBY OVERRIDES text block (Option 2: Squarespace
   editors can hide or reword any app card from inside Squarespace).

   Squarespace loads this via:
     <script src="https://gibby-app-ddjo.onrender.com/site-embed.js" defer></script>
   so future changes deploy from the app with no Squarespace edits.

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
   the card. Lines that match nothing continue the previous field. */
(function(){
function rep(m){try{fetch('https://gibby-app-ddjo.onrender.com/api/client-error',{method:'POST',mode:'no-cors',headers:{'Content-Type':'text/plain'},body:JSON.stringify({section:'site-embed',message:m,agent:navigator.userAgent,path:location.pathname})})}catch(e){}}
var MM={january:1,february:2,march:3,april:4,may:5,june:6,july:7,august:8,september:9,october:10,november:11,december:12};
function pdate(txt){var m=/(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})/i.exec(txt||'');if(!m)return null;var mo=MM[m[1].toLowerCase()],dy=+m[2];return(mo>=8?2026:2027)*10000+mo*100+dy}

function readOverrides(){
  var rules=[],host=null;
  var blocks=document.querySelectorAll('.sqs-block-html,.sqs-html-content');
  for(var i=0;i<blocks.length;i++){
    var t=(blocks[i].innerText||'').trim();
    if(/^gibby overrides/i.test(t)){host=blocks[i];break}
  }
  if(!host)return rules;
  var wrap=host.closest?host.closest('.sqs-block')||host:host;
  wrap.style.display='none';
  var cur=null,lastField=null;
  var lines=(host.innerText||'').split('\n');
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

function ginit(){
  var olds=document.querySelectorAll('.gibby-item,#gibbywrap,#gibbyframe');
  for(var i=0;i<olds.length;i++)olds[i].remove();
  if(location.pathname.indexOf('artworkshops')===-1)return;
  var list=document.querySelector('.user-items-list-simple');
  function esc(x){return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
  var tries=0;
  function load(){
    fetch('https://gibby-app-ddjo.onrender.com/embed.json').then(function(r){return r.json()}).then(function(d){
      var o2=document.querySelectorAll('.gibby-item');for(var i=0;i<o2.length;i++)o2[i].remove();
      if(!list){rep('no list found');return}
      var rules=readOverrides();
      var classes=applyOverrides(d.classes||[],rules);
      classes.forEach(function(c){
        var li=document.createElement('li');li.className='list-item gibby-item';
        var h='';
        if(c.img)h+='<div class="list-item-media" style="margin-bottom:4%;width:75%"><div class="list-item-media-inner" style="position:relative;padding-bottom:133.33%;overflow:hidden"><img class="list-image" loading="lazy" src="https://gibby-app-ddjo.onrender.com'+c.img+'" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;display:block"></div></div>';
        h+='<div class="list-item-content"><div class="list-item-content__text-wrapper"><h2 class="list-item-content__title" style="max-width:100%">'+esc(c.title)+'</h2><div class="list-item-content__description" style="margin-top:1%;max-width:100%"><p style="white-space:pre-wrap">'+esc(c.when)+'</p>';
        esc(c.desc).split('\n\n').forEach(function(p){if(p.trim())h+='<p style="white-space:pre-wrap">'+p+'</p>'});
        h+='<p style="white-space:pre-wrap">'+esc(c.ages)+(c.price?'  I  '+esc(c.price):'')+'</p></div></div><div class="list-item-content__button-wrapper"><div class="list-item-content__button-container" style="margin-top:4%;max-width:100%"><a class="list-item-content__button sqs-block-button-element sqs-block-button-element--medium sqs-button-element--primary" style="color:inherit" href="'+c.url+'" target="_blank" rel="noopener">Register</a></div></div></div>';
        li.innerHTML=h;
        var pp=c.date.split('-');var k=(+pp[0])*10000+(+pp[1])*100+(+pp[2]);
        var before=null;
        for(var j=0;j<list.children.length;j++){
          var el2=list.children[j];
          if(el2.classList.contains('gibby-item'))continue;
          var dEl=el2.querySelector('.list-item-content__description p');
          var kk=pdate(dEl?dEl.textContent:'');
          if(kk!==null&&kk>k){before=el2;break}
        }
        if(before){list.insertBefore(li,before)}else{list.appendChild(li)}
      });
      rep('v2 built '+classes.length+' items ('+rules.length+' override rules)');
    }).catch(function(e){rep('fetch failed try '+tries+': '+e);if(++tries<6)setTimeout(load,2000*tries)})
  }
  load()
}
document.addEventListener('DOMContentLoaded',ginit);
window.addEventListener('mercury:load',ginit);
if(document.readyState==='interactive'||document.readyState==='complete')ginit();
})();
