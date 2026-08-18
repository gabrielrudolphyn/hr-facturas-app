import sqlite3, shutil, hashlib, json
from pathlib import Path
from datetime import datetime

def map_dte_type(kind):
    k=(kind or '').upper()
    if 'EXENTA' in k: return 34
    if 'NOTA DE CREDITO' in k or 'NOTA DE CRÉDITO' in k: return 61
    if 'FACTURA' in k: return 33
    return None

def preflight(db_path):
    c=sqlite3.connect(db_path); c.row_factory=sqlite3.Row
    r={
      'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],
      'companies':c.execute('SELECT COUNT(*) FROM companies').fetchone()[0],
      'invoices':c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0],
      'payments':c.execute('SELECT COUNT(*) FROM payments').fetchone()[0],
      'reconciled_payments':c.execute('SELECT COUNT(*) FROM payments WHERE invoice_id IS NOT NULL').fetchone()[0],
      'invoice_total':c.execute('SELECT COALESCE(SUM(total),0) FROM invoices').fetchone()[0],
      'reconciled_total':c.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE invoice_id IS NOT NULL').fetchone()[0],
    }
    r['unknown_types']=[dict(x) for x in c.execute('SELECT id,kind FROM invoices').fetchall() if map_dte_type(x['kind']) is None]
    r['duplicates']=[dict(x) for x in c.execute('''SELECT company_id,number,kind,issuer_rut,COUNT(*) n FROM invoices GROUP BY company_id,number,kind,issuer_rut HAVING COUNT(*)>1''').fetchall()]
    r['invalid_rows']=[dict(x) for x in c.execute('''SELECT id,company_id,number,issue_date,issuer_rut,client_rut,total FROM invoices WHERE company_id IS NULL OR number IS NULL OR number<=0 OR issue_date IS NULL OR issue_date='' OR issuer_rut IS NULL OR issuer_rut='' OR client_rut IS NULL OR client_rut='' OR total IS NULL OR total<0''').fetchall()]
    c.close()
    r['critical_ok']=r['integrity']=='ok' and not r['unknown_types'] and not r['duplicates'] and not r['invalid_rows']
    return r

def create_schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS dte_documents(
      id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
      direction TEXT NOT NULL CHECK(direction IN ('EMITIDO','RECIBIDO')),
      source TEXT NOT NULL DEFAULT 'SII' CHECK(source IN ('SII','PDF','MANUAL')),
      dte_type INTEGER NOT NULL, folio INTEGER NOT NULL, issue_date TEXT NOT NULL,
      issuer_rut TEXT NOT NULL, issuer_name TEXT, receiver_rut TEXT NOT NULL, receiver_name TEXT,
      net_amount INTEGER DEFAULT 0, exempt_amount INTEGER DEFAULT 0, tax_amount INTEGER DEFAULT 0,
      total_amount INTEGER NOT NULL, sii_status_code TEXT, sii_status_glosa TEXT, sii_status_detail TEXT,
      sii_num_atencion TEXT, sii_checked_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
      source_hash TEXT, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      legacy_invoice_id INTEGER, legacy_document_id INTEGER,
      FOREIGN KEY(company_id) REFERENCES companies(id),
      UNIQUE(company_id,direction,dte_type,folio,issuer_rut)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_dte_legacy_invoice ON dte_documents(legacy_invoice_id) WHERE legacy_invoice_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS dte_app_state(
      dte_id INTEGER PRIMARY KEY,
      collection_status TEXT DEFAULT 'PENDIENTE' CHECK(collection_status IN ('PENDIENTE','PARCIAL','PAGADA','VENCIDA','NO_COBRAR','EN_REVISION')),
      due_date TEXT, paid_amount INTEGER DEFAULT 0, notes TEXT, assigned_to TEXT,
      last_collection_at TEXT, next_collection_at TEXT, manual_override INTEGER DEFAULT 0 CHECK(manual_override IN (0,1)),
      legacy_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(dte_id) REFERENCES dte_documents(id)
    );
    CREATE TABLE IF NOT EXISTS dte_relations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, source_dte_id INTEGER NOT NULL,
      target_dte_id INTEGER, target_dte_type INTEGER, target_folio INTEGER, target_issuer_rut TEXT,
      relation_type TEXT NOT NULL CHECK(relation_type IN ('ANULA','CORRIGE_MONTO','CORRIGE_TEXTO','REFERENCIA')),
      reference_code TEXT, reference_reason TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(company_id) REFERENCES companies(id), FOREIGN KEY(source_dte_id) REFERENCES dte_documents(id),
      FOREIGN KEY(target_dte_id) REFERENCES dte_documents(id),
      UNIQUE(source_dte_id,target_dte_type,target_folio,relation_type)
    );
    CREATE TABLE IF NOT EXISTS payment_allocations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id INTEGER NOT NULL, dte_id INTEGER NOT NULL,
      allocated_amount INTEGER NOT NULL CHECK(allocated_amount>0), confidence REAL DEFAULT 0,
      allocation_type TEXT DEFAULT 'AUTOMATIC' CHECK(allocation_type IN ('AUTOMATIC','SUGGESTED','MANUAL','MIGRATED')),
      created_at TEXT NOT NULL, FOREIGN KEY(payment_id) REFERENCES payments(id), FOREIGN KEY(dte_id) REFERENCES dte_documents(id),
      UNIQUE(payment_id,dte_id)
    );
    CREATE TABLE IF NOT EXISTS sii_sync_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
      direction TEXT NOT NULL CHECK(direction IN ('EMITIDO','RECIBIDO')), period_from TEXT, period_to TEXT,
      started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCESS','PARTIAL','ERROR')),
      documents_found INTEGER DEFAULT 0, documents_inserted INTEGER DEFAULT 0, documents_updated INTEGER DEFAULT 0,
      documents_unchanged INTEGER DEFAULT 0, documents_error INTEGER DEFAULT 0, error_message TEXT,
      FOREIGN KEY(company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS dte_sii_status_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, dte_id INTEGER NOT NULL, checked_at TEXT NOT NULL,
      status_code TEXT, status_glosa TEXT, status_detail TEXT, num_atencion TEXT, sync_run_id INTEGER,
      FOREIGN KEY(dte_id) REFERENCES dte_documents(id), FOREIGN KEY(sync_run_id) REFERENCES sii_sync_runs(id)
    );
    CREATE TABLE IF NOT EXISTS sii_sync_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, sync_run_id INTEGER NOT NULL, dte_id INTEGER, dte_type INTEGER,
      folio INTEGER, issuer_rut TEXT, action TEXT NOT NULL CHECK(action IN ('INSERTED','UPDATED','UNCHANGED','ERROR')),
      error_message TEXT, processed_at TEXT NOT NULL,
      FOREIGN KEY(sync_run_id) REFERENCES sii_sync_runs(id), FOREIGN KEY(dte_id) REFERENCES dte_documents(id)
    );
    CREATE TABLE IF NOT EXISTS migration_v53_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, migrated_at TEXT NOT NULL, old_invoice_id INTEGER,
      new_dte_id INTEGER, action TEXT NOT NULL, details TEXT, UNIQUE(old_invoice_id)
    );
    ''')

def source_hash(inv,dte_type):
    s='|'.join(map(str,[inv['company_id'],'EMITIDO',dte_type,inv['number'],inv['issue_date'],inv['issuer_rut'],inv['client_rut'],inv['net'] or 0,inv['tax'] or 0,inv['total'] or 0]))
    return hashlib.sha256(s.encode()).hexdigest()

def execute(db_path):
    pre=preflight(db_path)
    if not pre['critical_ok']:
        raise RuntimeError('Preflight falló: '+json.dumps(pre,ensure_ascii=False))
    backup_dir=Path('backups'); backup_dir.mkdir(exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    backup=backup_dir/f'facturas_pre_v53_{stamp}.db'
    shutil.copy2(db_path,backup)
    bc=sqlite3.connect(backup)
    if bc.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        bc.close(); raise RuntimeError('Backup no pasó integrity_check')
    bc.close()
    c=sqlite3.connect(db_path); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON')
    now=datetime.now().isoformat(timespec='seconds')
    try:
        c.execute('BEGIN IMMEDIATE'); create_schema(c)
        for inv in c.execute('SELECT * FROM invoices ORDER BY id').fetchall():
            dt=map_dte_type(inv['kind']); exempt=int(inv['net'] or 0) if dt==34 else 0; net=0 if dt==34 else int(inv['net'] or 0)
            c.execute('''INSERT OR IGNORE INTO dte_documents(company_id,direction,source,dte_type,folio,issue_date,issuer_rut,issuer_name,receiver_rut,receiver_name,net_amount,exempt_amount,tax_amount,total_amount,first_seen_at,last_seen_at,source_hash,active,legacy_invoice_id,legacy_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (inv['company_id'],'EMITIDO','PDF',dt,inv['number'],inv['issue_date'],inv['issuer_rut'],inv['issuer_name'],inv['client_rut'],inv['client_name'],net,exempt,int(inv['tax'] or 0),int(inv['total'] or 0),now,now,source_hash(inv,dt),1,inv['id'],inv['document_id']))
            dte=c.execute('SELECT id FROM dte_documents WHERE legacy_invoice_id=?',(inv['id'],)).fetchone()
            if not dte: raise RuntimeError(f'No se pudo mapear invoice {inv["id"]}')
            st='NO_COBRAR' if (inv['status'] or '').upper()=='ANULADA' else 'PENDIENTE'
            c.execute('''INSERT OR IGNORE INTO dte_app_state(dte_id,collection_status,paid_amount,legacy_status,created_at,updated_at) VALUES(?,?,?,?,?,?)''',(dte['id'],st,0,inv['status'],now,now))
            c.execute('''INSERT OR IGNORE INTO migration_v53_log(migrated_at,old_invoice_id,new_dte_id,action,details) VALUES(?,?,?,?,?)''',(now,inv['id'],dte['id'],'MIGRATED','invoices → dte_documents'))
        for p in c.execute('SELECT id,invoice_id,amount,confidence FROM payments WHERE invoice_id IS NOT NULL').fetchall():
            dte=c.execute('SELECT id FROM dte_documents WHERE legacy_invoice_id=?',(p['invoice_id'],)).fetchone()
            if not dte: raise RuntimeError(f'Pago {p["id"]} huérfano')
            c.execute('''INSERT OR IGNORE INTO payment_allocations(payment_id,dte_id,allocated_amount,confidence,allocation_type,created_at) VALUES(?,?,?,?,?,?)''',(p['id'],dte['id'],int(p['amount'] or 0),float(p['confidence'] or 0),'MIGRATED',now))
        c.execute('''UPDATE dte_app_state SET paid_amount=COALESCE((SELECT SUM(pa.allocated_amount) FROM payment_allocations pa WHERE pa.dte_id=dte_app_state.dte_id),0),updated_at=?''',(now,))
        for r in c.execute('''SELECT s.dte_id,s.collection_status,s.paid_amount,d.total_amount FROM dte_app_state s JOIN dte_documents d ON d.id=s.dte_id''').fetchall():
            if r['collection_status']=='NO_COBRAR': continue
            paid,total=int(r['paid_amount'] or 0),int(r['total_amount'] or 0)
            st='PAGADA' if paid>=total and total>0 else ('PARCIAL' if paid>0 else 'PENDIENTE')
            c.execute('UPDATE dte_app_state SET collection_status=?,updated_at=? WHERE dte_id=?',(st,now,r['dte_id']))
        v={}
        v['old_invoice_count']=c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
        v['new_dte_count']=c.execute('SELECT COUNT(*) FROM dte_documents WHERE legacy_invoice_id IS NOT NULL').fetchone()[0]
        v['old_invoice_total']=c.execute('SELECT COALESCE(SUM(total),0) FROM invoices').fetchone()[0]
        v['new_invoice_total']=c.execute('SELECT COALESCE(SUM(total_amount),0) FROM dte_documents WHERE legacy_invoice_id IS NOT NULL').fetchone()[0]
        v['old_reconciled_count']=c.execute('SELECT COUNT(*) FROM payments WHERE invoice_id IS NOT NULL').fetchone()[0]
        v['new_allocation_count']=c.execute("SELECT COUNT(*) FROM payment_allocations WHERE allocation_type='MIGRATED'").fetchone()[0]
        v['old_reconciled_total']=c.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE invoice_id IS NOT NULL').fetchone()[0]
        v['new_allocation_total']=c.execute("SELECT COALESCE(SUM(allocated_amount),0) FROM payment_allocations WHERE allocation_type='MIGRATED'").fetchone()[0]
        v['orphan_allocations']=c.execute('''SELECT COUNT(*) FROM payment_allocations pa LEFT JOIN payments p ON p.id=pa.payment_id LEFT JOIN dte_documents d ON d.id=pa.dte_id WHERE p.id IS NULL OR d.id IS NULL''').fetchone()[0]
        if not (v['old_invoice_count']==v['new_dte_count'] and v['old_invoice_total']==v['new_invoice_total'] and v['old_reconciled_count']==v['new_allocation_count'] and v['old_reconciled_total']==v['new_allocation_total'] and v['orphan_allocations']==0):
            raise RuntimeError('Validación crítica falló: '+json.dumps(v,ensure_ascii=False))
        c.commit(); v['integrity_after']=c.execute('PRAGMA integrity_check').fetchone()[0]; v['backup_path']=str(backup); v['success']=v['integrity_after']=='ok'; c.close(); return v
    except Exception:
        c.rollback(); c.close(); raise

def status(db_path):
    c=sqlite3.connect(db_path); c.row_factory=sqlite3.Row
    tables={x['name'] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'dte_documents' not in tables: c.close(); return {'migrated':False}
    r={'migrated':True,'dte_count':c.execute('SELECT COUNT(*) FROM dte_documents').fetchone()[0],'app_state_count':c.execute('SELECT COUNT(*) FROM dte_app_state').fetchone()[0],'allocation_count':c.execute('SELECT COUNT(*) FROM payment_allocations').fetchone()[0]}
    c.close(); return r
