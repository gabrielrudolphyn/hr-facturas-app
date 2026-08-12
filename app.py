import streamlit as st
import sqlite3, re
from pathlib import Path
from datetime import datetime
import pandas as pd
from pypdf import PdfReader

DB_PATH=Path('hr_facturas.db'); UPLOAD_DIR=Path('uploads'); UPLOAD_DIR.mkdir(exist_ok=True)
st.set_page_config(page_title='HR Arquitectos · Facturas y Cobranza',layout='wide')

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY,filename TEXT UNIQUE,doc_type TEXT,uploaded_at TEXT,raw_text TEXT);
    CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY,document_id INTEGER,invoice_number INTEGER,issue_date TEXT,client_name TEXT,client_rut TEXT,project TEXT,description TEXT,uf REAL,uf_value REAL,total INTEGER,status_dte TEXT DEFAULT 'Vigente',credit_note_ref TEXT,UNIQUE(invoice_number,issue_date,client_rut));
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,document_id INTEGER,payment_date TEXT,origin_name TEXT,origin_rut TEXT,amount INTEGER,description TEXT,status TEXT DEFAULT 'Pendiente de asignar',invoice_id INTEGER,confidence REAL DEFAULT 0);
    '''); c.commit(); c.close()
init_db()

MONTHS={'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12}

def pdf_text(path): return '\n'.join((p.extract_text() or '') for p in PdfReader(str(path)).pages)
def clean_rut(s): return re.sub(r'\s+','',s or '').replace('.','')
def money(s): return int(re.sub(r'[^\d]','',s or '0'))
def dec(s):
    try:return float((s or '').strip().replace('.','').replace(',','.'))
    except:return None

def date_es(text):
    m=re.search(r'Fecha Emision:\s*(\d{1,2})\s+de\s+([A-Za-záéíóúñÑ]+)\s+del\s+(\d{4})',text,re.I)
    if not m:return None
    mon=m.group(2).lower().translate(str.maketrans('áéíóú','aeiou'))
    return datetime(int(m.group(3)),MONTHS[mon],int(m.group(1))).date().isoformat()

def detect(text):
    u=text.upper()
    if 'NOTA DE CREDITO' in u:return 'nota_credito'
    if 'FACTURA NO AFECTA O' in u or 'FACTURA EXENTA' in u:return 'factura'
    if 'CARTOLAS HISTÓRICAS' in u or 'DETALLE MOVIMIENTOS' in u:return 'cartola'
    return 'desconocido'

def parse_invoice(text):
    n=re.search(r'N[º°]\s*(\d+)',text,re.I)
    client=re.search(r'SEÑOR\(ES\):\s*(.+)',text,re.I)
    rut=re.search(r'SEÑOR\(ES\):.*?\nR\.U\.T\.:\s*([0-9.\-\s]+)',text,re.I|re.S)
    totals=re.findall(r'TOTAL\s*\$\s*([\d\.]+)',text,re.I)
    uf=re.search(r'([\d.,]+)\s*UF\s+([\d.,]+)\s+([\d.]+)',text,re.I)
    u=text.upper()
    if 'PASEO SAN PEDRO' in u: project='Paseo San Pedro - Etapa 02'
    elif 'LAS RASTRAS' in u or 'LAS RATRAS' in u: project='Las Rastras'
    elif 'DECATHLON' in u: project='Decathlon - Vitrina Reflectante'
    else: project=''
    desc=[x.strip() for x in text.splitlines() if any(k in x.upper() for k in ['ADELANTO','ANTICIPO','DISEÑO','DOCUMENTACIÓN','DOCUMENTACION','CONTRA ENTREGA','ARQUITECTURA PROYECTO'])]
    return dict(invoice_number=int(n.group(1)) if n else None,issue_date=date_es(text),client_name=client.group(1).strip() if client else '',client_rut=clean_rut(rut.group(1)) if rut else '',project=project,description=' | '.join(desc[:3]),uf=dec(uf.group(1)) if uf else None,uf_value=dec(uf.group(2)) if uf else None,total=money(totals[-1]) if totals else 0)

def parse_nc(text):
    ref=re.search(r'ANULA DOCUMENTO.*?N[°º]\s*(\d+)',text,re.I|re.S)
    return int(ref.group(1)) if ref else None

def parse_cartola(text):
    lines=[re.sub(r'\s+',' ',x.strip()) for x in text.splitlines() if x.strip()]
    joined='\n'.join(lines); out=[]
    pat=re.compile(r'(\d{2}/\d{2}/\d{4})\s+\$\s*([\d\.]+)\s+(.+?)(?=\n\d{2}/\d{2}/\d{4}\s+\$|\Z)',re.S)
    for m in pat.finditer(joined):
        ds,ams,desc=m.groups(); u=desc.upper()
        inbound=any(k in u for k in ['TRANSF. CQ','TRANSF. SERVICIOS','TRANSF. GRS','TRANSF DE IAN','DEV IMPUESTO'])
        charge=any(k in u for k in ['PAGO RECURRENTE','COM.MANTENCION','COMPRA','TRANSF.INTERNET A','INTERESES','IMPUESTO SOBREGIRO'])
        if not inbound or charge: continue
        name=''; rut=''
        if 'CQ ESTUDIO' in u:name='CQ ESTUDIO SPA';rut='76328336-4'
        elif 'SERVICIOS PROFE' in u:name='SERVICIOS PROFESIONALES CQ LIMITADA';rut='77733463-8'
        elif 'GRS SPA' in u:name='GRS SPA'
        elif 'IAN ALBERTO' in u:name='IAN ALBERTO HSU MENDEZ'
        elif 'DEV IMPUESTO' in u:name='TESORERÍA'
        out.append(dict(payment_date=datetime.strptime(ds,'%d/%m/%Y').date().isoformat(),origin_name=name,origin_rut=rut,amount=money(ams),description=re.sub(r'\s+',' ',desc).strip()[:500]))
    return out

def reconcile():
    c=db(); invs=c.execute("SELECT * FROM invoices WHERE status_dte='Vigente'").fetchall(); pays=c.execute("SELECT * FROM payments WHERE status!='Conciliado'").fetchall()
    for p in pays:
        best=None; score=0
        for i in invs:
            s=(70 if p['amount']==i['total'] else 0)+(25 if p['origin_rut'] and clean_rut(p['origin_rut'])==clean_rut(i['client_rut']) else 0)+(5 if p['origin_name'] and p['origin_name'].upper() in i['client_name'].upper() else 0)
            if s>score: score=s;best=i
        if best and score>=90:c.execute("UPDATE payments SET status='Conciliado',invoice_id=?,confidence=? WHERE id=?",(best['id'],score/100,p['id']))
    c.commit();c.close()

def save_doc(f):
    path=UPLOAD_DIR/f.name;path.write_bytes(f.getvalue());text=pdf_text(path);typ=detect(text)
    c=db();c.execute("INSERT OR IGNORE INTO documents(filename,doc_type,uploaded_at,raw_text) VALUES(?,?,?,?)",(f.name,typ,datetime.now().isoformat(timespec='seconds'),text));c.commit();doc=c.execute('SELECT * FROM documents WHERE filename=?',(f.name,)).fetchone();did=doc['id']
    if typ=='factura':
        x=parse_invoice(text)
        if x['invoice_number'] and x['issue_date'] and x['total']:
            c.execute('''INSERT OR IGNORE INTO invoices(document_id,invoice_number,issue_date,client_name,client_rut,project,description,uf,uf_value,total,status_dte,credit_note_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(did,x['invoice_number'],x['issue_date'],x['client_name'],x['client_rut'],x['project'],x['description'],x['uf'],x['uf_value'],x['total'],'Vigente',''))
    elif typ=='nota_credito':
        ref=parse_nc(text)
        if ref:c.execute("UPDATE invoices SET status_dte='Anulada',credit_note_ref=? WHERE invoice_number=?",('NC '+f.name,ref))
    elif typ=='cartola':
        for p in parse_cartola(text):
            exists=c.execute('SELECT 1 FROM payments WHERE payment_date=? AND amount=? AND description=?',(p['payment_date'],p['amount'],p['description'])).fetchone()
            if not exists:c.execute('INSERT INTO payments(document_id,payment_date,origin_name,origin_rut,amount,description) VALUES(?,?,?,?,?,?)',(did,p['payment_date'],p['origin_name'],p['origin_rut'],p['amount'],p['description']))
    c.commit();c.close();reconcile();return typ

def q(sql):
    c=db();df=pd.read_sql_query(sql,c);c.close();return df

st.title('HR Arquitectos · Facturas y Cobranza')
st.caption('Carga mensual de facturas y cartolas, con conciliación automática y revisión manual.')
t1,t2,t3,t4=st.tabs(['Dashboard','Subir documentos','Facturas','Pagos'])
with t2:
    fs=st.file_uploader('Arrastra facturas, notas de crédito y cartolas PDF',type=['pdf'],accept_multiple_files=True)
    if fs and st.button('Procesar documentos',type='primary'):
        rows=[]
        for f in fs:
            try:rows.append([f.name,save_doc(f),'OK'])
            except Exception as e:rows.append([f.name,'error',str(e)])
        st.dataframe(pd.DataFrame(rows,columns=['Archivo','Tipo','Resultado']),use_container_width=True)
with t1:
    inv=q("SELECT * FROM invoices WHERE status_dte='Vigente'");pay=q('SELECT * FROM payments');con=pay[pay.status=='Conciliado'] if not pay.empty else pay;pend=pay[pay.status!='Conciliado'] if not pay.empty else pay
    vals=[int(inv.total.sum()) if not inv.empty else 0,int(pay.amount.sum()) if not pay.empty else 0,int(con.amount.sum()) if not con.empty else 0,int(pend.amount.sum()) if not pend.empty else 0]
    cols=st.columns(4)
    for c,label,v in zip(cols,['Facturado vigente','Pagos recibidos','Conciliados','Sin asignar'],vals):c.metric(label,f"${v:,.0f}".replace(',','.'))
    if not inv.empty:
        x=inv.copy();x['issue_date']=pd.to_datetime(x.issue_date);x['Mes']=x.issue_date.dt.to_period('M').astype(str);st.subheader('Facturación mensual');st.bar_chart(x.groupby('Mes').total.sum())
    st.subheader('Pendientes de revisión');st.dataframe(pend[['payment_date','origin_name','origin_rut','amount','description','status']] if not pend.empty else pend,use_container_width=True)
with t3:
    df=q("SELECT i.*,COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.invoice_id=i.id AND p.status='Conciliado'),0) pagado FROM invoices i ORDER BY issue_date DESC")
    if not df.empty:
        df['saldo']=df.apply(lambda r:0 if r.status_dte=='Anulada' else max(0,r.total-r.pagado),axis=1);st.dataframe(df[['invoice_number','issue_date','client_name','client_rut','project','description','total','pagado','saldo','status_dte','credit_note_ref']],use_container_width=True)
    else:st.info('Todavía no hay facturas cargadas.')
with t4:
    df=q('SELECT p.*,i.invoice_number FROM payments p LEFT JOIN invoices i ON i.id=p.invoice_id ORDER BY p.payment_date DESC');st.dataframe(df,use_container_width=True)
    st.subheader('Asignación manual');p=q("SELECT id,payment_date,origin_name,amount FROM payments WHERE status!='Conciliado'");i=q("SELECT id,invoice_number,client_name,total FROM invoices WHERE status_dte='Vigente'")
    if not p.empty and not i.empty:
        po={f"{r.id} · {r.payment_date} · {r.origin_name or 'Sin nombre'} · ${int(r.amount):,}".replace(',','.'):int(r.id) for r in p.itertuples()};io={f"F{int(r.invoice_number)} · {r.client_name} · ${int(r.total):,}".replace(',','.'):int(r.id) for r in i.itertuples()}
        ps=st.selectbox('Pago',list(po)); ins=st.selectbox('Factura',list(io))
        if st.button('Asignar pago a factura'):
            c=db();c.execute("UPDATE payments SET status='Conciliado',invoice_id=?,confidence=1 WHERE id=?",(io[ins],po[ps]));c.commit();c.close();st.success('Pago asignado correctamente.')
