#!/usr/bin/env python3
"""Independent ND0593TV Provider Plugin using managed HTTP form POST."""
from __future__ import annotations
import argparse, json, sys, threading, time, uuid
from typing import Any
from urllib.parse import urlencode

API_URL = "https://app.0593tv.cn/jhxtapi/jhxt/Live/detail"
USER_AGENT = "QZWireless/20241122 CFNetwork/3860.500.112 Darwin/25.4.0"
CHANNELS = {"nd-culture": ("20", "宁德文化旅游"), "nd-news": ("21", "宁德新闻综合")}
ALIASES = {"20": "nd-culture", "21": "nd-news", "culture": "nd-culture", "news": "nd-news"}
WRITE_LOCK=threading.Lock(); CALLBACK_LOCK=threading.Lock(); CALLBACKS={}
def frame(v):
 b=json.dumps(v,ensure_ascii=False,separators=(",",":")).encode()
 with WRITE_LOCK: sys.stdout.buffer.write(f"Content-Length: {len(b)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode()+b); sys.stdout.buffer.flush()
def read_frame():
 h={}
 while True:
  line=sys.stdin.buffer.readline()
  if not line:return None
  if line==b"\r\n":break
  k,v=line.decode("ascii").rstrip("\r\n").split(": ",1);h[k.lower()]=v
 return json.loads(sys.stdin.buffer.read(int(h["content-length"])).decode())
def reply(r,result=None,error=None): frame({"protocol_version":"1.1","kind":"response","sender":"plugin","request_id":r["request_id"],"status":"error" if error else "ok","result":result,"error":error,"diagnostics":{}})
def err(code,message,retryable=False,category="provider"): return {"code":code,"message":message,"retryable":retryable,"category":category,"details":{}}
def fetch(r,payload):
 cid=f"plugin:{uuid.uuid4().hex}";event=threading.Event()
 with CALLBACK_LOCK:CALLBACKS[cid]=[event,None]
 frame({"protocol_version":"1.1","kind":"request","sender":"plugin","request_id":cid,"method":"core.http.fetch","plugin_instance":r.get("plugin_instance"),"deadline_unix_ms":int((time.time()+10)*1000),"context":{"parent_request_id":r["request_id"]},"payload":payload})
 if not event.wait(10):
  with CALLBACK_LOCK:CALLBACKS.pop(cid,None)
  raise RuntimeError("PLUGIN_TIMEOUT")
 with CALLBACK_LOCK:resp=CALLBACKS.pop(cid)[1] or {}
 if resp.get("status")=="error":raise RuntimeError(str((resp.get("error") or {}).get("code") or "TEMPORARY_UPSTREAM_FAILURE"))
 return resp.get("result") or {}
def resolve(r):
 resource=str((r.get("payload") or {}).get("resource_id") or "").strip("/").lower();key=ALIASES.get(resource,resource);entry=CHANNELS.get(key)
 if entry: lid,name=entry
 elif resource.isdigit():lid,name=resource,resource
 else:reply(r,error=err("RESOURCE_NOT_FOUND","ND0593TV channel is not supported"));return
 body=urlencode({"uid":"0","device":"","nid":"","lid":lid,"siteid":"1"})
 try:response=fetch(r,{"method":"POST","url":API_URL,"headers":{"Accept":"*/*","Content-Type":"application/x-www-form-urlencoded","User-Agent":USER_AGENT},"body":{"text":body},"response_mode":"json"})
 except RuntimeError as exc:
  code=str(exc); mapped=code if code in {"CAPABILITY_DENIED","PLUGIN_TIMEOUT","AUTH_FAILED","RATE_LIMITED"} else "TEMPORARY_UPSTREAM_FAILURE"
  reply(r,error=err(mapped,"ND0593TV upstream request failed",True,"network"));return
 data=response.get("body")
 if not isinstance(data,dict) or data.get("code")!=200:reply(r,error=err("TEMPORARY_UPSTREAM_FAILURE","ND0593TV upstream returned a business error",True));return
 url=str(((data.get("data") or {}).get("link") or "")).strip()
 if not url.startswith(("http://","https://")):reply(r,error=err("TEMPORARY_UPSTREAM_FAILURE",f"ND0593TV {name} returned no playable stream",True));return
 reply(r,{"descriptor_version":"1.0","transport":"hls","url":url,"headers":{},"credential_refs":[],"ttl_seconds":1800,"expires_at":None,"volatile_url":True,"requires_proxy":False,"warnings":[]})
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--identity",default="org.waveflow/nd0593tv");ap.add_argument("--version",default="1.0.0");a=ap.parse_args()
 while True:
  r=read_frame()
  if r is None:return
  if r.get("kind")=="response" and r.get("sender")=="core":
   with CALLBACK_LOCK:
    cb=CALLBACKS.get(r.get("request_id"))
    if cb:cb[1]=r;cb[0].set()
   continue
  m=r.get("method")
  if m=="runtime.hello":reply(r,{"protocol_version":"1.1","plugin":a.identity,"version":a.version,"provider_contracts":[{"contract":"tv_provider","contract_version":"1.0","features":["resolve_stream"]}],"owned_schemes":[{"scheme":"nd0593tv","contract":"tv_provider"}],"capabilities":["tv.resolve_stream"],"permissions":["network"]})
  elif m=="runtime.health":reply(r,{"healthy":True})
  elif m=="runtime.shutdown":reply(r,{"accepted":True});return
  elif m=="tv.resolve_stream":threading.Thread(target=resolve,args=(r,),daemon=True).start()
  else:reply(r,error=err("RESOURCE_NOT_FOUND","Provider method is not supported"))
if __name__=="__main__":main()
