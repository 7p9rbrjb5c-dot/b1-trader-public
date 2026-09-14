
import os, json, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

KST=timezone(timedelta(hours=9))
BASE="https://mockapi.kiwoom.com" if os.getenv("KIWOOM_ENV","real").lower() in ("mock","demo") else "https://api.kiwoom.com"
START=(9,0); STOP=(15,20)
W={"vwap_position":3.0,"ma_alignment":3.0,"pullback_reclaim":3.0,"breakout_quality":3.0,"volume_expansion":2.0,"rsi_quality":2.0,"macd_momentum":2.0,"intraday_momentum":2.0}
TOKEN=None; TOKEN_TS=0.0
FAILS=0; LAST_SCAN=0.0; LAST_HEALTH=0.0; LAST_STATE=None

def emit(event, **kw):
    out={"system":"B1","event":event,"ts_kst":datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
         "live_orders_enabled":False,"dry_run":True}
    out.update(kw)
    print(json.dumps(out,ensure_ascii=False),flush=True)

def post(path, body, api_id=None, token=None, timeout=15):
    h={"Content-Type":"application/json;charset=UTF-8"}
    if token: h["authorization"]="Bearer "+token
    if api_id: h["api-id"]=api_id
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode(),headers=h,method="POST")
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode())

def token():
    global TOKEN,TOKEN_TS
    if TOKEN and time.time()-TOKEN_TS < 20*3600: return TOKEN
    k=os.getenv("APP_KEY","").strip(); s=os.getenv("APP_SECRET","").strip()
    if not k or not s: raise RuntimeError("APP_KEY/APP_SECRET missing")
    d=post("/oauth2/token",{"grant_type":"client_credentials","appkey":k,"secretkey":s})
    t=str(d.get("token") or "").strip()
    if not t: raise RuntimeError("token issue failed: "+str(d.get("return_msg")))
    TOKEN=t; TOKEN_TS=time.time(); emit("kiwoom_token_ok")
    return TOKEN

def api(path, api_id, body):
    d=post(path,body,api_id,token())
    if d.get("return_code") not in (None,0,"0"):
        if str(d.get("return_code")) in ("8005","401"):
            global TOKEN
            TOKEN=None
            d=post(path,body,api_id,token())
        if d.get("return_code") not in (None,0,"0"):
            raise RuntimeError(f"{api_id}: {d.get('return_msg',d.get('return_code'))}")
    return d

def fnum(x):
    try: return float(str(x or "0").replace(",","").replace("+",""))
    except: return 0.0
def anum(x): return abs(fnum(x))

def session(now):
    if os.getenv("B1_FORCE_CLOSED","false").lower()=="true": return "CLOSED_DAY"
    if now.weekday()>=5: return "CLOSED_DAY"
    hm=(now.hour,now.minute)
    return "ACTIVE" if START<=hm<=STOP else "ARMED"

def account_health():
    d=api("/api/dostk/acnt","kt00001",{"qry_tp":"3","dmst_stex_tp":"KRX"})
    emit("account_health",ok=True,source="kt00001")
    return True

def ema(vals,n):
    if not vals:return 0.0
    a=2/(n+1); e=vals[0]
    for v in vals[1:]: e=a*v+(1-a)*e
    return e

def rsi(vals,n=14):
    if len(vals)<n+1:return 50.0
    ds=[vals[i]-vals[i-1] for i in range(1,len(vals))]
    g=sum(max(x,0) for x in ds[-n:])/n
    l=sum(max(-x,0) for x in ds[-n:])/n
    return 100.0 if l==0 else 100-100/(1+g/l)

def chart_score(code):
    d=api("/api/dostk/chart","ka10080",{"stk_cd":code,"tic_scope":"1","upd_stkpc_tp":"1"})
    rows=d.get("stk_min_pole_chart_qry") or []
    rows=[r for r in rows if isinstance(r,dict)]
    rows=sorted(rows,key=lambda r:str(r.get("cntr_tm","")))[-60:]
    if len(rows)<26:return 0.0,{"reason":"insufficient_bars"}
    c=[anum(r.get("cur_prc")) for r in rows]; h=[anum(r.get("high_pric")) for r in rows]
    v=[anum(r.get("trde_qty")) for r in rows]
    last=c[-1]; ma5=sum(c[-5:])/5; ma10=sum(c[-10:])/10; ma20=sum(c[-20:])/20
    vv=sum(c[i]*max(v[i],1) for i in range(len(c)))/sum(max(x,1) for x in v)
    feats={}
    feats["vwap_position"]=max(0,min(1,0.5+(last/vv-1)*25)) if vv else 0
    feats["ma_alignment"]=(int(last>ma5)+int(ma5>ma10)+int(ma10>ma20))/3
    feats["pullback_reclaim"]=1.0 if last>ma5 and min(c[-4:-1])<=ma5 else 0.3 if last>ma5 else 0
    prevhi=max(h[-11:-1]); feats["breakout_quality"]=max(0,min(1,0.5+(last/prevhi-1)*50)) if prevhi else 0
    avgv=sum(v[-21:-1])/20 if sum(v[-21:-1]) else 0
    feats["volume_expansion"]=max(0,min(1,(v[-1]/avgv-0.8)/1.7)) if avgv else 0
    rv=rsi(c); feats["rsi_quality"]=1.0 if 50<=rv<=70 else max(0,1-abs(rv-60)/30)
    m=ema(c,12)-ema(c,26); sig=ema([ema(c[:i],12)-ema(c[:i],26) for i in range(26,len(c)+1)],9)
    feats["macd_momentum"]=1.0 if m>sig and m>0 else 0.4 if m>sig else 0
    mom=last/c[-6]-1 if c[-6] else 0; feats["intraday_momentum"]=max(0,min(1,mom/0.02))
    score=round(sum(W[k]*feats[k] for k in W),2)
    return min(20.0,score),{"rsi":round(rv,1),"features":{k:round(v,3) for k,v in feats.items()}}

def execution_strength_score(code):
    d=api("/api/dostk/stkinfo","ka10003",{"stk_cd":code})
    rows=d.get("cntr_infr") or []
    rows=[r for r in rows if isinstance(r,dict)]
    strength=anum(rows[0].get("cntr_str")) if rows else 0.0
    score=round(max(0,min(20,(strength-80.0)/70.0*20.0)),2)
    return score,round(strength,2)

def scans():
    tv=api("/api/dostk/rkinfo","ka10032",{"mrkt_tp":"000","mang_stk_incls":"0","stex_tp":"1"}).get("trde_prica_upper") or []
    tv=[r for r in tv if isinstance(r,dict)][:8]
    out=[]
    for i,row in enumerate(tv):
        c=str(row.get("stk_cd","")).replace("_AL","").replace("_NX","")
        if not c: continue
        tvs=round(max(0,20*(1-i/29)),2)
        es,strength=execution_strength_score(c)
        cs,meta=chart_score(c)
        out.append({"code":c,"name":row.get("stk_nm"),"price":anum(row.get("cur_prc")),
                    "trading_value":tvs,"execution_strength":es,"execution_strength_raw":strength,
                    "chart_entry":cs,"market_score_60":round(tvs+es+cs,2),
                    "full_score_status":"WAITING_NEWS_FUTURE","chart_meta":meta})
    out.sort(key=lambda x:x["market_score_60"],reverse=True)
    emit("candidate_scan",count=len(out),top=out[:5])

emit("worker_start",mode="paper_monitor_v3",base=BASE)
try:
    account_health()
except Exception as e:
    emit("account_health",ok=False,error=type(e).__name__,detail=str(e)[:180])

while True:
    now=datetime.now(KST); st=session(now)
    if st!=LAST_STATE:
        emit("session_state",state=st); LAST_STATE=st
    try:
        if st=="ACTIVE":
            if time.time()-LAST_HEALTH>=300:
                account_health(); LAST_HEALTH=time.time()
            if time.time()-LAST_SCAN>=60:
                scans(); LAST_SCAN=time.time()
            FAILS=0
    except Exception as e:
        FAILS+=1; emit("worker_error",state=st,fail_count=FAILS,error=type(e).__name__,detail=str(e)[:220])
        if FAILS>=3:
            emit("watchdog_restart",reason="three_consecutive_failures")
            raise SystemExit(2)
    emit("heartbeat",state=st,scanner="market60_attached",news_future="pending")
    time.sleep(30)
