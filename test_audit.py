import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import audit
import pgddl

class AuditTests(unittest.TestCase):
    def data(self):
        return {'registered':True,'total':3,'migrations':[], 'applied':[
            dict(step=1,line=1,kind='CREATE INDEX',sql='<script>alert(1)</script>',mode='executed',ms=123,detail={'lock_attempts':3,'rebuilt_views':4,'view_backup':'/tmp/a'},stamp='2026'),
            dict(step=2,line=2,kind='CREATE TABLE',sql='CREATE TABLE t(x int)',mode='acknowledged',ms=0,detail={'reason':'checked'},stamp='2026')],
            'errors':[dict(step=i,state='0A000',message='error '+str(i),stamp='2026') for i in (1,1,2,3)]}
    def test_classification(self):
        d=self.data(); s=audit.summarize(d)
        self.assertEqual((s['resolved'],s['manual'],s['unresolved'],s['recovered']),(2,1,1,1))
        self.assertEqual((s['retries'],s['views'],s['elapsed_ms']),(2,4,123))
        self.assertEqual(s['status'],'PENDENTE')
    def test_complete_ack(self):
        d=self.data();d['total']=2;d['errors'].pop()
        self.assertEqual(audit.summarize(d)['status'],'CONCLUÍDA COM RECONHECIMENTOS MANUAIS')
    def test_complete_success(self):
        d=self.data();d['total']=1;d['applied'].pop();d['errors']=d['errors'][:2]
        s=audit.summarize(d);self.assertEqual(s['status'],'CONCLUÍDA COM SUCESSO');self.assertEqual(s['unresolved'],0)
    def test_no_history(self):
        d=dict(registered=False,total=1,applied=[],errors=[],migrations=[])
        self.assertEqual(audit.summarize(d)['status'],'NÃO REGISTRADA')
    def test_status_history_filter(self):
        d=self.data();s=audit.summarize(d);m=SimpleNamespace(steps=[None,None,SimpleNamespace(number=3,line=3,sql='DDL')]);log=Mock()
        audit.status_log(log,d,s,m)
        self.assertEqual(log.warning.call_count,1)
        self.assertNotIn('error 1',str(log.mock_calls))
        log.reset_mock();audit.status_log(log,d,s,m,True)
        self.assertIn('error 1',str(log.mock_calls));self.assertIn('RECONHECIDA MANUALMENTE',str(log.mock_calls))
    def test_html_escape_and_unique_files(self):
        d=self.data();content=audit.render(d,audit.summarize(d),{'Banco':'<b>'})
        self.assertNotIn('<script>',content);self.assertIn('&lt;script&gt;',content);self.assertIn('&lt;b&gt;',content)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(audit.write_report(content,tmp));q=Path(audit.write_report(content,tmp));self.assertNotEqual(p,q)
            self.assertEqual(p.stat().st_mode & 0o777,0o600);self.assertEqual(p.read_text(),content)
    def test_session_settings(self):
        conn=Mock();conn.execute.return_value.fetchall.return_value=[('maintenance_work_mem','512MB')]
        args=SimpleNamespace(parallel_maintenance_workers=4,maintenance_work_mem='512MB')
        audit.configure(conn,args,Mock());self.assertEqual(conn.execute.call_args_list[0].args[1],('max_parallel_maintenance_workers','4'))
        self.assertEqual(conn.execute.call_args_list[1].args[1],('maintenance_work_mem','512MB'))
        self.assertNotIn('ALTER SYSTEM',str(conn.mock_calls))
    def test_session_default_untouched(self):
        conn=Mock();conn.execute.return_value.fetchall.return_value=[]
        audit.configure(conn,SimpleNamespace(parallel_maintenance_workers=None,maintenance_work_mem=None),Mock())
        self.assertEqual(conn.execute.call_count,1)
    def test_validation_last_type_and_sequences(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'x.ddl';f.write_text('ALTER TABLE t ADD x varchar(20); ALTER TABLE t ALTER COLUMN x TYPE varchar(40); CREATE SEQUENCE s; ALTER SEQUENCE s RESTART WITH 1;')
            m=pgddl.load_manifest(f)
        conn=Mock();conn.execute.return_value.fetchone.side_effect=[(1,'r'),('character varying(40)',),(2,'S'),(2,'S')]
        rows=audit.validate(conn,m,[{'step':i} for i in range(1,5)],'public',pgddl.target,pgddl.canonical_name,pgddl.column_request,pgddl.index_request)
        self.assertEqual([(r['step'],r['status']) for r in rows],[(2,'OK'),(4,'PARCIAL')]);self.assertNotIn('nextval',str(conn.mock_calls))
    def test_index_validation(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'x.ddl';f.write_text('CREATE INDEX ix ON t(x); CREATE UNIQUE INDEX iy ON t(y);');m=pgddl.load_manifest(f)
        conn=Mock();conn.execute.return_value.fetchone.side_effect=[(1,'r'),(False,True,'btree',True,['x']),(True,False,'btree',True,['y'])]
        rows=audit.validate(conn,m,[{'step':1},{'step':2}],'public',pgddl.target,pgddl.canonical_name,pgddl.column_request,pgddl.index_request)
        self.assertEqual([r['status'] for r in rows],['OK','DIVERGÊNCIA'])

if __name__=='__main__': unittest.main()
