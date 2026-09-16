import assert from 'node:assert/strict'
import { before, after, afterEach, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { JSDOM } from 'jsdom'

let dom, dir, mount, flushPromises, MarketInfo, Settings, wrapper
const globals = new Map()
const state = () => globalThis.__pluginPresentationTest
const runtimePlugin = overrides => ({ plugin: 'org.example/fixture', display_name: '测试提供方', version: '1.0.0', enabled: true, runtime_available: true, lifecycle_state: 'active', trust_state: 'third_party', trust_class: 'third_party', provider_contracts: [{contract:'radio_provider',features:['catalog','resolve_stream']}], owned_schemes: ['fixture'], ownership:[{scheme:'fixture',mode:'legacy'}], runtime:{type:'python',python_version_range:'>=3.11.0 <3.15.0',environment_status:'ready',dependencies:[],dependency_count:0}, permissions:{requested:[],pending:[],approved:[]}, market:{package_id:'third::fixture'}, ...overrides })
before(async () => {
  dom = new JSDOM('<!doctype html><html><body></body></html>')
  for (const key of ['window','document','Document','navigator','Element','Node','SVGElement','HTMLElement']) {
    globals.set(key,Object.getOwnPropertyDescriptor(globalThis,key))
    Object.defineProperty(globalThis,key,{configurable:true,value:key==='window'?dom.window:dom.window[key]})
  }
  ;({mount,flushPromises}=await import('@vue/test-utils'))
  const {build}=await import('vite'); const {default:vue}=await import('@vitejs/plugin-vue')
  dir=await mkdtemp(path.join(tmpdir(),'waveflow-plugin-ui-'))
  for (const [name,entry] of [['market','src/components/MarketPluginInfo.vue'],['settings','src/views/settings/PluginsSettings.vue']]) {
    await build({configFile:false,logLevel:'silent',plugins:[{
      name:'plugin-ui-fixture',enforce:'pre',
      resolveId(id){if(id==='../../api/plugins')return '\0plugins-api';if(id==='../../stores/toast')return '\0plugins-toast'},
      load(id){
        if(id==='\0plugins-toast')return 'export const useToastStore=()=>({askConfirm:(...a)=>globalThis.__pluginPresentationTest.confirm(...a),success:()=>{},error:()=>{}})'
        if(id==='\0plugins-api')return `export const fetchPlugins=async()=>({plugins:globalThis.__pluginPresentationTest.plugins}); export const fetchPlugin=async identity=>globalThis.__pluginPresentationTest.plugins.find(p=>p.plugin===identity); export const fetchDeveloperMode=async()=>({enabled:false}); export const pluginErrorMessage=()=>"操作失败"; ${['approvePluginPermission','disablePlugin','enablePlugin','installDeveloperPlugin','recoverPlugin','revokePluginPermission','setDeveloperMode','setPluginOwnership'].map(fn=>`export const ${fn}=(...args)=>globalThis.__pluginPresentationTest.action('${fn}',args);`).join('')}`
      },
    },vue()],build:{outDir:path.join(dir,name),lib:{entry:new URL('../../'+entry,import.meta.url).pathname,formats:['es'],fileName:()=> 'view.mjs'},rollupOptions:{external:['vue','vue-router'],output:{paths:{vue:import.meta.resolve('vue'),'vue-router':import.meta.resolve('vue-router')}}}}})
  }
  MarketInfo=(await import(pathToFileURL(path.join(dir,'market/view.mjs')))).default
  Settings=(await import(pathToFileURL(path.join(dir,'settings/view.mjs')))).default
})
afterEach(()=>{wrapper?.unmount();wrapper=null;document.body.innerHTML='';delete globalThis.__pluginPresentationTest})
after(async()=>{dom?.window.close();for(const[key,d]of globals)d?Object.defineProperty(globalThis,key,d):delete globalThis[key];if(dir)await rm(dir,{recursive:true,force:true})})
test('Market explicit publisher/contracts/permissions; manage appears only installed',async()=>{
  const pkg={publisher:{name:'第三方发布者'},plugin:{publisher_id:'org.example',plugin_id:'fixture',provider_contracts:[{contract:'radio_provider',features:['catalog']}],owned_schemes:[{scheme:'fixture'}],permissions:['network.direct'],platforms:[{runtime:'python'}]}}
  wrapper=mount(MarketInfo,{props:{pkg}})
  assert.match(wrapper.text(),/第三方发布者/);assert.match(wrapper.text(),/电台目录/);assert.match(wrapper.text(),/高风险/)
  assert.equal(wrapper.find('button').exists(),false)
  await wrapper.setProps({pkg:{...pkg,installed:true}})
  await wrapper.get('button').trigger('click');assert.equal(wrapper.emitted('manage').length,1)
  assert.equal(wrapper.get('details').element.open,false)
})
test('Market no-permission official binary retains sandbox warning and all dependencies',()=>{
  const dependencies=Array.from({length:12},(_,i)=>({name:'lib'+i,version:'1.0'}))
  wrapper=mount(MarketInfo,{props:{pkg:{publisher:{name:'WaveFlow'},installed:true,installed_trust_state:'official',plugin:{permissions:[],dependencies,platforms:[{runtime:'subprocess'}],provider_contracts:[{contract:'tv_provider',features:['resolve_stream']}],owned_schemes:[]}}}})
  assert.match(wrapper.text(),/未申请额外权限/);assert.match(wrapper.text(),/不代表插件运行于安全沙箱/)
  assert.match(wrapper.text(),/独立进程/);assert.match(wrapper.text(),/lib11/)
  assert.doesNotMatch(wrapper.text(),/电台目录/)
  assert.doesNotMatch(wrapper.text(),/支持 V1/)
})
async function openSettings(plugin){
  globalThis.__pluginPresentationTest={plugins:[plugin],calls:[],confirm:async()=>false,action:(name,args)=>state().calls.push({name,args})}
  wrapper=mount(Settings,{attachTo:document.body,global:{stubs:{RouterLink:{props:['to'],template:'<a :href="to"><slot /></a>'}}}});await flushPromises()
  await wrapper.findAll('button').find(x=>x.text()==='详情').trigger('click');await flushPromises()
}
test('Settings radio permissions absent, runtime shown, technical disclosure closed',async()=>{
  await openSettings(runtimePlugin())
  assert.match(document.body.textContent,/电台目录/);assert.match(document.body.textContent,/此插件未申请额外权限/)
  assert.match(document.body.textContent,/已信任的第三方发布者/)
  assert.equal(document.querySelector('.plugin-technical').open,false)
  assert.match(document.body.textContent,/没有通用的插件参数编辑器/)
})
test('Settings disabled state blocks ownership and cancelled enable sends no action',async()=>{
  await openSettings(runtimePlugin({enabled:false,runtime_available:false,lifecycle_state:'disabled'}))
  const ownership=[...document.querySelectorAll('button')].find(x=>x.textContent==='交由插件解析')
  assert.equal(ownership.disabled,true)
  assert.match(document.body.textContent,/已停用/)
  const enable=[...document.querySelectorAll('button')].find(x=>x.textContent==='启用插件');enable.click();await flushPromises()
  assert.equal(state().calls.length,0)
})
test('Settings permission approval preserves exact API identity after confirmation',async()=>{
  const p={name:'network.direct',risk:'high'}
  await openSettings(runtimePlugin({permissions:{requested:[p],pending:[p],approved:[]}}))
  state().confirm=async()=>true
  const approve=[...document.querySelectorAll('button')].find(x=>x.textContent==='允许');approve.click();await flushPromises()
  assert.deepEqual(state().calls,[{name:'approvePluginPermission',args:['org.example/fixture','network.direct','third::fixture']}])
})
