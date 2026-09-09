import assert from 'node:assert/strict'
import { before, after, afterEach, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { JSDOM } from 'jsdom'

let dom, dir, Home, mount, flushPromises, ref, createPinia, setActivePinia, usePlayerStore, wrapper
const saved = new Map()
const originalFetch = globalThis.fetch
const response = body => ({ok:true,json:async()=>body})
const row = {station_id:'radio_fixture',name:'音乐新闻测试',country:'TW',group_name:'目录甲',metadata:{tag:'自定义'},sources:[{source_id:'source_fixture'}]}
before(async () => {
  dom = new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:5173/'})
  for (const key of ['window','document','navigator','Element','Node','SVGElement','HTMLElement','getComputedStyle']) {
    saved.set(key,Object.getOwnPropertyDescriptor(globalThis,key))
    Object.defineProperty(globalThis,key,{configurable:true,value:key==='window'?dom.window:dom.window[key]})
  }
  saved.set('ResizeObserver',Object.getOwnPropertyDescriptor(globalThis,'ResizeObserver'))
  globalThis.ResizeObserver=class{observe(){} disconnect(){}}
  ;({mount,flushPromises}=await import('@vue/test-utils'))
  ;({ref}=await import('vue'))
  ;({createPinia,setActivePinia}=await import('pinia'))
  ;({usePlayerStore}=await import('../../src/stores/player.js'))
  const {build}=await import('vite'); const {default:vue}=await import('@vitejs/plugin-vue')
  dir=await mkdtemp(path.join(tmpdir(),'waveflow-radio-home-'))
  await build({configFile:false,logLevel:'silent',plugins:[vue()],build:{outDir:dir,lib:{entry:new URL('../../src/views/Home.vue',import.meta.url).pathname,formats:['es'],fileName:()=> 'home.mjs'},rollupOptions:{external:['vue','pinia'],output:{paths:{vue:import.meta.resolve('vue'),pinia:import.meta.resolve('pinia')}}}}})
  Home=(await import(pathToFileURL(path.join(dir,'home.mjs')))).default
})
afterEach(()=>{wrapper?.unmount();wrapper=null;globalThis.fetch=originalFetch;document.body.innerHTML=''})
after(async()=>{dom?.window.close();for(const[key,d]of saved)d?Object.defineProperty(globalThis,key,d):delete globalThis[key];if(dir)await rm(dir,{recursive:true,force:true})})
function openHome(){
  const pinia=createPinia();setActivePinia(pinia)
  wrapper=mount(Home,{attachTo:document.body,global:{plugins:[pinia],provide:{searchQuery:ref(''),scrollRef:ref(document.body)},stubs:{TagFilterRow:{props:['items','isActive'],emits:['select'],template:'<div><button v-for="item in items" :key="item.value" @click="$emit(\'select\',item)">{{item.label}}</button></div>'}}}})
  return usePlayerStore()
}
const button = text => wrapper.findAll('button').find(b=>b.text()===text)

test('single catalog failure is visible; retry reloads the same authority and explicit filters keep authority',async()=>{
  let failCatalog=true
  const calls=[]
  globalThis.fetch=async url=>{
    calls.push(String(url))
    return failCatalog?{ok:false}:response({stations:[row],catalog_states:[]})
  }
  const store=openHome();await flushPromises()
  assert.match(wrapper.text(),/电台目录暂时无法加载/)
  failCatalog=false;await button('重试加载').trigger('click');await flushPromises()
  assert.equal(calls.filter(url=>url.endsWith('/api/radio/stations')).length,2)
  assert.doesNotMatch(wrapper.text(),/部分电台目录暂时不可用/)
  assert.ok(button('自定义'))
  assert.equal(wrapper.find('select').exists(),false)
  assert.equal(button('目录甲'),undefined)
  await button('台湾').trigger('click')
  assert.equal(wrapper.findAll('.channel-card').length,1)
  await wrapper.get('.channel-card').trigger('click')
  assert.equal(store.currentStation,'radio_fixture')
  assert.equal(store.radioPlaybackIntent,'station_click')
  store.setLoading(false);await flushPromises()
  assert.equal(wrapper.get('.channel-card').classes().includes('channel-card-current'),true)
  store.togglePlay(false);await flushPromises()
  assert.equal(wrapper.get('.channel-card').classes().includes('channel-card-current'),false)
  assert.equal(wrapper.get('.channel-card').attributes('aria-current'),'true')
})

test('single catalog request is cancelled on unmount and cannot publish late results',async()=>{
  let finish
  const signals=[]
  globalThis.fetch=async(url,options)=>{
    signals.push(options.signal)
    return new Promise(resolve=>{finish=resolve})
  }
  const store=openHome();await flushPromises()
  assert.equal(store.stationList.length,0)
  assert.match(wrapper.text(),/正在加载电台目录/)
  wrapper.unmount();wrapper=null
  assert.equal(signals[0].aborted,true)
  assert.equal(signals[0].aborted,true)
  finish(response({stations:[row]}));await flushPromises()
  assert.deepEqual(store.stationList,[])
})

test('explicit provinces stay in the region row; names and directory brands never invent region or type', async () => {
  const stations = [
    { ...row, station_id: 'fujian_news', country: 'CN', group_name: '福建', name: '广东体育广播', metadata: { tag: 'news' } },
    { ...row, station_id: 'fujian_untyped', country: 'CN', group_name: '福建', metadata: {} },
    { ...row, station_id: 'unclassified', country: 'CN', group_name: 'MyRadio', name: '福建音乐广播', metadata: {} },
  ]
  globalThis.fetch = async () => response({ stations })
  openHome()
  await flushPromises()
  assert.equal(wrapper.find('select').exists(), false)
  assert.ok(button('福建'))
  assert.ok(button('中国大陆'))
  assert.equal(button('广东'), undefined)
  assert.equal(button('MyRadio'), undefined)
  assert.equal(button('音乐'), undefined)
  assert.equal(button('体育'), undefined)
  await button('福建').trigger('click')
  assert.equal(wrapper.findAll('.channel-card').length, 2)
  await button('新闻').trigger('click')
  assert.equal(wrapper.findAll('.channel-card').length, 1)
  assert.equal(wrapper.get('.card-channel-name').text(), '广东体育广播')
  await button('全部类型').trigger('click')
  assert.equal(wrapper.findAll('.channel-card').length, 2)
  await button('全部地区').trigger('click')
  assert.equal(wrapper.findAll('.channel-card').length, 3)
})

test('failed initial catalogs and valid empty catalogs are distinct and retryable',async()=>{
  let fail=true
  globalThis.fetch=async()=>fail?{ok:false}:response({stations:[]})
  openHome();await flushPromises()
  assert.match(wrapper.text(),/电台目录暂时无法加载/)
  fail=false;await button('重试加载').trigger('click');await flushPromises()
  assert.match(wrapper.text(),/暂无可用电台/)
  assert.doesNotMatch(wrapper.text(),/电台目录暂时无法加载/)
})
