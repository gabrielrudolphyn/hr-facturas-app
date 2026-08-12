import streamlit as st
import sqlite3, re
from pathlib import Path
from datetime import datetime
import pandas as pd
from pypdf import PdfReader

DB = Path("facturas_multiempresa.db")
UPLOADS = Path("uploads")
UPLOADS.mkdir(exist_ok=True)
st.set_page_config(page_title="Control de Facturas y Cobranza", layout="wide")

MONTHS={"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    c=conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY,name TEXT,rut TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY,filename TEXT UNIQUE,doc_type TEXT,company_id INTEGER,uploaded_at TEXT,raw_text TEXT);
    CREATE TABLE IF NOT EXISTS invoices(
      id INTEGER PRIMARY KEY,document_id INTEGER,company_id INTEGER,number INTEGER,kind TEXT,issue_date TEXT,
      issuer_name TEXT,issuer_rut TEXT,client_name TEXT,client_rut TEXT,description TEXT,
      uf REAL,uf_value REAL,net INTEGER,tax INTEGER,total INTEGER,status TEXT DEFAULT 'Vigente',credit_note_ref TEXT);
    CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY,document_id INTEGER,company_id INTEGER,payment_date TEXT,origin_name TEXT,
      origin_rut TEXT,amount INTEGER,description TEXT,status TEXT DEFAULT 'Pendiente de asignar',
      invoice_id INTEGER,confidence REAL DEFAULT 0);
    """)
    c.commit(); c.close()
init()

def clean_rut(s):
    x=re.sub(r"[^0-9Kk]","",s or "")
    return (x[:-1]+"-"+x[-1].upper()) if len(x)>1 else x

def digits(s): return re.sub(r"[^0-9Kk]","",s or "").upper()
def spaces(s): return re.sub(r"\s+"," ",s or "").strip()
def money(s): return int(re.sub(r"[^0-9]","",s or "0"))

def dec(s):
    if not s: return None
    s=s.strip()
    if "," in s: s=s.replace(".","").replace(",",".")
    elif s.count(".") and len(s.split(".")[-1])==3: s=s.replace(".","")
    try:return float(s)
    except:return None

def pdf_text(path):
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)

def doc_type(t):
    u=t.upper()
    if "NOTA DE CREDITO" in u:return "nota_credito"
    if "FACTURA ELECTRONICA" in u or "FACTURA NO AFECTA O" in u or "FACTURA EXENTA" in u:return "factura"
    if "ESTADO DE CUENTA" in u or "CARTOLAS HISTÓRICAS" in u or "DETALLE DE TRANSACCION" in u:return "cartola"
    return "desconocido"

def issue_date(t):
    m=re.search(r"Fecha Emision:\s*(\d{1,2})\s+de\s+([A-Za-záéíóúñÑ]+)\s+del\s+(\d{4})",t,re.I)
    if not m:return None
    mon=m.group(2).lower().translate(str.maketrans("áéíóú","aeiou"))
    return datetime(int(m.group(3)),MONTHS[mon],int(m.group(1))).date().isoformat()

def upsert_company(name,rut):
    rut=clean_rut(rut)
    if not rut:return None
    c=conn()
    c.execute("INSERT OR IGNORE INTO companies(name,rut) VALUES(?,?)",(spaces(name),rut))
    c.execute("UPDATE companies SET name=? WHERE rut=? AND ?<>''",(spaces(name),rut,spaces(name)))
    c.commit()
    r=c.execute("SELECT id FROM companies WHERE rut=?",(rut,)).fetchone()
    c.close()
    return r["id"] if r else None

def parse_invoice(t):
    u=t.upper()
    # emisor: RUT inmediatamente antes del tipo de DTE
    m=re.search(r"R\.U\.T\.:\s*([0-9.\-\sKk]+)\s*(?:FACTURA|NOTA DE CREDITO)",t,re.I|re.S)
    issuer_rut=clean_rut(m.group(1)) if m else ""
    # emisor: bloque inicial antes de Giro
    pre=re.split(r"\bGiro\s*:",t,flags=re.I)[0]
    il=[spaces(x) for x in pre.splitlines() if spaces(x)]
    issuer_name=" ".join(il[-4:]) if il else ""
    # cliente
    m=re.search(r"SEÑOR\(ES\):\s*(.+?)(?=\nR\.U\.T\.:)",t,re.I|re.S)
    client=spaces(m.group(1)) if m else ""
    m=re.search(r"SEÑOR\(ES\):.*?\nR\.U\.T\.:\s*([0-9.\-\sKk]+)",t,re.I|re.S)
    client_rut=clean_rut(m.group(1)) if m else ""
    # número
    m=re.search(r"(?:FACTURA(?:\s+NO AFECTA O\s+EXENTA)?\s+ELECTRONICA|NOTA DE CREDITO\s+ELECTRONICA).*?N[º°]?\s*(\d+)",t,re.I|re.S)
    num=int(m.group(1)) if m else None
    totals=re.findall(r"TOTAL\s*\$\s*([\d.]+)",t,re.I)
    total=money(totals[-1]) if totals else 0
    m=re.search(r"MONTO NETO\s*\$\s*([\d.]+)",t,re.I); net=money(m.group(1)) if m else 0
    m=re.search(r"I\.V\.A\.\s*19%\s*\$\s*([\d.]+)",t,re.I); tax=money(m.group(1)) if m else 0
    if not net:
        m=re.search(r"EXENTO\s*\$\s*([\d.]+)",t,re.I); net=money(m.group(1)) if m else total
    m=re.search(r"([\d.,]+)\s*UF\s+([\d.,]+)\s+([\d.]+)",t,re.I)
    uf,ufv=(dec(m.group(1)),dec(m.group(2))) if m else (None,None)
    m=re.search(r"Codigo\s+Descripcion.*?\n(.*?)(?=\nReferencias:|\nForma de Pago:|\nMONTO NETO|\nEXENTO|\nTimbre Electrónico)",t,re.I|re.S)
    desc=spaces(m.group(1))[:500] if m else ""
    kind="Factura Exenta" if ("FACTURA NO AFECTA O" in u or "FACTURA EXENTA" in u) else "Factura"
    return dict(number=num,issue_date=issue_date(t),issuer_name=issuer_name,issuer_rut=issuer_rut,
                client_name=client,client_rut=client_rut,description=desc,uf=uf,uf_value=ufv,
                net=net,tax=tax,total=total,kind=kind)

def parse_note(t):
    d=parse_invoice(t)
    m=re.search(r"ANULA DOCUMENTO.*?N[°º]\s*(\d+)",t,re.I|re.S)
    if not m:m=re.search(r"Factura.*?N[°º]\s*(\d+)",t,re.I|re.S)
    d["ref"]=int(m.group(1)) if m else None
    return d

def bank_company(t):
    m=re.search(r"Empresa:\s*(.+?)\s+RUT empresa:\s*([0-9.\-]+)",t,re.I)
    if m:return spaces(m.group(1)),clean_rut(m.group(2))
    # Estado de Cuenta: tomar línea después de SR(A)(ES), si existe
    lines=[spaces(x) for x in t.splitlines() if spaces(x)]
    for i,x in enumerate(lines):
        if x.upper()=="SR(A)(ES)" and i+1<len(lines): return lines[i+1],""
    return "",""

def parse_bank(t):
    out=[]; u=t.upper()
    if "CARTOLAS HISTÓRICAS" in u:
        lines=[spaces(x) for x in t.splitlines() if spaces(x)]
        j="\n".join(lines)
        pat=re.compile(r"(\d{2}/\d{2}/\d{4})\s+\$\s*([\d.]+)\s+(.+?)(?=\n\d{2}/\d{2}/\d{4}\s+\$|\Z)",re.S)
        for m in pat.finditer(j):
            ds,amt,desc=m.groups(); U=desc.upper()
            if any(k in U for k in ["PAGO RECURRENTE","COM.MANTENCION","COMPRA ","TRANSF.INTERNET A","TRANSF A ","INTERESES","IMPUESTO SOBREGIRO"]): continue
            if not any(k in U for k in ["TRANSF.","TRANSF DE","DEPOSITO","ABONO"]): continue
            compact=re.sub(r"[^0-9A-Za-z]","",desc)
            rm=re.search(r"(0?\d{7,9}[0-9Kk])",compact)
            rut=clean_rut(rm.group(1)) if rm else ""
            out.append(dict(date=datetime.strptime(ds,"%d/%m/%Y").date().isoformat(),name=spaces(desc)[:160],rut=rut,amount=money(amt),description=spaces(desc)[:500]))
    elif "ESTADO DE CUENTA" in u:
        # Banco Edwards: filas DD/MM y columnas cargo/abono.
        ym=re.findall(r"\d{2}/\d{2}/(\d{4})",t); year=int(ym[-1]) if ym else datetime.now().year
        for line in [spaces(x) for x in t.splitlines() if spaces(x)]:
            m=re.match(r"(\d{2}/\d{2})\s+(.+)",line)
            if not m:continue
            dm,rest=m.groups(); U=rest.upper()
            if "SALDO " in U or "COMISION" in U:continue
            if not any(k in U for k in ["ABONO","DEPOSITO","TRANSFER","TRANSF"]):continue
            nums=re.findall(r"\b\d{1,3}(?:\.\d{3})+\b",rest)
            if not nums:continue
            amt=money(nums[-2] if len(nums)>=2 else nums[-1])
            d,mo=map(int,dm.split("/"))
            try:ds=datetime(year,mo,d).date().isoformat()
            except:continue
            out.append(dict(date=ds,name=rest[:160],rut="",amount=amt,description=rest[:500]))
    return out

def reconcile(cid):
    c=conn()
    invs=c.execute("SELECT * FROM invoices WHERE company_id=? AND status='Vigente'",(cid,)).fetchall()
    pays=c.execute("SELECT * FROM payments WHERE company_id=? AND status!='Conciliado'",(cid,)).fetchall()
    for p in pays:
        best=None; score_best=0
        for i in invs:
            score=70 if p["amount"]==i["total"] else 0
            if p["origin_rut"] and i["client_rut"] and digits(p["origin_rut"])==digits(i["client_rut"]):score+=25
            pw=set(re.findall(r"[A-Z0-9]+",(p["origin_name"] or "").upper()))
            iw=set(re.findall(r"[A-Z0-9]+",(i["client_name"] or "").upper()))
            if len(pw&iw)>=2:score+=10
            elif len(pw&iw)==1:score+=5
            if score>score_best:best,score_best=i,score
        if best and score_best>=75:
            c.execute("UPDATE payments SET status='Conciliado',invoice_id=?,confidence=? WHERE id=?",(best["id"],min(score_best/100,1),p["id"]))
    c.commit(); c.close()

def save_doc(f):
    path=UPLOADS/f.name; path.write_bytes(f.getvalue())
    t=pdf_text(path); typ=doc_type(t); cname=crut=""; cid=None
    parsed=None
    if typ=="factura":
        parsed=parse_invoice(t); cname,crut=parsed["issuer_name"],parsed["issuer_rut"]; cid=upsert_company(cname,crut)
    elif typ=="nota_credito":
        parsed=parse_note(t); cname,crut=parsed["issuer_name"],parsed["issuer_rut"]; cid=upsert_company(cname,crut)
    elif typ=="cartola":
        cname,crut=bank_company(t); cid=upsert_company(cname,crut) if crut else None

    c=conn()
    c.execute("INSERT OR IGNORE INTO documents(filename,doc_type,company_id,uploaded_at,raw_text) VALUES(?,?,?,?,?)",(f.name,typ,cid,datetime.now().isoformat(timespec="seconds"),t))
    c.commit()
    did=c.execute("SELECT id FROM documents WHERE filename=?",(f.name,)).fetchone()["id"]

    if typ=="factura" and cid and parsed["number"] and parsed["total"]:
        exists=c.execute("SELECT 1 FROM invoices WHERE company_id=? AND number=? AND issue_date=?",(cid,parsed["number"],parsed["issue_date"])).fetchone()
        if not exists:
            c.execute("""INSERT INTO invoices(document_id,company_id,number,kind,issue_date,issuer_name,issuer_rut,client_name,client_rut,description,uf,uf_value,net,tax,total)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(did,cid,parsed["number"],parsed["kind"],parsed["issue_date"],parsed["issuer_name"],parsed["issuer_rut"],parsed["client_name"],parsed["client_rut"],parsed["description"],parsed["uf"],parsed["uf_value"],parsed["net"],parsed["tax"],parsed["total"]))
    elif typ=="nota_credito" and cid and parsed.get("ref"):
        c.execute("UPDATE invoices SET status='Anulada',credit_note_ref=? WHERE company_id=? AND number=?",(f.name,cid,parsed["ref"]))
    elif typ=="cartola":
        if not cid and cname:
            row=c.execute("SELECT id FROM companies WHERE UPPER(name) LIKE ? LIMIT 1",(f"%{cname.upper()}%",)).fetchone()
            if row: cid=row["id"]; c.execute("UPDATE documents SET company_id=? WHERE id=?",(cid,did))
        for p in parse_bank(t):
            if cid:
                ex=c.execute("SELECT 1 FROM payments WHERE company_id=? AND payment_date=? AND amount=? AND description=?",(cid,p["date"],p["amount"],p["description"])).fetchone()
                if not ex:c.execute("INSERT INTO payments(document_id,company_id,payment_date,origin_name,origin_rut,amount,description) VALUES(?,?,?,?,?,?,?)",(did,cid,p["date"],p["name"],p["rut"],p["amount"],p["description"]))
    c.commit(); c.close()
    if cid:reconcile(cid)
    return typ,cname,crut

def df(sql,params=()):
    c=conn(); x=pd.read_sql_query(sql,c,params=params); c.close(); return x

st.title("Control de Facturas y Cobranza")
st.caption("Versión multiempresa: facturas SII + cartolas bancarias + conciliación.")

companies=df("SELECT * FROM companies ORDER BY name")
cid=None
if not companies.empty:
    opts={f"{r['name']} · {r['rut']}":int(r["id"]) for _,r in companies.iterrows()}
    sel=st.selectbox("Empresa",list(opts)); cid=opts[sel]

tabs=st.tabs(["Dashboard","Subir documentos","Facturas","Pagos","Documentos"])
with tabs[1]:
    fs=st.file_uploader("Arrastra facturas, notas de crédito y cartolas PDF",type=["pdf"],accept_multiple_files=True)
    if fs and st.button("Procesar documentos",type="primary"):
        rows=[]
        for f in fs:
            try:
                typ,n,r=save_doc(f); rows.append([f.name,typ,n,r,"OK"])
            except Exception as e: rows.append([f.name,"error","","",str(e)])
        st.dataframe(pd.DataFrame(rows,columns=["Archivo","Tipo","Empresa detectada","RUT","Resultado"]),use_container_width=True)
        st.info("Si apareció una empresa nueva, recarga la página.")

with tabs[0]:
    if not cid: st.info("Sube una factura para crear la primera empresa.")
    else:
        inv=df("SELECT * FROM invoices WHERE company_id=? AND status='Vigente'",(cid,))
        pay=df("SELECT * FROM payments WHERE company_id=?",(cid,))
        rec=pay[pay.status=="Conciliado"] if not pay.empty else pay
        pen=pay[pay.status!="Conciliado"] if not pay.empty else pay
        vals=[int(inv.total.sum()) if not inv.empty else 0,int(pay.amount.sum()) if not pay.empty else 0,int(rec.amount.sum()) if not rec.empty else 0,int(pen.amount.sum()) if not pen.empty else 0]
        for col,(lab,val) in zip(st.columns(4),zip(["Facturado vigente","Pagos recibidos","Conciliados","Sin asignar"],vals)):
            col.metric(lab,f"${val:,.0f}".replace(",","."))
        st.subheader("Pagos pendientes de revisión")
        if pen.empty: st.success("No hay pagos pendientes.")
        else: st.dataframe(pen[["payment_date","origin_name","origin_rut","amount","description","status"]],use_container_width=True)

with tabs[2]:
    if cid:
        x=df("""SELECT i.*,COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id=i.id AND p.status='Conciliado'),0) pagado FROM invoices i WHERE company_id=? ORDER BY issue_date DESC""",(cid,))
        if not x.empty:
            x["saldo"]=x.apply(lambda r:0 if r.status=="Anulada" else max(0,r.total-r.pagado),axis=1)
            st.dataframe(x[["number","kind","issue_date","client_name","client_rut","description","net","tax","total","pagado","saldo","status","credit_note_ref"]],use_container_width=True)

with tabs[3]:
    if cid:
        x=df("""SELECT p.*,i.number invoice_number FROM payments p LEFT JOIN invoices i ON i.id=p.invoice_id WHERE p.company_id=? ORDER BY payment_date DESC""",(cid,))
        st.dataframe(x,use_container_width=True)
        pending=df("SELECT id,payment_date,origin_name,amount FROM payments WHERE company_id=? AND status!='Conciliado'",(cid,))
        invs=df("SELECT id,number,client_name,total FROM invoices WHERE company_id=? AND status='Vigente'",(cid,))
        if not pending.empty and not invs.empty:
            po={f"{r.id} · {r.payment_date} · {r.origin_name} · ${int(r.amount):,}".replace(",","."):int(r.id) for r in pending.itertuples()}
            io={f"F{int(r.number)} · {r.client_name} · ${int(r.total):,}".replace(",","."):int(r.id) for r in invs.itertuples()}
            ps=st.selectbox("Pago",list(po)); ins=st.selectbox("Factura",list(io))
            if st.button("Asignar pago a factura"):
                c=conn(); c.execute("UPDATE payments SET status='Conciliado',invoice_id=?,confidence=1 WHERE id=?",(io[ins],po[ps])); c.commit(); c.close()
                st.success("Pago asignado.")

with tabs[4]:
    if cid: st.dataframe(df("SELECT filename,doc_type,uploaded_at FROM documents WHERE company_id=? ORDER BY uploaded_at DESC",(cid,)),use_container_width=True)
