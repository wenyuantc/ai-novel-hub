import {Editor, Extension, Node, Mark, mergeAttributes} from '@tiptap/core'
import Document from '@tiptap/extension-document'
import {StarterKit} from '@tiptap/starter-kit'
import {Plugin,NodeSelection} from '@tiptap/pm/state'
import {Slice} from '@tiptap/pm/model'
import {isHistoryTransaction,closeHistory} from '@tiptap/pm/history'

const MyDoc=Document.extend({addAttributes(){return {markers:{default:[]}}}})
const Marker=Mark.create({name:'annotation',inclusive:false,excludes:'',addAttributes(){return {id:{default:null}}},parseHTML(){return [{tag:'mark[data-id]'}]},renderHTML({HTMLAttributes}){return ['mark',mergeAttributes(HTMLAttributes),0]}})
const TrackMarkers=Extension.create({name:'trackMarkers',addProseMirrorPlugins(){return [new Plugin({appendTransaction(transactions,oldState,newState){const markers=newState.doc.attrs.markers||[];if(!markers.length)return null;const ids=new Set();newState.doc.descendants(n=>{for(const mark of n.marks||[])if(mark.type.name==='annotation')ids.add(mark.attrs.id)});let changed=false;const next=markers.map(m=>{if(m.status==='active'&&!ids.has(m.id)){changed=true;return {...m,status:'stale'}}return m});if(!changed)return null;return newState.tr.setDocAttribute('markers',next)}})]}})
const Boundaries = Extension.create({
 name:'boundaries',
 addGlobalAttributes(){return [{types:['paragraph','heading'],attributes:{prefix:{default:'\n',rendered:false,keepOnSplit:false}}}]},
})
const PreservePrefixes=Extension.create({name:'preservePrefixes',addProseMirrorPlugins(){return [new Plugin({appendTransaction(transactions,oldState,newState){if(transactions.some(t=>t.getMeta('safe-scene-load')||t.getMeta('intentional-line-change')||t.getMeta('fix-prefix')))return null;const collect=doc=>{const out={prose:[]};let section=null;doc.descendants((node,pos,parent)=>{if(node.type.name==='section'){section=node.attrs.id;out[section]=[];node.descendants((child,off)=>{if(child.isTextblock)out[section].push({pos:pos+1+off,text:child.textContent,prefix:child.attrs.prefix})});section=null;return false}if(node.isTextblock)out.prose.push({pos,text:node.textContent,prefix:node.attrs.prefix})});return out};const before=collect(oldState.doc),after=collect(newState.doc);let tr=null;for(const scope of Object.keys(before)){const a=before[scope],b=after[scope];if(!b||a.length!==b.length||a.some((x,i)=>x.text!==b[i].text))continue;for(let i=0;i<a.length;i++){if(a[i].prefix===b[i].prefix)continue;tr=tr||newState.tr;const n=tr.doc.nodeAt(b[i].pos);tr.setNodeMarkup(b[i].pos,undefined,{...n.attrs,prefix:a[i].prefix})}}return tr?.setMeta('fix-prefix',true)||null}})]}})
const SectionGuard=Extension.create({name:'sectionGuard',priority:2500,addProseMirrorPlugins(){return [new Plugin({filterTransaction(tr,state){if(!tr.docChanged)return true;const ids=doc=>{const found=[];doc.descendants(node=>{if(node.type.name==='section')found.push(node.attrs.id)});return found};const before=ids(state.doc),after=ids(tr.doc);if(new Set(after).size!==after.length)return false;if(!tr.getMeta('safe-scene-load')&&!tr.getMeta('explicit-section-delete')&&!isHistoryTransaction(tr)&&before.some(id=>!after.includes(id)))return false;return true}})]}})
const SafePaste=Extension.create({name:'safePaste',priority:2100,addProseMirrorPlugins(){return [new Plugin({props:{handlePaste(view,event){const text=event.clipboardData?.getData('text/plain');if(text===undefined)return false;const json=docFromText(text);const doc=view.state.schema.nodeFromJSON(json);const slice=new Slice(doc.content,1,1);view.dispatch(view.state.tr.replaceSelection(slice).setMeta('intentional-line-change',true).scrollIntoView());event.preventDefault();return true}}})]}})
const SafeEnter=Extension.create({
 name:'safeEnter',priority:2000,
 addKeyboardShortcuts(){return {'Enter':()=>{
  let used=''
  return this.editor.chain().first(({commands})=>[
   ()=>{const ok=commands.splitListItem('listItem');if(ok)used='split';return ok},
   ()=>{const ok=commands.newlineInCode();if(ok)used='other';return ok},
   ()=>{const ok=commands.createParagraphNear();if(ok)used='other';return ok},
   ()=>{const ok=commands.liftEmptyBlock();if(ok)used='other';return ok},
   ()=>{const ok=commands.splitBlock();if(ok)used='split';return ok},
  ]).command(({tr})=>{
   const p=tr.selection.$from.parent
   if(used==='split')tr.setMeta('intentional-line-change',true)
   if(used==='split'&&(p.type.name==='paragraph'||p.type.name==='heading'))tr.setNodeMarkup(tr.selection.$from.before(),undefined,{...p.attrs,prefix:'\n'})
   return true
  }).run()
 }}}
})
const Section=Node.create({
 name:'section', group:'block', content:'block+', defining:true, isolating:true, draggable:true,
 addAttributes(){return {id:{default:'section-1'}}},
 parseHTML(){return [{tag:'div[data-section]'}]},
 renderHTML({HTMLAttributes}){return ['div',mergeAttributes(HTMLAttributes,{'data-section':'','class':'section'}),0]},
 addNodeView(){return ({node,getPos,editor})=>{const dom=document.createElement('div');dom.className='section';dom.draggable=true;dom.dataset.id=node.attrs.id;const button=document.createElement('button');button.textContent='拖动区块';button.dataset.dragHandle='';button.contentEditable='false';button.addEventListener('mousedown',()=>{const pos=getPos();if(typeof pos==='number')editor.view.dispatch(editor.state.tr.setSelection(NodeSelection.create(editor.state.doc,pos))) });const contentDOM=document.createElement('div');dom.append(button,contentDOM);return {dom,contentDOM}}},
})
const editor=new Editor({enableInputRules:false,enablePasteRules:false,element:document.querySelector('#editor'), extensions:[StarterKit.configure({document:false,trailingNode:false,code:false,codeBlock:false,horizontalRule:false,link:false,strike:false,underline:false}), MyDoc,Marker,TrackMarkers,Boundaries,PreservePrefixes,SectionGuard,SafePaste,SafeEnter,Section], content:{type:'doc',content:[{type:'paragraph',attrs:{prefix:''}}]}, enableContentCheck:true})
function docFromText(input){
 const parts=input.split(/(\r\n|\r|\n)/); const content=[]
 for(let i=0;i<parts.length;i+=2){ const p={type:'paragraph',attrs:{prefix:i===0?'':parts[i-1]}}; if(parts[i]) p.content=[{type:'text',text:parts[i]}]; content.push(p) }
 return {type:'doc',content}
}
function prose(node){
 if(node.type==='section') return ''
 if(node.type==='text') return node.text
 if(node.type==='hardBreak') return '\n'
 const t=(node.content??[]).map(prose).join('')
 return node.type==='paragraph'||node.type==='heading' ? (node.attrs?.prefix??'')+t : t
}
function load(text){editor.chain().setContent(docFromText(text),{errorOnInvalidContent:true}).command(({tr})=>{tr.setMeta('addToHistory',false);tr.setMeta('safe-scene-load',true);return true}).run();return prose(editor.getJSON())}
function segments(){
 const a=[];let offset=0
 const walk=(node,pos)=>{
  if(node.type.name==='section') return
  if(node.type.name==='paragraph'||node.type.name==='heading'){
   const prefix=node.attrs.prefix||''
   a.push({type:'prefix',pm:pos-1,to:pos+1,start:offset,end:offset+Array.from(prefix).length,text:prefix})
   offset+=Array.from(prefix).length
  }
  if(node.isText){a.push({type:'text',pm:pos,to:pos+node.nodeSize,start:offset,end:offset+Array.from(node.text).length,text:node.text});offset+=Array.from(node.text).length;return}
  if(node.type.name==='hardBreak'){a.push({type:'break',pm:pos,to:pos+1,start:offset,end:offset+1,text:'\n'});offset++;return}
  node.forEach((child,off)=>walk(child,pos+(node.type.name==='doc'?0:1)+off))
 }
 walk(editor.state.doc,0); return a
}
function state(){return {text:prose(editor.getJSON()),json:editor.getJSON(),segments:segments()}}
window.poc={editor,load,state,docFromText,prose}
document.querySelector('#reset').onclick=()=>load('甲😀\r\n\n乙é\t  \r第三行  \n')
document.querySelector('#bold').onclick=()=>editor.chain().focus().toggleBold().run()
document.querySelector('#heading').onclick=()=>editor.chain().focus().toggleHeading({level:2}).run()
document.querySelector('#list').onclick=()=>editor.chain().focus().toggleBulletList().run()
load('甲😀\r\n\n乙é\t  \r第三行  \n')
window.events=[]
editor.on('transaction',({transaction})=>{if(transaction.docChanged)window.events.push({before:transaction.before.toJSON(),after:transaction.doc.toJSON(),steps:transaction.steps.map((s,i)=>{const ranges=[];transaction.mapping.maps[i].forEach((a,b,c,d)=>ranges.push({a,b,c,d}));return {json:s.toJSON(),ranges}})})})
window.poc.mark=(from,to,id)=>editor.chain().setTextSelection({from,to}).setMark('annotation',{id}).command(({tr})=>{tr.setDocAttribute('markers',[...tr.doc.attrs.markers,{id,status:'active'}]);return true}).run()
window.poc.insertSection=(id)=>{
 const from=editor.state.selection.$from;const inside=from.parentOffset>0&&from.parentOffset<from.parent.content.size
 return editor.chain().insertContent({type:'section',attrs:{id},content:[{type:'paragraph',attrs:{prefix:''},content:[{type:'text',text:'备选'}]}]}).command(({tr})=>{if(!inside)return true;let after=null;tr.doc.descendants((node,pos)=>{if(node.type.name==='section'&&node.attrs.id===id)after=pos+node.nodeSize});if(after===null)return false;const n=tr.doc.nodeAt(after);if(!n||!['paragraph','heading'].includes(n.type.name))return false;tr.setNodeMarkup(after,undefined,{...n.attrs,prefix:''});return true}).run()
}
window.poc.cursors=()=>{
 let cp=0;const entries=[]
 const add=(pm,offset)=>{if(!entries.some(x=>x.pm===pm&&x.offset===offset))entries.push({pm,offset})}
 editor.state.doc.descendants((node,pos)=>{
  if(node.type.name==='section')return false
  if(node.type.name==='paragraph'||node.type.name==='heading'){cp+=Array.from(node.attrs.prefix||'').length;add(pos+1,cp)}
  if(node.isText){let pm=pos;add(pm,cp);for(const glyph of Array.from(node.text)){pm+=glyph.length;cp++;add(pm,cp)}}
  if(node.type.name==='hardBreak'){add(pos,cp);cp++;add(pos+1,cp)}
 })
 return entries
}
window.poc.toCp=(pm)=>{const found=window.poc.cursors().filter(x=>x.pm===pm);return found.length===1?found[0].offset:null}
window.poc.toPm=(offset)=>window.poc.cursors().filter(x=>x.offset===offset).map(x=>x.pm)

window.poc.breakHistory=()=>editor.view.dispatch(closeHistory(editor.state.tr))
